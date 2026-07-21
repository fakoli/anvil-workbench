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

import threading
from functools import wraps

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
    RetentionPreview,
    TERMINAL_TURN_STATUSES,
    Turn,
    TurnAudit,
    TurnLineage,
    TurnRedaction,
    VoiceEvent,
    conversation_audit,
    ephemeral_retention_policy,
    make_turn,
    purge_turn_content,
    require_content_hash_key,
    retention_preview_of,
    turn_audit,
    validate_turn_append,
)
from .conversation_models import is_metadata_only
from .models import OperationRef, new_id, now_utc
from .store import MemoryOperationReceiptStore, OperationOutcome, StoreError

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
    def create_ephemeral_conversation(self, actor: ConversationActor) -> Conversation: ...
    def get_conversation(self, actor: ConversationActor, conversation_id: str) -> Conversation: ...
    def list_conversations(
        self, actor: ConversationActor, include_archived: bool = False, *,
        pinned: bool | None = None, tag: str | None = None, folder: str | None = None,
    ) -> list[Conversation]: ...
    def search_conversations(
        self, actor: ConversationActor, query: str, include_archived: bool = False, *,
        pinned: bool | None = None, tag: str | None = None, folder: str | None = None,
    ) -> list[Conversation]: ...
    def rename_conversation(self, actor: ConversationActor, conversation_id: str, title: str) -> Conversation: ...
    def archive_conversation(self, actor: ConversationActor, conversation_id: str) -> Conversation: ...
    def unarchive_conversation(self, actor: ConversationActor, conversation_id: str) -> Conversation: ...
    def pin_conversation(self, actor: ConversationActor, conversation_id: str) -> Conversation: ...
    def unpin_conversation(self, actor: ConversationActor, conversation_id: str) -> Conversation: ...
    def add_conversation_tag(self, actor: ConversationActor, conversation_id: str, tag: str) -> Conversation: ...
    def remove_conversation_tag(self, actor: ConversationActor, conversation_id: str, tag: str) -> Conversation: ...
    def set_conversation_folder(self, actor: ConversationActor, conversation_id: str, folder: str) -> Conversation: ...
    def clear_conversation_folder(self, actor: ConversationActor, conversation_id: str) -> Conversation: ...
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
    def retention_preview(self, now: datetime | None = None) -> tuple[RetentionPreview, ...]: ...
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
        # Single-writer serialization for the in-memory backend: every public
        # method runs under this reentrant lock so concurrent threadpool
        # requests cannot interleave a mutation (the Postgres backend will use
        # row-level transactions instead).
        self._lock = threading.RLock()
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

    def create_ephemeral_conversation(self, actor: ConversationActor) -> Conversation:
        """Create a one-action ephemeral chat: ``metadata_only`` for BOTH content kinds.

        The single call persists a conversation under
        :func:`~workbench.conversation_models.ephemeral_retention_policy`, so no
        transcript content block can ever persist for either the text or the
        voice content kind (the append gate enforces the retention mapping).
        The record's ``is_metadata_only`` badge therefore truthfully reflects
        the durable policy rather than an asserted label.
        """
        return self.create_conversation(actor, ephemeral_retention_policy(), title=None)

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

    def list_conversations(
        self,
        actor: ConversationActor,
        include_archived: bool = False,
        *,
        pinned: bool | None = None,
        tag: str | None = None,
        folder: str | None = None,
    ) -> list[Conversation]:
        """List the acting actor's conversations, optionally filtered by organization.

        The result is always scoped to ``actor`` first, so every organization
        filter (``pinned``/``tag``/``folder``) narrows within the actor's own
        rows only — a filter naming another actor's tag or folder simply matches
        nothing here, and can never surface a foreign conversation or act as an
        existence oracle.  Pinned conversations sort ahead of the rest; within
        each group the newest-updated comes first.
        """
        self._require_actor(actor)
        wanted = _LISTABLE_STATUSES if include_archived else frozenset({"active"})
        values = [
            record for record in self.rows.conversations.values()
            if record.actor == actor
            and record.status in wanted
            and (pinned is None or record.pinned == pinned)
            and (tag is None or tag in record.tags)
            and (folder is None or record.folder == folder)
        ]
        values.sort(key=lambda record: record.updated_at, reverse=True)
        values.sort(key=lambda record: not record.pinned)  # stable: pinned first
        return values

    def search_conversations(
        self,
        actor: ConversationActor,
        query: str,
        include_archived: bool = False,
        *,
        pinned: bool | None = None,
        tag: str | None = None,
        folder: str | None = None,
    ) -> list[Conversation]:
        """Title search plus the same actor-scoped organization filters.

        Turn content is never scanned here; the query matches the title only,
        and the ``pinned``/``tag``/``folder`` filters are applied on top through
        :meth:`list_conversations`, so the search stays scoped to the acting
        actor exactly like the plain list.
        """
        needle = query.strip().lower() if isinstance(query, str) else ""
        if not needle:
            raise ConversationStoreError("search query must be a non-empty string")
        return [
            record for record in self.list_conversations(
                actor, include_archived=include_archived, pinned=pinned, tag=tag, folder=folder,
            )
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

    # -- organization metadata (pin / tags / folder) ----------------------
    #
    # Each mutation is actor-scoped (routed through ``_owned``/``_mutate_conversation``,
    # so a cross-actor probe is the same ``unknown conversation`` a missing id
    # raises), fails closed on an unsafe label (the ``Conversation`` model
    # rejects it), and emits a content-free ``ConversationAudit`` under a
    # distinct lifecycle ``kind``.  None of these values ever reaches turn
    # content or a Serving request.

    def pin_conversation(self, actor: ConversationActor, conversation_id: str) -> Conversation:
        updated = self._mutate_conversation(actor, conversation_id, pinned=True)
        return self._store_conversation("conversation.pinned", updated)

    def unpin_conversation(self, actor: ConversationActor, conversation_id: str) -> Conversation:
        updated = self._mutate_conversation(actor, conversation_id, pinned=False)
        return self._store_conversation("conversation.unpinned", updated)

    def add_conversation_tag(self, actor: ConversationActor, conversation_id: str, tag: str) -> Conversation:
        """Add one safe tag; unsafe or over-count tags fail closed at the model."""
        record = self._owned(actor, conversation_id)
        new_tags = tuple(record.tags) + (tag,)
        updated = self._mutate_conversation(actor, conversation_id, tags=new_tags)
        return self._store_conversation("conversation.tagged", updated)

    def remove_conversation_tag(self, actor: ConversationActor, conversation_id: str, tag: str) -> Conversation:
        """Remove one tag if present (idempotent); the actor keeps the rest."""
        record = self._owned(actor, conversation_id)
        new_tags = tuple(existing for existing in record.tags if existing != tag)
        updated = self._mutate_conversation(actor, conversation_id, tags=new_tags)
        return self._store_conversation("conversation.untagged", updated)

    def set_conversation_folder(self, actor: ConversationActor, conversation_id: str, folder: str) -> Conversation:
        """Move the conversation into one safe folder label; unsafe labels fail closed."""
        updated = self._mutate_conversation(actor, conversation_id, folder=folder)
        return self._store_conversation("conversation.foldered", updated)

    def clear_conversation_folder(self, actor: ConversationActor, conversation_id: str) -> Conversation:
        updated = self._mutate_conversation(actor, conversation_id, folder=None)
        return self._store_conversation("conversation.unfoldered", updated)

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
                pinned=False,
                tags=(),
                folder=None,
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

    def retention_preview(self, now: datetime | None = None) -> tuple[RetentionPreview, ...]:
        """OFF-READ-PATH, side-effect-free preview of what a batched pass WOULD enforce.

        Returns the content-free :class:`RetentionPreview` (conversation id,
        counts, and the created/updated/delete_after timestamps only — never a
        title or message content) for every conversation
        :meth:`enforce_retention` would act on at ``now``: a live conversation
        past its ``retention.delete_after`` ceiling, or a crashed
        ``deletion_pending`` record awaiting completion.  This method purges,
        deletes, and mutates NOTHING — it is pure inspection, so it can never
        become a read that initiates retention-expiry deletion.  Fails closed
        on a naive ``now`` exactly like the enforcement pass.
        """
        moment = now if now is not None else now_utc()
        if not isinstance(moment, datetime) or moment.tzinfo is None:
            raise ConversationStoreError("retention preview requires a timezone-aware datetime")
        previews: list[RetentionPreview] = []
        for record in self.rows.conversations.values():
            if record.status == "deletion_pending":
                reason = "deletion_pending"
            elif (
                record.status in _LISTABLE_STATUSES
                and record.retention.delete_after is not None
                and record.retention.delete_after <= moment
            ):
                reason = "retention_expired"
            else:
                continue
            previews.append(retention_preview_of(record, self.rows.turns.get(record.id, []), reason))
        return tuple(previews)

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


def _synchronize_memory_store() -> None:
    """Wrap every public MemoryConversationStore method under its instance lock."""

    def _guard(method):
        @wraps(method)
        def _locked(self, *args, **kwargs):
            with self._lock:
                return method(self, *args, **kwargs)
        return _locked

    for _name in (
        "create_conversation",
        "create_ephemeral_conversation",
        "get_conversation",
        "list_conversations",
        "search_conversations",
        "rename_conversation",
        "archive_conversation",
        "unarchive_conversation",
        "pin_conversation",
        "unpin_conversation",
        "add_conversation_tag",
        "remove_conversation_tag",
        "set_conversation_folder",
        "clear_conversation_folder",
        "append_turn",
        "retry_turn",
        "branch_turn",
        "advance_turn_status",
        "get_conversation_with_turns",
        "delete_conversation",
        "retention_preview",
        "enforce_retention",
        "recover_streaming_turns",
        "list_audit",
    ):
        setattr(MemoryConversationStore, _name, _guard(getattr(MemoryConversationStore, _name)))


_synchronize_memory_store()


# --------------------------------------------------------------------------- #
# First-party read-only conversation-search tool (reviewed-tools-plugins T010).
# --------------------------------------------------------------------------- #

import hashlib as _cs_hashlib

from .advanced_tools import delimit_untrusted_output

#: The reviewed first-party (hub-owned, NOT a plugin) search tool identity.  It
#: is a fixed, reviewed operation reference so its typed receipt names a stable
#: descriptor; the digest is a domain-separated constant, not a live input.
CONVERSATION_SEARCH_PROVIDER = "workbench-conversation-search"
CONVERSATION_SEARCH_TOOL_ID = "conversations.search"
CONVERSATION_SEARCH_CONTRACT_VERSION = "1.0.0"
_CONVERSATION_SEARCH_DIGEST = "sha256:" + _cs_hashlib.sha256(
    b"anvil-workbench/first-party/conversations.search/v1"
).hexdigest()
_CONVERSATION_SEARCH_OPERATION = OperationRef(
    provider=CONVERSATION_SEARCH_PROVIDER,
    id=CONVERSATION_SEARCH_TOOL_ID,
    contract_version=CONVERSATION_SEARCH_CONTRACT_VERSION,
    operation_digest=_CONVERSATION_SEARCH_DIGEST,
)

_SEARCH_RESULT_LIMIT = 50
_SEARCH_QUERY_MAX = 200


@dataclass(frozen=True)
class ConversationSearchResult:
    """One matched conversation projected as SAFE, metadata-only display data.

    Carries only owner-set organization metadata (id, title, pin/tag/folder,
    status, and the metadata-only badge) and the update timestamp.  It never
    carries a turn's transcript content: the underlying search is title-only and
    never scans :class:`~workbench.conversation_models.Turn` content, so
    metadata-only, purged, or deleted transcript content cannot appear here.
    """

    conversation_id: str
    title: str
    pinned: bool
    tags: tuple[str, ...]
    folder: str | None
    status: str
    metadata_only: bool
    updated_at: datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "conversation_id": self.conversation_id,
            "title": self.title,
            "pinned": self.pinned,
            "tags": list(self.tags),
            "folder": self.folder,
            "status": self.status,
            "metadata_only": self.metadata_only,
            "updated_at": self.updated_at.isoformat(),
        }


class ConversationSearchService:
    """First-party read-only "search my conversations" tool (T010).

    Scoped to the requesting conversation's actor BY CONSTRUCTION: it delegates
    to the store's actor-scoped title search, so a foreign actor's conversations
    are never in the universe searched.  A cross-actor probe and a nonexistent
    query therefore return the BYTE-IDENTICAL empty result envelope -- there is
    no 404-vs-empty distinction and no shape/existence signal by which a caller
    could learn that a matching conversation exists for someone else.

    Results are DELIMITED UNTRUSTED DATA: every match is projected to safe
    metadata (never turn content), the whole result list is wrapped through
    :func:`~workbench.advanced_tools.delimit_untrusted_output` (JSON-stringified,
    ``content_trust=untrusted_task_data``), so a conversation title carrying an
    injection or a fake capability profile is inert text incapable of selecting a
    tool or widening a profile.  Every search records a TYPED read receipt through
    the reused :class:`~workbench.store.MemoryOperationReceiptStore` spine, so the
    read is an idempotent, redacted, typed operation -- never a profile mutation.

    Metadata-only, purged, and deleted content never appears: the delegated
    search is title-only (it never reads a purged content block), and deleted /
    deletion-pending conversations are excluded from the searchable set by the
    store ``_LISTABLE_STATUSES`` filter.
    """

    def __init__(
        self,
        conversation_store: "ConversationStore",
        *,
        receipt_store: MemoryOperationReceiptStore | None = None,
    ) -> None:
        self._store = conversation_store
        self._receipts = receipt_store if receipt_store is not None else MemoryOperationReceiptStore()

    @property
    def receipts(self) -> MemoryOperationReceiptStore:
        return self._receipts

    def _project(self, conversation: Conversation) -> ConversationSearchResult:
        return ConversationSearchResult(
            conversation_id=conversation.id,
            # A matched title is only reached when it is non-None (the store title
            # search filters None titles), so the empty-string fallback is
            # defensive only.
            title=conversation.title or "",
            pinned=bool(conversation.pinned),
            tags=tuple(conversation.tags),
            folder=conversation.folder,
            status=conversation.status,
            metadata_only=is_metadata_only(conversation.retention),
            updated_at=conversation.updated_at,
        )

    def _idempotency_key(self, actor: ConversationActor, query: str) -> str:
        # Bound to the actor AND the exact query so an identical repeat replays the
        # same typed receipt; domain-separated + hashed so neither the actor id nor
        # the raw query text is echoed into the receipt key.
        material = f"{actor.actor_id}\x00{query}".encode("utf-8")
        return "cs:" + _cs_hashlib.sha256(b"anvil-workbench/conversation-search/v1\x00" + material).hexdigest()

    def search(
        self, actor: ConversationActor, query: str, *, request_id: str | None = None,
    ) -> dict[str, object]:
        """Return delimited untrusted search results + a typed read receipt.

        The returned envelope existence-signal surface -- ``content_trust``,
        ``delimited``, ``payload_json``, and ``result_count`` -- is byte-identical
        for a query that would match only a foreign actor conversation and a
        query that matches nothing at all, so the tool is never a cross-actor
        existence oracle.
        """
        if not isinstance(actor, ConversationActor):
            raise ConversationStoreError("a conversation search requires the acting ConversationActor")
        if not isinstance(query, str) or not query.strip():
            raise ConversationStoreError("search query must be a non-empty string")
        if len(query) > _SEARCH_QUERY_MAX:
            raise ConversationStoreError("search query is too long")
        # Actor-scoped BY CONSTRUCTION: the store search only ever ranges over
        # conversations owned by this actor.  include_archived surfaces the
        # actor own archived rows; deleted/deletion-pending rows are excluded by
        # the store listable-status filter, so purged/deleted content is
        # unreachable here.
        matches = self._store.search_conversations(actor, query, include_archived=True)
        results = [self._project(record).as_dict() for record in matches[:_SEARCH_RESULT_LIMIT]]
        envelope = delimit_untrusted_output(results)
        envelope["result_count"] = len(results)
        # A typed, idempotent, redacted read receipt through the reused spine: the
        # executor returns a plain ``succeeded`` outcome with NO external_ref, so
        # the receipt carries no match detail and cannot leak a foreign existence
        # signal, and -- being an operation receipt -- it cannot alter a profile.
        key = self._idempotency_key(actor, query)
        receipt, _replayed = self._receipts.record_attempt(
            run_id=f"conversation_search_{actor.actor_id}"[:120],
            command_id=key,
            operation=_CONVERSATION_SEARCH_OPERATION,
            idempotency_key=key,
            executor=lambda: OperationOutcome("succeeded"),
            request_id=request_id,
        )
        envelope["receipt"] = receipt
        return envelope
