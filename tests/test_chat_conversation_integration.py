"""End-to-end integration of durable conversation storage (chat-first-voice T002).

This fixture integrates the already-implemented chat-conversation slices
(`workbench.conversation_models`, `conversation_store`, `conversation_api`,
`idempotency_store`) and qualifies the FULL conversation lifecycle through the
actor-scoped HTTP surface that `create_app` mounts — never through store
internals where a public API exists.  The one exception is the content-free
audit stream, which by contract has NO actor-facing endpoint (audit is
hub-internal lifecycle metadata); it is inspected through the injected store's
`list_audit`, which is the only way to prove the audit shape.

A single durable `MemoryConversationStore` is injected so the same persisted
rows can be reopened by a fresh hub (a simulated restart) to qualify reload
recovery, and so the hub-internal audit can be read after the HTTP lifecycle.

Acceptance-criteria map (each criterion → the test that binds it):

1. The FULL lifecycle on ONE conversation entirely through actor-scoped APIs —
   create / append / rename+search / retry / branch / streaming-status advance /
   pin+unpin / tag add+remove / folder set+clear / mid-test reload over the same
   rows / archive+unarchive / delete → `test_full_conversation_lifecycle_through_the_actor_api`.
   (This binds strictly MORE than `test_conversation_api.test_full_conversation_
   lifecycle_through_the_api`: it adds the pin/tag/folder organization verbs and
   an in-lifecycle reload the unit test never exercises, and keeps the streaming
   advance + content_trust assertions the unit test has.)
2. Cross-actor enumeration and mutation of EVERY registered per-conversation-id
   endpoint (probe list derived from the app's route table, addition-proof) fail
   byte-identically to a missing id, revealing no existence →
   `test_cross_actor_enumeration_and_mutation_reveal_no_existence`.
3. Reload preserves committed turns, marks in-flight turns interrupted, keeps
   append-only branch/retry lineage → `test_reload_recovers_interrupted_and_preserves_lineage`.
4. Retention and deletion remove content while preserving only safe lifecycle
   metadata and the server-keyed content fingerprint; raw content digests are
   absent → `test_retention_and_deletion_remove_content_keep_safe_metadata`
   and `test_audit_stream_is_content_free_and_carries_only_the_keyed_fingerprint`.
"""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from workbench.api import create_app
from workbench.config import Settings
from workbench.conversation_store import MemoryConversationStore

# Hermetic fixture key for the server-keyed content fingerprint (PRD R008);
# production sources the key from WORKBENCH_CHAT_HASH_KEY instead.
KEY = "integration-content-hash-key-32b!"

OWNER = "operator"
OTHER = "reviewer"

# Independent re-derivation of the keyed turn-content fingerprint.  This test
# module deliberately does NOT call `conversation_models.turn_content_hash` (that
# would be a tautology — the code under test verifying itself).  Instead we
# reconstruct, from first principles, the exact bytes it HMACs:
#   * the canonical JSON of a sorted list of {content_trust, kind, text} blocks
#     (string keys, sorted, compact separators, UTF-8 — the documented canonical
#     contract encoding), and
#   * the domain-separation prefix, spelled out here as a literal.
# The prefix literal is the crux: it binds domain separation (kill-mutation
# `_CHAT_CONTENT_PREFIX` removal).  If the code drops the prefix, its stored hash
# no longer matches this independently-prefixed expectation and the test fails.
_CHAT_CONTENT_PREFIX_LITERAL = b"anvil-workbench/chat-turn-content/v1\0"
_CONTENT_TRUST_LITERAL = "untrusted_task_data"


def _expected_turn_content_hash(key: str, blocks: list[dict]) -> str:
    payload = [
        {"content_trust": _CONTENT_TRUST_LITERAL, "kind": block["kind"], "text": block["text"]}
        for block in blocks
    ]
    canonical = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    digest = hmac.new(key.encode("utf-8"), _CHAT_CONTENT_PREFIX_LITERAL + canonical, hashlib.sha256)
    return "hmac-sha256:" + digest.hexdigest()


def _settings(**overrides) -> Settings:
    values = dict(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner=OWNER, approvers=frozenset({OWNER, OTHER}), bridge_bootstrap_token="",
        anvil_router_base_url="", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=False,
        chat_content_hash_key=KEY,
    )
    values.update(overrides)
    return Settings(**values)


def _durable_store() -> MemoryConversationStore:
    return MemoryConversationStore(content_hash_key=KEY.encode("utf-8"))


def _client(store: MemoryConversationStore) -> TestClient:
    from workbench.graph import NullGraph
    from workbench.store import MemoryStore

    return TestClient(create_app(
        settings=_settings(), store=MemoryStore(), graph=NullGraph(), conversation_store=store,
    ))


def _as(actor: str) -> dict[str, str]:
    return {"X-Workbench-Actor": actor}


def _create(client: TestClient, actor: str = OWNER, title: str = "Kickoff") -> dict:
    response = client.post("/api/conversations", json={"title": title}, headers=_as(actor))
    assert response.status_code == 201, response.text
    return response.json()


def _append_root(client: TestClient, conversation_id: str, actor: str = OWNER, text: str = "hello") -> dict:
    response = client.post(f"/api/conversations/{conversation_id}/turns", json={
        "role": "user", "status": "complete",
        "lineage": {"parent_turn_id": None, "sibling_index": 0, "kind": "initial"},
        "content": [{"kind": "text", "text": text}],
    }, headers=_as(actor))
    assert response.status_code == 201, response.text
    return response.json()


# --- Criterion 1: the complete lifecycle through actor-scoped APIs -----------


def test_full_conversation_lifecycle_through_the_actor_api():
    store = _durable_store()
    with _client(store) as client:
        # create
        conversation = _create(client, title="Voice kickoff")
        conversation_id = conversation["id"]
        assert conversation["status"] == "active"

        # append the root user turn and a complete assistant reply
        root = _append_root(client, conversation_id, text="plan the demo")
        reply = client.post(f"/api/conversations/{conversation_id}/turns", json={
            "role": "assistant", "status": "complete",
            "lineage": {"parent_turn_id": root["id"], "sibling_index": 0, "kind": "branch"},
            "content": [{"kind": "text", "text": "here is the plan"}],
        }, headers=_as(OWNER)).json()
        assert reply["lineage"]["parent_turn_id"] == root["id"]

        # rename, then find it by the new title (search)
        renamed = client.post(
            f"/api/conversations/{conversation_id}/rename",
            json={"title": "Voice kickoff (renamed)"}, headers=_as(OWNER),
        )
        assert renamed.status_code == 200 and renamed.json()["title"] == "Voice kickoff (renamed)"
        found = client.get(
            "/api/conversations/search", params={"query": "renamed"}, headers=_as(OWNER),
        ).json()["conversations"]
        assert [item["id"] for item in found] == [conversation_id]

        # retry the reply → a new sibling under the same parent, history untouched
        retried = client.post(
            f"/api/conversations/{conversation_id}/turns/{reply['id']}/retry",
            json={"role": "assistant", "status": "complete", "content": [{"kind": "text", "text": "better plan"}]},
            headers=_as(OWNER),
        ).json()
        assert retried["lineage"] == {"parent_turn_id": root["id"], "sibling_index": 1, "kind": "retry"}

        # branch a follow-up user turn under the retried reply
        branched = client.post(
            f"/api/conversations/{conversation_id}/turns/{retried['id']}/branch",
            json={"role": "user", "status": "complete", "content": [{"kind": "text", "text": "go deeper"}]},
            headers=_as(OWNER),
        ).json()
        assert branched["lineage"]["kind"] == "branch"

        # a streaming assistant turn is advanced to a terminal state through the
        # /status verb (the streaming-status advance the unit lifecycle covers).
        streaming = client.post(f"/api/conversations/{conversation_id}/turns", json={
            "role": "assistant", "status": "streaming",
            "lineage": {"parent_turn_id": branched["id"], "sibling_index": 0, "kind": "branch"},
        }, headers=_as(OWNER)).json()
        assert streaming["completed_at"] is None and streaming["committed"] is False
        advanced = client.post(
            f"/api/conversations/{conversation_id}/turns/{streaming['id']}/status",
            json={"status": "complete"}, headers=_as(OWNER),
        )
        assert advanced.status_code == 200 and advanced.json()["committed"] is True

        # the full read returns every turn in lineage order; committed history intact,
        # and each block still carries the untrusted-task-data trust badge.
        full = client.get(f"/api/conversations/{conversation_id}", headers=_as(OWNER)).json()
        assert [turn["id"] for turn in full["turns"]] == [
            root["id"], reply["id"], retried["id"], branched["id"], streaming["id"],
        ]
        assert all(turn["committed"] for turn in full["turns"])
        assert full["turns"][0]["content"] == [
            {"kind": "text", "text": "plan the demo", "content_trust": "untrusted_task_data"},
        ]

        # Organization verbs (chat-first-voice T011) through the actor API:
        # pin then unpin, tag add then remove, folder set then clear — each
        # reflected in the conversation projection and the list filters.
        assert client.post(f"/api/conversations/{conversation_id}/pin", headers=_as(OWNER)).json()["pinned"] is True
        pinned_only = client.get(
            "/api/conversations", params={"pinned": "true"}, headers=_as(OWNER),
        ).json()["conversations"]
        assert [item["id"] for item in pinned_only] == [conversation_id]
        assert client.post(f"/api/conversations/{conversation_id}/unpin", headers=_as(OWNER)).json()["pinned"] is False
        assert client.get(
            "/api/conversations", params={"pinned": "true"}, headers=_as(OWNER),
        ).json()["conversations"] == []

        assert client.post(
            f"/api/conversations/{conversation_id}/tags", json={"tag": "demo"}, headers=_as(OWNER),
        ).json()["tags"] == ["demo"]
        tagged = client.get(
            "/api/conversations", params={"tag": "demo"}, headers=_as(OWNER),
        ).json()["conversations"]
        assert [item["id"] for item in tagged] == [conversation_id]
        assert client.post(
            f"/api/conversations/{conversation_id}/tags/remove", json={"tag": "demo"}, headers=_as(OWNER),
        ).json()["tags"] == []

        assert client.post(
            f"/api/conversations/{conversation_id}/folder", json={"folder": "planning"}, headers=_as(OWNER),
        ).json()["folder"] == "planning"
        foldered = client.get(
            "/api/conversations", params={"folder": "planning"}, headers=_as(OWNER),
        ).json()["conversations"]
        assert [item["id"] for item in foldered] == [conversation_id]
        assert client.post(
            f"/api/conversations/{conversation_id}/folder/clear", headers=_as(OWNER),
        ).json()["folder"] is None

    # MID-TEST RELOAD: a fresh hub over the SAME persisted rows (a simulated
    # restart) must serve the committed lifecycle unchanged — every turn already
    # advanced to a terminal state stays complete, none is lost, order holds.
    reloaded = MemoryConversationStore(
        store.rows, content_hash_key=KEY.encode("utf-8"), recover_on_open=True,
    )
    with _client(reloaded) as client:
        reread = client.get(f"/api/conversations/{conversation_id}", headers=_as(OWNER)).json()
        assert [turn["id"] for turn in reread["turns"]] == [
            root["id"], reply["id"], retried["id"], branched["id"], streaming["id"],
        ]
        assert all(turn["status"] == "complete" and turn["committed"] for turn in reread["turns"])

        # archive hides it from the default list; the archived filter shows it; unarchive restores
        assert client.post(
            f"/api/conversations/{conversation_id}/archive", headers=_as(OWNER),
        ).json()["status"] == "archived"
        assert client.get("/api/conversations", headers=_as(OWNER)).json()["conversations"] == []
        archived = client.get(
            "/api/conversations", params={"include_archived": "true"}, headers=_as(OWNER),
        ).json()["conversations"]
        assert [item["id"] for item in archived] == [conversation_id]
        assert client.post(
            f"/api/conversations/{conversation_id}/unarchive", headers=_as(OWNER),
        ).json()["status"] == "active"

        # tombstone deletion keeps identity plus content-free tombstone turns
        deleted = client.post(
            f"/api/conversations/{conversation_id}/delete",
            json={"mode": "purge_content_keep_tombstone"}, headers=_as(OWNER),
        )
        assert deleted.status_code == 200
        assert deleted.json()["status"] == "deleted"
        assert deleted.json()["deletion_mode"] == "purge_content_keep_tombstone"

        # full-purge deletion removes a second conversation entirely
        other = _create(client, title="short-lived")
        client.post(
            f"/api/conversations/{other['id']}/delete",
            json={"mode": "purge_all_records"}, headers=_as(OWNER),
        )
        assert client.get(f"/api/conversations/{other['id']}", headers=_as(OWNER)).status_code == 404


# --- Criterion 2: cross-actor isolation with no existence leak ---------------


# The prefix every per-conversation-id endpoint shares.  The probe list below is
# DERIVED from the app's registered routes under this prefix, not hand-listed, so
# a newly-added endpoint is probed automatically or the test fails loudly — the
# oracle surface can never silently grow past its cross-actor coverage.
_CONVERSATION_ID_PREFIX = "/api/conversations/{conversation_id}"

# A minimal, pydantic-valid body for each per-conversation-id endpoint, keyed by
# the route's path suffix.  Ownership (`_owned`) is checked strictly before any
# body-derived validation, so a foreign/missing id fails closed at the ownership
# gate with the fixed 404 body regardless of these values — but they must still
# parse, or the two arms would diverge at a 422 instead of meeting at the 404.
_PROBE_BODY_BY_SUFFIX: dict[str, dict | None] = {
    "": None,  # GET the conversation
    "/rename": {"title": "hijack"},
    "/archive": None,
    "/unarchive": None,
    "/pin": None,
    "/unpin": None,
    "/tags": {"tag": "hijacktag"},
    "/tags/remove": {"tag": "hijacktag"},
    "/folder": {"folder": "hijackfolder"},
    "/folder/clear": None,
    "/delete": {"mode": "purge_all_records"},
    # The live send/stream join: ownership is checked before route/serving, so a
    # foreign or missing id is the fixed 404 identically -- no existence oracle.
    "/send": {"route_id": "chat.heavy", "prompt": "hi", "controls": {}},
    "/turns": {"role": "assistant", "status": "complete"},
    "/turns/{turn_id}/retry": {"role": "assistant", "status": "complete"},
    "/turns/{turn_id}/branch": {"role": "assistant", "status": "complete"},
    "/turns/{turn_id}/status": {"status": "complete"},
}


def _flatten_routes(app) -> list:
    """Every leaf route the app serves, unwrapping the hub's ``_IncludedRouter``
    lazy-mount wrappers (whose real routes hang off ``original_router``) so the
    conversation router's endpoints are actually seen — not just the wrapper."""
    leaves: list = []

    def _walk(container) -> None:
        for route in getattr(container, "routes", []):
            wrapped = getattr(route, "original_router", None)
            if wrapped is not None:
                _walk(wrapped)
            else:
                leaves.append(route)

    _walk(app)
    return leaves


def _derive_conversation_id_probes(app) -> list[tuple[str, str, dict | None]]:
    """Enumerate (method, path_template, body) for EVERY registered route under
    ``/api/conversations/{conversation_id}``.

    Addition-proof by construction: the probe set is the app's own route table,
    and a route whose suffix has no declared minimal body fails the test rather
    than being silently skipped — so a new per-conversation-id endpoint cannot
    land without cross-actor coverage.
    """
    probes: list[tuple[str, str, dict | None]] = []
    for route in _flatten_routes(app):
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods or not path.startswith(_CONVERSATION_ID_PREFIX):
            continue
        suffix = path[len(_CONVERSATION_ID_PREFIX):]
        if suffix not in _PROBE_BODY_BY_SUFFIX:
            pytest.fail(
                f"registered per-conversation-id route {path!r} has no cross-actor probe "
                f"(unknown suffix {suffix!r}); add a minimal body to _PROBE_BODY_BY_SUFFIX",
            )
        for method in sorted(methods - {"HEAD", "OPTIONS"}):
            probes.append((method, path, _PROBE_BODY_BY_SUFFIX[suffix]))
    return probes


def test_cross_actor_enumeration_and_mutation_reveal_no_existence():
    store = _durable_store()
    with _client(store) as client:
        conversation_id = _create(client, actor=OWNER, title="private plans")["id"]
        turn_id = _append_root(client, conversation_id, actor=OWNER, text="secret content")["id"]

        # Enumeration: the other actor sees nothing and search is scoped to them.
        assert client.get("/api/conversations", headers=_as(OTHER)).json()["conversations"] == []
        assert client.get(
            "/api/conversations/search", params={"query": "private"}, headers=_as(OTHER),
        ).json()["conversations"] == []

        # Derive one probe per registered per-conversation-id route+method.
        probes = _derive_conversation_id_probes(client.app)
        probed_suffixes = {
            template[len(_CONVERSATION_ID_PREFIX):] for _method, template, _body in probes
        }
        # Every currently-implemented endpoint must be in the derived set — this
        # is the concrete floor beneath the addition-proof derivation, and it
        # includes the three (unpin / tags-remove / folder-clear) that previously
        # had ZERO cross-actor coverage anywhere in the suite.
        assert probed_suffixes == set(_PROBE_BODY_BY_SUFFIX), probed_suffixes
        missing_id = "conv_does_not_exist0"

        def _url(template: str, target: str) -> str:
            return template.replace("{conversation_id}", target).replace("{turn_id}", turn_id)

        for method, template, body in probes:
            foreign = client.request(method, _url(template, conversation_id), json=body, headers=_as(OTHER))
            missing = client.request(method, _url(template, missing_id), json=body, headers=_as(OWNER))
            assert foreign.status_code == 404, (template, foreign.text)
            assert missing.status_code == 404, (template, missing.text)
            # Raw bytes + status are byte-identical: a foreign id is indistinguishable
            # from a nonexistent one, so there is no cross-actor existence oracle.
            assert foreign.content == missing.content, template
            assert foreign.status_code == missing.status_code, template

        # None of the refused foreign mutations touched the owner's record.
        owner_read = client.get(f"/api/conversations/{conversation_id}", headers=_as(OWNER)).json()
        assert owner_read["conversation"]["status"] == "active"
        assert len(owner_read["turns"]) == 1


# --- Criterion 3: reload recovery + append-only lineage preserved ------------


def test_reload_recovers_interrupted_and_preserves_lineage():
    durable = _durable_store()
    with _client(durable) as client:
        conversation_id = _create(client, title="reloaded")["id"]
        root = _append_root(client, conversation_id, text="committed text")
        # A retry sibling: append-only branch/retry lineage that must survive reload.
        retried = client.post(f"/api/conversations/{conversation_id}/turns", json={
            "role": "assistant", "status": "complete",
            "lineage": {"parent_turn_id": root["id"], "sibling_index": 0, "kind": "branch"},
            "content": [{"kind": "text", "text": "reply"}],
        }, headers=_as(OWNER)).json()
        # An in-flight streaming turn that will be cut off by the "restart".
        streaming = client.post(f"/api/conversations/{conversation_id}/turns", json={
            "role": "assistant", "status": "streaming",
            "lineage": {"parent_turn_id": retried["id"], "sibling_index": 0, "kind": "branch"},
        }, headers=_as(OWNER)).json()
        assert streaming["status"] == "streaming" and streaming["completed_at"] is None

    # A fresh hub over the SAME persisted rows recovers on open (simulated restart).
    restarted = MemoryConversationStore(
        durable.rows, content_hash_key=KEY.encode("utf-8"), recover_on_open=True,
    )
    with _client(restarted) as client:
        turns = client.get(f"/api/conversations/{conversation_id}", headers=_as(OWNER)).json()["turns"]
        by_id = {turn["id"]: turn for turn in turns}
        # Committed turns preserved exactly.
        assert by_id[root["id"]]["status"] == "complete" and by_id[root["id"]]["committed"] is True
        assert by_id[retried["id"]]["status"] == "complete"
        # Append-only branch/retry lineage retained across the reload.
        assert by_id[retried["id"]]["lineage"]["parent_turn_id"] == root["id"]
        assert by_id[streaming["id"]]["lineage"]["parent_turn_id"] == retried["id"]
        # The in-flight turn is surfaced interrupted, never silently completed.
        recovered = by_id[streaming["id"]]
        assert recovered["status"] == "interrupted"
        assert recovered["interrupted"] is True and recovered["committed"] is False
        assert recovered["completed_at"] is not None


# --- Criterion 4: retention/deletion remove content, keep safe metadata ------


def test_retention_and_deletion_remove_content_keep_safe_metadata():
    store = _durable_store()
    with _client(store) as client:
        # An expired conversation (delete_after in the past) for the batched pass.
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        expired = client.post("/api/conversations", json={
            "title": "expired secret", "retention": {"delete_after": past},
        }, headers=_as(OWNER)).json()
        _append_root(client, expired["id"], text="expired secret content")

        # The operator-only preview is content-free (ids/counts/timestamps only)
        # and names the expired conversation without deleting anything.
        preview = client.get("/api/hub/retention/preview", headers=_as(OWNER)).json()["preview"]
        scoped = [row for row in preview if row["conversation_id"] == expired["id"]]
        assert scoped and scoped[0]["reason"] == "retention_expired"
        serialized_preview = str(preview)
        assert "expired secret" not in serialized_preview  # no title, no content
        # A read did NOT expire it — it is still live before the explicit pass.
        assert client.get(f"/api/conversations/{expired['id']}", headers=_as(OWNER)).status_code == 200

        # The explicit batched enforce pass tombstones the expired conversation.
        enforced = client.post("/api/hub/retention/enforce", headers=_as(OWNER)).json()["enforced"]
        assert expired["id"] in [row["conversation_id"] for row in enforced]
        tomb = client.get(f"/api/conversations/{expired['id']}", headers=_as(OWNER)).json()
        assert tomb["conversation"]["status"] == "deleted"
        assert tomb["conversation"]["title"] is None
        assert all(turn["content"] == [] and turn["content_purged"] for turn in tomb["turns"])
        assert "expired secret content" not in client.get(
            f"/api/conversations/{expired['id']}", headers=_as(OWNER),
        ).text
        # Direct durable-removal proof: the content is gone from the persisted
        # rows themselves, not merely absent from the HTTP projection.  Neither
        # the transcript body nor the (now-null) title survives in the store.
        assert "expired secret content" not in repr(store.rows)
        assert "expired secret" not in repr(store.rows)

        # Explicit tombstone deletion of a live conversation removes content too.
        live = _create(client, title="doomed")["id"]
        _append_root(client, live, text="the actual secret words")
        client.post(
            f"/api/conversations/{live}/delete",
            json={"mode": "purge_content_keep_tombstone"}, headers=_as(OWNER),
        )
        tombstone_read = client.get(f"/api/conversations/{live}", headers=_as(OWNER))
        assert tombstone_read.status_code == 200
        assert "the actual secret words" not in tombstone_read.text
        assert "doomed" not in tombstone_read.text
        # The keyed fingerprint is never serialized to the actor either.
        assert "content_hash" not in tombstone_read.text
        assert "hmac-sha256" not in tombstone_read.text
        # Direct durable-removal proof for the explicit tombstone path too: the
        # secret words and the title are absent from the persisted rows.
        assert "the actual secret words" not in repr(store.rows)
        assert "doomed" not in repr(store.rows)


def test_audit_stream_is_content_free_and_carries_only_the_keyed_fingerprint():
    store = _durable_store()
    secret = "top-secret transcript body"
    with _client(store) as client:
        conversation_id = _create(client, title="audited chat")["id"]
        _append_root(client, conversation_id, text=secret)
        client.post(
            f"/api/conversations/{conversation_id}/delete",
            json={"mode": "purge_content_keep_tombstone"}, headers=_as(OWNER),
        )

    # The audit stream is hub-internal (no actor endpoint by contract), so it is
    # read through the injected store — the only surface that exposes the shape.
    audit = store.list_audit(limit=100)
    assert audit, "the lifecycle must have produced audit events"

    # Every audit record is content-free: neither the title nor the transcript
    # text, nor a raw (unkeyed) sha256 digest of the content, appears anywhere.
    raw_content_digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    turn_audits = []
    for event in audit:
        blob = str(dataclasses.asdict(event.record))
        assert secret not in blob
        assert "audited chat" not in blob
        assert raw_content_digest not in blob  # no raw content digest, keyed only
        if hasattr(event.record, "content_hash"):
            turn_audits.append(event.record)

    # The server-keyed content fingerprint IS retained where explicitly required
    # (the turn audit), and it is a keyed HMAC — never a bare sha256:<hex>.
    assert turn_audits, "turn appends must be audited with the keyed fingerprint"
    for turn_audit_record in turn_audits:
        assert turn_audit_record.content_hash.startswith("hmac-sha256:")
        assert not turn_audit_record.content_hash.startswith("sha256:")

    # Domain-separation binding, verified INDEPENDENTLY: the stored fingerprint of
    # the appended turn equals the HMAC we recompute from first principles with the
    # domain-separation prefix spelled out as a literal (never via
    # `turn_content_hash`).  This kills the mutation that drops
    # `_CHAT_CONTENT_PREFIX`: without the prefix, the code's stored hash diverges
    # from this independently-prefixed expectation and the equality below fails.
    append_audits = [
        event.record for event in audit
        if event.kind == "turn.appended" and hasattr(event.record, "content_hash")
    ]
    assert len(append_audits) == 1, "exactly one turn was appended in this lifecycle"
    expected = _expected_turn_content_hash(KEY, [{"kind": "text", "text": secret}])
    assert append_audits[0].content_hash == expected
    # Guard the guard: a different content produces a different fingerprint, so the
    # equality above is content-sensitive, not a constant that any string matches.
    assert _expected_turn_content_hash(KEY, [{"kind": "text", "text": secret + "!"}]) != expected
