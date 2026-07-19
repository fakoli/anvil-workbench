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

        approval = test_client.post("/api/approvals", json={
            "project_id": project["id"], "bridge_id": bridge["id"], "action_type": "commit_pr",
            "payload": {"diff_hash": "before", "branch": "codex/demo"},
        }).json()
        denied = test_client.post(f"/api/bridge/{bridge['id']}/approvals/{approval['id']}/consume", headers=bridge_headers, json={"payload_hash": approval["payload_hash"]})
        assert denied.status_code == 409
        assert test_client.post(f"/api/approvals/{approval['id']}/approve", headers={"X-Workbench-Actor": "reviewer"}).status_code == 200
        queued_action = test_client.get(f"/api/bridge/{bridge['id']}/commands/next", headers=bridge_headers).json()
        assert queued_action["approval_id"] == approval["id"]
        changed = test_client.post(f"/api/bridge/{bridge['id']}/approvals/{approval['id']}/consume", headers=bridge_headers, json={"payload_hash": "changed"})
        assert changed.status_code == 409
        consumed = test_client.post(f"/api/bridge/{bridge['id']}/approvals/{approval['id']}/consume", headers=bridge_headers, json={"payload_hash": approval["payload_hash"]})
        assert consumed.status_code == 200
        assert consumed.json()["status"] == "consumed"


def test_api_never_exposes_a_bridge_secret_in_bootstrap():
    with client() as test_client:
        response = test_client.get("/api/bootstrap")
    assert response.status_code == 200
    assert "token" not in response.text.lower()
