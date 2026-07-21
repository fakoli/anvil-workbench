"""Retention enforcement, deletion semantics, and irreversibility.

Each test group maps to an acceptance criterion of chat-first-voice:T002.3:

1. Expired content is removed on enforcement and is not returned on later
   reads (``enforce_retention`` applies the ``retention.delete_after`` ceiling
   — the only age ceiling ``chat-conversation.v1`` declares — plus the
   deletion lifecycle).
2. Deletion removes retained conversation content while preserving only
   lifecycle and hash metadata, under the schema's two deletion modes.
3. Retained audit records contain no raw user or assistant content.
4. Redacted or deleted content cannot be recovered through public store or
   API operations — including a fresh store instance opened over the same
   persisted rows, proving the content bytes left the row storage.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from workbench.conversation_models import (
    ContentBlock,
    ConversationActor,
    ConversationAudit,
    ConversationDeletion,
    RetentionPolicy,
    TurnAudit,
    TurnLineage,
    TurnRedaction,
    VoiceEvent,
)
from workbench.conversation_store import (
    ConversationStoreError,
    MemoryConversationStore,
    UnknownConversationError,
)
from workbench.models import now_utc

ALICE = ConversationActor("operator_alice")
BOB = ConversationActor("operator_bob")
REDACTED = TurnRedaction("redacted", "workbench.default")
KEY = b"retention-test-content-hash-key1"

USER_SECRET = "user-secret-alpha-payload"
ASSISTANT_SECRET = "assistant-secret-beta-payload"
TITLE_SECRET = "title-secret-gamma"


def retention(delete_after: datetime | None = None) -> RetentionPolicy:
    return RetentionPolicy("workbench.default-90d", "retained_redacted", "retained_redacted", delete_after=delete_after)


def fresh(rows=None, **kwargs) -> MemoryConversationStore:
    return MemoryConversationStore(rows, content_hash_key=KEY, **kwargs)


def seeded_conversation(store: MemoryConversationStore, *, delete_after: datetime | None = None, title: str = TITLE_SECRET):
    """One conversation holding both roles' secrets plus a voice transcript."""
    conversation = store.create_conversation(ALICE, retention(delete_after), title=title)
    root = store.append_turn(
        ALICE, conversation.id, role="user", status="complete",
        lineage=TurnLineage(None, 0, "initial"), redaction=REDACTED,
        content=(ContentBlock("text", USER_SECRET), ContentBlock("transcript", USER_SECRET + " spoken")),
        voice_events=(VoiceEvent("stt_commit", now_utc(), transcript_chars=25),),
    )
    reply = store.append_turn(
        ALICE, conversation.id, role="assistant", status="complete",
        lineage=TurnLineage(root.id, 0, "initial"), redaction=REDACTED,
        content=(ContentBlock("text", ASSISTANT_SECRET),),
    )
    return conversation, root, reply


def assert_no_secrets(dumped: str) -> None:
    for secret in (USER_SECRET, ASSISTANT_SECRET, TITLE_SECRET):
        assert secret not in dumped


# --- Criterion 1: expiry enforcement removes content; later reads show none ---


def test_enforce_retention_purges_expired_conversations_and_later_reads_return_no_content():
    store = fresh()
    past = now_utc() - timedelta(days=1)
    expired, root, reply = seeded_conversation(store, delete_after=past)
    fresh_conv, _, _ = seeded_conversation(store, delete_after=None, title="Keep me around")

    enforced = store.enforce_retention(now_utc())
    assert [record.conversation_id for record in enforced] == [expired.id]
    assert all(isinstance(record, ConversationAudit) for record in enforced)
    assert enforced[0].deletion_mode == "purge_content_keep_tombstone"
    assert enforced[0].turn_count == 2 and enforced[0].committed_turn_count == 2

    # Later reads return the tombstoned shapes: no content, no title.
    tombstone = store.get_conversation(ALICE, expired.id)
    assert tombstone.status == "deleted" and tombstone.title is None
    assert tombstone.deletion is not None and tombstone.deletion.completed_at is not None
    _, turns = store.get_conversation_with_turns(ALICE, expired.id)
    assert [turn.id for turn in turns] == [root.id, reply.id]
    assert all(turn.content == () and turn.content_purged for turn in turns)
    # Lifecycle and fingerprint metadata survive on the tombstones.
    assert turns[0].content_hash == root.content_hash and turns[1].content_hash == reply.content_hash
    assert turns[0].voice_events == root.voice_events
    assert store.list_conversations(ALICE, include_archived=True) != [] # the unexpired one
    assert [item.id for item in store.list_conversations(ALICE)] == [fresh_conv.id]
    assert store.search_conversations(ALICE, "secret", include_archived=True) == []

    # The unexpired conversation is untouched.
    _, kept = store.get_conversation_with_turns(ALICE, fresh_conv.id)
    assert any(block.text == USER_SECRET for turn in kept for block in turn.content)
    # Enforcement is idempotent: the tombstone is never re-enforced.
    assert store.enforce_retention(now_utc()) == ()


def test_enforce_retention_leaves_unexpired_ceilings_alone_and_fails_closed_on_naive_now():
    store = fresh()
    future = now_utc() + timedelta(days=30)
    conversation, _, _ = seeded_conversation(store, delete_after=future)
    assert store.enforce_retention(now_utc()) == ()
    _, turns = store.get_conversation_with_turns(ALICE, conversation.id)
    assert any(block.text == USER_SECRET for turn in turns for block in turn.content)
    with pytest.raises(ConversationStoreError, match="timezone-aware"):
        store.enforce_retention(datetime(2026, 7, 20))  # naive: refuse, never guess
    # But a later sweep past the ceiling purges it.
    enforced = store.enforce_retention(future + timedelta(seconds=1))
    assert [record.conversation_id for record in enforced] == [conversation.id]
    _, purged = store.get_conversation_with_turns(ALICE, conversation.id)
    assert all(turn.content == () for turn in purged)


def test_enforce_retention_completes_a_crashed_pending_deletion():
    store = fresh()
    conversation, _, _ = seeded_conversation(store)
    # Simulate a crash between the persisted deletion_pending record and the
    # purge: hand-write the pending row the way delete_conversation does.
    record = store.rows.conversations[conversation.id]
    store.rows.conversations[conversation.id] = replace(
        record, status="deletion_pending",
        deletion=ConversationDeletion(now_utc(), "purge_all_records"), updated_at=now_utc(),
    )
    enforced = store.enforce_retention(now_utc())
    assert [record.deletion_mode for record in enforced] == ["purge_all_records"]
    # The reconciliation honoured the requested mode: everything is gone.
    assert conversation.id not in store.rows.conversations
    assert conversation.id not in store.rows.turns
    with pytest.raises(UnknownConversationError):
        store.get_conversation(ALICE, conversation.id)


# --- Criterion 2: deletion removes content, preserves lifecycle + hashes ------


def test_delete_keep_tombstone_purges_content_and_preserves_lifecycle_and_hashes():
    store = fresh()
    conversation, root, reply = seeded_conversation(store)
    final = store.delete_conversation(ALICE, conversation.id, "purge_content_keep_tombstone")
    assert isinstance(final, ConversationAudit)
    assert final.deletion_mode == "purge_content_keep_tombstone"
    assert (final.turn_count, final.committed_turn_count) == (2, 2)

    tombstone = store.get_conversation(ALICE, conversation.id)
    assert tombstone.status == "deleted" and tombstone.title is None
    assert tombstone.id == conversation.id and tombstone.actor == ALICE
    assert tombstone.created_at == conversation.created_at
    assert tombstone.deletion is not None
    assert tombstone.deletion.mode == "purge_content_keep_tombstone"
    assert tombstone.deletion.completed_at is not None
    assert tombstone.deletion.completed_at >= tombstone.deletion.requested_at

    # Surviving lineage keeps its original ids; only content is gone.
    _, turns = store.get_conversation_with_turns(ALICE, conversation.id)
    assert [turn.id for turn in turns] == [root.id, reply.id]
    for original, tomb in zip((root, reply), turns):
        assert tomb.content == () and tomb.content_purged
        assert tomb.content_hash == original.content_hash
        assert tomb.lineage == original.lineage and tomb.status == original.status
        assert tomb.created_at == original.created_at and tomb.completed_at == original.completed_at


def test_delete_purge_all_records_removes_conversation_and_turns_entirely():
    store = fresh()
    conversation, _, _ = seeded_conversation(store)
    final = store.delete_conversation(ALICE, conversation.id, "purge_all_records")
    assert final.deletion_mode == "purge_all_records" and final.status == "deleted"
    assert final.turn_count == 2  # the content-free audit keeps the counts

    # No tombstone: the id is indistinguishable from one that never existed.
    with pytest.raises(UnknownConversationError) as deleted_error:
        store.get_conversation(ALICE, conversation.id)
    with pytest.raises(UnknownConversationError) as missing_error:
        store.get_conversation(ALICE, "conv_never_was_here")
    assert str(deleted_error.value) == str(missing_error.value)
    assert conversation.id not in store.rows.conversations
    assert conversation.id not in store.rows.turns


def test_delete_is_actor_scoped_mode_allowlisted_and_not_repeatable():
    store = fresh()
    conversation, _, _ = seeded_conversation(store)
    with pytest.raises(UnknownConversationError):
        store.delete_conversation(BOB, conversation.id, "purge_all_records")
    with pytest.raises(ConversationStoreError, match="deletion mode"):
        store.delete_conversation(ALICE, conversation.id, "purge_some_things")
    # A refused delete purged nothing.
    _, turns = store.get_conversation_with_turns(ALICE, conversation.id)
    assert any(turn.content for turn in turns)

    store.delete_conversation(ALICE, conversation.id, "purge_content_keep_tombstone")
    with pytest.raises(ConversationStoreError, match="cannot be deleted again"):
        store.delete_conversation(ALICE, conversation.id, "purge_all_records")
    # An archived conversation can still be deleted.
    archived = store.create_conversation(ALICE, retention(), title="old thread")
    store.archive_conversation(ALICE, archived.id)
    assert store.delete_conversation(ALICE, archived.id, "purge_content_keep_tombstone").status == "deleted"


# --- Criterion 3: retained audit records contain no raw content ---------------


def test_retained_audit_records_contain_no_raw_user_or_assistant_content():
    store = fresh()
    deleted_conv, _, _ = seeded_conversation(store)
    expired_conv = store.create_conversation(
        ALICE, retention(now_utc() - timedelta(seconds=1)), title=TITLE_SECRET + " expired",
    )
    store.append_turn(
        ALICE, expired_conv.id, role="user", status="complete",
        lineage=TurnLineage(None, 0, "initial"), redaction=REDACTED,
        content=(ContentBlock("text", USER_SECRET),),
    )
    store.delete_conversation(ALICE, deleted_conv.id, "purge_content_keep_tombstone")
    store.enforce_retention(now_utc())

    events = store.list_audit(limit=100)
    assert {event.kind for event in events} >= {
        "conversation.created", "turn.appended", "conversation.deletion_requested",
        "turn.content_purged", "conversation.deleted", "conversation.retention_expired",
    }
    for event in events:
        assert isinstance(event.record, (ConversationAudit, TurnAudit))
    # Scan the full serialized retained audit rows for content strings.
    assert_no_secrets(repr(events))
    # The purge events retained the lifecycle/hash facts.
    purged_rows = [event.record for event in events if event.kind == "turn.content_purged"]
    assert purged_rows and all(
        isinstance(row, TurnAudit) and row.content_purged and row.content_hash.startswith("hmac-sha256:")
        for row in purged_rows
    )


# --- Criterion 4: purged content is unrecoverable through public surfaces -----


def test_deleted_content_is_unrecoverable_including_from_a_fresh_store_over_the_same_rows():
    store = fresh()
    conversation, root, _ = seeded_conversation(store)
    streaming = store.append_turn(
        ALICE, conversation.id, role="assistant", status="streaming",
        lineage=TurnLineage(root.id, 1, "retry"), redaction=REDACTED,
        content=(ContentBlock("text", ASSISTANT_SECRET + " partial"),),
    )
    store.delete_conversation(ALICE, conversation.id, "purge_content_keep_tombstone")

    # The content bytes left the row storage itself, not just the projections.
    assert_no_secrets(repr(store.rows))

    # A fresh instance over the same rows (with the key, after recovery) still
    # has nothing to return: the purge is durable, not an in-memory flag.
    reopened = MemoryConversationStore(store.rows, content_hash_key=KEY, recover_on_open=True)
    tombstone, turns = reopened.get_conversation_with_turns(ALICE, conversation.id)
    assert tombstone.status == "deleted" and tombstone.title is None
    assert all(turn.content == () and turn.content_purged for turn in turns)
    # The purged streaming turn was recovered as interrupted and stayed empty.
    recovered = next(turn for turn in turns if turn.id == streaming.id)
    assert recovered.status == "interrupted" and recovered.content == ()

    # No public operation recovers or extends the tombstone.
    assert reopened.list_conversations(ALICE, include_archived=True) == []
    assert reopened.search_conversations(ALICE, "secret", include_archived=True) == []
    with pytest.raises(ConversationStoreError, match="cannot be modified"):
        reopened.rename_conversation(ALICE, conversation.id, "bring it back")
    with pytest.raises(ConversationStoreError, match="only an active conversation"):
        reopened.archive_conversation(ALICE, conversation.id)
    with pytest.raises(ConversationStoreError, match="only an archived conversation"):
        reopened.unarchive_conversation(ALICE, conversation.id)
    with pytest.raises(ConversationStoreError, match="does not accept turn appends"):
        reopened.retry_turn(
            ALICE, conversation.id, recovered.id, role="assistant", status="complete",
            redaction=REDACTED, content=(ContentBlock("text", "resurrected"),),
        )
    assert_no_secrets(repr(reopened.rows))


def test_purge_all_records_leaves_no_recoverable_trace_of_content_anywhere():
    store = fresh()
    conversation, _, _ = seeded_conversation(store)
    store.delete_conversation(ALICE, conversation.id, "purge_all_records")
    # Rows, audit, and a fresh instance: no content anywhere, no identity row.
    assert_no_secrets(repr(store.rows))
    reopened = fresh(store.rows, recover_on_open=True)
    with pytest.raises(UnknownConversationError):
        reopened.get_conversation_with_turns(ALICE, conversation.id)
    assert reopened.list_conversations(ALICE, include_archived=True) == []
    # The lifecycle/hash audit facts are the only remnant, and they are clean.
    assert_no_secrets(repr(reopened.list_audit(limit=100)))


def test_crashed_pending_deletion_is_reconciled_on_read_reopen_and_owner_retry():
    # Simulate the crash window: pending state persisted, purge never ran.
    store = fresh()
    conversation, root, reply = seeded_conversation(store)
    pending = replace(
        store.rows.conversations[conversation.id],
        status="deletion_pending",
        deletion=ConversationDeletion(now_utc(), "purge_content_keep_tombstone"),
        updated_at=now_utc(),
    )
    store.rows.conversations[conversation.id] = pending

    # (a) A read never serves pending content: it completes the deletion first.
    record, turns = store.get_conversation_with_turns(ALICE, conversation.id)
    assert record.status == "deleted" and record.title is None
    assert all(turn.content_purged and not turn.content for turn in turns)

    # (b) recover_on_open reconciles a pending deletion at construction.
    store2 = fresh()
    conversation2, _, _ = seeded_conversation(store2)
    store2.rows.conversations[conversation2.id] = replace(
        store2.rows.conversations[conversation2.id],
        status="deletion_pending",
        deletion=ConversationDeletion(now_utc(), "purge_all_records"),
        updated_at=now_utc(),
    )
    reopened = MemoryConversationStore(store2.rows, content_hash_key=KEY, recover_on_open=True)
    with pytest.raises(UnknownConversationError):
        reopened.get_conversation(ALICE, conversation2.id)

    # (c) The owner's delete completes a pending deletion instead of refusing.
    store3 = fresh()
    conversation3, _, _ = seeded_conversation(store3)
    store3.rows.conversations[conversation3.id] = replace(
        store3.rows.conversations[conversation3.id],
        status="deletion_pending",
        deletion=ConversationDeletion(now_utc(), "purge_content_keep_tombstone"),
        updated_at=now_utc(),
    )
    final = store3.delete_conversation(ALICE, conversation3.id, "purge_content_keep_tombstone")
    assert final.deletion_mode == "purge_content_keep_tombstone"
    assert store3.get_conversation(ALICE, conversation3.id).status == "deleted"


def test_deleted_history_is_final_no_status_advance_on_tombstones():
    store = fresh()
    conversation, root, reply = seeded_conversation(store)
    streaming = store.append_turn(
        ALICE, conversation.id, role="assistant", status="streaming",
        lineage=TurnLineage(reply.id, 1, "branch"), redaction=REDACTED,
        content=(ContentBlock("text", "partial secret"),),
    )
    store.delete_conversation(ALICE, conversation.id, "purge_content_keep_tombstone")

    # The purged streaming turn was finalized as interrupted at deletion...
    _, turns = store.get_conversation_with_turns(ALICE, conversation.id)
    statuses = {turn.id: turn.status for turn in turns}
    assert statuses[streaming.id] == "interrupted"

    # ...and no tombstone turn can advance status afterwards.
    with pytest.raises(ConversationStoreError, match="final"):
        store.advance_turn_status(ALICE, conversation.id, streaming.id, "complete")
