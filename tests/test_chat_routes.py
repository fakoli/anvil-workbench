"""Hermetic contract tests for chat route discovery/validation (T003.1).

Criterion map:

1. Discovery returns exactly the configured chat routes with stable
   identifiers -- ``test_discovery_returns_exactly_the_configured_routes...``
2. Unknown routes and undeclared chat controls fail before a Serving request
   -- ``test_unknown_route_selection_fails_closed_before_any_serving_request``
   and ``test_undeclared_control_selection_fails_closed...``
3. Browser-facing metadata carries no token, provider URL, credential, or
   hidden policy field -- ``test_browser_metadata_exposes_only_safe_fields``
   plus the configuration-refusal tests.
4. No router branch constructs or invokes a raw-provider endpoint --
   ``test_no_raw_provider_fallback_in_workbench_sources`` and
   ``test_router_refuses_when_serving_is_unconfigured_instead_of_falling_back``.

No test issues a live Serving request; the configured route set is injected
as plain data.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from workbench import chat_routes, router
from workbench.chat_routes import (
    ChatRouteError,
    DiscoveredChatRoutes,
    discover_chat_routes,
    parse_chat_routes_config,
    validate_chat_route_selection,
)
from workbench.config import Settings

_REPO_ROOT = Path(__file__).resolve().parents[1]

_CONFIGURED = (
    {
        "route_id": "chat.fast",
        "display_name": "Fast chat",
        "serving_contract_version": "1.2.0",
        "route_digest": "sha256:" + "a" * 64,
        "model_profile": "chat-fast",
        "controls": ["max_output_tokens"],
    },
    {
        "provider": "anvil-serving",
        "route_id": "chat.heavy",
        "serving_contract_version": "1.2.0",
        "route_digest": "sha256:" + "b" * 64,
        "model_profile": "chat-heavy",
        "controls": ["temperature_milli", "max_output_tokens", "reasoning_effort"],
    },
)


def configured() -> list[dict]:
    return copy.deepcopy(list(_CONFIGURED))


# --- criterion 1: exactly the configured set, stable identifiers -------------


def test_discovery_returns_exactly_the_configured_routes_with_stable_identifiers():
    first = discover_chat_routes(configured())
    second = discover_chat_routes(configured())

    assert first.route_ids == ("chat.fast", "chat.heavy")
    assert first.route_ids == second.route_ids
    assert first.as_dict() == second.as_dict()
    fast = first.route("chat.fast")
    assert fast.provider == "anvil-serving"
    assert fast.route_digest == "sha256:" + "a" * 64
    assert fast.model_profile == "chat-fast"
    assert fast.controls == ("max_output_tokens",)
    heavy = first.route("chat.heavy")
    assert heavy.display_name == "chat.heavy"  # defaults to the stable id
    assert heavy.controls == ("max_output_tokens", "reasoning_effort", "temperature_milli")


def test_configured_routes_flow_from_the_environment_setting():
    settings = Settings.from_env({"WORKBENCH_CHAT_ROUTES": json.dumps(configured())})

    discovered = discover_chat_routes(parse_chat_routes_config(settings.chat_routes))

    assert discovered.route_ids == ("chat.fast", "chat.heavy")


def test_unconfigured_chat_routes_refuse_every_selection():
    settings = Settings.from_env({})
    discovered = discover_chat_routes(parse_chat_routes_config(settings.chat_routes))

    assert discovered.routes == ()
    with pytest.raises(ChatRouteError, match="not in the reviewed allowlist"):
        validate_chat_route_selection("chat.fast", None, discovered)


def test_malformed_configuration_documents_fail_closed():
    with pytest.raises(ChatRouteError, match="not valid JSON"):
        parse_chat_routes_config("{not json")
    with pytest.raises(ChatRouteError, match="JSON array"):
        parse_chat_routes_config('{"route_id": "chat.fast"}')
    with pytest.raises(ChatRouteError, match="JSON object"):
        parse_chat_routes_config('["chat.fast"]')
    with pytest.raises(ChatRouteError, match="sequence"):
        discover_chat_routes({"route_id": "chat.fast"})  # type: ignore[arg-type]


def test_discovery_refuses_duplicates_foreign_providers_and_bad_identifiers():
    duplicate = configured() + [configured()[0]]
    with pytest.raises(ChatRouteError, match="duplicate route"):
        discover_chat_routes(duplicate)

    foreign = configured()
    foreign[0]["provider"] = "raw-provider"
    with pytest.raises(ChatRouteError, match="may only reference anvil-serving"):
        discover_chat_routes(foreign)

    for key, value in (
        ("route_id", "Chat.Fast"),
        ("route_id", "chat/fast"),
        ("serving_contract_version", "1.2"),
        ("route_digest", "sha256:zz"),
        ("model_profile", "Chat Fast"),
    ):
        broken = configured()
        broken[0][key] = value
        with pytest.raises(ChatRouteError, match=f"invalid {key}"):
            discover_chat_routes(broken)

    missing = configured()
    del missing[0]["route_digest"]
    with pytest.raises(ChatRouteError, match="missing required keys: route_digest"):
        discover_chat_routes(missing)


def test_discovery_refuses_undeclared_config_keys_including_endpoint_shapes():
    for key in ("endpoint", "base_url", "url", "token", "api_key", "policy", "credentials"):
        smuggled = configured()
        smuggled[0][key] = "https://raw.example.com/v1"
        with pytest.raises(ChatRouteError, match=f"undeclared keys: {key}"):
            discover_chat_routes(smuggled)


def test_discovery_refuses_undeclared_or_duplicate_controls():
    unknown = configured()
    unknown[0]["controls"] = ["max_output_tokens", "system_prompt_override"]
    with pytest.raises(ChatRouteError, match="outside the chat-turn.v1 surface"):
        discover_chat_routes(unknown)

    duplicated = configured()
    duplicated[0]["controls"] = ["max_output_tokens", "max_output_tokens"]
    with pytest.raises(ChatRouteError, match="duplicate control"):
        discover_chat_routes(duplicated)


def test_display_name_cannot_carry_a_url_or_secret_marker():
    for display in ("https://raw.example.com", "bearer abc", "the api_key value", "x" * 121):
        unsafe = configured()
        unsafe[0]["display_name"] = display
        with pytest.raises(ChatRouteError, match="display_name"):
            discover_chat_routes(unsafe)


# --- criterion 2: refusals happen before any Serving request -----------------


def test_unknown_route_selection_fails_closed_before_any_serving_request(monkeypatch):
    discovered = discover_chat_routes(configured())
    # The only managed model path is the configured Serving client; prove the
    # refusal never reaches it by making any request attempt explode.
    monkeypatch.setattr(
        router, "_request", lambda *_a, **_k: pytest.fail("refusal must precede any Serving request")
    )

    with pytest.raises(ChatRouteError, match="not in the reviewed allowlist"):
        validate_chat_route_selection("chat.unlisted", {"max_output_tokens": 100}, discovered)


def test_undeclared_control_selection_fails_closed_before_any_serving_request(monkeypatch):
    discovered = discover_chat_routes(configured())
    monkeypatch.setattr(
        router, "_request", lambda *_a, **_k: pytest.fail("refusal must precede any Serving request")
    )

    # chat.fast declares only max_output_tokens; temperature is declared by the
    # contract but not by this route, and a made-up control is never declared.
    with pytest.raises(ChatRouteError, match="does not declare control"):
        validate_chat_route_selection("chat.fast", {"temperature_milli": 700}, discovered)
    with pytest.raises(ChatRouteError, match="outside the chat-turn.v1 surface"):
        validate_chat_route_selection("chat.heavy", {"tool_allowlist": ["shell"]}, discovered)


def test_control_values_are_bounded_to_the_chat_turn_contract():
    discovered = discover_chat_routes(configured())

    for name, value in (
        ("temperature_milli", 2001),
        ("temperature_milli", -1),
        ("temperature_milli", True),
        ("temperature_milli", 0.7),
        ("max_output_tokens", 0),
        ("reasoning_effort", "maximum"),
        ("reasoning_effort", 3),
    ):
        with pytest.raises(ChatRouteError, match=name):
            validate_chat_route_selection("chat.heavy", {name: value}, discovered)


def test_selection_refuses_a_caller_assembled_discovery_structure():
    lookalike = {"routes": [{"route_id": "chat.rogue"}]}
    with pytest.raises(ChatRouteError, match="frozen snapshot"):
        validate_chat_route_selection("chat.rogue", None, lookalike)  # type: ignore[arg-type]
    with pytest.raises(ChatRouteError, match="must be a string"):
        validate_chat_route_selection(None, None, discover_chat_routes(configured()))
    with pytest.raises(ChatRouteError, match="mapping"):
        validate_chat_route_selection(
            "chat.fast", ["max_output_tokens"], discover_chat_routes(configured())  # type: ignore[arg-type]
        )


def test_valid_selection_pins_the_route_and_accepted_controls():
    discovered = discover_chat_routes(configured())

    selection = validate_chat_route_selection(
        "chat.heavy", {"reasoning_effort": "high", "max_output_tokens": 2048}, discovered,
    )

    assert selection.route is discovered.route("chat.heavy")
    assert selection.controls_dict() == {"reasoning_effort": "high", "max_output_tokens": 2048}
    bare = validate_chat_route_selection("chat.fast", None, discovered)
    assert bare.controls == ()


# --- criterion 3: browser-safe metadata only ---------------------------------


def test_browser_metadata_exposes_only_safe_fields():
    view = discover_chat_routes(configured()).as_dict()

    assert set(view) == {"routes"}
    for route in view["routes"]:
        assert set(route) == {
            "provider", "route_id", "display_name", "serving_contract_version",
            "route_digest", "model_profile", "controls",
        }
        assert route["provider"] == "anvil-serving"
    serialized = json.dumps(view)
    for forbidden in (
        "http://", "https://", '"token"', '"url"', "base_url", "endpoint",
        "secret", "credential", "api_key", "authorization", "bearer",
        "password", '"policy"', "override",
    ):
        assert forbidden not in serialized, f"browser metadata leaked {forbidden!r}"


# --- criterion 4: no raw-provider fallback anywhere --------------------------


def test_no_raw_provider_fallback_in_workbench_sources():
    # AGENTS.md: "Never add a raw-provider fallback." Three proofs:
    # (a) no Workbench source names a raw provider host;
    # (b) the chat-route gate performs no network I/O at all;
    # (c) the Serving client and the chat gate embed no URL scheme literal --
    #     the only base URL either can use is the operator-configured one.
    sources = sorted((_REPO_ROOT / "workbench").rglob("*.py"))
    assert len(sources) >= 15, sources
    host_markers = (
        "openai.com", "api.openai", "anthropic.com", "api.anthropic",
        "googleapis.com", "bedrock", "mistral.ai", "cohere.com", "api.cohere",
        "openrouter", "groq.com", "together.ai", "azure.com", "11434",
    )
    violations: list[str] = []
    for source in sources:
        text = source.read_text(encoding="utf-8").lower()
        for marker in host_markers:
            if marker in text:
                violations.append(f"{source.name}: {marker}")
    assert violations == []

    chat_source = (_REPO_ROOT / "workbench" / "chat_routes.py").read_text(encoding="utf-8")
    assert re.search(r"\b(urllib|http\.client|requests|httpx|aiohttp|socket|websocket)\b", chat_source) is None
    router_source = (_REPO_ROOT / "workbench" / "router.py").read_text(encoding="utf-8")
    for scheme in ("http://", "https://"):
        assert scheme not in chat_source
        assert scheme not in router_source


def test_router_refuses_when_serving_is_unconfigured_instead_of_falling_back():
    # An unconfigured Serving client must refuse outright; a fallback default
    # endpoint would be a raw-provider path.
    with pytest.raises(router.RouterError, match="not configured"):
        router.route_decisions("", "")
    with pytest.raises(router.RouterError, match="not configured"):
        router.sandbox_response("", "", "chat-fast", "hello")
    # And the module-level declared surface is data plus validators only.
    assert not hasattr(chat_routes, "urlopen")
    assert isinstance(discover_chat_routes(configured()), DiscoveredChatRoutes)
