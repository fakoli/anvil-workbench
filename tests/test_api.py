from __future__ import annotations

from fastapi.testclient import TestClient

from workbench.api import create_app
from workbench.config import Settings
from workbench.graph import NullGraph
from workbench.store import MemoryStore


def client():
    settings = Settings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator", "reviewer"}), bridge_bootstrap_token="",
        anvil_router_base_url="http://100.87.34.66:8000/v1", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=True,
    )
    return TestClient(create_app(settings=settings, store=MemoryStore(), graph=NullGraph()))


def test_api_releases_only_an_approved_hash_bound_bridge_action():
    with client() as test_client:
        project = test_client.post("/api/projects", json={"name": "demo", "state_root": ".anvil"}).json()
        bridge_response = test_client.post(f"/api/projects/{project['id']}/bridges", json={"name": "demo bridge"}).json()
        bridge, token = bridge_response["bridge"], bridge_response["bootstrap_token"]
        run = test_client.post("/api/runs", json={"project_id": project["id"], "task_id": "task_48", "model": "heavy-local"}).json()
        bridge_headers = {"X-Workbench-Bridge": bridge["id"], "Authorization": f"Bearer {token}"}
        queued_run = test_client.get(f"/api/bridge/{bridge['id']}/commands/next", headers=bridge_headers).json()
        assert queued_run["action_type"] == "run_codex"
        assert queued_run["payload"]["run_id"] == run["id"]
        running = test_client.post(f"/api/bridge/{bridge['id']}/runs/{run['id']}/status", headers=bridge_headers, json={"status": "running"})
        assert running.status_code == 200
        assert running.json()["status"] == "running"
        reconciled = test_client.post(
            f"/api/bridge/{bridge['id']}/runs/{run['id']}/finalize", headers=bridge_headers,
            json={"status": "reconciliation", "command_id": queued_run["id"]},
        )
        assert reconciled.status_code == 200
        assert reconciled.json()["completed_at"] is not None
        assert test_client.get(f"/api/bridge/{bridge['id']}/commands/next", headers=bridge_headers).json() is None

        session_response = test_client.post("/api/sessions", json={
            "project_id": project["id"], "title": "approval delivery", "worktree_id": "checkout-a",
        }).json()
        started = test_client.post(
            f"/api/workflows/{session_response['workflow']['id']}/start",
            json={"task_id": "TASK-APPROVAL", "model": "heavy-local"},
        ).json()
        delivery_run = started["run"]
        queued_delivery = test_client.get(
            f"/api/bridge/{bridge['id']}/commands/next", headers=bridge_headers,
        ).json()
        assert queued_delivery["payload"]["run_id"] == delivery_run["id"]
        assert test_client.post(
            f"/api/bridge/{bridge['id']}/runs/{delivery_run['id']}/status",
            headers=bridge_headers, json={"status": "running"},
        ).status_code == 200
        finalized = test_client.post(
            f"/api/bridge/{bridge['id']}/runs/{delivery_run['id']}/finalize",
            headers=bridge_headers,
            json={"status": "evidenced", "command_id": queued_delivery["id"]},
        )
        assert finalized.status_code == 200
        assert finalized.json()["status"] == "evidenced"
        assert test_client.post(
            f"/api/bridge/{bridge['id']}/runs/{delivery_run['id']}/status",
            headers=bridge_headers, json={"status": "evidenced"},
        ).status_code == 422
        assert test_client.post(
            f"/api/bridge/{bridge['id']}/runs/{delivery_run['id']}/status",
            headers=bridge_headers, json={"status": "completed"},
        ).status_code == 422

        approval = test_client.post("/api/approvals", json={
            "project_id": project["id"], "bridge_id": bridge["id"], "action_type": "commit_pr",
            "payload": {
                "diff_hash": "before", "branch": "codex/demo",
                "run_id": delivery_run["id"], "session_id": session_response["session"]["id"],
                "worktree_id": "checkout-a", "lease_epoch": delivery_run["lease_epoch"],
            },
        }).json()
        denied = test_client.post(f"/api/bridge/{bridge['id']}/approvals/{approval['id']}/consume", headers=bridge_headers, json={"payload_hash": approval["payload_hash"]})
        assert denied.status_code == 409
        assert test_client.post(f"/api/approvals/{approval['id']}/approve", headers={"X-Workbench-Actor": "reviewer"}).status_code == 200
        queued_action = test_client.get(f"/api/bridge/{bridge['id']}/commands/next", headers=bridge_headers).json()
        assert queued_action["approval_id"] == approval["id"]
        changed = test_client.post(f"/api/bridge/{bridge['id']}/approvals/{approval['id']}/consume", headers=bridge_headers, json={"payload_hash": "changed"})
        assert changed.status_code == 409
        direct_delivery_consume = test_client.post(
            f"/api/bridge/{bridge['id']}/approvals/{approval['id']}/consume",
            headers=bridge_headers, json={"payload_hash": approval["payload_hash"]},
        )
        assert direct_delivery_consume.status_code == 409
        consumed = test_client.post(
            f"/api/bridge/{bridge['id']}/approvals/{approval['id']}/consume-for-run",
            headers=bridge_headers, json={"payload_hash": approval["payload_hash"]},
        )
        assert consumed.status_code == 200
        assert consumed.json()["status"] == "consumed"


def test_api_never_exposes_a_bridge_secret_in_bootstrap():
    with client() as test_client:
        response = test_client.get("/api/bootstrap")
    assert response.status_code == 200
    assert "token" not in response.text.lower()


def test_api_starts_a_version_pinned_session_workflow_with_a_fenced_bridge_command():
    with client() as test_client:
        project = test_client.post("/api/projects", json={"name": "demo", "state_root": ".anvil"}).json()
        bridge_response = test_client.post(f"/api/projects/{project['id']}/bridges", json={"name": "demo bridge"}).json()
        bridge, token = bridge_response["bridge"], bridge_response["bootstrap_token"]
        session_response = test_client.post("/api/sessions", json={
            "project_id": project["id"], "title": "first", "worktree_id": "checkout-a",
        })
        assert session_response.status_code == 201
        session, workflow = session_response.json()["session"], session_response.json()["workflow"]

        started = test_client.post(f"/api/workflows/{workflow['id']}/start", json={"task_id": "TASK-101", "model": "ignored-by-pinned-step"})
        assert started.status_code == 201
        run = started.json()["run"]
        assert run["session_id"] == session["id"]
        assert run["workflow_id"] == workflow["id"]
        assert run["workflow_step_id"] == "implement"
        assert run["lease_epoch"] == 1

        bridge_headers = {"X-Workbench-Bridge": bridge["id"], "Authorization": f"Bearer {token}"}
        command = test_client.get(f"/api/bridge/{bridge['id']}/commands/next", headers=bridge_headers).json()
        assert command["payload"]["worktree_id"] == "checkout-a"
        assert command["payload"]["lease_epoch"] == 1
        assert command["payload"]["workflow_id"] == workflow["id"]
        lease = test_client.get(f"/api/bridge/{bridge['id']}/runs/{run['id']}/lease", headers=bridge_headers)
        assert lease.status_code == 200
        assert lease.json()["lease_epoch"] == 1
        renewed = test_client.post(f"/api/bridge/{bridge['id']}/runs/{run['id']}/lease/renew", headers=bridge_headers)
        assert renewed.status_code == 200
        assert renewed.json()["id"] == run["id"]
        assert test_client.post(
            f"/api/bridge/{bridge['id']}/runs/{run['id']}/status",
            headers=bridge_headers, json={"status": "running"},
        ).status_code == 200
        completed = test_client.post(
            f"/api/bridge/{bridge['id']}/runs/{run['id']}/finalize",
            headers=bridge_headers,
            json={"status": "evidenced", "command_id": command["id"]},
        )
        assert completed.status_code == 200
        assert completed.json()["status"] == "evidenced"
        assert test_client.app.state.store.get_workflow(workflow["id"]).status == "waiting_approval"
        events = test_client.get(f"/api/sessions/{session['id']}/events").json()["events"]
        assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))


def test_bridge_cannot_write_events_or_evidence_for_another_project_run():
    with client() as test_client:
        first = test_client.post("/api/projects", json={"name": "first", "state_root": ".anvil"}).json()
        second = test_client.post("/api/projects", json={"name": "second", "state_root": ".anvil"}).json()
        first_bridge = test_client.post(f"/api/projects/{first['id']}/bridges", json={"name": "first bridge"}).json()
        second_bridge = test_client.post(f"/api/projects/{second['id']}/bridges", json={"name": "second bridge"}).json()
        run = test_client.post("/api/runs", json={"project_id": first["id"], "task_id": "TASK-1", "model": "planning"}).json()
        bridge, token = second_bridge["bridge"], second_bridge["bootstrap_token"]
        headers = {"X-Workbench-Bridge": bridge["id"], "Authorization": f"Bearer {token}"}

        event = test_client.post(f"/api/bridge/{bridge['id']}/events", headers=headers, json={
            "run_id": run["id"], "role": "bridge", "content": {"attempt": "cross-project"},
        })
        assert event.status_code == 409
        evidence = test_client.post(f"/api/bridge/{bridge['id']}/evidence", headers=headers, json={
            "source_kind": "failure", "source_id": "wrong-project", "project_id": first["id"], "payload": {"task_id": "TASK-1"},
        })
        assert evidence.status_code == 403
        assert first_bridge["bridge"]["id"] != bridge["id"]


def test_bridge_published_skills_are_digest_bound_to_the_next_work_packet():
    with client() as test_client:
        project = test_client.post("/api/projects", json={"name": "skills", "state_root": ".anvil"}).json()
        registered = test_client.post(f"/api/projects/{project['id']}/bridges", json={"name": "skills bridge"}).json()
        bridge, token = registered["bridge"], registered["bootstrap_token"]
        headers = {"X-Workbench-Bridge": bridge["id"], "Authorization": f"Bearer {token}"}
        digest = "a" * 64
        published = test_client.post(f"/api/bridge/{bridge['id']}/skills", headers=headers, json={"skills": [{
            "skill_id": "anvil:review", "description": "Review state evidence.", "content_sha256": digest,
        }]})
        assert published.status_code == 202
        session = test_client.post("/api/sessions", json={
            "project_id": project["id"], "title": "skill test", "worktree_id": "default", "skills": ["anvil:review"],
        }).json()
        directive = test_client.post(
            f"/api/sessions/{session['session']['id']}/directives", json={"content": "Run the independent evidence check."},
        )
        assert directive.status_code == 202
        started = test_client.post(f"/api/workflows/{session['workflow']['id']}/start", json={"task_id": "TASK-9"})
        assert started.status_code == 201
        command = test_client.get(f"/api/bridge/{bridge['id']}/commands/next", headers=headers).json()
        assert command["action_type"] == "run_codex"
        assert command["payload"]["skills"] == [{
            "skill_id": "anvil:review", "description": "Review state evidence.", "content_sha256": digest,
        }]
        assert command["payload"]["directives"] == ["Run the independent evidence check."]


def test_workflow_rejects_unpublished_skills_before_creating_a_run_or_starting_the_workflow():
    with client() as test_client:
        project = test_client.post("/api/projects", json={"name": "missing skill", "state_root": ".anvil"}).json()
        test_client.post(f"/api/projects/{project['id']}/bridges", json={"name": "bridge"})
        session = test_client.post("/api/sessions", json={
            "project_id": project["id"], "title": "needs a skill", "worktree_id": "default", "skills": ["anvil:review"],
        }).json()

        start = test_client.post(f"/api/workflows/{session['workflow']['id']}/start", json={"task_id": "TASK-10"})

        assert start.status_code == 409
        assert "publish every selected workflow skill" in start.json()["detail"]
        store = test_client.app.state.store
        assert store.get_workflow(session["workflow"]["id"]).status == "draft"
        assert store.list_runs(project["id"]) == []


def test_skills_probe_and_router_only_hub_actions_are_explicit(monkeypatch):
    settings = Settings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator"}), bridge_bootstrap_token="",
        anvil_router_base_url="http://100.87.34.66:8000/v1", anvil_router_token="server-held",
        sandbox_models=frozenset({"fast-local"}), identity_header="X-Workbench-Actor", allow_insecure_dev_actor=True,
    )
    from workbench import api as api_module

    monkeypatch.setattr(api_module, "route_decisions", lambda *_args: [
        {"workbench_run_id": "known", "request_id": "req-1", "model": "fast-local"},
        {"workbench_run_id": "not-workbench", "request_id": "req-2"},
    ])
    monkeypatch.setattr(api_module, "sandbox_response", lambda *_args: {"model": "fast-local", "status": "completed", "output_text": "safe"})
    with TestClient(create_app(settings=settings, store=MemoryStore(), graph=NullGraph())) as test_client:
        project = test_client.post("/api/projects", json={"name": "router", "state_root": ".anvil"}).json()
        bridge_response = test_client.post(f"/api/projects/{project['id']}/bridges", json={"name": "bridge"}).json()
        bridge, token = bridge_response["bridge"], bridge_response["bootstrap_token"]
        headers = {"X-Workbench-Bridge": bridge["id"], "Authorization": f"Bearer {token}"}
        test_client.post(f"/api/bridge/{bridge['id']}/skills", headers=headers, json={"skills": [{
            "skill_id": "anvil:review", "description": "Review.", "content_sha256": "b" * 64,
        }]})
        probe = test_client.post(f"/api/projects/{project['id']}/skills/probe")
        assert probe.status_code == 202
        queued = test_client.get(f"/api/bridge/{bridge['id']}/commands/next", headers=headers).json()
        assert queued["action_type"] == "skill_probe"

        run = test_client.post("/api/runs", json={"project_id": project["id"], "task_id": "TASK", "model": "fast-local"}).json()
        store = test_client.app.state.store
        store.runs["known"] = type(store.runs[run["id"]])("known", project["id"], "TASK", "fast-local", "queued")
        assert test_client.get("/api/routes").json()["routes"] == [{"workbench_run_id": "known", "request_id": "req-1", "model": "fast-local"}]
        assert test_client.post("/api/sandbox", json={"model": "fast-local", "input": "hello"}).json()["output_text"] == "safe"
        assert test_client.post("/api/sandbox", json={"model": "heavy-local", "input": "hello"}).status_code == 409


# ---------------------------------------------------------------------------
# Read-only project-context browser projection (state-context-operations
# T003.3 / T003.4): the hub exposes the explicitly non-canonical display
# read-model, project-scoped and fail-closed.
# ---------------------------------------------------------------------------

import json as _json
from pathlib import Path as _Path

from workbench.project_context import ProjectContextProjection
from workbench.project_context_store import MemoryProjectContextStore
from workbench.state_manifest import pin_state_read_operations
from workbench.state_snapshot_adapter import validate_snapshot_payload

_ROOT = _Path(__file__).resolve().parents[1]
_EXAMPLE_CATALOG = _ROOT / "docs" / "contracts" / "examples" / "anvil-state.catalog.v1.json"
_EXAMPLE_SNAPSHOT = _ROOT / "docs" / "contracts" / "examples" / "anvil-state.project-snapshot.v1.json"

CTX_ACTOR = {"X-Workbench-Actor": "operator"}


def _snapshot_projection() -> ProjectContextProjection:
    """A realistic projection derived from the checked-in snapshot fixture."""
    catalog = _json.loads(_EXAMPLE_CATALOG.read_text(encoding="utf-8"))
    operation = pin_state_read_operations(catalog).project_snapshot
    payload = _json.loads(_EXAMPLE_SNAPSHOT.read_text(encoding="utf-8"))
    snapshot = validate_snapshot_payload(payload, operation)
    return ProjectContextProjection.from_snapshot(snapshot)


def context_client(store: MemoryProjectContextStore | None) -> TestClient:
    settings = Settings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator", "reviewer"}), bridge_bootstrap_token="",
        anvil_router_base_url="", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=True,
    )
    return TestClient(create_app(
        settings=settings, store=MemoryStore(), graph=NullGraph(), project_context_store=store,
    ))


def test_project_context_response_is_readable_scoped_and_non_canonical():
    # T003.3 criterion 1: readable hierarchy, scoped identifiers, source
    # revision/digest, and an explicit non-canonical label.
    store = MemoryProjectContextStore()
    projection = _snapshot_projection()
    store.publish(projection.project_id, projection)
    with context_client(store) as client_:
        response = client_.get(f"/api/projects/{projection.project_id}/context", headers=CTX_ACTOR)
        assert response.status_code == 200, response.text
        context = response.json()["context"]

        # Explicit non-canonical labeling at the projection and summary level.
        assert context["canonical"] is False and context["non_canonical"] is True
        assert context["project_id"] == projection.project_id
        assert context["source_digest"] == projection.source_digest

        # Readable hierarchy with scoped identifiers and per-summary attribution.
        prd_ids = {p["scoped_id"] for p in context["prds"]}
        task_ids = {t["scoped_id"] for t in context["tasks"]}
        feature_ids = {f["scoped_id"] for f in context["features"]}
        assert prd_ids == {"release-alpha", "release-beta"}
        assert task_ids == {"release-alpha:T001", "release-beta:T001", "release-beta:T002.2"}
        assert feature_ids == {"release-alpha:F001", "release-beta:F001", "release-beta:F002"}
        for summary in context["prds"] + context["features"] + context["tasks"]:
            assert summary["non_canonical"] is True
            assert summary["source_digest"] == projection.source_digest
            assert summary["source_revision"] >= 1

        # The by-digest detail endpoint returns the same projection.
        detail = client_.get(
            f"/api/projects/{projection.project_id}/context/{projection.source_digest}",
            headers=CTX_ACTOR,
        )
        assert detail.status_code == 200
        assert detail.json()["context"] == context


def test_project_context_duplicate_task_identities_are_preserved():
    # T003.4 criterion 3: same-numbered tasks in different PRDs stay distinct.
    store = MemoryProjectContextStore()
    projection = _snapshot_projection()
    store.publish(projection.project_id, projection)
    with context_client(store) as client_:
        context = client_.get(
            f"/api/projects/{projection.project_id}/context", headers=CTX_ACTOR,
        ).json()["context"]
        task_ids = [t["scoped_id"] for t in context["tasks"]]
        assert "release-alpha:T001" in task_ids and "release-beta:T001" in task_ids
        assert len(task_ids) == len(set(task_ids))


def test_cross_project_context_read_is_indistinct_from_missing():
    # T003.3 criterion 2 / T003.4 criterion 2: project A cannot read project B's
    # context through the latest or detail endpoint; a foreign digest resolves
    # to the byte-identical 404 of a truly missing record -- no existence oracle.
    store = MemoryProjectContextStore()
    projection = _snapshot_projection()  # owned by "project_example"
    store.publish(projection.project_id, projection)
    with context_client(store) as client_:
        # A different project's latest is simply missing.
        missing_latest = client_.get("/api/projects/other-project/context", headers=CTX_ACTOR)
        assert missing_latest.status_code == 404

        # Reading project_example's real digest under another project's scope is
        # byte-identical to reading a digest that was never published. Compare the
        # raw response bytes (not parsed JSON) so the assertion proves the
        # "byte-identical" claim it makes.
        foreign = client_.get(
            f"/api/projects/other-project/context/{projection.source_digest}", headers=CTX_ACTOR,
        )
        never = client_.get(
            "/api/projects/other-project/context/sha256:" + "0" * 64, headers=CTX_ACTOR,
        )
        assert foreign.status_code == never.status_code == 404
        assert foreign.content == never.content
        # And the same bytes a genuinely-missing latest returns.
        assert missing_latest.status_code == 404
        assert missing_latest.content == foreign.content


def test_project_context_response_carries_no_prohibited_fields():
    # T003.3 criterion 3 / T003.4 criterion 3: no State path, credential field,
    # token, or raw provider payload is representable in the response.
    store = MemoryProjectContextStore()
    projection = _snapshot_projection()
    store.publish(projection.project_id, projection)
    with context_client(store) as client_:
        raw = client_.get(
            f"/api/projects/{projection.project_id}/context", headers=CTX_ACTOR,
        ).text
        lowered = raw.lower()
        for marker in ("state.db", ".anvil", "-wal", "-shm", "://", "sqlite", "api_key", "bearer", "argv"):
            assert marker not in lowered, f"response leaked marker {marker!r}"


def test_unconfigured_project_context_store_fails_closed():
    # Fail-closed when the projection is not configured (it is deliberately not
    # wired into the live poll loop): every endpoint refuses with 503.
    with context_client(None) as client_:
        assert client_.get("/api/projects/project_example/context", headers=CTX_ACTOR).status_code == 503
        assert client_.get(
            "/api/projects/project_example/context/sha256:" + "a" * 64, headers=CTX_ACTOR,
        ).status_code == 503


def test_project_context_read_requires_a_trusted_allowlisted_actor():
    # The read surface is behind the same trusted actor dependency as the rest
    # of the hub: a non-allowlisted identity is refused (403), never served.
    store = MemoryProjectContextStore()
    projection = _snapshot_projection()
    store.publish(projection.project_id, projection)
    settings = Settings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator"}), bridge_bootstrap_token="",
        anvil_router_base_url="", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=False,
    )
    with TestClient(create_app(
        settings=settings, store=MemoryStore(), graph=NullGraph(), project_context_store=store,
    )) as client_:
        # No identity header at all -> 401.
        assert client_.get(f"/api/projects/{projection.project_id}/context").status_code == 401
        # A present but non-allowlisted identity -> 403.
        assert client_.get(
            f"/api/projects/{projection.project_id}/context",
            headers={"X-Workbench-Actor": "intruder"},
        ).status_code == 403


def test_malformed_project_scope_or_digest_is_rejected_before_the_store():
    store = MemoryProjectContextStore()
    projection = _snapshot_projection()
    store.publish(projection.project_id, projection)
    with context_client(store) as client_:
        # A malformed digest is rejected by the path pattern (422), never
        # reaching the store as a distinguishable error.
        assert client_.get(
            f"/api/projects/{projection.project_id}/context/not-a-digest", headers=CTX_ACTOR,
        ).status_code == 422


# ---------------------------------------------------------------------------
# Historical run-context read surface (state-context-operations:T005.3)
# ---------------------------------------------------------------------------

import copy as _copy

from workbench.capability_profiles import validate_project_profile
from workbench.models import (
    RunConstraints,
    RunContext,
    RunCursor,
    RunIdentity,
    RunReceipt,
    UntrustedEvidence,
    UntrustedTask,
    UntrustedTaskRef,
    RunWorkflowPin,
    run_capabilities_from_snapshot,
    run_skills_from_snapshot,
)
from workbench.provider_catalogs import (
    DEFAULT_PROVIDER_ALLOWLIST,
    PublishedCatalogSet,
    validate_provider_catalog,
)
from workbench.run_context_store import MemoryRunContextStore
from workbench.workflow_snapshot import compile_workflow_snapshot

_EXAMPLES_DIR = _ROOT / "docs" / "contracts" / "examples"


def _rc_example(name: str) -> dict:
    return _json.loads((_EXAMPLES_DIR / name).read_text(encoding="utf-8"))


def _rc_snapshot():
    published = PublishedCatalogSet(
        catalogs=tuple(
            validate_provider_catalog(provider, _rc_example(f"{provider}.catalog.v1.json"))
            for provider in sorted(DEFAULT_PROVIDER_ALLOWLIST)
        )
    )
    profile = validate_project_profile(
        _rc_example("project-capability-profile.v1.json"), published,
        configured_model_profiles=("coding-local", "planning-local"),
        configured_skills={"anvil:execute": "sha256:" + "7" * 64},
        approval_actions=("commit_pr", "merge_and_accept"),
    )
    workflow = _rc_example("delivery.workflow.v2.json")
    selected: list[dict] = []
    seen: set[tuple] = set()
    for step in workflow["steps"]:
        if step["kind"] != "operation":
            continue
        key = tuple(sorted(step["operation"].items()))
        if key not in seen:
            seen.add(key)
            selected.append(_copy.deepcopy(step["operation"]))
    return compile_workflow_snapshot(
        workflow, profile, published, selected_operations=selected,
        selected_skills=[{"id": "anvil:execute", "digest": "sha256:" + "7" * 64}],
        route="coding-local",
    )


def _run_context(**task_overrides) -> RunContext:
    snapshot = _rc_snapshot()
    task = task_overrides.pop("task", None) or UntrustedTask(
        ref=UntrustedTaskRef(prd_id="release-beta", task_id="T001", prd_revision=5),
        title="Add a documented operation contract",
        acceptance_criteria=("Add a versioned resource", "Validate its JSON shape"),
        work_packet_digest="sha256:" + "8" * 64,
        scope=("docs/contracts",),
    )
    return RunContext.capture(
        context_id="ctx_run_history_0001",
        identity=RunIdentity(
            run_id="run_history_1", session_id="sess_1", bridge_id="bridge_1",
            worktree_name="checkout-a", task_id="release-beta:T001",
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
        task=task,
        evidence=(UntrustedEvidence(citation="state-event:claim", summary="Task claim is active."),),
    )


def run_context_client(store: MemoryRunContextStore | None) -> TestClient:
    settings = Settings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator", "reviewer"}), bridge_bootstrap_token="",
        anvil_router_base_url="", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=True,
    )
    return TestClient(create_app(
        settings=settings, store=MemoryStore(), graph=NullGraph(), run_context_store=store,
    ))


def test_run_context_history_round_trips_trusted_and_untrusted():
    # T005.3 criterion 1 + 3: the API returns the stored snapshot with trusted
    # policy and untrusted PRD/task data in two separately labeled structures.
    store = MemoryRunContextStore()
    context = _run_context()
    store.capture("project_a", context)
    with run_context_client(store) as client_:
        response = client_.get(
            "/api/projects/project_a/runs/run_history_1/context", headers=CTX_ACTOR,
        )
        assert response.status_code == 200, response.text
        body = response.json()["context"]
        assert body == context.as_dict()
        assert body["trusted"]["trust"] == "trusted_execution_policy"
        assert body["untrusted"]["content_trust"] == "untrusted_task_data"


def test_run_context_history_reads_only_the_stored_snapshot_immune_to_renames():
    # T005.3 criterion 2: a later task/PRD rename does not change the titles or
    # revisions the historical read returns -- it reads only the stored snapshot.
    store = MemoryRunContextStore()
    context = _run_context()
    store.capture("project_a", context)

    # A later rename produces a DIFFERENT context for the same run; the store
    # refuses to rewrite it (immutability), so the read is unaffected.
    from workbench.run_context_store import RunContextImmutableError

    renamed = _run_context(
        task=UntrustedTask(
            ref=UntrustedTaskRef(prd_id="release-beta", task_id="T001", prd_revision=9),
            title="Renamed long after queue time",
            acceptance_criteria=("Totally different criterion",),
            work_packet_digest="sha256:" + "c" * 64,
        ),
    )
    import pytest

    with pytest.raises(RunContextImmutableError):
        store.capture("project_a", renamed)

    with run_context_client(store) as client_:
        body = client_.get(
            "/api/projects/project_a/runs/run_history_1/context", headers=CTX_ACTOR,
        ).json()["context"]
    assert body["untrusted"]["task"]["title"] == "Add a documented operation contract"
    assert body["untrusted"]["task"]["ref"]["prd_revision"] == 5


def test_cross_project_run_context_read_is_indistinct_from_missing():
    # T005.3 criterion 1: a run owned by another project is byte-identical to a
    # genuinely missing run -- no existence oracle across the project boundary.
    store = MemoryRunContextStore()
    store.capture("project_b", _run_context())
    with run_context_client(store) as client_:
        foreign = client_.get(
            "/api/projects/project_a/runs/run_history_1/context", headers=CTX_ACTOR,
        )
        never = client_.get(
            "/api/projects/project_a/runs/run_absent/context", headers=CTX_ACTOR,
        )
        missing_owner = client_.get(
            "/api/projects/project_b/runs/run_absent/context", headers=CTX_ACTOR,
        )
        assert foreign.status_code == never.status_code == missing_owner.status_code == 404
        # Byte-identical bodies (raw content, not parsed JSON).
        assert foreign.content == never.content == missing_owner.content


def test_run_context_history_carries_no_secret_path_command_or_payload():
    # T005.3 criterion 3: seed the untrusted prose with credentials; the stored
    # + rendered snapshot scrubs them and the closed field set exposes no State
    # path, credential field, raw command, or provider payload.
    store = MemoryRunContextStore()
    seeded = _run_context(
        task=UntrustedTask(
            ref=UntrustedTaskRef(prd_id="release-beta", task_id="T001", prd_revision=5),
            title="Fix token=supersecretvalue and Bearer sk-live-abc123DEADBEEF",
            acceptance_criteria=("Rotate api_key=leakvalue",),
            work_packet_digest="sha256:" + "8" * 64,
        ),
    )
    store.capture("project_a", seeded)
    with run_context_client(store) as client_:
        raw = client_.get(
            "/api/projects/project_a/runs/run_history_1/context", headers=CTX_ACTOR,
        ).text

    for leaked in ("supersecretvalue", "sk-live-abc123DEADBEEF", "leakvalue"):
        assert leaked not in raw
    assert "[REDACTED]" in raw
    lowered = raw.lower()
    for marker in ("state.db", ".anvil", "-wal", "-shm", "://", "sqlite"):
        assert marker not in lowered, f"run-context response leaked marker {marker!r}"

    # No serialized FIELD NAME names a State-storage, credential, or raw
    # execution surface (derive the key set from the actual response).
    body = _json.loads(raw)

    def _keys(value, acc):
        if isinstance(value, dict):
            for key, nested in value.items():
                acc.append(key)
                _keys(nested, acc)
        elif isinstance(value, list):
            for nested in value:
                _keys(nested, acc)

    keys: list[str] = []
    _keys(body, keys)
    forbidden = (
        "state_db", "sqlite", "journal", "wal", "shm", "path", "mount",
        "token", "secret", "api_key", "apikey", "password", "credential", "bearer",
        "adapter", "argv", "command", "endpoint", "input_schema", "output_schema",
    )
    for key in keys:
        lowered_key = key.lower()
        for marker in forbidden:
            assert marker not in lowered_key, f"run-context field {key!r} looks like a {marker!r} surface"


def test_unconfigured_run_context_store_fails_closed():
    with run_context_client(None) as client_:
        assert client_.get(
            "/api/projects/project_a/runs/run_history_1/context", headers=CTX_ACTOR,
        ).status_code == 503


def test_run_context_history_requires_a_trusted_allowlisted_actor():
    store = MemoryRunContextStore()
    store.capture("project_a", _run_context())
    settings = Settings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator"}), bridge_bootstrap_token="",
        anvil_router_base_url="", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=False,
    )
    with TestClient(create_app(
        settings=settings, store=MemoryStore(), graph=NullGraph(), run_context_store=store,
    )) as client_:
        assert client_.get("/api/projects/project_a/runs/run_history_1/context").status_code == 401
        assert client_.get(
            "/api/projects/project_a/runs/run_history_1/context",
            headers={"X-Workbench-Actor": "intruder"},
        ).status_code == 403


def test_malformed_run_id_is_rejected_before_the_store():
    store = MemoryRunContextStore()
    store.capture("project_a", _run_context())
    with run_context_client(store) as client_:
        # A run id containing a path separator is rejected by the path pattern
        # (422 or 404 for a non-matching route), never reaching the store as a
        # distinguishable error. A space is not in the grammar -> 422.
        assert client_.get(
            "/api/projects/project_a/runs/has%20space/context", headers=CTX_ACTOR,
        ).status_code == 422
