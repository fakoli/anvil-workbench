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
from workbench.router import (
    RouterError,
    _router_root,
    route_tier_health,
    route_tier_signals,
    router_health,
)

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


def _tiers(rows):
    return {"tiers": rows}


def _by_id(components):
    return {component["id"]: component for component in components}


def _older_router(*args):
    """A tier-health reader standing in for a router that predates #292 -- it does
    not serve /v1/health/tiers, so the service must FALL BACK to /decisions."""
    raise RouterError("Anvil Serving tier-health response has an unexpected shape")


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
        tier_health_reader=lambda *a: calls.append(a) or _tiers([]),
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

    def tier_health_reader(base, token):
        # Record the base, then behave like an older router so the FALLBACK signals
        # read is exercised too -- both readers must get only the router base.
        seen["tier_health"] = (base, token)
        raise RouterError("no /v1/health/tiers on this router")

    def signals_reader(base, token, limit):
        seen["signals"] = (base, token, limit)
        return _signals([])

    service = ModelHealthService(
        _settings(), clock=_CLOCK, health_reader=health_reader,
        signals_reader=signals_reader, tier_health_reader=tier_health_reader,
    )
    service.snapshot()
    for key in ("health", "tier_health", "signals"):
        base = seen[key][0]
        assert base == _ROUTER_BASE
        for port in RAW_SERVE_PORTS:
            assert port not in base


def test_router_error_from_a_reader_degrades_and_never_raises():
    def boom(*args):
        raise RouterError("Anvil Serving is unreachable")

    service = ModelHealthService(
        _settings(), clock=_CLOCK, health_reader=boom, signals_reader=boom, tier_health_reader=boom,
    )
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
        tier_health_reader=_older_router,  # no live probe -> exercise the fallback
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


# --- live per-tier probe (#292): PREFERRED source -----------------------------

def _live_tiers():
    return _tiers([
        {"id": "heavy-local", "role": "llm", "status": "up",
         "last_check": "2026-07-22T10:00:00Z", "latency_ms": 42},
        {"id": "fast-local", "role": "llm", "status": "down", "reason": "probe_timeout"},
        {"id": "ocr-local", "role": "llm", "status": "up"},
        {"id": "dark-tts", "role": "tts", "status": "up"},
        # dark-stt reports a FALSE down (the router probes it via /v1/models, which
        # the STT serve does not implement) even though STT works via /audio/*.
        {"id": "dark-stt", "role": "stt", "status": "down", "reason": "models_endpoint_404"},
    ])


def test_tier_health_is_the_preferred_source_and_maps_live_statuses():
    components = _by_id(derive_model_health(_health(), tier_health=_live_tiers()))
    # up -> ok (served live), down -> down, straight through.
    assert components["heavy"]["status"] == "ok"
    assert components["heavy"]["last_seen"] == "2026-07-22T10:00:00Z"
    assert "42 ms" in components["heavy"]["detail"]  # latency populates the popover
    assert components["fast"]["status"] == "down"
    assert "probe_timeout" in components["fast"]["detail"]
    assert components["ocr"]["status"] == "ok"
    # It is a LIVE probe, so the detail must NOT carry the fallback "recent routing"
    # honesty caveat.
    assert "recent routing" not in components["heavy"]["detail"]


def test_tier_health_missing_tier_is_unknown_not_a_fake_ok_or_idle():
    components = _by_id(derive_model_health(_health(), tier_health=_tiers([
        {"id": "heavy-local", "role": "llm", "status": "up"},
    ])))
    assert components["fast"]["status"] == "unknown"  # not reported by the probe
    assert components["ocr"]["status"] == "unknown"


def test_tier_health_dark_stt_only_down_does_NOT_force_voice_down():
    # dark-tts up + dark-stt down -> voice is driven by TTS, so it is OK. The false
    # STT probe must never surface a voice outage.
    voice = _by_id(derive_model_health(_health(), tier_health=_live_tiers()))["voice"]
    assert voice["status"] == "ok"
    assert "TTS" in voice["detail"]  # labelled as the voice/TTS gateway, not STT


def test_tier_health_voice_maps_a_real_tts_outage_to_down():
    tiers = _tiers([{"id": "dark-tts", "role": "tts", "status": "down", "reason": "unloaded"}])
    voice = _by_id(derive_model_health(_health(), tier_health=tiers))["voice"]
    assert voice["status"] == "down"


def test_tier_health_voice_falls_back_to_gateway_registration_when_no_tts_tier():
    # No dark-tts row at all -> the voice dot falls back to audio-gateway
    # registration (still ignoring the false STT probe).
    no_tts = _tiers([{"id": "dark-stt", "role": "stt", "status": "down"}])
    assert _by_id(derive_model_health(_health(audio=True), tier_health=no_tts))["voice"]["status"] == "ok"
    assert _by_id(derive_model_health(_health(audio=False), tier_health=no_tts))["voice"]["status"] == "down"


def test_service_prefers_tier_health_and_does_not_read_the_decision_log():
    read = {"tier_health": 0, "signals": 0}

    def tier_health_reader(base, token):
        read["tier_health"] += 1
        return _live_tiers()

    def signals_reader(base, token, limit):
        read["signals"] += 1
        return _signals([])

    service = ModelHealthService(
        _settings(), clock=_CLOCK, health_reader=lambda *a: _health(),
        signals_reader=signals_reader, tier_health_reader=tier_health_reader,
    )
    snapshot = service.snapshot()
    # The live probe was read; the /decisions fallback was NOT.
    assert read == {"tier_health": 1, "signals": 0}
    assert _by_id(snapshot["components"])["heavy"]["status"] == "ok"
    # The source note reflects the LIVE probe, dropping the "recent routing" caveat.
    assert "live Anvil Serving tier-health probe" in snapshot["source_note"]
    assert "derived from recent" not in snapshot["source_note"]


def test_service_falls_back_to_the_decision_log_on_an_older_router():
    read = {"signals": 0}

    def signals_reader(base, token, limit):
        read["signals"] += 1
        return _signals([
            {"work_class": "chat", "created_at": "2026-07-22T10:00:00Z",
             "attempts": [{"tier_id": "heavy-local", "outcome": "served",
                           "verifier_passed": True, "reason": None}]},
        ])

    service = ModelHealthService(
        _settings(), clock=_CLOCK, health_reader=lambda *a: _health(),
        signals_reader=signals_reader, tier_health_reader=_older_router,
    )
    snapshot = service.snapshot()
    # The live probe was unavailable, so the decision log WAS read (the fallback).
    assert read == {"signals": 1}
    components = _by_id(snapshot["components"])
    assert components["heavy"]["status"] == "ok"
    assert "recent routing" in components["heavy"]["detail"]  # honest fallback caveat
    assert "not a live per-tier probe" in snapshot["source_note"]


# --- route_tier_health reader: boundary + shape ------------------------------

def test_route_tier_health_maps_the_payload_and_reaches_only_the_router_base(monkeypatch):
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

    body = ('{"tiers":[{"id":"heavy-local","role":"llm","status":"up",'
            '"last_check":"2026-07-22T10:00:00Z","latency_ms":42},'
            '{"id":"dark-stt","role":"stt","status":"down","reason":"models_endpoint_404"}]}')

    def fake_urlopen(request, timeout=0):
        urls.append(request.full_url)
        return _Resp(body)

    monkeypatch.setattr("workbench.router.urlopen", fake_urlopen)
    result = route_tier_health(_ROUTER_BASE, "server-held")

    # Served UNDER /v1 (the base already ends in /v1), never at the root, and never a
    # raw serve port.
    assert urls == ["http://100.87.34.66:8000/v1/health/tiers"]
    for port in RAW_SERVE_PORTS:
        assert port not in urls[0]
    by_id = {tier["id"]: tier for tier in result["tiers"]}
    assert by_id["heavy-local"]["status"] == "up"
    assert by_id["heavy-local"]["latency_ms"] == 42
    assert by_id["heavy-local"]["last_check"] == "2026-07-22T10:00:00Z"
    assert by_id["dark-stt"]["status"] == "down"


def test_route_tier_health_fails_closed_on_a_bad_shape(monkeypatch):
    class _Resp:
        def __init__(self, body):
            self._body = body.encode("utf-8")

        def read(self, *args):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    # Not a {"tiers": [...]} object -> the caller must FALL BACK, not trust it.
    monkeypatch.setattr("workbench.router.urlopen", lambda request, timeout=0: _Resp('{"unexpected":true}'))
    try:
        route_tier_health(_ROUTER_BASE, "server-held")
        assert False, "expected RouterError on a bad tier-health shape"
    except RouterError:
        pass


def test_route_tier_signals_preserves_an_rfc3339_created_at(monkeypatch):
    # The colon-bearing timestamp must survive the reader (a plain token
    # validator drops it, killing last_seen and the recency sort).
    class _Resp:
        def __init__(self, body):
            self._body = body.encode("utf-8")

        def read(self, *args):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    body = ('{"records":[{"work_class":"chat","created_at":"2026-07-22T10:00:00Z",'
            '"attempts":[{"tier_id":"heavy-local","outcome":"served","verifier_passed":true}]}]}')
    monkeypatch.setattr("workbench.router.urlopen", lambda request, timeout=0: _Resp(body))
    signals = route_tier_signals(_ROUTER_BASE, "server-held", 10)
    assert signals["records"][0]["created_at"] == "2026-07-22T10:00:00Z"
