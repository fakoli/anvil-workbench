"""Unit coverage for the observational system-health descriptors and posture
audit (preferences-configuration T003.1 / T008).

The acceptance criteria themselves are proven in ``test_security_contract.py``
(descriptor/redaction safety) and ``test_api.py`` (the read-only surface and
CLI/API parity).  This file pins the supporting edge behavior: timestamp
validation, digest determinism and sensitivity, field validation, bridge-signal
mapping, and the service lookup contract.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from workbench.config import Settings
from workbench.system_health import (
    BRIDGE_HEALTH_SIGNALS,
    IntegrationDescriptor,
    PostureCheck,
    PostureReport,
    SystemHealthService,
    UnknownIntegrationError,
    build_integration_descriptors,
    render_posture_rows,
    rfc3339,
    run_posture_audit,
)


def _settings(**overrides) -> Settings:
    base = dict(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator"}), bridge_bootstrap_token="",
        anvil_router_base_url="", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=False,
    )
    base.update(overrides)
    return Settings(**base)


def _descriptor(**overrides) -> IntegrationDescriptor:
    base = dict(
        integration_id="anvil_serving", title="Anvil Serving model plane",
        state="disabled", configured=False, owner="anvil-serving",
        remediation="Set the Serving credentials.",
    )
    base.update(overrides)
    return IntegrationDescriptor(**base)


# --- timestamp helper -------------------------------------------------------

def test_rfc3339_serializes_aware_instant_and_rejects_naive():
    assert rfc3339(datetime(2026, 7, 21, 1, 2, 3, tzinfo=timezone.utc)) == "2026-07-21T01:02:03Z"
    with pytest.raises(ValueError, match="timezone-aware"):
        rfc3339(datetime(2026, 7, 21, 1, 2, 3))


def test_descriptor_rejects_a_non_rfc3339_last_checked_at():
    with pytest.raises(ValueError, match="RFC 3339"):
        _descriptor(last_checked_at="2026-07-21 01:02:03")


# --- digest determinism & sensitivity --------------------------------------

def test_digest_excludes_timestamp_but_tracks_content():
    a = _descriptor(last_checked_at="2026-07-21T00:00:00Z")
    b = _descriptor(last_checked_at="2099-01-01T00:00:00Z")
    assert a.digest == b.digest  # timestamp is not part of the commitment
    # A content change (remediation) does move the digest.
    c = _descriptor(remediation="Different remediation text.", last_checked_at="2026-07-21T00:00:00Z")
    assert c.digest != a.digest


# --- field validation -------------------------------------------------------

def test_descriptor_rejects_state_configured_mismatch_and_bad_fields():
    with pytest.raises(ValueError, match="configured must agree with state"):
        _descriptor(state="ready", configured=False)
    with pytest.raises(ValueError, match="state must be one of"):
        _descriptor(state="haunted", configured=True)
    with pytest.raises(ValueError, match="version is invalid"):
        _descriptor(version="not a version!")
    with pytest.raises(ValueError, match="owner must be a safe"):
        _descriptor(owner="Anvil Serving!!")
    with pytest.raises(ValueError, match="dependency names an unknown integration"):
        _descriptor(dependencies=("nonexistent",))


def test_descriptor_version_and_detail_are_optional_and_serialize_only_when_present():
    minimal = _descriptor()
    data = minimal.as_dict()
    assert "version" not in data and "detail" not in data
    rich = _descriptor(version="responses/v1", detail="observed detail", state="ready", configured=True)
    rich_data = rich.as_dict()
    assert rich_data["version"] == "responses/v1" and rich_data["detail"] == "observed detail"


# --- bridge-signal mapping --------------------------------------------------

@pytest.mark.parametrize(
    "signal,expected_state",
    [(None, "disabled"), ("healthy", "ready"), ("degraded", "degraded"), ("unreachable", "degraded")],
)
def test_bridge_signal_maps_to_truthful_state(signal, expected_state):
    descriptors = {d.integration_id: d for d in build_integration_descriptors(_settings(), bridge_health=signal)}
    bridge = descriptors["project_bridge"]
    assert bridge.state == expected_state
    assert bridge.configured == (expected_state != "disabled")


def test_unknown_bridge_signal_is_rejected():
    assert "healthy" in BRIDGE_HEALTH_SIGNALS
    with pytest.raises(ValueError, match="unknown bridge health signal"):
        build_integration_descriptors(_settings(), bridge_health="on-fire")


# --- purpose retrieval requires its whole dependency chain ------------------

def test_purpose_retrieval_requires_serving_graph_and_embedding_model():
    partial = _settings(embedding_model="e", anvil_router_base_url="http://x", anvil_router_token="t")  # no neo4j
    descriptors = {d.integration_id: d for d in build_integration_descriptors(partial)}
    assert descriptors["purpose_retrieval"].state == "disabled"
    full = _settings(
        embedding_model="e", anvil_router_base_url="http://x", anvil_router_token="t", neo4j_password="p",
    )
    descriptors = {d.integration_id: d for d in build_integration_descriptors(full)}
    assert descriptors["purpose_retrieval"].state == "ready"
    assert set(descriptors["purpose_retrieval"].dependencies) == {"anvil_serving", "graph_projection"}


# --- posture report ---------------------------------------------------------

def test_posture_report_sorts_and_rejects_duplicate_ids():
    checks = (
        PostureCheck(check_id="posture.b", title="B", status="ok", severity="info", remediation="x"),
        PostureCheck(check_id="posture.a", title="A", status="ok", severity="info", remediation="x"),
    )
    report = PostureReport(checks=checks)
    assert [c.check_id for c in report.checks] == ["posture.a", "posture.b"]
    dup = (
        PostureCheck(check_id="posture.a", title="A", status="ok", severity="info", remediation="x"),
        PostureCheck(check_id="posture.a", title="A2", status="ok", severity="info", remediation="y"),
    )
    with pytest.raises(ValueError, match="unique"):
        PostureReport(checks=dup)


def test_render_posture_rows_is_timestamp_free():
    report = run_posture_audit(_settings(), checked_at="2026-07-21T00:00:00Z")
    rows = render_posture_rows(report)
    assert rows and all("2026-07-21" not in row for row in rows)
    # Each row is the stable id/status/severity/remediation tuple.
    assert rows[0].startswith("posture.")


# --- service lookup ---------------------------------------------------------

def test_service_get_returns_descriptor_or_raises_unknown():
    service = SystemHealthService(_settings(), clock=lambda: datetime(2026, 7, 21, tzinfo=timezone.utc))
    assert service.get("anvil_serving").integration_id == "anvil_serving"
    with pytest.raises(UnknownIntegrationError):
        service.get("not_declared")


def test_service_rejects_unknown_bridge_health_on_construction():
    with pytest.raises(ValueError, match="unknown bridge health signal"):
        SystemHealthService(_settings(), bridge_health="melting")
