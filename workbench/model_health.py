"""Backend model-health projection for the top-right debug indicator.

This is the read-only projection behind ``GET /api/system/model-health``.  It
answers one debugging question at a glance -- "how are the router and the model
planes doing?" -- for five components: the ``router`` itself, the ``heavy`` and
``fast`` local tiers, ``voice`` (the audio gateway), and ``ocr``.

Authority boundary (AGENTS.md + the anvil-serving#280 no-raw-provider boundary):
every signal here comes from the operator-configured Anvil Serving ROUTER surface
ONLY -- the router's own ``/health`` and its ``/decisions`` routing log, read
through :mod:`workbench.router` (:func:`~workbench.router.router_health` and
:func:`~workbench.router.route_tier_signals`).  The hub NEVER probes a raw model
serve host/port: the router does not expose a first-class per-tier health
endpoint, so per-tier and OCR status is DERIVED from recent routing decisions and
labelled honestly as such.  A recently-used tier reports an accurate served/
unavailable status; an unused one reports ``idle`` (not a fake ``ok``).

Honesty of each component:
  * ``router`` -- the router's ``/health`` up/down, plus whether the audio routes
    are registered.
  * ``heavy`` / ``fast`` -- the most recent ``/decisions`` attempt for
    ``tier_id`` ``heavy-local`` / ``fast-local`` (``served`` -> ok,
    ``skipped-unavailable`` -> down, not-seen -> idle).  It reflects recent
    ROUTING, not a live probe.
  * ``ocr`` -- the most recent decision whose ``work_class`` is ``ocr``.
  * ``voice`` -- the audio GATEWAY: ok when ``/health.routes`` registers both
    ``/v1/audio/speech`` and ``/v1/audio/transcriptions`` (the voice path the hub
    uses).  "voice ok" means the gateway is registered, not that a synthesis
    round-trip was just proven.

Fail-closed: a router that is unreachable degrades the ``router`` dot to ``down``
and every signal it cannot observe to ``unknown`` -- it never crashes the page and
never invents an ``ok``.  Every readable string is bounded, and the API last hop
scrubs the whole payload again, so no token, endpoint, host, or path can leak.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from .config import Settings
from .router import RouterError, route_tier_signals, router_health
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
_WORK_CLASS_OCR = "ocr"
#: The two audio routes whose registration is the voice-gateway liveness signal.
_AUDIO_ROUTES = ("/v1/audio/speech", "/v1/audio/transcriptions")

_MAX_DETAIL_CHARS = 200
_MAX_LAST_SEEN_CHARS = 40

#: The single honest note the browser shows so a per-tier dot is never mistaken
#: for a live probe.
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


def derive_model_health(
    health: dict[str, Any] | None,
    signals: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Derive the five component descriptors from the two router reads.

    ``health`` is the :func:`~workbench.router.router_health` projection, or
    ``None`` when that read failed (router unreachable -> ``router`` down, ``voice``
    unknown).  ``signals`` is the :func:`~workbench.router.route_tier_signals`
    projection, or ``None`` when the decision log could not be read (tiers/OCR
    unknown).  The two reads are independent, so each dot reflects the signal it
    actually depends on.
    """
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
    ) -> None:
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._ttl = ttl_seconds
        self._limit = decisions_limit
        self._health_reader = health_reader
        self._signals_reader = signals_reader
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
        if not self._configured():
            components = _unconfigured_components()
        else:
            base = self._settings.anvil_router_base_url
            token = self._settings.anvil_router_token
            try:
                health: dict[str, Any] | None = self._health_reader(base, token)
            except RouterError:
                health = None
            try:
                signals: dict[str, Any] | None = self._signals_reader(base, token, self._limit)
            except RouterError:
                signals = None
            components = derive_model_health(health, signals)
        return {
            "schema_version": MODEL_HEALTH_SCHEMA_VERSION,
            "checked_at": checked_at,
            "source_note": SOURCE_NOTE,
            "components": components,
        }
