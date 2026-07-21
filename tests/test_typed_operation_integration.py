"""End-to-end qualification of the typed-operation spine (state-context-operations:T006).

One in-process, hermetic fixture threads a profiled typed operation through the
whole critical path built by T006.1 / T006.2 / T006.3:

    model operation proposal
      -> hub descriptor resolution      (workbench.workflows.resolve_operation_request, T006.1)
      -> hub builds an invoke_operation bridge command
      -> immediate bridge authority preflight  (workbench.bridge.preflight_operation_command, T006.2)
      -> adapter dispatch (simulated locally on the bridge)
      -> durable idempotent typed receipt / reconciliation (workbench.store, T006.3)

It proves the happy path (non-gated and approval-gated) and every fail-closed
gate the acceptance criteria name: a stale catalog digest, a lost/expired lease,
a mismatched work packet, a replayed one-time approval, an unprofiled capability,
and an unknown external outcome -- each stops before or reconciles the effect and
never silently repeats it.  Nothing here is wired into the live bridge poll loop;
the pipeline is assembled explicitly in the test.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pytest

from workbench.bridge import OperationLeaseState, preflight_operation_command
from workbench.contracts import approval_payload_digest, validate_operation_receipt
from workbench.models import TypedOperationError
from workbench.store import (
    MemoryOperationApprovalStore,
    MemoryOperationReceiptStore,
    OperationOutcome,
    UnknownOutcomeError,
)
from workbench.workflows import resolve_operation_request

from _support import (
    capability_profile_document,
    compile_delivery_snapshot,
    invoke_operation_command,
    local_catalogs,
    operation_ref_for,
    published_catalog_set,
)

_NOW = datetime(2026, 7, 19, 0, 0, 0, tzinfo=timezone.utc)
_SUBMIT_INPUTS = {"task_ref": "release-beta:T001", "verification_receipt_ids": ["rcpt_v"]}
_COMMIT_INPUTS = {"diff_hash": "a" * 64, "branch": "codex/x", "title": "Anvil Workbench delivery", "base": "main"}


def _proposal(operation_id: str, input_obj: dict) -> dict:
    return {
        "schema_version": "workbench-model-proposal/v1",
        "kind": "operation_request",
        "reason": "Advance the delivery workflow.",
        "operation": operation_ref_for(operation_id),
        "input": input_obj,
    }


@dataclass
class TypedOperationHarness:
    """The wired, hermetic typed-operation pipeline reused by every test."""

    snapshot: Any
    published: Any
    catalogs: dict
    profile: dict
    approvals: MemoryOperationApprovalStore
    receipts: MemoryOperationReceiptStore
    lease_authority: Callable[[str], OperationLeaseState | None]
    dispatched: list[str]

    def dispatch(
        self,
        proposal: dict,
        *,
        idempotency_key: str,
        adapter: Callable[[str, dict], OperationOutcome],
        grant_id: str | None = None,
        action: str | None = None,
        command_mutator: Callable[[dict], None] | None = None,
        pinned_work_packet_digest: str | None = None,
        current_work_packet_digest: str | None = None,
    ) -> tuple[dict, bool]:
        # 1. Hub-side descriptor resolution (T006.1): a malformed / unprofiled /
        #    drifted proposal raises here, before any command is even built.
        resolved = resolve_operation_request(proposal, self.snapshot, self.published)

        # 2. Hub builds the pinned invoke_operation bridge command.
        payload_hash = approval_payload_digest(dict(resolved.inputs)) if grant_id else None
        command = invoke_operation_command(
            self.snapshot, operation_id=resolved.operation.id, inputs=dict(resolved.inputs),
            grant_id=grant_id, action=action, payload_hash=payload_hash,
        )
        if command_mutator is not None:
            command_mutator(command)

        # 3-6. The bridge preflight + adapter dispatch run INSIDE the idempotent
        #      receipt store, so a replay of the same key never re-preflights or
        #      re-dispatches.  A preflight refusal becomes a typed `denied`
        #      receipt (retriable), never a silent pass.
        def executor() -> OperationOutcome:
            try:
                pre = preflight_operation_command(
                    command, catalogs=self.catalogs, profile=self.profile,
                    lease_authority=self.lease_authority, approval_consumer=self.approvals, now=_NOW,
                    pinned_work_packet_digest=pinned_work_packet_digest,
                    current_work_packet_digest=current_work_packet_digest,
                )
            except TypedOperationError as exc:
                return OperationOutcome("denied", error=exc.refusal)
            self.dispatched.append(pre.bridge_adapter)
            return adapter(pre.bridge_adapter, dict(pre.inputs))

        return self.receipts.record_attempt(
            run_id=command["run_id"], command_id=command["command_id"], operation=resolved.operation,
            idempotency_key=idempotency_key, executor=executor, task_ref="release-beta:T001",
        )


def _harness(*, lease_epoch: int = 3, lease_minutes: int = 5) -> TypedOperationHarness:
    def authority(worktree_name: str) -> OperationLeaseState:
        return OperationLeaseState(worktree_name, lease_epoch, _NOW + timedelta(minutes=lease_minutes))

    return TypedOperationHarness(
        snapshot=compile_delivery_snapshot(),
        published=published_catalog_set(),
        catalogs=local_catalogs(),
        profile=capability_profile_document(),
        approvals=MemoryOperationApprovalStore(),
        receipts=MemoryOperationReceiptStore(),
        lease_authority=authority,
        dispatched=[],
    )


def _succeed(adapter: str, inputs: dict) -> OperationOutcome:
    return OperationOutcome("succeeded", external_ref={"adapter": adapter.replace(".", "_")},
                            evidence_refs=("state_event_evt1",))


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_profiled_non_gated_operation_flows_to_a_durable_success_receipt():
    harness = _harness()
    receipt, replayed = harness.dispatch(
        _proposal("state.evidence.submit", _SUBMIT_INPUTS),
        idempotency_key="run:run_example:evidence:1", adapter=_succeed,
    )
    assert harness.dispatched == ["state.cli.submit_evidence"]
    assert receipt["status"] == "succeeded"
    assert replayed is False
    validate_operation_receipt(receipt)
    assert receipt["operation"]["id"] == "state.evidence.submit"


def test_approval_gated_operation_consumes_one_time_grant_then_dispatches():
    harness = _harness()
    harness.approvals.grant(
        "approval_typedop_00000001", "commit_pr", approval_payload_digest(_COMMIT_INPUTS),
        "bridge_example", "project_example",
    )
    receipt, _ = harness.dispatch(
        _proposal("bridge.github.commit_pr", _COMMIT_INPUTS),
        idempotency_key="run:run_example:commit:1", adapter=_succeed,
        grant_id="approval_typedop_00000001", action="commit_pr",
    )
    assert harness.dispatched == ["bridge.github.commit_pr"]
    assert receipt["status"] == "succeeded"


def test_idempotent_replay_returns_the_receipt_without_re_dispatching():
    harness = _harness()
    first, replayed_first = harness.dispatch(
        _proposal("state.evidence.submit", _SUBMIT_INPUTS),
        idempotency_key="run:run_example:evidence:1", adapter=_succeed,
    )
    second, replayed_second = harness.dispatch(
        _proposal("state.evidence.submit", _SUBMIT_INPUTS),
        idempotency_key="run:run_example:evidence:1", adapter=_succeed,
    )
    assert harness.dispatched == ["state.cli.submit_evidence"]  # dispatched exactly once
    assert replayed_first is False and replayed_second is True
    assert first["receipt_id"] == second["receipt_id"]


# ---------------------------------------------------------------------------
# Fail-closed gates (no effect / reconcile, never a silent repeat)
# ---------------------------------------------------------------------------


def test_stale_catalog_digest_fails_closed_before_dispatch():
    harness = _harness()

    def stale(command: dict) -> None:
        for entry in command["workflow_snapshot"]["catalogs"]:
            if entry["provider"] == "anvil-state":
                entry["digest"] = "sha256:" + "0" * 64

    receipt, _ = harness.dispatch(
        _proposal("state.evidence.submit", _SUBMIT_INPUTS),
        idempotency_key="k-stale", adapter=_succeed, command_mutator=stale,
    )
    assert harness.dispatched == []
    assert receipt["status"] == "denied"
    assert receipt["error"]["code"] == "operation.digest_drift"


def test_lost_lease_fails_closed_before_dispatch():
    harness = _harness()
    harness.lease_authority = lambda name: None
    receipt, _ = harness.dispatch(
        _proposal("state.evidence.submit", _SUBMIT_INPUTS),
        idempotency_key="k-lease", adapter=_succeed,
    )
    assert harness.dispatched == []
    assert receipt["status"] == "denied"
    assert receipt["error"]["code"] == "lease.missing"


def test_fenced_lease_epoch_fails_closed_before_dispatch():
    harness = _harness(lease_epoch=9)
    receipt, _ = harness.dispatch(
        _proposal("state.evidence.submit", _SUBMIT_INPUTS),
        idempotency_key="k-epoch", adapter=_succeed,
    )
    assert harness.dispatched == []
    assert receipt["error"]["code"] == "lease.epoch_mismatch"


def test_changed_work_packet_fails_closed_before_dispatch():
    harness = _harness()
    receipt, _ = harness.dispatch(
        _proposal("state.evidence.submit", _SUBMIT_INPUTS),
        idempotency_key="k-packet", adapter=_succeed,
        pinned_work_packet_digest="sha256:" + "8" * 64,
        current_work_packet_digest="sha256:" + "9" * 64,
    )
    assert harness.dispatched == []
    assert receipt["error"]["code"] == "work_packet.digest_changed"


def test_replayed_approval_fails_closed_without_a_second_effect():
    harness = _harness()
    harness.approvals.grant(
        "approval_typedop_00000001", "commit_pr", approval_payload_digest(_COMMIT_INPUTS),
        "bridge_example", "project_example",
    )
    first, _ = harness.dispatch(
        _proposal("bridge.github.commit_pr", _COMMIT_INPUTS),
        idempotency_key="run:run_example:commit:1", adapter=_succeed,
        grant_id="approval_typedop_00000001", action="commit_pr",
    )
    assert first["status"] == "succeeded"
    # A NEW attempt (different idempotency key) reusing the consumed grant must
    # fail closed and never dispatch the external effect a second time.
    second, _ = harness.dispatch(
        _proposal("bridge.github.commit_pr", _COMMIT_INPUTS),
        idempotency_key="run:run_example:commit:2", adapter=_succeed,
        grant_id="approval_typedop_00000001", action="commit_pr",
    )
    assert harness.dispatched == ["bridge.github.commit_pr"]  # exactly one effect
    assert second["status"] == "denied"
    assert second["error"]["code"] == "approval.invalid"


def test_unprofiled_capability_is_refused_at_resolution():
    harness = _harness()
    with pytest.raises(TypedOperationError) as excinfo:
        harness.dispatch(
            _proposal("state.project.snapshot", {}),
            idempotency_key="k-unprofiled", adapter=_succeed,
        )
    assert excinfo.value.code == "operation.unprofiled"
    assert harness.dispatched == []


def test_unknown_external_outcome_reconciles_and_never_retries():
    harness = _harness()
    harness.approvals.grant(
        "approval_typedop_00000001", "commit_pr", approval_payload_digest(_COMMIT_INPUTS),
        "bridge_example", "project_example",
    )

    def interrupted(adapter: str, inputs: dict) -> OperationOutcome:
        raise UnknownOutcomeError(
            "the merge effect outcome is unknown", external_ref={"pr": "gh:1"}, reason="interrupted",
        )

    receipt, _ = harness.dispatch(
        _proposal("bridge.github.commit_pr", _COMMIT_INPUTS),
        idempotency_key="run:run_example:commit:1", adapter=interrupted,
        grant_id="approval_typedop_00000001", action="commit_pr",
    )
    assert receipt["status"] == "reconciliation_required"
    items = harness.receipts.list_reconciliations()
    assert len(items) == 1 and items[0]["reason"] == "interrupted"

    # Recovery: replaying the same key returns the stored reconciliation receipt
    # and does NOT re-run the unknown external effect.
    replay, replayed = harness.dispatch(
        _proposal("bridge.github.commit_pr", _COMMIT_INPUTS),
        idempotency_key="run:run_example:commit:1", adapter=interrupted,
        grant_id="approval_typedop_00000001", action="commit_pr",
    )
    assert replayed is True
    assert replay["receipt_id"] == receipt["receipt_id"]
    assert len(harness.receipts.list_reconciliations()) == 1


def test_every_terminal_receipt_is_schema_valid_and_redacted():
    harness = _harness()
    receipt, _ = harness.dispatch(
        _proposal("state.evidence.submit", _SUBMIT_INPUTS),
        idempotency_key="k-valid", adapter=_succeed,
    )
    validate_operation_receipt(receipt)
    assert receipt["redaction"]["status"] in ("redacted", "metadata_only")
    assert receipt["correlation"]["task_id"] == "release-beta:T001"
