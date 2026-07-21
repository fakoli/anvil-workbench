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
# Read-only system-health + observational posture surface (preferences-
# configuration T003.2 / T008): every declared integration's descriptor,
# truthful disabled/degraded states, a closed leak-proof response, GET-only (no
# mutation/execution/approval), and CLI/API finding parity.
# ---------------------------------------------------------------------------

from datetime import datetime as _datetime, timezone as _timezone

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
#: tautology.
_SYS_ALLOWED_FIELDS = frozenset({
    "configured", "dependencies", "digest", "integration_id", "non_canonical",
    "owner", "remediation", "schema_version", "state", "title",
    "version", "detail", "last_checked_at",
})
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


def test_system_health_last_hop_scrubs_an_adversarially_seeded_descriptor():
    # Redaction is enforced at the descriptor boundary, so even a service that
    # splices a secret/URL/path into descriptor prose cannot make the API emit it.
    seeded_remediation = (
        "token=leakedsecret at https://10.0.0.9/admin path /root/.ssh/id_rsa"
    )
    seeded = _SHDescriptor(
        integration_id="anvil_serving", title="Anvil Serving model plane",
        state="disabled", configured=False, owner="anvil-serving",
        remediation=seeded_remediation, last_checked_at="2026-07-21T00:00:00Z",
    )

    class _SeededService:
        def descriptors(self):
            return (seeded,)
        def get(self, integration_id):
            return seeded
        def posture(self):
            return _SHPostureReport(checks=(
                _SHPostureCheck(
                    check_id="posture.integration.anvil_serving", title="x",
                    status="disabled", severity="info", remediation=seeded_remediation,
                ),
            ))

    with _sys_client(_sys_settings(), service=_SeededService()) as client_:
        raw = client_.get("/api/system/health", headers=SYS_ACTOR).text
        assert "leakedsecret" not in raw and "10.0.0.9" not in raw and "/root/.ssh" not in raw
        assert "[REDACTED]" in raw
        posture_raw = client_.get("/api/system/posture", headers=SYS_ACTOR).text
        assert "leakedsecret" not in posture_raw and "10.0.0.9" not in posture_raw


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
        # Behavioral proof: a write verb against the surface is refused (405).
        for verb in (client_.post, client_.put, client_.patch, client_.delete):
            assert verb("/api/system/health", headers=SYS_ACTOR).status_code == 405
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
