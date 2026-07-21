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

1. Create / search / rename / archive / branch / reload / delete entirely
   through actor-scoped APIs → `test_full_conversation_lifecycle_through_the_actor_api`.
2. Cross-actor enumeration and mutation fail without revealing existence →
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
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from workbench.api import create_app
from workbench.config import Settings
from workbench.conversation_store import MemoryConversationStore

# Hermetic fixture key for the server-keyed content fingerprint (PRD R008);
# production sources the key from WORKBENCH_CHAT_HASH_KEY instead.
KEY = "integration-content-hash-key-32b!"

OWNER = "operator"
OTHER = "reviewer"


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

        # the full read returns every turn in lineage order; committed history intact
        full = client.get(f"/api/conversations/{conversation_id}", headers=_as(OWNER)).json()
        assert [turn["id"] for turn in full["turns"]] == [
            root["id"], reply["id"], retried["id"], branched["id"],
        ]
        assert all(turn["committed"] for turn in full["turns"])

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

        # Every cross-actor mutation/read is byte-identical to a truly missing id.
        turn_body = {"role": "assistant", "status": "complete"}

        def probes(target: str):
            prefix = f"/api/conversations/{target}"
            return [
                ("GET", prefix, None),
                ("POST", f"{prefix}/rename", {"title": "hijack"}),
                ("POST", f"{prefix}/archive", None),
                ("POST", f"{prefix}/delete", {"mode": "purge_all_records"}),
                ("POST", f"{prefix}/turns/{turn_id}/retry", turn_body),
                ("POST", f"{prefix}/turns/{turn_id}/branch", turn_body),
                ("POST", f"{prefix}/turns/{turn_id}/status", {"status": "complete"}),
            ]

        missing_id = "conv_does_not_exist0"
        for (method, url, body), (_, missing_url, missing_body) in zip(probes(conversation_id), probes(missing_id)):
            foreign = client.request(method, url, json=body, headers=_as(OTHER))
            missing = client.request(method, missing_url, json=missing_body, headers=_as(OWNER))
            assert foreign.status_code == 404, (url, foreign.text)
            assert missing.status_code == 404, (missing_url, missing.text)
            assert foreign.content == missing.content  # no existence oracle

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
