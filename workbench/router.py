"""Narrow server-side Anvil Serving reads and sandbox requests.

The browser never receives the router token.  This module intentionally talks
only to an operator-configured Anvil Serving URL; it has no provider fallback.
"""
from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .redaction import redact_config_text, redact_value


class RouterError(RuntimeError):
    """Anvil Serving could not complete a bounded Workbench request."""


def _request(base_url: str, token: str, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    if not base_url or not token:
        raise RouterError("Anvil Serving route access is not configured")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    body = None
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(base_url.rstrip("/") + path, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:  # nosec B310: operator-configured tailnet router
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RouterError(f"Anvil Serving rejected the request ({exc.code}): {detail}") from exc
    except URLError as exc:
        raise RouterError(f"Anvil Serving is unreachable: {exc.reason}") from exc
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise RouterError("Anvil Serving returned an invalid JSON response") from exc


def route_decisions(base_url: str, token: str, limit: int = 50) -> list[dict[str, Any]]:
    """Return only useful correlation metadata from the router decision log."""
    value = _request(base_url, token, "GET", f"/decisions?limit={max(1, min(limit, 100))}")
    rows = value.get("records", value.get("decisions", value)) if isinstance(value, dict) else value
    if not isinstance(rows, list):
        raise RouterError("Anvil Serving decisions response has an unexpected shape")
    allowed = {
        "request_id", "workbench_run_id", "task_id", "model", "served_model", "route",
        "tier", "profile", "reason", "created_at", "timestamp", "status", "fallback",
        "intent", "work_class", "served_tier", "fell_back",
        # Serving-supplied SAFE route-resolution metadata (chat-first-voice:T010):
        # what the request asked for, what Serving actually resolved to, how the
        # route was chosen, and a stable per-episode grouping id.  These are
        # REPORTED by Serving; Workbench never sets them.
        "requested_route", "served_route", "requested_model",
        "route_selection", "route_source", "divergence_reason", "episode_id",
        "correlation_id",
    }
    return [{key: redact_value(row[key]) for key in allowed if key in row} for row in rows if isinstance(row, dict)]


#: The two provenance values a route-resolution mark distinguishes: a route the
#: caller EXPLICITLY selected, versus one DEFAULTED from a stored preference.  A
#: decision that reports neither is surfaced with ``provenance = None`` — a mark
#: is never invented.
_ROUTE_SELECTION_VALUES = frozenset({"explicit", "preference_default"})


def _first_str(decision: Any, *keys: str) -> str | None:
    """The first present, non-empty STRING among ``keys`` (Serving-supplied only)."""
    if not isinstance(decision, dict):
        return None
    for key in keys:
        value = decision.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def route_resolution(decision: Any) -> dict[str, Any]:
    """Derive a truthful route-resolution mark from Serving-supplied safe metadata.

    SURFACE-ONLY, and deliberately so (chat-first-voice:T010 / AGENTS.md: Anvil
    Serving owns model policy — Workbench adds NO provider fallback).  This
    function READS the requested-vs-served route and the selection provenance that
    Serving reported and reports them back; it performs NO failover and NO
    retry-to-alternate-route — it never picks a substitute route, so ``served_route``
    is exactly the route Serving resolved, never a Workbench-chosen alternate.

    * ``requested_route`` / ``served_route`` come only from the decision's own
      fields; a mark is never fabricated from a route Workbench selected.
    * ``diverged`` is true when Serving reported a fallback (``fell_back``) or when
      the served route differs from the requested route it reported.
    * ``provenance`` distinguishes an EXPLICIT selection from a PREFERENCE-DEFAULTED
      one, taken from Serving's reported ``route_selection`` / ``route_source``;
      an unreported provenance stays ``None`` rather than being guessed.
    * ``episode_id`` groups one divergence episode so the browser can show the
      notice exactly once.  It is Serving's OWN episode/correlation id when
      reported; otherwise a stable key derived from the STABLE, non-free-text
      ``(requested_route, served_route, fell_back)`` shape — NEVER the free-text
      reason.  Keying off stable fields (not the reason) does two things: it keeps
      an unscrubbed reason (a credential a future direct caller passed in raw)
      from ever riding out through ``episode_id`` while ``divergence_reason`` is
      the visibly-scrubbed field, AND it makes the key identical whether or not
      Serving reported a reason on a given turn, so a re-announcement of the same
      episode can never slip through with a different key.  It is never a re-route.
    """
    requested = _first_str(decision, "requested_route", "route", "requested_model", "model")
    served = _first_str(decision, "served_route", "served_model", "model")
    provenance = _first_str(decision, "route_selection", "route_source")
    if provenance not in _ROUTE_SELECTION_VALUES:
        provenance = None
    fell_back = bool(decision.get("fell_back")) if isinstance(decision, dict) else False
    diverged = fell_back or (requested is not None and served is not None and requested != served)
    reason = _first_str(decision, "divergence_reason", "reason") if diverged else None
    # Serving's own episode/correlation id wins; both are stable, non-free-text ids.
    episode = _first_str(decision, "episode_id", "correlation_id")
    if diverged and episode is None:
        # A STABLE grouping key derived ONLY from stable, non-free-text fields —
        # NOT a route choice and NEVER the free-text reason.  Two turns of the same
        # episode share it even when Serving reports the reason on one turn and not
        # the other, so the browser shows the divergence notice exactly once; and a
        # credential smuggled into a raw reason can never leak through this key.
        episode = "ep:" + "|".join(str(part) for part in (requested, served, fell_back))
    return {
        "request_id": _first_str(decision, "request_id"),
        "requested_route": requested,
        "served_route": served,
        "provenance": provenance,
        "diverged": diverged,
        # Endpoint/path/host-scrub the reason at the SAME last-hop strength as the
        # rest of the config corpus (covers a dotless ``serving:8443`` host:port, a
        # provider URL, and a local path) — not just the credential scrub — so a
        # reason naming a provider host can never reach the browser.
        "divergence_reason": redact_config_text(reason) if isinstance(reason, str) else None,
        "episode_id": episode,
    }


#: Anvil Serving's declared audio surface.  Serving is the ONLY managed model
#: path and owns model policy (AGENTS.md); these two operator-configured
#: endpoints are the only places the voice relay reaches for STT/TTS.  There is
#: deliberately no provider fallback and no raw-provider path (no external
#: provider host, no provider API key): a Serving failure settles as a
#: :class:`RouterError`, never a retry against another provider.
_SERVING_TRANSCRIBE_PATH = "/audio/transcriptions"
_SERVING_SPEECH_PATH = "/audio/speech"

#: Hard ceilings on what a Serving audio response may hand back, so a
#: misbehaving upstream cannot smuggle an unbounded transcript or audio blob
#: through the relay.  These mirror the durable content-text bound and the
#: in-memory audio ceilings the relay service enforces on the way in.
_MAX_TRANSCRIPT_CHARS = 20_000
_MAX_SYNTH_AUDIO_B64 = 24_000_000


def voice_transcribe(
    base_url: str,
    token: str,
    *,
    model: str,
    audio_b64: str,
    audio_format: str,
    is_final: bool,
) -> dict[str, Any]:
    """Transcribe one in-memory audio chunk through Anvil Serving's STT surface.

    The audio is relayed to Serving's declared ``/audio/transcriptions`` endpoint
    only; the returned draft transcript is credential-scrubbed and bounded.  This
    function persists nothing and returns no audio — it is a transient draft used
    to seed an editable composer, never a committed turn.
    """
    response = _request(base_url, token, "POST", _SERVING_TRANSCRIBE_PATH, {
        "model": model,
        "audio": audio_b64,
        "format": audio_format,
        "mode": "final" if is_final else "interim",
    })
    if not isinstance(response, dict):
        raise RouterError("Anvil Serving transcription result has an unexpected shape")
    text = response.get("text")
    if not isinstance(text, str):
        text = ""
    duration = response.get("duration_ms")
    return {
        # Scrub the draft the same way every retained transcript is scrubbed, so a
        # credential the speaker uttered never rides the draft back to the browser.
        "text": redact_value(text[:_MAX_TRANSCRIPT_CHARS]),
        "is_final": bool(response.get("is_final", is_final)),
        "duration_ms": duration if isinstance(duration, int) and not isinstance(duration, bool) else None,
    }


def voice_synthesize(
    base_url: str,
    token: str,
    *,
    model: str,
    text: str,
    output_format: str,
) -> dict[str, Any]:
    """Synthesize playable audio for a message's text through Serving's TTS surface.

    The text is relayed to Serving's declared ``/audio/speech`` endpoint only.
    The returned audio is transient playback bytes (base64) the caller streams to
    the browser and never persists; this function mutates no message state.
    """
    response = _request(base_url, token, "POST", _SERVING_SPEECH_PATH, {
        "model": model,
        "input": text,
        "format": output_format,
    })
    if not isinstance(response, dict):
        raise RouterError("Anvil Serving speech result has an unexpected shape")
    audio_b64 = response.get("audio")
    if not isinstance(audio_b64, str) or not audio_b64:
        raise RouterError("Anvil Serving speech result carried no audio")
    if len(audio_b64) > _MAX_SYNTH_AUDIO_B64:
        raise RouterError("Anvil Serving speech result exceeds the audio ceiling")
    fmt = response.get("format")
    sample_rate = response.get("sample_rate")
    return {
        "audio_b64": audio_b64,
        "format": str(fmt) if isinstance(fmt, str) and fmt else output_format,
        "sample_rate": sample_rate if isinstance(sample_rate, int) and not isinstance(sample_rate, bool) else None,
    }


def sandbox_response(base_url: str, token: str, model: str, text: str) -> dict[str, Any]:
    """Use the Responses contract through Serving with deliberately small limits."""
    response = _request(base_url, token, "POST", "/responses", {
        "model": model,
        "input": text,
        "max_output_tokens": 400,
        "stream": False,
    })
    if not isinstance(response, dict):
        raise RouterError("Anvil Serving Responses result has an unexpected shape")
    output_text = response.get("output_text")
    if not isinstance(output_text, str):
        output_text = ""
    if not output_text:
        fragments: list[str] = []
        output = response.get("output", [])
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content", [])
                if not isinstance(content, list):
                    continue
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                        fragments.append(part["text"])
        output_text = "\n".join(fragments)
    return {
        "id": str(response.get("id", ""))[:200],
        "model": str(response.get("model", model))[:240],
        "status": str(response.get("status", "completed"))[:80],
        "output_text": redact_value(output_text[:12_000]),
    }
