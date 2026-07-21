"""Conversation/turn domain models: ownership, lifecycle, lineage, retention, audit.

Each test maps to an acceptance criterion of chat-first-voice:T002.1:

1. Conversations carry actor ownership, lifecycle state, timestamps, and a
   retention-policy reference.
2. Turns carry role, content, committed-or-interrupted state, content hash, and
   parent/branch lineage; records are append-only (frozen).
3. Lineage cannot cross conversation ownership boundaries, and the four hub
   append-time invariants (single null-parent root, sibling-slot uniqueness,
   parent existence in the same conversation, acyclicity) are refused fail-closed.
4. Safe audit models contain lifecycle and hash metadata without any retained
   message content field.
"""
from __future__ import annotations

import dataclasses
import re
from datetime import datetime, timezone

import pytest

from workbench.conversation_models import (
    Conversation,
    ConversationActor,
    ConversationAudit,
    ConversationDeletion,
    ConversationError,
    ContentBlock,
    RetentionPolicy,
    Turn,
    TurnAudit,
    TurnLineage,
    TurnRedaction,
    VoiceEvent,
    conversation_audit,
    make_turn,
    purge_turn_content,
    turn_audit,
    turn_content_hash,
    validate_turn_append,
)

NOW = datetime(2026, 7, 19, tzinfo=timezone.utc)
REDACTED = TurnRedaction("redacted", "workbench.default")
# Hermetic fixture keys for the server-keyed content fingerprint (PRD R008).
KEY = b"unit-test-content-hash-key-A-0001"
OTHER_KEY = b"unit-test-content-hash-key-B-0002"


def retention(transcript: str = "retained_redacted", voice: str = "retained_redacted") -> RetentionPolicy:
    return RetentionPolicy("workbench.default-90d", transcript, voice)


def conversation(conv_id: str = "conv_alpha_0001", **overrides: object) -> Conversation:
    values: dict = {
        "id": conv_id,
        "actor": ConversationActor("operator_example"),
        "retention": retention(),
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return Conversation(**values)


def turn(
    turn_id: str,
    conv_id: str = "conv_alpha_0001",
    *,
    parent: str | None = None,
    sibling: int = 0,
    lineage_kind: str = "initial",
    role: str = "user",
    status: str = "complete",
    content: tuple[ContentBlock, ...] = (ContentBlock("text", "hello"),),
    voice_events: tuple[VoiceEvent, ...] = (),
) -> Turn:
    return make_turn(
        id=turn_id,
        conversation_id=conv_id,
        role=role,
        mode="ordinary",
        status=status,
        lineage=TurnLineage(parent, sibling, lineage_kind),
        content=content,
        redaction=REDACTED,
        voice_events=voice_events,
        created_at=NOW,
        completed_at=None if status == "streaming" else NOW,
        content_hash_key=KEY,
    )


# --- Criterion 1: conversation ownership, lifecycle, timestamps, retention ---


def test_conversation_carries_owner_lifecycle_timestamps_and_retention_reference():
    record = conversation(title="Routed chat kickoff")
    assert record.actor.actor_id == "operator_example"
    assert record.actor.kind == "operator"
    assert record.status == "active"
    assert record.created_at == NOW and record.updated_at == NOW
    assert record.retention.policy_id == "workbench.default-90d"
    assert record.retention.transcript_text == "retained_redacted"


def test_conversation_lifecycle_actor_and_retention_are_validated_fail_closed():
    with pytest.raises(ConversationError, match="status"):
        conversation(status="paused")
    with pytest.raises(ConversationError, match="actor kind"):
        conversation(actor=ConversationActor("someone", kind="model"))
    with pytest.raises(ConversationError, match="conversation id"):
        conversation(conv_id="not-a-conversation-id")
    with pytest.raises(ConversationError, match="transcript_text"):
        conversation(retention=retention(transcript="retained_raw"))
    # Deletion lifecycle is typed: a deleting status requires the record, an
    # active status must not carry one, and the mode is allowlisted.
    with pytest.raises(ConversationError, match="requires a deletion record"):
        conversation(status="deletion_pending")
    with pytest.raises(ConversationError, match="must not carry a deletion record"):
        conversation(deletion=ConversationDeletion(NOW, "purge_all_records"))
    with pytest.raises(ConversationError, match="deletion mode"):
        ConversationDeletion(NOW, "purge_some_things")
    tombstoned = conversation(status="deleted", deletion=ConversationDeletion(NOW, "purge_content_keep_tombstone", NOW))
    assert tombstoned.deletion is not None and tombstoned.deletion.mode == "purge_content_keep_tombstone"


# --- Criterion 2: turn shape, committed/interrupted state, hash, immutability ---


def test_turn_and_conversation_records_are_frozen_append_only():
    record = turn("turn_root_0001")
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.status = "failed"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.content = ()  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        conversation().status = "archived"  # type: ignore[misc]
    assert isinstance(record.content, tuple)
    assert isinstance(record.voice_events, tuple)


def test_turn_represents_committed_and_interrupted_states_with_partial_content():
    committed = turn("turn_root_0001", role="user", status="complete")
    assert committed.committed and committed.terminal and not committed.interrupted

    partial = (ContentBlock("text", "Before claiming it you should"),)
    interrupted = turn(
        "turn_reply_0001", parent="turn_root_0001", role="assistant", status="interrupted", content=partial,
    )
    assert interrupted.interrupted and interrupted.terminal and not interrupted.committed
    assert interrupted.content == partial
    assert interrupted.role == "assistant"

    with pytest.raises(ConversationError, match="status"):
        turn("turn_root_0002", status="done")
    # Streaming has no completed_at; every terminal state requires one.
    with pytest.raises(ConversationError, match="completed_at"):
        make_turn(
            id="turn_root_0003", conversation_id="conv_alpha_0001", role="user", mode="ordinary",
            status="streaming", lineage=TurnLineage(None, 0), redaction=REDACTED,
            created_at=NOW, completed_at=NOW, content_hash_key=KEY,
        )
    with pytest.raises(ConversationError, match="completed_at"):
        make_turn(
            id="turn_root_0004", conversation_id="conv_alpha_0001", role="user", mode="ordinary",
            status="interrupted", lineage=TurnLineage(None, 0), redaction=REDACTED,
            created_at=NOW, completed_at=None, content_hash_key=KEY,
        )


def test_content_hash_is_a_keyed_deterministic_content_sensitive_fingerprint():
    blocks = (ContentBlock("text", "hello"), ContentBlock("summary", "short"))
    first = turn_content_hash(blocks, key=KEY)
    assert re.fullmatch(r"hmac-sha256:[a-f0-9]{64}", first)
    assert turn_content_hash(tuple(blocks), key=KEY) == first
    assert turn_content_hash((ContentBlock("text", "hello!"), ContentBlock("summary", "short")), key=KEY) != first
    assert turn_content_hash((), key=KEY) != first

    # PRD R008: identical content under DIFFERENT server keys must yield
    # different fingerprints — persisted metadata is no dictionary oracle.
    assert turn_content_hash(blocks, key=OTHER_KEY) != first
    # A weak, missing, or non-bytes key is refused fail-closed.
    with pytest.raises(ConversationError, match="content hash key"):
        turn_content_hash(blocks, key=b"short")
    with pytest.raises(ConversationError, match="content hash key"):
        turn_content_hash(blocks, key="a-string-not-bytes-but-long-enough")  # type: ignore[arg-type]

    # A malformed stored fingerprint is refused at construction; the old
    # unkeyed sha256: form is no longer accepted.
    with pytest.raises(ConversationError, match="content hash"):
        Turn(
            id="turn_root_0001", conversation_id="conv_alpha_0001", role="user", mode="ordinary",
            status="complete", lineage=TurnLineage(None, 0), content=blocks,
            content_hash="sha256:" + "0" * 64, redaction=REDACTED, created_at=NOW, completed_at=NOW,
        )
    assert turn("turn_root_0001").content_hash == turn_content_hash((ContentBlock("text", "hello"),), key=KEY)


def test_verify_content_hash_needs_the_hub_key_and_refuses_stale_hashes():
    blocks = (ContentBlock("text", "hello"),)
    live = turn("turn_root_0001", content=blocks)
    live.verify_content_hash(KEY)
    # The right shape under the wrong key never verifies.
    with pytest.raises(ConversationError, match="does not match"):
        live.verify_content_hash(OTHER_KEY)
    # A stored hash that does not recompute from the stored content is
    # detected by keyed verification, so a rewrite can never hide behind a
    # stale hash.
    stale = Turn(
        id="turn_root_0002", conversation_id="conv_alpha_0001", role="user", mode="ordinary",
        status="complete", lineage=TurnLineage(None, 0), content=blocks,
        content_hash=turn_content_hash((), key=KEY), redaction=REDACTED, created_at=NOW, completed_at=NOW,
    )
    with pytest.raises(ConversationError, match="does not match"):
        stale.verify_content_hash(KEY)


def test_turn_lineage_and_metadata_only_redaction_are_typed():
    with pytest.raises(ConversationError, match="must name its parent"):
        TurnLineage(None, 1, "retry")
    with pytest.raises(ConversationError, match="sibling index"):
        TurnLineage("turn_root_0001", 4097, "branch")
    with pytest.raises(ConversationError, match="lineage kind"):
        TurnLineage("turn_root_0001", 0, "rewrite")
    with pytest.raises(ConversationError, match="own lineage parent"):
        turn("turn_root_0001", parent="turn_root_0001", lineage_kind="branch")
    with pytest.raises(ConversationError, match="content kind"):
        ContentBlock("reasoning", "hidden")
    with pytest.raises(ConversationError, match="voice event"):
        VoiceEvent("audio_frame", NOW)
    # metadata_only turns persist counters and hashes, never content text.
    with pytest.raises(ConversationError, match="metadata_only"):
        make_turn(
            id="turn_root_0002", conversation_id="conv_alpha_0001", role="user", mode="ordinary",
            status="complete", lineage=TurnLineage(None, 0),
            content=(ContentBlock("text", "should not persist"),),
            redaction=TurnRedaction("metadata_only", "workbench.default"),
            created_at=NOW, completed_at=NOW, content_hash_key=KEY,
        )


# --- Criterion 3: append-time lineage invariants and ownership boundary ---


def test_append_accepts_root_reply_retry_and_branch_lineage():
    convo = conversation()
    root = validate_turn_append(convo, [], turn("turn_root_0001"))
    reply = validate_turn_append(
        convo, [root], turn("turn_reply_0001", parent="turn_root_0001", role="assistant"),
    )
    retry = validate_turn_append(
        convo, [root, reply],
        turn("turn_reply_0002", parent="turn_root_0001", sibling=1, lineage_kind="retry", role="assistant"),
    )
    branch = validate_turn_append(
        convo, [root, reply, retry],
        turn("turn_branch_0001", parent="turn_reply_0002", lineage_kind="branch"),
    )
    assert [item.id for item in (root, reply, retry, branch)] == [
        "turn_root_0001", "turn_reply_0001", "turn_reply_0002", "turn_branch_0001",
    ]


def test_append_refuses_a_second_null_parent_root():
    convo = conversation()
    root = turn("turn_root_0001")
    with pytest.raises(ConversationError, match="single null-parent root"):
        validate_turn_append(convo, [root], turn("turn_root_0002"))


def test_append_refuses_a_taken_parent_sibling_slot():
    convo = conversation()
    root = turn("turn_root_0001")
    reply = turn("turn_reply_0001", parent="turn_root_0001", role="assistant")
    with pytest.raises(ConversationError, match="already taken"):
        validate_turn_append(
            convo, [root, reply],
            turn("turn_reply_0002", parent="turn_root_0001", sibling=0, lineage_kind="retry", role="assistant"),
        )


def test_append_refuses_a_missing_parent_in_this_conversation():
    convo = conversation()
    root = turn("turn_root_0001")
    with pytest.raises(ConversationError, match="does not exist in this conversation"):
        validate_turn_append(convo, [root], turn("turn_reply_0001", parent="turn_ghost_0001", lineage_kind="branch"))


def test_append_refuses_a_parent_owned_by_another_conversation():
    convo = conversation()
    other = conversation("conv_other_0001")
    foreign_parent = turn("turn_foreign_001", other.id)
    # The parent turn id genuinely exists — but in the other conversation, so
    # criterion 3 requires the append to be refused on the ownership boundary.
    with pytest.raises(ConversationError, match="cannot cross conversation ownership"):
        validate_turn_append(
            convo, [foreign_parent],
            turn("turn_reply_0001", parent="turn_foreign_001", lineage_kind="branch"),
        )
    with pytest.raises(ConversationError, match="cannot cross conversation ownership"):
        validate_turn_append(other, [foreign_parent], turn("turn_reply_0002", convo.id, parent="turn_foreign_001", lineage_kind="branch"))


def test_append_refuses_cycles_in_existing_lineage_instead_of_extending_them():
    convo = conversation()
    # Per-record validation cannot see other records, so a corrupted store
    # could hold a two-turn cycle; the append gate must refuse to extend it.
    looped_a = turn("turn_loop_a_001", parent="turn_loop_b_001", lineage_kind="branch")
    looped_b = turn("turn_loop_b_001", parent="turn_loop_a_001", sibling=1, lineage_kind="branch")
    with pytest.raises(ConversationError, match="cycle"):
        validate_turn_append(
            convo, [looped_a, looped_b],
            turn("turn_child_0001", parent="turn_loop_a_001", sibling=2, lineage_kind="branch"),
        )


def test_append_is_append_only_and_scoped_to_an_active_owned_conversation():
    convo = conversation()
    root = turn("turn_root_0001")
    with pytest.raises(ConversationError, match="append-only"):
        validate_turn_append(convo, [root], turn("turn_root_0001"))
    with pytest.raises(ConversationError, match="does not belong to this conversation"):
        validate_turn_append(convo, [], turn("turn_root_0002", "conv_other_0001"))
    archived = conversation(status="archived")
    with pytest.raises(ConversationError, match="does not accept turn appends"):
        validate_turn_append(archived, [], turn("turn_root_0003"))


def test_append_enforces_the_retention_to_content_kind_mapping():
    stt = (VoiceEvent("stt_commit", NOW, transcript_chars=12),)
    transcript = (ContentBlock("transcript", "hello there"),)

    voice_metadata_only = conversation(retention=retention(voice="metadata_only"))
    with pytest.raises(ConversationError, match="voice_transcript_text=metadata_only"):
        validate_turn_append(voice_metadata_only, [], turn("turn_root_0001", content=transcript, voice_events=stt))
    # Only the transcript kind is governed: a text block still persists, and
    # bounded voice counters always survive.
    kept = validate_turn_append(voice_metadata_only, [], turn("turn_root_0001", voice_events=stt))
    assert kept.voice_events[0].transcript_chars == 12

    text_metadata_only = conversation(retention=retention(transcript="metadata_only"))
    with pytest.raises(ConversationError, match="transcript_text=metadata_only"):
        validate_turn_append(text_metadata_only, [], turn("turn_root_0001", content=transcript))
    assert validate_turn_append(text_metadata_only, [], turn("turn_root_0001", content=transcript, voice_events=stt))

    retained = conversation()
    assert validate_turn_append(retained, [], turn("turn_root_0001", content=transcript, voice_events=stt))


# --- Criterion 4: audit shapes carry lifecycle and hash metadata, no content ---


def test_audit_models_structurally_carry_no_message_content_field():
    scalar_annotations = {"str", "int", "bool", "str | None", "datetime", "datetime | None"}
    forbidden_names = {"text", "content", "title", "body", "message", "blocks", "transcript", "voice_events"}
    for model in (TurnAudit, ConversationAudit):
        for item in dataclasses.fields(model):
            assert item.name not in forbidden_names, f"{model.__name__}.{item.name}"
            assert not item.name.endswith("_text"), f"{model.__name__}.{item.name}"
            # Every audit field is a scalar: no ContentBlock, tuple, or dict
            # shape exists that could smuggle message content into audit rows.
            assert item.type in scalar_annotations, f"{model.__name__}.{item.name}: {item.type}"
    assert {item.name for item in dataclasses.fields(TurnAudit)} == {
        "turn_id", "conversation_id", "role", "mode", "status", "lineage_kind", "parent_turn_id",
        "sibling_index", "content_hash", "content_block_count", "voice_event_count", "content_purged",
        "created_at", "completed_at",
    }
    assert {item.name for item in dataclasses.fields(ConversationAudit)} == {
        "conversation_id", "actor_id", "status", "retention_policy_id", "deletion_mode",
        "turn_count", "committed_turn_count", "interrupted_turn_count", "created_at", "updated_at",
    }


def test_audit_projections_keep_lifecycle_lineage_and_hash_metadata_only():
    convo = conversation(status="deleted", deletion=ConversationDeletion(NOW, "purge_content_keep_tombstone", NOW))
    root = turn("turn_root_0001")
    partial = turn("turn_reply_0001", parent="turn_root_0001", role="assistant", status="interrupted",
                   content=(ContentBlock("text", "partial"),))
    stray = turn("turn_other_0001", "conv_other_0001")

    record = conversation_audit(convo, [root, partial, stray])
    assert record.turn_count == 2
    assert record.committed_turn_count == 1
    assert record.interrupted_turn_count == 1
    assert record.deletion_mode == "purge_content_keep_tombstone"
    assert record.retention_policy_id == "workbench.default-90d"

    row = turn_audit(partial)
    assert row.status == "interrupted"
    assert row.parent_turn_id == "turn_root_0001" and row.sibling_index == 0 and row.lineage_kind == "initial"
    assert row.content_hash == partial.content_hash
    assert row.content_block_count == 1 and row.voice_event_count == 0


def test_purge_turn_content_tombstone_keeps_lifecycle_and_fingerprint_only():
    live = turn(
        "turn_reply_0001", parent="turn_root_0001", role="assistant",
        content=(ContentBlock("text", "the secret answer"),),
        voice_events=(VoiceEvent("tts_start", NOW),),
    )
    tombstone = purge_turn_content(live)
    # Content is removed from the record, not flagged over.
    assert tombstone.content == () and tombstone.content_purged is True
    # Lifecycle, lineage, typed voice events, and the keyed fingerprint survive.
    assert (tombstone.id, tombstone.role, tombstone.mode, tombstone.status) == (live.id, live.role, live.mode, live.status)
    assert tombstone.lineage == live.lineage and tombstone.voice_events == live.voice_events
    assert (tombstone.created_at, tombstone.completed_at) == (live.created_at, live.completed_at)
    assert tombstone.content_hash == live.content_hash
    assert "secret answer" not in repr(tombstone)
    # A purged fingerprint deliberately cannot recompute, even with the key.
    with pytest.raises(ConversationError, match="cannot recompute"):
        tombstone.verify_content_hash(KEY)
    # Idempotent, and the purge is one-way: a purged record refuses content.
    assert purge_turn_content(tombstone) is tombstone
    with pytest.raises(ConversationError, match="content-purged"):
        dataclasses.replace(tombstone, content=(ContentBlock("text", "resurrected"),))
    with pytest.raises(ConversationError, match="purge requires a Turn"):
        purge_turn_content("turn_reply_0001")  # type: ignore[arg-type]
    # The audit projection records the purge without any content field.
    row = turn_audit(tombstone)
    assert row.content_purged is True and row.content_hash == live.content_hash


def test_deleted_tombstone_conversation_must_not_retain_a_title_and_ceiling_is_typed():
    with pytest.raises(ConversationError, match="must not retain a title"):
        conversation(
            status="deleted", title="still here",
            deletion=ConversationDeletion(NOW, "purge_content_keep_tombstone", NOW),
        )
    # The contract's only age ceiling is delete_after; it must be tz-aware.
    with pytest.raises(ConversationError, match="delete_after"):
        RetentionPolicy("workbench.default-90d", "retained_redacted", "retained_redacted",
                        delete_after=datetime(2026, 7, 19))
    with pytest.raises(ConversationError, match="delete_after"):
        RetentionPolicy("workbench.default-90d", "retained_redacted", "retained_redacted",
                        delete_after="2026-07-19T00:00:00Z")  # type: ignore[arg-type]
    bounded = RetentionPolicy("workbench.default-90d", "retained_redacted", "retained_redacted", delete_after=NOW)
    assert bounded.delete_after == NOW


def test_tts_only_voice_output_never_relaxes_the_text_retention_policy():
    tts_only = (VoiceEvent("tts_start", NOW),)
    transcript = (ContentBlock("transcript", "secret words"),)
    text_metadata_only = conversation(retention=retention(transcript="metadata_only"))

    with pytest.raises(ConversationError, match="transcript_text=metadata_only"):
        validate_turn_append(
            text_metadata_only, [], turn("turn_root_0001", content=transcript, voice_events=tts_only)
        )

    # Voice-INPUT events still route governance to the voice policy.
    stt = (VoiceEvent("stt_commit", NOW, transcript_chars=12),)
    assert validate_turn_append(
        text_metadata_only, [], turn("turn_root_0001", content=transcript, voice_events=stt)
    )
