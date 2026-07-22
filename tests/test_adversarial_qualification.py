"""Adversarial qualification index (state-context-operations:T007).

This is the single consolidating fixture the qualification record cites for the
run's headline hermetic guarantee: across the typed-operation spine, every
adversarial input **fails closed with a stable typed refusal code** (a declared
member of the closed :data:`workbench.models.OPERATION_REFUSAL_CODES` set) and
**attempts no external effect** (no adapter dispatch, no ``succeeded`` receipt,
no reconciliation from a gate that should have refused earlier).

It does not re-prove each gate in isolation --
``tests/test_typed_operation_integration.py`` does that, per fail-closed case,
and the redaction / no-existence-oracle / closed-schema / actor-scope / budget
/ one-time-approval dimensions are proven across ``test_security_contract.py``,
``test_harness_kernel.py``, ``test_run_context_adversarial.py``,
``test_project_context_adversarial.py``, ``test_workflow_snapshot.py``, and the
per-lane suites.  What this file adds is the *cross-surface invariant*: the same
two properties (declared-stable code + zero external effect) hold uniformly for
every operation-spine refusal, so a future rename of a refusal code, or a gate
that dispatches before it refuses, fails this qualification even if its own
local test were adjusted to match.

No network, no ``state.db``, no subprocess: the whole pipeline is the injected,
in-process harness assembled in the sibling integration module.
"""
from __future__ import annotations

import pytest

from workbench.models import OPERATION_REFUSAL_CODES, TypedOperationError

# Reuse the PROVEN, wired hermetic pipeline rather than a parallel one.  Only the
# non-``test_`` helpers are imported, so pytest does not double-collect the
# sibling's own cases here.
from test_typed_operation_integration import (
    _COMMIT_INPUTS,
    _SUBMIT_INPUTS,
    _harness,
    _proposal,
    _succeed,
)
from workbench.contracts import approval_payload_digest


def _refusal_code_and_dispatched(run) -> tuple[str, list[str]]:
    """Drive one adversarial scenario; return its typed code + the dispatch log.

    ``run(harness)`` performs exactly one ``harness.dispatch(...)``.  A refusal
    surfaces either as a ``denied`` receipt (bridge/hub preflight) or as a raised
    :class:`TypedOperationError` (hub-side resolution before a command exists);
    both carry a stable code and both must have dispatched nothing.
    """
    harness = _harness(**getattr(run, "harness_kwargs", {}))
    for grant in getattr(run, "grants", ()):  # pre-consume a one-time grant if needed
        harness.approvals.grant(*grant)
    try:
        receipt, _ = run(harness)
    except TypedOperationError as exc:
        return exc.code, list(harness.dispatched)
    assert receipt["status"] == "denied", f"expected a denied receipt, got {receipt['status']!r}"
    return receipt["error"]["code"], list(harness.dispatched)


# --- The adversarial scenarios, one per operation-spine refusal family. --------


def _stale_catalog(harness):
    def stale(command: dict) -> None:
        for entry in command["workflow_snapshot"]["catalogs"]:
            if entry["provider"] == "anvil-state":
                entry["digest"] = "sha256:" + "0" * 64

    return harness.dispatch(
        _proposal("state.evidence.submit", _SUBMIT_INPUTS),
        idempotency_key="q-stale", adapter=_succeed, command_mutator=stale,
    )


def _lost_lease(harness):
    harness.lease_authority = lambda name: None
    return harness.dispatch(
        _proposal("state.evidence.submit", _SUBMIT_INPUTS),
        idempotency_key="q-lease", adapter=_succeed,
    )


def _fenced_epoch(harness):
    return harness.dispatch(
        _proposal("state.evidence.submit", _SUBMIT_INPUTS),
        idempotency_key="q-epoch", adapter=_succeed,
    )
_fenced_epoch.harness_kwargs = {"lease_epoch": 9}


def _changed_packet(harness):
    return harness.dispatch(
        _proposal("state.evidence.submit", _SUBMIT_INPUTS),
        idempotency_key="q-packet", adapter=_succeed,
        pinned_work_packet_digest="sha256:" + "8" * 64,
        current_work_packet_digest="sha256:" + "9" * 64,
    )


def _replayed_approval(harness):
    # The single grant is consumed by the first dispatch; the SECOND reuse fails
    # closed with no second effect.
    first, _ = harness.dispatch(
        _proposal("bridge.github.commit_pr", _COMMIT_INPUTS),
        idempotency_key="q-commit-1", adapter=_succeed,
        grant_id="approval_typedop_00000001", action="commit_pr",
    )
    assert first["status"] == "succeeded" and harness.dispatched == ["bridge.github.commit_pr"]
    second = harness.dispatch(
        _proposal("bridge.github.commit_pr", _COMMIT_INPUTS),
        idempotency_key="q-commit-2", adapter=_succeed,
        grant_id="approval_typedop_00000001", action="commit_pr",
    )
    # After the replay, exactly ONE external effect ever dispatched.
    assert harness.dispatched == ["bridge.github.commit_pr"]
    return second
_replayed_approval.grants = (
    ("approval_typedop_00000001", "commit_pr", approval_payload_digest(_COMMIT_INPUTS),
     "bridge_example", "project_example"),
)


def _unprofiled(harness):
    # Refused at hub-side resolution -- raises before any command is built.
    return harness.dispatch(
        _proposal("state.project.snapshot", {}),
        idempotency_key="q-unprofiled", adapter=_succeed,
    )


_SCENARIOS = {
    "operation.digest_drift": _stale_catalog,
    "lease.missing": _lost_lease,
    "lease.epoch_mismatch": _fenced_epoch,
    "work_packet.digest_changed": _changed_packet,
    "approval.invalid": _replayed_approval,
    "operation.unprofiled": _unprofiled,
}


@pytest.mark.parametrize("expected_code, scenario", list(_SCENARIOS.items()))
def test_operation_spine_refusal_fails_closed_with_a_stable_code_and_no_effect(
    expected_code, scenario,
):
    """Every operation-spine adversarial input: declared-stable code + no effect."""
    code, dispatched = _refusal_code_and_dispatched(scenario)

    # (1) STABLE code: the refusal names a DECLARED member of the closed set, not
    #     an ad-hoc string.  A rename that forgets to keep the old member fails.
    assert code in OPERATION_REFUSAL_CODES, f"{code!r} is not a declared refusal code"
    # And it is the exact code this surface is contracted to raise.
    assert code == expected_code

    # (2) NO external effect: for a scenario whose refusal precedes any legitimate
    #     effect, nothing dispatched at all; the one scenario that legitimately
    #     dispatches once (the approval replay) proved `== ["bridge.github.commit_pr"]`
    #     inside its body -- never a SECOND effect from the refused reuse.
    if scenario is _replayed_approval:
        assert dispatched == ["bridge.github.commit_pr"]
    else:
        assert dispatched == []


def test_every_scenario_code_is_a_declared_member_of_the_closed_set():
    """The qualification's expected codes are a subset of the frozen closed set.

    Guards the set's shape (it is the durable receipt/reconciliation vocabulary):
    a member renamed or dropped without updating the qualification is caught here,
    not silently tolerated.
    """
    assert isinstance(OPERATION_REFUSAL_CODES, frozenset)
    assert set(_SCENARIOS).issubset(OPERATION_REFUSAL_CODES)
