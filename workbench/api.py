"""Private tailnet API for the Workbench hub.

The browser only receives redacted data and never receives a model, GitHub, or
bridge credential.  An identity-aware tailnet proxy should set
``X-Workbench-Actor``; the development fallback is the configured owner.
"""
from __future__ import annotations

import hashlib
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import Settings
from .conversation_api import build_conversation_router, register_conversation_handlers
from .conversation_store import ConversationStore, MemoryConversationStore
from .idempotency_store import IdempotencyStore, MemoryIdempotencyStore
from .graph import EvidenceGraph, Neo4jEvidenceGraph, NullGraph
from .models import as_json
from .retrieval import AnvilPurposeRetrieval
from .router import RouterError, route_decisions, sandbox_response
from .store import PostgresStore, StoreError, WorkbenchStore
from .voice import relay_realtime


def default_delivery_workflow(skills: list[str] | None = None) -> dict[str, Any]:
    """Small reviewed workflow used when a session does not supply a template."""
    return {
        "entry": "implement",
        "steps": [
            {"id": "implement", "kind": "agent", "model": "planning", "skills": skills or [], "next": ["review"]},
            {"id": "review", "kind": "approval_wait", "next": ["reconcile"]},
            {"id": "reconcile", "kind": "reconcile", "next": []},
        ],
    }


class ProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    state_root: str = Field(min_length=1, max_length=1024)


class BridgeInput(BaseModel):
    name: str = Field(min_length=1, max_length=160)


class RunInput(BaseModel):
    project_id: str
    task_id: str | None = None
    model: str = Field(min_length=1, max_length=240)


class SessionInput(BaseModel):
    project_id: str
    title: str = Field(min_length=1, max_length=160)
    worktree_id: str = Field(min_length=1, max_length=160)
    workflow_definition: dict[str, Any] | None = None
    skills: list[str] = Field(default_factory=list, max_length=16)


class WorkflowRevisionInput(BaseModel):
    expected_version: int = Field(ge=1)
    definition: dict[str, Any]


class WorkflowStartInput(BaseModel):
    task_id: str = Field(min_length=1, max_length=300)
    model: str = Field(default="planning", min_length=1, max_length=240)


class WorkflowStepInput(BaseModel):
    outcome: str = Field(pattern="^(succeeded|failed|cancelled)$")


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


class RunStatusInput(BaseModel):
    status: str = Field(pattern="^(running|reconciliation)$")


class RunFinalizationInput(BaseModel):
    status: str = Field(pattern="^(evidenced|reconciliation)$")
    command_id: str = Field(min_length=1, max_length=300)


class EvidenceInput(BaseModel):
    source_kind: str = Field(pattern="^(state_event|work_packet|route|evaluation|pull_request|approval|failure)$")
    source_id: str = Field(min_length=1, max_length=300)
    project_id: str
    payload: dict[str, Any]


class DirectiveInput(BaseModel):
    content: str = Field(min_length=1, max_length=8_000)


class BridgeSkillInput(BaseModel):
    skill_id: str = Field(pattern="^[a-zA-Z0-9][a-zA-Z0-9:_-]{0,119}$")
    description: str = Field(min_length=1, max_length=500)
    content_sha256: str = Field(pattern="^[a-f0-9]{64}$")


class BridgeSkillsInput(BaseModel):
    skills: list[BridgeSkillInput] = Field(default_factory=list, max_length=128)


class SandboxInput(BaseModel):
    model: str = Field(min_length=1, max_length=240)
    input: str = Field(min_length=1, max_length=8_000)


def _error(exc: StoreError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


def _store(settings: Settings) -> WorkbenchStore:
    store = PostgresStore(settings.database_url)
    store.initialize()
    return store


def _conversation_store(settings: Settings) -> ConversationStore | None:
    """Build chat persistence only when the hub holds the content-hash key.

    The key is hub configuration (``WORKBENCH_CHAT_HASH_KEY``); it is passed
    to the store constructor and held on the instance only, never persisted
    with the rows.  Without a key there is no store and the chat endpoints
    fail closed with 503.  A configured-but-invalid key raises loudly here
    instead of serving unkeyed fingerprints.
    """
    if not settings.chat_content_hash_key:
        return None
    return MemoryConversationStore(
        content_hash_key=settings.chat_content_hash_key.encode("utf-8"), recover_on_open=True,
    )


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
    conversation_store: ConversationStore | None = None,
    idempotency_store: IdempotencyStore | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    store = store or _store(settings)
    graph = graph or _graph(settings)
    conversation_store = conversation_store or _conversation_store(settings)
    idempotency_store = idempotency_store or MemoryIdempotencyStore()
    app = FastAPI(title="Anvil Workbench", version="0.1.0", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.store = store
    app.state.graph = graph
    app.state.conversation_store = conversation_store
    app.state.idempotency_store = idempotency_store

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

    # Actor-scoped chat surface (chat-first-voice T002.4): identity comes from
    # the same trusted ``actor`` dependency; the store enforces ownership.
    register_conversation_handlers(app)
    app.include_router(build_conversation_router(actor, conversation_store, idempotency_store))

    @app.get("/healthz")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "anvil-workbench", "graph": type(graph).__name__}

    @app.get("/api/bootstrap")
    def bootstrap(current_actor: str = Depends(actor)) -> dict[str, Any]:
        projects = [as_json(project) for project in store.list_projects()]
        sessions = store.list_sessions()
        directives = [
            as_json(event) for session in sessions for event in store.list_workflow_events(session.id)
            if event.kind == "operator.directive"
        ]
        return {
            "actor": current_actor,
            "projects": projects,
            "runs": [as_json(run) for run in store.list_runs()],
            "sessions": [as_json(session) for session in sessions],
            "workflows": [as_json(workflow) for workflow in store.list_workflows()],
            "approvals": [as_json(approval) for approval in store.list_approvals()],
            "skills": [as_json(skill) for skill in store.list_bridge_skills()],
            "directives": directives[-100:],
            "audit": [as_json(event) for event in store.list_audit()],
            "router_configured": bool(settings.anvil_router_base_url and settings.anvil_router_token),
            "sandbox": {"available": bool(settings.anvil_router_base_url and settings.anvil_router_token and settings.sandbox_models), "models": sorted(settings.sandbox_models)},
            "voice": {
                "available": bool(settings.anvil_voice_realtime_url),
                "transport": "workbench-realtime-relay" if settings.anvil_voice_realtime_url else "not_configured",
                "retains_transcripts": settings.voice_retain_transcripts,
            },
        }

    @app.post("/api/projects", status_code=status.HTTP_201_CREATED)
    def create_project(payload: ProjectInput, _: str = Depends(owner)) -> dict[str, Any]:
        return as_json(store.create_project(payload.name, payload.state_root))

    @app.post("/api/projects/{project_id}/bridges", status_code=status.HTTP_201_CREATED)
    def register_bridge(project_id: str, payload: BridgeInput, _: str = Depends(owner)) -> dict[str, Any]:
        bridge, token = store.register_bridge(project_id, payload.name)
        # Deliberately the single opportunity to retrieve the bridge secret.
        return {"bridge": as_json(bridge), "bootstrap_token": token}

    @app.post("/api/sessions", status_code=status.HTTP_201_CREATED)
    def create_session(payload: SessionInput, current_actor: str = Depends(actor)) -> dict[str, Any]:
        session, workflow = store.create_session(
            payload.project_id, payload.title, payload.worktree_id,
            payload.workflow_definition or default_delivery_workflow(payload.skills),
        )
        store.append_audit("session.requested", current_actor, payload.project_id, {
            "session_id": session.id, "workflow_id": workflow.id,
        })
        return {"session": as_json(session), "workflow": as_json(workflow)}

    @app.post("/api/sessions/{session_id}/directives", status_code=status.HTTP_202_ACCEPTED)
    def add_directive(session_id: str, payload: DirectiveInput, current_actor: str = Depends(actor)) -> dict[str, Any]:
        session = store.get_session(session_id)
        workflow = next(iter(store.list_workflows(session_id)), None)
        event = store.record_session_event(session.id, workflow.id if workflow else None, "operator.directive", {
            "content": payload.content.strip(), "actor": current_actor,
            "delivery": "included in the next bridge work packet for this session",
        })
        store.append_audit("session.directive_added", current_actor, session.project_id, {"session_id": session.id, "event_id": event.id})
        return as_json(event)

    @app.get("/api/sessions/{session_id}/events")
    def session_events(session_id: str, after_sequence: int = 0, _: str = Depends(actor)) -> dict[str, Any]:
        return {"events": [as_json(event) for event in store.list_workflow_events(session_id, max(after_sequence, 0))]}

    @app.post("/api/workflows/{workflow_id}/revise")
    def revise_workflow(workflow_id: str, payload: WorkflowRevisionInput, current_actor: str = Depends(actor)) -> dict[str, Any]:
        return as_json(store.revise_workflow(workflow_id, payload.expected_version, payload.definition, current_actor))

    @app.post("/api/workflows/{workflow_id}/start", status_code=status.HTTP_201_CREATED)
    def start_workflow(workflow_id: str, payload: WorkflowStartInput, current_actor: str = Depends(actor)) -> dict[str, Any]:
        started, run = store.start_workflow_run(
            workflow_id, payload.task_id, payload.model, current_actor,
        )
        return {"workflow": as_json(started), "run": as_json(run)}

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

    @app.post("/api/projects/{project_id}/skills/probe", status_code=status.HTTP_202_ACCEPTED)
    def probe_skills(project_id: str, current_actor: str = Depends(actor)) -> dict[str, Any]:
        project = next((item for item in store.list_projects() if item.id == project_id), None)
        if project is None or project.bridge_id is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="a project bridge is required before skills can be checked")
        store.enqueue_skill_probe(project.bridge_id)
        store.append_audit("bridge.skills_probe_requested", current_actor, project.id, {"bridge_id": project.bridge_id})
        return {"accepted": True, "bridge_id": project.bridge_id}

    @app.get("/api/bridge/{bridge_id}/commands/next")
    def next_command(bridge_id: str, authenticated_bridge: str = Depends(bridge_identity)) -> dict[str, Any] | None:
        if bridge_id != authenticated_bridge:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge id mismatch")
        return store.next_command(bridge_id)

    @app.post("/api/bridge/{bridge_id}/skills", status_code=status.HTTP_202_ACCEPTED)
    def publish_bridge_skills(
        bridge_id: str, payload: BridgeSkillsInput, authenticated_bridge: str = Depends(bridge_identity),
    ) -> dict[str, Any]:
        if bridge_id != authenticated_bridge:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge id mismatch")
        skills = store.replace_bridge_skills(bridge_id, [item.model_dump() for item in payload.skills])
        return {"skills": [as_json(skill) for skill in skills]}

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

    @app.post("/api/bridge/{bridge_id}/approvals/{approval_id}/consume-for-run")
    def consume_for_run(
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
        return as_json(store.consume_approval_for_run(approval_id, payload.get("payload_hash", ""), bridge_id))

    @app.post("/api/bridge/{bridge_id}/approvals/{approval_id}/complete-merge")
    def complete_merge(
        bridge_id: str,
        approval_id: str,
        payload: dict[str, str],
        authenticated_bridge: str = Depends(bridge_identity),
    ) -> dict[str, Any]:
        if bridge_id != authenticated_bridge:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge id mismatch")
        command_id = payload.get("command_id", "")
        if not command_id:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="command id is required for merge completion")
        return as_json(store.complete_approved_merge(
            approval_id, payload.get("payload_hash", ""), bridge_id, command_id,
        ))

    @app.post("/api/bridge/{bridge_id}/events", status_code=status.HTTP_202_ACCEPTED)
    def bridge_event(bridge_id: str, event: BridgeEvent, authenticated_bridge: str = Depends(bridge_identity)) -> dict[str, bool]:
        if bridge_id != authenticated_bridge:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge id mismatch")
        store.add_transcript(event.run_id, event.role, event.content, bridge_id)
        return {"accepted": True}

    @app.post("/api/bridge/{bridge_id}/commands/{command_id}/ack", status_code=status.HTTP_202_ACCEPTED)
    def acknowledge_bridge_command(
        bridge_id: str, command_id: str, authenticated_bridge: str = Depends(bridge_identity),
    ) -> dict[str, bool]:
        if bridge_id != authenticated_bridge:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge id mismatch")
        store.acknowledge_command(bridge_id, command_id)
        return {"acknowledged": True}

    @app.post("/api/bridge/{bridge_id}/runs/{run_id}/status")
    def bridge_run_status(
        bridge_id: str, run_id: str, payload: RunStatusInput,
        authenticated_bridge: str = Depends(bridge_identity),
    ) -> dict[str, Any]:
        if bridge_id != authenticated_bridge:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge id mismatch")
        return as_json(store.update_run_status(run_id, payload.status, bridge_id))

    @app.post("/api/bridge/{bridge_id}/runs/{run_id}/finalize")
    def finalize_bridge_run(
        bridge_id: str, run_id: str, payload: RunFinalizationInput,
        authenticated_bridge: str = Depends(bridge_identity),
    ) -> dict[str, Any]:
        if bridge_id != authenticated_bridge:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge id mismatch")
        return as_json(store.finalize_run_command(
            run_id, payload.status, bridge_id, payload.command_id,
        ))

    @app.get("/api/bridge/{bridge_id}/runs/{run_id}/lease")
    def bridge_run_lease(
        bridge_id: str, run_id: str,
        authenticated_bridge: str = Depends(bridge_identity),
    ) -> dict[str, Any]:
        if bridge_id != authenticated_bridge:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge id mismatch")
        run = store.validate_run_lease(run_id, bridge_id)
        if run.session_id is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="approved delivery actions require a session-bound run")
        session = store.get_session(run.session_id)
        return {
            "run_id": run.id,
            "session_id": session.id,
            "worktree_id": session.worktree_id,
            "lease_epoch": run.lease_epoch,
        }

    @app.post("/api/bridge/{bridge_id}/runs/{run_id}/lease/renew")
    def renew_bridge_run_lease(
        bridge_id: str, run_id: str,
        authenticated_bridge: str = Depends(bridge_identity),
    ) -> dict[str, Any]:
        if bridge_id != authenticated_bridge:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge id mismatch")
        return as_json(store.renew_run_lease(run_id, bridge_id))

    @app.post("/api/bridge/{bridge_id}/runs/{run_id}/lease/release")
    def release_bridge_run_lease(
        bridge_id: str, run_id: str,
        authenticated_bridge: str = Depends(bridge_identity),
    ) -> dict[str, Any]:
        if bridge_id != authenticated_bridge:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge id mismatch")
        return as_json(store.release_run_lease(run_id, bridge_id))

    @app.post("/api/bridge/{bridge_id}/evidence", status_code=status.HTTP_202_ACCEPTED)
    def project_evidence(bridge_id: str, evidence: EvidenceInput, authenticated_bridge: str = Depends(bridge_identity)) -> dict[str, Any]:
        if bridge_id != authenticated_bridge:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge id mismatch")
        project = next((item for item in store.list_projects() if item.id == evidence.project_id), None)
        if project is None or project.bridge_id != bridge_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge does not own this project")
        citation = graph.project(evidence.source_kind, evidence.source_id, evidence.project_id, evidence.payload)
        store.append_audit("evidence.projected", "bridge:" + bridge_id, evidence.project_id, {"citation": citation, "source_kind": evidence.source_kind})
        return {"accepted": True, "citation": citation}

    @app.get("/api/routes")
    def routes(limit: int = 50, _: str = Depends(actor)) -> dict[str, Any]:
        try:
            rows = route_decisions(settings.anvil_router_base_url, settings.anvil_router_token, limit)
        except RouterError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        known_runs = {run.id for run in store.list_runs()}
        return {"routes": [row for row in rows if row.get("workbench_run_id") in known_runs]}

    @app.post("/api/sandbox")
    def sandbox(payload: SandboxInput, current_actor: str = Depends(actor)) -> dict[str, Any]:
        if payload.model not in settings.sandbox_models:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="the requested sandbox model is not allowed")
        try:
            response = sandbox_response(settings.anvil_router_base_url, settings.anvil_router_token, payload.model, payload.input)
        except RouterError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        store.append_audit("sandbox.completed", current_actor, None, {
            "model": payload.model,
            "input_sha256": hashlib.sha256(payload.input.encode("utf-8")).hexdigest(),
            "output_characters": len(str(response.get("output_text", ""))),
        })
        return response

    @app.post("/api/bridge/{bridge_id}/workflows/{workflow_id}/steps/{step_id}")
    def bridge_workflow_step(
        bridge_id: str, workflow_id: str, step_id: str, payload: WorkflowStepInput,
        authenticated_bridge: str = Depends(bridge_identity),
    ) -> dict[str, Any]:
        if bridge_id != authenticated_bridge:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge id mismatch")
        workflow = store.get_workflow(workflow_id)
        project = next((item for item in store.list_projects() if item.id == workflow.project_id), None)
        if project is None or project.bridge_id != bridge_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge does not own this workflow")
        return as_json(store.complete_workflow_step(workflow_id, step_id, payload.outcome, "bridge:" + bridge_id))

    @app.get("/api/evidence/search")
    def evidence_search(project_id: str, query: str, _: str = Depends(actor)) -> dict[str, Any]:
        return {"results": graph.evidence_search(project_id, query)}

    @app.get("/api/tasks/{task_id}/lineage")
    def task_lineage(task_id: str, _: str = Depends(actor)) -> dict[str, Any]:
        return {"task_id": task_id, "lineage": graph.task_lineage(task_id)}

    @app.get("/api/failures/related")
    def related_failures(fingerprint: str, _: str = Depends(actor)) -> dict[str, Any]:
        return {"results": graph.related_failures(fingerprint)}

    @app.websocket("/api/sessions/{session_id}/voice/realtime")
    async def session_voice(session_id: str, websocket: WebSocket) -> None:
        current_actor = (websocket.headers.get(settings.identity_header) or "").strip()
        if not current_actor and settings.allow_insecure_dev_actor:
            current_actor = settings.owner
        if not current_actor or current_actor not in settings.approvers:
            await websocket.close(code=1008)
            return
        if not settings.anvil_voice_realtime_url:
            await websocket.close(code=1013)
            return
        try:
            session = store.get_session(session_id)
        except StoreError:
            await websocket.close(code=1008)
            return
        workflow = next(iter(store.list_workflows(session_id)), None)

        async def record(kind: str, data: dict[str, Any]) -> None:
            store.record_session_event(session.id, workflow.id if workflow else None, kind, data)

        await relay_realtime(
            websocket, settings.anvil_voice_realtime_url, settings.anvil_voice_realtime_token,
            record, settings.voice_retain_transcripts,
        )

    return app
