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

# Read-only system-health + observational posture surface (preferences-
# configuration T003.2 / T008): every declared integration's descriptor,
# truthful disabled/degraded states, a closed leak-proof response, GET-only (no
# mutation/execution/approval), and CLI/API finding parity.
# ---------------------------------------------------------------------------

from datetime import datetime as _datetime, timezone as _timezone

from _support import SYSTEM_HEALTH_DESCRIPTOR_FIELDS

from workbench.cli import main as _cli_main
from workbench.system_health import (
    INTEGRATION_IDS as _SH_IDS,
    IntegrationDescriptor as _SHDescriptor,
    PostureCheck as _SHPostureCheck,
    PostureReport as _SHPostureReport,
    SystemHealthService as _SHService,
    run_posture_audit as _sh_run_audit,
)

SYS_ACTOR = {"X-Workbench-Actor": "operator"}

#: The only fields a descriptor response object may carry. A field added outside
#: this set must fail the response test (leak-by-addition), so it is not a
#: tautology. Imported from ``conftest`` so this list and its twin in
#: ``test_security_contract.py`` are one source of truth and cannot drift.
_SYS_ALLOWED_FIELDS = SYSTEM_HEALTH_DESCRIPTOR_FIELDS
_FIXED_CLOCK = lambda: _datetime(2026, 7, 21, tzinfo=_timezone.utc)


def _sys_settings(**overrides) -> Settings:
    base = dict(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator", "reviewer"}), bridge_bootstrap_token="",
        anvil_router_base_url="", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=True,
    )
    base.update(overrides)
    return Settings(**base)


def _sys_client(settings: Settings, *, bridge_health=None, service=None) -> TestClient:
    service = service or _SHService(settings, clock=_FIXED_CLOCK, bridge_health=bridge_health)
    return TestClient(create_app(
        settings=settings, store=MemoryStore(), graph=NullGraph(), system_health=service,
    ))


def test_system_health_returns_a_descriptor_for_every_declared_integration():
    # T003.2 criterion 1: the endpoint returns descriptors for every declared
    # integration, each with a closed field set and an explicit non-canonical mark.
    with _sys_client(_sys_settings()) as client_:
        response = client_.get("/api/system/health", headers=SYS_ACTOR)
        assert response.status_code == 200, response.text
        integrations = response.json()["integrations"]
        assert {i["integration_id"] for i in integrations} == set(_SH_IDS)
        for descriptor in integrations:
            assert set(descriptor) - _SYS_ALLOWED_FIELDS == set(), descriptor
            assert descriptor["non_canonical"] is True
            assert descriptor["digest"].startswith("sha256:")
            assert descriptor["last_checked_at"] == "2026-07-21T00:00:00Z"


def test_system_health_reports_unavailable_integrations_as_disabled_or_degraded():
    # T003.2 criterion 2: unavailable integrations return disabled or degraded
    # states with remediation and no raw internals. Serving/graph are unset
    # (disabled); the bridge observation is degraded (passed through, criterion 4).
    with _sys_client(_sys_settings(), bridge_health="degraded") as client_:
        integrations = {
            i["integration_id"]: i
            for i in client_.get("/api/system/health", headers=SYS_ACTOR).json()["integrations"]
        }
        assert integrations["anvil_serving"]["state"] == "disabled"
        assert integrations["anvil_serving"]["configured"] is False
        assert integrations["anvil_serving"]["remediation"]
        # Bridge health passes through the SAME descriptor + redaction contract.
        assert integrations["project_bridge"]["state"] == "degraded"
        assert integrations["project_bridge"]["configured"] is True


def test_system_health_reports_configured_integrations_as_ready():
    # A configured plane is reported truthfully as ready (never a false disabled).
    settings = _sys_settings(anvil_router_base_url="http://serving", anvil_router_token="t")
    with _sys_client(settings) as client_:
        integrations = {
            i["integration_id"]: i
            for i in client_.get("/api/system/health", headers=SYS_ACTOR).json()["integrations"]
        }
        assert integrations["anvil_serving"]["state"] == "ready"
        assert integrations["anvil_serving"]["configured"] is True


def test_system_health_response_carries_no_credential_url_or_path_marker():
    # T003.2 criterion 2 ("no raw internals"): even with secret-shaped config
    # VALUES, the rendered response leaks no credential, endpoint URL, or path.
    settings = _sys_settings(
        anvil_router_base_url="https://100.87.34.66:8000/v1",
        anvil_router_token="sk-live-supersecretDEADBEEF",
        neo4j_password="/var/secrets/neo4j",
    )
    with _sys_client(settings) as client_:
        raw = client_.get("/api/system/health", headers=SYS_ACTOR).text.lower()
        for marker in ("supersecret", "deadbeef", "100.87.34.66", "://", "/var/secrets", "sk-live"):
            assert marker not in raw, f"system-health response leaked {marker!r}"


_BS = chr(92)

#: The full adversarial redaction corpus (finding 1), mirrored at the API last
#: hop: ``(fragment, [tokens that must be gone])``. Each proven-leak shape spans
#: the response body as a negative assertion.
_SYS_REDACTION_CORPUS = (
    ("AKIAIOSFODNN7EXAMPLE", ["AKIAIOSFODNN7EXAMPLE"]),
    ("aws_secret_access_key=wJalrXUtnFEMIK7bPxRfiCYEXAMPLEKEY", ["wJalrXUtnFEMI"]),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dozjgNryP4", ["eyJhbGci", "eyJzdWIi"]),
    ("-----BEGIN RSA PRIVATE KEY-----MIIEpQ-----END RSA PRIVATE KEY-----", ["MIIEpQ"]),
    ("100.64.0.5:8443", ["100.64.0.5"]),
    ("db.tail1234.ts.net:7687", ["tail1234", "ts.net"]),
    ("serving.tail1234.ts.net", ["serving.tail1234"]),
    ("//internalhost/admin", ["//internalhost"]),
    ("Server=db.internal;Password=hunter2;", ["db.internal", "hunter2"]),
    ("path=/etc/anvil/secret.conf", ["/etc/anvil"]),
    ("file:/var/lib/secrets/key", ["/var/lib/secrets"]),
    (_BS + _BS + "fileserver" + _BS + "secrets", ["fileserver"]),
    ("~/.ssh/id_rsa", ["id_rsa"]),
    ("deploy/.env", ["deploy/.env"]),
    ("certs/server.pem", ["certs/server.pem"]),
    ("prod.env", ["prod.env"]),
)


def _seeded_service(remediation: str, *, use_descriptor: bool = True):
    """A system-health service seeded with adversarial prose.

    With ``use_descriptor`` it returns a real (construction-scrubbed)
    ``IntegrationDescriptor``; with it False it returns a ROGUE, duck-typed
    object whose ``as_dict()`` emits raw, unscrubbed prose -- proving the API
    last hop is the guarantee, not descriptor construction (finding 2, option b).
    """
    class _RogueDescriptor:
        def as_dict(self):
            return {
                "integration_id": "anvil_serving", "state": "disabled",
                "configured": False, "owner": "anvil-serving",
                "remediation": remediation, "title": "Anvil Serving model plane",
                "dependencies": [], "non_canonical": True,
                "schema_version": "workbench-system-health/v1",
                "digest": "sha256:" + "a" * 64,
                "last_checked_at": "2026-07-21T00:00:00Z",
            }

    real = _SHDescriptor(
        integration_id="anvil_serving", title="Anvil Serving model plane",
        state="disabled", configured=False, owner="anvil-serving",
        remediation=remediation, last_checked_at="2026-07-21T00:00:00Z",
    )
    descriptor = real if use_descriptor else _RogueDescriptor()

    class _SeededService:
        def descriptors(self):
            return (descriptor,)
        def get(self, integration_id):
            return descriptor
        def posture(self):
            return _SHPostureReport(checks=(
                _SHPostureCheck(
                    check_id="posture.integration.anvil_serving", title="x",
                    status="disabled", severity="info", remediation=remediation,
                ),
            ))

    return _SeededService()


def test_system_health_last_hop_scrubs_an_adversarially_seeded_descriptor():
    # Redaction is enforced at the API boundary, so even a service that splices a
    # secret/URL/path into descriptor prose cannot make the API emit it. Every
    # proven-leak shape (finding 1) is checked across /health and /posture.
    for fragment, gone in _SYS_REDACTION_CORPUS:
        remediation = f"remediation {fragment} tail"
        with _sys_client(_sys_settings(), service=_seeded_service(remediation)) as client_:
            health_raw = client_.get("/api/system/health", headers=SYS_ACTOR).text
            detail_raw = client_.get("/api/system/health/anvil_serving", headers=SYS_ACTOR).text
            posture_raw = client_.get("/api/system/posture", headers=SYS_ACTOR).text
            for token in gone:
                for surface, raw in (("health", health_raw), ("detail", detail_raw), ("posture", posture_raw)):
                    assert token not in raw, f"{surface} leaked {token!r} from {fragment!r}"
            assert "remediation" in health_raw and "tail" in health_raw  # prose survives


def test_system_health_last_hop_scrubs_a_rogue_duck_typed_service_that_bypassed_construction():
    # Finding 2 (security lens, option b): the guarantee is the serialized API
    # boundary, not descriptor construction. A rogue, duck-typed service whose
    # as_dict() returns RAW unscrubbed prose (never went through _prose) must
    # still be scrubbed by the router's last-hop scrub before it reaches the
    # browser -- otherwise secrets ride straight through.
    remediation = (
        "token=leakedsecret at https://10.0.0.9/admin path /root/.ssh/id_rsa "
        "and AKIAIOSFODNN7EXAMPLE"
    )
    service = _seeded_service(remediation, use_descriptor=False)
    # Sanity: the rogue as_dict() really does emit the raw secret (so the test is
    # not vacuous -- construction-time scrubbing did NOT run here).
    assert "leakedsecret" in _json.dumps(service.get("anvil_serving").as_dict())
    with _sys_client(_sys_settings(), service=service) as client_:
        for path in ("/api/system/health", "/api/system/health/anvil_serving", "/api/system/posture"):
            raw = client_.get(path, headers=SYS_ACTOR).text
            for token in ("leakedsecret", "10.0.0.9", "/root/.ssh", "AKIAIOSFODNN7EXAMPLE", "://"):
                assert token not in raw, f"{path} leaked {token!r} from a rogue service"
            assert "[REDACTED" in raw  # a class marker proves the scrub ran


def test_system_health_surface_is_get_only_with_no_mutation_execution_or_approval_route():
    # T003.2 criterion 3 / T008: the surface exposes no mutation, execution, or
    # approval path. Every declared /api/system operation is GET-only (checked in
    # the OpenAPI schema), and every write verb is refused, never served.
    with _sys_client(_sys_settings()) as client_:
        paths = client_.app.openapi()["paths"]
        system_paths = {path: ops for path, ops in paths.items() if path.startswith("/api/system")}
        assert set(system_paths) == {
            "/api/system/health", "/api/system/health/{integration_id}", "/api/system/posture",
        }
        for path, operations in system_paths.items():
            assert set(operations) <= {"get"}, f"{path} declares non-GET operations: {sorted(operations)}"
        # Behavioral proof: a write verb against EVERY declared route -- the
        # collection, the per-integration detail, and the posture audit -- is
        # refused with 405, never served.
        for verb in (client_.post, client_.put, client_.patch, client_.delete):
            assert verb("/api/system/health", headers=SYS_ACTOR).status_code == 405
            assert verb("/api/system/health/anvil_serving", headers=SYS_ACTOR).status_code == 405
            assert verb("/api/system/posture", headers=SYS_ACTOR).status_code == 405


def test_system_health_one_integration_detail_and_unknown_and_malformed_ids():
    with _sys_client(_sys_settings()) as client_:
        # A known integration resolves to its own descriptor.
        one = client_.get("/api/system/health/anvil_serving", headers=SYS_ACTOR)
        assert one.status_code == 200
        assert one.json()["integration"]["integration_id"] == "anvil_serving"
        # An unknown (but well-formed) id is a plain 404 -- the catalog is public.
        unknown = client_.get("/api/system/health/not_a_real_one", headers=SYS_ACTOR)
        assert unknown.status_code == 404
        assert unknown.json()["detail"] == "unknown integration"
        # A malformed id is rejected at the edge (422) before the service.
        assert client_.get("/api/system/health/Bad-ID", headers=SYS_ACTOR).status_code == 422


def test_system_health_requires_a_trusted_allowlisted_actor():
    # Behind the same trusted actor dependency as the rest of the hub.
    settings = _sys_settings(approvers=frozenset({"operator"}), allow_insecure_dev_actor=False)
    with _sys_client(settings) as client_:
        for path in ("/api/system/health", "/api/system/health/anvil_serving", "/api/system/posture"):
            assert client_.get(path).status_code == 401  # no identity header
            assert client_.get(path, headers={"X-Workbench-Actor": "intruder"}).status_code == 403


def test_system_posture_endpoint_returns_deterministic_stable_id_findings():
    # T008: the posture endpoint returns stable, deterministic findings.
    with _sys_client(_sys_settings(allow_insecure_dev_actor=True)) as client_:
        body = client_.get("/api/system/posture", headers=SYS_ACTOR).json()
        ids = [c["check_id"] for c in body["checks"]]
        assert ids == sorted(ids) and len(ids) == len(set(ids))
        assert "posture.security.insecure_dev_actor" in ids
        # Re-fetching yields identical findings (timestamp is not part of them).
        again = client_.get("/api/system/posture", headers=SYS_ACTOR).json()
        assert again["checks"] == body["checks"]


def test_cli_and_system_health_api_render_identical_posture_findings(monkeypatch, capsys):
    # T008 criterion 3: CLI and System Health render identical findings for the
    # same configuration, because both call the one run_posture_audit runner.
    monkeypatch.setenv("WORKBENCH_ALLOW_INSECURE_DEV_ACTOR", "1")
    monkeypatch.setenv("ANVIL_ROUTER_BASE_URL", "http://serving")
    monkeypatch.setenv("ANVIL_ROUTER_TOKEN", "server-held")
    monkeypatch.setenv("WORKBENCH_IDENTITY_HEADER", "X-Workbench-Actor")
    settings = Settings.from_env()

    # CLI surface: emit JSON findings and parse them.
    assert _cli_main(["posture", "--json"]) == 0
    cli_findings = _json.loads(capsys.readouterr().out)

    # API surface: the same settings drive the mounted service.
    with _sys_client(settings) as client_:
        api_findings = client_.get("/api/system/posture", headers=SYS_ACTOR).json()["checks"]

    assert cli_findings == api_findings
    # And both agree with a direct run of the shared runner -- no surface drift.
    assert cli_findings == _sh_run_audit(settings).findings()


# ---------------------------------------------------------------------------
# Read-only reviewed-plugin discovery + install-receipt browser surface
# (reviewed-tools-plugins T002/T003): the hub exposes the redacted discovery
# projection and stored receipts, credential-reference-only and fail-closed.
# ---------------------------------------------------------------------------

import json as _pl_json
from pathlib import Path as _PlPath

from workbench.contracts import approval_payload_digest as _pl_approval_hash, contract_digest as _pl_digest
from workbench.plugin_host import (
    CredentialBroker as _PlBroker,
    HostInstallOutcome as _PlOutcome,
    PluginDiscovery as _PlDiscovery,
    PluginHostService as _PlService,
)

_PL_ROOT = _PlPath(__file__).resolve().parents[1]
_PL_EXAMPLES = _PL_ROOT / "docs" / "contracts" / "examples"
_PL_ACTOR = {"X-Workbench-Actor": "operator"}
_PL_NOTIFIER_DIGEST = "sha256:5474ca8eb2d41d767772c8a5ba33a1e90f5cb57017c4c8ab6487bd8ee6ba8dbb"


def _pl_load(name: str) -> dict:
    return _pl_json.loads((_PL_EXAMPLES / name).read_text(encoding="utf-8"))


def _pl_service_with_install():
    catalog = _pl_load("plugin.catalog.v1.json")
    capability = _pl_load("plugin.capability.v1.json")
    service = _PlService(_PlDiscovery(catalog, capability))
    # Persist one accepted install receipt so the receipt endpoint has a subject.
    subject = {
        "kind": "install", "plugin_id": "deploy-notifier",
        "plugin_digest": _PL_NOTIFIER_DIGEST, "target_version": "1.0.0",
    }
    request = {
        "schema_version": "workbench-plugin-request/v1",
        "request_id": "plugreq_installnotifier01",
        "request_digest": "sha256:" + "0" * 64,
        "kind": "install",
        "actor": {"actor_id": "operator-01", "kind": "operator"},
        "plugin": {"plugin_id": "deploy-notifier", "plugin_digest": _PL_NOTIFIER_DIGEST},
        "lifecycle": {"target_version": "1.0.0"},
        "approval": {
            "grant_id": "approval_installnotifier01", "action": "install_plugin",
            "payload_hash": _pl_approval_hash(subject),
        },
        "preview_ref": {"preview_id": "plugprev_installnotifier01"},
        "created_at": "2026-07-20T12:00:00Z",
    }
    request["request_digest"] = _pl_digest("plugin-request", request)
    broker = _PlBroker({"anvil-connector-host": ["deploy-channel-ref"]})
    service.store.install(
        request, _PlDiscovery(catalog, capability), broker,
        lambda discovered, handles: _PlOutcome(status="installed", output={"ok": True},
                                               summary="Installed deploy-notifier 1.0.0."),
    )
    return service, request["request_digest"]


def _pl_client(service) -> TestClient:
    settings = Settings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator", "reviewer"}), bridge_bootstrap_token="",
        anvil_router_base_url="", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=True,
    )
    return TestClient(create_app(
        settings=settings, store=MemoryStore(), graph=NullGraph(), plugin_host_service=service,
    ))


def test_unconfigured_plugin_host_fails_closed():
    # Fail-closed when the plugin host is not configured (deliberately not wired
    # into the live poll loop): every endpoint refuses with 503.
    settings = Settings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator"}), bridge_bootstrap_token="",
        anvil_router_base_url="", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=True,
    )
    with TestClient(create_app(settings=settings, store=MemoryStore(), graph=NullGraph())) as client_:
        assert client_.get("/api/plugins", headers=_PL_ACTOR).status_code == 503
        assert client_.get("/api/plugins/deploy-notifier", headers=_PL_ACTOR).status_code == 503
        assert client_.get(
            "/api/plugins/receipts/sha256:" + "0" * 64, headers=_PL_ACTOR
        ).status_code == 503


def test_plugin_discovery_lists_only_approved_and_enabled_plugins():
    # T002 criterion 1: the projection shows exactly the approved AND capability-
    # enabled plugins, and only the enabled tools of each.
    service, _ = _pl_service_with_install()
    with _pl_client(service) as client_:
        body = client_.get("/api/plugins", headers=_PL_ACTOR).json()
        ids = {p["plugin_id"] for p in body["plugins"]}
        assert ids == {"anvil-tasks-viewer", "deploy-notifier"}
        viewer = next(p for p in body["plugins"] if p["plugin_id"] == "anvil-tasks-viewer")
        # Only the profile-enabled tools are projected (tasks.list, issues.read).
        assert {t["tool_id"] for t in viewer["tools"]} == {"tasks.list", "issues.read"}


def test_plugin_discovery_omits_reviewed_but_not_enabled_plugins_and_tools():
    # Finding 2 (discriminating fixture): the catalog reviews BOTH plugins and all
    # tools, but the profile enables ONLY anvil-tasks-viewer's tasks.list. GET
    # /api/plugins must reflect the ENABLED set -- omitting the not-enabled
    # deploy-notifier plugin AND the not-enabled issues.read tool. It fails if the
    # projection iterates the catalog instead of the enabled set. (The prior
    # fixture enabled every plugin and tool, so a catalog-iterating regression
    # would have passed unnoticed.)
    catalog = _pl_load("plugin.catalog.v1.json")
    capability = _pl_load("plugin.capability.v1.json")
    capability["plugins"] = [e for e in capability["plugins"] if e["plugin_id"] == "anvil-tasks-viewer"]
    for entry in capability["plugins"]:
        entry["enabled_tools"] = ["tasks.list"]
    capability["digest"] = _pl_digest("plugin-capability", capability)
    service = _PlService(_PlDiscovery(catalog, capability))
    with _pl_client(service) as client_:
        body = client_.get("/api/plugins", headers=_PL_ACTOR).json()
        ids = {p["plugin_id"] for p in body["plugins"]}
        assert ids == {"anvil-tasks-viewer"}
        assert "deploy-notifier" not in ids
        viewer = next(p for p in body["plugins"] if p["plugin_id"] == "anvil-tasks-viewer")
        assert {t["tool_id"] for t in viewer["tools"]} == {"tasks.list"}
        assert "issues.read" not in {t["tool_id"] for t in viewer["tools"]}
        # The reviewed-but-not-enabled plugin is also a plain 404 at the detail hop.
        assert client_.get("/api/plugins/deploy-notifier", headers=_PL_ACTOR).status_code == 404


def test_plugin_host_wired_from_settings_when_both_files_declared(tmp_path):
    # Finding 3: an operator who declares BOTH the reviewed catalog and the
    # capability profile files gets a live read-only discovery surface built by
    # create_app from Settings -- no manual service injection. With neither declared
    # the plugin host stays unconfigured and fails closed (503).
    cat = tmp_path / "catalog.json"
    cap = tmp_path / "capability.json"
    cat.write_text(_pl_json.dumps(_pl_load("plugin.catalog.v1.json")), encoding="utf-8")
    cap.write_text(_pl_json.dumps(_pl_load("plugin.capability.v1.json")), encoding="utf-8")
    wired = Settings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator"}), bridge_bootstrap_token="",
        anvil_router_base_url="", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=True,
        plugin_catalog_file=str(cat), plugin_capability_file=str(cap),
    )
    with TestClient(create_app(settings=wired, store=MemoryStore(), graph=NullGraph())) as client_:
        resp = client_.get("/api/plugins", headers=_PL_ACTOR)
        assert resp.status_code == 200
        assert {p["plugin_id"] for p in resp.json()["plugins"]} == {"anvil-tasks-viewer", "deploy-notifier"}

    unset = Settings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator"}), bridge_bootstrap_token="",
        anvil_router_base_url="", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=True,
    )
    with TestClient(create_app(settings=unset, store=MemoryStore(), graph=NullGraph())) as client_:
        assert client_.get("/api/plugins", headers=_PL_ACTOR).status_code == 503


def test_plugin_discovery_returns_no_credential_value_to_the_browser():
    # T003 criterion 1 (return direction): the discovery projection reports
    # credentials by opaque reference only -- never a value.
    service, _ = _pl_service_with_install()
    with _pl_client(service) as client_:
        raw = client_.get("/api/plugins", headers=_PL_ACTOR).text
        notifier = next(
            p for p in client_.get("/api/plugins", headers=_PL_ACTOR).json()["plugins"]
            if p["plugin_id"] == "deploy-notifier"
        )
        assert notifier["credential"] == {
            "requirement": "host_owned",
            "owner_host": "anvil-connector-host",
            "credential_refs": ["deploy-channel-ref"],
        }
        lowered = raw.lower()
        for marker in ("secret", "password", "api_key", "bearer", "://"):
            assert marker not in lowered, f"discovery leaked {marker!r}"


def test_plugin_detail_is_indistinct_for_unknown_or_not_enabled():
    # An unknown plugin returns the byte-identical 404 of a genuinely missing one
    # (no existence oracle); a known+enabled one returns 200.
    service, _ = _pl_service_with_install()
    with _pl_client(service) as client_:
        assert client_.get("/api/plugins/deploy-notifier", headers=_PL_ACTOR).status_code == 200
        unknown = client_.get("/api/plugins/some-other-plugin", headers=_PL_ACTOR)
        never = client_.get("/api/plugins/zzz-not-here", headers=_PL_ACTOR)
        assert unknown.status_code == never.status_code == 404
        assert unknown.content == never.content


def test_plugin_receipt_endpoint_serves_redacted_receipt_and_404s_missing():
    service, digest = _pl_service_with_install()
    with _pl_client(service) as client_:
        ok = client_.get(f"/api/plugins/receipts/{digest}", headers=_PL_ACTOR)
        assert ok.status_code == 200
        receipt = ok.json()["receipt"]
        assert receipt["status"] == "accepted"
        assert receipt["credential_use"]["requirement"] == "host_owned"
        # No credential value in the served receipt.
        blob = ok.text.lower()
        for marker in ("secret", "password", "bearer", "://"):
            assert marker not in blob
        missing = client_.get("/api/plugins/receipts/sha256:" + "0" * 64, headers=_PL_ACTOR)
        assert missing.status_code == 404


def test_plugin_surface_requires_an_allowlisted_actor():
    service, _ = _pl_service_with_install()
    settings = Settings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator"}), bridge_bootstrap_token="",
        anvil_router_base_url="", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=False,
    )
    with TestClient(create_app(
        settings=settings, store=MemoryStore(), graph=NullGraph(), plugin_host_service=service,
    )) as client_:
        assert client_.get("/api/plugins", headers={"X-Workbench-Actor": "intruder"}).status_code == 403
        assert client_.get("/api/plugins").status_code == 401
# Actor-scoped preference read/write surface (preferences-configuration:T002.3)
# ---------------------------------------------------------------------------

import json as _pref_json
from pathlib import Path as _PrefPath

from workbench.store import MemoryPreferenceStore as _MemoryPreferenceStore

_PREF_ACTOR = {"X-Workbench-Actor": "operator"}
_PREF_OTHER = {"X-Workbench-Actor": "reviewer"}


def _pref_settings() -> Settings:
    return Settings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator", "reviewer"}), bridge_bootstrap_token="",
        anvil_router_base_url="http://100.87.34.66:8000/v1", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=True,
    )


def _pref_catalog() -> dict:
    path = _PrefPath(__file__).resolve().parents[1] / "docs" / "contracts" / "examples" / "settings-descriptor.v1.json"
    return _pref_json.loads(path.read_text(encoding="utf-8"))


def _pref_client(store: _MemoryPreferenceStore | None) -> TestClient:
    return TestClient(create_app(
        settings=_pref_settings(), store=MemoryStore(), graph=NullGraph(), preference_store=store,
    ))


def test_preferences_surface_fails_closed_when_unconfigured():
    # Not wired into the live loop: with no injected store every endpoint 503s.
    with _pref_client(None) as test_client:
        assert test_client.get("/api/preferences", headers=_PREF_ACTOR).status_code == 503
        assert test_client.get(
            "/api/preferences/personal.time_format", headers=_PREF_ACTOR
        ).status_code == 503


def test_preferences_effective_view_serializes_only_actor_view_and_clamps():
    store = _MemoryPreferenceStore(_pref_catalog())
    # Tighten the policy ceiling to 30 and set a personal value above it. The
    # policy ceiling is seeded via the authority path (an actor cannot write an
    # approval-gated policy value through set_preference).
    store.seed_authority_value("policy", "policy.transcript_retention_max_days", 30)
    store.set_preference("personal", "operator", "personal.chat_transcript_retention_days", 60, 0, "operator")
    with _pref_client(store) as test_client:
        body = test_client.get("/api/preferences", headers=_PREF_ACTOR).json()
    effective = {item["setting_id"]: item for item in body["effective"]}
    # The personal value is clamped down to the policy ceiling.
    assert effective["personal.chat_transcript_retention_days"]["value"] == 30
    assert effective["personal.chat_transcript_retention_days"]["source"] == "clamped"
    # Only actor-scope descriptors are serialized -- no authority/secret settings.
    catalog_ids = {setting["id"] for setting in body["catalog"]["settings"]}
    for authority_id in (
        "policy.transcript_retention_max_days", "deployment.identity_header_name",
        "deployment.state_read_location",
    ):
        assert authority_id not in catalog_ids
        assert authority_id not in effective
    blob = _pref_json.dumps(body)
    assert "state_read_location" not in blob and "identity_header" not in blob


def test_preference_write_read_and_version_increment_through_the_api():
    store = _MemoryPreferenceStore(_pref_catalog())
    with _pref_client(store) as test_client:
        first = test_client.put(
            "/api/preferences/personal.time_format", headers=_PREF_ACTOR,
            json={"scope": "personal", "value": "format_12h", "expected_version": 0},
        )
        assert first.status_code == 200 and first.json()["preference"]["write_version"] == 1
        read = test_client.get(
            "/api/preferences/personal.time_format?scope=personal", headers=_PREF_ACTOR,
        )
        assert read.status_code == 200 and read.json()["preference"]["value"] == "format_12h"


def test_stale_write_is_reload_required_409_distinct_from_a_422_validation_error():
    store = _MemoryPreferenceStore(_pref_catalog())
    with _pref_client(store) as test_client:
        test_client.put(
            "/api/preferences/personal.time_format", headers=_PREF_ACTOR,
            json={"scope": "personal", "value": "format_12h", "expected_version": 0},
        )
        # A stale expected_version is a reload-required 409, not a validation error.
        stale = test_client.put(
            "/api/preferences/personal.time_format", headers=_PREF_ACTOR,
            json={"scope": "personal", "value": "format_24h", "expected_version": 0},
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["reload_required"] is True
        assert stale.json()["detail"]["current_version"] == 1
        # A malformed value is a distinct 422 (not a reload conflict).
        bad = test_client.put(
            "/api/preferences/personal.chat_transcript_retention_days", headers=_PREF_ACTOR,
            json={"scope": "personal", "value": 5000, "expected_version": 0},
        )
        assert bad.status_code == 422


def test_cross_actor_preference_read_is_indistinct_not_found():
    store = _MemoryPreferenceStore(_pref_catalog())
    with _pref_client(store) as test_client:
        test_client.put(
            "/api/preferences/personal.time_format", headers=_PREF_ACTOR,
            json={"scope": "personal", "value": "format_12h", "expected_version": 0},
        )
        # Another actor cannot read operator's personal value: the response is the
        # SAME indistinct 404 body a genuinely missing preference returns.
        foreign = test_client.get(
            "/api/preferences/personal.time_format?scope=personal", headers=_PREF_OTHER,
        )
        missing = test_client.get(
            "/api/preferences/personal.landing_surface?scope=personal", headers=_PREF_OTHER,
        )
        assert foreign.status_code == missing.status_code == 404
        assert foreign.json() == missing.json() == {"detail": "unknown preference"}


def test_malformed_setting_id_is_rejected_at_the_edge():
    store = _MemoryPreferenceStore(_pref_catalog())
    with _pref_client(store) as test_client:
        # An id that does not match the setting-id grammar is a 422 at the edge.
        assert test_client.get(
            "/api/preferences/NotAValidId", headers=_PREF_ACTOR,
        ).status_code == 422


def test_cross_scope_write_is_indistinct_from_an_unknown_id_not_an_oracle():
    # T002.3 crit 2: a cross-scope WRITE must not be an existence oracle. Writing
    # a REAL authority setting id from a personal scope returns the SAME indistinct
    # 404 body as writing a genuinely unknown id -- so the write surface cannot be
    # used to learn which authority setting ids exist (the ids the read surface
    # hides). A distinct 409 "not owned by this scope" here would leak existence.
    store = _MemoryPreferenceStore(_pref_catalog())
    with _pref_client(store) as test_client:
        authority = test_client.put(
            "/api/preferences/deployment.state_read_location", headers=_PREF_ACTOR,
            json={"scope": "personal", "value": "x", "expected_version": 0},
        )
        unknown = test_client.put(
            "/api/preferences/personal.i_do_not_exist", headers=_PREF_ACTOR,
            json={"scope": "personal", "value": "x", "expected_version": 0},
        )
        # A policy (approval-gated) id is likewise indistinct from an unknown id.
        policy_id = test_client.put(
            "/api/preferences/policy.route_allowlist_profile", headers=_PREF_ACTOR,
            json={"scope": "personal", "value": "x", "expected_version": 0},
        )
    assert authority.status_code == unknown.status_code == policy_id.status_code == 404
    assert authority.json() == unknown.json() == policy_id.json() == {"detail": "unknown preference"}


def test_mis_scoped_injected_row_cannot_escalate_over_declared_precedence():
    # Finding 6: the GET merge is ownership-filtered, so a corrupt/injected row
    # bearing a foreign-scope id (a personal namespace carrying a policy ceiling
    # id) cannot override the real authority value against scope_precedence.
    from workbench.models import PreferenceRecord
    from workbench.store import PreferenceRows

    store = _MemoryPreferenceStore(_pref_catalog())
    # Seed the genuine authority ceiling at 30.
    store.seed_authority_value("policy", "policy.transcript_retention_max_days", 30)
    # Inject a corrupt personal-namespace row that spoofs the POLICY ceiling id
    # with a wide-open 365, plus a personal value of 60 that a lifted ceiling
    # would fail to clamp. (Constructed directly: set_preference would refuse the
    # spoofed policy-id row; 60 is within the personal bound [1, 90].)
    store.rows.records.setdefault(("personal", "operator"), {})
    store.rows.records[("personal", "operator")]["policy.transcript_retention_max_days"] = PreferenceRecord(
        setting_id="policy.transcript_retention_max_days", scope="personal", scope_key="operator",
        value=365, write_version=1, updated_by="operator",
    )
    store.set_preference("personal", "operator", "personal.chat_transcript_retention_days", 60, 0, "operator")
    with _pref_client(store) as test_client:
        body = test_client.get("/api/preferences", headers=_PREF_ACTOR).json()
    effective = {item["setting_id"]: item for item in body["effective"]}
    # The real authority ceiling (30) wins: the injected personal row is dropped
    # at the ownership-filtered merge, so the personal 200 is clamped to 30, not
    # to the spoofed 365.
    assert effective["personal.chat_transcript_retention_days"]["value"] == 30
    assert effective["personal.chat_transcript_retention_days"]["source"] == "clamped"


def test_preference_write_rejects_unknown_body_fields():
    # Finding 8: the write/reset inputs forbid unknown fields, so a client cannot
    # smuggle an undeclared key (e.g. a spoofed scope_key) past the typed edge.
    store = _MemoryPreferenceStore(_pref_catalog())
    with _pref_client(store) as test_client:
        resp = test_client.put(
            "/api/preferences/personal.time_format", headers=_PREF_ACTOR,
            json={"scope": "personal", "value": "format_12h", "expected_version": 0, "scope_key": "victim"},
        )
        assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# plan-task-delivery T002/T004/T008 — delivery projection browser surface,
# pinned operational rows/approval bindings, and typed directive semantics
# through the ACTUAL wired API entrypoint.
# --------------------------------------------------------------------------- #

from _support import load_example as _ptd_load_example
from workbench.contracts import contract_digest as _ptd_contract_digest
from workbench.delivery_projection import (
    ApprovalBinding as _PtdApprovalBinding,
    MemoryDeliveryProjectionStore as _PtdProjectionStore,
    RunDisplayRow as _PtdRunRow,
)

_PTD_ACTOR = {"X-Workbench-Actor": "operator"}
_PTD_DIGEST_A = "sha256:5ddaacfaf8405e6e3f0d0a920e0f1f2b20afadded4f8d98748fb42868da0ad2e"
_PTD_DIGEST_B = "sha256:" + "b" * 64


def _ptd_client(projection_store):
    settings = Settings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator", "reviewer"}), bridge_bootstrap_token="",
        anvil_router_base_url="http://100.87.34.66:8000/v1", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=True,
    )
    return TestClient(create_app(
        settings=settings, store=MemoryStore(), graph=NullGraph(),
        delivery_projection_store=projection_store,
    ))


def _ptd_reference(prd_id="release-alpha", snapshot_digest=None):
    ref = _ptd_load_example("task-reference.v1.json")
    if prd_id != "release-alpha":
        ref["ref"]["prd_id"] = prd_id
        ref["scoped_id"] = f"{prd_id}:T001"
        ref["run_label"] = f"{prd_id}:T001@r4"
        ref["hierarchy"]["prd_id"] = prd_id
    if snapshot_digest is not None:
        ref["source"]["snapshot_digest"] = snapshot_digest
    return ref


def _ptd_eligible(prd_id="release-alpha"):
    return {
        "schema_version": "workbench-delivery-eligibility/v1",
        "ref": {"prd_id": prd_id, "task_id": "T001", "prd_revision": 4},
        "scoped_id": f"{prd_id}:T001", "eligible": True, "state": "eligible",
        "reasons": [{"class": "info", "code": "info.ready", "content_trust": "untrusted_task_data",
                     "explanation": "All dependencies are merged and the source is current."}],
    }


def _ptd_prd_content(body):
    doc = {
        "schema_version": "workbench-prd-content/v1",
        "content_digest": "sha256:" + "0" * 64,
        "provider": "anvil-state",
        "generated_at": "2026-07-20T12:00:00Z",
        "prd": {"prd_id": "release-alpha", "title": "Chat-first Workbench", "status": "approved", "revision": 4},
        "content_trust": "untrusted_task_data",
        "content": {"format": "markdown", "body": body, "truncated": False,
                    "total_bytes": len(body.encode("utf-8"))},
        "redaction": {"status": "redacted", "ruleset": "hub.default"},
    }
    doc["content_digest"] = _ptd_contract_digest("prd-content", doc)
    return doc


def test_ptd_t002_delivery_surface_fails_closed_when_unconfigured():
    with _ptd_client(None) as client:
        r = client.get("/api/projects/proj/prds/release-alpha/tasks", headers=_PTD_ACTOR)
        assert r.status_code == 503


def test_ptd_t002_task_and_eligibility_readable_and_scoped():
    store = _PtdProjectionStore()
    store.capture_task_reference("proj", _ptd_reference("release-alpha", _PTD_DIGEST_A))
    store.capture_task_reference("proj", _ptd_reference("release-beta", _PTD_DIGEST_A))
    store.capture_eligibility("proj", _ptd_eligible("release-alpha"))
    with _ptd_client(store) as client:
        tasks = client.get("/api/projects/proj/prds/release-alpha/tasks", headers=_PTD_ACTOR).json()["tasks"]
        assert [t["scoped_id"] for t in tasks] == ["release-alpha:T001"]  # cross-PRD does not collapse
        one = client.get("/api/projects/proj/prds/release-alpha/tasks/T001", headers=_PTD_ACTOR).json()["task"]
        assert one["scoped_id"] == "release-alpha:T001"
        elig = client.get("/api/projects/proj/prds/release-alpha/tasks/T001/eligibility", headers=_PTD_ACTOR).json()
        assert elig["eligibility"]["state"] == "eligible"
        # Cross-project read is the indistinct 404, never an existence oracle.
        foreign = client.get("/api/projects/intruder/prds/release-alpha/tasks/T001", headers=_PTD_ACTOR)
        assert foreign.status_code == 404 and foreign.json()["detail"] == "unknown delivery record"


def test_ptd_t002_eligibility_becomes_stale_through_the_wired_get():
    store = _PtdProjectionStore()
    store.capture_task_reference("proj", _ptd_reference("release-alpha", _PTD_DIGEST_A))
    store.capture_eligibility("proj", _ptd_eligible("release-alpha"))
    with _ptd_client(store) as client:
        before = client.get("/api/projects/proj/prds/release-alpha/tasks/T001/eligibility",
                            headers=_PTD_ACTOR).json()["eligibility"]
        assert before["state"] == "eligible"
        # The source snapshot advances via a real recapture; the wired GET now
        # returns a stale verdict rather than the superseded eligible one.
        store.capture_task_reference("proj", _ptd_reference("release-alpha", _PTD_DIGEST_B))
        after = client.get("/api/projects/proj/prds/release-alpha/tasks/T001/eligibility",
                           headers=_PTD_ACTOR).json()["eligibility"]
        assert after["state"] == "stale" and after["eligible"] is False
        assert after["reasons"][0]["code"] == "stale.snapshot_superseded"


def test_ptd_t002_served_prd_body_is_redacted():
    leaky = (
        "See AKIA1234567890ABCDEF and token=supersecretvalue.\n"
        "Deploy from C:/Users/op/.anvil/state.db and /etc/anvil/prod.env.\n"
        "JWT eyJhbGciOiJI.eyJzdWIiOiIx.sig ghp_abcdefghijklmnopqrstuvwxyz0123456789.\n"
        "sk-proj-abcdefghijklmnopqrstuvwx reaches db.tail1234.ts.net:7687 at 100.64.0.5:8443.\n"
        "Server=db.internal;User Id=admin;Password=hunter2"
    )
    store = _PtdProjectionStore()
    store.capture_prd_content("proj", _ptd_prd_content(leaky))
    with _ptd_client(store) as client:
        body = client.get("/api/projects/proj/prds/release-alpha/content",
                          headers=_PTD_ACTOR).json()["content"]["content"]["body"]
    for secret in ("AKIA1234567890ABCDEF", "supersecretvalue", "state.db", "/etc/anvil",
                   "eyJhbGciOiJI", "ghp_abcdefghijklmnop", "sk-proj-abcdef", "tail1234.ts.net",
                   "100.64.0.5", "hunter2", "C:/Users"):
        assert secret not in body, f"leak survived: {secret}"
    assert "[REDACTED" in body  # the scrub actually fired


def test_ptd_t004_run_list_headline_is_title_and_approval_binding_readable():
    store = _PtdProjectionStore()
    store.capture_run_row("proj", _PtdRunRow(
        run_id="run_alpha_t001_0001", run_label="release-alpha:T001@r4", scoped_id="release-alpha:T001",
        prd_id="release-alpha", task_id="T001", prd_revision=4, task_title="Add routed chat",
        prd_title="Chat-first Workbench", status="running", attempt_label="attempt 1",
        started_at="2026-07-20T12:00:01Z", workflow_digest="sha256:" + "0" * 64,
        capability_profile_digest="sha256:" + "4" * 64,
    ))
    store.capture_approval_binding("proj", _PtdApprovalBinding(
        approval_id="approval_alpha_0001", scoped_id="release-alpha:T001",
        run_label="release-alpha:T001@r4", action="commit_pr", payload_hash="a" * 64,
        bridge_id="bridge-1", expires_at="2026-07-20T13:00:01Z",
        workflow_digest="sha256:" + "0" * 64, capability_profile_digest="sha256:" + "4" * 64,
    ))
    with _ptd_client(store) as client:
        runs = client.get("/api/projects/proj/delivery/runs?run_status=running", headers=_PTD_ACTOR).json()["runs"]
        assert len(runs) == 1 and runs[0]["headline"] == "Add routed chat"
        assert runs[0]["headline"] != runs[0]["scoped_id"]  # not a bare id
        binding = client.get("/api/projects/proj/delivery/approvals/approval_alpha_0001",
                             headers=_PTD_ACTOR).json()["approval"]
        assert binding["payload_hash"] == "a" * 64 and binding["action"] == "commit_pr"
        assert binding["scoped_id"] == "release-alpha:T001" and binding["run_label"] == "release-alpha:T001@r4"


def test_ptd_t008_directive_post_returns_typed_outcome_and_get_splits_pending():
    with client() as test_client:
        project = test_client.post("/api/projects", json={"name": "demo", "state_root": ".anvil"}).json()
        session = test_client.post("/api/sessions", json={
            "project_id": project["id"], "title": "s", "worktree_id": "checkout-a",
        }).json()["session"]
        posted = test_client.post(
            f"/api/sessions/{session['id']}/directives",
            json={"content": "Run the independent evidence check."},
        )
        assert posted.status_code == 202
        body = posted.json()
        assert body["outcome"] == "directive.queued_pending" and body["recorded"] is True
        view = test_client.get(f"/api/sessions/{session['id']}/directives", headers=_PTD_ACTOR).json()
        assert [d["content"] for d in view["pending"]] == ["Run the independent evidence check."]
        assert view["included"] == []


def test_ptd_t008_directive_content_is_scrubbed_before_persist():
    with client() as test_client:
        project = test_client.post("/api/projects", json={"name": "demo", "state_root": ".anvil"}).json()
        session = test_client.post("/api/sessions", json={
            "project_id": project["id"], "title": "s", "worktree_id": "checkout-a",
        }).json()["session"]
        test_client.post(
            f"/api/sessions/{session['id']}/directives",
            json={"content": "deploy from C:/secrets/state.db with token=supersecretvalue"},
        )
        view = test_client.get(f"/api/sessions/{session['id']}/directives", headers=_PTD_ACTOR).json()
        content = view["pending"][0]["content"]
        assert "state.db" not in content and "supersecretvalue" not in content
        assert "[REDACTED" in content


def test_ptd_t008_persisted_directive_scrubs_dotless_host_port():
    # Finding 4 (persisted channel): a scheme-less single-label host:port
    # (serving:8443) must be scrubbed before a directive is persisted/served. The
    # shared redact_config_text now removes the dotless label:port; reverting that
    # pattern lets serving:8443 ride out to the served directive view.
    with client() as test_client:
        project = test_client.post("/api/projects", json={"name": "demo", "state_root": ".anvil"}).json()
        session = test_client.post("/api/sessions", json={
            "project_id": project["id"], "title": "s", "worktree_id": "checkout-a",
        }).json()["session"]
        test_client.post(
            f"/api/sessions/{session['id']}/directives",
            json={"content": "point the run at serving:8443 before the gate"},
        )
        view = test_client.get(f"/api/sessions/{session['id']}/directives", headers=_PTD_ACTOR).json()
        content = view["pending"][0]["content"]
        assert "serving:8443" not in content
        assert "[REDACTED" in content


def test_ptd_t008_directive_reports_included_after_real_packet_assembly():
    # MUST-FIX 1 (wired, vacuum-proof): a directive already carried in a queued
    # run_codex packet must report INCLUDED, not pending forever. The live packet
    # assembler (workflow start -> start_workflow_run) now records the
    # operator.directive_packet marker; session_directive_view derives the split
    # from it. Reverting the marker recording makes this fail (still pending).
    with client() as test_client:
        project = test_client.post("/api/projects", json={"name": "wired", "state_root": ".anvil"}).json()
        registered = test_client.post(f"/api/projects/{project['id']}/bridges", json={"name": "bridge"}).json()
        bridge, token = registered["bridge"], registered["bootstrap_token"]
        headers = {"X-Workbench-Bridge": bridge["id"], "Authorization": f"Bearer {token}"}
        digest = "a" * 64
        test_client.post(f"/api/bridge/{bridge['id']}/skills", headers=headers, json={"skills": [{
            "skill_id": "anvil:review", "description": "Review state evidence.", "content_sha256": digest,
        }]})
        session = test_client.post("/api/sessions", json={
            "project_id": project["id"], "title": "wired", "worktree_id": "default", "skills": ["anvil:review"],
        }).json()
        posted = test_client.post(
            f"/api/sessions/{session['session']['id']}/directives",
            json={"content": "Run the independent evidence check."},
        ).json()
        directive_sequence = posted["event"]["sequence"]

        # Before packet assembly the directive is pending.
        before = test_client.get(
            f"/api/sessions/{session['session']['id']}/directives", headers=_PTD_ACTOR,
        ).json()
        assert [d["content"] for d in before["pending"]] == ["Run the independent evidence check."]
        assert before["included"] == [] and before["included_up_to_sequence"] == 0

        # Start the workflow: the real packet assembler snapshots the directive
        # into the queued run_codex payload AND records the packet-inclusion marker.
        started = test_client.post(f"/api/workflows/{session['workflow']['id']}/start", json={"task_id": "TASK-9"})
        assert started.status_code == 201

        after = test_client.get(
            f"/api/sessions/{session['session']['id']}/directives", headers=_PTD_ACTOR,
        ).json()
        assert [d["content"] for d in after["included"]] == ["Run the independent evidence check."]
        assert after["pending"] == []
        assert after["included_up_to_sequence"] >= directive_sequence


# --------------------------------------------------------------------------- #
# reviewed-tools-plugins T004/T005 — the read-only chat capability-pin +
# dispatch-record browser surface: fail-closed (503) until a dispatch service is
# injected, GET-only, and scrubbed at the last hop.
# --------------------------------------------------------------------------- #

import json as _ctd_json
from pathlib import Path as _CtdPath

from workbench.contracts import (
    approval_payload_digest as _ctd_subject_hash,
    contract_digest as _ctd_digest,
    _plugin_approval_subject as _ctd_subject,
)
from workbench.store import UnknownOutcomeError as _CtdUnknown
from workbench.tool_dispatch import (
    ChatToolDispatchService as _CtdService,
    ChatToolSession as _CtdSession,
)

_CTD_EX = _CtdPath(__file__).resolve().parents[1] / "docs" / "contracts" / "examples"
_CTD_ACTOR = {"X-Workbench-Actor": "operator"}
_CTD_NOTIFIER_DIGEST = "sha256:5474ca8eb2d41d767772c8a5ba33a1e90f5cb57017c4c8ab6487bd8ee6ba8dbb"


def _ctd_load(name):
    return _ctd_json.loads((_CTD_EX / name).read_text(encoding="utf-8"))


def _ctd_service_with_reconcile():
    session = _CtdSession(
        session_id="chatapi1", catalog=_ctd_load("plugin.catalog.v1.json"),
        capability=_ctd_load("plugin.capability.v1.json"),
        actor_id="operator-01", bridge_id="bridge-a", project_id="proj-1",
    )
    service = _CtdService(session)
    req = {
        "schema_version": "workbench-plugin-request/v1", "request_id": "plugreq_notifyapi0001",
        "kind": "tool_call", "actor": {"actor_id": "operator-01", "kind": "operator"},
        "plugin": {"plugin_id": "deploy-notifier", "plugin_digest": _CTD_NOTIFIER_DIGEST},
        "tool_call": {"tool_id": "notify.send", "inputs": {"message_ref": "deploy-msg-1"}},
        "created_at": "2026-07-20T12:00:00Z",
    }
    subject_hash = _ctd_subject_hash(_ctd_subject(req))
    req["approval"] = {"grant_id": "approval_apigrant0001", "action": "invoke_effect_tool",
                       "payload_hash": subject_hash}
    req["request_digest"] = _ctd_digest("plugin-request", req)
    service.approvals.grant("approval_apigrant0001", "invoke_effect_tool", subject_hash,
                            "bridge-a", "proj-1")

    def unconfirmed(_d, _i):
        raise _CtdUnknown("outcome unknown near serving:8443 token=ghp_abcdefghijklmnopqrstuvwxyz012345",
                          reason="unknown_outcome")

    service.dispatch(req, unconfirmed)
    return service, req["request_digest"]


def _ctd_client(service):
    settings = Settings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator"}), bridge_bootstrap_token="",
        anvil_router_base_url="", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=True,
    )
    return TestClient(create_app(settings=settings, store=MemoryStore(), graph=NullGraph(),
                                 chat_tool_dispatch_service=service))


def test_unconfigured_chat_tools_surface_fails_closed():
    settings = Settings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator"}), bridge_bootstrap_token="",
        anvil_router_base_url="", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=True,
    )
    with TestClient(create_app(settings=settings, store=MemoryStore(), graph=NullGraph())) as client_:
        assert client_.get("/api/chat/tools", headers=_CTD_ACTOR).status_code == 503
        assert client_.get(
            "/api/chat/tools/receipts/sha256:" + "0" * 64, headers=_CTD_ACTOR).status_code == 503
        assert client_.get(
            "/api/chat/tools/reconciliations/sha256:" + "0" * 64, headers=_CTD_ACTOR).status_code == 503


def test_chat_tools_lists_pinned_tools_by_reference_only():
    service, _ = _ctd_service_with_reconcile()
    with _ctd_client(service) as client_:
        body = client_.get("/api/chat/tools", headers=_CTD_ACTOR).json()
    tools = {t["tool_id"] for p in body["tools"] for t in p["tools"]}
    assert tools == {"tasks.list", "issues.read", "notify.send"}
    text = _ctd_json.dumps(body)
    assert '"value"' not in text and '"secret"' not in text


def test_chat_tools_served_reconciliation_is_scrubbed_and_unknown_is_a_plain_404():
    service, digest = _ctd_service_with_reconcile()
    with _ctd_client(service) as client_:
        resp = client_.get(f"/api/chat/tools/reconciliations/{digest}", headers=_CTD_ACTOR)
        assert resp.status_code == 200
        text = _ctd_json.dumps(resp.json())
        assert "serving:8443" not in text
        assert "ghp_abcdefghijklmnopqrstuvwxyz012345" not in text
        # An unknown digest is a plain fixed 404 (not an existence oracle).
        missing = client_.get(
            "/api/chat/tools/reconciliations/sha256:" + "0" * 64, headers=_CTD_ACTOR)
        assert missing.status_code == 404


# =========================================================================== #
# reviewed-tools-plugins T009: NO runtime or browser path accepts an OpenAPI URL
# or document. The reviewed-plugin browser surface is read-only (GET), and there
# is no compile/ingestion endpoint -- proven through the wired create_app router.
# =========================================================================== #


def test_t009_plugin_browser_surface_is_get_only_and_ingests_no_openapi():
    service, _digest = _pl_service_with_install()
    client = _pl_client(service)
    # There is no OpenAPI ingestion / compile endpoint at all.
    assert client.post("/api/plugins", json={"openapi": "3.0.3", "paths": {}}, headers=_PL_ACTOR).status_code in (404, 405)
    assert client.post("/api/plugins/compile", json={"url": "https://x/openapi.json"}, headers=_PL_ACTOR).status_code in (404, 405)
    # The discovery surface itself is GET-only: a write verb is refused.
    assert client.post("/api/plugins/anvil-tasks-viewer", json={}, headers=_PL_ACTOR).status_code in (404, 405)
    assert client.put("/api/plugins/anvil-tasks-viewer", json={}, headers=_PL_ACTOR).status_code in (404, 405)


def test_t009_chat_tools_surface_ingests_no_openapi():
    # The chat-tools surface is likewise read-only: no OpenAPI document can be
    # POSTed to compile or dispatch a connector at runtime.
    settings = Settings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator"}), bridge_bootstrap_token="",
        anvil_router_base_url="", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=True,
    )
    client = TestClient(create_app(settings=settings, store=MemoryStore(), graph=NullGraph()))
    r = client.post("/api/chat/tools", json={"openapi": "3.0.3"}, headers=_PL_ACTOR)
    assert r.status_code in (404, 405)
