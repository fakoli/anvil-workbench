"""Actor-scoped conversation store: ownership, lineage appends, reload recovery.

Each test group maps to an acceptance criterion of chat-first-voice:T002.2:

1. Every store operation requires actor identity and cannot return or mutate
   another actor's conversation (cross-actor probes are indistinguishable
   from a missing id).
2. Appending or retrying creates a new lineage-linked turn without changing
   committed history, and every append is refused by the store itself when it
   violates a ``validate_turn_append`` invariant or the retention gate.
3. Reload (a fresh store instance over the same persisted rows) restores
   committed turns in lineage order and marks unfinished streaming turns
   interrupted — never silently complete.
4. Search and archive operations remain scoped to the owning actor.
"""
from __future__ import annotations

import hashlib

import pytest

from workbench.conversation_models import (
    ContentBlock,
    ConversationActor,
    ConversationAudit,
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
# Hermetic fixture keys for the server-keyed content fingerprint (PRD R008).
KEY = b"store-test-content-hash-key-A-01"
OTHER_KEY = b"store-test-content-hash-key-B-02"


def retention(transcript: str = "retained_redacted", voice: str = "retained_redacted") -> RetentionPolicy:
    return RetentionPolicy("workbench.default-90d", transcript, voice)


def store_with_conversation(actor: ConversationActor = ALICE, title: str = "Kickoff chat"):
    store = MemoryConversationStore(content_hash_key=KEY)
    conversation = store.create_conversation(actor, retention(), title=title)
    return store, conversation


def append_root(store: MemoryConversationStore, actor: ConversationActor, conversation_id: str, text: str = "hello"):
    return store.append_turn(
        actor, conversation_id, role="user", status="complete",
        lineage=TurnLineage(None, 0, "initial"), redaction=REDACTED,
        content=(ContentBlock("text", text),),
    )


def append_child(
    store: MemoryConversationStore, actor: ConversationActor, conversation_id: str,
    parent_id: str, *, sibling: int = 0, role: str = "assistant", status: str = "complete",
    text: str = "reply", kind: str = "branch",
):
    return store.append_turn(
        actor, conversation_id, role=role, status=status,
        lineage=TurnLineage(parent_id, sibling, kind), redaction=REDACTED,
        content=(ContentBlock("text", text),),
    )


# --- Criterion 1: actor identity is required; no cross-actor read or write ---


def test_every_operation_requires_a_typed_actor_identity():
    store, conversation = store_with_conversation()
    with pytest.raises(ConversationStoreError, match="acting ConversationActor"):
        store.create_conversation("operator_alice", retention())  # type: ignore[arg-type]
    with pytest.raises(ConversationStoreError, match="acting ConversationActor"):
        store.get_conversation("operator_alice", conversation.id)  # type: ignore[arg-type]
    with pytest.raises(ConversationStoreError, match="acting ConversationActor"):
        store.list_conversations(None)  # type: ignore[arg-type]
    with pytest.raises(ConversationStoreError, match="acting ConversationActor"):
        store.search_conversations(object(), "kickoff")  # type: ignore[arg-type]


def test_cross_actor_probes_are_indistinguishable_from_a_missing_conversation():
    store, conversation = store_with_conversation(ALICE)
    root = append_root(store, ALICE, conversation.id)

    def probes(actor, conversation_id):
        yield lambda: store.get_conversation(actor, conversation_id)
        yield lambda: store.get_conversation_with_turns(actor, conversation_id)
        yield lambda: store.rename_conversation(actor, conversation_id, "hijacked")
        yield lambda: store.archive_conversation(actor, conversation_id)
        yield lambda: store.unarchive_conversation(actor, conversation_id)
        yield lambda: append_root(store, actor, conversation_id)
        yield lambda: store.retry_turn(
            actor, conversation_id, root.id, role="assistant", status="complete", redaction=REDACTED,
        )
        yield lambda: store.branch_turn(
            actor, conversation_id, root.id, role="assistant", status="complete", redaction=REDACTED,
        )
        yield lambda: store.advance_turn_status(actor, conversation_id, root.id, "complete")

    # Bob probing Alice's conversation raises exactly the same typed error and
    # message as anyone probing an id that does not exist: no existence leak.
    for foreign, missing in zip(probes(BOB, conversation.id), probes(ALICE, "conv_does_not_exist")):
        with pytest.raises(UnknownConversationError) as foreign_error:
            foreign()
        with pytest.raises(UnknownConversationError) as missing_error:
            missing()
        assert str(foreign_error.value) == str(missing_error.value) == "unknown conversation"

    # Nothing was mutated by the probes.
    unchanged, turns = store.get_conversation_with_turns(ALICE, conversation.id)
    assert unchanged.title == "Kickoff chat" and unchanged.status == "active"
    assert [turn.id for turn in turns] == [root.id]


def test_list_and_search_never_return_another_actors_conversations():
    store = MemoryConversationStore(content_hash_key=KEY)
    store.create_conversation(ALICE, retention(), title="Shared secret plan")
    store.create_conversation(BOB, retention(), title="Shared secret plan")
    assert {record.actor.actor_id for record in store.list_conversations(ALICE)} == {"operator_alice"}
    assert {record.actor.actor_id for record in store.list_conversations(BOB)} == {"operator_bob"}
    found = store.search_conversations(ALICE, "secret plan")
    assert len(found) == 1 and found[0].actor == ALICE


def test_ownership_compares_the_full_actor_identity_value():
    store, conversation = store_with_conversation(ALICE)
    with pytest.raises(UnknownConversationError):
        # A near-miss identity never resolves another actor's conversation.
        store.get_conversation(ConversationActor("operator_alice2"), conversation.id)


# --- Criterion 2: append/retry are lineage-linked and never rewrite history ---


def test_append_and_retry_create_lineage_linked_turns_without_rewriting_history():
    store, conversation = store_with_conversation()
    root = append_root(store, ALICE, conversation.id, "make a plan")
    first_reply = append_child(store, ALICE, conversation.id, root.id, text="plan v1", kind="initial")

    retry = store.retry_turn(
        ALICE, conversation.id, first_reply.id, role="assistant", status="complete",
        redaction=REDACTED, content=(ContentBlock("text", "plan v2"),),
    )
    assert retry.lineage.kind == "retry"
    assert retry.lineage.parent_turn_id == root.id
    assert retry.lineage.sibling_index == first_reply.lineage.sibling_index + 1

    branch = store.branch_turn(
        ALICE, conversation.id, retry.id, role="user", status="complete",
        redaction=REDACTED, content=(ContentBlock("text", "explore option B"),),
    )
    assert branch.lineage == TurnLineage(retry.id, 0, "branch")

    # Committed history is untouched: the original records are identical.
    _, turns = store.get_conversation_with_turns(ALICE, conversation.id)
    by_id = {turn.id: turn for turn in turns}
    assert by_id[root.id] == root and by_id[first_reply.id] == first_reply
    assert len(turns) == 4


def test_retrying_the_root_turn_is_refused():
    store, conversation = store_with_conversation()
    root = append_root(store, ALICE, conversation.id)
    with pytest.raises(ConversationStoreError, match="root turn cannot be retried"):
        store.retry_turn(ALICE, conversation.id, root.id, role="user", status="complete", redaction=REDACTED)


def test_branch_or_retry_of_an_unknown_turn_is_refused():
    store, conversation = store_with_conversation()
    append_root(store, ALICE, conversation.id)
    with pytest.raises(ConversationStoreError, match="unknown turn"):
        store.branch_turn(ALICE, conversation.id, "turn_missing_00", role="user", status="complete", redaction=REDACTED)
    with pytest.raises(ConversationStoreError, match="unknown turn"):
        store.retry_turn(ALICE, conversation.id, "turn_missing_00", role="user", status="complete", redaction=REDACTED)


def test_invariant_violating_appends_are_refused_by_the_store_itself():
    store, conversation = store_with_conversation()
    root = append_root(store, ALICE, conversation.id)
    reply = append_child(store, ALICE, conversation.id, root.id, kind="initial")

    # Second null-parent root.
    with pytest.raises(ConversationStoreError, match="single null-parent root"):
        append_root(store, ALICE, conversation.id, "second root")
    # Duplicate (parent, sibling_index) slot.
    with pytest.raises(ConversationStoreError, match="already taken"):
        append_child(store, ALICE, conversation.id, root.id, sibling=reply.lineage.sibling_index)
    # Nonexistent parent.
    with pytest.raises(ConversationStoreError, match="parent does not exist"):
        append_child(store, ALICE, conversation.id, "turn_notthere0", sibling=1)
    # Duplicate turn id (append-only).
    with pytest.raises(ConversationStoreError, match="append-only"):
        store.append_turn(
            ALICE, conversation.id, role="user", status="complete",
            lineage=TurnLineage(reply.id, 5, "branch"), redaction=REDACTED, turn_id=root.id,
        )
    # None of the refused appends changed the persisted history.
    _, turns = store.get_conversation_with_turns(ALICE, conversation.id)
    assert [turn.id for turn in turns] == [root.id, reply.id]


def test_same_actor_cross_conversation_parent_is_refused_but_cross_actor_parent_never_leaks():
    store = MemoryConversationStore(content_hash_key=KEY)
    alice_a = store.create_conversation(ALICE, retention(), title="A")
    alice_b = store.create_conversation(ALICE, retention(), title="B")
    bob_c = store.create_conversation(BOB, retention(), title="C")
    parent_in_a = append_root(store, ALICE, alice_a.id)
    parent_in_c = append_root(store, BOB, bob_c.id)
    append_root(store, ALICE, alice_b.id)

    # Same actor, different conversation: the ownership boundary is named.
    with pytest.raises(ConversationStoreError, match="different conversation"):
        append_child(store, ALICE, alice_b.id, parent_in_a.id, sibling=0)
    # Another actor's turn id is indistinguishable from a nonexistent parent.
    with pytest.raises(ConversationStoreError, match="parent does not exist"):
        append_child(store, ALICE, alice_b.id, parent_in_c.id, sibling=0)


def test_retention_gate_is_enforced_by_the_store_append_path():
    store = MemoryConversationStore(content_hash_key=KEY)
    conversation = store.create_conversation(ALICE, retention(transcript="metadata_only"), title="No transcripts")
    with pytest.raises(ConversationStoreError, match="metadata_only forbids persisting transcript"):
        store.append_turn(
            ALICE, conversation.id, role="user", status="complete",
            lineage=TurnLineage(None, 0, "initial"), redaction=REDACTED,
            content=(ContentBlock("transcript", "verbatim words"),),
        )
    # Voice-governed transcript is checked against the voice policy instead.
    voice_conv = store.create_conversation(ALICE, retention(voice="metadata_only"), title="No voice text")
    with pytest.raises(ConversationStoreError, match="voice_transcript_text=metadata_only"):
        store.append_turn(
            ALICE, voice_conv.id, role="user", status="complete",
            lineage=TurnLineage(None, 0, "initial"), redaction=REDACTED,
            content=(ContentBlock("transcript", "spoken words"),),
            voice_events=(VoiceEvent("stt_commit", now_utc(), transcript_chars=12),),
        )


def test_appends_to_an_archived_conversation_are_refused():
    store, conversation = store_with_conversation()
    append_root(store, ALICE, conversation.id)
    store.archive_conversation(ALICE, conversation.id)
    with pytest.raises(ConversationStoreError, match="does not accept turn appends"):
        append_child(store, ALICE, conversation.id, "turn_whatever0", sibling=0)


def test_terminal_turns_are_committed_history_and_cannot_change_status():
    store, conversation = store_with_conversation()
    root = append_root(store, ALICE, conversation.id)
    streaming = store.append_turn(
        ALICE, conversation.id, role="assistant", status="streaming",
        lineage=TurnLineage(root.id, 0, "initial"), redaction=REDACTED,
        content=(ContentBlock("text", "partial"),),
    )
    completed = store.advance_turn_status(ALICE, conversation.id, streaming.id, "complete")
    assert completed.status == "complete" and completed.completed_at is not None
    with pytest.raises(ConversationStoreError, match="committed history is immutable"):
        store.advance_turn_status(ALICE, conversation.id, streaming.id, "interrupted")
    with pytest.raises(ConversationStoreError, match="terminal state"):
        store.advance_turn_status(ALICE, conversation.id, root.id, "streaming")


# --- Criterion 3: reload restores committed order; unfinished turns interrupt ---


def test_reload_restores_committed_turns_in_lineage_order_and_interrupts_streaming():
    store, conversation = store_with_conversation()
    root = append_root(store, ALICE, conversation.id, "question")
    reply = append_child(store, ALICE, conversation.id, root.id, text="answer v1", kind="initial")
    retry = store.retry_turn(
        ALICE, conversation.id, reply.id, role="assistant", status="complete",
        redaction=REDACTED, content=(ContentBlock("text", "answer v2"),),
    )
    unfinished = store.append_turn(
        ALICE, conversation.id, role="assistant", status="streaming",
        lineage=TurnLineage(retry.id, 0, "initial"), redaction=REDACTED,
        content=(ContentBlock("text", "partial stream"),),
    )

    # Simulated hub restart: a fresh store instance over the same persisted rows.
    reopened = MemoryConversationStore(store.rows, content_hash_key=KEY)
    recovered = reopened.recover_streaming_turns()
    assert [audit.turn_id for audit in recovered] == [unfinished.id]
    assert all(isinstance(audit, TurnAudit) and audit.status == "interrupted" for audit in recovered)

    restored, turns = reopened.get_conversation_with_turns(ALICE, conversation.id)
    assert restored.id == conversation.id
    # Committed turns are restored unchanged, in lineage order.
    assert [turn.id for turn in turns] == [root.id, reply.id, retry.id, unfinished.id]
    assert turns[0] == root and turns[1] == reply and turns[2] == retry
    # The unfinished turn is interrupted with its partial content preserved —
    # never surfaced as complete.
    marked = turns[3]
    assert marked.status == "interrupted" and marked.completed_at is not None
    assert marked.content == unfinished.content and marked.content_hash == unfinished.content_hash
    # Recovery is idempotent: nothing left to flip.
    assert reopened.recover_streaming_turns() == ()


def test_lineage_order_visits_siblings_by_index_depth_first():
    store, conversation = store_with_conversation()
    root = append_root(store, ALICE, conversation.id)
    first = append_child(store, ALICE, conversation.id, root.id, sibling=0, kind="initial", text="first")
    deep = append_child(store, ALICE, conversation.id, first.id, sibling=0, text="deep")
    second = append_child(store, ALICE, conversation.id, root.id, sibling=1, kind="retry", text="second")
    _, turns = store.get_conversation_with_turns(ALICE, conversation.id)
    assert [turn.id for turn in turns] == [root.id, first.id, deep.id, second.id]


# --- Criterion 4: search and archive stay scoped to the owning actor ---


def test_archive_and_unarchive_lifecycle_stays_actor_scoped():
    store, conversation = store_with_conversation(ALICE, title="Archive me")
    archived = store.archive_conversation(ALICE, conversation.id)
    assert archived.status == "archived"
    with pytest.raises(ConversationStoreError, match="only an active conversation"):
        store.archive_conversation(ALICE, conversation.id)

    assert store.list_conversations(ALICE) == []
    assert [record.id for record in store.list_conversations(ALICE, include_archived=True)] == [conversation.id]
    assert store.search_conversations(ALICE, "archive") == []
    assert len(store.search_conversations(ALICE, "archive", include_archived=True)) == 1
    # The other actor can neither see nor unarchive it.
    assert store.list_conversations(BOB, include_archived=True) == []
    with pytest.raises(UnknownConversationError):
        store.unarchive_conversation(BOB, conversation.id)

    restored = store.unarchive_conversation(ALICE, conversation.id)
    assert restored.status == "active"
    with pytest.raises(ConversationStoreError, match="only an archived conversation"):
        store.unarchive_conversation(ALICE, conversation.id)


def test_search_is_title_metadata_only_and_never_matches_turn_content():
    store, conversation = store_with_conversation(ALICE, title="Weekly sync")
    append_root(store, ALICE, conversation.id, "the launch codes are hidden here")
    assert store.search_conversations(ALICE, "launch codes") == []
    assert [record.id for record in store.search_conversations(ALICE, "weekly")] == [conversation.id]
    with pytest.raises(ConversationStoreError, match="non-empty"):
        store.search_conversations(ALICE, "   ")


def test_rename_updates_title_fail_closed_and_bumps_updated_at():
    store, conversation = store_with_conversation(ALICE, title="Old title")
    renamed = store.rename_conversation(ALICE, conversation.id, "New title")
    assert renamed.title == "New title"
    assert renamed.updated_at >= conversation.updated_at
    with pytest.raises(ConversationStoreError, match="title is invalid"):
        store.rename_conversation(ALICE, conversation.id, "x" * 500)
    with pytest.raises(UnknownConversationError):
        store.rename_conversation(BOB, conversation.id, "Bob was here")
    assert store.get_conversation(ALICE, conversation.id).title == "New title"


# --- Audit projections stay content-free ---


def test_audit_projections_never_carry_titles_or_message_content():
    store, conversation = store_with_conversation(ALICE, title="Secret project title")
    append_root(store, ALICE, conversation.id, "secret message body")
    store.rename_conversation(ALICE, conversation.id, "Renamed secret title")
    store.archive_conversation(ALICE, conversation.id)

    events = store.list_audit(limit=50)
    assert {event.kind for event in events} == {
        "conversation.created", "turn.appended", "conversation.renamed", "conversation.archived",
    }
    for event in events:
        assert isinstance(event.record, (ConversationAudit, TurnAudit))
    dumped = repr(events)
    assert "secret" not in dumped.lower()
    assert "message body" not in dumped


def test_audit_content_hash_is_no_equality_oracle_without_the_hub_key():
    """The keyed fingerprint closes the content-equality/dictionary oracle.

    A shared audit stream once exposed a deterministic unsalted digest, so a
    reader could test guessed low-entropy prompts for equality.  With the
    HMAC conversion, equal content under different hub keys yields different
    values, and a party without the key cannot recompute either one.
    """
    guessable = "yes"
    store_a = MemoryConversationStore(content_hash_key=KEY)
    store_b = MemoryConversationStore(content_hash_key=OTHER_KEY)
    conv_a = store_a.create_conversation(ALICE, retention(), title="A")
    conv_b = store_b.create_conversation(BOB, retention(), title="B")
    turn_a = append_root(store_a, ALICE, conv_a.id, guessable)
    turn_b = append_root(store_b, BOB, conv_b.id, guessable)

    hashes = {
        event.record.content_hash
        for event in store_a.list_audit() + store_b.list_audit()
        if hasattr(event.record, "content_hash")
    }
    assert turn_a.content_hash in hashes and turn_b.content_hash in hashes
    # Identical content under DIFFERENT keys: different fingerprints.
    assert turn_a.content_hash != turn_b.content_hash
    # A keyless dictionary attacker recomputing plain SHA-256 over every
    # plausible canonicalization of the guessed content never matches.
    guesses = {
        "hmac-sha256:" + hashlib.sha256(prefix + body).hexdigest()
        for prefix in (b"", b"anvil-workbench/chat-turn-content/v1\0")
        for body in (
            guessable.encode(),
            b'[{"content_trust":"untrusted_task_data","kind":"text","text":"yes"}]',
        )
    } | {
        "sha256:" + hashlib.sha256(body).hexdigest()
        for body in (guessable.encode(),)
    }
    assert not (guesses & hashes)
    # Within one hub the fingerprint is still deterministic (integrity holds).
    conv_a2 = store_a.create_conversation(ALICE, retention(), title="A2")
    assert append_root(store_a, ALICE, conv_a2.id, guessable).content_hash == turn_a.content_hash


def test_content_hash_key_is_constructor_held_and_never_persisted_in_rows():
    store, conversation = store_with_conversation()
    append_root(store, ALICE, conversation.id, "hello")
    # The key lives on the instance, never inside the persisted row containers.
    dumped = repr(store.rows)
    assert KEY.decode() not in dumped and repr(KEY) not in dumped
    # A fresh instance over the same rows must be handed the key again, and a
    # weak or missing key is refused fail-closed.
    with pytest.raises(TypeError):
        MemoryConversationStore(store.rows)  # type: ignore[call-arg]
    with pytest.raises(ConversationStoreError, match="content hash key"):
        MemoryConversationStore(store.rows, content_hash_key=b"short")
    reopened = MemoryConversationStore(store.rows, content_hash_key=KEY)
    _, turns = reopened.get_conversation_with_turns(ALICE, conversation.id)
    assert [turn.content[0].text for turn in turns] == ["hello"]
    # Reads re-verify the keyed fingerprint: the wrong key fails closed
    # instead of serving content whose integrity cannot be established.
    wrong = MemoryConversationStore(store.rows, content_hash_key=OTHER_KEY)
    with pytest.raises(ConversationStoreError, match="does not match"):
        wrong.get_conversation_with_turns(ALICE, conversation.id)


def test_retry_overflow_raises_the_typed_store_error():
    store, conversation = store_with_conversation()
    root = append_root(store, ALICE, conversation.id)
    child = append_child(
        store, ALICE, conversation.id, root.id, sibling=4096, kind="branch",
    )
    with pytest.raises(ConversationStoreError, match="sibling index"):
        store.retry_turn(
            ALICE, conversation.id, child.id, role="assistant",
            status="complete", redaction=REDACTED, content=(ContentBlock("text", "again"),),
        )


def test_recover_on_open_interrupts_streaming_turns_at_construction():
    store, conversation = store_with_conversation()
    store.append_turn(
        ALICE, conversation.id, role="assistant", status="streaming",
        lineage=TurnLineage(None, 0, "initial"), redaction=REDACTED,
        content=(ContentBlock("text", "partial"),),
    )
    reopened = MemoryConversationStore(store.rows, content_hash_key=KEY, recover_on_open=True)
    _, turns = reopened.get_conversation_with_turns(ALICE, conversation.id)
    assert [turn.status for turn in turns] == ["interrupted"]
