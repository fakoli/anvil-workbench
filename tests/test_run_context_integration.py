"""End-to-end integration of the immutable run-context lane.

state-context-operations:T005 -- the capstone fixture for feature F003.  Unlike
the focused unit and adversarial suites, this wires the WHOLE run-context chain
together through ONE queued run:

    discover catalogs -> validate profile -> compile snapshot   (T004)
        -> RunContext.capture (trusted policy + untrusted data)  (T005.1)
        -> dispatch_with_run_context: persist BEFORE dispatch    (T005.2)
        -> GET /api/projects/{id}/runs/{run}/context             (T005.3)

Everything is hermetic and derived from the checked-in contract examples; no
live CLI, network, or bridge is touched.  The run context is deliberately NOT
wired into the live bridge poll loop -- this fixture proves the integrated
capture/persist/read-back path is coherent and fail-closed, not live activation.

Acceptance-criterion map:

* Criterion 1 (queue a run, prove the complete snapshot exists before dispatch,
  read the SAME stored snapshot through the historical API):
  ``test_queue_persists_before_dispatch_and_reads_back_the_same_snapshot``.
* Criterion 2 (later renames and catalog/route/skill refreshes never rewrite
  historical titles, revisions, descriptors, or digests):
  ``test_post_queue_renames_and_refreshes_never_rewrite_history``.
* Criterion 3 (trusted policy and untrusted data remain immutable and
  separately labeled):
  ``test_trusted_and_untrusted_remain_immutable_and_separately_labeled``.
* Criterion 4 (the rendered summary leaks no secret, path, command, credential,
  or provider payload):
  ``test_rendered_run_context_is_redacted_and_leaks_nothing``.
"""
from __future__ import annotations

import dataclasses
import json

import pytest
from fastapi.testclient import TestClient

from workbench.api import create_app
from workbench.config import Settings
from workbench.graph import NullGraph
from workbench.models import (
    RunContext,
    UntrustedEvidence,
    UntrustedTask,
    UntrustedTaskRef,
)
from workbench.run_context_store import (
    MemoryRunContextStore,
    RunContextImmutableError,
    dispatch_with_run_context,
)
from workbench.store import MemoryStore

from _support import build_run_context, compile_delivery_snapshot, load_example

ACTOR = {"X-Workbench-Actor": "operator"}
PROJECT = "project_delivery"


def _client(store: MemoryRunContextStore) -> TestClient:
    settings = Settings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator"}), bridge_bootstrap_token="",
        anvil_router_base_url="", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=True,
    )
    return TestClient(create_app(
        settings=settings, store=MemoryStore(), graph=NullGraph(), run_context_store=store,
    ))


def _refreshed_snapshot():
    """Compile a snapshot from a mutated (refreshed) anvil-state catalog.

    The refreshed world has different workflow/catalog/operation digests than
    the queue-time snapshot, so a run context built from it is genuinely
    distinct -- the realistic "someone refreshed the catalog after queueing".
    """
    from workbench.capability_profiles import validate_project_profile
    from workbench.contracts import contract_digest
    from workbench.provider_catalogs import (
        DEFAULT_PROVIDER_ALLOWLIST,
        PublishedCatalogSet,
        validate_provider_catalog,
    )
    from workbench.workflow_snapshot import compile_workflow_snapshot

    state = load_example("anvil-state.catalog.v1.json")
    claim = next(op for op in state["operations"] if op["id"] == "state.task.claim")
    claim["summary"] += " (refreshed)"
    for op in state["operations"]:
        op["operation_digest"] = contract_digest("operation", op)
    state["catalog_digest"] = contract_digest("catalog", state)

    published = PublishedCatalogSet(
        catalogs=tuple(
            validate_provider_catalog(
                provider, state if provider == "anvil-state" else load_example(f"{provider}.catalog.v1.json"),
            )
            for provider in sorted(DEFAULT_PROVIDER_ALLOWLIST)
        )
    )
    digests = {
        (catalog.provider, op.id, op.contract_version): op.operation_digest
        for catalog in published.catalogs
        for op in catalog.operations
    }
    profile_doc = load_example("project-capability-profile.v1.json")
    for grant in profile_doc["operations"]:
        grant["operation_digest"] = digests[(grant["provider"], grant["id"], grant["contract_version"])]
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
            ref["operation_digest"] = digests[(ref["provider"], ref["id"], ref["contract_version"])]
    selected: list[dict] = []
    seen: set[tuple] = set()
    for step in workflow["steps"]:
        if step["kind"] != "operation":
            continue
        key = tuple(sorted(step["operation"].items()))
        if key not in seen:
            seen.add(key)
            selected.append(dict(step["operation"]))
    return compile_workflow_snapshot(
        workflow, profile, published, selected_operations=selected,
        selected_skills=[{"id": "anvil:execute", "digest": "sha256:" + "7" * 64}],
        route="coding-local",
    )


# --- Criterion 1 ------------------------------------------------------------


def test_queue_persists_before_dispatch_and_reads_back_the_same_snapshot():
    store = MemoryRunContextStore()
    snapshot = compile_delivery_snapshot()
    dispatch_order: list[str] = []

    def build() -> RunContext:
        return build_run_context(snapshot=snapshot)

    def dispatch(run_context: RunContext) -> None:
        # The complete snapshot is already durably persisted at dispatch time.
        stored = store.get(PROJECT, run_context.run_id)
        assert stored.as_dict() == run_context.as_dict()
        dispatch_order.append("dispatch")

    queued = dispatch_with_run_context(store, PROJECT, build, dispatch)
    assert dispatch_order == ["dispatch"]

    # The SAME stored snapshot is readable through the historical API.
    with _client(store) as client_:
        response = client_.get(
            f"/api/projects/{PROJECT}/runs/{queued.run_id}/context", headers=ACTOR,
        )
        assert response.status_code == 200, response.text
        body = response.json()["context"]
    assert body == queued.as_dict()
    # It carries the immutable snapshot's exact authority pins.
    assert body["trusted"]["workflow"]["digest"] == snapshot.workflow_digest
    assert body["trusted"]["workflow"]["capability_profile_digest"] == snapshot.capability_profile_digest
    assert {c["provider"]: c["digest"] for c in body["trusted"]["workflow"]["catalogs"]} == {
        c.provider: c.catalog_digest for c in snapshot.catalogs
    }


# --- Criterion 2 ------------------------------------------------------------


def test_post_queue_renames_and_refreshes_never_rewrite_history():
    store = MemoryRunContextStore()
    queued = build_run_context()
    store.capture(PROJECT, queued)
    with _client(store) as client_:
        baseline = client_.get(
            f"/api/projects/{PROJECT}/runs/{queued.run_id}/context", headers=ACTOR,
        ).json()["context"]

    # A later PRD/task rename AND a catalog/route/skill refresh both produce a
    # distinct context for the same run; the immutable store refuses to rewrite.
    refreshed_snapshot = _refreshed_snapshot()
    assert refreshed_snapshot.workflow_digest != queued.trusted.workflow.workflow_digest
    refreshed = build_run_context(
        snapshot=refreshed_snapshot,
        task=UntrustedTask(
            ref=UntrustedTaskRef(prd_id="release-beta", task_id="T001", prd_revision=42),
            title="Renamed and re-scoped long after queue time",
            acceptance_criteria=("A criterion never reviewed at queue time",),
            work_packet_digest="sha256:" + "f" * 64,
        ),
    )
    assert refreshed.run_id == queued.run_id
    with pytest.raises(RunContextImmutableError):
        store.capture(PROJECT, refreshed)

    # Historical titles, revisions, descriptors, and digests are unchanged.
    with _client(store) as client_:
        after = client_.get(
            f"/api/projects/{PROJECT}/runs/{queued.run_id}/context", headers=ACTOR,
        ).json()["context"]
    assert after == baseline
    assert after["untrusted"]["task"]["title"] == "Add a documented operation contract"
    assert after["untrusted"]["task"]["ref"]["prd_revision"] == 5
    assert after["trusted"]["workflow"]["digest"] == queued.trusted.workflow.workflow_digest
    captured_ops = {c["operation_id"]: c["operation_digest"] for c in after["trusted"]["capabilities"]}
    for operation in queued.trusted.capabilities:
        assert captured_ops[operation.operation_id] == operation.operation_digest


# --- Criterion 3 ------------------------------------------------------------


def test_trusted_and_untrusted_remain_immutable_and_separately_labeled():
    store = MemoryRunContextStore()
    queued = build_run_context()
    store.capture(PROJECT, queued)

    # Frozen at every level.
    with pytest.raises(dataclasses.FrozenInstanceError):
        queued.trusted.workflow.workflow_digest = "sha256:" + "0" * 64  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        queued.untrusted.task.title = "rewritten"  # type: ignore[misc]

    with _client(store) as client_:
        body = client_.get(
            f"/api/projects/{PROJECT}/runs/{queued.run_id}/context", headers=ACTOR,
        ).json()["context"]

    # Two separately labeled structures; authority pins and PRD/task prose never
    # cross the boundary.
    assert set(body) == {"schema_version", "context_id", "trusted", "untrusted"}
    assert body["trusted"]["trust"] == "trusted_execution_policy"
    assert body["untrusted"]["content_trust"] == "untrusted_task_data"
    trusted_blob = json.dumps(body["trusted"])
    untrusted_blob = json.dumps(body["untrusted"])
    assert queued.trusted.workflow.workflow_digest in trusted_blob
    assert queued.trusted.workflow.workflow_digest not in untrusted_blob
    assert "Add a documented operation contract" in untrusted_blob
    assert "Add a documented operation contract" not in trusted_blob

    # The rendered snapshot round-trips through the closed-set reconstruction.
    assert RunContext.from_dict(body).as_dict() == body


# --- Criterion 4 ------------------------------------------------------------


def _walk(value, keys: list[str], strings: list[str]) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            keys.append(key)
            _walk(nested, keys, strings)
    elif isinstance(value, list):
        for nested in value:
            _walk(nested, keys, strings)
    elif isinstance(value, str):
        strings.append(value)


def test_rendered_run_context_is_redacted_and_leaks_nothing():
    store = MemoryRunContextStore()
    secret = "Bearer sk-live-abc123DEADBEEF token=supersecretvalue api_key=leakvalue"
    queued = build_run_context(
        task=UntrustedTask(
            ref=UntrustedTaskRef(prd_id="release-beta", task_id="T001", prd_revision=5),
            title=f"Deliver {secret}",
            acceptance_criteria=(f"Rotate {secret}",),
            work_packet_digest="sha256:" + "8" * 64,
            scope=(f"docs {secret}",),
        ),
        evidence=(UntrustedEvidence(citation=f"cite {secret}", summary=f"note {secret}"),),
    )
    store.capture(PROJECT, queued)
    with _client(store) as client_:
        raw = client_.get(
            f"/api/projects/{PROJECT}/runs/{queued.run_id}/context", headers=ACTOR,
        ).text

    # Seeded credentials are scrubbed on the last hop.
    for leaked in ("sk-live-abc123DEADBEEF", "supersecretvalue", "leakvalue"):
        assert leaked not in raw
    assert "[REDACTED]" in raw

    body = json.loads(raw)
    keys: list[str] = []
    strings: list[str] = []
    _walk(body, keys, strings)

    # No serialized FIELD NAME names a State-storage, credential, or raw
    # execution/provider-payload surface.
    forbidden_key_markers = (
        "state_db", "sqlite", "journal", "wal", "shm", "mount",
        "token", "secret", "api_key", "apikey", "password", "credential", "bearer",
        "adapter", "argv", "command", "input_schema", "output_schema", "execution",
    )
    for key in keys:
        lowered = key.lower()
        for marker in forbidden_key_markers:
            assert marker not in lowered, f"run-context field {key!r} looks like a {marker!r} surface"

    # No serialized VALUE splices a State-internal path or URL.
    for value in strings:
        lowered = value.lower()
        for marker in ("state.db", ".anvil", "-wal", "-shm", "://"):
            assert marker not in lowered, f"run-context value {value!r} leaked {marker!r}"
