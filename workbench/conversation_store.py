"""Durable actor-scoped conversation store (hub persistence slice).

This module persists ``chat-conversation.v1``/``chat-turn.v1`` records for the
hub: actor-owned conversations plus append-only turns.  Every mutation and
read requires the acting :class:`~workbench.conversation_models.ConversationActor`
and resolves conversations through that identity only — a probe against
another actor's conversation raises the same ``unknown conversation`` error as
a missing id, so record existence never leaks across owners.

Every turn append routes through
:func:`workbench.conversation_models.validate_turn_append`; this store is the
enforcing layer that function's contract names.  The lineage universe handed
to the gate is limited to the acting actor's own conversations, so a
same-actor cross-conversation parent is refused as an ownership violation
while a foreign actor's turn id is indistinguishable from a nonexistent one.

``MemoryConversationStore`` is the hermetic row-backed implementation in the
``MemoryStore`` idiom: all persisted values are frozen dataclasses and the
row containers can be handed to a fresh instance to simulate a process
restart.  After a restart, :meth:`MemoryConversationStore.recover_streaming_turns`
is the recovery path: a turn persisted in ``streaming`` status is surfaced as
``interrupted`` (with ``completed_at`` set), never silently completed.  A
production Postgres projection follows the ``PostgresStore`` idiom and lands
with the API slice; it is not implemented here.

Audit entries carry only the content-free ``TurnAudit``/``ConversationAudit``
shapes — never a title, content block, or transcript.

Retention and deletion (chat-first-voice:T002.3): ``delete_conversation``
honours the contract's two deletion modes and ``enforce_retention`` applies
the ``retention.delete_after`` ceiling — the only age ceiling
``chat-conversation.v1`` declares — plus reconciliation of a crashed
``deletion_pending`` purge.  A purge removes content blocks and titles from
the rows themselves (tombstones, not flags), so expired or deleted content is
unrecoverable through every public store operation, including a fresh
instance opened over the same rows.  Content fingerprints are keyed
HMAC-SHA256 values; the hub key is constructor-injected, held on the
instance only, and never written into the rows.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Protocol

from .conversation_models import (
    Conversation,
    ConversationActor,
    ConversationAudit,
    ConversationDeletion,
    ConversationError,
    ContentBlock,
    RetentionPolicy,
    TERMINAL_TURN_STATUSES,
    Turn,
    TurnAudit,
    TurnLineage,
    TurnRedaction,
    VoiceEvent,
    conversation_audit,
    make_turn,
    purge_turn_content,
    require_content_hash_key,
    turn_audit,
    validate_turn_append,
)
from .models import new_id, now_utc
from .store import StoreError

_LISTABLE_STATUSES = frozenset({"active", "archived"})
_UNKNOWN_CONVERSATION = "unknown conversation"


class ConversationStoreError(StoreError):
    """A conversation store operation violates the chat persistence contract."""


class UnknownConversationError(ConversationStoreError):
    """The conversation does not exist for this actor.

    Raised identically for a missing id and for another actor's conversation,
    so a cross-actor probe cannot learn whether the id exists.
    """


@dataclass(frozen=True)
class ConversationAuditEvent:
    """One content-free store audit entry (lifecycle metadata only)."""

    id: str
    kind: str
    record: ConversationAudit | TurnAudit
    created_at: datetime = field(default_factory=now_utc)


@dataclass
class ConversationRows:
    """The persisted row containers shared by store instances.

    Values are frozen dataclasses; the dict/list containers stand in for the
    durable tables, so a fresh ``MemoryConversationStore`` over the same rows
    simulates a hub restart over the same persisted records.
    """

    conversations: dict[str, Conversation] = field(default_factory=dict)
    turns: dict[str, list[Turn]] = field(default_factory=dict)
    audit: list[ConversationAuditEvent] = field(default_factory=list)


class ConversationStore(Protocol):
    def create_conversation(self, actor: ConversationActor, retention: RetentionPolicy, title: str | None = None) -> Conversation: ...
    def get_conversation(self, actor: ConversationActor, conversation_id: str) -> Conversation: ...
    def list_conversations(self, actor: ConversationActor, include_archived: bool = False) -> list[Conversation]: ...
    def search_conversations(self, actor: ConversationActor, query: str, include_archived: bool = False) -> list[Conversation]: ...
    def rename_conversation(self, actor: ConversationActor, conversation_id: str, title: str) -> Conversation: ...
    def archive_conversation(self, actor: ConversationActor, conversation_id: str) -> Conversation: ...
    def unarchive_conversation(self, actor: ConversationActor, conversation_id: str) -> Conversation: ...
    def append_turn(
        self, actor: ConversationActor, conversation_id: str, *, role: str, status: str,
        lineage: TurnLineage, redaction: TurnRedaction, mode: str = "ordinary",
        content: tuple[ContentBlock, ...] | list[ContentBlock] = (),
        voice_events: tuple[VoiceEvent, ...] | list[VoiceEvent] = (), turn_id: str | None = None,
    ) -> Turn: ...
    def retry_turn(
        self, actor: ConversationActor, conversation_id: str, retry_of_turn_id: str, *, role: str,
        status: str, redaction: TurnRedaction, mode: str = "ordinary",
        content: tuple[ContentBlock, ...] | list[ContentBlock] = (),
        voice_events: tuple[VoiceEvent, ...] | list[VoiceEvent] = (),
    ) -> Turn: ...
    def branch_turn(
        self, actor: ConversationActor, conversation_id: str, parent_turn_id: str, *, role: str,
        status: str, redaction: TurnRedaction, mode: str = "ordinary",
        content: tuple[ContentBlock, ...] | list[ContentBlock] = (),
        voice_events: tuple[VoiceEvent, ...] | list[VoiceEvent] = (),
    ) -> Turn: ...
    def advance_turn_status(self, actor: ConversationActor, conversation_id: str, turn_id: str, status: str) -> Turn: ...
    def get_conversation_with_turns(self, actor: ConversationActor, conversation_id: str) -> tuple[Conversation, tuple[Turn, ...]]: ...
    def delete_conversation(self, actor: ConversationActor, conversation_id: str, mode: str) -> ConversationAudit: ...
    # HUB-INTERNAL / SYSTEM-ONLY: the operations below take no actor and span
    # all actors' records. They must never be wired to an actor-facing
    # endpoint without explicit operator/system authorization and scoping.
    # TurnAudit.content_hash is a KEYED HMAC-SHA256 fingerprint (the hub key
    # is constructor-injected and never persisted with the rows), so a shared
    # audit stream is not a content-equality or dictionary oracle for a party
    # without the key — the API slice must still treat audit access as
    # privileged lifecycle metadata.
    def recover_streaming_turns(self) -> tuple[TurnAudit, ...]: ...
    def enforce_retention(self, now: datetime | None = None) -> tuple[ConversationAudit, ...]: ...
    def list_audit(self, limit: int = 20) -> list[ConversationAuditEvent]: ...


def _lineage_order(turns: list[Turn]) -> tuple[Turn, ...]:
    """Deterministic pre-order over the single-rooted turn tree.

    Children of each parent are visited by ascending ``sibling_index``.  A row
    set that is not one rooted tree (corruption the append gate would have
    refused) fails closed instead of rendering a partial history.
    """
    children: dict[str | None, list[Turn]] = {}
    for turn in turns:
        children.setdefault(turn.lineage.parent_turn_id, []).append(turn)
    for group in children.values():
        group.sort(key=lambda item: item.lineage.sibling_index)
    ordered: list[Turn] = []
    stack = list(reversed(children.get(None, [])))
    while stack:
        node = stack.pop()
        ordered.append(node)
        stack.extend(reversed(children.get(node.id, [])))
    if len(ordered) != len(turns):
        raise ConversationStoreError("persisted turn lineage is not a single rooted tree")
    return tuple(ordered)


class MemoryConversationStore:
    """Hermetic row-backed conversation store; requests are serialized in tests."""

    def __init__(
        self,
        rows: ConversationRows | None = None,
        *,
        content_hash_key: bytes,
        recover_on_open: bool = False,
    ) -> None:
        """Open the store over ``rows`` with the hub-held content-hash key.

        ``content_hash_key`` keys every turn-content fingerprint (HMAC-SHA256).
        It is hub configuration — constructor/environment supplied — and is
        held on the instance only, NEVER written into ``rows``; a fresh
        instance over the same rows must be given the key again.

        After a restart over persisted rows, ``recover_streaming_turns()`` MUST
        run before serving reads, or stale ``streaming`` turns can be advanced
        to ``complete`` and fabricate a finished response; pass
        ``recover_on_open=True`` to bind that recovery to construction.
        Turn IDs are unique per ``(conversation_id, turn_id)`` — a durable
        backend must key on the composite, never a global turn ID, or a
        cross-actor insert refusal becomes an existence oracle.
        """
        try:
            self._content_hash_key = require_content_hash_key(content_hash_key)
        except ConversationError as exc:
            raise ConversationStoreError(str(exc)) from exc
        self.rows = rows if rows is not None else ConversationRows()
        if recover_on_open:
            self.recover_streaming_turns()
            for record in list(self.rows.conversations.values()):
                if record.status == "deletion_pending":
                    self._complete_deletion(record)

    # -- internal helpers -------------------------------------------------

    @staticmethod
    def _require_actor(actor: ConversationActor) -> ConversationActor:
        if not isinstance(actor, ConversationActor):
            raise ConversationStoreError("a store operation requires the acting ConversationActor")
        return actor

    def _owned(self, actor: ConversationActor, conversation_id: str) -> Conversation:
        """Resolve a conversation through the acting actor's ownership only."""
        self._require_actor(actor)
        record = self.rows.conversations.get(conversation_id)
        if record is None or record.actor != actor:
            raise UnknownConversationError(_UNKNOWN_CONVERSATION)
        return record

    def _own_turns(self, conversation_id: str) -> list[Turn]:
        return self.rows.turns.setdefault(conversation_id, [])

    def _actor_turn_universe(self, actor: ConversationActor) -> list[Turn]:
        """All turns of conversations owned by this actor, for the append gate.

        The gate never sees another actor's turns, so a foreign actor's turn
        id used as a lineage parent is indistinguishable from a nonexistent
        one — no cross-actor existence oracle.
        """
        return [
            turn
            for conversation in self.rows.conversations.values()
            if conversation.actor == actor
            for turn in self.rows.turns.get(conversation.id, [])
        ]

    def _append_audit(self, kind: str, record: ConversationAudit | TurnAudit) -> None:
        self.rows.audit.append(ConversationAuditEvent(new_id("audit"), kind, record))

    def _audit_conversation(self, kind: str, record: Conversation) -> None:
        self._append_audit(kind, conversation_audit(record, self.rows.turns.get(record.id, [])))

    def _store_conversation(self, kind: str, record: Conversation) -> Conversation:
        self.rows.conversations[record.id] = record
        self._audit_conversation(kind, record)
        return record

    # -- conversation lifecycle -------------------------------------------

    def create_conversation(
        self, actor: ConversationActor, retention: RetentionPolicy, title: str | None = None,
    ) -> Conversation:
        self._require_actor(actor)
        try:
            record = Conversation(id=new_id("conv"), actor=actor, retention=retention, title=title)
        except ConversationError as exc:
            raise ConversationStoreError(str(exc)) from exc
        self.rows.turns[record.id] = []
        return self._store_conversation("conversation.created", record)

    def get_conversation(self, actor: ConversationActor, conversation_id: str) -> Conversation:
        return self._reconciled(self._owned(actor, conversation_id))

    def _reconciled(self, record: Conversation) -> Conversation:
        """Complete a crashed pending deletion before serving any read."""
        if record.status != "deletion_pending":
            return record
        self._complete_deletion(record)
        refreshed = self.rows.conversations.get(record.id)
        if refreshed is None:
            raise UnknownConversationError("unknown conversation")
        return refreshed

    def list_conversations(self, actor: ConversationActor, include_archived: bool = False) -> list[Conversation]:
        self._require_actor(actor)
        wanted = _LISTABLE_STATUSES if include_archived else frozenset({"active"})
        values = [
            record for record in self.rows.conversations.values()
            if record.actor == actor and record.status in wanted
        ]
        return sorted(values, key=lambda record: record.updated_at, reverse=True)

    def search_conversations(
        self, actor: ConversationActor, query: str, include_archived: bool = False,
    ) -> list[Conversation]:
        """Title/metadata search only — turn content is never scanned here."""
        needle = query.strip().lower() if isinstance(query, str) else ""
        if not needle:
            raise ConversationStoreError("search query must be a non-empty string")
        return [
            record for record in self.list_conversations(actor, include_archived=include_archived)
            if record.title is not None and needle in record.title.lower()
        ]

    def _mutate_conversation(self, actor: ConversationActor, conversation_id: str, **changes: object) -> Conversation:
        record = self._owned(actor, conversation_id)
        if record.status not in _LISTABLE_STATUSES:
            raise ConversationStoreError(f"a {record.status} conversation cannot be modified")
        try:
            return replace(record, updated_at=now_utc(), **changes)  # type: ignore[arg-type]
        except ConversationError as exc:
            raise ConversationStoreError(str(exc)) from exc

    def rename_conversation(self, actor: ConversationActor, conversation_id: str, title: str) -> Conversation:
        updated = self._mutate_conversation(actor, conversation_id, title=title)
        return self._store_conversation("conversation.renamed", updated)

    def archive_conversation(self, actor: ConversationActor, conversation_id: str) -> Conversation:
        record = self._owned(actor, conversation_id)
        if record.status != "active":
            raise ConversationStoreError("only an active conversation can be archived")
        updated = self._mutate_conversation(actor, conversation_id, status="archived")
        return self._store_conversation("conversation.archived", updated)

    def unarchive_conversation(self, actor: ConversationActor, conversation_id: str) -> Conversation:
        record = self._owned(actor, conversation_id)
        if record.status != "archived":
            raise ConversationStoreError("only an archived conversation can be unarchived")
        updated = self._mutate_conversation(actor, conversation_id, status="active")
        return self._store_conversation("conversation.unarchived", updated)

    # -- turn appends (all routed through validate_turn_append) ------------

    def append_turn(
        self, actor: ConversationActor, conversation_id: str, *, role: str, status: str,
        lineage: TurnLineage, redaction: TurnRedaction, mode: str = "ordinary",
        content: tuple[ContentBlock, ...] | list[ContentBlock] = (),
        voice_events: tuple[VoiceEvent, ...] | list[VoiceEvent] = (), turn_id: str | None = None,
    ) -> Turn:
        conversation = self._owned(actor, conversation_id)
        try:
            turn = make_turn(
                id=turn_id if turn_id is not None else new_id("turn"),
                conversation_id=conversation_id,
                role=role,
                mode=mode,
                status=status,
                lineage=lineage,
                content=content,
                redaction=redaction,
                voice_events=voice_events,
                completed_at=None if status == "streaming" else now_utc(),
                content_hash_key=self._content_hash_key,
            )
            validated = validate_turn_append(conversation, self._actor_turn_universe(actor), turn)
        except ConversationError as exc:
            raise ConversationStoreError(str(exc)) from exc
        self._own_turns(conversation_id).append(validated)
        self.rows.conversations[conversation_id] = replace(conversation, updated_at=now_utc())
        self._append_audit("turn.appended", turn_audit(validated))
        return validated

    def _find_turn(self, conversation_id: str, turn_id: str) -> Turn:
        for turn in self.rows.turns.get(conversation_id, []):
            if turn.id == turn_id:
                return turn
        raise ConversationStoreError("unknown turn")

    def _next_sibling_index(self, conversation_id: str, parent_turn_id: str | None) -> int:
        taken = [
            turn.lineage.sibling_index
            for turn in self.rows.turns.get(conversation_id, [])
            if turn.lineage.parent_turn_id == parent_turn_id
        ]
        return (max(taken) + 1) if taken else 0

    def retry_turn(
        self, actor: ConversationActor, conversation_id: str, retry_of_turn_id: str, *, role: str,
        status: str, redaction: TurnRedaction, mode: str = "ordinary",
        content: tuple[ContentBlock, ...] | list[ContentBlock] = (),
        voice_events: tuple[VoiceEvent, ...] | list[VoiceEvent] = (),
    ) -> Turn:
        """Append a new sibling of the retried turn; committed history is untouched."""
        self._owned(actor, conversation_id)
        target = self._find_turn(conversation_id, retry_of_turn_id)
        parent_id = target.lineage.parent_turn_id
        if parent_id is None:
            raise ConversationStoreError("the root turn cannot be retried")
        try:
            lineage = TurnLineage(parent_id, self._next_sibling_index(conversation_id, parent_id), "retry")
        except ConversationError as exc:
            raise ConversationStoreError(str(exc)) from exc
        return self.append_turn(
            actor, conversation_id, role=role, status=status, lineage=lineage,
            redaction=redaction, mode=mode, content=content, voice_events=voice_events,
        )

    def branch_turn(
        self, actor: ConversationActor, conversation_id: str, parent_turn_id: str, *, role: str,
        status: str, redaction: TurnRedaction, mode: str = "ordinary",
        content: tuple[ContentBlock, ...] | list[ContentBlock] = (),
        voice_events: tuple[VoiceEvent, ...] | list[VoiceEvent] = (),
    ) -> Turn:
        """Append a new child branch under an existing turn of this conversation."""
        self._owned(actor, conversation_id)
        self._find_turn(conversation_id, parent_turn_id)
        try:
            lineage = TurnLineage(parent_turn_id, self._next_sibling_index(conversation_id, parent_turn_id), "branch")
        except ConversationError as exc:
            raise ConversationStoreError(str(exc)) from exc
        return self.append_turn(
            actor, conversation_id, role=role, status=status, lineage=lineage,
            redaction=redaction, mode=mode, content=content, voice_events=voice_events,
        )

    # -- status advance, reads, recovery ----------------------------------

    def advance_turn_status(
        self, actor: ConversationActor, conversation_id: str, turn_id: str, status: str,
    ) -> Turn:
        """Advance one streaming turn to exactly one terminal state, in place.

        Per the turn contract only ``status``/``completed_at`` may advance;
        a terminal turn is committed history and can never change again.
        """
        record = self._owned(actor, conversation_id)
        if record.status not in _LISTABLE_STATUSES:
            raise ConversationStoreError(
                f"a {record.status} conversation's history is final and cannot advance"
            )
        target = self._find_turn(conversation_id, turn_id)
        if target.content_purged:
            raise ConversationStoreError("a purged tombstone turn cannot advance status")
        if status not in TERMINAL_TURN_STATUSES:
            raise ConversationStoreError(f"turn status can only advance to a terminal state, not {status!r}")
        if target.status != "streaming":
            raise ConversationStoreError("committed history is immutable; a terminal turn cannot change status")
        try:
            advanced = replace(target, status=status, completed_at=now_utc())
        except ConversationError as exc:
            raise ConversationStoreError(str(exc)) from exc
        self._replace_turn(conversation_id, advanced)
        self._append_audit("turn.status_advanced", turn_audit(advanced))
        return advanced

    def _replace_turn(self, conversation_id: str, advanced: Turn) -> None:
        turns = self._own_turns(conversation_id)
        for index, existing in enumerate(turns):
            if existing.id == advanced.id:
                turns[index] = advanced
                return
        raise ConversationStoreError("unknown turn")

    def get_conversation_with_turns(
        self, actor: ConversationActor, conversation_id: str,
    ) -> tuple[Conversation, tuple[Turn, ...]]:
        """Return the conversation and every persisted turn in lineage order.

        Every live (non-purged) turn's keyed fingerprint is re-verified against
        its stored content, so a content rewrite behind the store's back can
        never hide behind a stale hash; a purged tombstone deliberately keeps
        the fingerprint of the content that was removed.
        """
        record = self._reconciled(self._owned(actor, conversation_id))
        ordered = _lineage_order(list(self.rows.turns.get(conversation_id, [])))
        for item in ordered:
            if item.content_purged:
                continue
            try:
                item.verify_content_hash(self._content_hash_key)
            except ConversationError as exc:
                raise ConversationStoreError(str(exc)) from exc
        return record, ordered

    # -- retention enforcement and deletion --------------------------------

    def delete_conversation(self, actor: ConversationActor, conversation_id: str, mode: str) -> ConversationAudit:
        """Delete the owned conversation under one of the contract's two modes.

        ``purge_content_keep_tombstone`` removes every turn's content blocks
        and the title from the rows, leaving the identity row (status
        ``deleted`` with its typed deletion record) plus tombstone turns that
        keep only lifecycle, lineage, voice events, and the keyed content
        fingerprint.  ``purge_all_records`` removes the conversation and its
        turns entirely; only the content-free audit trail survives.  The purge
        completes synchronously before return; the transient
        ``deletion_pending`` state is persisted (and audited) first, so a
        crash between the two is reconciled by :meth:`enforce_retention`,
        never silently lost.  Returns the content-free final audit projection.
        """
        record = self._owned(actor, conversation_id)
        if record.status == "deletion_pending":
            # A crash between the persisted pending state and the purge is
            # completed here (using the already-persisted mode), never refused
            # into a fail-open dead end.
            return self._complete_deletion(record)
        if record.status not in _LISTABLE_STATUSES:
            raise ConversationStoreError(f"a {record.status} conversation cannot be deleted again")
        requested_at = now_utc()
        try:
            pending = replace(
                record,
                status="deletion_pending",
                deletion=ConversationDeletion(requested_at, mode),
                updated_at=requested_at,
            )
        except ConversationError as exc:
            raise ConversationStoreError(str(exc)) from exc
        self._store_conversation("conversation.deletion_requested", pending)
        return self._complete_deletion(pending)

    def _complete_deletion(self, pending: Conversation) -> ConversationAudit:
        """Purge content for a ``deletion_pending`` conversation, fail-closed."""
        deletion = pending.deletion
        if deletion is None:  # pragma: no cover - the model invariant forbids this
            raise ConversationStoreError("a pending deletion requires its deletion record")
        completed_at = now_utc()
        turns = self.rows.turns.get(pending.id, [])
        for index, item in enumerate(turns):
            if item.content_purged:
                continue
            if item.status == "streaming":
                # A cut-off response is finalized as interrupted, never left
                # advanceable to a fabricated completion on deleted history.
                item = replace(item, status="interrupted", completed_at=completed_at)
            tombstone = purge_turn_content(item)
            turns[index] = tombstone
            if deletion.mode == "purge_content_keep_tombstone":
                self._append_audit("turn.content_purged", turn_audit(tombstone))
        try:
            deleted = replace(
                pending,
                status="deleted",
                title=None,
                deletion=ConversationDeletion(deletion.requested_at, deletion.mode, completed_at),
                updated_at=completed_at,
            )
        except ConversationError as exc:
            raise ConversationStoreError(str(exc)) from exc
        if deletion.mode == "purge_all_records":
            final = conversation_audit(deleted, turns)
            self._append_audit("conversation.deleted", final)
            self.rows.conversations.pop(pending.id, None)
            self.rows.turns.pop(pending.id, None)
            return final
        self._store_conversation("conversation.deleted", deleted)
        return conversation_audit(deleted, self.rows.turns.get(deleted.id, []))

    def enforce_retention(self, now: datetime | None = None) -> tuple[ConversationAudit, ...]:
        """HUB-INTERNAL sweep applying retention ceilings and finishing deletions.

        Exactly what ``chat-conversation.v1`` declares, nothing more: the
        per-conversation ``retention.delete_after`` instant (the contract's
        only age ceiling) and the deletion lifecycle (a ``deletion_pending``
        record left behind by a crash mid-purge is completed here).  An
        expired conversation is purged as ``purge_content_keep_tombstone`` so
        the content-free lifecycle/fingerprint audit facts survive; later
        reads return only tombstoned shapes.  Idempotent: an already-deleted
        tombstone is never re-enforced.  Returns the content-free audit
        projections of the conversations enforced in this pass.
        """
        moment = now if now is not None else now_utc()
        if not isinstance(moment, datetime) or moment.tzinfo is None:
            raise ConversationStoreError("retention enforcement requires a timezone-aware datetime")
        enforced: list[ConversationAudit] = []
        for record in list(self.rows.conversations.values()):
            if record.status == "deletion_pending":
                enforced.append(self._complete_deletion(record))
            elif (
                record.status in _LISTABLE_STATUSES
                and record.retention.delete_after is not None
                and record.retention.delete_after <= moment
            ):
                expired = replace(
                    record,
                    status="deletion_pending",
                    deletion=ConversationDeletion(moment, "purge_content_keep_tombstone"),
                    updated_at=moment,
                )
                self._store_conversation("conversation.retention_expired", expired)
                enforced.append(self._complete_deletion(expired))
        return tuple(enforced)

    def recover_streaming_turns(self) -> tuple[TurnAudit, ...]:
        """Post-restart recovery: flip every persisted streaming turn to interrupted.

        A turn found in ``streaming`` status after a reload was cut off before
        its terminal state was recorded; it is surfaced as ``interrupted``
        with ``completed_at`` set — never silently completed.  Returns the
        content-free audit projections of the recovered turns.
        """
        recovered: list[TurnAudit] = []
        for conversation_id, turns in self.rows.turns.items():
            for index, turn in enumerate(turns):
                if turn.status != "streaming":
                    continue
                interrupted = replace(turn, status="interrupted", completed_at=now_utc())
                turns[index] = interrupted
                audit = turn_audit(interrupted)
                recovered.append(audit)
                self._append_audit("turn.recovered_interrupted", audit)
        return tuple(recovered)

    def list_audit(self, limit: int = 20) -> list[ConversationAuditEvent]:
        return list(reversed(self.rows.audit[-max(1, min(limit, 100)):]))
