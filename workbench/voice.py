"""Session-bound relay for Anvil Voice Realtime.

The browser talks only to Workbench.  Workbench authenticates the private
upstream with a hub-held token, strips tool/model controls from client events,
and persists only small redacted lifecycle summaries.  Audio payloads and raw
server event bodies are deliberately never written to the Workbench store.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping, Protocol, runtime_checkable

from fastapi import WebSocket, WebSocketDisconnect


class VoiceRelayError(ValueError):
    """The client attempted an unsupported Realtime operation."""


_CLIENT_EVENT_TYPES = frozenset({
    "session.update", "input_audio_buffer.append", "input_audio_buffer.commit",
    "input_audio_buffer.clear", "response.create", "response.cancel",
})
_SESSION_FIELDS = frozenset({
    "modalities", "voice", "input_audio_format", "output_audio_format", "turn_detection",
})
_MAX_AUDIO_EVENT_BYTES = 2_000_000


def sanitize_client_event(raw: str) -> dict[str, Any]:
    """Allow only push-to-talk Realtime controls, never model-selected tools."""
    try:
        event = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VoiceRelayError("voice event must be JSON") from exc
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise VoiceRelayError("voice event requires a type")
    event_type = event["type"]
    if event_type not in _CLIENT_EVENT_TYPES:
        raise VoiceRelayError("voice event type is not allowed")
    if event_type == "session.update":
        session = event.get("session")
        if not isinstance(session, dict):
            raise VoiceRelayError("session.update requires a session object")
        return {"type": event_type, "session": {key: value for key, value in session.items() if key in _SESSION_FIELDS}}
    if event_type == "input_audio_buffer.append":
        audio = event.get("audio")
        if not isinstance(audio, str) or not audio:
            raise VoiceRelayError("audio append requires an audio string")
        if len(audio) > _MAX_AUDIO_EVENT_BYTES:
            raise VoiceRelayError("audio chunk exceeds the Workbench voice limit")
        return {"type": event_type, "audio": audio}
    if event_type == "response.create":
        # Audio is supplied through the input buffer.  Do not let a browser
        # inject arbitrary tools, model routes, or independent text prompts.
        return {"type": event_type, "response": {"modalities": ["audio", "text"]}}
    return {"type": event_type}


def summarize_server_event(raw: str, retain_transcripts: bool = False) -> tuple[str, dict[str, Any]] | None:
    """Produce the only Realtime data eligible for durable session storage."""
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None
    event_type = event.get("type")
    if event_type == "input_audio_buffer.speech_started":
        return "voice.started", {}
    if event_type == "conversation.item.input_audio_transcription.delta":
        delta = event.get("delta")
        data: dict[str, Any] = {"characters": len(delta) if isinstance(delta, str) else 0}
        if retain_transcripts and isinstance(delta, str):
            data["transcript"] = delta
        return "voice.utterance.partial", data
    if event_type == "conversation.item.input_audio_transcription.completed":
        transcript = event.get("transcript")
        data = {"characters": len(transcript) if isinstance(transcript, str) else 0}
        if retain_transcripts and isinstance(transcript, str):
            data["transcript"] = transcript
        return "voice.utterance.final", data
    if event_type == "response.output_audio_transcript.delta":
        delta = event.get("delta")
        data = {"characters": len(delta) if isinstance(delta, str) else 0}
        if retain_transcripts and isinstance(delta, str):
            data["transcript"] = delta
        return "voice.response.partial", data
    if event_type == "response.output_audio.delta":
        audio = event.get("delta")
        return "voice.tts.chunk", {"bytes": len(audio) if isinstance(audio, str) else 0}
    if event_type == "response.done":
        return "voice.response.finished", {}
    if event_type in {"response.cancelled", "response.interrupted"}:
        return "voice.generation.interrupted", {}
    if event_type == "error":
        return "voice.error", {"code": str(event.get("error", {}).get("code", "upstream_error")) if isinstance(event.get("error"), dict) else "upstream_error"}
    return None


async def relay_realtime(
    websocket: WebSocket,
    upstream_url: str,
    upstream_token: str,
    on_event: Callable[[str, dict[str, Any]], Awaitable[None]],
    retain_transcripts: bool = False,
) -> None:
    """Relay one authenticated browser voice session to Anvil Voice Realtime."""
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - bundled by uvicorn[standard]
        raise RuntimeError("voice relay requires the WebSocket runtime bundled with uvicorn[standard]") from exc

    await websocket.accept()
    headers = {"Authorization": f"Bearer {upstream_token}"} if upstream_token else None
    try:
        async with websockets.connect(upstream_url, additional_headers=headers, max_size=_MAX_AUDIO_EVENT_BYTES * 2) as upstream:
            await on_event("voice.connected", {})

            async def browser_to_upstream() -> None:
                try:
                    while True:
                        cleaned = sanitize_client_event(await websocket.receive_text())
                        await upstream.send(json.dumps(cleaned, separators=(",", ":")))
                except WebSocketDisconnect:
                    return

            async def upstream_to_browser() -> None:
                async for raw in upstream:
                    if not isinstance(raw, str):
                        continue
                    summary = summarize_server_event(raw, retain_transcripts)
                    if summary is not None:
                        await on_event(*summary)
                    await websocket.send_text(raw)

            tasks = {asyncio.create_task(browser_to_upstream()), asyncio.create_task(upstream_to_browser())}
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            # Cancellation is expected for the sibling task.  A completed
            # task's policy error must reach the explicit browser guard.
            for task in done:
                task.result()
    except VoiceRelayError as exc:
        await websocket.send_json({"type": "error", "error": {"code": "invalid_voice_event", "message": str(exc)}})
        await websocket.close(code=1008)
    except Exception as exc:  # An upstream loss must be visible without leaking its detail to the browser.
        await on_event("voice.error", {"code": type(exc).__name__})
        await websocket.close(code=1011)
    finally:
        await on_event("voice.disconnected", {})


# ---------------------------------------------------------------------------
# Chat-first push-to-talk / read-aloud relay (chat-first-voice T005).
#
# A DISTINCT, request/response STT+TTS surface from the Realtime relay above.
# Its whole security contract: raw input audio and synthesized output audio are
# transient and in-memory only; the ONLY thing eligible for durable storage is a
# content-free typed lifecycle event (state + correlation + counts, never audio
# and never an unsubmitted transcript draft).  STT returns an editable DRAFT and
# creates NO turn; TTS returns transient playback audio and mutates NO message.
# Every relay hop goes through Anvil Serving (the injected transport), never a
# raw provider.
# ---------------------------------------------------------------------------

#: Accepted STT container/codecs and TTS output formats.  Closed allowlists:
#: anything else fails closed at the edge before any transport call.
STT_INPUT_FORMATS = frozenset({"pcm16", "wav", "webm_opus", "ogg_opus", "mp3"})
TTS_OUTPUT_FORMATS = frozenset({"pcm16", "wav", "mp3", "opus"})

#: In-memory ceilings.  Raw input audio is bounded before it is ever decoded or
#: relayed; synthesized audio and the TTS prompt text are bounded too, so a
#: single request can never carry an unbounded blob.
MAX_STT_INPUT_BYTES = 8_000_000        # ~8 MB single push-to-talk utterance
MAX_STT_DURATION_MS = 120_000          # 2 minutes of speech per utterance
MAX_TTS_TEXT_CHARS = 20_000            # mirrors the durable content-text bound
MAX_SYNTH_AUDIO_BYTES = 16_000_000     # ~16 MB of playback audio per request

#: Wall-clock deadline for one Serving relay hop.  A hung upstream is bounded
#: into a typed timeout rather than blocking a request forever.
VOICE_RELAY_TIMEOUT_S = 30.0

#: Concurrent in-flight relay hops allowed PER ACTOR.  A third simultaneous
#: request from the same actor fails closed rather than fanning out unbounded
#: work onto Serving.
MAX_VOICE_CONCURRENCY_PER_ACTOR = 2


def _voice_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class VoiceRequestError(Exception):
    """A voice relay request was refused.  Carries a fixed, non-leaking detail.

    Every subclass pins a stable ``status_code`` and a fixed ``public_detail``
    string.  The detail is a constant — it never interpolates a host, path,
    upstream body, or credential — so an error response can never become a leak
    channel (the security-contract corpus proves this end to end).
    """

    status_code = 400
    public_detail = "voice request was refused"

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or self.public_detail)


class VoiceScopeError(VoiceRequestError):
    """The actor is not authorized for voice, or the chat scope is invalid.

    Raised BEFORE any provider/transport call, so an unauthorized or wrong-scope
    request never reaches Serving (fail closed)."""

    status_code = 403
    public_detail = "voice is not authorized for this actor or conversation"


class VoiceBoundsError(VoiceRequestError):
    """A format, byte-size, duration, or text-length bound was exceeded."""

    status_code = 422
    public_detail = "voice request is outside the accepted bounds"


class VoiceConcurrencyError(VoiceRequestError):
    """The per-actor concurrent-relay ceiling is already in use."""

    status_code = 429
    public_detail = "too many concurrent voice requests for this actor"


class VoiceTimeoutError(VoiceRequestError):
    """The Serving relay hop exceeded its wall-clock deadline."""

    status_code = 504
    public_detail = "voice relay timed out"


class VoiceServingError(VoiceRequestError):
    """Anvil Serving refused or dropped the relay (no provider fallback)."""

    status_code = 502
    public_detail = "voice relay is unavailable"


@dataclass(frozen=True)
class TranscriptDraft:
    """A transient, editable transcript draft.  Carries NO audio.

    ``is_final`` distinguishes an interim caption from the committable final
    draft.  Returning this NEVER creates a turn: the actor reviews and edits the
    text, then submits it through the ordinary turn-append path.
    """

    text: str
    is_final: bool
    duration_ms: int | None = None


@dataclass(frozen=True)
class SynthesizedAudio:
    """Transient playback audio for one message.  NEVER persisted or mutating.

    The bytes are streamed to the browser and dropped; producing them changes no
    message or conversation state.
    """

    audio: bytes
    audio_format: str
    sample_rate: int | None = None


#: The closed set of durable voice lifecycle states.  Input/relay lifecycle plus
#: the typed voice events the chat-turn contract already declares.  There is
#: deliberately no state that could imply an audio payload.
VOICE_LIFECYCLE_STATES = frozenset({
    "utterance_start", "stt_commit", "tts_start", "tts_stop", "interruption", "error",
})


@dataclass(frozen=True)
class VoiceLifecycleEvent:
    """The ONLY voice datum eligible for durable storage: content-free metadata.

    Carries lifecycle state + correlation, and at most bounded COUNTS
    (``transcript_chars``, ``byte_count``, ``duration_ms``).  It has no field
    able to hold audio bytes, a base64/data-URI blob, or transcript-draft text —
    a caller literally cannot attach one.
    """

    conversation_id: str
    actor: str
    state: str
    correlation_id: str
    at: str = field(default_factory=_voice_now)
    duration_ms: int | None = None
    transcript_chars: int | None = None
    byte_count: int | None = None

    def __post_init__(self) -> None:
        if self.state not in VOICE_LIFECYCLE_STATES:
            raise VoiceBoundsError("voice lifecycle state is not allowlisted")
        for name in ("conversation_id", "actor", "correlation_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value) > 256:
                raise VoiceBoundsError("voice lifecycle correlation field is out of bounds")
        for name, ceiling in (
            ("duration_ms", MAX_STT_DURATION_MS),
            ("transcript_chars", MAX_TTS_TEXT_CHARS),
            ("byte_count", MAX_SYNTH_AUDIO_BYTES),
        ):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > ceiling
            ):
                raise VoiceBoundsError(f"voice lifecycle {name} is out of bounds")

    def as_event_data(self) -> dict[str, Any]:
        """The content-free dict persisted as a session/lifecycle event body."""
        data: dict[str, Any] = {"state": self.state, "correlation_id": self.correlation_id, "at": self.at}
        if self.duration_ms is not None:
            data["duration_ms"] = self.duration_ms
        if self.transcript_chars is not None:
            data["transcript_chars"] = self.transcript_chars
        if self.byte_count is not None:
            data["byte_count"] = self.byte_count
        return data


@runtime_checkable
class VoiceEventLog(Protocol):
    """A content-free sink for voice lifecycle events."""

    def record(self, event: VoiceLifecycleEvent) -> None: ...

    def events(self, conversation_id: str) -> tuple[VoiceLifecycleEvent, ...]: ...


class MemoryVoiceEventLog:
    """A hermetic, lock-guarded lifecycle log (the ``MemoryStore`` idiom)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._events: list[VoiceLifecycleEvent] = []

    def record(self, event: VoiceLifecycleEvent) -> None:
        if not isinstance(event, VoiceLifecycleEvent):
            raise VoiceBoundsError("voice event log accepts only VoiceLifecycleEvent")
        with self._lock:
            self._events.append(event)

    def events(self, conversation_id: str) -> tuple[VoiceLifecycleEvent, ...]:
        with self._lock:
            return tuple(e for e in self._events if e.conversation_id == conversation_id)


@runtime_checkable
class VoiceServingTransport(Protocol):
    """The injected Anvil Serving audio path.

    ``transcribe`` maps a bounded STT request to a draft mapping
    (``{text, is_final, duration_ms}``); ``synthesize`` maps a bounded TTS
    request to ``{audio_b64, format, sample_rate}``.  In production this is
    backed by Anvil Serving's declared ``/audio/*`` surface; in tests it is a
    stub.  It imports no raw provider and constructs no endpoint here, so the
    relay is structurally incapable of a provider fallback.
    """

    def transcribe(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def synthesize(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


class ServingVoiceTransport:
    """Production transport: Anvil Serving's declared audio surface only.

    Thin wrapper over :func:`workbench.router.voice_transcribe` /
    :func:`workbench.router.voice_synthesize`, which talk exclusively to the
    operator-configured Serving URL with no provider fallback.
    """

    def __init__(self, base_url: str, token: str, stt_model: str, tts_model: str) -> None:
        self._base_url = base_url
        self._token = token
        self._stt_model = stt_model
        self._tts_model = tts_model

    def transcribe(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        from .router import RouterError, voice_transcribe

        try:
            return voice_transcribe(
                self._base_url, self._token,
                model=self._stt_model,
                audio_b64=str(request["audio_b64"]),
                audio_format=str(request["audio_format"]),
                is_final=bool(request["is_final"]),
            )
        except RouterError as exc:  # a Serving failure, never a provider fallback
            raise VoiceServingError() from exc

    def synthesize(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        from .router import RouterError, voice_synthesize

        try:
            return voice_synthesize(
                self._base_url, self._token,
                model=self._tts_model,
                text=str(request["text"]),
                output_format=str(request["output_format"]),
            )
        except RouterError as exc:
            raise VoiceServingError() from exc


class _ActorConcurrencyGuard:
    """Bounds simultaneous in-flight relay hops per actor, fail-closed.

    A pure counter under one lock: entry admits an actor only while its live
    count is below the ceiling, otherwise it raises immediately (it does NOT
    block/queue, so a flood fails fast).  ``finally`` always releases, so a
    raising hop never leaks a slot.
    """

    def __init__(self, limit: int) -> None:
        self._limit = max(1, int(limit))
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    @contextmanager
    def hold(self, actor: str) -> Iterator[None]:
        with self._lock:
            current = self._counts.get(actor, 0)
            if current >= self._limit:
                raise VoiceConcurrencyError()
            self._counts[actor] = current + 1
        try:
            yield
        finally:
            with self._lock:
                remaining = self._counts.get(actor, 0) - 1
                if remaining <= 0:
                    self._counts.pop(actor, None)
                else:
                    self._counts[actor] = remaining


class VoiceRelayService:
    """STT/TTS relay: authorize, bound, relay through Serving, log content-free.

    Every request order is fixed and fail-closed: (1) authorize actor + chat
    scope BEFORE any provider call; (2) bound format / bytes / duration / text;
    (3) acquire a per-actor concurrency slot; (4) relay one Serving hop under a
    wall-clock timeout; (5) record ONE content-free lifecycle event.  Raw input
    audio and synthesized output audio never touch the event log or any durable
    surface — only counts, state, and correlation do.
    """

    def __init__(
        self,
        transport: VoiceServingTransport,
        *,
        voice_authorized: Callable[[str], bool] | frozenset[str] | set[str],
        scope_authorized: Callable[[str, str], bool],
        event_log: VoiceEventLog | None = None,
        concurrency_limit: int = MAX_VOICE_CONCURRENCY_PER_ACTOR,
        timeout_s: float = VOICE_RELAY_TIMEOUT_S,
    ) -> None:
        if not isinstance(transport, VoiceServingTransport):
            raise VoiceServingError("voice relay requires a VoiceServingTransport")
        self._transport = transport
        if callable(voice_authorized):
            self._voice_authorized = voice_authorized
        else:
            allowed = frozenset(voice_authorized)
            self._voice_authorized = lambda actor: actor in allowed
        self._scope_authorized = scope_authorized
        self._event_log = event_log
        self._guard = _ActorConcurrencyGuard(concurrency_limit)
        self._timeout_s = float(timeout_s)
        # A tiny pool bounds one blocking Serving hop into a real wall-clock
        # deadline without leaking threads across requests.
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="voice-relay")

    # -- gate -----------------------------------------------------------------

    def _authorize(self, actor: str, conversation_id: str) -> None:
        if not isinstance(actor, str) or not actor:
            raise VoiceScopeError()
        if not isinstance(conversation_id, str) or not conversation_id or len(conversation_id) > 256:
            raise VoiceScopeError()
        # Voice authorization AND chat-scope validity are both checked here,
        # BEFORE any transport call, so an invalid scope or unauthorized actor
        # never reaches the provider (fail closed).
        if not self._voice_authorized(actor):
            raise VoiceScopeError()
        if not self._scope_authorized(actor, conversation_id):
            raise VoiceScopeError()

    def _relay(self, call: Callable[[], Mapping[str, Any]]) -> Mapping[str, Any]:
        future = self._executor.submit(call)
        try:
            return future.result(timeout=self._timeout_s)
        except FutureTimeoutError as exc:
            future.cancel()
            raise VoiceTimeoutError() from exc
        except VoiceRequestError:
            raise
        except Exception as exc:  # any transport failure settles through Serving, never a fallback
            raise VoiceServingError() from exc

    def _log(self, event: VoiceLifecycleEvent) -> None:
        if self._event_log is not None:
            self._event_log.record(event)

    # -- STT ------------------------------------------------------------------

    def transcribe(
        self,
        *,
        actor: str,
        conversation_id: str,
        correlation_id: str,
        audio: bytes,
        audio_format: str,
        is_final: bool,
        duration_ms: int | None = None,
    ) -> TranscriptDraft:
        """Relay in-memory audio to an editable draft.  Creates NO turn."""
        self._authorize(actor, conversation_id)
        if not isinstance(audio, (bytes, bytearray)) or len(audio) == 0:
            raise VoiceBoundsError("voice input audio is empty")
        if len(audio) > MAX_STT_INPUT_BYTES:
            raise VoiceBoundsError("voice input audio exceeds the byte ceiling")
        if audio_format not in STT_INPUT_FORMATS:
            raise VoiceBoundsError("voice input format is not accepted")
        if duration_ms is not None and (
            not isinstance(duration_ms, int) or isinstance(duration_ms, bool)
            or duration_ms < 0 or duration_ms > MAX_STT_DURATION_MS
        ):
            raise VoiceBoundsError("voice input duration is out of bounds")
        audio_b64 = base64.b64encode(bytes(audio)).decode("ascii")
        request = {"audio_b64": audio_b64, "audio_format": audio_format, "is_final": bool(is_final)}
        with self._guard.hold(actor):
            result = self._relay(lambda: self._transport.transcribe(request))
        text = result.get("text") if isinstance(result, Mapping) else None
        text = text if isinstance(text, str) else ""
        if len(text) > MAX_TTS_TEXT_CHARS:
            text = text[:MAX_TTS_TEXT_CHARS]
        final = bool(result.get("is_final", is_final)) if isinstance(result, Mapping) else bool(is_final)
        result_duration = result.get("duration_ms") if isinstance(result, Mapping) else None
        if not (isinstance(result_duration, int) and not isinstance(result_duration, bool)):
            result_duration = duration_ms
        # ONE content-free lifecycle event: state + correlation + a CHAR COUNT,
        # never the draft text and never the audio.
        self._log(VoiceLifecycleEvent(
            conversation_id=conversation_id, actor=actor,
            state="stt_commit" if final else "utterance_start",
            correlation_id=correlation_id,
            duration_ms=result_duration if isinstance(result_duration, int) else None,
            transcript_chars=len(text),
        ))
        return TranscriptDraft(text=text, is_final=final, duration_ms=result_duration if isinstance(result_duration, int) else None)

    # -- TTS ------------------------------------------------------------------

    def synthesize(
        self,
        *,
        actor: str,
        conversation_id: str,
        correlation_id: str,
        message_ref: str,
        text: str,
        output_format: str = "mp3",
    ) -> SynthesizedAudio:
        """Relay a message's text to transient playback audio.  Mutates NOTHING."""
        self._authorize(actor, conversation_id)
        if not isinstance(message_ref, str) or not message_ref or len(message_ref) > 256:
            raise VoiceBoundsError("voice message reference is out of bounds")
        if not isinstance(text, str) or not text:
            raise VoiceBoundsError("voice synthesis text is empty")
        if len(text) > MAX_TTS_TEXT_CHARS:
            raise VoiceBoundsError("voice synthesis text exceeds the character ceiling")
        if output_format not in TTS_OUTPUT_FORMATS:
            raise VoiceBoundsError("voice output format is not accepted")
        request = {"text": text, "output_format": output_format}
        with self._guard.hold(actor):
            result = self._relay(lambda: self._transport.synthesize(request))
        audio_b64 = result.get("audio_b64") if isinstance(result, Mapping) else None
        if not isinstance(audio_b64, str) or not audio_b64:
            raise VoiceServingError()
        try:
            audio = base64.b64decode(audio_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise VoiceServingError() from exc
        if not audio or len(audio) > MAX_SYNTH_AUDIO_BYTES:
            raise VoiceServingError()
        fmt = result.get("format")
        fmt = fmt if isinstance(fmt, str) and fmt in TTS_OUTPUT_FORMATS else output_format
        sample_rate = result.get("sample_rate")
        sample_rate = sample_rate if isinstance(sample_rate, int) and not isinstance(sample_rate, bool) else None
        # ONE content-free lifecycle event: playback started, with a BYTE COUNT,
        # never the audio itself.  No message or conversation state is touched.
        self._log(VoiceLifecycleEvent(
            conversation_id=conversation_id, actor=actor, state="tts_start",
            correlation_id=correlation_id, byte_count=len(audio), transcript_chars=len(text),
        ))
        return SynthesizedAudio(audio=audio, audio_format=fmt, sample_rate=sample_rate)
