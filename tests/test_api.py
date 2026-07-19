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
        reconciled = test_client.post(f"/api/bridge/{bridge['id']}/runs/{run['id']}/status", headers=bridge_headers, json={"status": "reconciliation"})
        assert reconciled.status_code == 200
        assert reconciled.json()["completed_at"] is not None
        assert test_client.post(
            f"/api/bridge/{bridge['id']}/commands/{queued_run['id']}/ack", headers=bridge_headers,
        ).status_code == 202

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
        assert test_client.post(
            f"/api/bridge/{bridge['id']}/runs/{delivery_run['id']}/status",
            headers=bridge_headers, json={"status": "evidenced"},
        ).status_code == 200
        assert test_client.post(
            f"/api/bridge/{bridge['id']}/runs/{delivery_run['id']}/status",
            headers=bridge_headers, json={"status": "completed"},
        ).status_code == 422
        assert test_client.post(
            f"/api/bridge/{bridge['id']}/commands/{queued_delivery['id']}/ack", headers=bridge_headers,
        ).status_code == 202

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
        acknowledged = test_client.post(f"/api/bridge/{bridge['id']}/commands/{command['id']}/ack", headers=bridge_headers)
        assert acknowledged.status_code == 202

        completed = test_client.post(
            f"/api/bridge/{bridge['id']}/workflows/{workflow['id']}/steps/implement",
            headers=bridge_headers, json={"outcome": "succeeded"},
        )
        assert completed.status_code == 200
        assert completed.json()["status"] == "waiting_approval"
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
