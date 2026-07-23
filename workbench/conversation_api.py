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
import itertools
import json
import threading
from datetime import datetime
from typing import Any, AsyncIterator, Callable

import anyio
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
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
    RetentionPreview,
    Turn,
    TurnLineage,
    TurnRedaction,
    VoiceEvent,
    is_metadata_only,
)
from .conversation_store import (
    ConversationStore,
    ConversationStoreError,
    UnknownConversationError,
)
from .chat_routes import (
    ChatRouteError,
    ChatRouteSelection,
    DiscoveredChatRoutes,
    validate_chat_route_selection,
)
from .advanced_routes import (
    AdvancedRouteError,
    DiscoveredAdvancedRoutes,
    validate_advanced_selection,
)
from .advanced_runtime import (
    AdvancedTurnResult,
    stream_advanced_attempt,
    wire_outcome_for_state,
)
from .chat_stream import (
    CancellationToken,
    ChatStreamError,
    ChatStreamRelay,
    ServingStreamTransport,
    StreamOutcome,
    build_bounded_request,
)
from .idempotency_store import IdempotencyStore, MemoryIdempotencyStore, request_hash_for
from .models import new_id
from .redaction import redact_text
from .response_lifecycle_store import (
    IN_PROGRESS_STATE,
    LIFECYCLE_STATE_FOR_OUTCOME,
    ResponseLifecycleError,
    ResponseLifecycleStore,
)
from .stream_sequence import sequence_events

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


#: A safe organization label: the same bounded token the ``Conversation`` model
#: pins, mirrored here so an unsafe tag/folder is refused at the edge as 422
#: (never reaching turn content or a Serving request).
_ORG_TOKEN_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"


class TagInput(_ChatInput):
    tag: str = Field(pattern=_ORG_TOKEN_PATTERN)


class FolderInput(_ChatInput):
    folder: str = Field(pattern=_ORG_TOKEN_PATTERN)


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


class SendMessageInput(_ChatInput):
    """The browser's live send/stream body (``web/src/api.js`` ``sendMessage``).

    A closed field set matching exactly what the client posts:
    ``{route_id, route_selection, prompt, controls}``.  ``route_selection`` is the
    provenance the client records (``explicit`` / ``preference_default``) so Serving
    can echo it on a future resolution mark; it selects no route and is bounded but
    otherwise advisory here.  ``controls`` is validated fail-closed against the
    selected route's declared controls by ``validate_chat_route_selection`` before
    any Serving call, so an undeclared or out-of-range control is refused typed.
    """

    route_id: str = Field(min_length=1, max_length=128)
    prompt: str = Field(min_length=1, max_length=MAX_CONTENT_TEXT_CHARS)
    controls: dict[str, Any] = Field(default_factory=dict)
    route_selection: str | None = Field(default=None, max_length=64)


class AdvancedRunInput(_ChatInput):
    """The browser's advanced "Run branch" body (``web/src/api.js`` ``runAdvancedBranch``).

    A closed field set matching exactly what the client posts: the existing
    ``parent_turn_id`` to fork under, the client-local ``branch_id`` (echoed back on
    the terminal frame), the reviewed ``route_id`` and its tuned ``controls`` (a
    ``{name: value}`` mapping or a ``submitted_controls`` array -- both accepted and
    validated fail-closed by ``validate_advanced_selection`` before any Serving call),
    the bounded ``prompt``, an optional separate ``instructions`` system prompt (R003),
    and the ``structured_output_mode`` (default ``text``).  ``controls`` is typed
    ``Any`` so either submitted shape passes the edge and the fail-closed route
    validator -- not this model -- is the single allowlist authority.
    """

    parent_turn_id: str = Field(min_length=1, max_length=133)
    branch_id: str = Field(min_length=1, max_length=128)
    route_id: str = Field(min_length=1, max_length=128)
    controls: Any = Field(default_factory=dict)
    prompt: str = Field(min_length=1, max_length=MAX_CONTENT_TEXT_CHARS)
    instructions: str | None = Field(default=None, max_length=MAX_CONTENT_TEXT_CHARS)
    structured_output_mode: str = Field(default="text", pattern=r"^(text|json_schema)$")


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
        # Organization metadata (chat-first-voice T011): the actor's pin/tags/
        # folder labels for the conversation list.  Safe tokens only, never turn
        # content, and never part of a Serving request.
        "pinned": record.pinned,
        "tags": list(record.tags),
        "folder": record.folder,
        # Truthful ephemeral badge: computed from the durable retention policy
        # (both content kinds metadata_only), never an asserted label, so the
        # badge a browser rail renders always reflects the real policy.
        "ephemeral": is_metadata_only(record.retention),
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


def preview_json(preview: RetentionPreview) -> dict[str, Any]:
    """One content-free retention-preview row: ids, counts, and timestamps only.

    Deliberately carries no title and no message content of any kind — the
    preview is a scope review of the off-read-path batched pass, never a read
    of conversation content.
    """
    return {
        "conversation_id": preview.conversation_id,
        "reason": preview.reason,
        "turn_count": preview.turn_count,
        "committed_turn_count": preview.committed_turn_count,
        "interrupted_turn_count": preview.interrupted_turn_count,
        "created_at": _iso(preview.created_at),
        "updated_at": _iso(preview.updated_at),
        "delete_after": _iso(preview.delete_after),
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

    @router.post("/ephemeral", status_code=status.HTTP_201_CREATED)
    def create_ephemeral_conversation(
        actor: ConversationActor = Depends(chat_actor),
        idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
    ) -> dict[str, Any]:
        """One action → a metadata_only conversation whose badge is the true policy.

        A single POST creates the ephemeral conversation (both content kinds
        ``metadata_only``); the response's ``ephemeral`` badge is computed from
        that durable policy, so it cannot disagree with what was actually
        persisted.  Takes no body — the retention policy is fixed by the
        affordance, not caller-supplied.
        """
        def _run() -> dict[str, Any]:
            return conversation_json(chat_store().create_ephemeral_conversation(actor))

        return idempotent(actor, "conversations.create_ephemeral", idempotency_key, {}, _run)

    @router.get("")
    def list_conversations(
        include_archived: bool = False,
        pinned: bool | None = Query(default=None),
        tag: str | None = Query(default=None, pattern=_ORG_TOKEN_PATTERN),
        folder: str | None = Query(default=None, pattern=_ORG_TOKEN_PATTERN),
        actor: ConversationActor = Depends(chat_actor),
    ) -> dict[str, Any]:
        records = chat_store().list_conversations(
            actor, include_archived=include_archived, pinned=pinned, tag=tag, folder=folder,
        )
        return {"conversations": [conversation_json(record) for record in records]}

    @router.get("/search")
    def search_conversations(
        query: str = Query(min_length=1, max_length=MAX_TITLE_CHARS),
        include_archived: bool = False,
        pinned: bool | None = Query(default=None),
        tag: str | None = Query(default=None, pattern=_ORG_TOKEN_PATTERN),
        folder: str | None = Query(default=None, pattern=_ORG_TOKEN_PATTERN),
        actor: ConversationActor = Depends(chat_actor),
    ) -> dict[str, Any]:
        records = chat_store().search_conversations(
            actor, query, include_archived=include_archived, pinned=pinned, tag=tag, folder=folder,
        )
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

    # -- organization metadata (pin / tags / folder) ----------------------
    # Thin endpoints over the store's actor-scoped mutations; each returns the
    # updated conversation projection (with pinned/tags/folder) and honours an
    # optional idempotency key like every side-effecting endpoint.

    @router.post("/{conversation_id}/pin")
    def pin_conversation(
        conversation_id: str, actor: ConversationActor = Depends(chat_actor),
        idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
    ) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            return conversation_json(chat_store().pin_conversation(actor, conversation_id))

        return idempotent(
            actor, "conversations.pin", idempotency_key, {"conversation_id": conversation_id}, _run,
        )

    @router.post("/{conversation_id}/unpin")
    def unpin_conversation(
        conversation_id: str, actor: ConversationActor = Depends(chat_actor),
        idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
    ) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            return conversation_json(chat_store().unpin_conversation(actor, conversation_id))

        return idempotent(
            actor, "conversations.unpin", idempotency_key, {"conversation_id": conversation_id}, _run,
        )

    @router.post("/{conversation_id}/tags")
    def add_conversation_tag(
        conversation_id: str, payload: TagInput, actor: ConversationActor = Depends(chat_actor),
        idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
    ) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            return conversation_json(chat_store().add_conversation_tag(actor, conversation_id, payload.tag))

        return idempotent(
            actor, "conversations.add_tag", idempotency_key,
            {"conversation_id": conversation_id, "body": payload.model_dump(mode="json")}, _run,
        )

    @router.post("/{conversation_id}/tags/remove")
    def remove_conversation_tag(
        conversation_id: str, payload: TagInput, actor: ConversationActor = Depends(chat_actor),
        idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
    ) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            return conversation_json(chat_store().remove_conversation_tag(actor, conversation_id, payload.tag))

        return idempotent(
            actor, "conversations.remove_tag", idempotency_key,
            {"conversation_id": conversation_id, "body": payload.model_dump(mode="json")}, _run,
        )

    @router.post("/{conversation_id}/folder")
    def set_conversation_folder(
        conversation_id: str, payload: FolderInput, actor: ConversationActor = Depends(chat_actor),
        idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
    ) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            return conversation_json(chat_store().set_conversation_folder(actor, conversation_id, payload.folder))

        return idempotent(
            actor, "conversations.set_folder", idempotency_key,
            {"conversation_id": conversation_id, "body": payload.model_dump(mode="json")}, _run,
        )

    @router.post("/{conversation_id}/folder/clear")
    def clear_conversation_folder(
        conversation_id: str, actor: ConversationActor = Depends(chat_actor),
        idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
    ) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            return conversation_json(chat_store().clear_conversation_folder(actor, conversation_id))

        return idempotent(
            actor, "conversations.clear_folder", idempotency_key, {"conversation_id": conversation_id}, _run,
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


def build_hub_retention_router(
    owner_dependency: Callable[..., str],
    conversation_store: ConversationStore | None,
) -> APIRouter:
    """Operator-only hub surface for the OFF-READ-PATH batched retention pass.

    These are the store's HUB-INTERNAL retention operations, wired ONLY behind
    the hub's ``owner`` (operator) dependency and mounted OFF the actor
    conversation surface (``/api/hub/retention`` — never ``/api/conversations``,
    which by design exposes no retention/enforce/audit path).  A single actor
    must never be able to preview across, or trigger deletion of, another
    actor's records; only the hub operator can.

    * ``GET /api/hub/retention/preview`` returns the content-free scope of the
      next pass (ids, counts, timestamps only) WITHOUT deleting anything — a
      read here never initiates expiry.
    * ``POST /api/hub/retention/enforce`` runs the explicit batched pass; this
      is the only actor/HTTP path that initiates retention-expiry deletion.

    When ``conversation_store`` is ``None`` the hub has no content-hash key and
    both endpoints refuse with 503, matching the actor chat surface.
    """
    router = APIRouter(prefix="/api/hub/retention")

    def chat_store() -> ConversationStore:
        if conversation_store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="chat persistence is not configured; set WORKBENCH_CHAT_HASH_KEY",
            )
        return conversation_store

    @router.get("/preview")
    def retention_preview(_: str = Depends(owner_dependency)) -> dict[str, Any]:
        previews = chat_store().retention_preview()
        return {"preview": [preview_json(item) for item in previews]}

    @router.post("/enforce")
    def enforce_retention(_: str = Depends(owner_dependency)) -> dict[str, Any]:
        enforced = chat_store().enforce_retention()
        return {"enforced": [deletion_json(item) for item in enforced]}

    return router


#: The redaction posture every send-persisted turn carries.  ``redacted`` matches
#: the credential scrub applied to the durable content on the way in.
_SEND_REDACTION = TurnRedaction("redacted", "workbench.default")

#: Bounded conversation history included in the Serving request so a follow-up
#: turn carries prior context (D1).  No reviewed chat contract pins a history
#: bound, so this mirrors the voice manifest precedent (``history_max_turns=8``)
#: plus a total char budget; only ``complete`` user/assistant turns are included,
#: oldest dropped first so the most recent context always survives.
HISTORY_MAX_TURNS = 8
HISTORY_MAX_CHARS = 16_000

#: Per-actor concurrent live-stream ceiling (G7), mirroring the voice relay's
#: per-actor concurrency bound: each stream can hold a Serving connection for up
#: to the read timeout, so one actor cannot open unbounded concurrent streams.
MAX_CONCURRENT_STREAMS_PER_ACTOR = 2

#: Sentinel a threadpool frame-pull returns when the relay is exhausted.
_PULL_DONE = object()

#: The three non-completed relay terminals, and their chat-turn.v1 statuses.  A
#: settled-but-not-``complete`` terminal maps truthfully here; anything else that
#: reaches the disconnect settle path is partial and can never be ``complete``.
_NONCOMPLETE_TERMINALS = frozenset({
    StreamOutcome.cancelled, StreamOutcome.timed_out, StreamOutcome.serving_unavailable,
})


def _send_lineage(turns: tuple[Turn, ...] | list[Turn], parent_id: str | None) -> TurnLineage:
    """Build a valid append lineage under ``parent_id`` (``None`` for the root).

    A straight conversation is a linear chain: the first turn is the single
    null-parent root and every later turn hangs off the previous tip.  Every turn
    uses kind ``initial`` (a linear reply is neither a retry nor a branch), so the
    browser (``web/src/App.jsx`` renders a lineage chip only when ``kind !==
    'initial'``) shows a plain turn -- no spurious BRANCH label (D3).  A non-root
    ``initial`` turn is valid: only ``retry``/``branch`` require a named parent, and
    ``validate_turn_append``'s single-root rule constrains only null-parent turns.
    """
    taken = [t.lineage.sibling_index for t in turns if t.lineage.parent_turn_id == parent_id]
    sibling_index = (max(taken) + 1) if taken else 0
    return TurnLineage(parent_id, sibling_index, "initial")


def _history_messages(turns: tuple[Turn, ...] | list[Turn]) -> list[dict[str, str]]:
    """Bounded prior-turn context for the Serving request (D1).

    Only ``complete`` user/assistant turns (never a failed/cancelled/streaming
    partial, never a purged tombstone) are included, in order, as
    ``{role, content}`` messages -- reading the already-redacted durable content.
    Bounded to the last :data:`HISTORY_MAX_TURNS` turns, then trimmed oldest-first
    until under :data:`HISTORY_MAX_CHARS`, so a long history can never blow the
    request size and the most recent context always survives.
    """
    messages: list[dict[str, str]] = []
    for turn in turns:
        if turn.status != "complete" or turn.content_purged or turn.role not in ("user", "assistant"):
            continue
        text = "".join(block.text for block in turn.content)
        if not text:
            continue
        messages.append({"role": turn.role, "content": text})
    messages = messages[-HISTORY_MAX_TURNS:]
    while messages and sum(len(message["content"]) for message in messages) > HISTORY_MAX_CHARS:
        messages.pop(0)
    return messages


def _content_blocks(text: str) -> tuple[ContentBlock, ...]:
    """Split streamed text into bounded ContentBlocks (G6).

    The relay accumulates up to ``chat_stream.MAX_OUTPUT_CHARS`` (100k), which
    exceeds one ContentBlock's ``MAX_CONTENT_TEXT_CHARS`` (20k), so a long
    completion is chunked across blocks (bounded by ``MAX_CONTENT_BLOCKS``) instead
    of raising an over-length store error mid-persist -- which would abort the
    stream after the lifecycle already advanced, leaving the terminal diverged from
    zero durable turns.
    """
    if not text:
        return ()
    chunks = [
        text[i:i + MAX_CONTENT_TEXT_CHARS]
        for i in range(0, len(text), MAX_CONTENT_TEXT_CHARS)
    ][:MAX_CONTENT_BLOCKS]
    return tuple(ContentBlock("text", chunk) for chunk in chunks)


def _partial_settlement(relay: ChatStreamRelay) -> tuple[StreamOutcome, str]:
    """The (outcome, chat-turn.v1 status) for a stream torn down before a terminal.

    NEVER ``complete`` (D4): the client disconnected before the relay delivered a
    terminal, so the accumulated text is partial and must settle
    cancelled/interrupted/failed.  A relay that DID settle a non-completed terminal
    (cancelled/timed_out/serving_unavailable) is mapped truthfully; anything else
    (still streaming, or a completion we never delivered) settles ``cancelled``.
    """
    outcome = relay.outcome
    if outcome in _NONCOMPLETE_TERMINALS:
        return outcome, relay.terminal_turn_status()
    return StreamOutcome.cancelled, "cancelled"


def build_chat_send_router(
    actor_dependency: Callable[..., str],
    conversation_store: ConversationStore | None,
    lifecycle_store: ResponseLifecycleStore | None,
    routes_provider: Callable[[], DiscoveredChatRoutes],
    transport_factory: Callable[[ChatRouteSelection], ServingStreamTransport],
) -> APIRouter:
    """Mount ``POST /api/conversations/{id}/send`` -- the live send/stream join.

    This is the production endpoint the browser client already targets
    (``web/src/api.js`` ``sendMessage``): it POSTs
    ``{route_id, route_selection, prompt, controls}`` and reads a newline-delimited
    stream of ``chat_stream.RelayEvent`` frames (``delta`` / ``terminal`` only), each
    stamped with a per-stream ``seq`` that starts at 1 and increases by 1 (the FE
    reducer resets per send, so a first frame above 1 reads as a dropped-frame gap).

    The discipline, in order and fail-closed:

    * The conversation must belong to the acting actor.  A missing or foreign id
      raises ``UnknownConversationError``, rendered as the same fixed 404 as a
      truly missing id (no existence oracle) -- BEFORE any Serving call or write.
    * The ``{route_id, controls}`` selection is validated against the operator-
      discovered routes; an unknown route or undeclared/out-of-range control is a
      typed 422 refusal, strictly BEFORE any Serving call or durable write.
    * A per-actor concurrent-stream ceiling refuses (429) beyond
      :data:`MAX_CONCURRENT_STREAMS_PER_ACTOR`, before the user turn is written.
    * The user turn is persisted durably (credential-scrubbed content); bounded
      prior context is included in the Serving request so a follow-up has history.
    * The assistant response is streamed as NDJSON ``RelayEvent`` frames.  Frame
      pulls run in a threadpool with a per-frame await boundary, so a client
      disconnect cancels the pull (its result discarded) and the stream settles
      via the ``finally`` -- the terminal-complete branch runs ONLY when a terminal
      is actually pulled and processed while the client is connected.  A stream
      torn down before a terminal never persists ``complete``.
    * On the terminal frame the assistant turn is persisted durably FIRST, then the
      lifecycle advances to the terminal, so a persist failure can never leave a
      ``completed`` lifecycle with no durable turn.  A Serving failure settles a
      ``serving_unavailable`` terminal (persisted ``failed``); the router URL/token
      never leak.

    When ``conversation_store`` or ``lifecycle_store`` is ``None`` the endpoint
    refuses with 503, matching the sibling conversation surface.
    """
    router = APIRouter(prefix="/api/conversations")

    # Per-actor active-stream counters (G7), guarded by a lock so the threadpool
    # streaming path and concurrent requests cannot corrupt the count.
    active_streams: dict[str, int] = {}
    active_lock = threading.Lock()

    def _acquire_stream_slot(actor_id: str) -> bool:
        with active_lock:
            if active_streams.get(actor_id, 0) >= MAX_CONCURRENT_STREAMS_PER_ACTOR:
                return False
            active_streams[actor_id] = active_streams.get(actor_id, 0) + 1
            return True

    def _release_stream_slot(actor_id: str) -> None:
        with active_lock:
            remaining = active_streams.get(actor_id, 0) - 1
            if remaining <= 0:
                active_streams.pop(actor_id, None)
            else:
                active_streams[actor_id] = remaining

    def stores() -> tuple[ConversationStore, ResponseLifecycleStore]:
        if conversation_store is None or lifecycle_store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="chat persistence is not configured; set WORKBENCH_CHAT_HASH_KEY",
            )
        return conversation_store, lifecycle_store

    def chat_actor(current_actor: str = Depends(actor_dependency)) -> ConversationActor:
        return conversation_actor(current_actor)

    @router.post("/{conversation_id}/send")
    async def send_message(
        conversation_id: str,
        payload: SendMessageInput,
        actor: ConversationActor = Depends(chat_actor),
    ) -> StreamingResponse:
        store, lifecycle = stores()
        # (1) Ownership: a missing OR foreign id raises UnknownConversationError,
        # rendered as the fixed 404 -- strictly before any write or Serving call.
        _conversation, prior_turns = store.get_conversation_with_turns(actor, conversation_id)

        # (2) Fail-closed route/control validation, strictly before any Serving
        # request or durable write.  Malformed operator config -> 503; an unknown
        # route or undeclared/out-of-range control -> typed 422.
        try:
            discovered = routes_provider()
        except ChatRouteError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="chat routes are not configured",
            ) from exc
        try:
            selection = validate_chat_route_selection(payload.route_id, payload.controls, discovered)
        except ChatRouteError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="chat route selection is not allowed",
            ) from exc

        # (3) Per-actor concurrency ceiling, before any durable write (so a refusal
        # leaves no orphan user turn).
        if not _acquire_stream_slot(actor.actor_id):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="too many concurrent chat streams are active for this actor",
            )

        try:
            # (4) Persist the user turn durably (credential-scrubbed) before the
            # stream opens, so the send is recorded even if the client drops.
            user_turn = store.append_turn(
                actor, conversation_id, role="user", status="complete",
                lineage=_send_lineage(prior_turns, prior_turns[-1].id if prior_turns else None),
                redaction=_SEND_REDACTION,
                content=(ContentBlock("text", redact_text(payload.prompt)),),
            )

            # (5) Assemble the bounded Serving request WITH prior context (D1): the
            # bounded request bounds the prompt+controls, then ``input`` becomes the
            # history messages plus the current user prompt.
            base_request = build_bounded_request(selection, payload.prompt)
            base_request["input"] = (
                _history_messages(prior_turns) + [{"role": "user", "content": payload.prompt}]
            )
            cancel = CancellationToken()
            transport = transport_factory(selection)
            relay = ChatStreamRelay.for_prepared_request(base_request, transport, cancel)
            request_id = new_id("resp")
            lifecycle.begin(actor, conversation_id, request_id)
            # Per-STREAM seq starting at 1 (D2): the FE reducer resets per send, so
            # a first frame above 1 reads as a dropped-frame gap.  The lifecycle
            # per-response record commits these same per-stream seqs (its
            # last_committed_seq starts at 0 for each fresh response).
            stream_seq = itertools.count(1)
            stream_gen = relay.stream()
            seq_gen = sequence_events(stream_gen, lambda: next(stream_seq))
            frame_iter = iter(seq_gen)
        except Exception:
            _release_stream_slot(actor.actor_id)
            raise

        def _persist_assistant(text: str, turn_status: str) -> str:
            turn = store.append_turn(
                actor, conversation_id, role="assistant", status=turn_status,
                lineage=TurnLineage(user_turn.id, 0, "initial"),
                redaction=_SEND_REDACTION, content=_content_blocks(redact_text(text)),
            )
            return turn.id

        def _pull() -> Any:
            try:
                return next(frame_iter)
            except StopIteration:
                return _PULL_DONE

        async def _frames() -> AsyncIterator[bytes]:
            parts: list[str] = []
            settled = False
            try:
                while True:
                    # The raw frame pull runs in a threadpool with
                    # ``abandon_on_cancel``: on a client disconnect the await is
                    # cancelled IMMEDIATELY (the blocked read is abandoned, not
                    # awaited), so the ``finally`` fires and trips ``cancel`` -- the
                    # relay/transport then tear down and no terminal is PROCESSED
                    # after the client is gone (the sync-generator threadpool race
                    # that persisted a partial turn as complete under real uvicorn,
                    # where a non-cancellable pull would have to run to completion
                    # first -- D4/G1).
                    frame = await anyio.to_thread.run_sync(_pull, abandon_on_cancel=True)
                    if frame is _PULL_DONE:
                        break
                    if frame.kind == "delta":
                        lifecycle.advance(actor, request_id, IN_PROGRESS_STATE, seq=frame.seq)
                        parts.append(frame.text)
                        # NOTE (G5/G8): the wire delta is redacted per fragment while
                        # the durable turn redacts the JOINED text below; a secret
                        # split across two deltas is redacted durably but a fragment
                        # may reach the (authenticated, tailnet-only) owner's browser.
                        # A streaming boundary cannot retroactively scrub already-sent
                        # bytes; the durable record is always fully scrubbed.
                        yield _ndjson({"seq": frame.seq, "kind": "delta", "text": redact_text(frame.text)})
                    else:
                        # Persist the assistant turn FIRST, then advance the lifecycle
                        # to the terminal (G6): if the persist fails, the lifecycle is
                        # NOT left ``completed`` with zero durable turns -- it settles
                        # ``interrupted`` and the wire reports serving_unavailable.
                        try:
                            assistant_turn_id = _persist_assistant("".join(parts), relay.terminal_turn_status())
                        except ConversationStoreError:
                            lifecycle.advance(
                                actor, request_id,
                                LIFECYCLE_STATE_FOR_OUTCOME[StreamOutcome.serving_unavailable.value],
                                seq=frame.seq,
                            )
                            settled = True
                            yield _ndjson({
                                "seq": frame.seq, "kind": "terminal",
                                "outcome": StreamOutcome.serving_unavailable.value,
                            })
                            return
                        lifecycle.advance(
                            actor, request_id, LIFECYCLE_STATE_FOR_OUTCOME[frame.outcome.value], seq=frame.seq,
                        )
                        settled = True
                        # Carry the persisted assistant turn_id so the client can
                        # adopt it in place of its optimistic local id -- otherwise a
                        # follow-on fork (Branch/Retry/advanced Run branch) from a
                        # just-sent turn would reference a non-existent local id and
                        # fail closed (404/409).  Mirrors the advanced-run terminal.
                        yield _ndjson({
                            "seq": frame.seq, "kind": "terminal",
                            "outcome": frame.outcome.value, "turn_id": assistant_turn_id,
                        })
            finally:
                if not settled:
                    # Torn down before a terminal (client disconnect / abort).
                    # Cancel the upstream and explicitly close BOTH generators:
                    # closing seq_gen does NOT propagate to relay.stream() (its
                    # for-loop abandons the inner iterator), so close stream_gen too
                    # (G1) -- the relay's own finally then tears down the transport.
                    cancel.cancel()
                    for generator in (seq_gen, stream_gen):
                        try:
                            generator.close()
                        except Exception:  # noqa: BLE001 - a pull may still be executing it; the cancel tears it down
                            pass
                    outcome, turn_status = _partial_settlement(relay)
                    try:
                        lifecycle.advance(
                            actor, request_id, LIFECYCLE_STATE_FOR_OUTCOME[outcome.value], seq=next(stream_seq),
                        )
                    except ResponseLifecycleError:
                        pass  # already terminal (a race settled it); leave it stable
                    try:
                        _persist_assistant("".join(parts), turn_status)
                    except ConversationStoreError:
                        pass  # best-effort; lifecycle already reflects the non-complete terminal
                _release_stream_slot(actor.actor_id)

        return StreamingResponse(_frames(), media_type="application/x-ndjson")

    return router


#: The redaction posture every advanced fork carries: the durable turn holds only a
#: status + the redacted advanced-trace.v1 summary, never raw assistant text (mirrors
#: ``open_advanced_branch`` / ``dispatch_parallel``).
_ADVANCED_REDACTION = TurnRedaction("redacted", "advanced-trace-v1")


def build_advanced_run_router(
    actor_dependency: Callable[..., str],
    conversation_store: ConversationStore | None,
    lifecycle_store: ResponseLifecycleStore | None,
    advanced_routes_provider: Callable[[], DiscoveredAdvancedRoutes],
    transport_factory: Callable[[Any], ServingStreamTransport],
) -> APIRouter:
    """Mount ``POST /api/conversations/{id}/advanced/run`` -- Advanced mode's live run.

    The streaming sibling of ``build_chat_send_router``: the browser client
    (``web/src/api.js`` ``runAdvancedBranch``) POSTs
    ``{parent_turn_id, branch_id, route_id, controls, prompt, instructions?,
    structured_output_mode?}`` and reads the same newline-delimited relay-frame
    stream (``delta`` / ``terminal``), except the terminal additionally carries the
    settled ``turn_id``, the echoed ``branch_id``, and the redacted
    advanced-trace.v1 record for the inspector.

    The discipline mirrors the chat send join, in order and fail-closed:

    * Ownership first: a missing OR foreign conversation id raises
      ``UnknownConversationError``, rendered as the same fixed 404 as a truly
      missing id (no existence oracle) -- BEFORE any Serving call or write.
    * The ``{route_id, controls}`` selection is validated against the operator-
      discovered ADVANCED routes; an unknown route or undeclared/out-of-range
      control is a typed 422, strictly BEFORE any Serving call or durable write.
      A provider that itself fails (unconfigured advanced routes) is a 503.
    * A per-actor concurrent-stream ceiling refuses (429) beyond
      :data:`MAX_CONCURRENT_STREAMS_PER_ACTOR`, before the sibling turn is forked.
    * The advanced attempt is forked as an ordinary streaming ``mode="advanced"``
      sibling under the existing parent (``branch_turn``, exactly as
      ``dispatch_parallel``) -- no advanced-branch.v1 record is synthesized and no
      new conversation identity is minted.  The durable turn carries a status + the
      redacted trace summary only, never raw assistant text.
    * The response is streamed as NDJSON relay frames.  Frame pulls run in a
      threadpool with a per-frame await boundary, so a client disconnect cancels
      the pull and the stream settles the turn ``cancelled`` via the ``finally`` --
      the terminal-complete branch runs ONLY when a terminal is actually reached
      while the client is connected, so a torn-down stream never renders complete.
    * On the settled result the durable turn advances to the state's terminal
      status; the wire terminal carries the reducer-vocabulary ``outcome`` mapped
      from the refined state, so a schema-invalid / malformed / timed-out /
      unavailable attempt is never presented as a clean completion.

    When ``conversation_store`` or ``lifecycle_store`` is ``None`` the endpoint
    refuses with 503, matching the sibling chat send surface.
    """
    router = APIRouter(prefix="/api/conversations")

    active_streams: dict[str, int] = {}
    active_lock = threading.Lock()

    def _acquire_stream_slot(actor_id: str) -> bool:
        with active_lock:
            if active_streams.get(actor_id, 0) >= MAX_CONCURRENT_STREAMS_PER_ACTOR:
                return False
            active_streams[actor_id] = active_streams.get(actor_id, 0) + 1
            return True

    def _release_stream_slot(actor_id: str) -> None:
        with active_lock:
            remaining = active_streams.get(actor_id, 0) - 1
            if remaining <= 0:
                active_streams.pop(actor_id, None)
            else:
                active_streams[actor_id] = remaining

    def stores() -> tuple[ConversationStore, ResponseLifecycleStore]:
        if conversation_store is None or lifecycle_store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="chat persistence is not configured; set WORKBENCH_CHAT_HASH_KEY",
            )
        return conversation_store, lifecycle_store

    def advanced_actor(current_actor: str = Depends(actor_dependency)) -> ConversationActor:
        return conversation_actor(current_actor)

    @router.post("/{conversation_id}/advanced/run")
    async def run_advanced(
        conversation_id: str,
        payload: AdvancedRunInput,
        actor: ConversationActor = Depends(advanced_actor),
    ) -> StreamingResponse:
        store, lifecycle = stores()
        # (1) Ownership: a missing OR foreign id raises UnknownConversationError,
        # rendered as the fixed 404 -- strictly before any write or Serving call.
        store.get_conversation_with_turns(actor, conversation_id)

        # (2) Fail-closed route/control validation, strictly before any Serving
        # request or durable write.  An unconfigured provider -> 503; an unknown
        # route or undeclared/out-of-range control -> typed 422.
        try:
            discovered = advanced_routes_provider()
        except AdvancedRouteError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="advanced routes are not configured",
            ) from exc
        try:
            selection = validate_advanced_selection(payload.route_id, payload.controls, discovered)
        except AdvancedRouteError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="advanced route selection is not allowed",
            ) from exc

        # (3) Per-actor concurrency ceiling, before any durable write (so a refusal
        # leaves no orphan sibling turn).
        if not _acquire_stream_slot(actor.actor_id):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="too many concurrent chat streams are active for this actor",
            )

        try:
            # (4) Fork the advanced attempt as an ordinary streaming mode="advanced"
            # sibling under the existing parent -- exactly as dispatch_parallel; the
            # store proves the parent exists in this conversation and prior turns are
            # untouched.  No advanced-branch.v1 record and no new identity.
            turn = store.branch_turn(
                actor, conversation_id, payload.parent_turn_id,
                role="assistant", status="streaming", redaction=_ADVANCED_REDACTION, mode="advanced",
            )
            turn_id = turn.id
            cancel = CancellationToken()
            transport = transport_factory(selection)
            request_id = new_id("resp")
            # The AUTHORITATIVE branch id is minted server-side (like
            # dispatch_parallel), never the client's: the durable advanced-trace.v1
            # record pins branch_id to the ``advbranch_`` grammar, so trusting a
            # client-supplied value would fail the trace schema on every completed
            # attempt.  The client's ``payload.branch_id`` is an advisory local
            # label only; the terminal frame returns THIS id and the browser adopts
            # it (web/src/api.js settles ``branchId`` from ``frame.branch_id``).
            branch_id = new_id("advbranch")
            # Per-STREAM wire seq starting at 1 (the FE reducer resets per run, so a
            # first frame above 1 reads as a dropped-frame gap).  The durable
            # lifecycle heartbeat inside stream_advanced_attempt draws its OWN
            # per-conversation seqs; this counter is the browser wire sequence.
            stream_seq = itertools.count(1)
            attempt_gen = stream_advanced_attempt(
                selection=selection, prompt=payload.prompt, transport=transport,
                branch_id=branch_id, conversation_id=conversation_id, turn_id=turn_id,
                lifecycle_store=lifecycle, actor=actor, request_id=request_id,
                instructions=payload.instructions, cancel=cancel,
                structured_output_mode=payload.structured_output_mode,
            )
        except Exception:
            _release_stream_slot(actor.actor_id)
            raise

        result_box: dict[str, AdvancedTurnResult] = {}

        def _pull() -> Any:
            try:
                return next(attempt_gen)
            except StopIteration as stop:
                if isinstance(stop.value, AdvancedTurnResult):
                    result_box["result"] = stop.value
                return _PULL_DONE

        async def _frames() -> AsyncIterator[bytes]:
            settled = False
            try:
                while True:
                    # The raw frame pull runs in a threadpool with
                    # ``abandon_on_cancel``: on a client disconnect the await is
                    # cancelled immediately and the ``finally`` fires, tripping
                    # ``cancel`` so the relay tears down and no terminal is PROCESSED
                    # after the client is gone (the settle-as-complete race D4/G1).
                    frame = await anyio.to_thread.run_sync(_pull, abandon_on_cancel=True)
                    if frame is _PULL_DONE:
                        result = result_box.get("result")
                        if result is None:
                            # The generator ended without a settled result (only
                            # reachable if it was torn down); let the finally settle
                            # the turn cancelled rather than emit a completion.
                            break
                        # Persist the settled terminal turn status FIRST, then emit
                        # the wire terminal carrying the settled ids + redacted trace.
                        store.advance_turn_status(actor, conversation_id, turn_id, result.turn_status)
                        settled = True
                        yield _ndjson({
                            "seq": next(stream_seq), "kind": "terminal",
                            "outcome": wire_outcome_for_state(result.state),
                            "turn_id": turn_id, "branch_id": branch_id,
                            "trace": result.trace,
                        })
                        break
                    # Every yielded frame is a delta RelayEvent (the terminal is
                    # consumed inside stream_advanced_attempt).  The wire delta is
                    # redacted per fragment; the durable trace carries only the
                    # redacted summary, never the raw text.
                    yield _ndjson({
                        "seq": next(stream_seq), "kind": "delta", "text": redact_text(frame.text),
                    })
            finally:
                if not settled:
                    # Torn down before a terminal (client disconnect / abort): trip
                    # the cancel so the relay/transport tear down and close the
                    # generator.  Closing it raises GeneratorExit AT the suspended
                    # ``yield`` inside stream_advanced_attempt, so its own tail settle
                    # never runs -- settle BOTH the durable turn AND the
                    # reconnect-safe lifecycle record here (mirroring send_message),
                    # never a completion after cancel.  A terminal advance takes no
                    # seq (the in_progress->terminal transition is seq-independent),
                    # so it can't be refused as a stale frame.
                    cancel.cancel()
                    try:
                        attempt_gen.close()
                    except Exception:  # noqa: BLE001 - a pull may still execute it; cancel tears it down
                        pass
                    try:
                        lifecycle.advance(actor, request_id, "cancelled")
                    except ResponseLifecycleError:
                        pass  # already terminal (a race settled it); leave it stable
                    try:
                        store.advance_turn_status(actor, conversation_id, turn_id, "cancelled")
                    except ConversationStoreError:
                        pass  # already settled by a race; leave the terminal stable
                _release_stream_slot(actor.actor_id)

        return StreamingResponse(_frames(), media_type="application/x-ndjson")

    return router


def _ndjson(frame: dict[str, Any]) -> bytes:
    """Serialize one relay frame as a newline-delimited JSON line (the FE transport)."""
    return (json.dumps(frame, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
