"""End-to-end integration of catalog discovery and capability pinning.

This is the T004 integrate-and-qualify fixture
(state-context-operations:T004).  It wires the three already-implemented,
individually-tested halves together through ONE discovered catalog set:

    provider-catalog discovery   ->  project capability profile
    (workbench.provider_catalogs)     (workbench.capability_profiles)
                              \\->  immutable workflow snapshot + preflight
                                   (workbench.workflow_snapshot)

Discovery runs once through a real :class:`ProviderCatalogRegistry` over
injected/local sources (State via an injected ``state_describe`` runner, the
other providers via reviewed ``local_json`` files) and pins the frozen
:class:`PublishedCatalogSet`.  The SAME published set validates the project
profile, compiles the workflow snapshot, and preflights it, so the integration
proves the pinned provider/operation/skill/route/approval descriptors and
digests flow coherently from ``anvil describe`` through to an immutable
:class:`WorkflowSnapshot` and a fail-closed preflight.

The whole pipeline is hermetic: every transport is an injected runner or a
tmp-path file and no live CLI is executed.  This is integration-of-existing
parts only; the trio is still deliberately NOT wired into live workflow
queueing (live qualification stays gated on providers actually serving these
catalogs, fakoli/anvil#178).

Acceptance-criterion map (state-context-operations:T004):

* Criterion 1 (discover reviewed catalogs, validate a project profile, compile
  an immutable snapshot of every selected descriptor and digest):
  ``test_pipeline_discovers_validates_and_compiles_one_immutable_snapshot``,
  ``test_discovery_runs_once_and_the_same_pinned_set_feeds_every_stage``.
* Criterion 2 (unknown/stale/duplicate/conflicting/unprofiled providers,
  operations, routes, skills, approvals, budgets, or digests fail before
  queueing):
  ``test_unknown_or_conflicting_catalog_fails_discovery_before_any_profile``,
  ``test_stale_or_unprofiled_operation_fails_before_compilation``,
  ``test_unpinned_route_skill_or_approval_action_fails_before_compilation``,
  ``test_budget_limits_are_pinned_from_the_reviewed_profile_only``.
* Criterion 3 (browser or model input cannot extend the reviewed profile or
  select an unpinned capability):
  ``test_model_supplied_authority_cannot_widen_the_reviewed_profile``,
  ``test_discovered_but_unprofiled_capability_cannot_be_selected``.
* Criterion 4 (a later catalog or profile refresh does not reinterpret an
  already-compiled workflow):
  ``test_catalog_refresh_never_reinterprets_a_compiled_snapshot``,
  ``test_profile_refresh_is_caught_by_preflight_not_by_rewrite``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from workbench.capability_profiles import (
    CapabilityProfileError,
    PinnedCapabilityProfile,
    validate_project_profile,
)
from workbench.contracts import contract_digest
from workbench.provider_catalogs import (
    DEFAULT_PROVIDER_ALLOWLIST,
    CatalogSource,
    ProviderCatalogError,
    ProviderCatalogRegistry,
    PublishedCatalogSet,
)
from workbench.workflow_snapshot import (
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


# --- fixture documents ------------------------------------------------------


def load(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def catalog_example(provider: str) -> dict:
    return load(f"{provider}.catalog.v1.json")


def profile_example() -> dict:
    return load("project-capability-profile.v1.json")


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


def describe_envelope(catalog: dict, prefix: str = "") -> str:
    return prefix + json.dumps({"ok": True, "command": "describe", "data": catalog})


def write_catalog(directory: Path, catalog: dict, name: str | None = None) -> str:
    path = directory / (name or f"{catalog['provider']}.catalog.json")
    path.write_text(json.dumps(catalog), encoding="utf-8")
    return str(path)


def workflow_operation_refs(workflow: dict | None = None) -> list[dict]:
    """The de-duplicated operation references the workflow's steps pin."""
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


def workflow_skill_refs(workflow: dict | None = None) -> list[dict]:
    document = workflow if workflow is not None else workflow_example()
    refs: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for step in document["steps"]:
        if step.get("kind") != "agent":
            continue
        for skill in step.get("skills", ()):
            key = (str(skill["id"]), str(skill["digest"]))
            if key not in seen:
                seen.add(key)
                refs.append(copy.deepcopy(skill))
    return refs


# --- the integrated pipeline (one discovered set feeds every stage) ---------


class IntegratedPipeline:
    """Discovery + profile + snapshot wired over ONE published catalog set.

    A single :class:`ProviderCatalogRegistry` discovers and pins the frozen
    :class:`PublishedCatalogSet` once (State via an injected ``state_describe``
    runner recording argv, the other providers via reviewed ``local_json``
    files).  The SAME published set validates the profile and compiles the
    workflow snapshot, mirroring exactly how a live bridge would resolve one
    discovered catalog set and reuse it for every downstream authority check.
    """

    def __init__(
        self,
        directory: Path,
        *,
        state_catalog: dict | None = None,
        serving_catalog: dict | None = None,
        bridge_catalog: dict | None = None,
        profile: dict | None = None,
    ) -> None:
        self.describe_calls: list[list[str]] = []

        def describe_runner(args) -> str:
            self.describe_calls.append(list(args))
            return describe_envelope(
                state_catalog if state_catalog is not None else catalog_example("anvil-state"),
                prefix="anvil status line\n",
            )

        sources = [
            CatalogSource("anvil-state", "state_describe", "anvil describe --json"),
            CatalogSource(
                "anvil-serving",
                "local_json",
                write_catalog(
                    directory,
                    serving_catalog if serving_catalog is not None else catalog_example("anvil-serving"),
                ),
            ),
            CatalogSource(
                "project-bridge",
                "local_json",
                write_catalog(
                    directory,
                    bridge_catalog if bridge_catalog is not None else catalog_example("project-bridge"),
                ),
            ),
        ]
        self.registry = ProviderCatalogRegistry(sources, runner=describe_runner)
        self._profile_document = profile if profile is not None else profile_example()

    def published(self) -> PublishedCatalogSet:
        return self.registry.published()

    def profile(self, **overrides) -> PinnedCapabilityProfile:
        kwargs: dict = {
            "configured_model_profiles": CONFIGURED_MODEL_PROFILES,
            "configured_skills": dict(CONFIGURED_SKILLS),
            "approval_actions": CONFIGURED_APPROVAL_ACTIONS,
        }
        kwargs.update(overrides)
        return validate_project_profile(self._profile_document, self.published(), **kwargs)

    def compile(
        self,
        *,
        workflow: dict | None = None,
        profile: PinnedCapabilityProfile | None = None,
        **overrides,
    ) -> WorkflowSnapshot:
        document = workflow if workflow is not None else workflow_example()
        kwargs: dict = {
            "selected_operations": workflow_operation_refs(document),
            "selected_skills": workflow_skill_refs(document),
            "route": "coding-local",
        }
        kwargs.update(overrides)
        return compile_workflow_snapshot(
            document,
            profile if profile is not None else self.profile(),
            self.published(),
            **kwargs,
        )


def drift_kinds(error: WorkflowSnapshotDriftError) -> set[str]:
    return {drift.kind for drift in error.drifts}


# --- Criterion 1: discover, validate, compile one immutable snapshot --------


def test_pipeline_discovers_validates_and_compiles_one_immutable_snapshot(tmp_path: Path) -> None:
    """Criterion 1: discovery -> profile -> immutable snapshot of every pin."""
    pipeline = IntegratedPipeline(tmp_path)

    published = pipeline.published()
    profile = pipeline.profile()
    snapshot = pipeline.compile(profile=profile)

    # Discovery genuinely ran the injected describe transport (never a
    # hardcoded path) and published every configured provider.
    assert pipeline.describe_calls == [["anvil", "describe", "--json"]]
    assert published.providers == ("anvil-serving", "anvil-state", "project-bridge")

    # The profile pinned exactly the reviewed operations against the discovered
    # catalogs, at their exact discovered digests.
    profile_fixture = profile_example()
    assert profile.id == profile_fixture["id"]
    assert profile.digest == profile_fixture["digest"]
    assert {(g.provider, g.id, g.operation_digest) for g in profile.operations} == {
        (g["provider"], g["id"], g["operation_digest"]) for g in profile_fixture["operations"]
    }

    # Every discovered catalog is pinned into the snapshot at its exact digest.
    assert {c.provider: c.catalog_digest for c in snapshot.catalogs} == {
        provider: catalog_example(provider)["catalog_digest"]
        for provider in DEFAULT_PROVIDER_ALLOWLIST
    }

    # Every selected operation is pinned and source-attributed to the catalog
    # revision discovery produced.
    expected_ops = {
        (ref["provider"], ref["id"], ref["contract_version"], ref["operation_digest"])
        for ref in workflow_operation_refs()
    }
    assert {
        (op.provider, op.id, op.contract_version, op.operation_digest) for op in snapshot.operations
    } == expected_ops
    for op in snapshot.operations:
        assert op.source_catalog_digest == published.catalog(op.provider).catalog_digest

    # Skills, route, model profiles, approval actions, and budget are pinned
    # and attributed to the reviewed profile.
    assert [s.as_dict() for s in snapshot.skills] == [
        {"id": "anvil:execute", "digest": "sha256:" + "7" * 64,
         "source_profile_digest": profile_fixture["digest"]}
    ]
    assert snapshot.route is not None and snapshot.route.model_profile == "coding-local"
    assert snapshot.model_profiles == ("coding-local",)
    assert snapshot.approval_actions == ("commit_pr", "merge_and_accept")
    assert snapshot.limits.as_dict() == profile_fixture["limits"]

    # The snapshot advertises a recomputable digest, and preflight against the
    # same discovered world passes.
    payload = snapshot.as_dict()
    assert payload["snapshot_digest"] == snapshot.snapshot_digest
    del payload["snapshot_digest"]
    assert contract_digest("workflow-snapshot", payload) == snapshot.snapshot_digest
    assert preflight_snapshot(snapshot, pipeline.published(), profile) is None


def test_discovery_runs_once_and_the_same_pinned_set_feeds_every_stage(tmp_path: Path) -> None:
    """Criterion 1: one cached published set is reused by profile and compile."""
    pipeline = IntegratedPipeline(tmp_path)
    first = pipeline.published()
    # Profile validation and compilation both reuse the cached published set;
    # nothing rediscovers, so the describe transport ran exactly once.
    profile = pipeline.profile()
    pipeline.compile(profile=profile)
    assert pipeline.published() is first
    assert pipeline.describe_calls == [["anvil", "describe", "--json"]]


# --- Criterion 2: fail before queueing --------------------------------------


def test_unknown_or_conflicting_catalog_fails_discovery_before_any_profile(tmp_path: Path) -> None:
    """Criterion 2: a bad catalog fails discovery, so no profile/snapshot forms."""
    # A source naming a provider outside the allowlist is refused at
    # construction, before any bytes are read.
    with pytest.raises(ProviderCatalogError, match="outside the configured allowlist"):
        ProviderCatalogRegistry(
            [CatalogSource("mystery", "local_json", str(tmp_path / "x.json"))]
        )

    # A duplicate operation in a discovered catalog fails the whole load.
    duplicated = catalog_example("project-bridge")
    duplicated["operations"].append(copy.deepcopy(duplicated["operations"][0]))
    dup_pipeline = IntegratedPipeline(tmp_path, bridge_catalog=rehash_catalog(duplicated))
    with pytest.raises(ProviderCatalogError, match="duplicate operation"):
        dup_pipeline.published()

    # A digest that does not recompute fails closed before any semantic check.
    drifted = catalog_example("anvil-serving")
    drifted["operations"][0]["summary"] += "!"  # digest deliberately NOT recomputed
    drift_pipeline = IntegratedPipeline(tmp_path, serving_catalog=drifted)
    with pytest.raises(ProviderCatalogError, match="digest validation"):
        drift_pipeline.published()

    # Two sources claiming one provider with different digests conflict.
    original = catalog_example("anvil-serving")
    revised = catalog_example("anvil-serving")
    revised["catalog_version"] = "2027-01-01"
    conflicting = ProviderCatalogRegistry(
        [
            CatalogSource("anvil-serving", "local_json", write_catalog(tmp_path, original, "a.json")),
            CatalogSource("anvil-serving", "local_json", write_catalog(tmp_path, rehash_catalog(revised), "b.json")),
        ]
    )
    with pytest.raises(ProviderCatalogError, match="conflicting catalogs claim provider anvil-serving"):
        conflicting.published()


def test_stale_or_unprofiled_operation_fails_before_compilation(tmp_path: Path) -> None:
    """Criterion 2: a profile pin stale against discovery fails at validation."""
    # A profile grant whose digest is stale against the discovered catalog is
    # refused when the profile is validated -- before any workflow compiles.
    stale_profile = profile_example()
    stale_profile["operations"][0]["operation_digest"] = "sha256:" + "a" * 64
    pipeline = IntegratedPipeline(tmp_path, profile=rehash_profile(stale_profile))
    with pytest.raises(CapabilityProfileError, match="stale against the discovered"):
        pipeline.profile()

    # A reviewed profile grant whose provider was not discovered is refused
    # too: narrow discovery so project-bridge is absent, and the reviewed
    # bridge grants can no longer pin.
    partial = IntegratedPipeline(tmp_path)
    published_without_bridge = PublishedCatalogSet(
        catalogs=tuple(c for c in partial.published().catalogs if c.provider != "project-bridge")
    )
    with pytest.raises(CapabilityProfileError, match="no discovered catalog"):
        validate_project_profile(
            profile_example(),
            published_without_bridge,
            configured_model_profiles=CONFIGURED_MODEL_PROFILES,
            configured_skills=dict(CONFIGURED_SKILLS),
            approval_actions=CONFIGURED_APPROVAL_ACTIONS,
        )


def test_unpinned_route_skill_or_approval_action_fails_before_compilation(tmp_path: Path) -> None:
    """Criterion 2: a route/skill/approval outside the profile fails to compile."""
    pipeline = IntegratedPipeline(tmp_path)

    # A route the reviewed profile does not pin cannot be selected.
    with pytest.raises(WorkflowSnapshotError, match="route is not pinned"):
        pipeline.compile(route="research-remote")

    # A selected skill absent from the profile cannot be pinned.
    with pytest.raises(WorkflowSnapshotError, match="not pinned by the capability profile"):
        pipeline.compile(
            selected_skills=[
                {"id": "anvil:execute", "digest": "sha256:" + "7" * 64},
                {"id": "anvil:finish", "digest": "sha256:" + "8" * 64},
            ]
        )

    # A workflow approval action outside a narrowed profile cannot compile.
    narrowed = profile_example()
    narrowed["approval_actions"] = ["commit_pr"]
    narrowed_pipeline = IntegratedPipeline(tmp_path, profile=rehash_profile(narrowed))
    with pytest.raises(WorkflowSnapshotError, match="approval action is not pinned"):
        narrowed_pipeline.compile(profile=narrowed_pipeline.profile(approval_actions=("commit_pr",)))


def test_budget_limits_are_pinned_from_the_reviewed_profile_only(tmp_path: Path) -> None:
    """Criterion 2: the compiled budget is exactly the reviewed profile's, not
    a workflow- or caller-chosen value."""
    tightened = profile_example()
    tightened["limits"] = {"max_parallel_runs": 1, "max_agent_turns": 3, "max_tool_calls": 5}
    pipeline = IntegratedPipeline(tmp_path, profile=rehash_profile(tightened))
    snapshot = pipeline.compile()
    assert snapshot.limits.as_dict() == {
        "max_parallel_runs": 1, "max_agent_turns": 3, "max_tool_calls": 5
    }


# --- Criterion 3: model/browser input cannot widen the reviewed profile -----


def test_model_supplied_authority_cannot_widen_the_reviewed_profile(tmp_path: Path) -> None:
    """Criterion 3: caller-assembled catalog/profile mappings are refused by type.

    A hub- or model-supplied mapping (the shape a browser payload would take)
    has no parameter to arrive through: validation and compilation accept only
    the registry's own published set and the validator's own pinned profile.
    """
    pipeline = IntegratedPipeline(tmp_path)
    published = pipeline.published()
    profile = pipeline.profile()

    caller_assembled_catalogs = {p: catalog_example(p) for p in DEFAULT_PROVIDER_ALLOWLIST}
    with pytest.raises(CapabilityProfileError, match="registry's published set"):
        validate_project_profile(
            profile_example(),
            caller_assembled_catalogs,  # type: ignore[arg-type]
            configured_model_profiles=CONFIGURED_MODEL_PROFILES,
            configured_skills=dict(CONFIGURED_SKILLS),
            approval_actions=CONFIGURED_APPROVAL_ACTIONS,
        )
    with pytest.raises(WorkflowSnapshotError, match="registry's published set"):
        compile_workflow_snapshot(
            workflow_example(), profile, caller_assembled_catalogs,  # type: ignore[arg-type]
            selected_operations=workflow_operation_refs(),
        )
    with pytest.raises(WorkflowSnapshotError, match="validator's pinned profile"):
        compile_workflow_snapshot(
            workflow_example(), profile_example(), published,  # type: ignore[arg-type]
            selected_operations=workflow_operation_refs(),
        )

    # A model that adds a capability to its selection cannot widen authority:
    # a schema-valid model profile the reviewed profile does not pin is refused.
    with pytest.raises(CapabilityProfileError, match="not operator-configured"):
        pipeline.profile(configured_model_profiles=("planning-local",))  # drops coding-local


def test_discovered_but_unprofiled_capability_cannot_be_selected(tmp_path: Path) -> None:
    """Criterion 3: an operation discovered in a catalog but absent from the
    reviewed profile cannot be selected into a snapshot."""
    pipeline = IntegratedPipeline(tmp_path)
    # state.project.snapshot is discovered in the anvil-state catalog but is not
    # in the reviewed delivery profile's allowlist.
    unprofiled = next(
        op for op in catalog_example("anvil-state")["operations"]
        if op["id"] == "state.project.snapshot"
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
        pipeline.compile(selected_operations=refs)


# --- Criterion 4: a refresh never reinterprets a compiled snapshot ----------


def test_catalog_refresh_never_reinterprets_a_compiled_snapshot(tmp_path: Path) -> None:
    """Criterion 4: after a catalog refresh, preflight catches drift; the
    already-compiled snapshot stays byte-identical and never silently upgrades."""
    pipeline = IntegratedPipeline(tmp_path)
    profile = pipeline.profile()
    snapshot = pipeline.compile(profile=profile)
    baseline = json.dumps(snapshot.as_dict(), sort_keys=True)
    baseline_digest = snapshot.snapshot_digest

    # Refresh discovery: a NEW registry over a mutated anvil-state catalog
    # whose state.task.claim operation (which the snapshot actually pins)
    # changes summary -> new operation + catalog digests.
    refreshed_state = catalog_example("anvil-state")
    claim_op = next(
        op for op in refreshed_state["operations"] if op["id"] == "state.task.claim"
    )
    claim_op["summary"] += " (refreshed)"
    rehash_catalog(refreshed_state)
    refreshed = IntegratedPipeline(tmp_path, state_catalog=refreshed_state)
    refreshed_published = refreshed.published()
    assert (
        refreshed_published.catalog("anvil-state").catalog_digest
        != pipeline.published().catalog("anvil-state").catalog_digest
    )

    # The already-compiled snapshot is byte-identical: a refresh cannot
    # reinterpret it, and its digest still recomputes.
    assert json.dumps(snapshot.as_dict(), sort_keys=True) == baseline
    assert snapshot.snapshot_digest == baseline_digest

    # Preflight against the refreshed world fails closed with stable typed
    # drift metadata, not a silent upgrade of the reviewed run context.
    refreshed_profile = validate_project_profile(
        _profile_repinned_to(refreshed_published),
        refreshed_published,
        configured_model_profiles=CONFIGURED_MODEL_PROFILES,
        configured_skills=dict(CONFIGURED_SKILLS),
        approval_actions=CONFIGURED_APPROVAL_ACTIONS,
    )
    with pytest.raises(WorkflowSnapshotDriftError) as excinfo:
        preflight_snapshot(snapshot, refreshed_published, refreshed_profile)
    assert {"operation_digest_changed", "catalog_digest_changed"} <= drift_kinds(excinfo.value)
    op_drift = next(d for d in excinfo.value.drifts if d.kind == "operation_digest_changed")
    assert op_drift.provider == "anvil-state"
    assert op_drift.descriptor == "state.task.claim"
    assert op_drift.pinned != op_drift.current

    # And the original snapshot STILL reads back byte-identical afterwards.
    assert json.dumps(snapshot.as_dict(), sort_keys=True) == baseline


def _profile_repinned_to(published: PublishedCatalogSet) -> dict:
    """Return the reviewed profile with operation digests re-pinned to a
    refreshed discovered catalog set (a freshly reviewed profile revision)."""
    profile = profile_example()
    current = {
        (catalog.provider, op.id, op.contract_version): op.operation_digest
        for catalog in published.catalogs
        for op in catalog.operations
    }
    for grant in profile["operations"]:
        grant["operation_digest"] = current[
            (grant["provider"], grant["id"], grant["contract_version"])
        ]
    profile["revision"] = "2.0.0"
    return rehash_profile(profile)


def test_profile_refresh_is_caught_by_preflight_not_by_rewrite(tmp_path: Path) -> None:
    """Criterion 4: a refreshed profile revision drifts the pinned snapshot at
    preflight; the compiled snapshot's profile attribution never rewrites."""
    pipeline = IntegratedPipeline(tmp_path)
    snapshot = pipeline.compile()
    pinned_profile_digest = snapshot.capability_profile_digest

    reversioned = profile_example()
    reversioned["revision"] = "2.0.0"
    refreshed_profile = validate_project_profile(
        rehash_profile(reversioned),
        pipeline.published(),
        configured_model_profiles=CONFIGURED_MODEL_PROFILES,
        configured_skills=dict(CONFIGURED_SKILLS),
        approval_actions=CONFIGURED_APPROVAL_ACTIONS,
    )
    with pytest.raises(WorkflowSnapshotDriftError) as excinfo:
        preflight_snapshot(snapshot, pipeline.published(), refreshed_profile)
    assert "profile_digest_changed" in drift_kinds(excinfo.value)
    record = next(d for d in excinfo.value.drifts if d.kind == "profile_digest_changed")
    assert record.pinned == pinned_profile_digest
    assert record.current == refreshed_profile.digest
    # The snapshot's own profile attribution is unchanged by the refresh.
    assert snapshot.capability_profile_digest == pinned_profile_digest
