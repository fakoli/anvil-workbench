"""Off-read-path retention enforcement, content-free preview, ephemeral chat.

Each test group maps to an acceptance criterion of chat-first-voice:T009:

1. The retention preview carries conversation ids, counts, and timestamps only
   — never a title or any message content. A forbidden-marker scan over the
   serialized preview (store objects and the API JSON) proves no leakage.
2. Reads never trigger deletion work: a conversation past its ``delete_after``
   ceiling is still returned in full by every read surface (get / list / search
   and the HTTP projection) until the explicit batched ``enforce_retention``
   pass runs. Only that pass — and its operator ``/enforce`` endpoint — initiates
   retention-expiry deletion. (The separate crashed-``deletion_pending``
   reconcile-on-read remains intact; it completes an already-requested deletion,
   it does not initiate one.)
3. The ephemeral affordance creates a ``metadata_only`` conversation (both
   content kinds) in one action, and its ``ephemeral`` badge reflects the true
   durable policy, both at the store and over HTTP.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from workbench.api import create_app
from workbench.config import Settings
from workbench.conversation_models import (
    ContentBlock,
    ConversationActor,
    RetentionPolicy,
    RetentionPreview,
    TurnLineage,
    TurnRedaction,
    VoiceEvent,
    ephemeral_retention_policy,
    is_metadata_only,
)
from workbench.conversation_store import (
    ConversationStoreError,
    MemoryConversationStore,
)
from workbench.graph import NullGraph
from workbench.models import now_utc
from workbench.store import MemoryStore

# --- store-level fixtures ----------------------------------------------------

ALICE = ConversationActor("operator_alice")
BOB = ConversationActor("operator_bob")
REDACTED = TurnRedaction("redacted", "workbench.default")
KEY = b"t009-retention-content-hash-key1"

USER_SECRET = "user-secret-alpha-payload"
ASSISTANT_SECRET = "assistant-secret-beta-payload"
TITLE_SECRET = "title-secret-gamma"
_SECRETS = (USER_SECRET, ASSISTANT_SECRET, TITLE_SECRET)


def retention(delete_after: datetime | None = None) -> RetentionPolicy:
    return RetentionPolicy(
        "workbench.default-90d", "retained_redacted", "retained_redacted", delete_after=delete_after,
    )


def fresh(rows=None, **kwargs) -> MemoryConversationStore:
    return MemoryConversationStore(rows, content_hash_key=KEY, **kwargs)


def seeded(store: MemoryConversationStore, *, delete_after: datetime | None = None, title: str = TITLE_SECRET):
    """One conversation holding both roles' secrets under ``delete_after``."""
    conversation = store.create_conversation(ALICE, retention(delete_after), title=title)
    root = store.append_turn(
        ALICE, conversation.id, role="user", status="complete",
        lineage=TurnLineage(None, 0, "initial"), redaction=REDACTED,
        content=(ContentBlock("text", USER_SECRET),),
    )
    store.append_turn(
        ALICE, conversation.id, role="assistant", status="complete",
        lineage=TurnLineage(root.id, 0, "initial"), redaction=REDACTED,
        content=(ContentBlock("text", ASSISTANT_SECRET),),
    )
    return conversation


def assert_no_secrets(dumped: str) -> None:
    for secret in _SECRETS:
        assert secret not in dumped


# --- Criterion 1: content-free preview --------------------------------------


def test_retention_preview_is_content_free_ids_counts_and_timestamps_only():
    store = fresh()
    past = now_utc() - timedelta(days=1)
    expired = seeded(store, delete_after=past)
    seeded(store, delete_after=None, title="Keep me")  # not previewed

    preview = store.retention_preview(now_utc())
    assert [row.conversation_id for row in preview] == [expired.id]
    row = preview[0]
    assert isinstance(row, RetentionPreview)
    assert row.reason == "retention_expired"
    assert row.turn_count == 2 and row.committed_turn_count == 2
    assert row.created_at == expired.created_at and row.delete_after == past

    # The preview object itself carries no title/content field at all.
    assert not hasattr(row, "title")
    assert_no_secrets(repr(preview))


def test_retention_preview_does_not_mutate_or_delete_anything():
    store = fresh()
    past = now_utc() - timedelta(days=1)
    expired = seeded(store, delete_after=past)

    # Previewing twice is pure: the conversation is still live with its content.
    assert [row.conversation_id for row in store.retention_preview(now_utc())] == [expired.id]
    assert [row.conversation_id for row in store.retention_preview(now_utc())] == [expired.id]
    record, turns = store.get_conversation_with_turns(ALICE, expired.id)
    assert record.status == "active"
    assert any(block.text == USER_SECRET for turn in turns for block in turn.content)


def test_retention_preview_includes_crashed_pending_deletions_and_fails_closed_on_naive_now():
    from dataclasses import replace

    from workbench.conversation_models import ConversationDeletion

    store = fresh()
    conversation = seeded(store)
    store.rows.conversations[conversation.id] = replace(
        store.rows.conversations[conversation.id],
        status="deletion_pending",
        deletion=ConversationDeletion(now_utc(), "purge_all_records"),
        updated_at=now_utc(),
    )
    preview = store.retention_preview(now_utc())
    assert [(row.conversation_id, row.reason) for row in preview] == [(conversation.id, "deletion_pending")]
    with pytest.raises(ConversationStoreError, match="timezone-aware"):
        store.retention_preview(datetime(2026, 7, 20))


# --- Criterion 2: reads never trigger deletion; only enforce does ------------


def test_reads_do_not_expire_a_conversation_past_its_delete_after_ceiling():
    store = fresh()
    past = now_utc() - timedelta(days=1)
    expired = seeded(store, delete_after=past)

    # Every read surface still returns the still-live conversation in full,
    # even though its ceiling has passed — a read must not initiate deletion.
    assert store.get_conversation(ALICE, expired.id).status == "active"
    assert [c.id for c in store.list_conversations(ALICE)] == [expired.id]
    assert [c.id for c in store.search_conversations(ALICE, "secret")] == [expired.id]
    record, turns = store.get_conversation_with_turns(ALICE, expired.id)
    assert record.status == "active"
    assert any(block.text == USER_SECRET for turn in turns for block in turn.content)
    # Nothing was purged by the reads.
    assert store.rows.conversations[expired.id].status == "active"

    # Only the explicit batched pass deletes it.
    enforced = store.enforce_retention(now_utc())
    assert [row.conversation_id for row in enforced] == [expired.id]
    tombstone, purged = store.get_conversation_with_turns(ALICE, expired.id)
    assert tombstone.status == "deleted"
    assert all(turn.content == () and turn.content_purged for turn in purged)


def test_crashed_pending_deletion_still_reconciles_on_read_unlike_expiry():
    # A crashed deletion_pending (an already-REQUESTED deletion) is still
    # completed on read — that is distinct from initiating expiry on a live
    # conversation, which criterion 2 forbids.
    from dataclasses import replace

    from workbench.conversation_models import ConversationDeletion

    store = fresh()
    conversation = seeded(store)
    store.rows.conversations[conversation.id] = replace(
        store.rows.conversations[conversation.id],
        status="deletion_pending",
        deletion=ConversationDeletion(now_utc(), "purge_content_keep_tombstone"),
        updated_at=now_utc(),
    )
    record, turns = store.get_conversation_with_turns(ALICE, conversation.id)
    assert record.status == "deleted"
    assert all(turn.content_purged for turn in turns)


# --- Criterion 3: one-action ephemeral, truthful badge ----------------------


def test_ephemeral_policy_is_metadata_only_for_both_content_kinds():
    policy = ephemeral_retention_policy()
    assert policy.transcript_text == "metadata_only"
    assert policy.voice_transcript_text == "metadata_only"
    assert is_metadata_only(policy) is True
    assert is_metadata_only(retention()) is False


def test_create_ephemeral_conversation_is_one_action_metadata_only_with_true_badge():
    store = fresh()
    conversation = store.create_ephemeral_conversation(ALICE)
    # One action produced a metadata_only conversation; the badge is truthful.
    assert conversation.retention.transcript_text == "metadata_only"
    assert conversation.retention.voice_transcript_text == "metadata_only"
    assert is_metadata_only(conversation.retention) is True
    assert conversation.actor == ALICE and conversation.status == "active"

    # The policy is enforced, not decorative: a transcript block is refused, so
    # the ephemeral badge cannot disagree with what may actually persist.
    root = store.append_turn(
        ALICE, conversation.id, role="user", status="complete",
        lineage=TurnLineage(None, 0, "initial"), redaction=REDACTED,
        content=(ContentBlock("text", "hi"),),
    )
    with pytest.raises(ConversationStoreError, match="metadata_only forbids"):
        store.append_turn(
            ALICE, conversation.id, role="assistant", status="complete",
            lineage=TurnLineage(root.id, 0, "initial"), redaction=REDACTED,
            content=(ContentBlock("transcript", "spoken words"),),
            voice_events=(VoiceEvent("stt_commit", now_utc(), transcript_chars=11),),
        )


# --- API-level coverage ------------------------------------------------------

OWNER = "operator"
OTHER = "reviewer"
API_KEY = "t009-api-content-hash-key-32bytes"


def settings(**overrides) -> Settings:
    values = dict(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner=OWNER, approvers=frozenset({OWNER, OTHER}), bridge_bootstrap_token="",
        anvil_router_base_url="", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=False,
        chat_content_hash_key=API_KEY,
    )
    values.update(overrides)
    return Settings(**values)


def client(**overrides) -> TestClient:
    return TestClient(create_app(settings=settings(**overrides), store=MemoryStore(), graph=NullGraph()))


def as_actor(name: str) -> dict[str, str]:
    return {"X-Workbench-Actor": name}


def test_api_ephemeral_endpoint_creates_metadata_only_in_one_action_with_true_badge():
    with client() as c:
        response = c.post("/api/conversations/ephemeral", headers=as_actor(OWNER))
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["ephemeral"] is True
        assert body["retention"]["transcript_text"] == "metadata_only"
        assert body["retention"]["voice_transcript_text"] == "metadata_only"

        # A later read reports the same truthful badge.
        fetched = c.get(f"/api/conversations/{body['id']}", headers=as_actor(OWNER))
        assert fetched.json()["conversation"]["ephemeral"] is True

        # An ordinary conversation's badge is false.
        ordinary = c.post("/api/conversations", json={"title": "regular"}, headers=as_actor(OWNER))
        assert ordinary.json()["ephemeral"] is False


def test_api_hub_preview_is_operator_only_and_content_free():
    with client() as c:
        # Seed an expired conversation with a secret title via the actor surface.
        created = c.post(
            "/api/conversations",
            json={
                "title": TITLE_SECRET,
                "retention": {"delete_after": (now_utc() - timedelta(days=1)).isoformat()},
            },
            headers=as_actor(OWNER),
        )
        conversation_id = created.json()["id"]
        c.post(
            f"/api/conversations/{conversation_id}/turns",
            json={
                "role": "user", "status": "complete",
                "lineage": {"parent_turn_id": None, "sibling_index": 0, "kind": "initial"},
                "content": [{"kind": "text", "text": USER_SECRET}],
            },
            headers=as_actor(OWNER),
        )

        # A non-owner (allowlisted) actor is refused the operator surface.
        assert c.get("/api/hub/retention/preview", headers=as_actor(OTHER)).status_code == 403

        preview = c.get("/api/hub/retention/preview", headers=as_actor(OWNER))
        assert preview.status_code == 200, preview.text
        rows = preview.json()["preview"]
        assert [row["conversation_id"] for row in rows] == [conversation_id]
        assert rows[0]["turn_count"] == 1 and rows[0]["reason"] == "retention_expired"
        # No title/content leaks through the serialized preview response.
        assert_no_secrets(preview.text)
        assert "title" not in preview.text


def test_api_read_does_not_expire_only_the_enforce_endpoint_does():
    with client() as c:
        created = c.post(
            "/api/conversations",
            json={
                "title": "soon gone",
                "retention": {"delete_after": (now_utc() - timedelta(days=1)).isoformat()},
            },
            headers=as_actor(OWNER),
        )
        conversation_id = created.json()["id"]

        # Reading the past-ceiling conversation does not delete it.
        got = c.get(f"/api/conversations/{conversation_id}", headers=as_actor(OWNER))
        assert got.json()["conversation"]["status"] == "active"
        assert [item["id"] for item in c.get("/api/conversations", headers=as_actor(OWNER)).json()["conversations"]] == [conversation_id]

        # A non-owner cannot trigger the batched pass.
        assert c.post("/api/hub/retention/enforce", headers=as_actor(OTHER)).status_code == 403

        # The operator's explicit enforce pass is what deletes it.
        enforced = c.post("/api/hub/retention/enforce", headers=as_actor(OWNER))
        assert enforced.status_code == 200, enforced.text
        assert [row["conversation_id"] for row in enforced.json()["enforced"]] == [conversation_id]
        after = c.get(f"/api/conversations/{conversation_id}", headers=as_actor(OWNER))
        assert after.json()["conversation"]["status"] == "deleted"


def test_api_hub_retention_refuses_when_chat_persistence_is_unconfigured():
    with client(chat_content_hash_key="") as c:
        assert c.get("/api/hub/retention/preview", headers=as_actor(OWNER)).status_code == 503
        assert c.post("/api/hub/retention/enforce", headers=as_actor(OWNER)).status_code == 503
