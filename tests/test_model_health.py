"""Backend model-health projection tests (top-right debug indicator).

Covers the pure derivation (router /health + /decisions fixtures -> the five
component statuses), the TTL-cached service, honest fail-closed degradation, and
-- the load-bearing boundary assertion -- that the readers are only ever handed
the operator-configured ROUTER base, never a raw model-serve host/port.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from workbench.config import Settings
from workbench.model_health import (
    COMPONENT_ORDER,
    MODEL_HEALTH_STATUSES,
    ModelHealthService,
    derive_model_health,
)
from workbench.router import RouterError, _router_root, route_tier_signals, router_health

# The raw model-serve ports whose direct probing #280 removed. The hub must never
# reach any of these -- only the router base -- so they are the negative assertion.
RAW_SERVE_PORTS = (":30002", ":30003", ":30005", ":30006", ":30010", ":30011")

_ROUTER_BASE = "http://100.87.34.66:8000/v1"
_CLOCK = lambda: datetime(2026, 7, 22, tzinfo=timezone.utc)


def _settings(**overrides) -> Settings:
    base = dict(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator"}), bridge_bootstrap_token="",
        anvil_router_base_url=_ROUTER_BASE, anvil_router_token="server-held",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=True,
    )
    base.update(overrides)
    return Settings(**base)


def _health(status="ok", audio=True):
    routes = ["/v1/chat/completions", "/v1/decisions"]
    if audio:
        routes = ["/v1/audio/speech", "/v1/audio/transcriptions", *routes]
    return {"status": status, "routes": routes}


def _signals(records):
    return {"totals": {"served_tiers": {}, "attempt_outcomes": {}}, "records": records}


def _by_id(components):
    return {component["id"]: component for component in components}


# --- derivation --------------------------------------------------------------

def test_derives_the_five_components_in_render_order():
    components = derive_model_health(_health(), _signals([]))
    assert tuple(component["id"] for component in components) == COMPONENT_ORDER
    for component in components:
        assert component["status"] in MODEL_HEALTH_STATUSES
        assert component["label"] and component["detail"]


def test_served_tier_maps_to_ok_and_carries_last_seen():
    signals = _signals([
        {"work_class": "chat", "created_at": "2026-07-22T10:00:00Z",
         "attempts": [{"tier_id": "heavy-local", "outcome": "served", "verifier_passed": True, "reason": None}]},
    ])
    heavy = _by_id(derive_model_health(_health(), signals))["heavy"]
    assert heavy["status"] == "ok"
    assert heavy["last_seen"] == "2026-07-22T10:00:00Z"
    assert "recent routing" in heavy["detail"]


def test_skipped_unavailable_tier_maps_to_down():
    signals = _signals([
        {"work_class": "chat-fast", "created_at": "2026-07-22T10:00:00Z",
         "attempts": [{"tier_id": "fast-local", "outcome": "skipped-unavailable",
                       "verifier_passed": None, "reason": "health_transport_URLError"}]},
    ])
    fast = _by_id(derive_model_health(_health(), signals))["fast"]
    assert fast["status"] == "down"
    assert "health_transport_URLError" in fast["detail"]


def test_absent_tier_maps_to_idle_not_a_fake_ok():
    fast = _by_id(derive_model_health(_health(), _signals([])))["fast"]
    assert fast["status"] == "idle"


def test_served_attempt_with_failed_verifier_is_degraded():
    signals = _signals([
        {"work_class": "chat", "created_at": "2026-07-22T10:00:00Z",
         "attempts": [{"tier_id": "heavy-local", "outcome": "served", "verifier_passed": False, "reason": None}]},
    ])
    assert _by_id(derive_model_health(_health(), signals))["heavy"]["status"] == "degraded"


def test_most_recent_decision_wins_by_created_at():
    signals = _signals([
        {"work_class": "chat", "created_at": "2026-07-22T09:00:00Z",
         "attempts": [{"tier_id": "heavy-local", "outcome": "skipped-unavailable", "verifier_passed": None, "reason": None}]},
        {"work_class": "chat", "created_at": "2026-07-22T11:00:00Z",
         "attempts": [{"tier_id": "heavy-local", "outcome": "served", "verifier_passed": True, "reason": None}]},
    ])
    heavy = _by_id(derive_model_health(_health(), signals))["heavy"]
    assert heavy["status"] == "ok"  # the 11:00 served decision, not the 09:00 skip
    assert heavy["last_seen"] == "2026-07-22T11:00:00Z"


def test_ocr_work_class_served_is_ok_and_unavailable_is_down():
    served = _signals([
        {"work_class": "ocr", "created_at": "2026-07-22T10:00:00Z",
         "attempts": [{"tier_id": "ocr-local", "outcome": "served", "verifier_passed": True, "reason": None}]},
    ])
    assert _by_id(derive_model_health(_health(), served))["ocr"]["status"] == "ok"
    down = _signals([
        {"work_class": "ocr", "created_at": "2026-07-22T10:00:00Z",
         "attempts": [{"tier_id": "ocr-local", "outcome": "skipped-unavailable", "verifier_passed": None, "reason": None}]},
    ])
    assert _by_id(derive_model_health(_health(), down))["ocr"]["status"] == "down"


def test_ocr_idle_when_no_ocr_routing_seen():
    signals = _signals([
        {"work_class": "chat", "created_at": "2026-07-22T10:00:00Z",
         "attempts": [{"tier_id": "heavy-local", "outcome": "served", "verifier_passed": True, "reason": None}]},
    ])
    assert _by_id(derive_model_health(_health(), signals))["ocr"]["status"] == "idle"


def test_voice_ok_when_both_audio_routes_registered_else_down():
    assert _by_id(derive_model_health(_health(audio=True), _signals([])))["voice"]["status"] == "ok"
    assert _by_id(derive_model_health(_health(audio=False), _signals([])))["voice"]["status"] == "down"


def test_router_unreachable_is_down_and_voice_unknown_but_tiers_still_derive():
    # Independent reads: a failed /health does not blind the decision-derived tiers.
    signals = _signals([
        {"work_class": "chat", "created_at": "2026-07-22T10:00:00Z",
         "attempts": [{"tier_id": "heavy-local", "outcome": "served", "verifier_passed": True, "reason": None}]},
    ])
    components = _by_id(derive_model_health(None, signals))
    assert components["router"]["status"] == "down"
    assert components["voice"]["status"] == "unknown"
    assert components["heavy"]["status"] == "ok"


def test_router_ok_but_decisions_unavailable_leaves_tiers_unknown():
    components = _by_id(derive_model_health(_health(), None))
    assert components["router"]["status"] == "ok"
    assert components["heavy"]["status"] == "unknown"
    assert components["fast"]["status"] == "unknown"
    assert components["ocr"]["status"] == "unknown"


# --- service (TTL, fail-closed, boundary) ------------------------------------

def test_unconfigured_router_reports_all_unknown_and_reads_nothing():
    calls = []
    service = ModelHealthService(
        _settings(anvil_router_base_url="", anvil_router_token=""),
        clock=_CLOCK,
        health_reader=lambda *a: calls.append(a) or _health(),
        signals_reader=lambda *a: calls.append(a) or _signals([]),
    )
    snapshot = service.snapshot()
    assert calls == []  # nothing is probed when no router is configured
    assert {component["status"] for component in snapshot["components"]} == {"unknown"}
    assert snapshot["schema_version"] == "workbench-model-health/v1"
    assert "not a live per-tier probe" in snapshot["source_note"]


def test_service_passes_only_the_router_base_never_a_serve_host():
    seen = {}

    def health_reader(base, token):
        seen["health"] = (base, token)
        return _health()

    def signals_reader(base, token, limit):
        seen["signals"] = (base, token, limit)
        return _signals([])

    service = ModelHealthService(_settings(), clock=_CLOCK, health_reader=health_reader, signals_reader=signals_reader)
    service.snapshot()
    for key in ("health", "signals"):
        base = seen[key][0]
        assert base == _ROUTER_BASE
        for port in RAW_SERVE_PORTS:
            assert port not in base


def test_router_error_from_a_reader_degrades_and_never_raises():
    def boom(*args):
        raise RouterError("Anvil Serving is unreachable")

    service = ModelHealthService(_settings(), clock=_CLOCK, health_reader=boom, signals_reader=boom)
    components = _by_id(service.snapshot()["components"])
    assert components["router"]["status"] == "down"
    assert components["heavy"]["status"] == "unknown"


def test_snapshot_is_ttl_cached_so_a_poll_does_not_hammer_the_router():
    now = {"t": datetime(2026, 7, 22, tzinfo=timezone.utc)}
    counter = {"health": 0, "signals": 0}

    def health_reader(base, token):
        counter["health"] += 1
        return _health()

    def signals_reader(base, token, limit):
        counter["signals"] += 1
        return _signals([])

    service = ModelHealthService(
        _settings(), clock=lambda: now["t"], ttl_seconds=10.0,
        health_reader=health_reader, signals_reader=signals_reader,
    )
    service.snapshot()
    service.snapshot()  # within TTL -> served from cache
    assert counter == {"health": 1, "signals": 1}
    now["t"] += timedelta(seconds=11)  # past TTL -> refetch
    service.snapshot()
    assert counter == {"health": 2, "signals": 2}


# --- router.py readers: boundary + shape -------------------------------------

def test_router_root_strips_a_trailing_v1_segment():
    assert _router_root("http://100.87.34.66:8000/v1") == "http://100.87.34.66:8000"
    assert _router_root("http://100.87.34.66:8000/v1/") == "http://100.87.34.66:8000"
    assert _router_root("http://serving") == "http://serving"


def test_readers_reach_only_the_router_host_never_a_serve_port(monkeypatch):
    urls = []

    class _Resp:
        def __init__(self, body):
            self._body = body.encode("utf-8")

        def read(self, *args):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=0):
        urls.append(request.full_url)
        if request.full_url.endswith("/health"):
            body = '{"status":"ok","routes":["/v1/audio/speech","/v1/audio/transcriptions"]}'
        else:
            body = '{"totals":{},"records":[]}'
        return _Resp(body)

    monkeypatch.setattr("workbench.router.urlopen", fake_urlopen)
    router_health(_ROUTER_BASE, "server-held")
    route_tier_signals(_ROUTER_BASE, "server-held", 25)

    assert urls == [
        "http://100.87.34.66:8000/health",       # health at the ROOT (/v1 stripped)
        "http://100.87.34.66:8000/v1/decisions?limit=25",  # decisions under /v1
    ]
    for url in urls:
        for port in RAW_SERVE_PORTS:
            assert port not in url


def test_route_tier_signals_scrubs_and_bounds_the_reason(monkeypatch):
    class _Resp:
        def __init__(self, body):
            self._body = body.encode("utf-8")

        def read(self, *args):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    leaky = ('{"records":[{"work_class":"ocr","attempts":['
             '{"tier_id":"ocr-local","outcome":"skipped-unavailable",'
             '"verify_reason":"connect to 100.64.0.9:30010 failed"}]}]}')
    monkeypatch.setattr("workbench.router.urlopen", lambda request, timeout=0: _Resp(leaky))
    signals = route_tier_signals(_ROUTER_BASE, "server-held", 10)
    reason = signals["records"][0]["attempts"][0]["reason"]
    assert "100.64.0.9" not in (reason or "")
