"""Actor-scoped conversation and turn API projection (chat-first-voice T002.4).

A thin HTTP projection over :mod:`workbench.conversation_store`: every
endpoint derives the acting identity from the trusted request-context
dependency the hub passes in (the tailnet identity header resolved by
``create_app``'s ``actor`` dependency), never from a request body or query
field.  The input models forbid unknown fields, so a smuggled ``actor`` body
field is rejected with 422 instead of silently ignored, and unknown query
parameters are ignored by the framework without ever reaching the store.

All business rules stay in the store — ownership, lineage, retention,
deletion reconciliation, streaming recovery.  The router only converts typed
values, maps errors, and serializes responses:

* The owning actor's own conversation content IS returned (this is the
  actor's read surface; the chat contracts permit content to the owner).
* Responses never serialize the content-hash fingerprint or its key, another
  actor's records, or hub-internal fields.  The store's HUB-INTERNAL
  operations (``list_audit``, ``recover_streaming_turns``,
  ``enforce_retention``) are deliberately not wired to any endpoint.
* A cross-actor probe raises the store's ``UnknownConversationError``, which
  the registered handler renders as the same fixed 404 body as a truly
  missing id — no existence leak.
* When the hub has no configured content-hash key there is no conversation
  store, and every chat endpoint refuses with 503 instead of serving.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Callable

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .conversation_models import (
    MAX_CONTENT_BLOCKS,
    MAX_CONTENT_TEXT_CHARS,
    MAX_SIBLING_INDEX,
    MAX_TITLE_CHARS,
    MAX_VOICE_EVENTS,
    ContentBlock,
    Conversation,
    ConversationActor,
    ConversationAudit,
    ConversationError,
    RetentionPolicy,
    Turn,
    TurnLineage,
    TurnRedaction,
    VoiceEvent,
)
from .conversation_store import ConversationStore, UnknownConversationError
from .idempotency_store import IdempotencyStore, MemoryIdempotencyStore, request_hash_for

#: The one non-leaking body every unknown-or-foreign conversation lookup gets.
_UNKNOWN_DETAIL = "unknown conversation"


def conversation_actor(trusted_name: str) -> ConversationActor:
    """Map the trusted request-context identity to its conversation owner.

    The tailnet identity is already authenticated and allowlisted by the hub's
    ``actor`` dependency before it reaches here.  A login that fits the
    conversation actor-id charset is used directly; one that does not (for
    example an email login containing ``@``) is mapped deterministically to a
    hashed ``id-<sha256>`` id so the same identity always owns the same
    conversations.  The ``id-`` prefix is RESERVED for that hashed namespace:
    a direct login that itself begins with ``id-`` is hashed too, so the
    hashed and direct id-spaces are provably disjoint and two distinct logins
    can never collapse to the same owner.  The mapping never consults request
    data — only the trusted identity string.
    """
    try:
        if trusted_name.startswith("id-"):
            raise ConversationError("id- is a reserved hashed-actor namespace")
        return ConversationActor(trusted_name)
    except ConversationError:
        digest = hashlib.sha256(trusted_name.encode("utf-8")).hexdigest()
        return ConversationActor(f"id-{digest}")


# --- bounded request models (chat-conversation.v1 / chat-turn.v1 limits) ----


class _ChatInput(BaseModel):
    """Chat inputs fail closed on unknown fields (no smuggled ``actor``)."""

    model_config = ConfigDict(extra="forbid")


class RetentionInput(_ChatInput):
    policy_id: str = Field(default="workbench.default", pattern=r"^[a-z][a-z0-9._-]{0,63}$")
    transcript_text: str = Field(default="retained_redacted", pattern=r"^(retained_redacted|metadata_only)$")
    voice_transcript_text: str = Field(default="retained_redacted", pattern=r"^(retained_redacted|metadata_only)$")
    delete_after: datetime | None = None


class ConversationInput(_ChatInput):
    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE_CHARS)
    retention: RetentionInput = Field(default_factory=RetentionInput)


class RenameInput(_ChatInput):
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)


class DeleteInput(_ChatInput):
    mode: str = Field(pattern=r"^(purge_content_keep_tombstone|purge_all_records)$")


class LineageInput(_ChatInput):
    parent_turn_id: str | None = Field(default=None, min_length=13, max_length=133)
    sibling_index: int = Field(default=0, ge=0, le=MAX_SIBLING_INDEX)
    kind: str = Field(default="initial", pattern=r"^(initial|retry|branch)$")


class ContentBlockInput(_ChatInput):
    kind: str = Field(pattern=r"^(text|transcript|summary)$")
    text: str = Field(max_length=MAX_CONTENT_TEXT_CHARS)


class VoiceEventInput(_ChatInput):
    event: str = Field(pattern=r"^(utterance_start|stt_commit|tts_start|tts_stop|interruption)$")
    at: datetime
    duration_ms: int | None = Field(default=None, ge=0, le=3_600_000)
    transcript_chars: int | None = Field(default=None, ge=0, le=100_000)


class RedactionInput(_ChatInput):
    status: str = Field(default="redacted", pattern=r"^(redacted|metadata_only)$")
    ruleset: str = Field(default="workbench.default", pattern=r"^[a-z][a-z0-9._-]{1,127}$")


class TurnBodyInput(_ChatInput):
    """The caller-supplied slice of one turn (retry/branch lineage is derived)."""

    role: str = Field(pattern=r"^(user|assistant)$")
    status: str = Field(pattern=r"^(streaming|complete|interrupted|cancelled|failed)$")
    mode: str = Field(default="ordinary", pattern=r"^(ordinary|advanced)$")
    redaction: RedactionInput = Field(default_factory=RedactionInput)
    content: list[ContentBlockInput] = Field(default_factory=list, max_length=MAX_CONTENT_BLOCKS)
    voice_events: list[VoiceEventInput] = Field(default_factory=list, max_length=MAX_VOICE_EVENTS)


class TurnAppendInput(TurnBodyInput):
    lineage: LineageInput = Field(default_factory=LineageInput)


class TurnStatusInput(_ChatInput):
    status: str = Field(pattern=r"^(complete|interrupted|cancelled|failed)$")


# --- typed-value conversion (validation errors fail closed as 409) ----------


def _retention(payload: RetentionInput) -> RetentionPolicy:
    return RetentionPolicy(
        payload.policy_id, payload.transcript_text, payload.voice_transcript_text, payload.delete_after,
    )


def _redaction(payload: RedactionInput) -> TurnRedaction:
    return TurnRedaction(payload.status, payload.ruleset)


def _content(blocks: list[ContentBlockInput]) -> tuple[ContentBlock, ...]:
    return tuple(ContentBlock(block.kind, block.text) for block in blocks)


def _voice_events(events: list[VoiceEventInput]) -> tuple[VoiceEvent, ...]:
    return tuple(
        VoiceEvent(event.event, event.at, event.duration_ms, event.transcript_chars)
        for event in events
    )


# --- owner-facing serialization (never the fingerprint, never the key) ------


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def conversation_json(record: Conversation) -> dict[str, Any]:
    deletion = record.deletion
    return {
        "id": record.id,
        "status": record.status,
        "title": record.title,
        "retention": {
            "policy_id": record.retention.policy_id,
            "transcript_text": record.retention.transcript_text,
            "voice_transcript_text": record.retention.voice_transcript_text,
            "delete_after": _iso(record.retention.delete_after),
        },
        "deletion": None if deletion is None else {
            "mode": deletion.mode,
            "requested_at": _iso(deletion.requested_at),
            "completed_at": _iso(deletion.completed_at),
        },
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
    }


def turn_json(turn: Turn) -> dict[str, Any]:
    """One turn as the owner sees it: content, lineage pointers, truthful state.

    ``committed``/``interrupted`` mirror the domain properties so an
    interrupted or still-streaming response can never read as complete, and
    the ``lineage`` block carries the ``(parent_turn_id, sibling_index,
    kind)`` pointers.  The keyed content fingerprint is hub-internal and is
    deliberately absent.
    """
    return {
        "id": turn.id,
        "conversation_id": turn.conversation_id,
        "role": turn.role,
        "mode": turn.mode,
        "status": turn.status,
        "committed": turn.committed,
        "interrupted": turn.interrupted,
        "terminal": turn.terminal,
        "content_purged": turn.content_purged,
        "lineage": {
            "parent_turn_id": turn.lineage.parent_turn_id,
            "sibling_index": turn.lineage.sibling_index,
            "kind": turn.lineage.kind,
        },
        "content": [
            {"kind": block.kind, "text": block.text, "content_trust": block.content_trust}
            for block in turn.content
        ],
        "voice_events": [
            {
                "event": event.event,
                "at": _iso(event.at),
                "duration_ms": event.duration_ms,
                "transcript_chars": event.transcript_chars,
            }
            for event in turn.voice_events
        ],
        "created_at": _iso(turn.created_at),
        "completed_at": _iso(turn.completed_at),
    }


def deletion_json(audit: ConversationAudit) -> dict[str, Any]:
    """The content-free deletion outcome; counts and lifecycle only."""
    return {
        "conversation_id": audit.conversation_id,
        "status": audit.status,
        "deletion_mode": audit.deletion_mode,
        "turn_count": audit.turn_count,
        "committed_turn_count": audit.committed_turn_count,
        "interrupted_turn_count": audit.interrupted_turn_count,
    }


# --- wiring -----------------------------------------------------------------


def register_conversation_handlers(app: FastAPI) -> None:
    """Map store errors to non-leaking HTTP responses.

    ``UnknownConversationError`` (raised identically for a missing id and a
    foreign actor's id) always renders the same fixed 404 body, so the two
    cases are byte-identical on the wire.  Any other chat contract violation
    is a 409, matching the hub's ``StoreError`` idiom.
    """

    @app.exception_handler(UnknownConversationError)
    async def _unknown_conversation(_: Request, __: UnknownConversationError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": _UNKNOWN_DETAIL})

    @app.exception_handler(ConversationError)
    async def _conversation_error(_: Request, exc: ConversationError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})


#: The header carrying the caller-supplied idempotency key on a side-effecting
#: chat request.  It is optional: a request without it executes normally (no
#: dedup); one with it converges concurrent or retried requests on one record.
IDEMPOTENCY_HEADER = "Idempotency-Key"


def build_conversation_router(
    actor_dependency: Callable[..., str],
    conversation_store: ConversationStore | None,
    idempotency_store: IdempotencyStore | None = None,
) -> APIRouter:
    """Build the actor-scoped chat router over an already-authenticated actor.

    ``actor_dependency`` is the hub's trusted identity dependency (header +
    allowlist); it is the only source of the acting identity.  When
    ``conversation_store`` is ``None`` the hub has no content-hash key and
    every endpoint refuses with 503.

    Every side-effecting endpoint honours an optional ``Idempotency-Key``
    header via ``idempotency_store``: a retried or concurrent request carrying
    the same key and the same payload yields exactly one record and replays the
    identical response, the key is scoped per actor and operation, and the same
    key with a materially different payload is refused with a typed conflict.
    A fresh :class:`~workbench.idempotency_store.MemoryIdempotencyStore` is
    built when one is not injected.
    """
    router = APIRouter(prefix="/api/conversations")
    idempotency = idempotency_store if idempotency_store is not None else MemoryIdempotencyStore()

    def chat_store() -> ConversationStore:
        if conversation_store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="chat persistence is not configured; set WORKBENCH_CHAT_HASH_KEY",
            )
        return conversation_store

    def chat_actor(current_actor: str = Depends(actor_dependency)) -> ConversationActor:
        return conversation_actor(current_actor)

    def idempotent(
        actor: ConversationActor,
        operation: str,
        key: str | None,
        material: dict[str, Any],
        executor: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        """Run a side-effecting endpoint under optional idempotency.

        Without a key the operation executes normally (no dedup); with one it is
        deduplicated per ``(actor, operation, key)`` and bound to the canonical
        hash of ``material`` (the path identifiers plus the validated body), so
        a retry with the same payload replays the stored response and a reused
        key with a different payload is refused.
        """
        if key is None:
            return executor()
        request_hash = request_hash_for(operation, material)
        response, _ = idempotency.run(actor, operation, key, request_hash, executor)
        return response

    @router.post("", status_code=status.HTTP_201_CREATED)
    def create_conversation(
        payload: ConversationInput,
        actor: ConversationActor = Depends(chat_actor),
        idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
    ) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            record = chat_store().create_conversation(actor, _retention(payload.retention), title=payload.title)
            return conversation_json(record)

        return idempotent(actor, "conversations.create", idempotency_key, {"body": payload.model_dump(mode="json")}, _run)

    @router.get("")
    def list_conversations(
        include_archived: bool = False, actor: ConversationActor = Depends(chat_actor),
    ) -> dict[str, Any]:
        records = chat_store().list_conversations(actor, include_archived=include_archived)
        return {"conversations": [conversation_json(record) for record in records]}

    @router.get("/search")
    def search_conversations(
        query: str = Query(min_length=1, max_length=MAX_TITLE_CHARS),
        include_archived: bool = False,
        actor: ConversationActor = Depends(chat_actor),
    ) -> dict[str, Any]:
        records = chat_store().search_conversations(actor, query, include_archived=include_archived)
        return {"conversations": [conversation_json(record) for record in records]}

    @router.get("/{conversation_id}")
    def get_conversation(conversation_id: str, actor: ConversationActor = Depends(chat_actor)) -> dict[str, Any]:
        record, turns = chat_store().get_conversation_with_turns(actor, conversation_id)
        return {"conversation": conversation_json(record), "turns": [turn_json(turn) for turn in turns]}

    @router.post("/{conversation_id}/rename")
    def rename_conversation(
        conversation_id: str, payload: RenameInput, actor: ConversationActor = Depends(chat_actor),
        idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
    ) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            return conversation_json(chat_store().rename_conversation(actor, conversation_id, payload.title))

        return idempotent(
            actor, "conversations.rename", idempotency_key,
            {"conversation_id": conversation_id, "body": payload.model_dump(mode="json")}, _run,
        )

    @router.post("/{conversation_id}/archive")
    def archive_conversation(
        conversation_id: str, actor: ConversationActor = Depends(chat_actor),
        idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
    ) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            return conversation_json(chat_store().archive_conversation(actor, conversation_id))

        return idempotent(
            actor, "conversations.archive", idempotency_key, {"conversation_id": conversation_id}, _run,
        )

    @router.post("/{conversation_id}/unarchive")
    def unarchive_conversation(
        conversation_id: str, actor: ConversationActor = Depends(chat_actor),
        idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
    ) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            return conversation_json(chat_store().unarchive_conversation(actor, conversation_id))

        return idempotent(
            actor, "conversations.unarchive", idempotency_key, {"conversation_id": conversation_id}, _run,
        )

    @router.post("/{conversation_id}/delete")
    def delete_conversation(
        conversation_id: str, payload: DeleteInput, actor: ConversationActor = Depends(chat_actor),
        idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
    ) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            return deletion_json(chat_store().delete_conversation(actor, conversation_id, payload.mode))

        return idempotent(
            actor, "conversations.delete", idempotency_key,
            {"conversation_id": conversation_id, "body": payload.model_dump(mode="json")}, _run,
        )

    @router.post("/{conversation_id}/turns", status_code=status.HTTP_201_CREATED)
    def append_turn(
        conversation_id: str, payload: TurnAppendInput, actor: ConversationActor = Depends(chat_actor),
        idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
    ) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            turn = chat_store().append_turn(
                actor, conversation_id,
                role=payload.role, status=payload.status, mode=payload.mode,
                lineage=TurnLineage(payload.lineage.parent_turn_id, payload.lineage.sibling_index, payload.lineage.kind),
                redaction=_redaction(payload.redaction),
                content=_content(payload.content),
                voice_events=_voice_events(payload.voice_events),
            )
            return turn_json(turn)

        return idempotent(
            actor, "conversations.append_turn", idempotency_key,
            {"conversation_id": conversation_id, "body": payload.model_dump(mode="json")}, _run,
        )

    @router.post("/{conversation_id}/turns/{turn_id}/retry", status_code=status.HTTP_201_CREATED)
    def retry_turn(
        conversation_id: str, turn_id: str, payload: TurnBodyInput, actor: ConversationActor = Depends(chat_actor),
        idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
    ) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            turn = chat_store().retry_turn(
                actor, conversation_id, turn_id,
                role=payload.role, status=payload.status, mode=payload.mode,
                redaction=_redaction(payload.redaction),
                content=_content(payload.content),
                voice_events=_voice_events(payload.voice_events),
            )
            return turn_json(turn)

        return idempotent(
            actor, "conversations.retry_turn", idempotency_key,
            {"conversation_id": conversation_id, "turn_id": turn_id, "body": payload.model_dump(mode="json")}, _run,
        )

    @router.post("/{conversation_id}/turns/{turn_id}/branch", status_code=status.HTTP_201_CREATED)
    def branch_turn(
        conversation_id: str, turn_id: str, payload: TurnBodyInput, actor: ConversationActor = Depends(chat_actor),
        idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
    ) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            turn = chat_store().branch_turn(
                actor, conversation_id, turn_id,
                role=payload.role, status=payload.status, mode=payload.mode,
                redaction=_redaction(payload.redaction),
                content=_content(payload.content),
                voice_events=_voice_events(payload.voice_events),
            )
            return turn_json(turn)

        return idempotent(
            actor, "conversations.branch_turn", idempotency_key,
            {"conversation_id": conversation_id, "turn_id": turn_id, "body": payload.model_dump(mode="json")}, _run,
        )

    @router.post("/{conversation_id}/turns/{turn_id}/status")
    def advance_turn_status(
        conversation_id: str, turn_id: str, payload: TurnStatusInput, actor: ConversationActor = Depends(chat_actor),
        idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
    ) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            return turn_json(chat_store().advance_turn_status(actor, conversation_id, turn_id, payload.status))

        return idempotent(
            actor, "conversations.advance_turn_status", idempotency_key,
            {"conversation_id": conversation_id, "turn_id": turn_id, "body": payload.model_dump(mode="json")}, _run,
        )

    return router
