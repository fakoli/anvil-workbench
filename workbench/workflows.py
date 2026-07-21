"""Durable, allowlisted workflow-definition helpers.

Workbench workflows deliberately describe orchestration state rather than
execute arbitrary model-authored code.  Bridge commands and approvals remain
the only way to affect a worktree, GitHub, State, or Serving policy.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Mapping

from .models import (
    OperationRef,
    OperationRefusal,
    ResolvedOperation,
    TypedOperationError,
)


WORKFLOW_STEP_KINDS = frozenset({
    "agent", "tool", "condition", "fan_out", "join", "approval_wait",
    "evidence_submit", "reconcile", "cancel",
})
WORKFLOW_STATUSES = frozenset({"draft", "running", "waiting_approval", "completed", "reconciliation", "cancelled"})
_SKILL_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9:_-]{0,119}$")


class WorkflowError(ValueError):
    """A definition or transition would make the harness ambiguous or unsafe."""


def validate_definition(value: dict[str, Any]) -> dict[str, Any]:
    """Return a canonical safe definition or reject unsupported graph semantics."""
    if not isinstance(value, dict):
        raise WorkflowError("workflow definition must be an object")
    steps = value.get("steps")
    if not isinstance(steps, list) or not steps or len(steps) > 64:
        raise WorkflowError("workflow definition requires between 1 and 64 steps")
    canonical_steps: list[dict[str, Any]] = []
    step_ids: set[str] = set()
    for raw in steps:
        if not isinstance(raw, dict):
            raise WorkflowError("workflow steps must be objects")
        step_id = raw.get("id")
        kind = raw.get("kind")
        if not isinstance(step_id, str) or not step_id.strip() or len(step_id) > 120:
            raise WorkflowError("workflow step id is required")
        if step_id in step_ids:
            raise WorkflowError("workflow step ids must be unique")
        if kind not in WORKFLOW_STEP_KINDS:
            raise WorkflowError(f"workflow step kind is not allowlisted: {kind}")
        next_steps = raw.get("next", [])
        if not isinstance(next_steps, list) or not all(isinstance(item, str) and item for item in next_steps):
            raise WorkflowError("workflow step next must be a list of step ids")
        skill_ids = raw.get("skills", [])
        if not isinstance(skill_ids, list) or len(skill_ids) > 16 or not all(
            isinstance(item, str) and _SKILL_ID.fullmatch(item) for item in skill_ids
        ):
            raise WorkflowError("workflow step skills must be up to 16 configured skill names")
        if len(set(skill_ids)) != len(skill_ids):
            raise WorkflowError("workflow step skills must be unique")
        clean = copy.deepcopy(raw)
        clean["id"] = step_id.strip()
        clean["kind"] = kind
        clean["next"] = list(next_steps)
        clean["skills"] = list(skill_ids)
        canonical_steps.append(clean)
        step_ids.add(step_id)
    entry = value.get("entry", canonical_steps[0]["id"])
    if entry not in step_ids:
        raise WorkflowError("workflow entry must reference a step")
    for step in canonical_steps:
        missing = set(step["next"]) - step_ids
        if missing:
            raise WorkflowError(f"workflow next references unknown steps: {', '.join(sorted(missing))}")
        if step["id"] in step["next"]:
            raise WorkflowError("workflow steps may not self-loop in v1")
    adjacency = {step["id"]: tuple(step["next"]) for step in canonical_steps}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(step_id: str) -> None:
        if step_id in visiting:
            raise WorkflowError("workflow cycles are not supported in v1")
        if step_id in visited:
            return
        visiting.add(step_id)
        for next_step in adjacency[step_id]:
            visit(next_step)
        visiting.remove(step_id)
        visited.add(step_id)

    for step_id in adjacency:
        visit(step_id)
    return {"entry": entry, "steps": canonical_steps}


# ---------------------------------------------------------------------------
# Typed operation workflow validation and descriptor resolution
# (state-context-operations:T006.1)
# ---------------------------------------------------------------------------
#
# A model or browser may propose that the run invoke one operation.  It proposes
# only an operation *reference* (provider/id/version/digest) plus a typed input
# object -- never a command, an adapter, a path, or a skill body.  This resolver
# is the hub-side gate that turns that proposal into a :class:`ResolvedOperation`
# ONLY when it resolves to a descriptor the run is actually pinned to:
#
# * the provider must have a discovered catalog, the operation must be present
#   at the EXACT pinned ``(id, contract_version, operation_digest)``, and it must
#   be one of the run's compiled snapshot operations (the profile-allowlisted,
#   workflow-referenced set).  A proposal that names an unknown provider, a
#   drifted digest, or a capability absent from the pinned profile fails closed;
# * the typed input must validate against the operation's pinned input schema --
#   a closed ``additionalProperties:false`` object schema in the reviewed
#   catalogs -- so an undeclared field (where a raw command, path, or secret
#   would ride) is refused before any dispatch.
#
# Every refusal is a stable :class:`OperationRefusal` code, never a bare string,
# so a caller (and a denied receipt) asserts on the claimed reason.  This is the
# hub-side counterpart to :func:`workbench.contracts.validate_bridge_command_snapshot`
# (the bridge re-derives everything locally in its own immediate preflight).


def _refuse(code: str, summary: str) -> "TypedOperationError":
    return TypedOperationError(OperationRefusal(code, summary))


def _published_operation(published_catalogs: Any, ref: OperationRef) -> Any:
    """Resolve one operation's public descriptor at its exact pinned digest.

    Fails closed with a typed refusal when the provider has no discovered
    catalog (``operation.provider_unknown``), the ``(id, contract_version)`` is
    absent (``operation.unknown``), or the pinned digest no longer matches the
    discovered descriptor (``operation.digest_drift``).
    """
    from .provider_catalogs import ProviderCatalogError

    try:
        catalog = published_catalogs.catalog(ref.provider)
    except ProviderCatalogError as exc:
        raise _refuse(
            "operation.provider_unknown",
            f"the operation names a provider with no discovered catalog: {ref.provider}",
        ) from exc
    candidates = [
        operation for operation in catalog.operations
        if operation.id == ref.id and operation.contract_version == ref.contract_version
    ]
    if not candidates:
        raise _refuse(
            "operation.unknown",
            f"the operation is not present in the discovered {ref.provider} catalog: {ref.id} {ref.contract_version}",
        )
    for operation in candidates:
        if operation.operation_digest == ref.operation_digest:
            return operation
    raise _refuse(
        "operation.digest_drift",
        f"the pinned operation digest no longer matches the discovered {ref.provider} catalog: {ref.id}",
    )


def _validate_operation_inputs(descriptor: Any, inputs: Any) -> dict[str, Any]:
    """Validate a typed input object against the operation's pinned input schema."""
    from jsonschema.exceptions import ValidationError

    from .contracts import ContractValidationError, check_operation_input_schema
    from jsonschema import Draft202012Validator

    if not isinstance(inputs, Mapping):
        raise _refuse("operation.input_not_object", "the operation input must be an object")
    input_schema = descriptor.input_schema
    if not isinstance(input_schema, Mapping):
        raise _refuse("operation.schema_unresolvable", "the pinned operation has no object input schema")
    try:
        check_operation_input_schema(input_schema)
    except ContractValidationError as exc:
        raise _refuse("operation.schema_unresolvable", f"the pinned operation input schema {exc}") from exc
    try:
        Draft202012Validator(dict(input_schema)).validate(dict(inputs))
    except ValidationError as exc:
        raise _refuse(
            "operation.input_invalid",
            f"the operation input does not match the pinned schema: {exc.message}",
        ) from exc
    except Exception as exc:  # a referencing/registry failure is not a ValidationError
        raise _refuse(
            "operation.schema_unresolvable",
            f"the pinned operation input schema cannot be evaluated: {exc}",
        ) from exc
    return dict(inputs)


_GATED_EFFECTS = frozenset({"external_effect", "policy_mutation"})


def resolve_operation(
    ref: OperationRef, inputs: Any, snapshot: Any, published_catalogs: Any,
) -> ResolvedOperation:
    """Resolve a pinned operation reference + typed input for the run.

    ``snapshot`` must be the compiler's own :class:`WorkflowSnapshot` and
    ``published_catalogs`` the registry's :class:`PublishedCatalogSet`; a
    caller-assembled mapping is refused by type so a hub- or model-supplied
    catalog/snapshot has no parameter to arrive through.  Returns a
    :class:`ResolvedOperation` only when the reference resolves at its exact
    pinned digest in BOTH the discovered catalog and the run's compiled
    snapshot, and the typed input validates against the pinned input schema.
    """
    from .provider_catalogs import PublishedCatalogSet
    from .workflow_snapshot import WorkflowSnapshot

    if not isinstance(snapshot, WorkflowSnapshot):
        raise _refuse("proposal.malformed", "resolution requires the compiler's own workflow snapshot")
    if not isinstance(published_catalogs, PublishedCatalogSet):
        raise _refuse("proposal.malformed", "resolution requires the registry's published catalog set")
    descriptor = _published_operation(published_catalogs, ref)
    # The run may invoke only an operation compiled into its immutable snapshot
    # (the profile-allowlisted AND workflow-referenced set).  An operation that
    # exists in the catalog but was never selected for this run is unprofiled
    # for the run: a model cannot widen its authority by naming it.
    selected = {operation.operation_digest for operation in snapshot.operations if
                operation.provider == ref.provider and operation.id == ref.id
                and operation.contract_version == ref.contract_version}
    if ref.operation_digest not in selected:
        raise _refuse(
            "operation.unprofiled",
            f"the operation is not in the run's pinned capability profile: {ref.provider} {ref.id}",
        )
    validated = _validate_operation_inputs(descriptor, inputs)
    return ResolvedOperation(
        operation=ref,
        effect=descriptor.effect,
        gate_required=descriptor.effect in _GATED_EFFECTS,
        approval_action=None,
        inputs=validated,
    )


def resolve_operation_request(
    proposal: Any, snapshot: Any, published_catalogs: Any,
) -> ResolvedOperation:
    """Resolve a model ``operation_request`` proposal into a pinned operation.

    ``proposal`` is a ``workbench-model-proposal/v1`` ``operation_request``
    (ids-only operation reference plus a typed ``input`` object).  A malformed
    proposal -- a non-object, a wrong ``kind``, an undeclared field, or a
    missing reference -- fails closed with ``proposal.malformed``; a well-formed
    proposal is resolved by :func:`resolve_operation`.  A model can therefore
    never mint a new privilege by emitting a command name, a skill, or arbitrary
    JSON: only a pinned, profiled operation reference resolves.
    """
    if not isinstance(proposal, Mapping):
        raise _refuse("proposal.malformed", "operation proposal is not an object")
    if proposal.get("schema_version") != "workbench-model-proposal/v1":
        raise _refuse("proposal.malformed", "operation proposal has an unexpected schema version")
    if proposal.get("kind") != "operation_request":
        raise _refuse("proposal.malformed", "operation proposal is not an operation_request")
    allowed = {"schema_version", "kind", "reason", "operation", "input"}
    if set(proposal) - allowed:
        raise _refuse("proposal.malformed", "operation proposal carries undeclared fields")
    ref = OperationRef.from_mapping(proposal.get("operation"), "proposal operation")
    return resolve_operation(ref, proposal.get("input"), snapshot, published_catalogs)


def _operation_step_binding_keys(step: Mapping[str, Any]) -> set[str]:
    inputs = step.get("inputs")
    return set(inputs) if isinstance(inputs, Mapping) else set()


def validate_workflow_operations(
    workflow: Mapping[str, Any], snapshot: Any, published_catalogs: Any,
) -> tuple[ResolvedOperation, ...]:
    """Resolve every operation step in a compiled workflow to a pinned descriptor.

    For each ``operation`` step: the referenced operation must resolve at its
    exact pinned digest in the snapshot + discovered catalog, and the step's
    input BINDING keys must exactly cover the descriptor's declared input schema
    (every required property bound, no undeclared key through which a raw
    command/path/secret binding could ride).  A step's bindings are ids-only
    literal/reference forms (validated by the workflow contract), so this checks
    the KEY set against the pinned schema, not the runtime values.  Returns one
    :class:`ResolvedOperation` per operation step (inputs empty: values bind at
    dispatch), or raises a typed refusal.
    """
    if not isinstance(workflow, Mapping):
        raise _refuse("proposal.malformed", "workflow is not an object")
    resolved: list[ResolvedOperation] = []
    for step in workflow.get("steps", ()):
        if not isinstance(step, Mapping) or step.get("kind") != "operation":
            continue
        ref = OperationRef.from_mapping(step.get("operation"), "workflow step operation")
        descriptor = _published_operation(published_catalogs, ref)
        from .provider_catalogs import PublishedCatalogSet
        from .workflow_snapshot import WorkflowSnapshot

        if not isinstance(snapshot, WorkflowSnapshot):
            raise _refuse("proposal.malformed", "resolution requires the compiler's own workflow snapshot")
        if not isinstance(published_catalogs, PublishedCatalogSet):
            raise _refuse("proposal.malformed", "resolution requires the registry's published catalog set")
        selected = {
            operation.operation_digest for operation in snapshot.operations
            if operation.provider == ref.provider and operation.id == ref.id
            and operation.contract_version == ref.contract_version
        }
        if ref.operation_digest not in selected:
            raise _refuse(
                "operation.unprofiled",
                f"the workflow operation is not in the pinned capability profile: {ref.provider} {ref.id}",
            )
        schema = descriptor.input_schema if isinstance(descriptor.input_schema, Mapping) else {}
        declared = set(schema.get("properties", {})) if isinstance(schema.get("properties"), Mapping) else set()
        required = set(schema.get("required", ())) if isinstance(schema.get("required"), (list, tuple)) else set()
        binding_keys = _operation_step_binding_keys(step)
        undeclared = binding_keys - declared
        if undeclared:
            raise _refuse(
                "operation.input_invalid",
                f"workflow operation step binds undeclared input fields: {sorted(undeclared)}",
            )
        missing = required - binding_keys
        if missing:
            raise _refuse(
                "operation.input_invalid",
                f"workflow operation step omits required input fields: {sorted(missing)}",
            )
        resolved.append(
            ResolvedOperation(
                operation=ref,
                effect=descriptor.effect,
                gate_required=descriptor.effect in _GATED_EFFECTS,
                approval_action=None,
                inputs={},
            )
        )
    return tuple(resolved)


def step_by_id(definition: dict[str, Any], step_id: str) -> dict[str, Any]:
    for step in definition["steps"]:
        if step["id"] == step_id:
            return step
    raise WorkflowError("workflow step is not present in this version")


def next_cursor(definition: dict[str, Any], step_id: str) -> tuple[str, ...]:
    """Resolve a completed node's explicit next nodes without executing them."""
    return tuple(step_by_id(definition, step_id)["next"])


def _can_reach(definition: dict[str, Any], start: str, target: str) -> bool:
    pending = [start]
    visited: set[str] = set()
    while pending:
        current = pending.pop()
        if current == target:
            return True
        if current in visited:
            continue
        visited.add(current)
        pending.extend(step_by_id(definition, current)["next"])
    return False


def advance_cursor(
    definition: dict[str, Any], cursor: tuple[str, ...], completed_step_id: str,
) -> tuple[str, ...]:
    """Remove one finished branch and merge successors without dropping siblings.

    A join becomes runnable only after no other unfinished cursor branch can
    still reach it. This gives fan-out branches a small durable barrier without
    executing model-authored logic in the store.
    """
    remaining = [step_id for step_id in cursor if step_id != completed_step_id]
    successors = next_cursor(definition, completed_step_id)
    merged = list(remaining)
    for successor in successors:
        if successor in merged:
            continue
        if step_by_id(definition, successor)["kind"] == "join" and any(
            _can_reach(definition, active_step, successor) for active_step in remaining
        ):
            continue
        merged.append(successor)
    return tuple(merged)
