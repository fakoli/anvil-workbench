"""Private tailnet API for the Workbench hub.

The browser only receives redacted data and never receives a model, GitHub, or
bridge credential.  An identity-aware tailnet proxy should set
``X-Workbench-Actor``; the development fallback is the configured owner.
"""
from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import Settings
from .graph import EvidenceGraph, Neo4jEvidenceGraph, NullGraph
from .models import as_json
from .retrieval import AnvilPurposeRetrieval
from .store import MemoryStore, PostgresStore, StoreError, WorkbenchStore


class ProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    state_root: str = Field(min_length=1, max_length=1024)


class BridgeInput(BaseModel):
    name: str = Field(min_length=1, max_length=160)


class RunInput(BaseModel):
    project_id: str
    task_id: str | None = None
    model: str = Field(min_length=1, max_length=240)


class ApprovalInput(BaseModel):
    project_id: str
    action_type: str = Field(pattern="^(commit_pr|merge_and_accept|state_apply|deploy|model_policy)$")
    payload: dict[str, Any]
    ttl_seconds: int = Field(default=900, ge=30, le=86_400)
    bridge_id: str | None = None


class BridgeEvent(BaseModel):
    run_id: str
    role: str = Field(min_length=1, max_length=80)
    content: Any


class EvidenceInput(BaseModel):
    source_kind: str = Field(pattern="^(state_event|work_packet|route|evaluation|pull_request|approval|failure)$")
    source_id: str = Field(min_length=1, max_length=300)
    project_id: str
    payload: dict[str, Any]


def _error(exc: StoreError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


def _store(settings: Settings) -> WorkbenchStore:
    store = PostgresStore(settings.database_url)
    store.initialize()
    return store


def _graph(settings: Settings) -> EvidenceGraph:
    if not settings.neo4j_password:
        return NullGraph()
    retrieval = None
    if settings.anvil_router_base_url and settings.anvil_router_token and settings.embedding_model:
        retrieval = AnvilPurposeRetrieval(
            settings.anvil_router_base_url, settings.anvil_router_token,
            settings.embedding_model, settings.rerank_model or None,
        )
    return Neo4jEvidenceGraph(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password, retrieval=retrieval)


def create_app(
    settings: Settings | None = None,
    store: WorkbenchStore | None = None,
    graph: EvidenceGraph | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    store = store or _store(settings)
    graph = graph or _graph(settings)
    app = FastAPI(title="Anvil Workbench", version="0.1.0", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.store = store
    app.state.graph = graph

    def actor(request: Request) -> str:
        name = (request.headers.get(settings.identity_header) or "").strip()
        if not name and settings.allow_insecure_dev_actor:
            name = settings.owner
        if not name:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="trusted tailnet identity is required")
        if name not in settings.approvers:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="actor is not allowlisted")
        return name

    def owner(current_actor: str = Depends(actor)) -> str:
        if current_actor != settings.owner:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="owner permission required")
        return current_actor

    def bridge_identity(
        request: Request,
        x_workbench_bridge: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> str:
        bridge_id = x_workbench_bridge or request.path_params.get("bridge_id")
        if not bridge_id or not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bridge authentication required")
        try:
            store.authenticate_bridge(bridge_id, authorization.removeprefix("Bearer "))
        except StoreError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
        return bridge_id

    @app.exception_handler(StoreError)
    async def store_error_handler(_: Request, exc: StoreError):
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})

    @app.get("/healthz")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "anvil-workbench", "graph": type(graph).__name__}

    @app.get("/api/bootstrap")
    def bootstrap(_: str = Depends(actor)) -> dict[str, Any]:
        projects = [as_json(project) for project in store.list_projects()]
        return {
            "projects": projects,
            "runs": [as_json(run) for run in store.list_runs()],
            "approvals": [as_json(approval) for approval in store.list_approvals()],
            "router_configured": bool(settings.anvil_router_base_url),
        }

    @app.post("/api/projects", status_code=status.HTTP_201_CREATED)
    def create_project(payload: ProjectInput, _: str = Depends(owner)) -> dict[str, Any]:
        return as_json(store.create_project(payload.name, payload.state_root))

    @app.post("/api/projects/{project_id}/bridges", status_code=status.HTTP_201_CREATED)
    def register_bridge(project_id: str, payload: BridgeInput, _: str = Depends(owner)) -> dict[str, Any]:
        bridge, token = store.register_bridge(project_id, payload.name)
        # Deliberately the single opportunity to retrieve the bridge secret.
        return {"bridge": as_json(bridge), "bootstrap_token": token}

    @app.post("/api/runs", status_code=status.HTTP_201_CREATED)
    def create_run(payload: RunInput, current_actor: str = Depends(actor)) -> dict[str, Any]:
        project = next((project for project in store.list_projects() if project.id == payload.project_id), None)
        if project is None or project.bridge_id is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="a project bridge is required before a run can start")
        run = store.create_run(payload.project_id, payload.task_id, payload.model)
        store.enqueue_run(project.bridge_id, run)
        store.append_audit("run.requested", current_actor, payload.project_id, {"run_id": run.id})
        return as_json(run)

    @app.post("/api/approvals", status_code=status.HTTP_201_CREATED)
    def request_approval(payload: ApprovalInput, current_actor: str = Depends(actor)) -> dict[str, Any]:
        approval = store.create_approval(
            payload.project_id, payload.action_type, payload.payload, current_actor,
            payload.ttl_seconds, payload.bridge_id,
        )
        return as_json(approval)

    @app.post("/api/approvals/{approval_id}/approve")
    def approve(approval_id: str, current_actor: str = Depends(actor)) -> dict[str, Any]:
        approval = store.approve(approval_id, current_actor, settings.approvers)
        if approval.bridge_id:
            store.enqueue_command(approval.bridge_id, approval)
        return as_json(approval)

    @app.get("/api/bridge/{bridge_id}/commands/next")
    def next_command(bridge_id: str, authenticated_bridge: str = Depends(bridge_identity)) -> dict[str, Any] | None:
        if bridge_id != authenticated_bridge:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge id mismatch")
        return store.next_command(bridge_id)

    @app.post("/api/bridge/{bridge_id}/approvals/{approval_id}/consume")
    def consume(
        bridge_id: str,
        approval_id: str,
        payload: dict[str, str],
        authenticated_bridge: str = Depends(bridge_identity),
    ) -> dict[str, Any]:
        if bridge_id != authenticated_bridge:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge id mismatch")
        approval = store.get_approval(approval_id)
        if approval.bridge_id != bridge_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="approval belongs to another bridge")
        return as_json(store.consume(approval_id, payload.get("payload_hash", "")))

    @app.post("/api/bridge/{bridge_id}/events", status_code=status.HTTP_202_ACCEPTED)
    def bridge_event(bridge_id: str, event: BridgeEvent, authenticated_bridge: str = Depends(bridge_identity)) -> dict[str, bool]:
        if bridge_id != authenticated_bridge:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge id mismatch")
        store.add_transcript(event.run_id, event.role, event.content)
        return {"accepted": True}

    @app.post("/api/bridge/{bridge_id}/evidence", status_code=status.HTTP_202_ACCEPTED)
    def project_evidence(bridge_id: str, evidence: EvidenceInput, authenticated_bridge: str = Depends(bridge_identity)) -> dict[str, Any]:
        if bridge_id != authenticated_bridge:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge id mismatch")
        citation = graph.project(evidence.source_kind, evidence.source_id, evidence.project_id, evidence.payload)
        store.append_audit("evidence.projected", "bridge:" + bridge_id, evidence.project_id, {"citation": citation, "source_kind": evidence.source_kind})
        return {"accepted": True, "citation": citation}

    @app.get("/api/evidence/search")
    def evidence_search(project_id: str, query: str, _: str = Depends(actor)) -> dict[str, Any]:
        return {"results": graph.evidence_search(project_id, query)}

    @app.get("/api/tasks/{task_id}/lineage")
    def task_lineage(task_id: str, _: str = Depends(actor)) -> dict[str, Any]:
        return {"task_id": task_id, "lineage": graph.task_lineage(task_id)}

    @app.get("/api/failures/related")
    def related_failures(fingerprint: str, _: str = Depends(actor)) -> dict[str, Any]:
        return {"results": graph.related_failures(fingerprint)}

    return app
