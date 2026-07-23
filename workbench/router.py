"""Narrow server-side Anvil Serving reads and sandbox requests.

The browser never receives the router token.  This module intentionally talks
only to an operator-configured Anvil Serving URL; it has no provider fallback.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterator, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .redaction import redact_config_text, redact_value


class RouterError(RuntimeError):
    """Anvil Serving could not complete a bounded Workbench request."""


#: Hard ceiling on a single non-streaming router JSON response.  Bounds memory
#: BEFORE the body is fully buffered, so a misbehaving/compromised upstream cannot
#: smuggle a multi-GB blob through ``_request`` -- comfortably above the largest
#: legitimate response (a ~24 MB base64 TTS audio payload, ``_MAX_SYNTH_AUDIO_B64``).
_MAX_RESPONSE_BYTES = 32_000_000


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
            # Bounded read: cap memory at the ceiling+1 BEFORE decoding, so a
            # misbehaving upstream cannot buffer an unbounded body here.
            buffered = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(buffered) > _MAX_RESPONSE_BYTES:
                raise RouterError("Anvil Serving returned an oversized response")
            raw = buffered.decode("utf-8")
    except HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", errors="replace")[:300]
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


# ---------------------------------------------------------------------------
# Backend model-health signals (top-right debug indicator).
#
# Both readers below talk ONLY to the operator-configured Anvil Serving ROUTER
# surface -- the SAME ``ANVIL_ROUTER_BASE_URL`` + ``ANVIL_ROUTER_TOKEN`` every
# other read here uses -- through the bounded, redirect-free :func:`_request`.
# Neither embeds a raw model-serve host or port: the router is the only place the
# hub reaches, so a per-tier status is DERIVED from the router's own decision log,
# never probed against a serve directly (that boundary deviation was removed in
# anvil-serving#280 and would be flagged by the no-raw-provider scan).
# ---------------------------------------------------------------------------

#: The router serves ``/health`` at its ROOT, while the chat/decisions routes live
#: under ``/v1`` (``ANVIL_ROUTER_BASE_URL`` ends in ``/v1``).  Derive the root by
#: dropping a trailing ``/v1`` from the OPERATOR-configured base -- a pure string
#: transform of the configured URL, never a fabricated or embedded host.
def _router_root(base_url: str) -> str:
    trimmed = (base_url or "").rstrip("/")
    if trimmed.endswith("/v1"):
        trimmed = trimmed[: -len("/v1")]
    return trimmed


_ROUTER_HEALTH_PATH = "/health"
_MAX_HEALTH_ROUTES = 64
_MAX_HEALTH_ROUTE_CHARS = 120
_MAX_SIGNAL_RECORDS = 100
_MAX_ATTEMPTS_PER_RECORD = 16
_MAX_SIGNAL_TOKEN_CHARS = 64
_MAX_SIGNAL_REASON_CHARS = 120
_MAX_SIGNAL_COUNT = 1_000_000
#: A safe route/tier/work-class token: the router emits short lowercase-ish path
#: and id tokens (``/v1/audio/speech``, ``heavy-local``, ``chat-fast``); anything
#: longer or shaped like a URL/secret is length-clamped, and the endpoint scrubs
#: the whole payload again at the last hop.
_SIGNAL_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,63}$")


def _safe_token(value: Any) -> str | None:
    """A bounded, safe short token, or ``None`` when the value is not one."""
    if isinstance(value, str) and _SIGNAL_TOKEN.match(value):
        return value[:_MAX_SIGNAL_TOKEN_CHARS]
    return None


#: An RFC3339-ish timestamp is the ONE signal field that legitimately carries
#: ``:``/``+``/``T``/``Z`` (``2026-07-22T10:00:00Z``), so ``_safe_token`` (which
#: excludes ``:``) would drop it -- killing ``last_seen`` and the recency sort.
#: This validator admits only those extra timestamp characters, still length- and
#: shape-bounded; the value is re-scrubbed at the API last hop regardless.
_SIGNAL_TIMESTAMP = re.compile(r"^[0-9][0-9T:.+\-]{0,39}Z?$")


def _safe_timestamp(value: Any) -> str | None:
    """A bounded, safe RFC3339-ish timestamp string, or ``None``."""
    if isinstance(value, str) and _SIGNAL_TIMESTAMP.match(value):
        return value[:_MAX_SIGNAL_TOKEN_CHARS]
    return None


def _safe_count_map(value: Any) -> dict[str, int]:
    """A bounded ``{safe_token: non-negative int}`` projection of a totals map."""
    out: dict[str, int] = {}
    if isinstance(value, dict):
        for key, count in value.items():
            token = _safe_token(key)
            if token is not None and isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= _MAX_SIGNAL_COUNT:
                out[token] = count
    return out


def router_health(base_url: str, token: str) -> dict[str, Any]:
    """Read the router's own ``/health`` (liveness + registered route families).

    Talks only to the operator-configured router root (``base`` minus ``/v1``) via
    the bounded :func:`_request`.  Returns a small, safe projection --
    ``{"status", "routes": [...]}`` -- keeping only the router's up/down status and
    which route families are registered (the ``/v1/audio/*`` presence is the voice
    gateway liveness signal).  A malformed body raises :class:`RouterError` so the
    caller can degrade the router dot to ``down`` honestly.
    """
    value = _request(_router_root(base_url), token, "GET", _ROUTER_HEALTH_PATH)
    if not isinstance(value, dict):
        raise RouterError("Anvil Serving health response has an unexpected shape")
    status = value.get("status")
    routes = value.get("routes")
    safe_routes: list[str] = []
    if isinstance(routes, list):
        for route in routes[:_MAX_HEALTH_ROUTES]:
            if isinstance(route, str) and route:
                safe_routes.append(route[:_MAX_HEALTH_ROUTE_CHARS])
    return {
        "status": status[:_MAX_SIGNAL_TOKEN_CHARS] if isinstance(status, str) else None,
        "routes": safe_routes,
    }


def route_tier_signals(base_url: str, token: str, limit: int = 50) -> dict[str, Any]:
    """Read the router decision log as bounded, safe per-tier/work-class signals.

    Talks only to the operator-configured router base (``{base}/decisions``, i.e.
    ``{router}/v1/decisions``) via :func:`_request`.  Unlike :func:`route_decisions`
    (which projects run-correlation fields) this keeps exactly the fields a tier/
    work-class health derivation needs: each record's ``work_class`` and
    ``created_at`` plus its ``attempts: [{tier_id, outcome, verifier_passed,
    reason}]`` (the router's own ``verify_reason`` is credential/endpoint-scrubbed
    and bounded), plus the aggregate ``totals``.  Everything is length-bounded and
    the endpoint scrubs the whole payload again at the last hop, so no token, URL,
    host, or path can ride out.  This is a PASSIVE read of the router's routing
    history -- never a probe against a model serve.
    """
    value = _request(base_url, token, "GET", f"/decisions?limit={max(1, min(limit, 100))}")
    records_raw: Any = []
    totals_raw: Any = {}
    if isinstance(value, dict):
        records_raw = value.get("records", value.get("decisions", []))
        totals_raw = value.get("totals", {})
    elif isinstance(value, list):
        records_raw = value
    if not isinstance(records_raw, list):
        raise RouterError("Anvil Serving decisions response has an unexpected shape")

    records: list[dict[str, Any]] = []
    for row in records_raw[:_MAX_SIGNAL_RECORDS]:
        if not isinstance(row, dict):
            continue
        attempts: list[dict[str, Any]] = []
        raw_attempts = row.get("attempts", [])
        if isinstance(raw_attempts, list):
            for attempt in raw_attempts[:_MAX_ATTEMPTS_PER_RECORD]:
                if not isinstance(attempt, dict):
                    continue
                tier_id = _safe_token(attempt.get("tier_id"))
                outcome = _safe_token(attempt.get("outcome"))
                if tier_id is None and outcome is None:
                    continue
                verifier = attempt.get("verifier_passed")
                reason = attempt.get("verify_reason")
                attempts.append({
                    "tier_id": tier_id,
                    "outcome": outcome,
                    "verifier_passed": verifier if isinstance(verifier, bool) else None,
                    "reason": redact_config_text(reason)[:_MAX_SIGNAL_REASON_CHARS]
                    if isinstance(reason, str) and reason else None,
                })
        records.append({
            "work_class": _safe_token(row.get("work_class")),
            "created_at": _safe_timestamp(row.get("created_at") or row.get("timestamp")),
            "attempts": attempts,
        })

    totals: dict[str, Any] = {}
    if isinstance(totals_raw, dict):
        totals = {
            "served_tiers": _safe_count_map(totals_raw.get("served_tiers")),
            "attempt_outcomes": _safe_count_map(totals_raw.get("attempt_outcomes")),
        }
    return {"totals": totals, "records": records}


#: Anvil Serving's first-class per-tier health probe (anvil-serving#292), served
#: UNDER ``/v1`` (``{router}/v1/health/tiers``) -- so it is read against the
#: OPERATOR-configured base directly (the base already ends in ``/v1``), unlike the
#: root ``/health``.  It is a REAL per-tier probe (not derived from routing), so it
#: is PREFERRED over :func:`route_tier_signals` when the router serves it.
_ROUTER_TIER_HEALTH_PATH = "/health/tiers"
_MAX_HEALTH_TIERS = 64
#: An upper bound on a reported probe latency (1 hour), so a misbehaving upstream
#: cannot smuggle an absurd integer through the popover.
_MAX_HEALTH_LATENCY_MS = 3_600_000


def route_tier_health(base_url: str, token: str) -> dict[str, Any]:
    """Read Anvil Serving's first-class per-tier health probe (anvil-serving#292).

    Talks only to the operator-configured router base (``{base}/health/tiers``, i.e.
    ``{router}/v1/health/tiers``) via the bounded, redirect-free :func:`_request` --
    never a raw model-serve host/port.  Unlike :func:`route_tier_signals` (which
    DERIVES a per-tier status from the routing log) this is a LIVE per-tier probe the
    router runs itself, so the model-health projection PREFERS it.

    Each row is scrubbed and bounded exactly like the decision-signal fields: ``id``,
    ``role`` and ``status`` are short safe tokens; ``last_check`` an RFC3339-ish
    timestamp; ``latency_ms`` a bounded non-negative int; ``reason`` a
    credential/endpoint-scrubbed, length-clamped category string.  A row without a
    safe ``id`` AND ``status`` is dropped.  A body that is not ``{"tiers": [...]}``
    raises :class:`RouterError` so the caller can FALL BACK to the decision-derived
    signals (an older router that does not serve this endpoint).
    """
    value = _request(base_url, token, "GET", _ROUTER_TIER_HEALTH_PATH)
    tiers_raw = value.get("tiers") if isinstance(value, dict) else None
    if not isinstance(tiers_raw, list):
        raise RouterError("Anvil Serving tier-health response has an unexpected shape")
    tiers: list[dict[str, Any]] = []
    for row in tiers_raw[:_MAX_HEALTH_TIERS]:
        if not isinstance(row, dict):
            continue
        tier_id = _safe_token(row.get("id"))
        status = _safe_token(row.get("status"))
        if tier_id is None or status is None:
            continue
        latency = row.get("latency_ms")
        reason = row.get("reason")
        tiers.append({
            "id": tier_id,
            "role": _safe_token(row.get("role")),
            "status": status,
            "last_check": _safe_timestamp(row.get("last_check")),
            "latency_ms": latency
            if isinstance(latency, int) and not isinstance(latency, bool) and 0 <= latency <= _MAX_HEALTH_LATENCY_MS
            else None,
            "reason": redact_config_text(reason)[:_MAX_SIGNAL_REASON_CHARS]
            if isinstance(reason, str) and reason else None,
        })
    return {"tiers": tiers}


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


#: Anvil Serving's declared unified audio gateway (anvil-serving#280).  Serving is
#: the ONLY managed model path and owns model policy (AGENTS.md); these two
#: operator-configured router endpoints are the only places the voice relay
#: reaches for STT/TTS.  The hub talks ONLY to the router base -- never to a raw
#: STT/TTS serve.  There is deliberately no provider fallback and no raw-provider
#: path (no external provider host, no provider API key): a Serving failure
#: settles as a :class:`RouterError`, never a retry against another provider.
#:
#: The wire contract, verified against the live router (#280):
#:   STT  POST {base}/audio/transcriptions
#:        -> {"purpose":"stt","audio_b64":<b64>,"format":"wav","is_final":true}
#:        <- {"text","is_final","duration_ms","model","request_id","latency_ms"}
#:   TTS  POST {base}/audio/speech
#:        -> {"purpose":"tts","input":<text>,"response_format":"pcm16"}
#:        <- {"audio_b64","format","sample_rate","model","request_id","latency_ms"}
#: ``purpose`` is REQUIRED (the gateway 400s without it); the configured model id
#: rides alongside (the gateway accepts it) so a deployment can pin the served
#: STT/TTS model.
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
    """Transcribe one in-memory audio chunk through Anvil Serving's STT gateway.

    The audio is relayed to Serving's declared ``/audio/transcriptions`` gateway
    (anvil-serving#280) only, with the REQUIRED ``purpose="stt"`` discriminator
    and the base64 audio under ``audio_b64``; the returned draft transcript is
    credential-scrubbed and bounded.  This function persists nothing and returns
    no audio — it is a transient draft used to seed an editable composer, never a
    committed turn.
    """
    response = _request(base_url, token, "POST", _SERVING_TRANSCRIBE_PATH, {
        "purpose": "stt",
        "model": model,
        "audio_b64": audio_b64,
        "format": audio_format,
        "is_final": is_final,
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
    """Synthesize playable audio for a message's text through Serving's TTS gateway.

    The text is relayed to Serving's declared ``/audio/speech`` gateway
    (anvil-serving#280) only, with the REQUIRED ``purpose="tts"`` discriminator and
    the requested ``response_format``.  The returned audio is transient playback
    bytes (base64 under ``audio_b64``) the caller streams to the browser and never
    persists; this function mutates no message state.  The gateway reports its own
    ``sample_rate`` (24000 for the served TTS model) so playback is not garbled.
    """
    response = _request(base_url, token, "POST", _SERVING_SPEECH_PATH, {
        "purpose": "tts",
        "model": model,
        "input": text,
        "response_format": output_format,
    })
    if not isinstance(response, dict):
        raise RouterError("Anvil Serving speech result has an unexpected shape")
    audio_b64 = response.get("audio_b64")
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


#: Anvil Serving's declared Responses stream path.  Serving is the ONLY managed
#: model path and owns model policy (AGENTS.md); the chat send/stream join relays
#: exactly this endpoint with ``stream: True``.  There is no provider fallback and
#: no raw-provider path -- a failure settles as a terminal Serving outcome.
_SERVING_RESPONSES_PATH = "/responses"

#: The ONLY request fields Anvil Serving's ``/v1/responses`` accepts.  The hub's
#: bounded request (:func:`workbench.chat_stream.build_bounded_request` and the
#: advanced-mode equivalent) also carries internal correlation fields -- notably
#: ``route_id`` -- for hub-side audit/keying; those are NOT part of the Serving
#: Responses schema, and Serving fail-closes an unknown field with a 400
#: ``unsupported_feature`` (rejecting the whole request, not ignoring the field).
#: The transport therefore projects the outbound body to exactly this allowlist so
#: an internal field can never leak to Serving and break the stream.  Extend this
#: set (never send outside it) if Serving's Responses schema gains a field.
_SERVING_RESPONSES_FIELDS = frozenset(
    {"model", "input", "stream", "max_output_tokens", "temperature", "reasoning"}
)

#: Hard ceilings on the relayed Serving Responses SSE stream, mirroring the
#: in-relay bounds in :mod:`workbench.chat_stream` and the byte ceilings the voice
#: functions enforce: a misbehaving upstream cannot stream unboundedly or smuggle
#: an over-long single event through the relay.
_MAX_RESPONSES_STREAM_EVENTS = 10_000
_MAX_SSE_EVENT_BYTES = 1_000_000
#: Per-READ (per-socket-op) deadline handed to ``urlopen``; the stdlib applies it
#: to each blocking read, NOT to the whole stream.  A slow-but-steady upstream is
#: additionally bounded by the event-count and per-event byte ceilings above.
_RESPONSES_READ_TIMEOUT = 300

#: Sentinel returned by :func:`_dispatch_sse_event` for the ``data: [DONE]`` marker.
_SSE_DONE = object()


def _dispatch_sse_event(payload: str) -> Any:
    """Parse one dispatched SSE ``data`` payload into an event mapping.

    ``payload`` is the FULL accumulated data of one SSE event (every consecutive
    ``data:`` line joined with ``\\n`` per the SSE spec), so a JSON value split
    across continuation lines parses correctly instead of being silently dropped.
    ``[DONE]`` returns the :data:`_SSE_DONE` sentinel; an empty, non-JSON, or
    non-object payload returns ``None`` (ignored, never failing the stream).
    """
    stripped = payload.strip()
    if not stripped:
        return None
    if stripped == "[DONE]":
        return _SSE_DONE
    try:
        event = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def stream_responses(
    base_url: str, token: str, request: Mapping[str, Any], cancel: Any,
) -> Iterator[dict[str, Any]]:
    """Relay Anvil Serving's streaming Responses SSE events (chat-first-voice live join).

    A generator implementing the ``ServingStreamTransport.open`` contract the chat
    relay (:class:`workbench.chat_stream.ChatStreamRelay`) expects: it yields each
    parsed SSE event mapping (a dict with a ``type`` key) from Serving's declared
    ``/responses`` endpoint with ``stream: True``.  ``request`` is the already-bounded
    Responses request assembled by ``workbench.chat_stream.build_bounded_request``
    (validated route/controls only -- never an endpoint, URL, or credential).

    SSE parsing follows the spec: consecutive ``data:`` lines accumulate and are
    dispatched (parsed once) on the blank separator line, so a JSON payload split
    across continuation lines is relayed intact; ``event:``/``id:``/``retry:`` field
    lines and ``:`` comment/keepalive lines are ignored; CRLF and LF both terminate
    a line; and a trailing event a server left without a final blank line is flushed.

    Talks ONLY to the operator-configured Anvil Serving URL over the tailnet; there
    is no provider fallback and no raw-provider path.  Bounded like the voice relay:
    each line is read with an explicit byte limit (so a newline-free stream cannot
    buffer unbounded memory), the accumulated event data is capped, and the event
    count is capped.  The caller's ``CancellationToken`` is honored before each read
    and the connection is torn down on every exit (``close()`` on the ``finally``),
    so a browser cancel terminates the upstream request.  A connection/HTTP failure
    raises :class:`RouterError` (the relay settles it ``serving_unavailable``); a
    read deadline raises ``TimeoutError`` (the relay settles it ``timed_out``) --
    neither leaks the router URL or token.
    """
    if not base_url or not token:
        raise RouterError("Anvil Serving route access is not configured")
    # Project to exactly the Serving-supported Responses fields.  The hub's request
    # carries internal correlation fields (e.g. ``route_id``) that Serving rejects
    # with a 400 ``unsupported_feature``; sending them would fail-close every turn.
    payload = {key: request[key] for key in request if key in _SERVING_RESPONSES_FIELDS}
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
    }
    http_request = Request(
        base_url.rstrip("/") + _SERVING_RESPONSES_PATH, data=body, headers=headers, method="POST",
    )
    try:
        response = urlopen(http_request, timeout=_RESPONSES_READ_TIMEOUT)  # nosec B310: operator-configured tailnet router
    except HTTPError as exc:
        raise RouterError(f"Anvil Serving rejected the stream ({exc.code})") from exc
    except URLError as exc:
        raise RouterError(f"Anvil Serving is unreachable: {exc.reason}") from exc
    events = 0
    data_parts: list[str] = []
    data_len = 0
    try:
        while True:
            if cancel.cancelled:
                return
            # Bounded read: at most the byte ceiling + 1, so a newline-free stream
            # cannot buffer unbounded memory.  A returned chunk at the limit that is
            # NOT newline-terminated is an over-long line -> fail closed.
            raw = response.readline(_MAX_SSE_EVENT_BYTES + 1)
            if not raw:
                break  # upstream closed
            if len(raw) > _MAX_SSE_EVENT_BYTES and not raw.endswith((b"\n", b"\r")):
                raise RouterError("Anvil Serving stream line exceeds the byte ceiling")
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line == "":
                # A blank line dispatches the buffered event (SSE spec).
                if data_parts:
                    event = _dispatch_sse_event("\n".join(data_parts))
                    data_parts = []
                    data_len = 0
                    if event is _SSE_DONE:
                        return
                    if event is not None:
                        events += 1
                        if events > _MAX_RESPONSES_STREAM_EVENTS:
                            raise RouterError("Anvil Serving stream exceeded the event ceiling")
                        yield event
                continue
            if line.startswith(":"):
                continue  # SSE comment / keepalive
            if line.startswith("data:"):
                fragment = line[len("data:"):]
                if fragment.startswith(" "):
                    fragment = fragment[1:]
                data_len += len(fragment) + 1
                if data_len > _MAX_SSE_EVENT_BYTES:
                    raise RouterError("Anvil Serving stream event exceeds the byte ceiling")
                data_parts.append(fragment)
            # Non-data field lines (event:/id:/retry:) carry no payload we relay.
        # Flush a trailing event a server left without a final blank separator line.
        if data_parts:
            event = _dispatch_sse_event("\n".join(data_parts))
            if event is not None and event is not _SSE_DONE:
                yield event
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - best-effort teardown of the upstream request
                pass


class ServingResponsesTransport:
    """Production ``ServingStreamTransport`` backed by Anvil Serving's Responses stream.

    Implements the structural ``open(request, cancel)`` contract the chat relay
    (:class:`workbench.chat_stream.ChatStreamRelay`) expects, returning the bounded
    :func:`stream_responses` generator.  It holds the operator-configured Serving
    base URL and token on the instance only (never handed to the browser), and
    reaches ONLY that URL -- there is no provider fallback and no raw-provider path.
    """

    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = base_url
        self._token = token

    def open(self, request: Mapping[str, Any], cancel: Any) -> Iterator[dict[str, Any]]:
        return stream_responses(self._base_url, self._token, request, cancel)
