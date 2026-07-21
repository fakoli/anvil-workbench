"""Hermetic tests for Advanced route capability + control discovery (AMP T002).

Criterion map (from ``anvil show advanced-model-playground:T002``):

1. Unsupported, out-of-bounds, policy-owned, stale, or unknown controls are
   rejected before a Serving request --
   ``test_c1_unknown_route_refused``, ``test_c1_undeclared_control_refused``,
   ``test_c1_out_of_bounds_and_type_refused``, ``test_c1_enum_not_allowed_refused``,
   ``test_c1_policy_owned_override_refused``, ``test_c1_stale_pin_invalidates``.
2. Serving credentials and raw operational endpoints never reach the browser --
   ``test_c2_config_schema_rejects_endpoint_and_credential_keys``,
   ``test_c2_browser_projection_carries_no_endpoint_or_credential``,
   ``test_c2_display_name_refuses_credential_or_host_material``,
   ``test_c2_browser_projection_last_hop_scrubs_smuggled_material``.
3. Catalog drift invalidates the request/preset rather than silently changing
   values -- ``test_c3_route_capability_repair_detects_drift``,
   ``test_c3_ready_when_no_drift``, ``test_c1_stale_pin_invalidates``.
4. Browser metadata identifies the effective source and disabled reason without
   exposing hidden policy fields -- ``test_c4_control_view_reports_source_and_disabled_reason``.

Every test is hermetic: no network, no CLI, config is an in-memory dict list.
"""
from __future__ import annotations

import pytest

from workbench import advanced_routes as ar
from workbench.advanced_routes import (
    AdvancedRouteError,
    discover_advanced_routes,
    route_capability_repair,
    validate_advanced_selection,
)

_ROUTE_DIGEST = "sha256:" + "a1" * 32
_PROFILE_DIGEST = "sha256:" + "b2" * 32


def _route_config(**overrides):
    entry = {
        "route_id": "route.chat-fast",
        "display_name": "Fast chat",
        "route_digest": _ROUTE_DIGEST,
        "profile_digest": _PROFILE_DIGEST,
        "serving_contract_version": "1.0.0",
        "model_profile": "chat-fast",
        "structured_output_supported": True,
        "tools_supported": True,
        "supported_controls": [
            {"name": "temperature_milli", "type": "int", "bounds": {"min": 0, "max": 2000}, "default": 700},
            {"name": "reasoning_effort", "type": "enum",
             "allowed_values": ["low", "medium", "high"], "default": "medium"},
            {"name": "response_streaming", "type": "bool", "default": True, "policy_owned": True},
        ],
    }
    entry.update(overrides)
    return entry


def _discovered():
    return discover_advanced_routes([_route_config()])


# --- Criterion 1: reject before a Serving request ----------------------------


def test_c1_unknown_route_refused():
    with pytest.raises(AdvancedRouteError) as exc:
        validate_advanced_selection("route.missing", {}, _discovered())
    assert exc.value.reason == ar.REASON_ROUTE_UNKNOWN


def test_c1_undeclared_control_refused():
    with pytest.raises(AdvancedRouteError) as exc:
        validate_advanced_selection("route.chat-fast", {"seed": 42}, _discovered())
    assert exc.value.reason == ar.REASON_CONTROL_UNSUPPORTED


def test_c1_out_of_bounds_and_type_refused():
    with pytest.raises(AdvancedRouteError) as exc:
        validate_advanced_selection("route.chat-fast", {"temperature_milli": 9000}, _discovered())
    assert exc.value.reason == ar.REASON_CONTROL_OUT_OF_BOUNDS

    with pytest.raises(AdvancedRouteError) as exc:
        validate_advanced_selection("route.chat-fast", {"temperature_milli": "hot"}, _discovered())
    assert exc.value.reason == ar.REASON_CONTROL_TYPE

    # A bool passed where an int is declared is refused as a type error (bool is
    # not an int here), never silently coerced.
    with pytest.raises(AdvancedRouteError) as exc:
        validate_advanced_selection("route.chat-fast", {"temperature_milli": True}, _discovered())
    assert exc.value.reason == ar.REASON_CONTROL_TYPE


def test_c1_enum_not_allowed_refused():
    with pytest.raises(AdvancedRouteError) as exc:
        validate_advanced_selection("route.chat-fast", {"reasoning_effort": "extreme"}, _discovered())
    assert exc.value.reason == ar.REASON_CONTROL_NOT_ALLOWED


def test_c1_policy_owned_override_refused():
    # response_streaming is policy_owned (default True); a crafted override to
    # False is refused on the policy-owned reason.
    with pytest.raises(AdvancedRouteError) as exc:
        validate_advanced_selection(
            "route.chat-fast",
            [{"name": "response_streaming", "value": False, "provenance": "declared"}],
            _discovered(),
        )
    assert exc.value.reason == ar.REASON_POLICY_OWNED_READONLY

    # Echoing the declared default with policy_override provenance is accepted and
    # normalized.
    selection = validate_advanced_selection(
        "route.chat-fast",
        [{"name": "response_streaming", "value": True, "provenance": "policy_override"}],
        _discovered(),
    )
    assert selection.controls == (("response_streaming", True, "policy_override"),)


def test_c1_valid_selection_is_canonical_and_within_bounds():
    selection = validate_advanced_selection(
        "route.chat-fast",
        {"reasoning_effort": "high", "temperature_milli": 300},
        _discovered(),
    )
    # Sorted by name; provenance defaults to declared for actor-set controls.
    assert selection.controls == (
        ("reasoning_effort", "high", "declared"),
        ("temperature_milli", 300, "declared"),
    )
    assert selection.controls_dict() == {"reasoning_effort": "high", "temperature_milli": 300}
    # The submitted_controls projection round-trips into the branch shape.
    assert selection.submitted_controls() == [
        {"name": "reasoning_effort", "value": "high", "provenance": "declared"},
        {"name": "temperature_milli", "value": 300, "provenance": "declared"},
    ]


def test_c1_stale_pin_invalidates():
    discovered = _discovered()
    stale = {
        "route_id": "route.chat-fast",
        "route_digest": "sha256:" + "cc" * 32,  # no longer matches live
        "profile_digest": _PROFILE_DIGEST,
    }
    with pytest.raises(AdvancedRouteError) as exc:
        validate_advanced_selection("route.chat-fast", {}, discovered, pinned=stale)
    assert exc.value.reason == ar.REASON_ROUTE_DIGEST_DRIFT

    stale_profile = {"route_id": "route.chat-fast", "profile_digest": "sha256:" + "dd" * 32}
    with pytest.raises(AdvancedRouteError) as exc:
        validate_advanced_selection("route.chat-fast", {}, discovered, pinned=stale_profile)
    assert exc.value.reason == ar.REASON_PROFILE_DIGEST_DRIFT

    # A matching pin is accepted.
    fresh = {"route_id": "route.chat-fast", "route_digest": _ROUTE_DIGEST, "profile_digest": _PROFILE_DIGEST}
    validate_advanced_selection("route.chat-fast", {}, discovered, pinned=fresh)


def test_c1_selection_requires_the_frozen_snapshot():
    with pytest.raises(AdvancedRouteError) as exc:
        validate_advanced_selection("route.chat-fast", {}, {"routes": []})  # type: ignore[arg-type]
    assert exc.value.reason == ar.REASON_MALFORMED


# --- Criterion 2: no credentials/endpoints reach the browser -----------------


def test_c2_config_schema_rejects_endpoint_and_credential_keys():
    for leak in ("endpoint", "base_url", "url", "token", "api_key", "authorization", "credential", "policy"):
        with pytest.raises(AdvancedRouteError) as exc:
            discover_advanced_routes([_route_config(**{leak: "x"})])
        assert exc.value.reason == ar.REASON_MALFORMED


def test_c2_browser_projection_carries_no_endpoint_or_credential():
    projection = _discovered().browser_projection()
    serialized = repr(projection).lower()
    for forbidden in ("http", "bearer", "endpoint", "://", "secret", "token", "credential", "password"):
        assert forbidden not in serialized
    route = projection["routes"][0]
    # Identifiers, digests, and control metadata only -- no endpoint/policy field.
    assert set(route) == {
        "provider", "route_id", "display_name", "route_digest", "profile_digest",
        "serving_contract_version", "model_profile",
        "structured_output_supported", "tools_supported", "controls",
    }
    assert route["route_digest"] == _ROUTE_DIGEST  # digest survives the scrub


def test_c2_display_name_refuses_credential_or_host_material():
    for bad in ("Bearer sk-secret", "serving.tail1234.ts.net", "sk-ant-api03-XXXXXXXXXXXXXXXXXXXX"):
        with pytest.raises(AdvancedRouteError) as exc:
            discover_advanced_routes([_route_config(display_name=bad)])
        assert exc.value.reason == ar.REASON_MALFORMED


def test_c2_browser_projection_last_hop_scrubs_smuggled_material():
    # The display charset already forbids URL/path punctuation, but the browser
    # projection additionally runs the config-text last-hop scrub, so even if a
    # descriptor were constructed directly with a smuggled endpoint/path it is
    # scrubbed in the SERVED projection (defense in depth).
    descriptor = ar.AdvancedRouteCapability(
        route_id="route.chat-fast",
        display_name="opened C:/Users/op/.anvil/state.db via https://serving.internal:8443",
        route_digest=_ROUTE_DIGEST,
        profile_digest=_PROFILE_DIGEST,
        serving_contract_version="1.0.0",
        model_profile="chat-fast",
        supported_controls=(
            ar.AdvancedControlDescriptor(name="temperature_milli", type="int",
                                         default=700, bounds=(0, 2000)),
        ),
    )
    served = descriptor.browser_projection()
    display = served["display_name"]
    assert "https://serving.internal" not in display
    assert "C:/Users" not in display
    assert "state.db" not in display
    assert "[REDACTED" in display  # the smuggled material was scrubbed at the last hop
    # The digest is delimiter-anchored and survives intact.
    assert served["route_digest"] == _ROUTE_DIGEST


# --- Criterion 3: catalog drift invalidates, never silently substitutes ------


def test_c3_route_capability_repair_detects_drift():
    discovered = _discovered()
    # Route digest drift.
    pinned = {"route_id": "route.chat-fast", "route_digest": "sha256:" + "cc" * 32,
              "profile_digest": _PROFILE_DIGEST}
    repair = route_capability_repair(pinned, discovered)
    assert repair["status"] == "repair_required"
    assert repair["drifted_refs"] == [
        {"ref_kind": "route", "id": "route.chat-fast", "pinned_digest": "sha256:" + "cc" * 32}
    ]

    # A route that vanished from the catalog drifts on both refs, never substituted.
    gone = {"route_id": "route.deleted", "route_digest": _ROUTE_DIGEST, "profile_digest": _PROFILE_DIGEST}
    repair_gone = route_capability_repair(gone, discovered)
    assert repair_gone["status"] == "repair_required"
    assert {ref["ref_kind"] for ref in repair_gone["drifted_refs"]} == {"route", "profile"}


def test_c3_ready_when_no_drift():
    discovered = _discovered()
    pinned = {"route_id": "route.chat-fast", "route_digest": _ROUTE_DIGEST, "profile_digest": _PROFILE_DIGEST}
    assert route_capability_repair(pinned, discovered) == {"status": "ready"}


def test_c3_repair_required_when_route_vanished_without_digests():
    # Drift must invalidate, never silently pass (crit 3). A pin naming a route
    # absent from the live catalog -- even with NO digests to compare -- must fail
    # closed as repair_required, not fail OPEN to {"status": "ready"}.
    discovered = _discovered()
    gone = {"route_id": "route.deleted"}  # not in the catalog, no pinned digests
    repair = route_capability_repair(gone, discovered)
    assert repair["status"] == "repair_required"
    assert {ref["ref_kind"] for ref in repair["drifted_refs"]} == {"route", "profile"}


def test_c3_repair_required_when_pinned_digest_is_not_a_valid_digest_string():
    # A pinned ref that is missing or a non-string / malformed digest cannot be
    # verified against the live catalog; it must fail closed as repair_required,
    # never be skipped into a silent "ready".
    discovered = _discovered()
    # Non-string route_digest (profile matches live).
    non_string = {"route_id": "route.chat-fast", "route_digest": 12345, "profile_digest": _PROFILE_DIGEST}
    repair = route_capability_repair(non_string, discovered)
    assert repair["status"] == "repair_required"
    assert any(ref["ref_kind"] == "route" for ref in repair["drifted_refs"])

    # Missing route_digest entirely (present live route) also fails closed.
    missing = {"route_id": "route.chat-fast", "profile_digest": _PROFILE_DIGEST}
    assert route_capability_repair(missing, discovered)["status"] == "repair_required"

    # A malformed (non-sha256) digest string is likewise unverifiable -> repair.
    malformed = {"route_id": "route.chat-fast", "route_digest": "not-a-digest",
                 "profile_digest": _PROFILE_DIGEST}
    assert route_capability_repair(malformed, discovered)["status"] == "repair_required"


# --- Criterion 4: browser metadata identifies source + disabled reason -------


def test_c4_control_view_reports_source_and_disabled_reason():
    route = _discovered().route("route.chat-fast")
    views = {v["name"]: v for v in (c.control_view() for c in route.supported_controls)}

    temp = views["temperature_milli"]
    assert temp["editable"] is True
    assert temp["source"] == "route_default"
    assert temp["disabled_reason"] is None
    assert temp["bounds"] == {"min": 0, "max": 2000}

    streaming = views["response_streaming"]
    assert streaming["editable"] is False
    assert streaming["source"] == "policy_owned"
    assert streaming["disabled_reason"] == "policy_owned"
    # No hidden policy field leaks into the view.
    assert set(streaming) == {"name", "type", "default", "editable", "source", "disabled_reason"}


def test_c4_as_route_capability_round_trips_into_the_branch_shape():
    from workbench.contracts import validate_advanced_branch

    route = _discovered().route("route.chat-fast")
    selection = validate_advanced_selection(
        "route.chat-fast",
        [{"name": "temperature_milli", "value": 300, "provenance": "declared"},
         {"name": "response_streaming", "value": True, "provenance": "policy_override"}],
        _discovered(),
    )
    branch = {
        "schema_version": "workbench-advanced-branch/v1",
        "branch_id": "advbranch_capability_0001",
        "mode": "advanced",
        "conversation_ref": {
            "binding": "existing_conversation",
            "conversation_id": "conv_advanced_playground_0001",
            "fork_point": {"parent_turn_id": "turn_userroot_0001"},
        },
        "retention": {"class": "durable", "saved_at": "2026-07-20T10:05:00Z"},
        "route_capability": route.as_route_capability(),
        "submitted_controls": selection.submitted_controls(),
        "repair": {"status": "ready"},
        "created_at": "2026-07-20T10:00:00Z",
    }
    # The discovery-produced capability + validated selection is accepted by the
    # merged AMP:T001 branch validator through the wired path (not hand-built).
    validate_advanced_branch(branch)


def test_discovery_rejects_malformed_controls():
    for bad_control in (
        {"name": "seed", "type": "int", "default": 7},  # int without bounds
        {"name": "truncation", "type": "enum", "default": "auto"},  # enum without allowed_values
        {"name": "x", "type": "int", "bounds": {"min": 10, "max": 0}, "default": 5},  # inverted bounds
        {"name": "x", "type": "int", "bounds": {"min": 0, "max": 10}, "default": 99},  # default out of bounds
        {"name": "x", "type": "bool", "default": "yes"},  # bool default not a bool
        {"name": "x", "type": "int", "bounds": {"min": 0, "max": 10}, "default": 5, "extra": 1},  # extra key
    ):
        with pytest.raises(AdvancedRouteError) as exc:
            discover_advanced_routes([_route_config(supported_controls=[bad_control])])
        assert exc.value.reason == ar.REASON_MALFORMED
