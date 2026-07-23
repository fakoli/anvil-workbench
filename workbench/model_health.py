"""Backend model-health projection for the top-right debug indicator.

This is the read-only projection behind ``GET /api/system/model-health``.  It
answers one debugging question at a glance -- "how are the router and the model
planes doing?" -- for five components: the ``router`` itself, the ``heavy`` and
``fast`` local tiers, ``voice`` (the audio gateway), and ``ocr``.

Authority boundary (AGENTS.md + the anvil-serving#280 no-raw-provider boundary):
every signal here comes from the operator-configured Anvil Serving ROUTER surface
ONLY -- the router's own ``/health``, its first-class ``/v1/health/tiers`` probe,
and its ``/decisions`` routing log, read through :mod:`workbench.router`
(:func:`~workbench.router.router_health`, :func:`~workbench.router.route_tier_health`,
and :func:`~workbench.router.route_tier_signals`).  The hub NEVER probes a raw model
serve host/port.

Source preference: when the router serves the live per-tier probe
``/v1/health/tiers`` (anvil-serving#292) its statuses map STRAIGHT THROUGH
(up/degraded/down) -- a real probe, so no "recent routing" caveat.  Only when that
endpoint is unavailable (an older router) does per-tier and OCR status FALL BACK to
being DERIVED from recent ``/decisions`` routing (a recently-used tier reports an
accurate served/unavailable status; an unused one reports ``idle``, not a fake
``ok``).  The ``source_note`` labels which source was used.

Honesty of each component:
  * ``router`` -- the router's ``/health`` up/down, plus whether the audio routes
    are registered.
  * ``heavy`` / ``fast`` / ``ocr`` -- the live tier-health status of ``heavy-local``
    / ``fast-local`` / ``ocr-local`` when the probe is available; otherwise the most
    recent ``/decisions`` outcome for that tier/work-class (fallback).
  * ``voice`` -- the audio GATEWAY: the live ``dark-tts`` tier status when available,
    else whether ``/health.routes`` registers both ``/v1/audio/speech`` and
    ``/v1/audio/transcriptions``.  It deliberately IGNORES ``dark-stt``, which the
    router probes via ``/v1/models`` (unimplemented by the STT serve) and so reports
    a FALSE ``down`` even though STT works via ``/audio/*``.  "voice ok" means the
    TTS tier is up / the gateway is registered, not that a synthesis round-trip was
    just proven.

Fail-closed: a router that is unreachable degrades the ``router`` dot to ``down``
and every signal it cannot observe to ``unknown`` -- it never crashes the page and
never invents an ``ok``.  Every readable string is bounded, and the API last hop
scrubs the whole payload again, so no token, endpoint, host, or path can leak.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from .config import Settings
from .router import RouterError, route_tier_health, route_tier_signals, router_health
from .system_health import rfc3339

MODEL_HEALTH_SCHEMA_VERSION = "workbench-model-health/v1"

#: The five statuses a component may report.  ``ok`` green, ``degraded`` and
#: ``idle`` amber, ``down`` red, ``unknown`` grey -- the browser adds a distinct
#: non-color glyph per status so the indicator is colorblind-safe.
MODEL_HEALTH_STATUSES = frozenset({"ok", "degraded", "down", "idle", "unknown"})

#: The fixed component catalog, in the render order the top-right cluster uses
#: (Router / Heavy / Fast / Voice / OCR).  Deployment-invariant, never secret.
COMPONENT_ORDER = ("router", "heavy", "fast", "voice", "ocr")
_LABELS = {"router": "Router", "heavy": "Heavy", "fast": "Fast", "voice": "Voice", "ocr": "OCR"}

_TIER_HEAVY = "heavy-local"
_TIER_FAST = "fast-local"
#: The live per-tier probe ids (anvil-serving#292) the components map to directly.
_TIER_OCR = "ocr-local"
#: The voice dot maps to the TTS tier, NOT ``dark-stt``: the router probes STT via
#: ``/v1/models`` (which the STT serve does not implement), so ``dark-stt`` reports
#: a FALSE ``down`` even though STT works via ``/audio/*``.  Deriving voice from
#: ``dark-tts`` (and audio-gateway registration) avoids surfacing a false outage.
_TIER_TTS = "dark-tts"
_WORK_CLASS_OCR = "ocr"
#: The two audio routes whose registration is the voice-gateway liveness signal.
_AUDIO_ROUTES = ("/v1/audio/speech", "/v1/audio/transcriptions")

#: Live tier-health ``status`` (up/degraded/down) -> component status.  A status
#: the probe does not report maps to ``unknown`` (never a fake ``ok``).
_TIER_STATUS_MAP = {"up": "ok", "degraded": "degraded", "down": "down"}

_MAX_DETAIL_CHARS = 200
_MAX_LAST_SEEN_CHARS = 40

#: The honest note shown when per-tier status came from the LIVE ``/v1/health/tiers``
#: probe (anvil-serving#292): a real per-tier probe, so no "recent routing" caveat.
SOURCE_NOTE_LIVE = (
    "Per-tier and OCR status is a live Anvil Serving tier-health probe. Voice "
    "reflects the TTS tier and audio-gateway registration (the STT /v1/models "
    "probe is not a voice-liveness signal)."
)

#: The honest note shown on the FALLBACK path -- when the live tier-health endpoint
#: is unavailable (an older router) and per-tier status is DERIVED from the routing
#: log.  Kept distinct so a derived dot is never mistaken for a live probe.
SOURCE_NOTE = (
    "Per-tier and OCR status is derived from recent Anvil Serving routing "
    "decisions, not a live per-tier probe. Voice reflects audio-gateway "
    "registration."
)


def _component(comp_id: str, status: str, detail: str, last_seen: str | None = None) -> dict[str, Any]:
    comp: dict[str, Any] = {
        "id": comp_id,
        "label": _LABELS[comp_id],
        "status": status,
        "detail": detail[:_MAX_DETAIL_CHARS],
    }
    if last_seen:
        comp["last_seen"] = str(last_seen)[:_MAX_LAST_SEEN_CHARS]
    return comp


def _audio_registered(routes: list[Any]) -> int:
    present = {route for route in routes if isinstance(route, str)}
    return sum(1 for audio in _AUDIO_ROUTES if audio in present)


def _router_component(health: dict[str, Any] | None) -> dict[str, Any]:
    if health is None:
        return _component("router", "down", "Router is unreachable.")
    registered = _audio_registered(health.get("routes") or [])
    audio = "audio routes registered" if registered == 2 else (
        "audio routes partially registered" if registered == 1 else "audio routes not registered"
    )
    if health.get("status") == "ok":
        return _component("router", "ok", f"Router healthy; {audio}.")
    return _component("router", "degraded", f"Router responded but not ok; {audio}.")


def _voice_component(health: dict[str, Any] | None) -> dict[str, Any]:
    if health is None:
        return _component("voice", "unknown", "Voice gateway status unknown (router unreachable).")
    registered = _audio_registered(health.get("routes") or [])
    if registered == 2:
        return _component("voice", "ok", "Audio gateway registered (voice path liveness).")
    if registered == 1:
        return _component("voice", "degraded", "Audio gateway only partially registered.")
    return _component("voice", "down", "Audio gateway not registered.")


def _status_from_outcome(outcome: str | None, verifier_passed: bool | None, reason: str | None) -> tuple[str, str]:
    """Map one attempt outcome to a (status, detail) pair, honestly."""
    if outcome == "served":
        if verifier_passed is False:
            return "degraded", "Served, but the verifier rejected the output."
        return "ok", "Served on the most recent attempt."
    if outcome == "skipped-unavailable":
        suffix = f" ({reason})" if reason else ""
        return "down", f"Unavailable on the most recent attempt{suffix}."
    suffix = f" ({reason})" if reason else ""
    return "degraded", f"Last attempt outcome: {outcome or 'unknown'}{suffix}."


def _normalized_records(signals: dict[str, Any]) -> list[dict[str, Any]]:
    """Records ordered most-recent-first.

    Sort by ``created_at`` descending when EVERY record carries one (ISO strings
    sort chronologically); otherwise trust the router's own order (a decision tail
    is conventionally newest-first).  Either way index 0 is treated as the most
    recent, so the derivation reflects the latest routing.
    """
    records = signals.get("records") or []
    if records and all(record.get("created_at") for record in records):
        return sorted(records, key=lambda record: record["created_at"], reverse=True)
    return records


def _tier_component(comp_id: str, tier_id: str, signals: dict[str, Any] | None) -> dict[str, Any]:
    if signals is None:
        return _component(comp_id, "unknown", "Tier status unknown (routing log unavailable).")
    for record in _normalized_records(signals):
        for attempt in record.get("attempts") or []:
            if attempt.get("tier_id") == tier_id:
                status, detail = _status_from_outcome(
                    attempt.get("outcome"), attempt.get("verifier_passed"), attempt.get("reason"),
                )
                return _component(comp_id, status, f"{detail} (from recent routing)", record.get("created_at"))
    return _component(comp_id, "idle", "No recent routing to this tier.")


def _record_outcome(record: dict[str, Any]) -> tuple[str | None, bool | None, str | None]:
    """Collapse a decision's attempts into one representative outcome.

    A served attempt wins (the work class was satisfied); else if every attempt
    was ``skipped-unavailable`` the class is down; else the last attempt's outcome.
    """
    attempts = record.get("attempts") or []
    for attempt in attempts:
        if attempt.get("outcome") == "served":
            return "served", attempt.get("verifier_passed"), attempt.get("reason")
    if attempts and all(attempt.get("outcome") == "skipped-unavailable" for attempt in attempts):
        last = attempts[-1]
        return "skipped-unavailable", None, last.get("reason")
    if attempts:
        last = attempts[-1]
        return last.get("outcome"), last.get("verifier_passed"), last.get("reason")
    return None, None, None


def _work_class_component(comp_id: str, work_class: str, signals: dict[str, Any] | None) -> dict[str, Any]:
    if signals is None:
        return _component(comp_id, "unknown", f"{_LABELS[comp_id]} status unknown (routing log unavailable).")
    for record in _normalized_records(signals):
        if record.get("work_class") == work_class:
            outcome, verifier_passed, reason = _record_outcome(record)
            status, detail = _status_from_outcome(outcome, verifier_passed, reason)
            return _component(comp_id, status, f"{detail} (from recent routing)", record.get("created_at"))
    return _component(comp_id, "idle", f"No recent {_LABELS[comp_id]} routing.")


def _component_from_tier(comp_id: str, tier: dict[str, Any] | None) -> dict[str, Any]:
    """Map ONE live tier-health row to a component descriptor.

    ``up``/``degraded``/``down`` map straight through; a tier the live probe does
    not report is ``unknown`` (never a fake ``ok`` or a routing-derived ``idle``).
    ``last_check`` populates ``last_seen`` and ``latency_ms`` rides in the detail so
    the popover shows both.
    """
    if tier is None:
        return _component(comp_id, "unknown", f"{_LABELS[comp_id]} not reported by the live tier-health probe.")
    raw_status = tier.get("status")
    status = _TIER_STATUS_MAP.get(raw_status, "unknown")
    detail = f"Live tier probe reports {raw_status or 'an unknown status'}."
    reason = tier.get("reason")
    if reason:
        detail += f" ({reason})"
    latency = tier.get("latency_ms")
    if isinstance(latency, int) and not isinstance(latency, bool):
        detail += f" {latency} ms."
    return _component(comp_id, status, detail, tier.get("last_check"))


def _voice_from_tier_health(
    health: dict[str, Any] | None, tts_tier: dict[str, Any] | None,
) -> dict[str, Any]:
    """Derive the voice-gateway dot from the TTS tier AND audio-gateway registration.

    Deliberately NEVER consults ``dark-stt``: the router probes STT via
    ``/v1/models`` (unimplemented by the STT serve), so ``dark-stt`` reports a FALSE
    ``down`` while STT actually works via ``/audio/*``.  When the live probe reports
    the TTS tier, that status drives the dot; otherwise the dot falls back to whether
    the router registered both ``/v1/audio/*`` routes.  It is labelled as the voice
    gateway so it is never read as a full synthesis round-trip.
    """
    if tts_tier is not None:
        raw = tts_tier.get("status")
        mapped = _TIER_STATUS_MAP.get(raw)
        if mapped is not None:
            detail = f"Voice gateway TTS tier reports {raw} (live probe)."
            reason = tts_tier.get("reason")
            if reason:
                detail += f" ({reason})"
            return _component("voice", mapped, detail, tts_tier.get("last_check"))
    # No usable TTS tier row: fall back to audio-gateway registration (same signal
    # the /health-only path uses), which is unaffected by the false STT probe.
    return _voice_component(health)


def _components_from_tier_health(
    health: dict[str, Any] | None, tier_health: dict[str, Any],
) -> list[dict[str, Any]]:
    """The PREFERRED derivation: live per-tier probe (anvil-serving#292) + ``/health``."""
    by_id: dict[str, dict[str, Any]] = {}
    for tier in tier_health.get("tiers") or []:
        tier_id = tier.get("id")
        if isinstance(tier_id, str):
            by_id[tier_id] = tier
    return [
        _router_component(health),
        _component_from_tier("heavy", by_id.get(_TIER_HEAVY)),
        _component_from_tier("fast", by_id.get(_TIER_FAST)),
        _voice_from_tier_health(health, by_id.get(_TIER_TTS)),
        _component_from_tier("ocr", by_id.get(_TIER_OCR)),
    ]


def derive_model_health(
    health: dict[str, Any] | None,
    signals: dict[str, Any] | None = None,
    *,
    tier_health: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Derive the five component descriptors from the router reads.

    ``health`` is the :func:`~workbench.router.router_health` projection, or ``None``
    when that read failed (router unreachable -> ``router`` down, ``voice`` unknown).

    When ``tier_health`` (the :func:`~workbench.router.route_tier_health` projection)
    is supplied it is the PREFERRED source: heavy/fast/ocr and the voice dot come from
    the live per-tier probe (anvil-serving#292), mapped straight through.

    Otherwise the FALLBACK path DERIVES tier/OCR status from ``signals`` (the
    :func:`~workbench.router.route_tier_signals` routing-log projection), or ``None``
    when the decision log could not be read (tiers/OCR ``unknown``).  Router and voice
    always come from ``/health``, so each dot reflects the signal it depends on.
    """
    if tier_health is not None:
        return _components_from_tier_health(health, tier_health)
    return [
        _router_component(health),
        _tier_component("heavy", _TIER_HEAVY, signals),
        _tier_component("fast", _TIER_FAST, signals),
        _voice_component(health),
        _work_class_component("ocr", _WORK_CLASS_OCR, signals),
    ]


def _unconfigured_components() -> list[dict[str, Any]]:
    """When no router is configured nothing can be observed: honest ``unknown``."""
    return [
        _component("router", "unknown", "Router access is not configured."),
        _component("heavy", "unknown", "Tier status unknown (router not configured)."),
        _component("fast", "unknown", "Tier status unknown (router not configured)."),
        _component("voice", "unknown", "Voice gateway status unknown (router not configured)."),
        _component("ocr", "unknown", "OCR status unknown (router not configured)."),
    ]


class ModelHealthService:
    """Builds the model-health snapshot from ONLY the router surface, with a TTL.

    Holds the operator-configured router base URL and token on the instance (never
    handed to the browser) and reads ONLY that router -- ``/health`` and
    ``/decisions`` -- through the injected readers (defaulting to
    :mod:`workbench.router`).  A short TTL cache bounds how often a poll can hit the
    router.  Every RouterError degrades honestly rather than propagating, so the
    endpoint never 500s and the indicator never blocks the page.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        clock: Callable[[], datetime] | None = None,
        ttl_seconds: float = 10.0,
        decisions_limit: int = 50,
        health_reader: Callable[[str, str], dict[str, Any]] = router_health,
        signals_reader: Callable[[str, str, int], dict[str, Any]] = route_tier_signals,
        tier_health_reader: Callable[[str, str], dict[str, Any]] = route_tier_health,
    ) -> None:
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._ttl = ttl_seconds
        self._limit = decisions_limit
        self._health_reader = health_reader
        self._signals_reader = signals_reader
        self._tier_health_reader = tier_health_reader
        self._cache: tuple[datetime, dict[str, Any]] | None = None

    def _configured(self) -> bool:
        return bool(self._settings.anvil_router_base_url and self._settings.anvil_router_token)

    def snapshot(self) -> dict[str, Any]:
        """The cached-or-fresh model-health snapshot (bounded router calls)."""
        now = self._clock()
        if self._cache is not None:
            cached_at, payload = self._cache
            if 0 <= (now - cached_at).total_seconds() < self._ttl:
                return payload
        payload = self._build(now)
        self._cache = (now, payload)
        return payload

    def _build(self, now: datetime) -> dict[str, Any]:
        checked_at = rfc3339(now)
        # Default to the fallback note; only the live-probe path overrides it. When
        # no router is configured nothing is probed, so the honest note is the
        # fallback wording ("not a live per-tier probe").
        source_note = SOURCE_NOTE
        if not self._configured():
            components = _unconfigured_components()
        else:
            base = self._settings.anvil_router_base_url
            token = self._settings.anvil_router_token
            try:
                health: dict[str, Any] | None = self._health_reader(base, token)
            except RouterError:
                health = None
            # PREFER the live per-tier probe (anvil-serving#292). Only FALL BACK to
            # the decision-log derivation when it is unavailable (an older router).
            try:
                tier_health: dict[str, Any] | None = self._tier_health_reader(base, token)
            except RouterError:
                tier_health = None
            if tier_health is not None:
                components = derive_model_health(health, tier_health=tier_health)
                source_note = SOURCE_NOTE_LIVE
            else:
                try:
                    signals: dict[str, Any] | None = self._signals_reader(base, token, self._limit)
                except RouterError:
                    signals = None
                components = derive_model_health(health, signals)
        return {
            "schema_version": MODEL_HEALTH_SCHEMA_VERSION,
            "checked_at": checked_at,
            "source_note": source_note,
            "components": components,
        }
