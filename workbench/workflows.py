"""Durable, allowlisted workflow-definition helpers.

Workbench workflows deliberately describe orchestration state rather than
execute arbitrary model-authored code.  Bridge commands and approvals remain
the only way to affect a worktree, GitHub, State, or Serving policy.
"""
from __future__ import annotations

import copy
from typing import Any


WORKFLOW_STEP_KINDS = frozenset({
    "agent", "tool", "condition", "fan_out", "join", "approval_wait",
    "evidence_submit", "reconcile", "cancel",
})
WORKFLOW_STATUSES = frozenset({"draft", "running", "waiting_approval", "completed", "reconciliation", "cancelled"})


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
        clean = copy.deepcopy(raw)
        clean["id"] = step_id.strip()
        clean["kind"] = kind
        clean["next"] = list(next_steps)
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


def step_by_id(definition: dict[str, Any], step_id: str) -> dict[str, Any]:
    for step in definition["steps"]:
        if step["id"] == step_id:
            return step
    raise WorkflowError("workflow step is not present in this version")


def next_cursor(definition: dict[str, Any], step_id: str) -> tuple[str, ...]:
    """Resolve a completed node's explicit next nodes without executing them."""
    return tuple(step_by_id(definition, step_id)["next"])
