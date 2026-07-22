"""Wired-path proofs for the live-deployment composition (workbench.deployment).

Every surface here is built through the REAL composition function
``build_live_overrides`` (the same code ``create_live_app`` runs in production),
then mounted on the real app through ``create_app``.  Only the infra store/graph
are substituted for their in-memory equivalents, exactly as the rest of the
suite does -- the surfaces under test are constructed by production code, never
hand-built.  The proofs assert:

* the default (empty ``WORKBENCH_LIVE_SURFACES``) keeps every injectable surface
  503, byte-for-byte today's behavior;
* each opted-in surface serves its real HTTP contract (not 503), redacted;
* an unknown surface name and a malformed chat-route config fail closed;
* ``/api/chat/routes`` serves the configured catalog, leaks no base_url/token,
  and degrades to the honest empty allowlist the web client renders as "No
  routes configured".
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from workbench.api import create_app
from workbench.config import Settings
from workbench.deployment import (
    DeploymentConfigError,
    build_live_overrides,
    create_live_app,
    parse_live_surfaces,
)
from workbench.graph import NullGraph
from workbench.store import MemoryStore

_ACTOR = {"X-Workbench-Actor": "operator"}
_CATALOG_FILE = "docs/contracts/examples/settings-descriptor.v1.json"
_PLUGIN_CATALOG_FILE = "docs/contracts/examples/plugin.catalog.v1.json"
_AUDIT_KEY = "deployment-wiring-audit-key-000"  # >= 16 octets
_CHAT_HASH_KEY = "deployment-wiring-chat-hash-key"

#: A valid reviewed chat route in the closed WORKBENCH_CHAT_ROUTES shape.
_ROUTE = {
    "route_id": "route.fast",
    "display_name": "Fast local",
    "serving_contract_version": "1.0.0",
    "route_digest": "sha256:" + "a" * 64,
    "model_profile": "chat-fast",
    "controls": ["temperature_milli"],
}
_EXPECTED_ROUTE_KEYS = frozenset({
    "provider", "route_id", "display_name", "serving_contract_version",
    "route_digest", "model_profile", "controls",
})


def _base_env(**extra: str) -> dict[str, str]:
    env = {
        "WORKBENCH_OWNER": "operator",
        "WORKBENCH_APPROVERS": "operator",
        "WORKBENCH_IDENTITY_HEADER": "X-Workbench-Actor",
        "WORKBENCH_ALLOW_INSECURE_DEV_ACTOR": "1",
    }
    env.update(extra)
    return env


def _wired_client(env: dict[str, str]) -> TestClient:
    """Build the real app from the deployment composition, infra substituted."""
    settings = Settings.from_env(env)
    overrides = build_live_overrides(env)
    return TestClient(create_app(
        settings=settings, store=MemoryStore(), graph=NullGraph(), **overrides,
    ))


def _wired_overrides_and_client(env: dict[str, str]) -> tuple[dict, TestClient]:
    """Same as :func:`_wired_client` but also hands back the composed overrides.

    A test can then make an IDENTITY assertion over the shared instances (e.g. the
    gate's preference store IS the read surface's) and, playing the operator's
    out-of-band approval role, mint a one-time grant on the gate's OWN approval
    store -- exactly the seam the deployment composition produced.
    """
    settings = Settings.from_env(env)
    overrides = build_live_overrides(env)
    client = TestClient(create_app(
        settings=settings, store=MemoryStore(), graph=NullGraph(), **overrides,
    ))
    return overrides, client


def _memory_infra(monkeypatch) -> None:
    """Substitute the ONLY infra ``create_live_app`` would otherwise build itself
    (the Postgres store + Neo4j graph) with in-memory equivalents, so the REAL
    ``create_live_app`` composition runs end-to-end without external infra -- the
    same substitution the rest of this suite applies, moved to the ``_store`` /
    ``_graph`` seam so ``create_live_app`` itself is exercised, not bypassed.
    """
    monkeypatch.setattr("workbench.api._store", lambda settings: MemoryStore())
    monkeypatch.setattr("workbench.api._graph", lambda settings: NullGraph())


# ---------------------------------------------------------------------------
# (a) default env keeps every injectable surface 503, exactly as today
# ---------------------------------------------------------------------------

#: (path, method, json-body) probes for each injectable surface's browser edge.
_INJECTABLE_PROBES = [
    ("/api/preferences", "get", None),
    ("/api/policy-operations/preview", "post", {
        "setting_id": "personal.appearance_density", "scope": "personal",
        "operation": "preference.set", "op_version": 1, "value": "compact",
    }),
    ("/api/configuration/export", "get", None),
    ("/api/conversation-transfer/audit", "get", None),
    ("/api/chat/advanced/presets", "get", None),
    ("/api/chat/advanced/templates", "get", None),
    ("/api/chat/advanced/ratings/criteria", "get", None),
    ("/api/plugin-preferences/anvil-tasks-viewer/tasks.list", "get", None),
    ("/api/conversations/search?query=hi", "get", None),
    ("/api/skill-adoptions", "get", None),
]


def test_default_env_keeps_every_injectable_surface_503():
    # Empty WORKBENCH_LIVE_SURFACES => no overrides => create_app leaves every
    # injectable surface None => 503, byte-for-byte the hermetic default.
    assert build_live_overrides(_base_env()) == {}
    client = _wired_client(_base_env())
    for path, method, body in _INJECTABLE_PROBES:
        response = getattr(client, method)(path, headers=_ACTOR, json=body) if body else getattr(client, method)(path, headers=_ACTOR)
        assert response.status_code == 503, f"{path} should fail closed by default, got {response.status_code}"


# ---------------------------------------------------------------------------
# (b) each opted-in surface serves its real contract through HTTP
# ---------------------------------------------------------------------------


def test_preference_store_serves_live_catalog_and_effective_values():
    env = _base_env(
        WORKBENCH_LIVE_SURFACES="preference_store",
        WORKBENCH_SETTINGS_CATALOG_FILE=_CATALOG_FILE,
    )
    client = _wired_client(env)
    body = client.get("/api/preferences", headers=_ACTOR)
    assert body.status_code == 200
    payload = body.json()
    ids = {setting["id"] for setting in payload["catalog"]["settings"]}
    assert "personal.appearance_density" in ids
    # authority/secret descriptors are never serialized to the actor view
    assert "deployment.state_read_location" not in ids
    assert "state_read_location" not in json.dumps(payload)


def test_policy_gate_serves_a_real_preview_over_the_observational_spine():
    env = _base_env(
        WORKBENCH_LIVE_SURFACES="policy_gate_service",
        WORKBENCH_SETTINGS_CATALOG_FILE=_CATALOG_FILE,
    )
    client = _wired_client(env)
    preview = client.post("/api/policy-operations/preview", headers=_ACTOR, json={
        "setting_id": "personal.appearance_density", "scope": "personal",
        "operation": "preference.set", "op_version": 1, "value": "compact",
    })
    assert preview.status_code == 200
    payload = preview.json()
    # The gate targets the observational, HUB-LOCAL preference spine (never an
    # external production effect): the previewed effect resolves hub-local and the
    # operation names exactly the requested personal setting.
    assert payload["preview"]["operation"]["setting_id"] == "personal.appearance_density"
    assert payload["preview"]["effect_summary"].endswith("hub-local")


def test_policy_gate_applies_over_the_shared_preference_store_and_is_visible_at_the_read_surface():
    # M1 regression: the gate MUST commit an approved hub-local change into the
    # SAME preference store the read/export surfaces serve. A private gate store
    # would receipt the change `succeeded` yet leave /api/preferences stale.
    env = _base_env(
        WORKBENCH_LIVE_SURFACES="preference_store,policy_gate_service",
        WORKBENCH_SETTINGS_CATALOG_FILE=_CATALOG_FILE,
    )
    overrides, client = _wired_overrides_and_client(env)
    gate = overrides["policy_gate_service"]
    # IDENTITY: the gate's store IS the shared preference store create_app serves.
    assert gate.preferences is overrides["preference_store"]

    op = {
        "setting_id": "personal.appearance_density", "scope": "personal",
        "operation": "preference.set", "op_version": 1, "value": "compact",
    }

    def _density(payload: dict) -> dict:
        return next(v for v in payload["effective"] if v["setting_id"] == "personal.appearance_density")

    # BEFORE: the read surface shows the reviewed default, provenance `default`.
    before = _density(client.get("/api/preferences", headers=_ACTOR).json())
    assert before["value"] == "comfortable" and before["source"] == "default"

    # The operator's out-of-band approval binds EXACTLY the previewed effect; mint
    # it on the gate's own approval store, then drive apply through REAL HTTP.
    binding = client.post("/api/policy-operations/approval-binding", headers=_ACTOR, json=op).json()
    gate.approvals.grant(
        "grant_density_compact", binding["action"], binding["payload_hash"],
        binding["actor"], binding["scope_key"],
    )
    applied = client.post(
        "/api/policy-operations/apply", headers=_ACTOR,
        json={**op, "grant_id": "grant_density_compact"},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["receipt"]["status"] == "succeeded"

    # VISIBLE: the gate-applied change now shows at /api/preferences, provenance
    # flipped from the reviewed default to a stored value.
    after = _density(client.get("/api/preferences", headers=_ACTOR).json())
    assert after["value"] == "compact" and after["source"] == "stored"


def test_configuration_transfer_export_shares_the_preference_store():
    env = _base_env(
        WORKBENCH_LIVE_SURFACES="preference_store,configuration_transfer_service",
        WORKBENCH_SETTINGS_CATALOG_FILE=_CATALOG_FILE,
        WORKBENCH_PREF_AUDIT_KEY=_AUDIT_KEY,
    )
    # A write through the live /api/preferences surface must be visible in the
    # configuration export -- proving both wrap the SAME store instance.
    client = _wired_client(env)
    put = client.put("/api/preferences/personal.time_format", headers=_ACTOR, json={
        "scope": "personal", "value": "format_12h", "expected_version": 0,
    })
    assert put.status_code == 200
    export = client.get("/api/configuration/export", headers=_ACTOR)
    assert export.status_code == 200
    values = {s["setting_id"]: s["value"] for s in export.json()["settings"]}
    assert values.get("personal.time_format") == "format_12h"


def test_conversation_search_and_transfer_serve_over_shared_conversation_store():
    # MUST-4 regression: the search/transfer surfaces must wrap the SAME
    # conversation store the chat write path uses. A conversation CREATED through
    # the real chat HTTP surface must therefore be findable by the injectable
    # search surface AND exportable by the transfer surface. If the deployment
    # composition dropped the shared-store handoff (create_app builds its OWN
    # store), the chat write would land in a DIFFERENT instance than the surfaces
    # wrap, so both value assertions below would fail closed -- which is exactly
    # the defect an empty-envelope-only assertion let survive.
    env = _base_env(
        WORKBENCH_LIVE_SURFACES="conversation_search_service,conversation_transfer_service",
        WORKBENCH_CHAT_HASH_KEY=_CHAT_HASH_KEY,
        WORKBENCH_PREF_AUDIT_KEY=_AUDIT_KEY,
    )
    client = _wired_client(env)

    # A cross-actor / empty probe is still the byte-identical empty envelope.
    search = client.get("/api/conversation-search?query=nothing-here", headers=_ACTOR)
    assert search.status_code == 200 and search.json()["result_count"] == 0

    # CREATE a conversation through the REAL chat write surface.
    title = "Router qualification alpha"
    created = client.post("/api/conversations", headers=_ACTOR, json={"title": title})
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]

    # FINDABLE: the injectable search surface (which wraps the shared store) finds
    # the just-written conversation by title.
    found = client.get("/api/conversation-search?query=qualification", headers=_ACTOR)
    assert found.status_code == 200
    found_payload = found.json()
    assert found_payload["result_count"] == 1
    results = json.loads(found_payload["payload_json"])
    assert [r["conversation_id"] for r in results] == [conversation_id]

    # EXPORTABLE: the transfer surface (same shared store) exports that exact
    # conversation, title round-tripping through the redacted export.
    export = client.get(f"/api/conversation-transfer/export/{conversation_id}", headers=_ACTOR)
    assert export.status_code == 200, export.text
    assert export.json()["conversation"]["title"] == title

    # The transfer audit still serves (empty until an export/import is audited).
    audit = client.get("/api/conversation-transfer/audit", headers=_ACTOR)
    assert audit.status_code == 200


def test_advanced_playground_stores_serve_live_actor_private_surfaces():
    env = _base_env(
        WORKBENCH_LIVE_SURFACES="advanced_preset_store,advanced_template_store,advanced_rating_store",
        WORKBENCH_PREF_AUDIT_KEY=_AUDIT_KEY,
    )
    client = _wired_client(env)
    presets = client.get("/api/chat/advanced/presets", headers=_ACTOR)
    assert presets.status_code == 200 and presets.json()["presets"] == []
    templates = client.get("/api/chat/advanced/templates", headers=_ACTOR)
    assert templates.status_code == 200 and templates.json()["templates"] == []
    criteria = client.get("/api/chat/advanced/ratings/criteria", headers=_ACTOR)
    assert criteria.status_code == 200 and len(criteria.json()["criteria"]) >= 1


def test_plugin_preferences_and_skill_adoptions_serve_live():
    env = _base_env(
        WORKBENCH_LIVE_SURFACES="plugin_preference_service,skill_adoption_store",
        WORKBENCH_PLUGIN_CATALOG_FILE=_PLUGIN_CATALOG_FILE,
    )
    client = _wired_client(env)
    prefs = client.get("/api/plugin-preferences/anvil-tasks-viewer/tasks.list", headers=_ACTOR)
    assert prefs.status_code == 200
    adoptions = client.get("/api/skill-adoptions", headers=_ACTOR)
    assert adoptions.status_code == 200 and adoptions.json()["adoptions"] == []


# ---------------------------------------------------------------------------
# (c) malformed WORKBENCH_LIVE_SURFACES and dependency gaps fail closed
# ---------------------------------------------------------------------------


def test_unknown_surface_name_fails_closed_at_startup():
    with pytest.raises(DeploymentConfigError) as exc:
        parse_live_surfaces("preference_store,not_a_real_surface")
    assert "not_a_real_surface" in str(exc.value)


def test_catalog_surface_without_catalog_file_fails_closed():
    with pytest.raises(DeploymentConfigError):
        build_live_overrides(_base_env(WORKBENCH_LIVE_SURFACES="preference_store"))


def test_audit_surface_without_key_fails_closed():
    with pytest.raises(DeploymentConfigError):
        build_live_overrides(_base_env(WORKBENCH_LIVE_SURFACES="advanced_preset_store"))


def test_conversation_surface_without_chat_hash_key_fails_closed():
    with pytest.raises(DeploymentConfigError):
        build_live_overrides(_base_env(
            WORKBENCH_LIVE_SURFACES="conversation_search_service",
            WORKBENCH_PREF_AUDIT_KEY=_AUDIT_KEY,
        ))


def test_malformed_catalog_file_fails_closed(tmp_path):
    bad = tmp_path / "bad-catalog.json"
    bad.write_text("{ not valid json", encoding="utf-8")
    with pytest.raises(DeploymentConfigError):
        build_live_overrides(_base_env(
            WORKBENCH_LIVE_SURFACES="preference_store",
            WORKBENCH_SETTINGS_CATALOG_FILE=str(bad),
        ))


def test_short_audit_key_fails_closed():
    with pytest.raises(DeploymentConfigError):
        build_live_overrides(_base_env(
            WORKBENCH_LIVE_SURFACES="advanced_preset_store",
            WORKBENCH_PREF_AUDIT_KEY="tooshort",
        ))


# ---------------------------------------------------------------------------
# (d) /api/chat/routes serves the configured catalog, never leaks internals
# ---------------------------------------------------------------------------


def test_chat_routes_serves_configured_catalog_without_leaking_config_internals():
    env = _base_env(WORKBENCH_CHAT_ROUTES=json.dumps([_ROUTE]))
    client = _wired_client(env)
    response = client.get("/api/chat/routes", headers=_ACTOR)
    assert response.status_code == 200
    payload = response.json()
    assert [route["route_id"] for route in payload["routes"]] == ["route.fast"]
    route = payload["routes"][0]
    # The projection is the closed chat-turn.v1 route-reference shape: exactly the
    # declared keys, nothing more (an additionalProperties:false analog).
    assert set(route.keys()) == _EXPECTED_ROUTE_KEYS
    assert route["display_name"] == "Fast local"
    assert route["provider"] == "anvil-serving"
    assert route["route_digest"] == "sha256:" + "a" * 64
    # No endpoint/host/token config internal is representable or present.
    blob = json.dumps(payload)
    for forbidden in ("base_url", "endpoint", "token", "://", "credential", "password"):
        assert forbidden not in blob


def test_chat_routes_actor_gated():
    env = _base_env(
        WORKBENCH_ALLOW_INSECURE_DEV_ACTOR="0",
        WORKBENCH_CHAT_ROUTES=json.dumps([_ROUTE]),
    )
    settings = Settings.from_env(env)
    client = TestClient(create_app(settings=settings, store=MemoryStore(), graph=NullGraph()))
    # No trusted identity header, dev actor off => 401 before any projection.
    assert client.get("/api/chat/routes").status_code == 401


def test_chat_routes_malformed_config_fails_closed():
    env = _base_env(WORKBENCH_CHAT_ROUTES="{ not a json array")
    client = _wired_client(env)
    response = client.get("/api/chat/routes", headers=_ACTOR)
    assert response.status_code == 503
    # A malformed catalog never serves a partial route list.
    assert "routes" not in response.json()


# ---------------------------------------------------------------------------
# (e) the empty-config degrade matches the web client's "No routes configured"
# ---------------------------------------------------------------------------


def test_chat_routes_empty_config_is_the_honest_empty_allowlist():
    # web/src/App.jsx:933 does `value.routes || []` then RouteSelect renders
    # "No routes configured" when the list is empty (App.jsx:608). A 200 with an
    # empty routes array is exactly that honest degrade.
    client = _wired_client(_base_env())  # WORKBENCH_CHAT_ROUTES unset
    response = client.get("/api/chat/routes", headers=_ACTOR)
    assert response.status_code == 200
    assert response.json() == {"routes": []}


def test_create_live_app_default_produces_no_overrides():
    # The composition seam with an empty switch selects nothing to wire.
    assert build_live_overrides(_base_env()) == {}
    # create_live_app is importable and callable as the entrypoint seam; the empty
    # switch path is the same all-None default (its infra build is exercised in
    # deployment, not hermetically here).
    assert callable(create_live_app)


# ---------------------------------------------------------------------------
# (f) the REAL create_live_app entrypoint is executed, infra substituted at the
#     _store/_graph seam so no external Postgres/Neo4j is reached
# ---------------------------------------------------------------------------


def test_create_live_app_empty_env_probes_fail_closed(monkeypatch):
    # S1: run the REAL create_live_app with an empty switch; a spot-check of
    # injectable probes still fails closed with 503, exactly the hermetic default.
    _memory_infra(monkeypatch)
    client = TestClient(create_live_app(_base_env()))
    for path, method, body in _INJECTABLE_PROBES[:3]:
        response = (
            getattr(client, method)(path, headers=_ACTOR, json=body)
            if body else getattr(client, method)(path, headers=_ACTOR)
        )
        assert response.status_code == 503, f"{path} should fail closed by default"


def test_create_live_app_one_surface_env_serves_that_surface_live(monkeypatch):
    # S1: run the REAL create_live_app with a single surface opted in; that
    # surface serves its real 200 contract while the rest stay 503.
    _memory_infra(monkeypatch)
    client = TestClient(create_live_app(_base_env(
        WORKBENCH_LIVE_SURFACES="preference_store",
        WORKBENCH_SETTINGS_CATALOG_FILE=_CATALOG_FILE,
    )))
    live = client.get("/api/preferences", headers=_ACTOR)
    assert live.status_code == 200
    ids = {setting["id"] for setting in live.json()["catalog"]["settings"]}
    assert "personal.appearance_density" in ids
    # A surface NOT opted in remains fail-closed.
    assert client.get("/api/skill-adoptions", headers=_ACTOR).status_code == 503


def test_create_live_app_malformed_chat_routes_fails_closed_at_startup(monkeypatch):
    # S4: a present-but-malformed WORKBENCH_CHAT_ROUTES raises at STARTUP (not a
    # per-request 503 indistinguishable from unconfigured). Empty stays valid.
    _memory_infra(monkeypatch)
    with pytest.raises(DeploymentConfigError):
        create_live_app(_base_env(WORKBENCH_CHAT_ROUTES="{ not a json array"))
    # A malformed non-empty ARRAY (a route missing required keys) also fails closed.
    with pytest.raises(DeploymentConfigError):
        create_live_app(_base_env(WORKBENCH_CHAT_ROUTES=json.dumps([{"route_id": "route.x"}])))
    # Empty is valid: create_live_app boots and serves the honest empty allowlist.
    client = TestClient(create_live_app(_base_env(WORKBENCH_CHAT_ROUTES="")))
    assert client.get("/api/chat/routes", headers=_ACTOR).json() == {"routes": []}


def test_delivery_projection_surface_requires_a_seed_directory():
    """Naming delivery_projection_store without a seed env fails closed at startup."""
    from workbench.deployment import DeploymentConfigError, build_live_overrides

    with pytest.raises(DeploymentConfigError, match="validated projection seed"):
        build_live_overrides({"WORKBENCH_LIVE_SURFACES": "delivery_projection_store"})

    with pytest.raises(DeploymentConfigError, match="missing seed directory"):
        build_live_overrides({
            "WORKBENCH_LIVE_SURFACES": "delivery_projection_store",
            "WORKBENCH_DELIVERY_PROJECTION_SEED": "Z:/nope/definitely-missing-seed",
        })


# ---------------------------------------------------------------------------
# (e) voice_relay_service wires the Dark Serving audio adapter, gated + closed
# ---------------------------------------------------------------------------

import base64 as _b64
import json as _json

_VOICE_STT_URL = "http://serving-stt.internal:30010/v1/audio/transcriptions"
_VOICE_TTS_URL = "http://serving-tts.internal:30011/v1/audio/speech"


class _FakeAudioServeResponse:
    """One canned serve answer for the monkeypatched ``urlopen`` context manager."""

    def __init__(self, body: bytes) -> None:
        self.status = 200
        self._body = body

    def __enter__(self) -> "_FakeAudioServeResponse":
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    def read(self, n: int = -1) -> bytes:
        return self._body if (n is None or n < 0) else self._body[:n]


def _stub_audio_serves(monkeypatch) -> None:
    """Route the adapter's STT/TTS hop to canned answers -- no real serve is hit.

    The STT URL answers the parakeet ``{"text"}`` shape; the TTS URL answers raw
    PCM16 bytes (the kokoro shape). This exercises the WIRED deployment transport
    end to end without a network.
    """
    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        if request.full_url == _VOICE_TTS_URL:
            return _FakeAudioServeResponse(b"\x07\x08" * 64)  # raw PCM16
        return _FakeAudioServeResponse(_json.dumps({"text": "wired draft words"}).encode())

    monkeypatch.setattr("workbench.serving_audio._urlopen", fake_urlopen)


def _voice_env(**extra: str) -> dict[str, str]:
    return _base_env(
        WORKBENCH_LIVE_SURFACES="voice_relay_service",
        WORKBENCH_CHAT_HASH_KEY=_CHAT_HASH_KEY,
        ANVIL_VOICE_STT_URL=_VOICE_STT_URL,
        ANVIL_VOICE_TTS_URL=_VOICE_TTS_URL,
        **extra,
    )


def test_voice_relay_defaults_to_503_when_not_wired():
    # The voice lane fails closed by default (no WORKBENCH_LIVE_SURFACES entry).
    client = _wired_client(_base_env(WORKBENCH_CHAT_HASH_KEY=_CHAT_HASH_KEY))
    r = client.post("/api/chat/voice/transcribe", headers=_ACTOR, json={
        "conversation_id": "c", "audio_base64": _b64.b64encode(b"x").decode(),
        "audio_format": "wav", "is_final": True,
    })
    assert r.status_code == 503


def test_voice_relay_wired_serves_stt_and_tts_over_the_dark_serves(monkeypatch):
    # The wired relay goes 503 -> 200 through the REAL deployment composition and
    # the REAL DarkServingAudioTransport (only the network hop is stubbed).
    _stub_audio_serves(monkeypatch)
    client = _wired_client(_voice_env())

    # Scope is authoritative: relay only for a conversation the actor OWNS. Create
    # one through the real chat surface (the SAME shared store the relay checks).
    created = client.post("/api/conversations", headers=_ACTOR, json={"title": "voice conv"})
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]

    # STT: multipart upload -> editable draft (no turn created).
    stt = client.post("/api/chat/voice/transcribe", headers=_ACTOR, json={
        "conversation_id": conversation_id,
        "audio_base64": _b64.b64encode(b"RIFF____WAVE" + b"\x00" * 48).decode(),
        "audio_format": "wav", "is_final": True,
    })
    assert stt.status_code == 200, stt.text
    # The 60-byte WAV body estimates to 0 ms (8 samples at 16 kHz): a concrete
    # value, never self-referential.
    assert stt.json() == {"draft": {"text": "wired draft words", "is_final": True, "duration_ms": 0}}

    # TTS: raw PCM16 -> transient playback audio, reporting the serve sample rate.
    tts = client.post("/api/chat/voice/speak", headers=_ACTOR, json={
        "conversation_id": conversation_id, "message_ref": "m1",
        "text": "please read this back", "output_format": "pcm16",
    })
    assert tts.status_code == 200, tts.text
    payload = tts.json()
    assert _b64.b64decode(payload["audio_base64"]) == b"\x07\x08" * 64
    assert payload["audio_format"] == "pcm16"
    assert payload["sample_rate"] == 24000  # the default kokoro serve rate, honored


def test_voice_relay_sample_rate_env_is_honored(monkeypatch):
    _stub_audio_serves(monkeypatch)
    client = _wired_client(_voice_env(ANVIL_VOICE_TTS_SAMPLE_RATE="22050"))
    created = client.post("/api/conversations", headers=_ACTOR, json={"title": "c"})
    conversation_id = created.json()["id"]
    tts = client.post("/api/chat/voice/speak", headers=_ACTOR, json={
        "conversation_id": conversation_id, "message_ref": "m", "text": "hi", "output_format": "pcm16",
    })
    assert tts.status_code == 200
    assert tts.json()["sample_rate"] == 22050


def test_voice_relay_scope_refuses_a_conversation_the_actor_does_not_own(monkeypatch):
    # A second allowlisted actor may reach the endpoint, but the ownership scope
    # check (through the shared store) fails closed with 403 for someone else's id.
    _stub_audio_serves(monkeypatch)
    client = _wired_client(_voice_env(WORKBENCH_APPROVERS="operator,intruder"))
    owned = client.post("/api/conversations", headers=_ACTOR, json={"title": "owners only"})
    conversation_id = owned.json()["id"]
    # ``intruder`` is allowlisted (passes the actor gate) but owns nothing here.
    r = client.post("/api/chat/voice/transcribe", headers={"X-Workbench-Actor": "intruder"}, json={
        "conversation_id": conversation_id,
        "audio_base64": _b64.b64encode(b"wavbytes____").decode(), "audio_format": "wav", "is_final": True,
    })
    assert r.status_code == 403


def test_voice_relay_without_serve_urls_fails_closed_at_startup():
    # Naming voice_relay_service but leaving the serve URLs unset fails the hub
    # closed at startup, not a per-request 503 an operator cannot diagnose.
    with pytest.raises(DeploymentConfigError, match="ANVIL_VOICE_STT_URL"):
        build_live_overrides(_base_env(
            WORKBENCH_LIVE_SURFACES="voice_relay_service",
            WORKBENCH_CHAT_HASH_KEY=_CHAT_HASH_KEY,
        ))
    # A non-http URL is also refused.
    with pytest.raises(DeploymentConfigError):
        build_live_overrides(_base_env(
            WORKBENCH_LIVE_SURFACES="voice_relay_service",
            WORKBENCH_CHAT_HASH_KEY=_CHAT_HASH_KEY,
            ANVIL_VOICE_STT_URL="ftp://nope",
            ANVIL_VOICE_TTS_URL=_VOICE_TTS_URL,
        ))


def test_voice_relay_without_chat_persistence_fails_closed_at_startup():
    # The scope check needs the shared conversation store; without the chat hash
    # key there is no store, so the build fails closed with a precise message.
    with pytest.raises(DeploymentConfigError):
        build_live_overrides(_base_env(
            WORKBENCH_LIVE_SURFACES="voice_relay_service",
            ANVIL_VOICE_STT_URL=_VOICE_STT_URL,
            ANVIL_VOICE_TTS_URL=_VOICE_TTS_URL,
        ))
