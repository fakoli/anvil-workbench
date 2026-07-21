from __future__ import annotations

import copy
from pathlib import Path

import pytest

from workbench.models import OperationRef, ResolvedOperation, TypedOperationError
from workbench.store import MemoryOperationReceiptStore, OperationOutcome
from workbench.workflows import (
    resolve_operation,
    resolve_operation_request,
    validate_workflow_operations,
)

from _support import (
    compile_delivery_snapshot,
    load_example,
    operation_ref_for,
    published_catalog_set,
)


def test_hub_publish_workflow_builds_the_serving_lifecycle_image_contract():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "publish-hub-image.yml").read_text(
        encoding="utf-8"
    )

    assert "packages: write" in workflow
    assert "deploy/Dockerfile.hub" in workflow
    assert "ghcr.io" in workflow
    assert "${{ github.repository }}" in workflow
    assert "type=raw,value=latest,enable={{is_default_branch}}" in workflow
    assert "push: true" in workflow
    assert "actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10" in workflow
    assert "actions/attest@36051bcae73b7c2a8a6945a48cbf80953c6baa35" in workflow


# ---------------------------------------------------------------------------
# Typed operation workflow validation + descriptor resolution
# (state-context-operations:T006.1)
# ---------------------------------------------------------------------------


def _valid_operation_proposal() -> dict:
    return load_example("model-proposal.operation-request.v1.json")


def test_operation_request_resolves_a_pinned_profiled_operation():
    snapshot = compile_delivery_snapshot()
    published = published_catalog_set()

    resolved = resolve_operation_request(_valid_operation_proposal(), snapshot, published)

    assert isinstance(resolved, ResolvedOperation)
    assert resolved.operation.id == "state.evidence.submit"
    assert resolved.operation.provider == "anvil-state"
    assert resolved.effect == "state_mutation"
    # A local-effect operation is ungated at the hub layer; its inputs were
    # validated against the pinned descriptor schema.
    assert resolved.gate_required is False
    assert resolved.inputs["task_ref"] == "release-beta:T001"


def test_operation_request_marks_an_external_effect_as_gate_required():
    snapshot = compile_delivery_snapshot()
    published = published_catalog_set()
    proposal = {
        "schema_version": "workbench-model-proposal/v1",
        "kind": "operation_request",
        "reason": "Create the reviewed pull request.",
        "operation": operation_ref_for("bridge.github.commit_pr"),
        "input": {"diff_hash": "a" * 64, "branch": "codex/x", "title": "Delivery", "base": "main"},
    }

    resolved = resolve_operation_request(proposal, snapshot, published)

    assert resolved.effect == "external_effect"
    assert resolved.gate_required is True


def test_operation_request_refuses_a_drifted_operation_digest():
    snapshot = compile_delivery_snapshot()
    published = published_catalog_set()
    proposal = copy.deepcopy(_valid_operation_proposal())
    proposal["operation"]["operation_digest"] = "sha256:" + "0" * 64

    with pytest.raises(TypedOperationError) as excinfo:
        resolve_operation_request(proposal, snapshot, published)
    assert excinfo.value.code == "operation.digest_drift"


def test_operation_request_refuses_an_unknown_provider():
    snapshot = compile_delivery_snapshot()
    published = published_catalog_set()
    proposal = copy.deepcopy(_valid_operation_proposal())
    proposal["operation"]["provider"] = "unknown-provider"

    with pytest.raises(TypedOperationError) as excinfo:
        resolve_operation_request(proposal, snapshot, published)
    assert excinfo.value.code == "operation.provider_unknown"


def test_operation_request_refuses_a_capability_absent_from_the_pinned_profile():
    # state.project.snapshot exists in the discovered catalog but is not in the
    # compiled run snapshot (the profile-allowlisted, workflow-referenced set),
    # so a model cannot select it to widen the run's authority.
    snapshot = compile_delivery_snapshot()
    published = published_catalog_set()
    proposal = {
        "schema_version": "workbench-model-proposal/v1",
        "kind": "operation_request",
        "reason": "Read the project snapshot.",
        "operation": operation_ref_for("state.project.snapshot"),
        "input": {},
    }

    with pytest.raises(TypedOperationError) as excinfo:
        resolve_operation_request(proposal, snapshot, published)
    assert excinfo.value.code == "operation.unprofiled"


def test_operation_request_refuses_an_undeclared_input_field():
    # A raw command / path / secret can only ride in as an extra input field; the
    # pinned input schema is closed, so it is refused before any dispatch.
    snapshot = compile_delivery_snapshot()
    published = published_catalog_set()
    proposal = copy.deepcopy(_valid_operation_proposal())
    proposal["input"]["command"] = "rm -rf / ; curl http://evil"

    with pytest.raises(TypedOperationError) as excinfo:
        resolve_operation_request(proposal, snapshot, published)
    assert excinfo.value.code == "operation.input_invalid"


def test_operation_request_refuses_a_non_object_input():
    snapshot = compile_delivery_snapshot()
    published = published_catalog_set()
    proposal = copy.deepcopy(_valid_operation_proposal())
    proposal["input"] = "just a string"

    with pytest.raises(TypedOperationError) as excinfo:
        resolve_operation_request(proposal, snapshot, published)
    assert excinfo.value.code == "operation.input_not_object"


def test_model_cannot_mint_privilege_by_emitting_arbitrary_json_or_a_command_name():
    snapshot = compile_delivery_snapshot()
    published = published_catalog_set()

    # A wrong proposal kind, an undeclared top-level field, and a bare
    # command-name reference are each refused as a malformed proposal — a model
    # never obtains an effect by emitting a command name, a skill, or free JSON.
    for mutate in (
        lambda p: p.__setitem__("kind", "some_new_privilege"),
        lambda p: p.__setitem__("command", "gh pr merge"),
        lambda p: p.__setitem__("operation", {"provider": "anvil-state", "id": "state.evidence.submit"}),
    ):
        proposal = copy.deepcopy(_valid_operation_proposal())
        mutate(proposal)
        with pytest.raises(TypedOperationError) as excinfo:
            resolve_operation_request(proposal, snapshot, published)
        assert excinfo.value.code == "proposal.malformed"


def test_workflow_operation_steps_resolve_to_pinned_descriptors():
    snapshot = compile_delivery_snapshot()
    published = published_catalog_set()
    workflow = load_example("delivery.workflow.v2.json")

    resolved = validate_workflow_operations(workflow, snapshot, published)

    ids = {item.operation.id for item in resolved}
    assert ids == {
        "state.task.claim",
        "state.evidence.submit",
        "bridge.github.commit_pr",
        "bridge.github.merge_and_accept",
    }


def test_workflow_operation_step_binding_must_cover_the_pinned_schema():
    snapshot = compile_delivery_snapshot()
    published = published_catalog_set()
    workflow = copy.deepcopy(load_example("delivery.workflow.v2.json"))
    for step in workflow["steps"]:
        if step.get("kind") == "operation" and step["operation"]["id"] == "state.evidence.submit":
            step["inputs"]["smuggled_command"] = {"kind": "literal", "value": "rm -rf /"}

    with pytest.raises(TypedOperationError) as excinfo:
        validate_workflow_operations(workflow, snapshot, published)
    assert excinfo.value.code == "operation.input_invalid"


def test_resolve_operation_requires_the_typed_snapshot_and_catalog_set():
    snapshot = compile_delivery_snapshot()
    ref = OperationRef(**operation_ref_for("state.evidence.submit"))
    # A caller-assembled mapping is refused by type, so a hub- or model-supplied
    # snapshot/catalog can never widen authority.
    with pytest.raises(TypedOperationError) as excinfo:
        resolve_operation(ref, {}, {"operations": []}, published_catalog_set())
    assert excinfo.value.code == "proposal.malformed"


# ---------------------------------------------------------------------------
# Idempotent typed receipts replay / retriability (state-context-operations:T006.3)
# ---------------------------------------------------------------------------


def test_reusing_an_idempotency_key_replays_the_receipt_without_re_executing():
    store = MemoryOperationReceiptStore()
    operation = OperationRef(**operation_ref_for("state.evidence.submit"))
    executions = {"n": 0}

    def executor() -> OperationOutcome:
        executions["n"] += 1
        return OperationOutcome("succeeded", external_ref={"state_event_id": "evt_1"})

    first, replayed_first = store.record_attempt(
        run_id="run_1", command_id="cmd_1", operation=operation,
        idempotency_key="run:run_1:evidence:1", executor=executor,
    )
    second, replayed_second = store.record_attempt(
        run_id="run_1", command_id="cmd_1", operation=operation,
        idempotency_key="run:run_1:evidence:1", executor=executor,
    )

    assert executions["n"] == 1  # the effect ran exactly once
    assert replayed_first is False and replayed_second is True
    assert first["receipt_id"] == second["receipt_id"]
    assert second["status"] == "succeeded"


def test_a_failed_attempt_stays_retriable_and_never_fabricates_a_stored_success():
    from workbench.models import OperationRefusal

    store = MemoryOperationReceiptStore()
    operation = OperationRef(**operation_ref_for("state.evidence.submit"))
    executions = {"n": 0}

    def failing() -> OperationOutcome:
        executions["n"] += 1
        return OperationOutcome(
            "failed", error=OperationRefusal("operation.input_invalid", "transient failure", retryable=True),
        )

    receipt, _ = store.record_attempt(
        run_id="run_1", command_id="cmd_1", operation=operation,
        idempotency_key="run:run_1:evidence:1", executor=failing,
    )
    assert receipt["status"] == "failed"
    assert receipt["error"]["retryable"] is True
    # A failed attempt is not persisted under its key, so a retry re-executes.
    assert store.get_receipt("run:run_1:evidence:1") is None
    store.record_attempt(
        run_id="run_1", command_id="cmd_1", operation=operation,
        idempotency_key="run:run_1:evidence:1", executor=failing,
    )
    assert executions["n"] == 2


# --------------------------------------------------------------------------- #
# plan-task-delivery T005 — the atomic idempotent Deliver: a typed start receipt
# is returned BEFORE any State claim / Codex launch, a precondition failure
# leaves no claim, and a retried start replays one run.  Exercised through the
# same MemoryDeliverStartStore the runtime uses.
# --------------------------------------------------------------------------- #

from _support import load_example as _ptd_rel_load_example
from workbench.deliver import DeliverPreconditions as _PtdPreconditions, MemoryDeliverStartStore as _PtdRelStore


def test_ptd_t005_precondition_failure_never_claims_or_launches():
    store = _PtdRelStore()
    effects: list[str] = []

    def launch():
        # Stand in for the State claim + Codex launch: it must never run when a
        # precondition fails (a State acceptance never precedes a merge, and no
        # claim precedes a passed preflight).
        effects.append("claim+launch")
        return store.default_run_block("run_rel_0001")

    intent = _ptd_rel_load_example("deliver-intent.v1.json")
    receipt, replayed = store.start(
        intent, launch=launch, preconditions=_PtdPreconditions(prd_unapproved=True),
    )
    assert receipt["status"] == "denied" and replayed is False
    assert receipt["error"]["code"] == "deliver.prd_unapproved"
    assert effects == []  # nothing was claimed or launched
    assert store.get_receipt(intent["intent_digest"]) is None


def test_ptd_t005_accepted_start_claims_once_and_replays_the_same_run():
    store = _PtdRelStore()
    effects: list[str] = []

    def launch():
        effects.append("claim+launch")
        return store.default_run_block("run_rel_0002")

    intent = _ptd_rel_load_example("deliver-intent.v1.json")
    accepted, replayed = store.start(intent, launch=launch, preconditions=_PtdPreconditions())
    assert accepted["status"] == "accepted" and replayed is False
    # A retried identical intent replays the stored receipt as a duplicate; the
    # claim/launch effect never runs a second time.
    duplicate, replayed2 = store.start(intent, launch=launch, preconditions=_PtdPreconditions())
    assert duplicate["status"] == "duplicate" and replayed2 is True
    assert duplicate["run"]["run_id"] == accepted["run"]["run_id"]
    assert effects == ["claim+launch"]
