"""Shared, non-conftest test helpers and constants.

This is a plain importable module (NOT ``conftest``) so the factories and
constants below survive a future ``tests/__init__.py`` or an
``importmode=importlib`` switch: test modules import it by name the same way
they import each other, rather than depending on the special-cased ``conftest``
import path.

It provides the single hermetic discovery -> profile -> snapshot -> capture
pipeline (``compile_delivery_snapshot`` / ``build_run_context``) so the
run-context tests and the harness-kernel tests never re-implement it, and the
single closed system-health descriptor field set
(``SYSTEM_HEALTH_DESCRIPTOR_FIELDS``) so the API-surface and security-contract
tests cannot drift.  Everything is hermetic: only checked-in example JSON is
read, no CLI or network.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from workbench.capability_profiles import validate_project_profile
from workbench.models import (
    RunConstraints,
    RunContext,
    RunCursor,
    RunIdentity,
    RunReceipt,
    RunWorkflowPin,
    UntrustedEvidence,
    UntrustedTask,
    UntrustedTaskRef,
    run_capabilities_from_snapshot,
    run_skills_from_snapshot,
)
from workbench.provider_catalogs import (
    DEFAULT_PROVIDER_ALLOWLIST,
    PublishedCatalogSet,
    validate_provider_catalog,
)
from workbench.workflow_snapshot import WorkflowSnapshot, compile_workflow_snapshot

_EXAMPLES = Path(__file__).resolve().parents[1] / "docs" / "contracts" / "examples"

#: The exact closed field set a system-health descriptor may serialize.  A field
#: added outside this set (a leak-by-addition) must fail the response/descriptor
#: tests, so the assertion is not a tautology.  Kept here, imported by the API
#: surface test and the security-contract test, so the two can never drift.
SYSTEM_HEALTH_DESCRIPTOR_FIELDS = frozenset({
    "configured", "dependencies", "digest", "integration_id", "non_canonical",
    "owner", "remediation", "schema_version", "state", "title",
    "version", "detail", "last_checked_at",
})


def load_example(name: str) -> dict:
    return json.loads((_EXAMPLES / name).read_text(encoding="utf-8"))


def compile_delivery_snapshot() -> WorkflowSnapshot:
    """Compile the reviewed delivery snapshot from the checked-in examples."""
    published = PublishedCatalogSet(
        catalogs=tuple(
            validate_provider_catalog(provider, load_example(f"{provider}.catalog.v1.json"))
            for provider in sorted(DEFAULT_PROVIDER_ALLOWLIST)
        )
    )
    profile = validate_project_profile(
        load_example("project-capability-profile.v1.json"),
        published,
        configured_model_profiles=("coding-local", "planning-local"),
        configured_skills={"anvil:execute": "sha256:" + "7" * 64},
        approval_actions=("commit_pr", "merge_and_accept"),
    )
    workflow = load_example("delivery.workflow.v2.json")
    selected: list[dict] = []
    seen: set[tuple] = set()
    for step in workflow["steps"]:
        if step["kind"] != "operation":
            continue
        key = tuple(sorted(step["operation"].items()))
        if key not in seen:
            seen.add(key)
            selected.append(copy.deepcopy(step["operation"]))
    return compile_workflow_snapshot(
        workflow, profile, published,
        selected_operations=selected,
        selected_skills=[{"id": "anvil:execute", "digest": "sha256:" + "7" * 64}],
        route="coding-local",
    )


def build_run_context(snapshot: WorkflowSnapshot | None = None, **overrides: Any) -> RunContext:
    """Assemble a complete, valid run context; overrides replace capture kwargs."""
    snapshot = snapshot or compile_delivery_snapshot()
    kwargs: dict[str, Any] = dict(
        context_id="ctx_run_shared_0001",
        identity=RunIdentity(
            run_id="run_shared_1", session_id="sess_1", bridge_id="bridge_1",
            worktree_name="checkout-a", task_id="release-beta:T001", request_id="req_1",
        ),
        workflow=RunWorkflowPin.from_snapshot(snapshot),
        capabilities=run_capabilities_from_snapshot(snapshot),
        skills=run_skills_from_snapshot(snapshot, {"anvil:execute": "State-backed guidance."}),
        constraints=RunConstraints(
            turn_limit=12, tool_limit=24,
            stop_conditions=("Do not submit evidence before verification passes.",),
        ),
        cursor=RunCursor(
            step_id="implement", attempt=1,
            completed_receipts=(RunReceipt(receipt_id="rcpt_claim", summary="claim succeeded"),),
        ),
        task=UntrustedTask(
            ref=UntrustedTaskRef(prd_id="release-beta", task_id="T001", prd_revision=5),
            title="Add a documented operation contract",
            acceptance_criteria=("Add a versioned resource", "Validate its JSON shape"),
            work_packet_digest="sha256:" + "8" * 64,
            scope=("docs/contracts",),
            verification_plan=("Run the allowlisted verification command.",),
        ),
        evidence=(UntrustedEvidence(citation="state-event:claim", summary="Task claim is active."),),
    )
    kwargs.update(overrides)
    return RunContext.capture(**kwargs)
