"""Actor-scoped conversation/turn API (chat-first-voice T002.4).

Each test group maps to an acceptance criterion:

1. Every endpoint derives the actor from the trusted request context (the
   identity header) only; a body or query ``actor`` field is rejected or
   ignored, and an unauthenticated or non-allowlisted request is refused.
2. The management, append, and branch/retry endpoints cover the complete
   durable-chat lifecycle end-to-end through HTTP.
3. A cross-actor probe on every conversation endpoint returns the byte-equal
   404 body of a truly missing id — no existence leak.
4. Turn responses expose truthful committed/interrupted state and lineage
   pointers.

Plus the negative surface: no hub-internal endpoint (audit/recovery/retention)
is exposed, deleted conversations return content-free tombstone shapes, no
response ever carries the keyed content fingerprint, and a hub without the
content-hash key refuses chat endpoints instead of serving.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from workbench.api import create_app
from workbench.config import Settings
from workbench.conversation_api import conversation_actor
from workbench.conversation_store import MemoryConversationStore
from workbench.graph import NullGraph
from workbench.store import MemoryStore

# Hermetic fixture key for the server-keyed content fingerprint (PRD R008);
# production sources the key from WORKBENCH_CHAT_HASH_KEY instead.
KEY = "api-test-content-hash-key-32byte"

OWNER = "operator"
OTHER = "reviewer"


def settings(**overrides) -> Settings:
    values = dict(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner=OWNER, approvers=frozenset({OWNER, OTHER}), bridge_bootstrap_token="",
        anvil_router_base_url="", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=False,
        chat_content_hash_key=KEY,
    )
    values.update(overrides)
    return Settings(**values)


def client(conversation_store: MemoryConversationStore | None = None, **overrides) -> TestClient:
    return TestClient(create_app(
        settings=settings(**overrides), store=MemoryStore(), graph=NullGraph(),
        conversation_store=conversation_store,
    ))


def as_actor(name: str) -> dict[str, str]:
    return {"X-Workbench-Actor": name}


def create_conversation(test_client: TestClient, actor: str = OWNER, title: str = "Kickoff chat") -> dict:
    response = test_client.post("/api/conversations", json={"title": title}, headers=as_actor(actor))
    assert response.status_code == 201, response.text
    return response.json()


def append_root(test_client: TestClient, conversation_id: str, actor: str = OWNER, text: str = "hello") -> dict:
    response = test_client.post(f"/api/conversations/{conversation_id}/turns", json={
        "role": "user", "status": "complete",
        "lineage": {"parent_turn_id": None, "sibling_index": 0, "kind": "initial"},
        "content": [{"kind": "text", "text": text}],
    }, headers=as_actor(actor))
    assert response.status_code == 201, response.text
    return response.json()


# --- Criterion 1: the actor comes from the trusted request context only -----


def test_actor_is_derived_from_the_identity_header_never_from_body_or_query():
    with client() as test_client:
        # A smuggled body actor field is rejected outright (unknown fields fail closed).
        smuggled = test_client.post(
            "/api/conversations", json={"title": "mine", "actor": OTHER}, headers=as_actor(OWNER),
        )
        assert smuggled.status_code == 422

        # A smuggled query actor parameter is ignored: the conversation is owned
        # by the header identity, and the named query actor cannot see it.
        created = test_client.post(
            "/api/conversations?actor=" + OTHER, json={"title": "mine"}, headers=as_actor(OWNER),
        )
        assert created.status_code == 201
        owner_list = test_client.get("/api/conversations", headers=as_actor(OWNER)).json()["conversations"]
        other_list = test_client.get("/api/conversations", headers=as_actor(OTHER)).json()["conversations"]
        assert [item["id"] for item in owner_list] == [created.json()["id"]]
        assert other_list == []


def test_unauthenticated_and_non_allowlisted_requests_are_refused():
    with client() as test_client:
        assert test_client.get("/api/conversations").status_code == 401
        assert test_client.get("/api/conversations", headers=as_actor("intruder")).status_code == 403
        assert test_client.post("/api/conversations", json={}, headers=as_actor("intruder")).status_code == 403


# --- Criterion 2: the complete durable-chat lifecycle through the API -------


def test_full_conversation_lifecycle_through_the_api():
    with client() as test_client:
        conversation = create_conversation(test_client, title="Voice kickoff")
        conversation_id = conversation["id"]
        assert conversation["status"] == "active"
        assert conversation["retention"]["transcript_text"] == "retained_redacted"

        # Rename, then find it by the new title.
        renamed = test_client.post(
            f"/api/conversations/{conversation_id}/rename",
            json={"title": "Voice kickoff (renamed)"}, headers=as_actor(OWNER),
        )
        assert renamed.status_code == 200
        assert renamed.json()["title"] == "Voice kickoff (renamed)"
        found = test_client.get(
            "/api/conversations/search", params={"query": "renamed"}, headers=as_actor(OWNER),
        ).json()["conversations"]
        assert [item["id"] for item in found] == [conversation_id]

        # Append the root user turn and a complete assistant reply branch.
        root = append_root(test_client, conversation_id, text="plan the demo")
        reply = test_client.post(f"/api/conversations/{conversation_id}/turns", json={
            "role": "assistant", "status": "complete",
            "lineage": {"parent_turn_id": root["id"], "sibling_index": 0, "kind": "branch"},
            "content": [{"kind": "text", "text": "here is the plan"}],
        }, headers=as_actor(OWNER)).json()
        assert reply["lineage"]["parent_turn_id"] == root["id"]

        # Retry the reply: a new sibling under the same parent, history untouched.
        retried = test_client.post(
            f"/api/conversations/{conversation_id}/turns/{reply['id']}/retry",
            json={"role": "assistant", "status": "complete", "content": [{"kind": "text", "text": "better plan"}]},
            headers=as_actor(OWNER),
        )
        assert retried.status_code == 201
        assert retried.json()["lineage"] == {"parent_turn_id": root["id"], "sibling_index": 1, "kind": "retry"}

        # Branch a follow-up user turn under the retried reply.
        branched = test_client.post(
            f"/api/conversations/{conversation_id}/turns/{retried.json()['id']}/branch",
            json={"role": "user", "status": "complete", "content": [{"kind": "text", "text": "go deeper"}]},
            headers=as_actor(OWNER),
        )
        assert branched.status_code == 201
        assert branched.json()["lineage"]["kind"] == "branch"

        # Streaming assistant turn advanced to a terminal state.
        streaming = test_client.post(f"/api/conversations/{conversation_id}/turns", json={
            "role": "assistant", "status": "streaming",
            "lineage": {"parent_turn_id": branched.json()["id"], "sibling_index": 0, "kind": "branch"},
        }, headers=as_actor(OWNER)).json()
        assert streaming["completed_at"] is None
        advanced = test_client.post(
            f"/api/conversations/{conversation_id}/turns/{streaming['id']}/status",
            json={"status": "complete"}, headers=as_actor(OWNER),
        )
        assert advanced.status_code == 200
        assert advanced.json()["committed"] is True

        # The full read returns every turn in lineage order with content.
        full = test_client.get(f"/api/conversations/{conversation_id}", headers=as_actor(OWNER)).json()
        assert [turn["id"] for turn in full["turns"]] == [
            root["id"], reply["id"], retried.json()["id"], branched.json()["id"], streaming["id"],
        ]
        assert full["turns"][0]["content"] == [
            {"kind": "text", "text": "plan the demo", "content_trust": "untrusted_task_data"},
        ]

        # Archive hides it from the default list; the archived filter shows it.
        assert test_client.post(
            f"/api/conversations/{conversation_id}/archive", headers=as_actor(OWNER),
        ).json()["status"] == "archived"
        assert test_client.get("/api/conversations", headers=as_actor(OWNER)).json()["conversations"] == []
        archived_list = test_client.get(
            "/api/conversations", params={"include_archived": "true"}, headers=as_actor(OWNER),
        ).json()["conversations"]
        assert [item["id"] for item in archived_list] == [conversation_id]
        assert test_client.post(
            f"/api/conversations/{conversation_id}/unarchive", headers=as_actor(OWNER),
        ).json()["status"] == "active"

        # Tombstone deletion keeps identity plus content-free tombstone turns.
        deleted = test_client.post(
            f"/api/conversations/{conversation_id}/delete",
            json={"mode": "purge_content_keep_tombstone"}, headers=as_actor(OWNER),
        )
        assert deleted.status_code == 200
        assert deleted.json()["status"] == "deleted"
        assert deleted.json()["deletion_mode"] == "purge_content_keep_tombstone"

        # Full-purge deletion removes the second conversation entirely.
        other = create_conversation(test_client, title="short-lived")
        purge = test_client.post(
            f"/api/conversations/{other['id']}/delete",
            json={"mode": "purge_all_records"}, headers=as_actor(OWNER),
        )
        assert purge.status_code == 200
        assert test_client.get(f"/api/conversations/{other['id']}", headers=as_actor(OWNER)).status_code == 404


def test_append_bounds_and_contract_violations_fail_closed():
    with client() as test_client:
        conversation_id = create_conversation(test_client)["id"]
        append_root(test_client, conversation_id)
        # A second null-parent root violates the lineage invariant -> 409.
        second_root = test_client.post(f"/api/conversations/{conversation_id}/turns", json={
            "role": "user", "status": "complete",
            "lineage": {"parent_turn_id": None, "sibling_index": 1, "kind": "initial"},
        }, headers=as_actor(OWNER))
        assert second_root.status_code == 409
        # Out-of-bounds content is refused at the boundary -> 422.
        oversized = test_client.post(f"/api/conversations/{conversation_id}/turns", json={
            "role": "user", "status": "complete",
            "content": [{"kind": "text", "text": "x" * 20_001}],
        }, headers=as_actor(OWNER))
        assert oversized.status_code == 422
        unknown_role = test_client.post(f"/api/conversations/{conversation_id}/turns", json={
            "role": "system", "status": "complete",
        }, headers=as_actor(OWNER))
        assert unknown_role.status_code == 422


# --- Criterion 3: cross-actor probes are byte-equal to a missing id ---------


def test_cross_actor_probes_return_the_byte_equal_missing_id_response():
    with client() as test_client:
        conversation_id = create_conversation(test_client)["id"]
        turn_id = append_root(test_client, conversation_id)["id"]
        missing_id = "conv_does_not_exist"
        turn_body = {"role": "assistant", "status": "complete"}

        def probes(target: str) -> list:
            prefix = f"/api/conversations/{target}"
            return [
                ("GET", prefix, None),
                ("POST", f"{prefix}/rename", {"title": "hijacked"}),
                ("POST", f"{prefix}/archive", None),
                ("POST", f"{prefix}/unarchive", None),
                ("POST", f"{prefix}/delete", {"mode": "purge_all_records"}),
                ("POST", f"{prefix}/turns", {**turn_body, "lineage": {"parent_turn_id": turn_id, "sibling_index": 3, "kind": "branch"}}),
                ("POST", f"{prefix}/turns/{turn_id}/retry", turn_body),
                ("POST", f"{prefix}/turns/{turn_id}/branch", turn_body),
                ("POST", f"{prefix}/turns/{turn_id}/status", {"status": "complete"}),
            ]

        for (method, url, body), (_, missing_url, missing_body) in zip(probes(conversation_id), probes(missing_id)):
            foreign = test_client.request(method, url, json=body, headers=as_actor(OTHER))
            missing = test_client.request(method, missing_url, json=missing_body, headers=as_actor(OWNER))
            assert foreign.status_code == 404, (url, foreign.text)
            assert missing.status_code == 404, (missing_url, missing.text)
            assert foreign.content == missing.content

        # List and search scope silently to the caller; nothing was mutated.
        assert test_client.get("/api/conversations", headers=as_actor(OTHER)).json()["conversations"] == []
        assert test_client.get(
            "/api/conversations/search", params={"query": "Kickoff"}, headers=as_actor(OTHER),
        ).json()["conversations"] == []
        record = test_client.get(f"/api/conversations/{conversation_id}", headers=as_actor(OWNER)).json()
        assert record["conversation"]["status"] == "active"
        assert len(record["turns"]) == 1


# --- Criterion 4: truthful interruption state and lineage pointers ----------


def test_turn_responses_expose_truthful_state_and_lineage_pointers():
    with client() as test_client:
        conversation_id = create_conversation(test_client)["id"]
        root = append_root(test_client, conversation_id)
        assert root["committed"] is True and root["interrupted"] is False
        assert root["lineage"] == {"parent_turn_id": None, "sibling_index": 0, "kind": "initial"}

        streaming = test_client.post(f"/api/conversations/{conversation_id}/turns", json={
            "role": "assistant", "status": "streaming",
            "lineage": {"parent_turn_id": root["id"], "sibling_index": 0, "kind": "branch"},
        }, headers=as_actor(OWNER)).json()
        assert streaming["status"] == "streaming"
        assert streaming["committed"] is False and streaming["terminal"] is False
        assert streaming["completed_at"] is None

        interrupted = test_client.post(
            f"/api/conversations/{conversation_id}/turns/{streaming['id']}/status",
            json={"status": "interrupted"}, headers=as_actor(OWNER),
        ).json()
        assert interrupted["interrupted"] is True and interrupted["committed"] is False
        assert interrupted["completed_at"] is not None
        # Committed history is immutable: no second advance.
        assert test_client.post(
            f"/api/conversations/{conversation_id}/turns/{streaming['id']}/status",
            json={"status": "complete"}, headers=as_actor(OWNER),
        ).status_code == 409

        retried = test_client.post(
            f"/api/conversations/{conversation_id}/turns/{streaming['id']}/retry",
            json={"role": "assistant", "status": "complete", "content": [{"kind": "text", "text": "recovered"}]},
            headers=as_actor(OWNER),
        ).json()
        assert retried["lineage"] == {"parent_turn_id": root["id"], "sibling_index": 1, "kind": "retry"}


def test_a_restart_surfaces_a_cut_off_streaming_turn_as_interrupted():
    durable = MemoryConversationStore(content_hash_key=KEY.encode("utf-8"))
    with client(conversation_store=durable) as test_client:
        conversation_id = create_conversation(test_client)["id"]
        root = append_root(test_client, conversation_id)
        test_client.post(f"/api/conversations/{conversation_id}/turns", json={
            "role": "assistant", "status": "streaming",
            "lineage": {"parent_turn_id": root["id"], "sibling_index": 0, "kind": "branch"},
        }, headers=as_actor(OWNER))
    # A fresh hub over the same persisted rows recovers on open.
    restarted = MemoryConversationStore(durable.rows, content_hash_key=KEY.encode("utf-8"), recover_on_open=True)
    with client(conversation_store=restarted) as test_client:
        turns = test_client.get(f"/api/conversations/{conversation_id}", headers=as_actor(OWNER)).json()["turns"]
        assert turns[1]["status"] == "interrupted"
        assert turns[1]["interrupted"] is True and turns[1]["committed"] is False
        assert turns[1]["completed_at"] is not None


# --- No hub-internal surface, tombstone shapes, fail-closed key -------------


def _all_route_paths(routes) -> list[str]:
    paths: list[str] = []
    for route in routes:
        path = getattr(route, "path", None)
        if isinstance(path, str):
            paths.append(path)
        # An included APIRouter is mounted as a wrapper object; its concrete
        # routes live on the wrapper's own router.
        nested = getattr(route, "routes", None) or getattr(
            getattr(route, "original_router", None), "routes", None,
        )
        if nested:
            paths.extend(_all_route_paths(nested))
    return paths


def test_no_hub_internal_conversation_surface_is_exposed():
    with client() as test_client:
        chat_paths = [
            path for path in _all_route_paths(test_client.app.routes)
            if path.startswith("/api/conversations")
        ]
        assert chat_paths, "the chat router must be mounted"
        for forbidden in ("audit", "recover", "retention"):
            assert not [path for path in chat_paths if forbidden in path]


def test_responses_never_serialize_the_keyed_content_fingerprint():
    with client() as test_client:
        conversation_id = create_conversation(test_client)["id"]
        append_root(test_client, conversation_id)
        listed = test_client.get("/api/conversations", headers=as_actor(OWNER))
        full = test_client.get(f"/api/conversations/{conversation_id}", headers=as_actor(OWNER))
        deleted = test_client.post(
            f"/api/conversations/{conversation_id}/delete",
            json={"mode": "purge_content_keep_tombstone"}, headers=as_actor(OWNER),
        )
        for response in (listed, full, deleted):
            assert "content_hash" not in response.text
            assert "hmac-sha256" not in response.text
            assert "actor" not in response.text


def test_deleted_conversations_return_content_free_tombstone_shapes():
    with client() as test_client:
        conversation_id = create_conversation(test_client, title="secret plans")["id"]
        append_root(test_client, conversation_id, text="the secret content")
        test_client.post(
            f"/api/conversations/{conversation_id}/delete",
            json={"mode": "purge_content_keep_tombstone"}, headers=as_actor(OWNER),
        )
        response = test_client.get(f"/api/conversations/{conversation_id}", headers=as_actor(OWNER))
        assert response.status_code == 200
        tombstone = response.json()
        assert tombstone["conversation"]["status"] == "deleted"
        assert tombstone["conversation"]["title"] is None
        assert tombstone["conversation"]["deletion"]["mode"] == "purge_content_keep_tombstone"
        assert all(turn["content"] == [] and turn["content_purged"] for turn in tombstone["turns"])
        assert "the secret content" not in response.text
        assert "secret plans" not in response.text
        # A tombstone accepts no further appends or lifecycle changes.
        assert test_client.post(f"/api/conversations/{conversation_id}/turns", json={
            "role": "user", "status": "complete",
        }, headers=as_actor(OWNER)).status_code == 409
        # And it never reappears in the default or archived listings.
        everything = test_client.get(
            "/api/conversations", params={"include_archived": "true"}, headers=as_actor(OWNER),
        ).json()["conversations"]
        assert everything == []


def test_chat_endpoints_refuse_when_the_content_hash_key_is_unset():
    with client(chat_content_hash_key="") as test_client:
        refusals = [
            test_client.get("/api/conversations", headers=as_actor(OWNER)),
            test_client.post("/api/conversations", json={}, headers=as_actor(OWNER)),
            test_client.get("/api/conversations/conv_missing00", headers=as_actor(OWNER)),
            test_client.post("/api/conversations/conv_missing00/turns", json={
                "role": "user", "status": "complete",
            }, headers=as_actor(OWNER)),
        ]
        for response in refusals:
            assert response.status_code == 503
            assert "WORKBENCH_CHAT_HASH_KEY" in response.json()["detail"]


def test_actor_id_namespace_is_disjoint_between_direct_and_hashed_logins():
    # A charset-valid login that itself begins with the reserved id- prefix is
    # hashed, so it can never collide with the hashed mapping of another login.
    import hashlib

    email = "alice@corp.example"
    hashed = conversation_actor(email)
    literal = conversation_actor(f"id-{hashlib.sha256(email.encode()).hexdigest()}")
    assert hashed.actor_id != literal.actor_id
    # Two distinct emails also stay distinct.
    assert conversation_actor("a@x").actor_id != conversation_actor("b@x").actor_id


def test_email_identity_fallback_owns_a_stable_actor_scope_over_http():
    # The production identity header carries emails (charset-invalid), which
    # exercise the sha256 fallback branch — untested by the name-based cases.
    email = "sdoumbouya81@gmail.com"
    other = "someone-else@corp.example"
    approvers = frozenset({email, other})
    api = client(identity_header="Tailscale-User-Login", owner=email, approvers=approvers)
    api2 = client(
        conversation_store=api.app.state.conversation_store,
        identity_header="Tailscale-User-Login", owner=email, approvers=approvers,
    )
    created = api.post(
        "/api/conversations", headers={"Tailscale-User-Login": email},
        json={"title": "Kickoff"},
    )
    assert created.status_code == 201
    conversation_id = created.json()["id"]
    # Same email → same owner on a second app instance over shared rows.
    listed = api2.get("/api/conversations", headers={"Tailscale-User-Login": email})
    assert [c["id"] for c in listed.json()["conversations"]] == [conversation_id]
    # A different email cannot see it (byte-equal missing-id refusal).
    foreign = api.get(
        f"/api/conversations/{conversation_id}", headers={"Tailscale-User-Login": other},
    )
    missing = api.get(
        "/api/conversations/conv_does_not_exist_00", headers={"Tailscale-User-Login": other},
    )
    assert foreign.status_code == missing.status_code
    assert foreign.content == missing.content
