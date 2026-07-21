"""Idempotency keys on side-effecting chat APIs (chat-first-voice T007).

Each group maps to a binding acceptance criterion:

1. Concurrent or retried requests carrying one key yield exactly one turn or
   lifecycle record and return the identical response.
2. Keys are scoped per actor: reusing another actor's key cannot read or replay
   their result — it executes fresh in the reuser's own scope.
3. A key reused with a materially different payload is rejected with a typed
   conflict, never silently deduplicated to the earlier result.

The store-level tests exercise the reentrant-lock race directly (a real racing
test with a tight ``sys.setswitchinterval``, mirroring the response lifecycle
store's fixed concurrency test); the API-level tests prove the keys surface at
the HTTP layer and dedup end-to-end.
"""
from __future__ import annotations

import sys
import threading

import pytest
from fastapi.testclient import TestClient

from workbench.api import create_app
from workbench.config import Settings
from workbench.conversation_models import ConversationActor
from workbench.graph import NullGraph
from workbench.idempotency_store import (
    IdempotencyConflictError,
    IdempotencyError,
    MemoryIdempotencyStore,
    request_hash_for,
)
from workbench.store import MemoryStore

ALICE = ConversationActor("operator_alice")
BOB = ConversationActor("operator_bob")

KEY = "api-test-content-hash-key-32byte"
OWNER = "operator"
OTHER = "reviewer"


# =========================================================================
# Store-level unit tests
# =========================================================================


def _hash(operation: str, body: dict) -> str:
    return request_hash_for(operation, {"body": body})


# --- Criterion 1: exactly one record, identical response, under concurrency -


def test_retry_with_same_key_and_payload_replays_without_re_executing():
    store = MemoryIdempotencyStore()
    calls: list[int] = []

    def executor() -> dict:
        calls.append(1)
        return {"id": "record-created-once", "seq": len(calls)}

    request_hash = _hash("op", {"text": "hello"})
    first, replayed_first = store.run(ALICE, "op", "k-1", request_hash, executor)
    second, replayed_second = store.run(ALICE, "op", "k-1", request_hash, executor)

    assert len(calls) == 1  # executed exactly once
    assert replayed_first is False and replayed_second is True
    assert first == second == {"id": "record-created-once", "seq": 1}
    assert len(store.rows.records) == 1


def test_concurrent_same_key_requests_yield_exactly_one_record():
    store = MemoryIdempotencyStore()
    executions: list[str] = []
    lock = threading.Lock()

    def executor() -> dict:
        # Real work under contention: record and return a per-execution marker.
        with lock:
            executions.append("ran")
        return {"id": "the-one-record", "runs": 1}

    # Force aggressive thread switching so the race is real: without the store's
    # lock two threads both observe "no record" and both execute; with it, the
    # first commits and the second replays.
    previous_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    barrier = threading.Barrier(2)
    request_hash = _hash("op", {"text": "concurrent"})
    results: dict[str, dict] = {}

    def race(name: str) -> None:
        barrier.wait()
        response, _ = store.run(ALICE, "op", "race-key", request_hash, executor)
        results[name] = response

    try:
        threads = [threading.Thread(target=race, args=(name,)) for name in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(previous_interval)

    assert len(executions) == 1  # exactly one record was created
    assert len(store.rows.records) == 1
    assert results["a"] == results["b"] == {"id": "the-one-record", "runs": 1}


def test_a_failed_execution_stores_nothing_and_stays_retriable():
    store = MemoryIdempotencyStore()
    attempts: list[int] = []

    def flaky() -> dict:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("transient")
        return {"id": "eventually", "attempt": len(attempts)}

    request_hash = _hash("op", {"text": "retry-after-failure"})
    with pytest.raises(RuntimeError, match="transient"):
        store.run(ALICE, "op", "k-flaky", request_hash, flaky)
    assert len(store.rows.records) == 0  # a failed effect memoizes nothing

    response, replayed = store.run(ALICE, "op", "k-flaky", request_hash, flaky)
    assert replayed is False
    assert response == {"id": "eventually", "attempt": 2}
    assert len(store.rows.records) == 1


# --- Criterion 2: keys are scoped per actor (no cross-actor read/replay) -----


def test_another_actors_key_executes_fresh_in_the_reusers_scope():
    store = MemoryIdempotencyStore()

    def alice_exec() -> dict:
        return {"id": "alice-secret-record", "owner": "alice"}

    def bob_exec() -> dict:
        return {"id": "bob-own-record", "owner": "bob"}

    request_hash = _hash("op", {"text": "same key different owners"})
    alice_result, _ = store.run(ALICE, "op", "shared-key", request_hash, alice_exec)
    # Bob reuses the SAME key + SAME payload hash: it must NOT replay Alice's
    # result — Bob's scope is disjoint, so his own executor runs and he sees his
    # own record, never Alice's.
    bob_result, bob_replayed = store.run(BOB, "op", "shared-key", request_hash, bob_exec)

    assert bob_replayed is False
    assert bob_result == {"id": "bob-own-record", "owner": "bob"}
    assert bob_result != alice_result
    assert len(store.rows.records) == 2  # one record per actor scope


def test_operation_scope_separates_the_same_key_across_endpoints():
    store = MemoryIdempotencyStore()
    request_hash_a = _hash("op.a", {"text": "a"})
    request_hash_b = _hash("op.b", {"text": "b"})
    a, _ = store.run(ALICE, "op.a", "k", request_hash_a, lambda: {"id": "a"})
    # The same key under a different operation is a different scope: it executes
    # instead of colliding with the first operation's result.
    b, replayed = store.run(ALICE, "op.b", "k", request_hash_b, lambda: {"id": "b"})
    assert a == {"id": "a"}
    assert b == {"id": "b"} and replayed is False
    assert len(store.rows.records) == 2


# --- Criterion 3: same key + different payload is rejected -------------------


def test_same_key_different_payload_is_rejected_not_deduplicated():
    store = MemoryIdempotencyStore()
    first, _ = store.run(ALICE, "op", "k-dup", _hash("op", {"text": "original"}), lambda: {"id": "original"})
    with pytest.raises(IdempotencyConflictError, match="materially different payload"):
        store.run(ALICE, "op", "k-dup", _hash("op", {"text": "MUTATED"}), lambda: {"id": "mutated"})
    # The original record is untouched — the conflict never overwrote it.
    assert store.rows.records[(ALICE.actor_id, "op", "k-dup")].response == {"id": "original"}
    assert first == {"id": "original"}


def test_store_validates_actor_key_and_hash():
    store = MemoryIdempotencyStore()
    with pytest.raises(IdempotencyError, match="acting ConversationActor"):
        store.run("operator_alice", "op", "k", _hash("op", {}), lambda: {})  # type: ignore[arg-type]
    with pytest.raises(IdempotencyError, match="idempotency key"):
        store.run(ALICE, "op", "   ", _hash("op", {}), lambda: {})
    with pytest.raises(IdempotencyError, match="idempotency key"):
        store.run(ALICE, "op", "x" * 201, _hash("op", {}), lambda: {})


def test_stored_response_is_isolated_from_later_mutation():
    store = MemoryIdempotencyStore()
    request_hash = _hash("op", {"text": "mutate"})
    first, _ = store.run(ALICE, "op", "k-iso", request_hash, lambda: {"nested": {"v": 1}})
    first["nested"]["v"] = 999  # mutate the returned copy
    replayed, _ = store.run(ALICE, "op", "k-iso", request_hash, lambda: {"nested": {"v": 1}})
    assert replayed == {"nested": {"v": 1}}  # the persisted record is untouched


# =========================================================================
# API-level tests (keys surface at the HTTP layer)
# =========================================================================


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


def client(**overrides) -> TestClient:
    return TestClient(create_app(
        settings=settings(**overrides), store=MemoryStore(), graph=NullGraph(),
    ))


def as_actor(name: str) -> dict:
    return {"X-Workbench-Actor": name}


def keyed(name: str, key: str) -> dict:
    return {"X-Workbench-Actor": name, "Idempotency-Key": key}


def test_repeated_create_with_one_key_makes_one_conversation():
    with client() as test_client:
        body = {"title": "Kickoff chat"}
        first = test_client.post("/api/conversations", json=body, headers=keyed(OWNER, "create-1"))
        second = test_client.post("/api/conversations", json=body, headers=keyed(OWNER, "create-1"))
        assert first.status_code == 201 and second.status_code == 201
        # Identical response including the once-generated id and timestamps.
        assert first.json() == second.json()
        # Exactly one conversation persisted.
        listing = test_client.get("/api/conversations", headers=as_actor(OWNER)).json()["conversations"]
        assert [item["id"] for item in listing] == [first.json()["id"]]


def test_repeated_append_turn_with_one_key_makes_one_turn():
    with client() as test_client:
        conversation = test_client.post(
            "/api/conversations", json={"title": "chat"}, headers=as_actor(OWNER),
        ).json()
        cid = conversation["id"]
        turn_body = {
            "role": "user", "status": "complete",
            "lineage": {"parent_turn_id": None, "sibling_index": 0, "kind": "initial"},
            "content": [{"kind": "text", "text": "hello"}],
        }
        first = test_client.post(f"/api/conversations/{cid}/turns", json=turn_body, headers=keyed(OWNER, "turn-1"))
        second = test_client.post(f"/api/conversations/{cid}/turns", json=turn_body, headers=keyed(OWNER, "turn-1"))
        assert first.status_code == 201 and second.status_code == 201
        assert first.json() == second.json()
        # The conversation has exactly one turn.
        turns = test_client.get(f"/api/conversations/{cid}", headers=as_actor(OWNER)).json()["turns"]
        assert len(turns) == 1
        assert turns[0]["id"] == first.json()["id"]


def test_same_key_different_payload_is_rejected_at_the_http_layer():
    with client() as test_client:
        first = test_client.post(
            "/api/conversations", json={"title": "Original"}, headers=keyed(OWNER, "conflict-1"),
        )
        assert first.status_code == 201
        conflict = test_client.post(
            "/api/conversations", json={"title": "Different"}, headers=keyed(OWNER, "conflict-1"),
        )
        assert conflict.status_code == 409
        assert "materially different payload" in conflict.json()["detail"]
        # Only the first conversation exists.
        listing = test_client.get("/api/conversations", headers=as_actor(OWNER)).json()["conversations"]
        assert [item["id"] for item in listing] == [first.json()["id"]]


def test_cross_actor_key_reuse_cannot_read_or_replay_another_actors_result():
    with client() as test_client:
        body = {"title": "Owner private chat"}
        owner_created = test_client.post("/api/conversations", json=body, headers=keyed(OWNER, "shared"))
        assert owner_created.status_code == 201
        # OTHER reuses the SAME key + SAME payload: it must create OTHER's own
        # conversation, never replay or read the owner's record.
        other_created = test_client.post("/api/conversations", json=body, headers=keyed(OTHER, "shared"))
        assert other_created.status_code == 201
        assert other_created.json()["id"] != owner_created.json()["id"]
        # Each actor sees only their own conversation.
        owner_ids = [c["id"] for c in test_client.get("/api/conversations", headers=as_actor(OWNER)).json()["conversations"]]
        other_ids = [c["id"] for c in test_client.get("/api/conversations", headers=as_actor(OTHER)).json()["conversations"]]
        assert owner_ids == [owner_created.json()["id"]]
        assert other_ids == [other_created.json()["id"]]


def test_requests_without_a_key_are_never_deduplicated():
    with client() as test_client:
        body = {"title": "No key"}
        first = test_client.post("/api/conversations", json=body, headers=as_actor(OWNER))
        second = test_client.post("/api/conversations", json=body, headers=as_actor(OWNER))
        assert first.status_code == 201 and second.status_code == 201
        assert first.json()["id"] != second.json()["id"]  # two distinct records
        listing = test_client.get("/api/conversations", headers=as_actor(OWNER)).json()["conversations"]
        assert len(listing) == 2


def test_repeated_same_key_create_over_http_yields_one_conversation():
    # The genuine concurrency proof is at the store layer (deterministic under
    # the lock); at the HTTP layer we assert a retried key dedups to one record
    # and an identical response, without sharing a TestClient across threads
    # (httpx/TestClient is not documented as concurrent-safe).
    with client() as test_client:
        first = test_client.post(
            "/api/conversations", json={"title": "Raced"}, headers=keyed(OWNER, "race-http"),
        )
        second = test_client.post(
            "/api/conversations", json={"title": "Raced"}, headers=keyed(OWNER, "race-http"),
        )
        assert first.status_code == 201 and second.status_code == 201
        assert first.json() == second.json()
        listing = test_client.get("/api/conversations", headers=as_actor(OWNER)).json()["conversations"]
        assert len(listing) == 1


def test_rename_and_delete_endpoints_dedup_a_reused_key():
    # Breadth beyond create/append: two more wired endpoints (rename, delete)
    # must each dedup a retried key to one effect with an identical response.
    with client() as test_client:
        cid = test_client.post(
            "/api/conversations", json={"title": "chat"}, headers=as_actor(OWNER),
        ).json()["id"]

        r1 = test_client.post(
            f"/api/conversations/{cid}/rename", json={"title": "renamed"}, headers=keyed(OWNER, "rename-1"),
        )
        r2 = test_client.post(
            f"/api/conversations/{cid}/rename", json={"title": "renamed"}, headers=keyed(OWNER, "rename-1"),
        )
        assert r1.status_code == 200 and r2.status_code == 200
        # Identical response and a single rename effect (both replays agree).
        assert r1.json() == r2.json()
        assert r1.json().get("title") == "renamed"

        d1 = test_client.post(
            f"/api/conversations/{cid}/delete", json={"mode": "purge_content_keep_tombstone"},
            headers=keyed(OWNER, "delete-1"),
        )
        d2 = test_client.post(
            f"/api/conversations/{cid}/delete", json={"mode": "purge_content_keep_tombstone"},
            headers=keyed(OWNER, "delete-1"),
        )
        assert d1.status_code == d2.status_code
        assert d1.json() == d2.json()
