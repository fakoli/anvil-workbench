"""Redacted conversation export / same-actor import (chat-first-voice:T012).

A vertical slice over the reviewed conversation spine, built in the exact idiom
of :mod:`workbench.configuration_transfer`.  It never becomes a new privilege: it
reads and writes ONLY through the injected
:class:`~workbench.conversation_store.ConversationStore` (actor-scoped ownership,
append-only lineage, retention/redaction enforcement) that the rest of the chat
surface uses.  Three operations compose those primitives:

* **export** — a CLOSED, versioned, REDACTED serialization of one conversation the
  requesting actor OWNS, plus a SAFE OPAQUE actor/conversation reference (never the
  raw identity).  A ``metadata_only`` turn, a redaction ``metadata_only`` turn, and
  a purged/deleted tombstone export as METADATA ONLY — their content is structurally
  absent, mirroring the voice relay's no-raw-audio / no-transcript-draft discipline.
  Every retained content string is value-scanned with
  :func:`~workbench.redaction.redact_conversation_text` (the config-strength scrub
  plus the no-audio media scrub) and the router scrubs the serialized body at the
  last hop, so a secret / path / dotless ``serving:8443`` / ``AKIA…`` / JWT / PEM /
  DB URL / ``data:audio;base64`` blob can never ride out.

* **import preview** ("sampling") — a content-free SAMPLE of what an apply would
  create (turn counts by role/lifecycle, how many carry content vs are
  metadata-only) plus the closed-schema validity verdict.  Mutates NOTHING.

* **import apply** — validate the WHOLE artifact (closed envelope,
  ``additionalProperties:false`` recursively), then replay it into a BRAND-NEW
  conversation owned by the REQUESTING actor ONLY.  It can never target another
  actor or an existing conversation (no cross-actor / cross-conversation write, no
  existence oracle), and it replays every turn through the store's append gate, so
  append-only lineage is preserved and can never be rewritten.  Deleted or purged
  content can NEVER re-enter: the export omits it, and the import refuses an
  artifact that pairs a purged / metadata-only turn with content — nothing is
  resurrected on a round trip.  The apply is ATOMIC: it is fully pre-validated, and
  any residual failure rolls the just-created conversation back entirely.

Like the other supervision models this service is not wired into the live bridge
poll loop; the hub app leaves it ``None`` (fail-closed 503) until injected.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from .conversation_models import (
    CONTENT_KINDS,
    LINEAGE_KINDS,
    MAX_CONTENT_TEXT_CHARS,
    MAX_SIBLING_INDEX,
    MAX_TITLE_CHARS,
    REDACTION_STATUSES,
    TERMINAL_TURN_STATUSES,
    TURN_MODES,
    TURN_ROLES,
    ContentBlock,
    Conversation,
    ConversationActor,
    ConversationError,
    RetentionPolicy,
    Turn,
    TurnLineage,
    TurnRedaction,
    VoiceEvent,
    is_metadata_only,
)
from .conversation_store import ConversationStore, ConversationStoreError
from .models import new_id, now_utc, opaque_scope_ref, require_pref_audit_key
from .redaction import redact_conversation_text

#: The pinned export/envelope schema version.  An envelope declaring any other
#: version is an unknown/unsupported envelope and is REJECTED, not coerced.
CONVERSATION_EXPORT_SCHEMA_VERSION = "workbench-conversation-export/v1"

#: The CLOSED key set of each envelope level.  A key outside these sets is an
#: unknown extension the import refuses rather than interpreting loosely
#: (``additionalProperties:false`` recursively).
_ENVELOPE_KEYS = frozenset({"schema_version", "source", "conversation", "turns"})
_SOURCE_KEYS = frozenset({"actor_ref", "conversation_ref"})
_CONVERSATION_KEYS = frozenset({"title", "retention", "metadata_only"})
_RETENTION_KEYS = frozenset({"policy_id", "transcript_text", "voice_transcript_text"})
_TURN_KEYS = frozenset({
    "id", "role", "mode", "status", "lineage", "redaction", "voice_events",
    "content", "content_purged", "content_omitted", "content_omitted_reason",
})
_LINEAGE_KEYS = frozenset({"parent_turn_id", "sibling_index", "kind"})
_REDACTION_KEYS = frozenset({"status", "ruleset"})
_CONTENT_KEYS = frozenset({"kind", "text"})
_VOICE_EVENT_KEYS = frozenset({"event", "at", "duration_ms", "transcript_chars"})

#: Hard bound on how many turns one import may replay, so a hand-crafted artifact
#: cannot ask the hub to build an unbounded conversation.
_MAX_IMPORT_TURNS = 5_000


class ConversationTransferError(ValueError):
    """A conversation envelope is malformed/unsupported, or an import is invalid.

    Raised BEFORE any effect, so a rejected envelope or an invalid import mutates
    nothing.  The API maps it to a typed 422.
    """


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _content_omitted(turn: Turn) -> tuple[bool, str | None]:
    """Whether a turn's content is structurally excluded from the export.

    Content is NEVER exported for a purged/deleted tombstone or a
    ``metadata_only`` redaction turn — only lifecycle, lineage, and voice-event
    metadata survive, mirroring the durable no-content discipline.
    """
    if turn.content_purged:
        return True, "purged"
    if turn.redaction.status == "metadata_only":
        return True, "metadata_only"
    return False, None


class ConversationTransferService:
    """The wired export/import entrypoint over the reviewed conversation spine.

    Composes the reviewed primitives rather than re-implementing them: the
    injected :class:`~workbench.conversation_store.ConversationStore`
    (actor-scoped ownership + append-only lineage + retention/redaction
    enforcement) and the shared keyed opaque-reference / audit-fingerprint
    primitives.  The audit trail is an in-memory list of NON-IDENTIFYING records
    keyed by the same server-held key as the opaque refs.
    """

    def __init__(self, conversation_store: ConversationStore, *, audit_key: bytes) -> None:
        self._store = conversation_store
        self._audit_key = require_pref_audit_key(audit_key)
        self._audit_records: list[dict[str, Any]] = []

    # -- export (pure read; cannot mutate) ----------------------------------

    def export(self, *, actor: ConversationActor, conversation_id: str) -> dict[str, Any]:
        """A CLOSED, versioned, redacted export of one OWNED conversation.

        Reads through the store's actor-scoped ownership resolution, so a
        conversation owned by another actor (or a missing id) raises the store's
        ``UnknownConversationError`` — the export is never a cross-actor read or
        existence oracle.  Content is included ONLY for a live, retained,
        non-purged turn (value-scanned); a metadata_only / purged / deleted turn
        exports as metadata only.  The source names the actor and conversation by
        a SAFE OPAQUE keyed token only, never the raw id.
        """
        record, turns = self._store.get_conversation_with_turns(actor, conversation_id)
        return {
            "schema_version": CONVERSATION_EXPORT_SCHEMA_VERSION,
            "source": {
                "actor_ref": opaque_scope_ref("actor", actor.actor_id, key=self._audit_key),
                "conversation_ref": opaque_scope_ref("conversation", record.id, key=self._audit_key),
            },
            "conversation": {
                # The title is owner prose; value-scan it like any content string.
                "title": redact_conversation_text(record.title) if record.title else None,
                "retention": {
                    "policy_id": record.retention.policy_id,
                    "transcript_text": record.retention.transcript_text,
                    "voice_transcript_text": record.retention.voice_transcript_text,
                },
                "metadata_only": is_metadata_only(record.retention),
            },
            "turns": [self._export_turn(turn) for turn in turns],
        }

    def _export_turn(self, turn: Turn) -> dict[str, Any]:
        omitted, reason = _content_omitted(turn)
        content: list[dict[str, Any]] = []
        if not omitted:
            content = [
                {"kind": block.kind, "text": redact_conversation_text(block.text)}
                for block in turn.content
            ]
        return {
            "id": turn.id,
            "role": turn.role,
            "mode": turn.mode,
            "status": turn.status,
            "lineage": {
                "parent_turn_id": turn.lineage.parent_turn_id,
                "sibling_index": turn.lineage.sibling_index,
                "kind": turn.lineage.kind,
            },
            "redaction": {"status": turn.redaction.status, "ruleset": turn.redaction.ruleset},
            "voice_events": [
                {
                    "event": event.event,
                    "at": _iso(event.at),
                    "duration_ms": event.duration_ms,
                    "transcript_chars": event.transcript_chars,
                }
                for event in turn.voice_events
            ],
            "content": content,
            "content_purged": turn.content_purged,
            "content_omitted": omitted,
            "content_omitted_reason": reason,
        }

    # -- import (validate / preview / apply) --------------------------------

    def import_preview(self, *, actor: ConversationActor, envelope: Mapping[str, Any]) -> dict[str, Any]:
        """A content-free SAMPLE of what an apply would create; mutate NOTHING.

        Fully validates the closed envelope (so an invalid artifact is rejected
        here before any apply) and returns counts by role/lifecycle plus how many
        turns carry content vs are metadata-only — never a content string.
        """
        plan = self._plan(envelope)
        turns = plan["turns"]
        content_turns = sum(1 for spec in turns if spec["content"])
        return {
            "valid": True,
            "schema_version": CONVERSATION_EXPORT_SCHEMA_VERSION,
            "turn_count": len(turns),
            "role_counts": {
                role: sum(1 for spec in turns if spec["role"] == role)
                for role in sorted(TURN_ROLES)
            },
            "content_turn_count": content_turns,
            "metadata_only_turn_count": len(turns) - content_turns,
            "metadata_only_conversation": plan["metadata_only"],
        }

    def import_apply(self, *, actor: ConversationActor, envelope: Mapping[str, Any]) -> dict[str, Any]:
        """Replay a valid artifact into a NEW conversation owned by ``actor``, atomically.

        The whole artifact is validated first; then a fresh conversation is
        created for the REQUESTING actor and every turn is replayed through the
        store's append gate (preserving ids + lineage), so append-only history is
        preserved and can never be rewritten.  It targets the requesting actor's
        scope ONLY — never another actor or an existing conversation.  On any
        residual failure the just-created conversation is purged entirely, so the
        apply is all-or-nothing.  Records one audit entry per created turn.
        """
        plan = self._plan(envelope)
        created = self._store.create_conversation(actor, plan["retention"], title=plan["title"])
        # Turn ids are unique across ALL of the acting actor's conversations, so an
        # import must MINT fresh ids and remap every parent reference through them
        # rather than reuse the exported ids (which would collide with the source
        # conversation).  The export is in lineage pre-order, so a parent is always
        # remapped before the child that names it; a dangling parent reference is
        # left untranslated and the append gate refuses it (then we roll back).
        try:
            id_map: dict[str, str] = {}
            for spec in plan["turns"]:
                new_tid = new_id("turn")
                old_id = spec["id"]
                if old_id is not None:
                    id_map[old_id] = new_tid
                old_lineage: TurnLineage = spec["lineage"]
                parent = old_lineage.parent_turn_id
                new_parent = id_map.get(parent, parent) if parent is not None else None
                lineage = TurnLineage(new_parent, old_lineage.sibling_index, old_lineage.kind)
                self._store.append_turn(
                    actor,
                    created.id,
                    role=spec["role"],
                    status=spec["status"],
                    mode=spec["mode"],
                    lineage=lineage,
                    redaction=spec["redaction"],
                    content=spec["content"],
                    voice_events=spec["voice_events"],
                    turn_id=new_tid,
                )
        except (ConversationStoreError, ConversationError) as exc:
            # Roll the partial import back entirely, so a residual gate refusal
            # leaves nothing behind — the apply is all-or-nothing.
            try:
                self._store.delete_conversation(actor, created.id, "purge_all_records")
            except ConversationStoreError:  # pragma: no cover - defensive cleanup
                pass
            raise ConversationTransferError(
                f"the import could not be applied atomically and was rolled back: {exc}"
            ) from exc
        self._record_audit("conversation.import", actor, created.id, len(plan["turns"]))
        return {
            "conversation_id": created.id,
            "turn_count": len(plan["turns"]),
            "metadata_only_conversation": plan["metadata_only"],
        }

    # -- internal validation / planning -------------------------------------

    def _plan(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        """Validate the closed envelope into a typed, append-ready plan (no I/O).

        Raises :class:`ConversationTransferError` on any malformed/unsupported
        shape or any content-resurrection attempt, BEFORE the caller touches the
        store, so an invalid import applies nothing.
        """
        validated = validate_conversation_envelope(envelope)
        retention, title, metadata_only = self._plan_conversation(validated.get("conversation"))
        raw_turns = validated["turns"]
        if len(raw_turns) > _MAX_IMPORT_TURNS:
            raise ConversationTransferError("the conversation export carries too many turns")
        specs = [self._plan_turn(entry) for entry in raw_turns]
        return {
            "retention": retention,
            "title": title,
            "metadata_only": metadata_only,
            "turns": specs,
        }

    def _plan_conversation(
        self, block: Mapping[str, Any] | None,
    ) -> tuple[RetentionPolicy, str | None, bool]:
        block = block or {}
        retention_block = block.get("retention") or {}
        title = block.get("title")
        if title is not None and (not isinstance(title, str) or len(title) > MAX_TITLE_CHARS):
            raise ConversationTransferError("conversation title is invalid")
        try:
            retention = RetentionPolicy(
                policy_id=str(retention_block.get("policy_id", "workbench.default")),
                transcript_text=str(retention_block.get("transcript_text", "retained_redacted")),
                voice_transcript_text=str(retention_block.get("voice_transcript_text", "retained_redacted")),
                delete_after=None,
            )
        except ConversationError as exc:
            raise ConversationTransferError(f"conversation retention is invalid: {exc}") from exc
        return retention, (redact_conversation_text(title) if title else None), is_metadata_only(retention)

    def _plan_turn(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        role = entry.get("role")
        if role not in TURN_ROLES:
            raise ConversationTransferError("turn role is invalid")
        mode = entry.get("mode", "ordinary")
        if mode not in TURN_MODES:
            raise ConversationTransferError("turn mode is invalid")
        # A cut-off (streaming) turn is never resurrected as advanceable: any
        # non-terminal status is imported as interrupted, mirroring the store's
        # crash-recovery discipline.
        raw_status = entry.get("status")
        status = raw_status if raw_status in TERMINAL_TURN_STATUSES else "interrupted"

        content_purged = bool(entry.get("content_purged", False))
        content_omitted = bool(entry.get("content_omitted", False))
        redaction = self._plan_redaction(entry.get("redaction"))
        raw_content = entry.get("content", [])
        if not isinstance(raw_content, list):
            raise ConversationTransferError("turn content must be a list")

        # DELETED/PURGED NEVER RESURRECTS: an artifact that pairs a purged,
        # content-omitted, or metadata_only turn with actual content blocks is
        # refused outright — the round trip can never reintroduce removed content.
        content_forbidden = content_purged or content_omitted or redaction.status == "metadata_only"
        if content_forbidden and raw_content:
            raise ConversationTransferError(
                "a purged or metadata-only turn cannot carry content on import; deleted content never resurrects"
            )
        content = () if content_forbidden else self._plan_content(raw_content)

        return {
            "id": self._plan_turn_id(entry.get("id")),
            "role": role,
            "mode": mode,
            "status": status,
            "lineage": self._plan_lineage(entry.get("lineage")),
            "redaction": redaction,
            "content": content,
            "voice_events": self._plan_voice_events(entry.get("voice_events", [])),
        }

    @staticmethod
    def _plan_turn_id(turn_id: Any) -> str | None:
        if turn_id is None:
            return None
        if not isinstance(turn_id, str):
            raise ConversationTransferError("turn id must be a string")
        return turn_id

    def _plan_lineage(self, block: Any) -> TurnLineage:
        block = block or {}
        if not isinstance(block, Mapping):
            raise ConversationTransferError("turn lineage must be an object")
        parent = block.get("parent_turn_id")
        sibling = block.get("sibling_index", 0)
        kind = block.get("kind", "initial")
        if kind not in LINEAGE_KINDS:
            raise ConversationTransferError("turn lineage kind is invalid")
        if not isinstance(sibling, int) or isinstance(sibling, bool) or not 0 <= sibling <= MAX_SIBLING_INDEX:
            raise ConversationTransferError("turn lineage sibling_index is out of bounds")
        try:
            return TurnLineage(parent, sibling, kind)
        except ConversationError as exc:
            raise ConversationTransferError(f"turn lineage is invalid: {exc}") from exc

    def _plan_redaction(self, block: Any) -> TurnRedaction:
        block = block or {}
        if not isinstance(block, Mapping):
            raise ConversationTransferError("turn redaction must be an object")
        status = block.get("status", "redacted")
        ruleset = block.get("ruleset", "workbench.default")
        if status not in REDACTION_STATUSES:
            raise ConversationTransferError("turn redaction status is invalid")
        try:
            return TurnRedaction(status, ruleset)
        except ConversationError as exc:
            raise ConversationTransferError(f"turn redaction is invalid: {exc}") from exc

    def _plan_content(self, blocks: list[Any]) -> tuple[ContentBlock, ...]:
        result: list[ContentBlock] = []
        for block in blocks:
            if not isinstance(block, Mapping):
                raise ConversationTransferError("a content block must be an object")
            kind = block.get("kind")
            text = block.get("text")
            if kind not in CONTENT_KINDS:
                raise ConversationTransferError("content block kind is invalid")
            if not isinstance(text, str) or len(text) > MAX_CONTENT_TEXT_CHARS:
                raise ConversationTransferError("content block text is invalid")
            try:
                # Defence in depth: re-scan every content string on the way IN too,
                # so an imported artifact can never introduce an unscrubbed secret.
                result.append(ContentBlock(kind, redact_conversation_text(text)))
            except ConversationError as exc:
                raise ConversationTransferError(f"content block is invalid: {exc}") from exc
        return tuple(result)

    def _plan_voice_events(self, events: Any) -> tuple[VoiceEvent, ...]:
        if not isinstance(events, list):
            raise ConversationTransferError("turn voice_events must be a list")
        result: list[VoiceEvent] = []
        for event in events:
            if not isinstance(event, Mapping):
                raise ConversationTransferError("a voice event must be an object")
            at = event.get("at")
            parsed_at = self._parse_at(at)
            try:
                result.append(
                    VoiceEvent(
                        str(event.get("event")),
                        parsed_at,
                        event.get("duration_ms"),
                        event.get("transcript_chars"),
                    )
                )
            except ConversationError as exc:
                raise ConversationTransferError(f"voice event is invalid: {exc}") from exc
        return tuple(result)

    @staticmethod
    def _parse_at(value: Any) -> datetime:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except ValueError as exc:
                raise ConversationTransferError("voice event timestamp is invalid") from exc
        raise ConversationTransferError("voice event requires a timestamp")

    # -- audit --------------------------------------------------------------

    def _record_audit(self, action: str, actor: ConversationActor, conversation_id: str, turn_count: int) -> None:
        self._audit_records.append({
            "action": action,
            # NON-IDENTIFYING: the actor is named only by the same keyed opaque
            # token the export uses, never the raw identity.
            "actor_ref": opaque_scope_ref("actor", actor.actor_id, key=self._audit_key),
            "conversation_ref": opaque_scope_ref("conversation", conversation_id, key=self._audit_key),
            "turn_count": turn_count,
            "recorded_at": now_utc().isoformat(),
        })

    def audit_records(self) -> list[dict[str, Any]]:
        """The non-identifying audit trail of applied imports (browser-safe)."""
        return [dict(record) for record in self._audit_records]


def _reject_extra(block: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    extra = set(map(str, block)) - allowed
    if extra:
        raise ConversationTransferError(
            f"conversation envelope {where} has unsupported keys: {sorted(extra)}"
        )


def validate_conversation_envelope(envelope: Any) -> dict[str, Any]:
    """Return the envelope only if it is a CLOSED, supported conversation export.

    Fail closed on anything unexpected: a non-object, an unknown key at ANY level
    (an extension envelope), a wrong/absent ``schema_version``, a non-list
    ``turns``, or a malformed nested object.  An unsupported extension envelope is
    REJECTED, never interpreted loosely (``additionalProperties:false`` applied
    recursively).
    """
    if not isinstance(envelope, Mapping):
        raise ConversationTransferError("a conversation envelope must be an object")
    _reject_extra(envelope, _ENVELOPE_KEYS, "top-level")
    if envelope.get("schema_version") != CONVERSATION_EXPORT_SCHEMA_VERSION:
        raise ConversationTransferError(
            "conversation envelope declares an unknown or unsupported schema_version"
        )
    source = envelope.get("source")
    if source is not None:
        if not isinstance(source, Mapping):
            raise ConversationTransferError("conversation envelope source must be an object")
        _reject_extra(source, _SOURCE_KEYS, "source")
    conversation = envelope.get("conversation")
    if conversation is not None:
        if not isinstance(conversation, Mapping):
            raise ConversationTransferError("conversation envelope conversation must be an object")
        _reject_extra(conversation, _CONVERSATION_KEYS, "conversation")
        retention = conversation.get("retention")
        if retention is not None:
            if not isinstance(retention, Mapping):
                raise ConversationTransferError("conversation envelope retention must be an object")
            _reject_extra(retention, _RETENTION_KEYS, "retention")
    turns = envelope.get("turns")
    if not isinstance(turns, list):
        raise ConversationTransferError("conversation envelope turns must be a list")
    for entry in turns:
        if not isinstance(entry, Mapping):
            raise ConversationTransferError("a conversation turn entry must be an object")
        _reject_extra(entry, _TURN_KEYS, "turn")
        lineage = entry.get("lineage")
        if lineage is not None:
            if not isinstance(lineage, Mapping):
                raise ConversationTransferError("a turn lineage must be an object")
            _reject_extra(lineage, _LINEAGE_KEYS, "turn lineage")
        redaction = entry.get("redaction")
        if redaction is not None:
            if not isinstance(redaction, Mapping):
                raise ConversationTransferError("a turn redaction must be an object")
            _reject_extra(redaction, _REDACTION_KEYS, "turn redaction")
        content = entry.get("content")
        if content is not None:
            if not isinstance(content, list):
                raise ConversationTransferError("a turn content must be a list")
            for block in content:
                if not isinstance(block, Mapping):
                    raise ConversationTransferError("a turn content block must be an object")
                _reject_extra(block, _CONTENT_KEYS, "content block")
        voice_events = entry.get("voice_events")
        if voice_events is not None:
            if not isinstance(voice_events, list):
                raise ConversationTransferError("a turn voice_events must be a list")
            for event in voice_events:
                if not isinstance(event, Mapping):
                    raise ConversationTransferError("a turn voice event must be an object")
                _reject_extra(event, _VOICE_EVENT_KEYS, "voice event")
    return dict(envelope)
