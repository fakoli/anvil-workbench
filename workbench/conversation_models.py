"""Durable chat conversation and turn domain models with retention and lineage.

These frozen values mirror the proposed ``chat-conversation.v1`` and
``chat-turn.v1`` contract resources (``docs/contracts/schemas``) for the hub
store: one mode-agnostic conversation identity owned by an operator actor, and
append-only turns with typed ``(parent_turn_id, sibling_index)`` lineage.  A
retry or branch is a new turn, never a rewrite; after creation a turn record is
immutable.

Two rule families the JSON Schemas cannot express live here instead:

* The cross-record lineage invariants from ``docs/contracts/README.md``
  convention 11 — exactly one null-parent root per conversation,
  ``(parent_turn_id, sibling_index)`` uniqueness, parent existence in the same
  conversation, and acyclicity — enforced fail-closed by
  :func:`validate_turn_append` before the hub store persists an append.
* The retention mapping — a conversation whose ``transcript_text`` or
  ``voice_transcript_text`` policy is ``metadata_only`` may never persist a
  ``kind: "transcript"`` content block for that content kind.  Voice-input
  provenance is inferred from the voice-INPUT events attached to the turn
  (``VOICE_INPUT_EVENT_KINDS``); voice-output events never relax the text
  policy.  PRECONDITION for the hub store: persist a voice transcript in the
  same append as its voice events — a caller that strips the events routes
  the transcript to the ``transcript_text`` policy.

There is deliberately no field able to carry raw audio frames or
hidden/encrypted model reasoning, on the turn records or on the safe audit
shapes.  The audit models expose lifecycle, lineage, hash, and bounded count
metadata only — never message content.

The content fingerprint is a keyed HMAC-SHA256 (chat-first-voice PRD R008):
the hub holds the key and injects it at store construction; it is never
persisted next to the hashes, so persisted metadata is not a content-equality
or dictionary oracle for parties without the key.  Retention/deletion
tombstoning (:func:`purge_turn_content`) removes content blocks from the row
outright, keeping only lifecycle, lineage, typed voice events, and that
fingerprint; a purged record refuses content at construction, so the purge is
one-way.

Deliberate scope omissions versus ``chat-turn.v1``: the ``route`` (with its
assistant-requires-route/request_id conditional), ``usage``,
``advanced_controls``, and context blocks are Serving-integration concerns
owned by the streaming/route tasks; these models neither carry nor enforce
them, so an assistant ``Turn`` here is a subset of the schema shape until
that slice lands.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field, replace
from datetime import datetime

from .contracts import canonical_json_bytes
from .models import now_utc


class ConversationError(ValueError):
    """A conversation or turn record would violate the chat contract, fail closed."""


CONVERSATION_STATUSES = frozenset({"active", "archived", "deletion_pending", "deleted"})
_DELETION_REQUIRED_STATUSES = frozenset({"deletion_pending", "deleted"})
DELETION_MODES = frozenset({"purge_content_keep_tombstone", "purge_all_records"})
ACTOR_KINDS = frozenset({"operator"})
RETENTION_VALUES = frozenset({"retained_redacted", "metadata_only"})
#: Why a conversation appears in an off-read-path retention preview/pass.
RETENTION_PREVIEW_REASONS = frozenset({"retention_expired", "deletion_pending"})
#: The policy id an ephemeral (metadata_only) conversation is created under.
EPHEMERAL_RETENTION_POLICY_ID = "workbench.ephemeral"

TURN_ROLES = frozenset({"user", "assistant"})
TURN_MODES = frozenset({"ordinary", "advanced"})
TURN_STATUSES = frozenset({"streaming", "complete", "interrupted", "cancelled", "failed"})
TERMINAL_TURN_STATUSES = frozenset({"complete", "interrupted", "cancelled", "failed"})
LINEAGE_KINDS = frozenset({"initial", "retry", "branch"})
CONTENT_KINDS = frozenset({"text", "transcript", "summary"})
CONTENT_TRUST = "untrusted_task_data"
REDACTION_STATUSES = frozenset({"redacted", "metadata_only"})
VOICE_EVENT_KINDS = frozenset({"utterance_start", "stt_commit", "tts_start", "tts_stop", "interruption"})
#: Voice-INPUT events: only these make a turn voice-governed for retention.
#: Voice-output (tts_*) or interruption events never relax the text policy.
VOICE_INPUT_EVENT_KINDS = frozenset({"utterance_start", "stt_commit"})

MAX_CONTENT_BLOCKS = 64
MAX_CONTENT_TEXT_CHARS = 20000
MAX_VOICE_EVENTS = 64
MAX_SIBLING_INDEX = 4096
MAX_TITLE_CHARS = 200

_CONVERSATION_ID = re.compile(r"^conv_[a-zA-Z0-9_-]{8,128}$")
_TURN_ID = re.compile(r"^turn_[a-zA-Z0-9_-]{8,128}$")
_ACTOR_ID = re.compile(r"^[a-zA-Z0-9._-]{1,128}$")
_POLICY_TOKEN = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_REDACTION_RULESET = re.compile(r"^[a-z][a-z0-9._-]{1,127}$")

# Domain separation so a chat content fingerprint can never collide with a
# contract digest computed over the same bytes (see docs/contracts/DIGESTING.md
# idiom).  The fingerprint is a KEYED HMAC-SHA256 (chat-first-voice PRD R008):
# chat prompts are low-entropy and guessable, so an unkeyed digest persisted in
# metadata/audit rows would be a content-equality/dictionary oracle.  The hub
# holds the key, injects it at store construction, and never persists it next
# to the hashes.
_CHAT_CONTENT_PREFIX = b"anvil-workbench/chat-turn-content/v1\0"
MIN_CONTENT_HASH_KEY_BYTES = 16
_CONTENT_HASH = re.compile(r"^hmac-sha256:[a-f0-9]{64}$")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConversationError(message)


@dataclass(frozen=True)
class ConversationActor:
    """The owning operator identity for one conversation."""

    actor_id: str
    kind: str = "operator"

    def __post_init__(self) -> None:
        _require(isinstance(self.actor_id, str) and bool(_ACTOR_ID.match(self.actor_id)), "conversation actor id is invalid")
        _require(self.kind in ACTOR_KINDS, f"conversation actor kind is not allowlisted: {self.kind!r}")


@dataclass(frozen=True)
class RetentionPolicy:
    """The per-conversation retention declaration for redacted text only.

    Raw audio and hidden reasoning are never retained under any policy value;
    no field here (or anywhere in this module) can name them.
    """

    policy_id: str
    transcript_text: str
    voice_transcript_text: str
    delete_after: datetime | None = None

    def __post_init__(self) -> None:
        _require(isinstance(self.policy_id, str) and bool(_POLICY_TOKEN.match(self.policy_id)), "retention policy id is invalid")
        _require(self.transcript_text in RETENTION_VALUES, f"retention transcript_text is not allowlisted: {self.transcript_text!r}")
        _require(
            self.voice_transcript_text in RETENTION_VALUES,
            f"retention voice_transcript_text is not allowlisted: {self.voice_transcript_text!r}",
        )
        # ``delete_after`` is the contract's only age ceiling (chat-conversation.v1
        # retention block); it must be a timezone-aware instant so an enforcement
        # sweep can compare it fail-closed against an aware "now".
        if self.delete_after is not None:
            _require(
                isinstance(self.delete_after, datetime) and self.delete_after.tzinfo is not None,
                "retention delete_after must be a timezone-aware datetime",
            )


def is_metadata_only(retention: RetentionPolicy) -> bool:
    """True only when BOTH content kinds are ``metadata_only``.

    This is the truthful "ephemeral" predicate a badge reads: a conversation
    whose ``transcript_text`` and ``voice_transcript_text`` are both
    ``metadata_only`` can never persist a transcript content block for either
    kind (:func:`validate_turn_append` enforces the mapping), so the badge is a
    fact about the durable policy, not a label the caller asserts.
    """
    return (
        isinstance(retention, RetentionPolicy)
        and retention.transcript_text == "metadata_only"
        and retention.voice_transcript_text == "metadata_only"
    )


def ephemeral_retention_policy() -> RetentionPolicy:
    """The metadata_only retention policy for a one-action ephemeral chat.

    Both content kinds are ``metadata_only``, so the resulting conversation is
    ephemeral by construction and :func:`is_metadata_only` reports it truthfully.
    """
    return RetentionPolicy(EPHEMERAL_RETENTION_POLICY_ID, "metadata_only", "metadata_only")


@dataclass(frozen=True)
class ConversationDeletion:
    """A typed deletion record; surviving lineage keeps its original ids."""

    requested_at: datetime
    mode: str
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        _require(self.mode in DELETION_MODES, f"deletion mode is not allowlisted: {self.mode!r}")
        _require(isinstance(self.requested_at, datetime), "deletion requested_at must be a datetime")
        if self.completed_at is not None:
            _require(isinstance(self.completed_at, datetime), "deletion completed_at must be a datetime")


@dataclass(frozen=True)
class Conversation:
    """One durable, mode-agnostic conversation identity owned by an actor."""

    id: str
    actor: ConversationActor
    retention: RetentionPolicy
    status: str = "active"
    title: str | None = None
    deletion: ConversationDeletion | None = None
    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)

    def __post_init__(self) -> None:
        _require(isinstance(self.id, str) and bool(_CONVERSATION_ID.match(self.id)), "conversation id is invalid")
        _require(isinstance(self.actor, ConversationActor), "conversation actor must be a ConversationActor")
        _require(isinstance(self.retention, RetentionPolicy), "conversation retention must be a RetentionPolicy")
        _require(self.status in CONVERSATION_STATUSES, f"conversation status is not allowlisted: {self.status!r}")
        if self.title is not None:
            _require(isinstance(self.title, str) and len(self.title) <= MAX_TITLE_CHARS, "conversation title is invalid")
        if self.status in _DELETION_REQUIRED_STATUSES:
            _require(isinstance(self.deletion, ConversationDeletion), f"conversation status {self.status!r} requires a deletion record")
        else:
            _require(self.deletion is None, f"conversation status {self.status!r} must not carry a deletion record")
        # A ``deleted`` row is the post-purge tombstone: identity, lifecycle,
        # and counts only.  The title is untrusted prose content and must not
        # survive the purge.
        if self.status == "deleted":
            _require(self.title is None, "a deleted conversation tombstone must not retain a title")


@dataclass(frozen=True)
class ContentBlock:
    """One visible, redacted content block; no reasoning/audio/attachment kind exists."""

    kind: str
    text: str
    content_trust: str = CONTENT_TRUST

    def __post_init__(self) -> None:
        _require(self.kind in CONTENT_KINDS, f"content kind is not allowlisted: {self.kind!r}")
        _require(isinstance(self.text, str) and len(self.text) <= MAX_CONTENT_TEXT_CHARS, "content text is invalid")
        _require(self.content_trust == CONTENT_TRUST, "content blocks are always untrusted task data")


@dataclass(frozen=True)
class TurnLineage:
    """Typed branch/retry lineage within the owning conversation's turn DAG."""

    parent_turn_id: str | None
    sibling_index: int
    kind: str = "initial"

    def __post_init__(self) -> None:
        if self.parent_turn_id is not None:
            _require(
                isinstance(self.parent_turn_id, str) and bool(_TURN_ID.match(self.parent_turn_id)),
                "lineage parent turn id is invalid",
            )
        _require(
            isinstance(self.sibling_index, int) and not isinstance(self.sibling_index, bool)
            and 0 <= self.sibling_index <= MAX_SIBLING_INDEX,
            "lineage sibling index is out of bounds",
        )
        _require(self.kind in LINEAGE_KINDS, f"lineage kind is not allowlisted: {self.kind!r}")
        # A retry or branch replays under an existing parent.  A null-parent
        # retry/branch would necessarily be a second root, which the append
        # invariant refuses anyway; refuse the shape at construction.
        if self.kind != "initial":
            _require(self.parent_turn_id is not None, f"a {self.kind} turn must name its parent turn")


@dataclass(frozen=True)
class VoiceEvent:
    """One typed voice lifecycle event; no field can carry an audio payload."""

    event: str
    at: datetime
    duration_ms: int | None = None
    transcript_chars: int | None = None

    def __post_init__(self) -> None:
        _require(self.event in VOICE_EVENT_KINDS, f"voice event is not allowlisted: {self.event!r}")
        if self.duration_ms is not None:
            _require(isinstance(self.duration_ms, int) and 0 <= self.duration_ms <= 3_600_000, "voice event duration is out of bounds")
        if self.transcript_chars is not None:
            _require(
                isinstance(self.transcript_chars, int) and 0 <= self.transcript_chars <= 100_000,
                "voice event transcript_chars is out of bounds",
            )


@dataclass(frozen=True)
class TurnRedaction:
    """The redaction posture this turn was persisted under."""

    status: str
    ruleset: str

    def __post_init__(self) -> None:
        _require(self.status in REDACTION_STATUSES, f"redaction status is not allowlisted: {self.status!r}")
        _require(isinstance(self.ruleset, str) and bool(_REDACTION_RULESET.match(self.ruleset)), "redaction ruleset is invalid")


def require_content_hash_key(key: bytes) -> bytes:
    """Fail closed unless ``key`` is a usable server-held fingerprint key.

    The key is hub configuration (constructor/environment supplied); it must
    never be persisted next to the fingerprints it keys.
    """
    _require(
        isinstance(key, bytes) and len(key) >= MIN_CONTENT_HASH_KEY_BYTES,
        f"content hash key must be bytes of at least {MIN_CONTENT_HASH_KEY_BYTES} octets",
    )
    return key


def turn_content_hash(content: tuple[ContentBlock, ...] | list[ContentBlock], *, key: bytes) -> str:
    """Keyed ``hmac-sha256:<hex>`` fingerprint over the canonical content blocks.

    Identical content under different keys yields different fingerprints, so a
    party holding persisted metadata but not the hub key cannot test content
    equality or run a dictionary of guessed prompts against the stored values.
    """
    require_content_hash_key(key)
    for block in content:
        _require(isinstance(block, ContentBlock), "content hash input must be ContentBlock values")
    payload = [
        {"content_trust": block.content_trust, "kind": block.kind, "text": block.text}
        for block in content
    ]
    digest = hmac.new(key, _CHAT_CONTENT_PREFIX + canonical_json_bytes(payload), hashlib.sha256)
    return "hmac-sha256:" + digest.hexdigest()


@dataclass(frozen=True)
class Turn:
    """One append-only turn.  Per the contract, after creation only ``status``
    and ``completed_at`` may advance, from ``streaming`` to exactly one
    terminal state — the store advances those two fields in place; everything
    else is immutable, and a correction is a new sibling turn (an interrupted
    response keeps the partial text actually delivered, never fabricated as
    complete).  These frozen values model one observed state; the store owns
    the status advance.
    """

    id: str
    conversation_id: str
    role: str
    mode: str
    status: str
    lineage: TurnLineage
    content: tuple[ContentBlock, ...]
    content_hash: str
    redaction: TurnRedaction
    voice_events: tuple[VoiceEvent, ...] = ()
    created_at: datetime = field(default_factory=now_utc)
    completed_at: datetime | None = None
    #: True only on a retention/deletion tombstone: the content blocks were
    #: removed from the row and only lifecycle, lineage, typed voice events,
    #: and the keyed content fingerprint survive.
    content_purged: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "content", tuple(self.content))
        object.__setattr__(self, "voice_events", tuple(self.voice_events))
        _require(isinstance(self.id, str) and bool(_TURN_ID.match(self.id)), "turn id is invalid")
        _require(
            isinstance(self.conversation_id, str) and bool(_CONVERSATION_ID.match(self.conversation_id)),
            "turn conversation id is invalid",
        )
        _require(self.role in TURN_ROLES, f"turn role is not allowlisted: {self.role!r}")
        _require(self.mode in TURN_MODES, f"turn mode is not allowlisted: {self.mode!r}")
        _require(self.status in TURN_STATUSES, f"turn status is not allowlisted: {self.status!r}")
        _require(isinstance(self.lineage, TurnLineage), "turn lineage must be a TurnLineage")
        _require(isinstance(self.redaction, TurnRedaction), "turn redaction must be a TurnRedaction")
        _require(len(self.content) <= MAX_CONTENT_BLOCKS, "turn content exceeds the block bound")
        for block in self.content:
            _require(isinstance(block, ContentBlock), "turn content must contain ContentBlock values")
        _require(len(self.voice_events) <= MAX_VOICE_EVENTS, "turn voice events exceed the bound")
        for event in self.voice_events:
            _require(isinstance(event, VoiceEvent), "turn voice events must contain VoiceEvent values")
        _require(self.lineage.parent_turn_id != self.id, "a turn cannot be its own lineage parent")
        # The fingerprint is keyed, so the record can only check its shape here;
        # the store recomputes it with the hub key (:meth:`verify_content_hash`)
        # so a content rewrite can never hide behind a stale hash.
        _require(
            isinstance(self.content_hash, str) and bool(_CONTENT_HASH.match(self.content_hash)),
            "turn content hash is invalid",
        )
        _require(isinstance(self.content_purged, bool), "turn content_purged must be a bool")
        if self.content_purged:
            _require(self.content == (), "a content-purged turn must not carry content blocks")
        if self.redaction.status == "metadata_only":
            _require(self.content == (), "a metadata_only turn must not persist content text")
        if self.status == "streaming":
            _require(self.completed_at is None, "a streaming turn cannot carry completed_at")
        else:
            _require(self.completed_at is not None, f"a {self.status} turn requires completed_at")

    def verify_content_hash(self, key: bytes) -> None:
        """Fail closed unless the stored fingerprint recomputes from the stored content.

        A content-purged tombstone deliberately keeps the fingerprint of the
        purged content, so recomputation is meaningless there; callers must
        skip purged tombstones (calling anyway fails closed).
        """
        _require(not self.content_purged, "a content-purged turn's fingerprint cannot recompute")
        _require(
            self.content_hash == turn_content_hash(self.content, key=key),
            "turn content hash does not match its content",
        )

    @property
    def committed(self) -> bool:
        return self.status == "complete"

    @property
    def interrupted(self) -> bool:
        return self.status == "interrupted"

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_TURN_STATUSES


def make_turn(
    *,
    id: str,
    conversation_id: str,
    role: str,
    mode: str,
    status: str,
    lineage: TurnLineage,
    content: tuple[ContentBlock, ...] | list[ContentBlock] = (),
    redaction: TurnRedaction,
    voice_events: tuple[VoiceEvent, ...] | list[VoiceEvent] = (),
    created_at: datetime | None = None,
    completed_at: datetime | None = None,
    content_hash_key: bytes,
) -> Turn:
    """Build a live turn with its keyed fingerprint computed from the content."""
    return Turn(
        id=id,
        conversation_id=conversation_id,
        role=role,
        mode=mode,
        status=status,
        lineage=lineage,
        content=tuple(content),
        content_hash=turn_content_hash(tuple(content), key=content_hash_key),
        redaction=redaction,
        voice_events=tuple(voice_events),
        created_at=created_at if created_at is not None else now_utc(),
        completed_at=completed_at,
    )


def purge_turn_content(turn: Turn) -> Turn:
    """Project one turn into its content-purged tombstone.

    The content blocks are REMOVED from the record, not flagged over: only
    lifecycle (ids, role, mode, status, timestamps), lineage, typed voice
    events, and the keyed content fingerprint survive.  Idempotent; the purged
    marker is one-way because a purged record refuses content at construction.
    """
    _require(isinstance(turn, Turn), "purge requires a Turn value")
    if turn.content_purged:
        return turn
    return replace(turn, content=(), content_purged=True)


def validate_turn_append(
    conversation: Conversation,
    existing_turns: tuple[Turn, ...] | list[Turn],
    new_turn: Turn,
) -> Turn:
    """Fail-closed append gate for the hub store (contracts README convention 11).

    Enforces, before persistence, the cross-record rules the turn schema cannot
    express: appends only to the owning active conversation, a single
    null-parent root, ``(parent_turn_id, sibling_index)`` uniqueness, parent
    existence in the same conversation (a parent id living in another
    conversation is refused even though the turn exists there), acyclicity, and
    the retention mapping for ``kind: "transcript"`` content.  Returns the
    validated turn unchanged; raises :class:`ConversationError` otherwise.
    """
    _require(isinstance(new_turn, Turn), "append requires a Turn value")
    _require(
        new_turn.conversation_id == conversation.id,
        "turn does not belong to this conversation; lineage cannot cross conversation ownership",
    )
    _require(
        conversation.status == "active",
        f"conversation {conversation.status!r} does not accept turn appends",
    )

    # Lineage is scoped to the owning conversation: turns from any other
    # conversation are invisible to parent resolution by construction.
    own = [turn for turn in existing_turns if turn.conversation_id == conversation.id]
    _require(
        all(turn.id != new_turn.id for turn in existing_turns),
        f"turn id already exists and turns are append-only: {new_turn.id}",
    )

    parent_id = new_turn.lineage.parent_turn_id
    if parent_id is None:
        _require(
            all(turn.lineage.parent_turn_id is not None for turn in own),
            "conversation already has its single null-parent root turn",
        )
    else:
        own_by_id = {turn.id: turn for turn in own}
        if parent_id not in own_by_id:
            foreign = any(turn.id == parent_id for turn in existing_turns)
            if foreign:
                raise ConversationError(
                    "lineage parent belongs to a different conversation; lineage cannot cross conversation ownership",
                )
            raise ConversationError(f"lineage parent does not exist in this conversation: {parent_id}")
        # The new turn id is fresh and its parent pre-exists, so the append
        # itself cannot close a cycle; walk the ancestor chain anyway so a
        # corrupted existing record set is refused instead of extended.
        cursor: str | None = parent_id
        steps = 0
        while cursor is not None:
            steps += 1
            _require(steps <= len(own), "existing turn lineage contains a cycle")
            node = own_by_id.get(cursor)
            _require(node is not None, f"existing turn lineage chain is broken at: {cursor}")
            assert node is not None
            cursor = node.lineage.parent_turn_id

    _require(
        all(
            (turn.lineage.parent_turn_id, turn.lineage.sibling_index)
            != (parent_id, new_turn.lineage.sibling_index)
            for turn in own
        ),
        "lineage (parent_turn_id, sibling_index) is already taken in this conversation",
    )

    # Retention mapping: metadata_only means no transcript content block may
    # persist for that content kind; only bounded counters/metadata survive.
    # A turn is voice-INPUT governed only when a voice-input event is present;
    # voice-output events (tts_*) never relax the text policy. When BOTH input
    # kinds are conceivable the stricter policy wins, and provenance is
    # inferred from the events the caller attached — the hub store must
    # persist voice events with their transcript in the same append (see the
    # module docstring), or the text policy governs.
    has_voice_input = any(
        event.event in VOICE_INPUT_EVENT_KINDS for event in new_turn.voice_events
    )
    for block in new_turn.content:
        if block.kind != "transcript":
            continue
        governing = "voice_transcript_text" if has_voice_input else "transcript_text"
        _require(
            getattr(conversation.retention, governing) == "retained_redacted",
            f"conversation retention {governing}=metadata_only forbids persisting transcript content",
        )
    return new_turn


@dataclass(frozen=True)
class TurnAudit:
    """Safe lifecycle audit for one turn: lineage, hash, and counts only.

    Deliberately no message-content field of any kind — no text, no content
    blocks, no title — so this shape can survive content purges and feed
    redacted projections.
    """

    turn_id: str
    conversation_id: str
    role: str
    mode: str
    status: str
    lineage_kind: str
    parent_turn_id: str | None
    sibling_index: int
    content_hash: str
    content_block_count: int
    voice_event_count: int
    content_purged: bool
    created_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True)
class ConversationAudit:
    """Safe lifecycle audit for one conversation: identity, lifecycle, counts.

    This is also the tombstone shape ``purge_content_keep_tombstone`` leaves
    behind: identity plus turn counts, never retained message content.
    """

    conversation_id: str
    actor_id: str
    status: str
    retention_policy_id: str
    deletion_mode: str | None
    turn_count: int
    committed_turn_count: int
    interrupted_turn_count: int
    created_at: datetime
    updated_at: datetime


def turn_audit(turn: Turn) -> TurnAudit:
    """Project one turn into its content-free audit record."""
    return TurnAudit(
        turn_id=turn.id,
        conversation_id=turn.conversation_id,
        role=turn.role,
        mode=turn.mode,
        status=turn.status,
        lineage_kind=turn.lineage.kind,
        parent_turn_id=turn.lineage.parent_turn_id,
        sibling_index=turn.lineage.sibling_index,
        content_hash=turn.content_hash,
        content_block_count=len(turn.content),
        voice_event_count=len(turn.voice_events),
        content_purged=turn.content_purged,
        created_at=turn.created_at,
        completed_at=turn.completed_at,
    )


@dataclass(frozen=True)
class RetentionPreview:
    """Content-free projection of a conversation a batched retention pass WOULD act on.

    Identity, the lifecycle timestamps, the ``delete_after`` ceiling, and
    bounded turn counts only — deliberately no ``title`` and no message content
    of any kind, so an operator can review the scope of the off-read-path pass
    (:meth:`~workbench.conversation_store.MemoryConversationStore.retention_preview`)
    without content leaking into the preview and without a read ever triggering
    deletion.
    """

    conversation_id: str
    reason: str
    turn_count: int
    committed_turn_count: int
    interrupted_turn_count: int
    created_at: datetime
    updated_at: datetime
    delete_after: datetime | None

    def __post_init__(self) -> None:
        _require(
            self.reason in RETENTION_PREVIEW_REASONS,
            f"retention preview reason is not allowlisted: {self.reason!r}",
        )


def retention_preview_of(
    conversation: Conversation, turns: tuple[Turn, ...] | list[Turn], reason: str,
) -> RetentionPreview:
    """Project one conversation into its content-free retention-preview row."""
    own = [turn for turn in turns if turn.conversation_id == conversation.id]
    return RetentionPreview(
        conversation_id=conversation.id,
        reason=reason,
        turn_count=len(own),
        committed_turn_count=sum(1 for turn in own if turn.committed),
        interrupted_turn_count=sum(1 for turn in own if turn.interrupted),
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        delete_after=conversation.retention.delete_after,
    )


def conversation_audit(conversation: Conversation, turns: tuple[Turn, ...] | list[Turn]) -> ConversationAudit:
    """Project one conversation and its turns into a content-free audit record."""
    own = [turn for turn in turns if turn.conversation_id == conversation.id]
    return ConversationAudit(
        conversation_id=conversation.id,
        actor_id=conversation.actor.actor_id,
        status=conversation.status,
        retention_policy_id=conversation.retention.policy_id,
        deletion_mode=conversation.deletion.mode if conversation.deletion is not None else None,
        turn_count=len(own),
        committed_turn_count=sum(1 for turn in own if turn.committed),
        interrupted_turn_count=sum(1 for turn in own if turn.interrupted),
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )
