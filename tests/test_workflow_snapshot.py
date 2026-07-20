"""Hermetic tests for workflow-snapshot compilation and preflight drift.

Fixtures are the checked-in contract examples under
``docs/contracts/examples/``: the delivery workflow, the project capability
profile, and the three provider catalogs.  No live CLI, network, or bridge is
touched; the discovered set is built directly with
:func:`validate_provider_catalog` and the profile with
:func:`validate_project_profile`.

Acceptance mapping (state-context-operations:T004.3):

* Criterion 1 (a compiled workflow stores one immutable, source-attributed
  snapshot of every selected descriptor and digest):
  ``test_compiled_snapshot_pins_every_selected_descriptor_with_source_attribution``,
  ``test_snapshot_digest_is_deterministic_and_recomputable``,
  ``test_bridge_snapshot_block_passes_the_reference_bridge_validator``.
* Criterion 2 (a later catalog or profile refresh does not change an existing
  workflow snapshot):
  ``test_catalog_and_profile_refresh_never_reinterprets_a_compiled_snapshot``,
  ``test_snapshot_is_frozen_and_deep_copy_isolated``.
* Criterion 3 (missing or changed pinned digests fail preflight with stable
  drift metadata before an effect):
  ``test_preflight_passes_a_matching_snapshot``,
  ``test_preflight_detects_operation_digest_drift``,
  ``test_preflight_detects_operation_removal``,
  ``test_preflight_detects_catalog_digest_drift``,
  ``test_preflight_detects_missing_catalog``,
  ``test_preflight_detects_profile_digest_and_skill_drift``,
  ``test_preflight_detects_route_model_profile_and_approval_action_removal``,
  ``test_preflight_detects_a_tampered_snapshot``,
  ``test_preflight_refuses_untyped_authority_inputs``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from workbench.capability_profiles import PinnedCapabilityProfile, validate_project_profile
from workbench.contracts import ContractValidationError, contract_digest
from workbench.provider_catalogs import (
    DEFAULT_PROVIDER_ALLOWLIST,
    PublishedCatalogSet,
    validate_provider_catalog,
)
from workbench.workflow_snapshot import (
    SnapshotDrift,
    WorkflowSnapshot,
    WorkflowSnapshotDriftError,
    WorkflowSnapshotError,
    compile_workflow_snapshot,
    preflight_snapshot,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "docs" / "contracts" / "examples"

CONFIGURED_MODEL_PROFILES = ("coding-local", "planning-local")
CONFIGURED_SKILLS = {"anvil:execute": "sha256:" + "7" * 64}
CONFIGURED_APPROVAL_ACTIONS = ("commit_pr", "merge_and_accept")
WORKFLOW_DIGEST = "sha256:08eb89de05b27a1d22db4b26ca743f4e5cf60b47efca0f5ff0d5fc65d868e73a"


def load(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def catalog_example(provider: str) -> dict:
    return load(f"{provider}.catalog.v1.json")


def workflow_example() -> dict:
    return load("delivery.workflow.v2.json")


def rehash_catalog(catalog: dict) -> dict:
    for operation in catalog["operations"]:
        operation["operation_digest"] = contract_digest("operation", operation)
    catalog["catalog_digest"] = contract_digest("catalog", catalog)
    return catalog


def rehash_profile(profile: dict) -> dict:
    profile["digest"] = contract_digest("profile", profile)
    return profile


def discovered(overrides: dict[str, dict] | None = None) -> PublishedCatalogSet:
    catalogs = []
    for provider in sorted(DEFAULT_PROVIDER_ALLOWLIST):
        document = (overrides or {}).get(provider, catalog_example(provider))
        catalogs.append(validate_provider_catalog(provider, document))
    return PublishedCatalogSet(catalogs=tuple(catalogs))


def pinned_profile(
    profile: dict | None = None,
    catalogs: PublishedCatalogSet | None = None,
    **overrides,
) -> PinnedCapabilityProfile:
    kwargs: dict = {
        "configured_model_profiles": CONFIGURED_MODEL_PROFILES,
        "configured_skills": dict(CONFIGURED_SKILLS),
        "approval_actions": CONFIGURED_APPROVAL_ACTIONS,
    }
    kwargs.update(overrides)
    return validate_project_profile(
        profile if profile is not None else load("project-capability-profile.v1.json"),
        catalogs if catalogs is not None else discovered(),
        **kwargs,
    )


def workflow_operation_refs(workflow: dict | None = None) -> list[dict]:
    """The exact operation references the delivery workflow's steps pin."""
    document = workflow if workflow is not None else workflow_example()
    refs: list[dict] = []
    seen: set[tuple] = set()
    for step in document["steps"]:
        if step["kind"] != "operation":
            continue
        key = tuple(sorted(step["operation"].items()))
        if key not in seen:
            seen.add(key)
            refs.append(copy.deepcopy(step["operation"]))
    return refs


def compile_snapshot(
    workflow: dict | None = None,
    profile: PinnedCapabilityProfile | None = None,
    catalogs: PublishedCatalogSet | None = None,
    **overrides,
) -> WorkflowSnapshot:
    document = workflow if workflow is not None else workflow_example()
    kwargs: dict = {
        "selected_operations": workflow_operation_refs(document),
        "selected_skills": [{"id": "anvil:execute", "digest": "sha256:" + "7" * 64}],
        "route": "coding-local",
    }
    kwargs.update(overrides)
    return compile_workflow_snapshot(
        document,
        profile if profile is not None else pinned_profile(),
        catalogs if catalogs is not None else discovered(),
        **kwargs,
    )


def drift_kinds(error: WorkflowSnapshotDriftError) -> set[str]:
    return {drift.kind for drift in error.drifts}


# --- Criterion 1: one immutable, source-attributed snapshot of every pin ---


def test_compiled_snapshot_pins_every_selected_descriptor_with_source_attribution() -> None:
    workflow = workflow_example()
    profile_fixture = load("project-capability-profile.v1.json")
    snapshot = compile_snapshot()

    # The workflow identity and digest.
    assert snapshot.workflow_id == workflow["id"]
    assert snapshot.workflow_revision == workflow["revision"]
    assert snapshot.workflow_digest == WORKFLOW_DIGEST

    # The capability profile identity and digest.
    assert snapshot.capability_profile_id == profile_fixture["id"]
    assert snapshot.capability_profile_revision == profile_fixture["revision"]
    assert snapshot.capability_profile_digest == profile_fixture["digest"]

    # Every discovered provider catalog is pinned at its exact digest.
    catalog_digests = {
        provider: catalog_example(provider)["catalog_digest"]
        for provider in DEFAULT_PROVIDER_ALLOWLIST
    }
    assert {c.provider: c.catalog_digest for c in snapshot.catalogs} == catalog_digests

    # Every selected operation is pinned exactly and source-attributed to the
    # catalog revision it came from.
    expected_keys = {
        (ref["provider"], ref["id"], ref["contract_version"], ref["operation_digest"])
        for ref in workflow_operation_refs(workflow)
    }
    assert {
        (op.provider, op.id, op.contract_version, op.operation_digest)
        for op in snapshot.operations
    } == expected_keys
    for operation in snapshot.operations:
        source = catalog_example(operation.provider)
        assert operation.source_catalog_digest == source["catalog_digest"]
        assert operation.source_catalog_version == source["catalog_version"]
        declared = next(
            item for item in source["operations"] if item["id"] == operation.id
        )
        assert operation.effect == declared["effect"]

    # Skills, route, model profiles, approval actions, and budget are pinned
    # and attributed to the profile revision.
    assert [skill.as_dict() for skill in snapshot.skills] == [
        {
            "id": "anvil:execute",
            "digest": "sha256:" + "7" * 64,
            "source_profile_digest": profile_fixture["digest"],
        }
    ]
    assert snapshot.route is not None
    assert snapshot.route.model_profile == "coding-local"
    assert snapshot.route.source_profile_digest == profile_fixture["digest"]
    assert snapshot.model_profiles == ("coding-local",)
    assert snapshot.approval_actions == ("commit_pr", "merge_and_accept")
    assert snapshot.limits.as_dict() == profile_fixture["limits"]

    # The snapshot digest is advertised and recomputes over its own payload.
    payload = snapshot.as_dict()
    assert payload["snapshot_digest"] == snapshot.snapshot_digest
    del payload["snapshot_digest"]
    assert contract_digest("workflow-snapshot", payload) == snapshot.snapshot_digest


def test_snapshot_digest_is_deterministic_and_recomputable() -> None:
    first = compile_snapshot()
    second = compile_snapshot()
    assert first.snapshot_digest == second.snapshot_digest
    assert first.as_dict() == second.as_dict()


def test_bridge_snapshot_block_passes_the_reference_bridge_validator() -> None:
    from workbench.contracts import validate_bridge_command_snapshot

    snapshot = compile_snapshot()
    block = snapshot.bridge_snapshot()
    assert block["workflow_digest"] == WORKFLOW_DIGEST
    assert block["capability_profile_digest"] == snapshot.capability_profile_digest

    catalogs = {provider: catalog_example(provider) for provider in DEFAULT_PROVIDER_ALLOWLIST}
    profile = load("project-capability-profile.v1.json")
    command = load("bridge-command.invoke-operation.v1.json")
    command["workflow_snapshot"] = block
    validate_bridge_command_snapshot(command, catalogs, profile)


# --- Compile-time fail-closed rules ---


def test_compile_refuses_untyped_authority_inputs() -> None:
    with pytest.raises(WorkflowSnapshotError, match="validator's pinned profile"):
        compile_snapshot(profile=load("project-capability-profile.v1.json"))  # type: ignore[arg-type]
    with pytest.raises(WorkflowSnapshotError, match="registry's published set"):
        compile_snapshot(
            catalogs={p: catalog_example(p) for p in DEFAULT_PROVIDER_ALLOWLIST},  # type: ignore[arg-type]
        )
    with pytest.raises(WorkflowSnapshotError, match="not a JSON object"):
        compile_workflow_snapshot(
            ["not", "an", "object"],  # type: ignore[arg-type]
            pinned_profile(),
            discovered(),
            selected_operations=workflow_operation_refs(),
        )


def test_compile_refuses_a_malformed_workflow() -> None:
    extended = workflow_example()
    extended["shell_hook"] = "rm -rf /"
    with pytest.raises(WorkflowSnapshotError, match="workflow contract"):
        compile_snapshot(workflow=extended)

    unversioned = workflow_example()
    del unversioned["revision"]
    with pytest.raises(WorkflowSnapshotError, match="workflow contract"):
        compile_snapshot(workflow=unversioned)


def test_compile_refuses_an_unallowlisted_or_undiscovered_selection() -> None:
    # Discovered by the catalog but absent from the profile allowlist.
    catalog = catalog_example("anvil-state")
    unprofiled = next(
        op for op in catalog["operations"] if op["id"] == "state.project.snapshot"
    )
    refs = workflow_operation_refs() + [
        {
            "provider": "anvil-state",
            "id": unprofiled["id"],
            "contract_version": unprofiled["contract_version"],
            "operation_digest": unprofiled["operation_digest"],
        }
    ]
    with pytest.raises(WorkflowSnapshotError, match="not allowlisted by the pinned capability profile"):
        compile_snapshot(selected_operations=refs)

    # Allowlist-shaped but not discovered at that digest: the profile pin
    # itself would have failed, so simulate via a stale digest reference.
    stale = workflow_operation_refs()
    stale[0]["operation_digest"] = "sha256:" + "a" * 64
    with pytest.raises(WorkflowSnapshotError, match="not allowlisted"):
        compile_snapshot(selected_operations=stale)


def test_compile_requires_selection_to_exactly_cover_the_workflow() -> None:
    refs = workflow_operation_refs()
    with pytest.raises(WorkflowSnapshotError, match="unselected operation"):
        compile_snapshot(selected_operations=refs[:-1])

    duplicated = refs + [copy.deepcopy(refs[0])]
    with pytest.raises(WorkflowSnapshotError, match="duplicate selected operation"):
        compile_snapshot(selected_operations=duplicated)

    with pytest.raises(WorkflowSnapshotError, match="unselected skill"):
        compile_snapshot(selected_skills=[])

    unknown_skill = [
        {"id": "anvil:execute", "digest": "sha256:" + "7" * 64},
        {"id": "anvil:finish", "digest": "sha256:" + "8" * 64},
    ]
    with pytest.raises(WorkflowSnapshotError, match="not pinned by the capability profile"):
        compile_snapshot(selected_skills=unknown_skill)

    stale_skill = [{"id": "anvil:execute", "digest": "sha256:" + "9" * 64}]
    with pytest.raises(WorkflowSnapshotError, match="differs from the profile pin"):
        compile_snapshot(selected_skills=stale_skill)

    with pytest.raises(WorkflowSnapshotError, match="route is not pinned"):
        compile_snapshot(route="research-remote")


def test_compile_refuses_an_unused_selected_grant() -> None:
    # An operation grant the workflow never references would be silent excess
    # authority in the immutable run context; the compile refuses it.
    profile_fixture = load("project-capability-profile.v1.json")
    serving_ref = next(
        op for op in profile_fixture["operations"] if op["provider"] == "anvil-serving"
    )
    refs = workflow_operation_refs() + [copy.deepcopy(serving_ref)]
    with pytest.raises(WorkflowSnapshotError, match="not referenced by the workflow"):
        compile_snapshot(selected_operations=refs)


def test_compile_refuses_a_workflow_approval_action_outside_the_profile() -> None:
    narrowed = load("project-capability-profile.v1.json")
    narrowed["approval_actions"] = ["commit_pr"]
    profile = pinned_profile(
        profile=rehash_profile(narrowed), approval_actions=("commit_pr",)
    )
    with pytest.raises(WorkflowSnapshotError, match="approval action is not pinned"):
        compile_snapshot(profile=profile)


# --- Criterion 2: a refresh never reinterprets a compiled snapshot ---


def test_catalog_and_profile_refresh_never_reinterprets_a_compiled_snapshot() -> None:
    catalog_documents = {
        provider: catalog_example(provider) for provider in DEFAULT_PROVIDER_ALLOWLIST
    }
    profile_document = load("project-capability-profile.v1.json")
    registry_set = PublishedCatalogSet(
        catalogs=tuple(
            validate_provider_catalog(provider, catalog_documents[provider])
            for provider in sorted(DEFAULT_PROVIDER_ALLOWLIST)
        )
    )
    profile = validate_project_profile(
        profile_document,
        registry_set,
        configured_model_profiles=CONFIGURED_MODEL_PROFILES,
        configured_skills=dict(CONFIGURED_SKILLS),
        approval_actions=CONFIGURED_APPROVAL_ACTIONS,
    )
    workflow_document = workflow_example()
    selected = workflow_operation_refs(workflow_document)

    snapshot = compile_workflow_snapshot(
        workflow_document,
        profile,
        registry_set,
        selected_operations=selected,
        selected_skills=[{"id": "anvil:execute", "digest": "sha256:" + "7" * 64}],
        route="coding-local",
    )
    baseline = json.dumps(snapshot.as_dict(), sort_keys=True)
    baseline_digest = snapshot.snapshot_digest

    # Refresh the world: mutate every source document the snapshot was
    # compiled from (new operation summaries, new catalog versions, a new
    # profile revision) and mutate the very objects passed to the compiler.
    for provider, document in catalog_documents.items():
        document["catalog_version"] = "2026-12-31"
        for operation in document["operations"]:
            operation["summary"] += " (refreshed)"
        rehash_catalog(document)
    profile_document["revision"] = "2.0.0"
    refreshed_operation_digests = {
        (provider, operation["id"], operation["contract_version"]): operation["operation_digest"]
        for provider, document in catalog_documents.items()
        for operation in document["operations"]
    }
    for grant in profile_document["operations"]:
        grant["operation_digest"] = refreshed_operation_digests[
            (grant["provider"], grant["id"], grant["contract_version"])
        ]
    rehash_profile(profile_document)
    for ref in selected:
        ref["operation_digest"] = "sha256:" + "f" * 64
    workflow_document["revision"] = "9.9.9"

    refreshed_set = PublishedCatalogSet(
        catalogs=tuple(
            validate_provider_catalog(provider, catalog_documents[provider])
            for provider in sorted(DEFAULT_PROVIDER_ALLOWLIST)
        )
    )
    assert refreshed_set.catalog("anvil-state").catalog_digest != snapshot.catalogs[0].catalog_digest

    # The already-compiled snapshot is byte-identical: the refresh cannot
    # reinterpret it, and its digest still recomputes.
    assert json.dumps(snapshot.as_dict(), sort_keys=True) == baseline
    assert snapshot.snapshot_digest == baseline_digest
    payload = snapshot.as_dict()
    del payload["snapshot_digest"]
    assert contract_digest("workflow-snapshot", payload) == baseline_digest

    # The pre-refresh workflow document pins the old operation digests, so it
    # can no longer compile against the refreshed world: refusal, never a
    # silent upgrade of an already-reviewed definition.
    refreshed_profile = validate_project_profile(
        profile_document,
        refreshed_set,
        configured_model_profiles=CONFIGURED_MODEL_PROFILES,
        configured_skills=dict(CONFIGURED_SKILLS),
        approval_actions=CONFIGURED_APPROVAL_ACTIONS,
    )
    with pytest.raises(WorkflowSnapshotError, match="not allowlisted"):
        compile_workflow_snapshot(
            workflow_example(),
            refreshed_profile,
            refreshed_set,
            selected_operations=workflow_operation_refs(),
            selected_skills=[{"id": "anvil:execute", "digest": "sha256:" + "7" * 64}],
            route="coding-local",
        )

    # A workflow revision re-pinned to the refreshed digests compiles into a
    # DIFFERENT snapshot; the two run contexts stay distinct.
    refreshed_workflow = workflow_example()
    for step in refreshed_workflow["steps"]:
        if step["kind"] != "operation":
            continue
        reference = step["operation"]
        reference["operation_digest"] = refreshed_operation_digests[
            (reference["provider"], reference["id"], reference["contract_version"])
        ]
    refreshed_snapshot = compile_workflow_snapshot(
        refreshed_workflow,
        refreshed_profile,
        refreshed_set,
        selected_operations=workflow_operation_refs(refreshed_workflow),
        selected_skills=[{"id": "anvil:execute", "digest": "sha256:" + "7" * 64}],
        route="coding-local",
    )
    assert refreshed_snapshot.snapshot_digest != baseline_digest
    assert refreshed_snapshot.workflow_digest != snapshot.workflow_digest

    # And the original snapshot STILL reads back byte-identical afterwards.
    assert json.dumps(snapshot.as_dict(), sort_keys=True) == baseline


def test_snapshot_is_frozen_and_deep_copy_isolated() -> None:
    snapshot = compile_snapshot()
    expected = snapshot.as_dict()

    with pytest.raises(AttributeError):
        snapshot.operations = ()  # type: ignore[misc]
    with pytest.raises(AttributeError):
        snapshot.operations[0].operation_digest = "sha256:" + "0" * 64  # type: ignore[misc]
    with pytest.raises(AttributeError):
        snapshot.limits.max_tool_calls = 999  # type: ignore[misc]
    with pytest.raises(AttributeError):
        snapshot.catalogs[0].catalog_digest = "sha256:" + "0" * 64  # type: ignore[misc]

    # Mutating the dict projections cannot reach back into the snapshot.
    view = snapshot.as_dict()
    view["operations"].append({"provider": "project-bridge", "id": "bridge.shell.exec"})
    view["catalogs"][0]["catalog_digest"] = "sha256:" + "1" * 64
    view["limits"]["max_parallel_runs"] = 16
    block = snapshot.bridge_snapshot()
    block["catalogs"].clear()
    assert snapshot.as_dict() == expected


# --- Criterion 3: preflight drift fails closed with stable typed metadata ---


def test_preflight_passes_a_matching_snapshot() -> None:
    snapshot = compile_snapshot()
    assert preflight_snapshot(snapshot, discovered(), pinned_profile()) is None


def test_preflight_detects_operation_digest_drift() -> None:
    snapshot = compile_snapshot()
    drifted = catalog_example("anvil-state")
    claim = next(op for op in drifted["operations"] if op["id"] == "state.task.claim")
    pinned_digest = claim["operation_digest"]
    claim["summary"] += "!"
    rehash_catalog(drifted)
    current = discovered({"anvil-state": drifted})

    with pytest.raises(WorkflowSnapshotDriftError) as excinfo:
        preflight_snapshot(snapshot, current, pinned_profile())
    kinds = drift_kinds(excinfo.value)
    # The changed operation also changes its enclosing catalog digest; both
    # drifts are reported, each with stable typed metadata.
    assert kinds == {"operation_digest_changed", "catalog_digest_changed"}
    record = next(d for d in excinfo.value.drifts if d.kind == "operation_digest_changed")
    assert record.provider == "anvil-state"
    assert record.descriptor == "state.task.claim"
    assert record.pinned == pinned_digest
    assert record.current == claim["operation_digest"]
    assert record.current != record.pinned
    assert record.as_dict() == {
        "kind": "operation_digest_changed",
        "descriptor": "state.task.claim",
        "provider": "anvil-state",
        "pinned": pinned_digest,
        "current": claim["operation_digest"],
    }


def test_preflight_detects_operation_removal() -> None:
    snapshot = compile_snapshot()
    narrowed = catalog_example("anvil-state")
    pinned_digest = next(
        op["operation_digest"] for op in narrowed["operations"] if op["id"] == "state.task.claim"
    )
    narrowed["operations"] = [
        op for op in narrowed["operations"] if op["id"] != "state.task.claim"
    ]
    rehash_catalog(narrowed)
    current = discovered({"anvil-state": narrowed})

    with pytest.raises(WorkflowSnapshotDriftError) as excinfo:
        preflight_snapshot(snapshot, current, pinned_profile())
    assert drift_kinds(excinfo.value) == {"operation_removed", "catalog_digest_changed"}
    record = next(d for d in excinfo.value.drifts if d.kind == "operation_removed")
    assert (record.provider, record.descriptor) == ("anvil-state", "state.task.claim")
    assert record.pinned == pinned_digest
    assert record.current is None


def test_preflight_detects_catalog_digest_drift() -> None:
    snapshot = compile_snapshot()
    reversioned = catalog_example("anvil-serving")
    pinned_digest = reversioned["catalog_digest"]
    reversioned["catalog_version"] = "2027-01-01"
    rehash_catalog(reversioned)
    current = discovered({"anvil-serving": reversioned})

    with pytest.raises(WorkflowSnapshotDriftError) as excinfo:
        preflight_snapshot(snapshot, current, pinned_profile())
    # Only the catalog identity moved; every pinned operation still resolves.
    assert drift_kinds(excinfo.value) == {"catalog_digest_changed"}
    (record,) = excinfo.value.drifts
    assert (record.provider, record.descriptor) == ("anvil-serving", "anvil-serving")
    assert record.pinned == pinned_digest
    assert record.current == reversioned["catalog_digest"]


def test_preflight_detects_missing_catalog() -> None:
    snapshot = compile_snapshot()
    without_bridge = PublishedCatalogSet(
        catalogs=tuple(
            validate_provider_catalog(provider, catalog_example(provider))
            for provider in sorted(DEFAULT_PROVIDER_ALLOWLIST)
            if provider != "project-bridge"
        )
    )
    with pytest.raises(WorkflowSnapshotDriftError) as excinfo:
        preflight_snapshot(snapshot, without_bridge, pinned_profile())
    kinds = drift_kinds(excinfo.value)
    assert "catalog_missing" in kinds
    missing = next(d for d in excinfo.value.drifts if d.kind == "catalog_missing")
    assert missing.provider == "project-bridge"
    assert missing.current is None
    # Its pinned operations are unresolvable too and each is reported.
    removed = {d.descriptor for d in excinfo.value.drifts if d.kind == "operation_removed"}
    assert removed == {"bridge.github.commit_pr", "bridge.github.merge_and_accept"}


def test_preflight_detects_profile_digest_and_skill_drift() -> None:
    snapshot = compile_snapshot()

    # Profile identity drift alone.
    reversioned = load("project-capability-profile.v1.json")
    reversioned["revision"] = "2.0.0"
    current_profile = pinned_profile(profile=rehash_profile(reversioned))
    with pytest.raises(WorkflowSnapshotDriftError) as excinfo:
        preflight_snapshot(snapshot, discovered(), current_profile)
    assert drift_kinds(excinfo.value) == {"profile_digest_changed"}
    (record,) = excinfo.value.drifts
    assert record.descriptor == "private-delivery-default"
    assert record.pinned == snapshot.capability_profile_digest
    assert record.current == current_profile.digest

    # A refreshed skill digest is reported per-descriptor as well.
    reskilled = load("project-capability-profile.v1.json")
    reskilled["skills"][0]["digest"] = "sha256:" + "8" * 64
    current_profile = pinned_profile(
        profile=rehash_profile(reskilled),
        configured_skills={"anvil:execute": "sha256:" + "8" * 64},
    )
    with pytest.raises(WorkflowSnapshotDriftError) as excinfo:
        preflight_snapshot(snapshot, discovered(), current_profile)
    assert drift_kinds(excinfo.value) == {"profile_digest_changed", "skill_digest_changed"}
    skill_drift = next(d for d in excinfo.value.drifts if d.kind == "skill_digest_changed")
    assert skill_drift.descriptor == "anvil:execute"
    assert skill_drift.pinned == "sha256:" + "7" * 64
    assert skill_drift.current == "sha256:" + "8" * 64

    # A removed skill is a distinct stable kind.
    unskilled = load("project-capability-profile.v1.json")
    unskilled["skills"] = []
    current_profile = pinned_profile(profile=rehash_profile(unskilled))
    with pytest.raises(WorkflowSnapshotDriftError) as excinfo:
        preflight_snapshot(snapshot, discovered(), current_profile)
    assert "skill_removed" in drift_kinds(excinfo.value)


def test_preflight_detects_route_model_profile_and_approval_action_removal() -> None:
    snapshot = compile_snapshot()
    narrowed = load("project-capability-profile.v1.json")
    narrowed["model_profiles"] = ["planning-local"]
    narrowed["approval_actions"] = ["commit_pr"]
    current_profile = pinned_profile(
        profile=rehash_profile(narrowed), approval_actions=("commit_pr",)
    )
    with pytest.raises(WorkflowSnapshotDriftError) as excinfo:
        preflight_snapshot(snapshot, discovered(), current_profile)
    kinds = drift_kinds(excinfo.value)
    assert {"profile_digest_changed", "model_profile_removed", "route_removed", "approval_action_removed"} <= kinds
    route_drift = next(d for d in excinfo.value.drifts if d.kind == "route_removed")
    assert route_drift.descriptor == "coding-local"
    action_drift = next(d for d in excinfo.value.drifts if d.kind == "approval_action_removed")
    assert action_drift.descriptor == "merge_and_accept"


def test_preflight_detects_a_tampered_snapshot() -> None:
    snapshot = compile_snapshot()
    object.__setattr__(snapshot, "workflow_digest", "sha256:" + "e" * 64)
    with pytest.raises(WorkflowSnapshotDriftError) as excinfo:
        preflight_snapshot(snapshot, discovered(), pinned_profile())
    assert drift_kinds(excinfo.value) == {"snapshot_digest_mismatch"}
    (record,) = excinfo.value.drifts
    assert record.pinned == snapshot.snapshot_digest
    assert record.current != record.pinned


def test_preflight_refuses_untyped_authority_inputs() -> None:
    snapshot = compile_snapshot()
    with pytest.raises(WorkflowSnapshotError, match="compiler's own WorkflowSnapshot"):
        preflight_snapshot(snapshot.as_dict(), discovered(), pinned_profile())  # type: ignore[arg-type]
    with pytest.raises(WorkflowSnapshotError, match="registry's published set"):
        preflight_snapshot(
            snapshot,
            {p: catalog_example(p) for p in DEFAULT_PROVIDER_ALLOWLIST},  # type: ignore[arg-type]
            pinned_profile(),
        )
    with pytest.raises(WorkflowSnapshotError, match="validator's pinned profile"):
        preflight_snapshot(
            snapshot, discovered(), load("project-capability-profile.v1.json"),  # type: ignore[arg-type]
        )


def test_drift_records_use_only_declared_stable_kinds() -> None:
    with pytest.raises(WorkflowSnapshotError, match="drift kind is not declared"):
        SnapshotDrift(
            kind="something_new", descriptor="x", provider=None, pinned=None, current=None
        )


def test_workflow_contract_schema_trust_root_fails_closed(monkeypatch, tmp_path) -> None:
    from workbench import contracts as contracts_module

    contracts_module._reset_workflow_contract_validator_cache()
    monkeypatch.setattr(
        contracts_module, "_WORKFLOW_CONTRACT_SCHEMA_PATH", tmp_path / "absent.schema.json"
    )
    with pytest.raises(ContractValidationError, match="schema is unavailable"):
        contracts_module.workflow_contract_validator()

    base = json.loads(
        (ROOT / "docs" / "contracts" / "schemas" / "workflow.v2.schema.json").read_text(encoding="utf-8")
    )
    del base["properties"]["steps"]["maxItems"]
    drifted = tmp_path / "drifted.schema.json"
    drifted.write_text(json.dumps(base), encoding="utf-8")
    contracts_module._reset_workflow_contract_validator_cache()
    monkeypatch.setattr(contracts_module, "_WORKFLOW_CONTRACT_SCHEMA_PATH", drifted)
    with pytest.raises(ContractValidationError, match="no longer closes its root object or bounds steps"):
        contracts_module.workflow_contract_validator()
    contracts_module._reset_workflow_contract_validator_cache()
