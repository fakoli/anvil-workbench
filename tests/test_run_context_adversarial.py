"""Adversarial qualification of run-context immutability and safe rendering.

state-context-operations:T005.4 -- the adversarial hardening pass over the
immutable run context (T005.1), its capture-before-dispatch persistence
(T005.2), and its historical read API (T005.3).

Acceptance-criterion map:

* Criterion 1 (run context exists before bridge dispatch and cannot be changed
  afterward):
  ``test_context_is_stored_before_dispatch_and_frozen_afterward``,
  ``test_stored_snapshot_cannot_be_rewritten_by_a_later_capture``.
* Criterion 2 (rename tasks/PRDs and refresh catalogs after queueing without
  changing historical output):
  ``test_world_refresh_after_queueing_never_changes_historical_output``.
* Criterion 3 (safe rendering; trusted/untrusted separation):
  ``test_adversarial_prose_across_every_field_is_scrubbed_in_the_render``,
  ``test_trusted_and_untrusted_stay_separate_and_reject_leak_by_addition``,
  ``test_every_run_context_route_is_cross_project_indistinct``.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from workbench.api import create_app
from workbench.config import Settings
from workbench.graph import NullGraph
from workbench.models import (
    RunContext,
    RunContextError,
    RunIdentity,
    UntrustedEvidence,
    UntrustedTask,
    UntrustedTaskRef,
)
from workbench.run_context_store import (
    MemoryRunContextStore,
    RunContextImmutableError,
    UnknownRunContextError,
    dispatch_with_run_context,
)
from workbench.store import MemoryStore

from _support import build_run_context, compile_delivery_snapshot, load_example

ACTOR = {"X-Workbench-Actor": "operator"}


def _client(store: MemoryRunContextStore | None) -> TestClient:
    settings = Settings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator"}), bridge_bootstrap_token="",
        anvil_router_base_url="", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=True,
    )
    return TestClient(create_app(
        settings=settings, store=MemoryStore(), graph=NullGraph(), run_context_store=store,
    ))


# --- Criterion 1: exists before dispatch, frozen afterward ------------------


def test_context_is_stored_before_dispatch_and_frozen_afterward():
    store = MemoryRunContextStore()
    observed_at_dispatch: list[dict] = []

    def dispatch(run_context: RunContext) -> None:
        # The context is already durably readable at dispatch time.
        observed_at_dispatch.append(store.get("project_a", run_context.run_id).as_dict())

    persisted = dispatch_with_run_context(
        store, "project_a", lambda: build_run_context(), dispatch,
    )
    assert observed_at_dispatch == [persisted.as_dict()]

    # After dispatch the stored record is frozen: the returned object cannot be
    # mutated, and re-reading yields the identical snapshot.
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        persisted.untrusted.task.title = "changed after dispatch"  # type: ignore[misc]
    assert store.get("project_a", persisted.run_id).as_dict() == persisted.as_dict()

    # A build that fails to resolve a required field never reaches dispatch.
    dispatched: list[str] = []
    with pytest.raises(RunContextError):
        dispatch_with_run_context(
            MemoryRunContextStore(), "project_a",
            lambda: build_run_context(context_id="not-ctx"),
            lambda rc: dispatched.append(rc.run_id),
        )
    assert dispatched == []


def test_stored_snapshot_cannot_be_rewritten_by_a_later_capture():
    store = MemoryRunContextStore()
    original = build_run_context()
    store.capture("project_a", original)
    baseline = original.as_dict()

    # Every distinct later capture for the same run is refused, regardless of
    # which field changed; the stored queue-time snapshot never moves.
    variants = [
        build_run_context(
            task=UntrustedTask(
                ref=UntrustedTaskRef(prd_id="release-beta", task_id="T001", prd_revision=8),
                title="renamed", acceptance_criteria=("x",), work_packet_digest="sha256:" + "d" * 64,
            ),
        ),
        build_run_context(
            identity=RunIdentity(
                run_id="run_shared_1", session_id="sess_other", bridge_id="bridge_other",
                worktree_name="checkout-b",
            ),
        ),
    ]
    for variant in variants:
        assert variant.run_id == original.run_id
        with pytest.raises(RunContextImmutableError):
            store.capture("project_a", variant)
    assert store.get("project_a", original.run_id).as_dict() == baseline


# --- Criterion 2: world refresh after queueing -------------------------------


def test_world_refresh_after_queueing_never_changes_historical_output():
    store = MemoryRunContextStore()
    queued = build_run_context()
    store.capture("project_a", queued)
    baseline = queued.as_dict()

    with _client(store) as client_:
        before = client_.get(
            f"/api/projects/project_a/runs/{queued.run_id}/context", headers=ACTOR,
        ).json()["context"]

    # Refresh the world: a mutated anvil-state catalog produces a snapshot with
    # different workflow/catalog/operation digests, and the task is renamed.
    refreshed_state = load_example("anvil-state.catalog.v1.json")
    claim = next(op for op in refreshed_state["operations"] if op["id"] == "state.task.claim")
    claim["summary"] += " (refreshed)"
    from workbench.contracts import contract_digest

    for op in refreshed_state["operations"]:
        op["operation_digest"] = contract_digest("operation", op)
    refreshed_state["catalog_digest"] = contract_digest("catalog", refreshed_state)

    # A run context built from the refreshed world differs; capturing it for the
    # SAME run is refused, so the historical record cannot be reinterpreted.
    from workbench.capability_profiles import validate_project_profile
    from workbench.provider_catalogs import (
        DEFAULT_PROVIDER_ALLOWLIST,
        PublishedCatalogSet,
        validate_provider_catalog,
    )
    from workbench.workflow_snapshot import compile_workflow_snapshot

    published = PublishedCatalogSet(
        catalogs=tuple(
            validate_provider_catalog(
                provider,
                refreshed_state if provider == "anvil-state"
                else load_example(f"{provider}.catalog.v1.json"),
            )
            for provider in sorted(DEFAULT_PROVIDER_ALLOWLIST)
        )
    )
    profile_doc = load_example("project-capability-profile.v1.json")
    refreshed_digests = {
        (catalog.provider, op.id, op.contract_version): op.operation_digest
        for catalog in published.catalogs
        for op in catalog.operations
    }
    for grant in profile_doc["operations"]:
        grant["operation_digest"] = refreshed_digests[
            (grant["provider"], grant["id"], grant["contract_version"])
        ]
    profile_doc["digest"] = contract_digest("profile", profile_doc)
    profile = validate_project_profile(
        profile_doc, published,
        configured_model_profiles=("coding-local", "planning-local"),
        configured_skills={"anvil:execute": "sha256:" + "7" * 64},
        approval_actions=("commit_pr", "merge_and_accept"),
    )
    workflow = load_example("delivery.workflow.v2.json")
    for step in workflow["steps"]:
        if step["kind"] == "operation":
            ref = step["operation"]
            ref["operation_digest"] = refreshed_digests[
                (ref["provider"], ref["id"], ref["contract_version"])
            ]
    selected: list[dict] = []
    seen: set[tuple] = set()
    for step in workflow["steps"]:
        if step["kind"] != "operation":
            continue
        key = tuple(sorted(step["operation"].items()))
        if key not in seen:
            seen.add(key)
            selected.append(dict(step["operation"]))
    refreshed_snapshot = compile_workflow_snapshot(
        workflow, profile, published, selected_operations=selected,
        selected_skills=[{"id": "anvil:execute", "digest": "sha256:" + "7" * 64}],
        route="coding-local",
    )
    assert refreshed_snapshot.workflow_digest != queued.trusted.workflow.workflow_digest

    refreshed_context = build_run_context(
        snapshot=refreshed_snapshot,
        task=UntrustedTask(
            ref=UntrustedTaskRef(prd_id="release-beta", task_id="T001", prd_revision=11),
            title="Renamed after a full world refresh",
            acceptance_criteria=("A criterion nobody reviewed at queue time",),
            work_packet_digest="sha256:" + "e" * 64,
        ),
    )
    assert refreshed_context.run_id == queued.run_id
    with pytest.raises(RunContextImmutableError):
        store.capture("project_a", refreshed_context)

    with _client(store) as client_:
        after = client_.get(
            f"/api/projects/project_a/runs/{queued.run_id}/context", headers=ACTOR,
        ).json()["context"]

    # Titles, revisions, and every pinned digest returned for the run are
    # exactly the queue-time values, untouched by the refresh.
    assert after == before == baseline
    assert after["untrusted"]["task"]["title"] == "Add a documented operation contract"
    assert after["untrusted"]["task"]["ref"]["prd_revision"] == 5
    assert after["trusted"]["workflow"]["digest"] == queued.trusted.workflow.workflow_digest


# --- Criterion 3: safe rendering + trusted/untrusted separation --------------


def test_adversarial_prose_across_every_field_is_scrubbed_in_the_render():
    store = MemoryRunContextStore()
    secret = "Bearer sk-live-abc123DEADBEEF token=supersecretvalue api_key=leakvalue"
    context = build_run_context(
        task=UntrustedTask(
            ref=UntrustedTaskRef(prd_id="release-beta", task_id="T001", prd_revision=5),
            title=f"title {secret}",
            acceptance_criteria=(f"criterion {secret}",),
            work_packet_digest="sha256:" + "8" * 64,
            scope=(f"scope {secret}",),
            verification_plan=(f"verify {secret}",),
        ),
        evidence=(UntrustedEvidence(citation=f"cite {secret}", summary=f"summary {secret}"),),
    )
    store.capture("project_a", context)
    with _client(store) as client_:
        raw = client_.get(
            f"/api/projects/project_a/runs/{context.run_id}/context", headers=ACTOR,
        ).text
    for leaked in ("sk-live-abc123DEADBEEF", "supersecretvalue", "leakvalue"):
        assert leaked not in raw
    assert "[REDACTED]" in raw


def test_trusted_and_untrusted_stay_separate_and_reject_leak_by_addition():
    store = MemoryRunContextStore()
    context = build_run_context()
    store.capture("project_a", context)
    with _client(store) as client_:
        body = client_.get(
            f"/api/projects/project_a/runs/{context.run_id}/context", headers=ACTOR,
        ).json()["context"]

    # Two separately labeled structures; authority pins live only under trusted,
    # PRD/task prose only under untrusted.
    assert set(body) == {"schema_version", "context_id", "trusted", "untrusted"}
    trusted_blob = json.dumps(body["trusted"])
    untrusted_blob = json.dumps(body["untrusted"])
    assert context.trusted.workflow.workflow_digest in trusted_blob
    assert context.trusted.workflow.workflow_digest not in untrusted_blob
    assert "Add a documented operation contract" in untrusted_blob
    assert "Add a documented operation contract" not in trusted_blob

    # Leak-by-addition is rejected on BOTH structures: an undeclared field
    # anywhere fails the closed-set reconstruction rather than riding through.
    poisoned_trusted = json.loads(json.dumps(body))
    poisoned_trusted["trusted"]["identity"]["state_db_path"] = "/var/anvil/state.db"
    with pytest.raises(RunContextError, match="identity carries undeclared fields"):
        RunContext.from_dict(poisoned_trusted)

    poisoned_untrusted = json.loads(json.dumps(body))
    poisoned_untrusted["untrusted"]["evidence"][0]["bridge_command"] = "codex exec"
    with pytest.raises(RunContextError, match="evidence carries undeclared fields"):
        RunContext.from_dict(poisoned_untrusted)


def _run_context_routes(app) -> list[str]:
    """Every registered run-context route template (derive, don't hardcode).

    Derived from the app's OpenAPI paths -- the authoritative registry of every
    mounted route -- so a future run-context endpoint is automatically covered
    by the cross-project probe below.
    """
    return [
        path for path in app.openapi()["paths"]
        if path.startswith("/api/projects") and "/runs/" in path and path.endswith("/context")
    ]


def test_every_run_context_route_is_cross_project_indistinct():
    store = MemoryRunContextStore()
    context = build_run_context()
    store.capture("project_b", context)
    app = create_app(
        settings=Settings(
            database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
            owner="operator", approvers=frozenset({"operator"}), bridge_bootstrap_token="",
            anvil_router_base_url="", anvil_router_token="",
            identity_header="X-Workbench-Actor", allow_insecure_dev_actor=True,
        ),
        store=MemoryStore(), graph=NullGraph(), run_context_store=store,
    )
    routes = _run_context_routes(app)
    # The lane adds exactly one run-context route; if that ever grows, this probe
    # must still cover every one (it iterates the derived list).
    assert routes, "expected at least one run-context route to probe"

    with TestClient(app) as client_:
        for template in routes:
            foreign = template.replace("{project_id}", "project_a").replace("{run_id}", context.run_id)
            missing = template.replace("{project_id}", "project_a").replace("{run_id}", "run_absent")
            owner_missing = template.replace("{project_id}", "project_b").replace("{run_id}", "run_absent")
            foreign_resp = client_.get(foreign, headers=ACTOR)
            missing_resp = client_.get(missing, headers=ACTOR)
            owner_missing_resp = client_.get(owner_missing, headers=ACTOR)
            assert foreign_resp.status_code == 404, template
            # A foreign run is byte-identical to a genuinely missing one.
            assert foreign_resp.content == missing_resp.content == owner_missing_resp.content

    # The store itself refuses the cross-project read with the indistinct error.
    with pytest.raises(UnknownRunContextError):
        store.get("project_a", context.run_id)
