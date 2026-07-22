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
    env = _base_env(
        WORKBENCH_LIVE_SURFACES="conversation_search_service,conversation_transfer_service",
        WORKBENCH_CHAT_HASH_KEY=_CHAT_HASH_KEY,
        WORKBENCH_PREF_AUDIT_KEY=_AUDIT_KEY,
    )
    client = _wired_client(env)
    search = client.get("/api/conversations/search?query=nothing", headers=_ACTOR)
    assert search.status_code == 200
    # A cross-actor / empty probe is the byte-identical empty envelope.
    assert search.json()["conversations"] == []
    audit = client.get("/api/conversation-transfer/audit", headers=_ACTOR)
    assert audit.status_code == 200
    assert audit.json()["audit"] == []


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
