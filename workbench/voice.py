"""Session-bound relay for Anvil Voice Realtime.

The browser talks only to Workbench.  Workbench authenticates the private
upstream with a hub-held token, strips tool/model controls from client events,
and persists only small redacted lifecycle summaries.  Audio payloads and raw
server event bodies are deliberately never written to the Workbench store.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

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
