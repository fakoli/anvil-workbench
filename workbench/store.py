"""Workbench operational store.

Postgres is the production source for Workbench-owned records.  The in-memory
implementation exists only for hermetic tests and local API smoke tests; Anvil
State remains the source of truth for delivery state in every mode.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import replace
from datetime import timedelta
from typing import Any, Protocol

from .models import (
    Approval, AuditEvent, Bridge, BridgeSkill, Project,
    ResourceLease, Run, Session, Workflow, WorkflowEvent,
    new_id, now_utc,
)
from .redaction import redact_value
from .store_base import StoreError
from .workflows import WorkflowError, advance_cursor, step_by_id, validate_definition


_GIT_HEAD_SHA = re.compile(r"^[0-9a-f]{40,64}$")


_RUN_STATUS_TRANSITIONS = {
    "queued": frozenset({"running", "reconciliation"}),
    "running": frozenset({"evidenced", "reconciliation"}),
    # Evidence proves the implementation phase only.  A delivery is complete
    # only after the bridge has observed merge and State acceptance.
    "evidenced": frozenset({"reconciliation"}),
    "completed": frozenset(),
    "reconciliation": frozenset(),
}


def payload_hash(payload: dict[str, Any]) -> str:
    """Stable hash bound to both approval and the bridge-side action."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class WorkbenchStore(Protocol):
    def create_project(self, name: str, state_root: str) -> Project: ...
    def list_projects(self) -> list[Project]: ...
    def list_runs(self, project_id: str | None = None) -> list[Run]: ...
    def list_audit(self, limit: int = 20) -> list[AuditEvent]: ...
    def create_session(self, project_id: str, title: str, worktree_id: str, workflow_definition: dict[str, Any]) -> tuple[Session, Workflow]: ...
    def list_sessions(self, project_id: str | None = None) -> list[Session]: ...
    def get_session(self, session_id: str) -> Session: ...
    def get_workflow(self, workflow_id: str) -> Workflow: ...
    def list_workflows(self, session_id: str | None = None) -> list[Workflow]: ...
    def list_workflow_events(self, session_id: str, after_sequence: int = 0) -> list[WorkflowEvent]: ...
    def record_session_event(self, session_id: str, workflow_id: str | None, kind: str, data: dict[str, Any]) -> WorkflowEvent: ...
    def revise_workflow(self, workflow_id: str, expected_version: int, definition: dict[str, Any], actor: str) -> Workflow: ...
    def start_workflow(self, workflow_id: str, actor: str) -> Workflow: ...
    def start_workflow_run(self, workflow_id: str, task_id: str, model: str, actor: str) -> tuple[Workflow, Run]: ...
    def complete_workflow_step(self, workflow_id: str, step_id: str, outcome: str, actor: str) -> Workflow: ...
    def acquire_lease(self, resource_key: str, session_id: str, ttl_seconds: int) -> ResourceLease: ...
    def validate_run_lease(self, run_id: str, bridge_id: str) -> Run: ...
    def renew_run_lease(self, run_id: str, bridge_id: str, ttl_seconds: int = 300) -> Run: ...
    def release_run_lease(self, run_id: str, bridge_id: str) -> Run: ...
    def list_approvals(self, project_id: str | None = None) -> list[Approval]: ...
    def register_bridge(self, project_id: str, name: str) -> tuple[Bridge, str]: ...
    def authenticate_bridge(self, bridge_id: str, token: str) -> Bridge: ...
    def replace_bridge_skills(self, bridge_id: str, skills: list[dict[str, str]]) -> list[BridgeSkill]: ...
    def list_bridge_skills(self, project_id: str | None = None) -> list[BridgeSkill]: ...
    def create_run(self, project_id: str, task_id: str | None, model: str, session_id: str | None = None, workflow_id: str | None = None, workflow_step_id: str | None = None, lease_epoch: int | None = None) -> Run: ...
    def update_run_status(self, run_id: str, status: str, bridge_id: str) -> Run: ...
    def finalize_run_command(self, run_id: str, status: str, bridge_id: str, command_id: str) -> Run: ...
    def add_transcript(self, run_id: str, role: str, content: Any, bridge_id: str | None = None) -> None: ...
    def create_approval(self, project_id: str, action_type: str, payload: dict[str, Any], requested_by: str, ttl_seconds: int, bridge_id: str | None) -> Approval: ...
    def get_approval(self, approval_id: str) -> Approval: ...
    def approve(self, approval_id: str, actor: str, approvers: frozenset[str]) -> Approval: ...
    def consume(self, approval_id: str, action_payload_hash: str) -> Approval: ...
    def consume_approval_for_run(self, approval_id: str, action_payload_hash: str, bridge_id: str) -> Approval: ...
    def complete_approved_merge(self, approval_id: str, action_payload_hash: str, bridge_id: str, command_id: str | None = None) -> Run: ...
    def enqueue_command(self, bridge_id: str, approval: Approval) -> None: ...
    def enqueue_run(self, bridge_id: str, run: Run) -> None: ...
    def enqueue_skill_probe(self, bridge_id: str) -> None: ...
    def next_command(self, bridge_id: str) -> dict[str, Any] | None: ...
    def acknowledge_command(self, bridge_id: str, command_id: str) -> None: ...
    def append_audit(self, kind: str, actor: str, project_id: str | None, data: dict[str, Any]) -> AuditEvent: ...


class MemoryStore:
    """Small, lock-free test store; API requests are serialized in tests."""

    def __init__(self) -> None:
        self.projects: dict[str, Project] = {}
        self.bridges: dict[str, Bridge] = {}
        self.bridge_skills: dict[str, dict[str, BridgeSkill]] = {}
        self.runs: dict[str, Run] = {}
        self.sessions: dict[str, Session] = {}
        self.workflows: dict[str, Workflow] = {}
        self.workflow_events: dict[str, list[WorkflowEvent]] = {}
        self.leases: dict[str, ResourceLease] = {}
        self.transcripts: dict[str, list[dict[str, Any]]] = {}
        self.approvals: dict[str, Approval] = {}
        self.commands: dict[str, list[dict[str, Any]]] = {}
        self.audit: list[AuditEvent] = []

    def append_audit(self, kind: str, actor: str, project_id: str | None, data: dict[str, Any]) -> AuditEvent:
        event = AuditEvent(new_id("audit"), kind, actor, project_id, redact_value(data))
        self.audit.append(event)
        return event

    def create_project(self, name: str, state_root: str) -> Project:
        project = Project(new_id("project"), name.strip(), state_root.strip())
        self.projects[project.id] = project
        self.append_audit("project.created", "owner", project.id, {"name": project.name})
        return project

    def list_projects(self) -> list[Project]:
        return list(self.projects.values())

    def list_runs(self, project_id: str | None = None) -> list[Run]:
        values = list(self.runs.values())
        return [run for run in values if project_id is None or run.project_id == project_id]

    def list_audit(self, limit: int = 20) -> list[AuditEvent]:
        return list(reversed(self.audit[-max(1, min(limit, 100)):]))

    def create_session(
        self, project_id: str, title: str, worktree_id: str, workflow_definition: dict[str, Any],
    ) -> tuple[Session, Workflow]:
        if project_id not in self.projects:
            raise StoreError("unknown project")
        try:
            definition = validate_definition(workflow_definition)
        except WorkflowError as exc:
            raise StoreError(str(exc)) from exc
        session = Session(new_id("session"), project_id, title.strip(), worktree_id.strip())
        workflow = Workflow(new_id("workflow"), project_id, session.id, 1, definition)
        self.sessions[session.id] = session
        self.workflows[workflow.id] = workflow
        self.workflow_events[session.id] = []
        self._append_workflow_event(session.id, workflow.id, "session.created", {"title": session.title, "worktree_id": session.worktree_id})
        self._append_workflow_event(session.id, workflow.id, "workflow.created", {"workflow_id": workflow.id, "version": workflow.version})
        self.append_audit("session.created", "operator", project_id, {"session_id": session.id, "workflow_id": workflow.id, "worktree_id": session.worktree_id})
        return session, workflow

    def list_sessions(self, project_id: str | None = None) -> list[Session]:
        values = sorted(self.sessions.values(), key=lambda session: session.updated_at, reverse=True)
        return [session for session in values if project_id is None or session.project_id == project_id]

    def get_session(self, session_id: str) -> Session:
        try:
            return self.sessions[session_id]
        except KeyError as exc:
            raise StoreError("unknown session") from exc

    def get_workflow(self, workflow_id: str) -> Workflow:
        try:
            return self.workflows[workflow_id]
        except KeyError as exc:
            raise StoreError("unknown workflow") from exc

    def list_workflows(self, session_id: str | None = None) -> list[Workflow]:
        values = sorted(self.workflows.values(), key=lambda workflow: workflow.updated_at, reverse=True)
        return [workflow for workflow in values if session_id is None or workflow.session_id == session_id]

    def _append_workflow_event(self, session_id: str, workflow_id: str | None, kind: str, data: dict[str, Any]) -> WorkflowEvent:
        events = self.workflow_events.setdefault(session_id, [])
        event = WorkflowEvent(new_id("event"), session_id, workflow_id, len(events) + 1, kind, redact_value(data))
        events.append(event)
        return event

    def list_workflow_events(self, session_id: str, after_sequence: int = 0) -> list[WorkflowEvent]:
        self.get_session(session_id)
        return [event for event in self.workflow_events.get(session_id, []) if event.sequence > after_sequence]

    def record_session_event(self, session_id: str, workflow_id: str | None, kind: str, data: dict[str, Any]) -> WorkflowEvent:
        self.get_session(session_id)
        return self._append_workflow_event(session_id, workflow_id, kind, data)

    def revise_workflow(self, workflow_id: str, expected_version: int, definition: dict[str, Any], actor: str) -> Workflow:
        workflow = self.get_workflow(workflow_id)
        if workflow.status != "draft":
            raise StoreError("running workflows are version-pinned and cannot be reinterpreted")
        if workflow.version != expected_version:
            raise StoreError("workflow version changed; reload before revising")
        try:
            clean = validate_definition(definition)
        except WorkflowError as exc:
            raise StoreError(str(exc)) from exc
        updated = replace(workflow, version=workflow.version + 1, definition=clean, updated_at=now_utc())
        self.workflows[workflow_id] = updated
        self._append_workflow_event(workflow.session_id, workflow.id, "workflow.revised", {"version": updated.version, "actor": actor})
        return updated

    def start_workflow(self, workflow_id: str, actor: str) -> Workflow:
        workflow = self.get_workflow(workflow_id)
        if workflow.status != "draft":
            raise StoreError("workflow is not a draft")
        entry = str(workflow.definition["entry"])
        updated = replace(workflow, status="running", cursor=(entry,), updated_at=now_utc())
        self.workflows[workflow_id] = updated
        self._append_workflow_event(workflow.session_id, workflow.id, "workflow.started", {"step_id": entry, "actor": actor})
        return updated

    def start_workflow_run(
        self, workflow_id: str, task_id: str, model: str, actor: str,
    ) -> tuple[Workflow, Run]:
        """Start a draft and enqueue its first run as one memory-store mutation."""
        workflow = self.get_workflow(workflow_id)
        if workflow.status != "draft":
            raise StoreError("workflow is not a draft")
        project = self.projects.get(workflow.project_id)
        if project is None or project.bridge_id is None or project.bridge_id not in self.bridges:
            raise StoreError("a project bridge is required before a workflow can start")
        session = self.get_session(workflow.session_id)
        if session.status != "active":
            raise StoreError("session is not active for this project")
        entry = str(workflow.definition["entry"])
        step = step_by_id(workflow.definition, entry)
        if step["kind"] != "agent":
            raise StoreError("v1 workflows must begin with an agent step")
        published = self.bridge_skills.get(project.bridge_id, {})
        skills: list[dict[str, str]] = []
        for skill_id in step.get("skills", []):
            skill = published.get(skill_id)
            if skill is None:
                raise StoreError(
                    "the bridge must publish every selected workflow skill before the workflow can start: "
                    + skill_id
                )
            skills.append({
                "skill_id": skill.skill_id,
                "description": skill.description,
                "content_sha256": skill.content_sha256,
            })
        if any(
            run.session_id == session.id and run.status in {"queued", "running"}
            for run in self.runs.values()
        ):
            raise StoreError("a session may have only one active run")
        resource_key = "worktree:" + session.worktree_id
        now = now_utc()
        existing = self.leases.get(resource_key)
        if existing is not None and existing.expires_at > now and existing.session_id != session.id:
            raise StoreError("resource is leased by another active session")
        lease = ResourceLease(
            resource_key, session.id, (existing.epoch + 1) if existing else 1,
            now + timedelta(seconds=300), now,
        )
        selected_model = str(step.get("model") or model).strip()
        run = Run(
            new_id("run"), workflow.project_id, task_id, selected_model, "queued",
            session_id=session.id, workflow_id=workflow.id,
            workflow_step_id=entry, lease_epoch=lease.epoch,
        )
        directive_events = [
            event
            for event in self.workflow_events.get(session.id, [])
            if event.kind == "operator.directive" and isinstance(event.data.get("content"), str)
        ]
        # The packet carries only the trailing window of directives; the highest
        # included sequence is the packet inclusion high-water mark recorded below
        # so ``session_directive_view`` reports the truthful pending/included split.
        included_directives = directive_events[-32:]
        directives = [str(event.data["content"]) for event in included_directives]
        directive_high_water = included_directives[-1].sequence if included_directives else 0
        command_payload = {
            "run_id": run.id, "project_id": run.project_id, "task_id": run.task_id,
            "model": run.model, "session_id": run.session_id,
            "workflow_id": run.workflow_id, "workflow_step_id": run.workflow_step_id,
            "lease_epoch": run.lease_epoch, "worktree_id": session.worktree_id,
            "skills": skills, "directives": directives,
        }
        started = replace(workflow, status="running", cursor=(entry,), updated_at=now)

        # Every fallible validation above precedes this mutation block.
        self.leases[resource_key] = lease
        self.runs[run.id] = run
        self.transcripts[run.id] = []
        self.workflows[workflow.id] = started
        self.commands[project.bridge_id].append({
            "id": new_id("command"), "approval_id": None, "action_type": "run_codex",
            "payload": command_payload, "payload_hash": payload_hash(command_payload),
        })
        self._append_workflow_event(session.id, workflow.id, "lease.acquired", {
            "resource_key": resource_key, "epoch": lease.epoch,
        })
        self._append_workflow_event(session.id, workflow.id, "workflow.started", {
            "step_id": entry, "actor": actor, "run_id": run.id,
        })
        # Record the packet-inclusion marker (operator.directive_packet) naming the
        # highest directive sequence carried in this queued run_codex payload, so a
        # directive already in the packet is reported INCLUDED, not pending forever.
        if directive_high_water > 0:
            self._append_workflow_event(session.id, workflow.id, "operator.directive_packet", {
                "included_up_to_sequence": directive_high_water,
            })
        self.append_audit("run.created", actor, workflow.project_id, {
            "run_id": run.id, "task_id": task_id, "model": run.model,
            "session_id": session.id, "workflow_id": workflow.id,
            "lease_epoch": lease.epoch,
        })
        self.append_audit("workflow.run_requested", actor, workflow.project_id, {
            "workflow_id": workflow.id, "step_id": entry, "run_id": run.id,
        })
        return started, run

    def complete_workflow_step(self, workflow_id: str, step_id: str, outcome: str, actor: str) -> Workflow:
        workflow = self.get_workflow(workflow_id)
        if workflow.status not in {"running", "waiting_approval"} or step_id not in workflow.cursor:
            raise StoreError("workflow step is not runnable")
        if outcome not in {"succeeded", "failed", "cancelled"}:
            raise StoreError("workflow outcome is invalid")
        if outcome == "failed":
            status_value, cursor = "reconciliation", ()
        elif outcome == "cancelled":
            status_value, cursor = "cancelled", ()
        else:
            cursor = advance_cursor(workflow.definition, workflow.cursor, step_id)
            next_kinds = {step_by_id(workflow.definition, next_step)["kind"] for next_step in cursor}
            status_value = "completed" if not cursor else "waiting_approval" if "approval_wait" in next_kinds else "reconciliation" if "reconcile" in next_kinds else "running"
        updated = replace(workflow, status=status_value, cursor=tuple(cursor), updated_at=now_utc())
        self.workflows[workflow_id] = updated
        self._append_workflow_event(workflow.session_id, workflow.id, "workflow.step.finished", {"step_id": step_id, "outcome": outcome, "next": list(cursor), "actor": actor})
        return updated

    def acquire_lease(self, resource_key: str, session_id: str, ttl_seconds: int) -> ResourceLease:
        session = self.get_session(session_id)
        now = now_utc()
        existing = self.leases.get(resource_key)
        if existing is not None and existing.expires_at > now and existing.session_id != session.id:
            raise StoreError("resource is leased by another active session")
        epoch = (existing.epoch + 1) if existing is not None else 1
        lease = ResourceLease(resource_key, session.id, epoch, now + timedelta(seconds=ttl_seconds))
        self.leases[resource_key] = lease
        self._append_workflow_event(session.id, None, "lease.acquired", {"resource_key": resource_key, "epoch": epoch})
        return lease

    def validate_run_lease(self, run_id: str, bridge_id: str) -> Run:
        """Fence a queued session run before its bridge can touch a worktree."""
        run = self.runs.get(run_id)
        if run is None:
            raise StoreError("unknown run")
        project = self.projects.get(run.project_id)
        if project is None or project.bridge_id != bridge_id:
            raise StoreError("bridge does not own this run")
        if run.session_id is None:
            return run
        session = self.get_session(run.session_id)
        lease = self.leases.get("worktree:" + session.worktree_id)
        if (
            lease is None
            or lease.expires_at <= now_utc()
            or lease.session_id != session.id
            or lease.epoch != run.lease_epoch
        ):
            raise StoreError("run worktree lease is stale or no longer owned by its session")
        return run

    def renew_run_lease(self, run_id: str, bridge_id: str, ttl_seconds: int = 300) -> Run:
        run = self.validate_run_lease(run_id, bridge_id)
        if run.session_id is None:
            return run
        session = self.get_session(run.session_id)
        resource_key = "worktree:" + session.worktree_id
        lease = self.leases[resource_key]
        renewed = replace(lease, expires_at=now_utc() + timedelta(seconds=ttl_seconds))
        self.leases[resource_key] = renewed
        self._append_workflow_event(session.id, run.workflow_id, "lease.renewed", {
            "resource_key": resource_key, "epoch": renewed.epoch,
        })
        return run

    def release_run_lease(self, run_id: str, bridge_id: str) -> Run:
        run = self.validate_run_lease(run_id, bridge_id)
        if run.session_id is None:
            return run
        session = self.get_session(run.session_id)
        resource_key = "worktree:" + session.worktree_id
        lease = self.leases.get(resource_key)
        if lease is not None and lease.session_id == session.id and lease.epoch == run.lease_epoch:
            self.leases[resource_key] = replace(lease, expires_at=now_utc())
            self._append_workflow_event(session.id, run.workflow_id, "lease.released", {
                "resource_key": resource_key, "epoch": lease.epoch,
            })
        return run

    def list_approvals(self, project_id: str | None = None) -> list[Approval]:
        values = list(self.approvals.values())
        return [approval for approval in values if project_id is None or approval.project_id == project_id]

    def register_bridge(self, project_id: str, name: str) -> tuple[Bridge, str]:
        if project_id not in self.projects:
            raise StoreError("unknown project")
        raw = secrets.token_urlsafe(32)
        bridge = Bridge(new_id("bridge"), project_id, name, token_hash(raw))
        self.bridges[bridge.id] = bridge
        self.bridge_skills[bridge.id] = {}
        self.commands[bridge.id] = []
        project = self.projects[project_id]
        self.projects[project_id] = Project(project.id, project.name, project.state_root, bridge.id, project.created_at)
        self.append_audit("bridge.registered", "owner", project_id, {"bridge_id": bridge.id, "name": name})
        return bridge, raw

    def authenticate_bridge(self, bridge_id: str, token: str) -> Bridge:
        bridge = self.bridges.get(bridge_id)
        if bridge is None or not secrets.compare_digest(bridge.token_hash, token_hash(token)):
            raise StoreError("invalid bridge credentials")
        return bridge

    def replace_bridge_skills(self, bridge_id: str, skills: list[dict[str, str]]) -> list[BridgeSkill]:
        bridge = self.bridges.get(bridge_id)
        if bridge is None:
            raise StoreError("unknown bridge")
        clean: dict[str, BridgeSkill] = {}
        for skill in skills:
            skill_id = str(skill.get("skill_id", "")).strip()
            description = str(skill.get("description", "")).strip()
            digest = str(skill.get("content_sha256", "")).strip()
            if not skill_id or not description or len(digest) != 64 or skill_id in clean:
                raise StoreError("invalid bridge skill metadata")
            clean[skill_id] = BridgeSkill(bridge_id, skill_id, description, digest)
        self.bridge_skills[bridge_id] = clean
        self.append_audit("bridge.skills_published", "bridge:" + bridge_id, bridge.project_id, {"skill_ids": sorted(clean)})
        return list(clean.values())

    def list_bridge_skills(self, project_id: str | None = None) -> list[BridgeSkill]:
        bridge_ids = {
            bridge.id for bridge in self.bridges.values()
            if project_id is None or bridge.project_id == project_id
        }
        return [
            skill for bridge_id in bridge_ids for skill in self.bridge_skills.get(bridge_id, {}).values()
        ]

    def create_run(self, project_id: str, task_id: str | None, model: str, session_id: str | None = None, workflow_id: str | None = None, workflow_step_id: str | None = None, lease_epoch: int | None = None) -> Run:
        if project_id not in self.projects:
            raise StoreError("unknown project")
        if session_id is not None:
            session = self.get_session(session_id)
            if session.project_id != project_id or session.status != "active":
                raise StoreError("session is not active for this project")
            if any(run.session_id == session_id and run.status in {"queued", "running"} for run in self.runs.values()):
                raise StoreError("a session may have only one active run")
            lease = self.acquire_lease("worktree:" + session.worktree_id, session_id, 300)
            lease_epoch = lease.epoch
        run = Run(new_id("run"), project_id, task_id, model, "queued", session_id=session_id, workflow_id=workflow_id, workflow_step_id=workflow_step_id, lease_epoch=lease_epoch)
        self.runs[run.id] = run
        self.transcripts[run.id] = []
        self.append_audit("run.created", "operator", project_id, {"run_id": run.id, "task_id": task_id, "model": model, "session_id": session_id, "workflow_id": workflow_id, "lease_epoch": lease_epoch})
        return run

    def update_run_status(self, run_id: str, status: str, bridge_id: str) -> Run:
        run = self.runs.get(run_id)
        if run is None:
            raise StoreError("unknown run")
        project = self.projects.get(run.project_id)
        if project is None or project.bridge_id != bridge_id:
            raise StoreError("bridge does not own this run")
        if status not in _RUN_STATUS_TRANSITIONS:
            raise StoreError("invalid run status")
        if status == run.status:
            return run
        if status not in _RUN_STATUS_TRANSITIONS.get(run.status, frozenset()):
            raise StoreError(f"invalid run status transition: {run.status} -> {status}")
        completed_at = now_utc() if status in {"evidenced", "reconciliation"} else None
        updated = replace(run, status=status, completed_at=completed_at)
        self.runs[run_id] = updated
        if status == "reconciliation" and updated.session_id is not None:
            session = self.get_session(updated.session_id)
            resource_key = "worktree:" + session.worktree_id
            lease = self.leases.get(resource_key)
            if lease is not None and lease.session_id == session.id and lease.epoch == updated.lease_epoch:
                self.leases[resource_key] = replace(lease, expires_at=completed_at)
                self._append_workflow_event(session.id, None, "lease.released", {"resource_key": resource_key, "epoch": lease.epoch})
        if updated.workflow_id is not None and updated.session_id is not None and status == "reconciliation":
            workflow = self.get_workflow(updated.workflow_id)
            if workflow.status not in {"completed", "reconciliation", "cancelled"}:
                self.workflows[workflow.id] = replace(
                    workflow, status="reconciliation", cursor=(), updated_at=now_utc(),
                )
                self._append_workflow_event(updated.session_id, workflow.id, "workflow.delivery_finished", {
                    "run_id": updated.id, "status": "reconciliation",
                })
        self.append_audit("run.status_changed", "bridge:" + bridge_id, run.project_id, {"run_id": run_id, "status": status})
        return updated

    def finalize_run_command(
        self, run_id: str, status: str, bridge_id: str, command_id: str,
    ) -> Run:
        if status not in {"evidenced", "reconciliation"}:
            raise StoreError("run finalization status is invalid")
        commands = self.commands.get(bridge_id)
        if commands is None:
            raise StoreError("unknown bridge")
        command = next((item for item in commands if item["id"] == command_id), None)
        if (
            command is None or command.get("action_type") != "run_codex"
            or command.get("payload", {}).get("run_id") != run_id
        ):
            raise StoreError("run finalization command does not match this run")
        run = self.runs.get(run_id)
        if run is None:
            raise StoreError("unknown run")
        project = self.projects.get(run.project_id)
        if project is None or project.bridge_id != bridge_id:
            raise StoreError("bridge does not own this run")
        if run.status != status and status not in _RUN_STATUS_TRANSITIONS.get(run.status, frozenset()):
            raise StoreError(f"invalid run status transition: {run.status} -> {status}")

        completed_at = run.completed_at or now_utc()
        updated = replace(run, status=status, completed_at=completed_at)
        if (
            status == "evidenced" and run.status != "evidenced"
            and run.workflow_id is not None and run.workflow_step_id is not None
        ):
            self.complete_workflow_step(
                run.workflow_id, run.workflow_step_id, "succeeded", "bridge:" + bridge_id,
            )
        if status == "reconciliation" and run.session_id is not None:
            session = self.get_session(run.session_id)
            resource_key = "worktree:" + session.worktree_id
            lease = self.leases.get(resource_key)
            if lease is not None and lease.session_id == session.id and lease.epoch == run.lease_epoch:
                self.leases[resource_key] = replace(lease, expires_at=completed_at)
                self._append_workflow_event(session.id, run.workflow_id, "lease.released", {
                    "resource_key": resource_key, "epoch": lease.epoch,
                })
        if status == "reconciliation" and run.workflow_id is not None and run.session_id is not None:
            workflow = self.get_workflow(run.workflow_id)
            if workflow.status not in {"completed", "reconciliation", "cancelled"}:
                self.workflows[workflow.id] = replace(
                    workflow, status="reconciliation", cursor=(), updated_at=completed_at,
                )
                self._append_workflow_event(run.session_id, workflow.id, "workflow.delivery_finished", {
                    "run_id": run.id, "status": "reconciliation",
                })
        self.runs[run_id] = updated
        self.commands[bridge_id] = [item for item in commands if item["id"] != command_id]
        self.append_audit("run.finalized", "bridge:" + bridge_id, run.project_id, {
            "run_id": run_id, "status": status, "command_id": command_id,
        })
        return updated

    def add_transcript(self, run_id: str, role: str, content: Any, bridge_id: str | None = None) -> None:
        run = self.runs.get(run_id)
        if run is None:
            raise StoreError("unknown run")
        if bridge_id is not None:
            project = self.projects.get(run.project_id)
            if project is None or project.bridge_id != bridge_id:
                raise StoreError("bridge does not own this run")
        self.transcripts[run_id].append({"role": role, "content": redact_value(content), "created_at": now_utc().isoformat()})

    def create_approval(self, project_id: str, action_type: str, payload: dict[str, Any], requested_by: str, ttl_seconds: int, bridge_id: str | None) -> Approval:
        if project_id not in self.projects:
            raise StoreError("unknown project")
        if bridge_id is not None and bridge_id not in self.bridges:
            raise StoreError("unknown bridge")
        if action_type not in {"commit_pr", "merge_and_accept"}:
            raise StoreError("approval action is not executable by the v1 bridge")
        if action_type in {"commit_pr", "merge_and_accept"}:
            if bridge_id is None:
                raise StoreError("GitHub delivery approval requires a project bridge")
            run_id = payload.get("run_id")
            session_id = payload.get("session_id")
            worktree_id = payload.get("worktree_id")
            lease_epoch = payload.get("lease_epoch")
            if not all(isinstance(value, str) and value for value in (run_id, session_id, worktree_id)) or not isinstance(lease_epoch, int):
                raise StoreError("GitHub delivery approval must bind run, session, worktree, and lease epoch")
            run = self.validate_run_lease(run_id, bridge_id)
            if run.project_id != project_id or run.session_id != session_id or run.lease_epoch != lease_epoch:
                raise StoreError("GitHub delivery approval binding does not match the active run")
            session = self.get_session(session_id)
            if session.worktree_id != worktree_id or run.status != "evidenced":
                raise StoreError("GitHub delivery approval requires an evidenced run in its leased worktree")
            if action_type == "merge_and_accept":
                if payload.get("task_id") != run.task_id:
                    raise StoreError("merge approval task id must match the evidenced run")
                expected_head_sha = payload.get("expected_head_sha")
                if not isinstance(expected_head_sha, str) or _GIT_HEAD_SHA.fullmatch(expected_head_sha) is None:
                    raise StoreError("merge approval requires the expected pull request head SHA")
        clean = redact_value(payload)
        approval = Approval(
            new_id("approval"), project_id, action_type, clean, payload_hash(clean), requested_by,
            now_utc() + timedelta(seconds=ttl_seconds), bridge_id=bridge_id,
        )
        self.approvals[approval.id] = approval
        self.append_audit("approval.requested", requested_by, project_id, {"approval_id": approval.id, "action_type": action_type, "payload_hash": approval.payload_hash})
        return approval

    def get_approval(self, approval_id: str) -> Approval:
        try:
            return self.approvals[approval_id]
        except KeyError as exc:
            raise StoreError("unknown approval") from exc

    def approve(self, approval_id: str, actor: str, approvers: frozenset[str]) -> Approval:
        if actor not in approvers:
            raise StoreError("actor is not an allowlisted approver")
        approval = self.get_approval(approval_id)
        if approval.status != "pending":
            raise StoreError("approval is not pending")
        if approval.expired:
            raise StoreError("approval expired")
        approved = Approval(**{**approval.__dict__, "status": "approved", "approved_by": actor, "approved_at": now_utc()})
        self.approvals[approval.id] = approved
        self.append_audit("approval.granted", actor, approval.project_id, {"approval_id": approval.id, "payload_hash": approval.payload_hash})
        return approved

    def consume(self, approval_id: str, action_payload_hash: str) -> Approval:
        approval = self.get_approval(approval_id)
        if approval.action_type in {"commit_pr", "merge_and_accept"}:
            raise StoreError("GitHub delivery approvals require an active run lease")
        if approval.status != "approved" or approval.expired:
            raise StoreError("approval is not valid for execution")
        if not secrets.compare_digest(approval.payload_hash, action_payload_hash):
            raise StoreError("action payload differs from the approved hash")
        used = Approval(**{**approval.__dict__, "status": "consumed", "consumed_at": now_utc()})
        self.approvals[approval.id] = used
        self.append_audit("approval.consumed", approval.approved_by or "unknown", approval.project_id, {"approval_id": approval.id, "payload_hash": approval.payload_hash})
        return used

    def consume_approval_for_run(self, approval_id: str, action_payload_hash: str, bridge_id: str) -> Approval:
        """Atomically revalidate and consume a GitHub grant while extending its lease."""
        approval = self.get_approval(approval_id)
        if approval.action_type not in {"commit_pr", "merge_and_accept"}:
            raise StoreError("approval is not a GitHub delivery action")
        if approval.bridge_id != bridge_id or approval.status != "approved" or approval.expired:
            raise StoreError("approval is not valid for this bridge action")
        if not secrets.compare_digest(approval.payload_hash, action_payload_hash):
            raise StoreError("action payload differs from the approved hash")
        binding = approval.payload
        run_id = binding.get("run_id")
        session_id = binding.get("session_id")
        worktree_id = binding.get("worktree_id")
        lease_epoch = binding.get("lease_epoch")
        if not all(isinstance(value, str) and value for value in (run_id, session_id, worktree_id)) or not isinstance(lease_epoch, int):
            raise StoreError("GitHub delivery approval has an invalid run binding")
        run = self.validate_run_lease(run_id, bridge_id)
        if (
            run.project_id != approval.project_id
            or run.session_id != session_id
            or run.lease_epoch != lease_epoch
            or self.get_session(session_id).worktree_id != worktree_id
            or run.status != "evidenced"
        ):
            raise StoreError("GitHub delivery approval binding no longer matches the active evidenced run")
        if approval.action_type == "merge_and_accept" and binding.get("task_id") != run.task_id:
            raise StoreError("merge approval task id no longer matches the evidenced run")
        self.renew_run_lease(run_id, bridge_id)
        used = Approval(**{**approval.__dict__, "status": "consumed", "consumed_at": now_utc()})
        self.approvals[approval.id] = used
        self.append_audit("approval.consumed", approval.approved_by or "unknown", approval.project_id, {
            "approval_id": approval.id, "payload_hash": approval.payload_hash,
        })
        return used

    def complete_approved_merge(
        self, approval_id: str, action_payload_hash: str, bridge_id: str, command_id: str | None = None,
    ) -> Run:
        """Finalize only the already-consumed merge/State-acceptance action."""
        approval = self.get_approval(approval_id)
        if (
            approval.action_type != "merge_and_accept"
            or approval.bridge_id != bridge_id
            or approval.status != "consumed"
            or not secrets.compare_digest(approval.payload_hash, action_payload_hash)
        ):
            raise StoreError("merge approval is not valid for delivery completion")
        binding = approval.payload
        run_id = binding.get("run_id")
        session_id = binding.get("session_id")
        worktree_id = binding.get("worktree_id")
        lease_epoch = binding.get("lease_epoch")
        if not all(isinstance(value, str) and value for value in (run_id, session_id, worktree_id)) or not isinstance(lease_epoch, int):
            raise StoreError("merge approval has an invalid run binding")
        run = self.validate_run_lease(run_id, bridge_id)
        if (
            run.status != "evidenced"
            or run.project_id != approval.project_id
            or run.session_id != session_id
            or run.lease_epoch != lease_epoch
            or self.get_session(session_id).worktree_id != worktree_id
        ):
            raise StoreError("merge approval binding no longer matches the active evidenced run")
        if binding.get("task_id") != run.task_id:
            raise StoreError("merge approval task id no longer matches the evidenced run")
        completed_at = now_utc()
        completed = replace(run, status="completed", completed_at=completed_at)
        self.runs[run.id] = completed
        resource_key = "worktree:" + worktree_id
        lease = self.leases.get(resource_key)
        if lease is not None and lease.session_id == session_id and lease.epoch == lease_epoch:
            self.leases[resource_key] = replace(lease, expires_at=completed_at)
            self._append_workflow_event(session_id, run.workflow_id, "lease.released", {
                "resource_key": resource_key, "epoch": lease_epoch,
            })
        if run.workflow_id is not None:
            workflow = self.get_workflow(run.workflow_id)
            if workflow.status not in {"completed", "reconciliation", "cancelled"}:
                self.workflows[workflow.id] = replace(workflow, status="completed", cursor=(), updated_at=completed_at)
                self._append_workflow_event(session_id, workflow.id, "workflow.delivery_finished", {
                    "run_id": run.id, "status": "completed",
                })
        if command_id is not None:
            commands = self.commands.get(bridge_id)
            if commands is None:
                raise StoreError("unknown bridge")
            remaining = [
                command for command in commands
                if not (command["id"] == command_id and command.get("approval_id") == approval_id)
            ]
            if len(remaining) == len(commands):
                raise StoreError("merge completion command is not owned by this approval")
            self.commands[bridge_id] = remaining
        self.append_audit("run.completed", "bridge:" + bridge_id, run.project_id, {
            "run_id": run.id, "approval_id": approval_id,
        })
        return completed

    def enqueue_command(self, bridge_id: str, approval: Approval) -> None:
        if approval.status != "approved":
            raise StoreError("only approved actions may be queued")
        self.commands.setdefault(bridge_id, []).append({
            "id": new_id("command"), "approval_id": approval.id, "action_type": approval.action_type,
            "payload": approval.payload, "payload_hash": approval.payload_hash,
        })

    def enqueue_run(self, bridge_id: str, run: Run) -> None:
        if bridge_id not in self.bridges:
            raise StoreError("unknown bridge")
        session = self.get_session(run.session_id) if run.session_id else None
        skills: list[dict[str, str]] = []
        if run.workflow_id and run.workflow_step_id:
            step = step_by_id(self.get_workflow(run.workflow_id).definition, run.workflow_step_id)
            published = self.bridge_skills.get(bridge_id, {})
            for skill_id in step.get("skills", []):
                skill = published.get(skill_id)
                if skill is None:
                    raise StoreError(f"workflow skill is not published by this bridge: {skill_id}")
                skills.append({
                    "skill_id": skill.skill_id,
                    "description": skill.description,
                    "content_sha256": skill.content_sha256,
                })
        directive_events = [
            event for event in self.workflow_events.get(run.session_id or "", [])
            if event.kind == "operator.directive" and isinstance(event.data.get("content"), str)
        ]
        included_directives = directive_events[-32:]
        directives = [str(event.data.get("content", "")) for event in included_directives]
        directive_high_water = included_directives[-1].sequence if included_directives else 0
        payload = {
            "run_id": run.id, "project_id": run.project_id, "task_id": run.task_id,
            "model": run.model, "session_id": run.session_id,
            "workflow_id": run.workflow_id, "workflow_step_id": run.workflow_step_id,
            "lease_epoch": run.lease_epoch,
            "worktree_id": session.worktree_id if session else None,
            "skills": skills,
            "directives": directives,
        }
        self.commands.setdefault(bridge_id, []).append({
            "id": new_id("command"), "approval_id": None, "action_type": "run_codex",
            "payload": payload, "payload_hash": payload_hash(payload),
        })
        # Record the packet-inclusion marker so a directive carried in this queued
        # packet reports INCLUDED via session_directive_view (T008 criterion 2).
        if run.session_id and directive_high_water > 0:
            self._append_workflow_event(run.session_id, run.workflow_id, "operator.directive_packet", {
                "included_up_to_sequence": directive_high_water,
            })

    def enqueue_skill_probe(self, bridge_id: str) -> None:
        bridge = self.bridges.get(bridge_id)
        if bridge is None:
            raise StoreError("unknown bridge")
        skills = sorted(self.bridge_skills.get(bridge_id, {}).values(), key=lambda item: item.skill_id)
        if not skills:
            raise StoreError("the bridge has not published any configured skills")
        payload = {"project_id": bridge.project_id, "skills": [
            {"skill_id": skill.skill_id, "content_sha256": skill.content_sha256} for skill in skills
        ]}
        self.commands.setdefault(bridge_id, []).append({
            "id": new_id("command"), "approval_id": None, "action_type": "skill_probe",
            "payload": payload, "payload_hash": payload_hash(payload),
        })

    def next_command(self, bridge_id: str) -> dict[str, Any] | None:
        commands = self.commands.get(bridge_id, [])
        now = now_utc()
        for command in commands:
            leased_until = command.get("lease_expires_at")
            if leased_until is not None and leased_until > now:
                continue
            command["lease_expires_at"] = now + timedelta(minutes=5)
            command["delivery_attempts"] = int(command.get("delivery_attempts", 0)) + 1
            return dict(command)
        return None

    def acknowledge_command(self, bridge_id: str, command_id: str) -> None:
        commands = self.commands.get(bridge_id)
        if commands is None:
            raise StoreError("unknown bridge")
        command = next((item for item in commands if item["id"] == command_id), None)
        if command is None:
            raise StoreError("unknown bridge command")
        if command["action_type"] == "run_codex":
            raise StoreError("run commands must be acknowledged through terminal finalization")
        remaining = [item for item in commands if item["id"] != command_id]
        self.commands[bridge_id] = remaining


class PostgresStore:
    """Production Postgres implementation of the Workbench store.

    Unlike the test-only ``MemoryStore``, every Workbench-owned record is
    persisted before it can be returned to an API caller.  It deliberately has
    no tables for Anvil State tasks, claims, or acceptance: those remain in the
    project-local State CLI/event stream.
    """

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self._conn: Any = None
        self._jsonb: Any = None

    def _connection(self) -> Any:
        if self._conn is None:
            raise RuntimeError("PostgresStore.initialize() must run before requests")
        return self._conn

    @staticmethod
    def _project(row: dict[str, Any]) -> Project:
        return Project(row["id"], row["name"], row["state_root"], row["bridge_id"], row["created_at"])

    @staticmethod
    def _bridge(row: dict[str, Any]) -> Bridge:
        return Bridge(row["id"], row["project_id"], row["name"], row["token_hash"], row["created_at"], row["last_seen_at"])

    @staticmethod
    def _bridge_skill(row: dict[str, Any]) -> BridgeSkill:
        return BridgeSkill(row["bridge_id"], row["skill_id"], row["description"], row["content_sha256"], row["updated_at"])

    @staticmethod
    def _audit(row: dict[str, Any]) -> AuditEvent:
        return AuditEvent(row["id"], row["kind"], row["actor"], row["project_id"], row["data"], row["created_at"])

    @staticmethod
    def _run(row: dict[str, Any]) -> Run:
        return Run(
            row["id"], row["project_id"], row["task_id"], row["model"], row["status"],
            row["created_at"], row["completed_at"], row.get("session_id"), row.get("workflow_id"),
            row.get("workflow_step_id"), row.get("lease_epoch"),
        )

    @staticmethod
    def _session(row: dict[str, Any]) -> Session:
        return Session(
            row["id"], row["project_id"], row["title"], row["worktree_id"], row["status"],
            row["voice_enabled"], row["created_at"], row["updated_at"],
        )

    @staticmethod
    def _workflow(row: dict[str, Any]) -> Workflow:
        return Workflow(
            row["id"], row["project_id"], row["session_id"], row["version"], row["definition"],
            row["status"], tuple(row["cursor"]), row["created_at"], row["updated_at"],
        )

    @staticmethod
    def _workflow_event(row: dict[str, Any]) -> WorkflowEvent:
        return WorkflowEvent(
            row["id"], row["session_id"], row["workflow_id"], row["sequence"], row["kind"],
            row["data"], row["created_at"],
        )

    @staticmethod
    def _lease(row: dict[str, Any]) -> ResourceLease:
        return ResourceLease(row["resource_key"], row["session_id"], row["epoch"], row["expires_at"], row["created_at"])

    @staticmethod
    def _approval(row: dict[str, Any]) -> Approval:
        return Approval(
            row["id"], row["project_id"], row["action_type"], row["payload"], row["payload_hash"],
            row["requested_by"], row["expires_at"], row["status"], row["approved_by"],
            row["approved_at"], row["consumed_at"], row["bridge_id"], row["created_at"],
        )

    def _json(self, value: Any) -> Any:
        return self._jsonb(value)

    def initialize(self) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
            from psycopg.types.json import Jsonb
        except ImportError as exc:  # pragma: no cover - dependency contract
            raise RuntimeError("psycopg is required for the production Workbench store") from exc
        self._jsonb = Jsonb
        self._conn = psycopg.connect(self.database_url, autocommit=True, row_factory=dict_row)
        with self._conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS workbench_projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, state_root TEXT NOT NULL,
                    bridge_id TEXT, created_at TIMESTAMPTZ NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workbench_bridges (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES workbench_projects(id),
                    name TEXT NOT NULL, token_hash TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL,
                    last_seen_at TIMESTAMPTZ
                );
                CREATE TABLE IF NOT EXISTS workbench_bridge_skills (
                    bridge_id TEXT NOT NULL REFERENCES workbench_bridges(id),
                    skill_id TEXT NOT NULL, description TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (bridge_id, skill_id)
                );
                CREATE TABLE IF NOT EXISTS workbench_runs (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES workbench_projects(id),
                    task_id TEXT, model TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL, completed_at TIMESTAMPTZ,
                    session_id TEXT, workflow_id TEXT, workflow_step_id TEXT, lease_epoch INTEGER
                );
                CREATE TABLE IF NOT EXISTS workbench_sessions (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES workbench_projects(id),
                    title TEXT NOT NULL, worktree_id TEXT NOT NULL, status TEXT NOT NULL,
                    voice_enabled BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workbench_workflows (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES workbench_projects(id),
                    session_id TEXT NOT NULL REFERENCES workbench_sessions(id), version INTEGER NOT NULL,
                    definition JSONB NOT NULL, status TEXT NOT NULL, cursor JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workbench_workflow_events (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES workbench_sessions(id),
                    workflow_id TEXT REFERENCES workbench_workflows(id), sequence BIGINT NOT NULL,
                    kind TEXT NOT NULL, data JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL,
                    UNIQUE(session_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS workbench_resource_leases (
                    resource_key TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES workbench_sessions(id),
                    epoch INTEGER NOT NULL, expires_at TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workbench_transcripts (
                    run_id TEXT NOT NULL REFERENCES workbench_runs(id), ordinal BIGSERIAL PRIMARY KEY,
                    role TEXT NOT NULL, content JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workbench_approvals (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES workbench_projects(id),
                    action_type TEXT NOT NULL, payload JSONB NOT NULL, payload_hash TEXT NOT NULL,
                    requested_by TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL, status TEXT NOT NULL,
                    approved_by TEXT, approved_at TIMESTAMPTZ, consumed_at TIMESTAMPTZ,
                    bridge_id TEXT REFERENCES workbench_bridges(id), created_at TIMESTAMPTZ NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workbench_commands (
                    id TEXT PRIMARY KEY, bridge_id TEXT NOT NULL REFERENCES workbench_bridges(id),
                    approval_id TEXT REFERENCES workbench_approvals(id), action_type TEXT NOT NULL,
                    payload JSONB NOT NULL, payload_hash TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL,
                    delivery_attempts INTEGER NOT NULL DEFAULT 0, lease_expires_at TIMESTAMPTZ
                );
                CREATE TABLE IF NOT EXISTS workbench_audit (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, actor TEXT NOT NULL,
                    project_id TEXT, data JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL
                );
                CREATE INDEX IF NOT EXISTS workbench_commands_bridge_idx ON workbench_commands (bridge_id, created_at);
                CREATE INDEX IF NOT EXISTS workbench_bridge_skills_bridge_idx ON workbench_bridge_skills (bridge_id, skill_id);
                CREATE INDEX IF NOT EXISTS workbench_approvals_project_idx ON workbench_approvals (project_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS workbench_sessions_project_idx ON workbench_sessions (project_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS workbench_workflow_events_session_idx ON workbench_workflow_events (session_id, sequence);
                ALTER TABLE workbench_runs ADD COLUMN IF NOT EXISTS session_id TEXT;
                ALTER TABLE workbench_runs ADD COLUMN IF NOT EXISTS workflow_id TEXT;
                ALTER TABLE workbench_runs ADD COLUMN IF NOT EXISTS workflow_step_id TEXT;
                ALTER TABLE workbench_runs ADD COLUMN IF NOT EXISTS lease_epoch INTEGER;
                ALTER TABLE workbench_commands ADD COLUMN IF NOT EXISTS delivery_attempts INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE workbench_commands ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;
            """)

    def append_audit(self, kind: str, actor: str, project_id: str | None, data: dict[str, Any]) -> AuditEvent:
        event = AuditEvent(new_id("audit"), kind, actor, project_id, redact_value(data))
        with self._connection().cursor() as cur:
            cur.execute(
                "INSERT INTO workbench_audit (id, kind, actor, project_id, data, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
                (event.id, event.kind, event.actor, event.project_id, self._json(event.data), event.created_at),
            )
        return event

    def create_project(self, name: str, state_root: str) -> Project:
        project = Project(new_id("project"), name.strip(), state_root.strip())
        with self._connection().cursor() as cur:
            cur.execute(
                "INSERT INTO workbench_projects (id, name, state_root, bridge_id, created_at) VALUES (%s,%s,%s,%s,%s)",
                (project.id, project.name, project.state_root, None, project.created_at),
            )
        self.append_audit("project.created", "owner", project.id, {"name": project.name})
        return project

    def list_projects(self) -> list[Project]:
        with self._connection().cursor() as cur:
            cur.execute("SELECT * FROM workbench_projects ORDER BY created_at DESC")
            return [self._project(row) for row in cur.fetchall()]

    def list_runs(self, project_id: str | None = None) -> list[Run]:
        query = "SELECT * FROM workbench_runs"
        values: tuple[Any, ...] = ()
        if project_id is not None:
            query += " WHERE project_id = %s"
            values = (project_id,)
        query += " ORDER BY created_at DESC"
        with self._connection().cursor() as cur:
            cur.execute(query, values)
            return [self._run(row) for row in cur.fetchall()]

    def list_audit(self, limit: int = 20) -> list[AuditEvent]:
        with self._connection().cursor() as cur:
            cur.execute("SELECT * FROM workbench_audit ORDER BY created_at DESC LIMIT %s", (max(1, min(limit, 100)),))
            return [self._audit(row) for row in cur.fetchall()]

    def _append_workflow_event(self, session_id: str, workflow_id: str | None, kind: str, data: dict[str, Any]) -> WorkflowEvent:
        event = WorkflowEvent(new_id("event"), session_id, workflow_id, 0, kind, redact_value(data))
        connection = self._connection()
        with connection.transaction():
            with connection.cursor() as cur:
                cur.execute("SELECT id FROM workbench_sessions WHERE id = %s FOR UPDATE", (session_id,))
                if cur.fetchone() is None:
                    raise StoreError("unknown session")
                cur.execute("SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence FROM workbench_workflow_events WHERE session_id = %s", (session_id,))
                sequence = int(cur.fetchone()["sequence"])
                event = WorkflowEvent(event.id, event.session_id, event.workflow_id, sequence, event.kind, event.data, event.created_at)
                cur.execute(
                    "INSERT INTO workbench_workflow_events (id,session_id,workflow_id,sequence,kind,data,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (event.id, event.session_id, event.workflow_id, event.sequence, event.kind, self._json(event.data), event.created_at),
                )
                cur.execute("UPDATE workbench_sessions SET updated_at = %s WHERE id = %s", (event.created_at, session_id))
        return event

    def create_session(
        self, project_id: str, title: str, worktree_id: str, workflow_definition: dict[str, Any],
    ) -> tuple[Session, Workflow]:
        try:
            definition = validate_definition(workflow_definition)
        except WorkflowError as exc:
            raise StoreError(str(exc)) from exc
        session = Session(new_id("session"), project_id, title.strip(), worktree_id.strip())
        workflow = Workflow(new_id("workflow"), project_id, session.id, 1, definition)
        connection = self._connection()
        with connection.transaction():
            with connection.cursor() as cur:
                cur.execute("SELECT id FROM workbench_projects WHERE id = %s", (project_id,))
                if cur.fetchone() is None:
                    raise StoreError("unknown project")
                cur.execute(
                    "INSERT INTO workbench_sessions (id,project_id,title,worktree_id,status,voice_enabled,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (session.id, session.project_id, session.title, session.worktree_id, session.status, session.voice_enabled, session.created_at, session.updated_at),
                )
                cur.execute(
                    "INSERT INTO workbench_workflows (id,project_id,session_id,version,definition,status,cursor,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (workflow.id, workflow.project_id, workflow.session_id, workflow.version, self._json(workflow.definition), workflow.status, self._json(list(workflow.cursor)), workflow.created_at, workflow.updated_at),
                )
        self._append_workflow_event(session.id, workflow.id, "session.created", {"title": session.title, "worktree_id": session.worktree_id})
        self._append_workflow_event(session.id, workflow.id, "workflow.created", {"workflow_id": workflow.id, "version": workflow.version})
        self.append_audit("session.created", "operator", project_id, {"session_id": session.id, "workflow_id": workflow.id, "worktree_id": session.worktree_id})
        return session, workflow

    def list_sessions(self, project_id: str | None = None) -> list[Session]:
        query = "SELECT * FROM workbench_sessions"
        values: tuple[Any, ...] = ()
        if project_id is not None:
            query += " WHERE project_id = %s"
            values = (project_id,)
        query += " ORDER BY updated_at DESC"
        with self._connection().cursor() as cur:
            cur.execute(query, values)
            return [self._session(row) for row in cur.fetchall()]

    def get_session(self, session_id: str) -> Session:
        with self._connection().cursor() as cur:
            cur.execute("SELECT * FROM workbench_sessions WHERE id = %s", (session_id,))
            row = cur.fetchone()
        if row is None:
            raise StoreError("unknown session")
        return self._session(row)

    def get_workflow(self, workflow_id: str) -> Workflow:
        with self._connection().cursor() as cur:
            cur.execute("SELECT * FROM workbench_workflows WHERE id = %s", (workflow_id,))
            row = cur.fetchone()
        if row is None:
            raise StoreError("unknown workflow")
        return self._workflow(row)

    def list_workflows(self, session_id: str | None = None) -> list[Workflow]:
        query = "SELECT * FROM workbench_workflows"
        values: tuple[Any, ...] = ()
        if session_id is not None:
            query += " WHERE session_id = %s"
            values = (session_id,)
        query += " ORDER BY updated_at DESC"
        with self._connection().cursor() as cur:
            cur.execute(query, values)
            return [self._workflow(row) for row in cur.fetchall()]

    def list_workflow_events(self, session_id: str, after_sequence: int = 0) -> list[WorkflowEvent]:
        self.get_session(session_id)
        with self._connection().cursor() as cur:
            cur.execute("SELECT * FROM workbench_workflow_events WHERE session_id = %s AND sequence > %s ORDER BY sequence", (session_id, after_sequence))
            return [self._workflow_event(row) for row in cur.fetchall()]

    def record_session_event(self, session_id: str, workflow_id: str | None, kind: str, data: dict[str, Any]) -> WorkflowEvent:
        self.get_session(session_id)
        return self._append_workflow_event(session_id, workflow_id, kind, data)

    def revise_workflow(self, workflow_id: str, expected_version: int, definition: dict[str, Any], actor: str) -> Workflow:
        try:
            clean = validate_definition(definition)
        except WorkflowError as exc:
            raise StoreError(str(exc)) from exc
        now = now_utc()
        with self._connection().cursor() as cur:
            cur.execute(
                "UPDATE workbench_workflows SET version = version + 1, definition = %s, updated_at = %s WHERE id = %s AND version = %s AND status = 'draft' RETURNING *",
                (self._json(clean), now, workflow_id, expected_version),
            )
            row = cur.fetchone()
        if row is None:
            raise StoreError("workflow is running or changed; reload before revising")
        workflow = self._workflow(row)
        self._append_workflow_event(workflow.session_id, workflow.id, "workflow.revised", {"version": workflow.version, "actor": actor})
        return workflow

    def start_workflow(self, workflow_id: str, actor: str) -> Workflow:
        workflow = self.get_workflow(workflow_id)
        if workflow.status != "draft":
            raise StoreError("workflow is not a draft")
        entry = str(workflow.definition["entry"])
        now = now_utc()
        with self._connection().cursor() as cur:
            cur.execute(
                "UPDATE workbench_workflows SET status = 'running', cursor = %s, updated_at = %s WHERE id = %s AND status = 'draft' RETURNING *",
                (self._json([entry]), now, workflow_id),
            )
            row = cur.fetchone()
        if row is None:
            raise StoreError("workflow is not a draft")
        started = self._workflow(row)
        self._append_workflow_event(started.session_id, started.id, "workflow.started", {"step_id": entry, "actor": actor})
        return started

    def start_workflow_run(
        self, workflow_id: str, task_id: str, model: str, actor: str,
    ) -> tuple[Workflow, Run]:
        """Atomically fence a worktree, start a workflow, and queue its run."""
        connection = self._connection()
        with connection.transaction():
            with connection.cursor() as cur:
                # Discover ids first, then take locks in the same
                # project -> session -> workflow order as run creation.
                cur.execute("SELECT * FROM workbench_workflows WHERE id = %s", (workflow_id,))
                workflow_row = cur.fetchone()
                if workflow_row is None:
                    raise StoreError("unknown workflow")
                workflow = self._workflow(workflow_row)
                cur.execute("SELECT bridge_id FROM workbench_projects WHERE id = %s FOR UPDATE", (workflow.project_id,))
                project = cur.fetchone()
                bridge_id = project["bridge_id"] if project is not None else None
                if bridge_id is None:
                    raise StoreError("a project bridge is required before a workflow can start")
                cur.execute("SELECT * FROM workbench_sessions WHERE id = %s FOR UPDATE", (workflow.session_id,))
                session = cur.fetchone()
                if session is None or session["status"] != "active":
                    raise StoreError("session is not active for this project")
                cur.execute("SELECT * FROM workbench_workflows WHERE id = %s FOR UPDATE", (workflow_id,))
                workflow_row = cur.fetchone()
                if workflow_row is None:
                    raise StoreError("unknown workflow")
                if workflow_row["status"] != "draft":
                    raise StoreError("workflow is not a draft")
                workflow = self._workflow(workflow_row)

                entry = str(workflow.definition["entry"])
                step = step_by_id(workflow.definition, entry)
                if step["kind"] != "agent":
                    raise StoreError("v1 workflows must begin with an agent step")
                skills: list[dict[str, str]] = []
                missing: list[str] = []
                for skill_id in step.get("skills", []):
                    cur.execute(
                        "SELECT * FROM workbench_bridge_skills WHERE bridge_id = %s AND skill_id = %s",
                        (bridge_id, skill_id),
                    )
                    skill_row = cur.fetchone()
                    if skill_row is None:
                        missing.append(skill_id)
                    else:
                        skill = self._bridge_skill(skill_row)
                        skills.append({
                            "skill_id": skill.skill_id,
                            "description": skill.description,
                            "content_sha256": skill.content_sha256,
                        })
                if missing:
                    raise StoreError(
                        "the bridge must publish every selected workflow skill before the workflow can start: "
                        + ", ".join(missing)
                    )
                cur.execute(
                    "SELECT id FROM workbench_runs WHERE session_id = %s AND status IN ('queued', 'running') FOR UPDATE",
                    (workflow.session_id,),
                )
                if cur.fetchone() is not None:
                    raise StoreError("a session may have only one active run")

                now = now_utc()
                resource_key = "worktree:" + str(session["worktree_id"])
                cur.execute(
                    "SELECT * FROM workbench_resource_leases WHERE resource_key = %s FOR UPDATE",
                    (resource_key,),
                )
                existing = cur.fetchone()
                if existing is not None and existing["expires_at"] > now and existing["session_id"] != workflow.session_id:
                    raise StoreError("resource is leased by another active session")
                lease = ResourceLease(
                    resource_key, workflow.session_id, (int(existing["epoch"]) + 1) if existing else 1,
                    now + timedelta(seconds=300), now,
                )
                cur.execute(
                    "INSERT INTO workbench_resource_leases (resource_key,session_id,epoch,expires_at,created_at) "
                    "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (resource_key) DO UPDATE SET "
                    "session_id = EXCLUDED.session_id, epoch = EXCLUDED.epoch, "
                    "expires_at = EXCLUDED.expires_at, created_at = EXCLUDED.created_at",
                    (lease.resource_key, lease.session_id, lease.epoch, lease.expires_at, lease.created_at),
                )

                selected_model = str(step.get("model") or model).strip()
                run = Run(
                    new_id("run"), workflow.project_id, task_id, selected_model, "queued",
                    session_id=workflow.session_id, workflow_id=workflow.id,
                    workflow_step_id=entry, lease_epoch=lease.epoch,
                )
                cur.execute(
                    "INSERT INTO workbench_runs "
                    "(id,project_id,task_id,model,status,created_at,completed_at,session_id,workflow_id,workflow_step_id,lease_epoch) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        run.id, run.project_id, run.task_id, run.model, run.status,
                        run.created_at, None, run.session_id, run.workflow_id,
                        run.workflow_step_id, run.lease_epoch,
                    ),
                )
                cur.execute(
                    "UPDATE workbench_workflows SET status = 'running', cursor = %s, updated_at = %s "
                    "WHERE id = %s AND status = 'draft' RETURNING *",
                    (self._json([entry]), now, workflow.id),
                )
                started_row = cur.fetchone()
                if started_row is None:
                    raise StoreError("workflow is not a draft")
                cur.execute(
                    "SELECT data, sequence FROM workbench_workflow_events "
                    "WHERE session_id = %s AND kind = 'operator.directive' ORDER BY sequence",
                    (workflow.session_id,),
                )
                directive_rows = [row for row in cur.fetchall() if isinstance(row["data"], dict)]
                included_rows = directive_rows[-32:]
                directives = [str(row["data"].get("content", "")) for row in included_rows]
                directive_high_water = int(included_rows[-1]["sequence"]) if included_rows else 0
                command_payload = {
                    "run_id": run.id, "project_id": run.project_id, "task_id": run.task_id,
                    "model": run.model, "session_id": run.session_id,
                    "workflow_id": run.workflow_id, "workflow_step_id": run.workflow_step_id,
                    "lease_epoch": run.lease_epoch, "worktree_id": str(session["worktree_id"]),
                    "skills": skills, "directives": directives,
                }
                cur.execute(
                    "INSERT INTO workbench_commands "
                    "(id,bridge_id,approval_id,action_type,payload,payload_hash,created_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        new_id("command"), bridge_id, None, "run_codex",
                        self._json(command_payload), payload_hash(command_payload), now,
                    ),
                )

                cur.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence "
                    "FROM workbench_workflow_events WHERE session_id = %s",
                    (workflow.session_id,),
                )
                sequence = int(cur.fetchone()["sequence"])
                event_values = [
                    ("lease.acquired", {"resource_key": resource_key, "epoch": lease.epoch}),
                    ("workflow.started", {"step_id": entry, "actor": actor, "run_id": run.id}),
                ]
                # Packet-inclusion marker (operator.directive_packet): the highest
                # directive sequence carried in the queued run_codex payload above,
                # so session_directive_view reports it INCLUDED (T008 criterion 2).
                if directive_high_water > 0:
                    event_values.append(
                        ("operator.directive_packet", {"included_up_to_sequence": directive_high_water})
                    )
                for offset, (kind, data) in enumerate(event_values):
                    cur.execute(
                        "INSERT INTO workbench_workflow_events "
                        "(id,session_id,workflow_id,sequence,kind,data,created_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (
                            new_id("event"), workflow.session_id, workflow.id, sequence + offset,
                            kind, self._json(redact_value(data)), now,
                        ),
                    )
                cur.execute(
                    "UPDATE workbench_sessions SET updated_at = %s WHERE id = %s",
                    (now, workflow.session_id),
                )
                audits = (
                    ("run.created", {
                        "run_id": run.id, "task_id": task_id, "model": run.model,
                        "session_id": run.session_id, "workflow_id": run.workflow_id,
                        "lease_epoch": run.lease_epoch,
                    }),
                    ("workflow.run_requested", {
                        "workflow_id": workflow.id, "step_id": entry, "run_id": run.id,
                    }),
                )
                for kind, data in audits:
                    cur.execute(
                        "INSERT INTO workbench_audit (id,kind,actor,project_id,data,created_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s)",
                        (
                            new_id("audit"), kind, actor, workflow.project_id,
                            self._json(redact_value(data)), now,
                        ),
                    )
        return self._workflow(started_row), run

    def complete_workflow_step(self, workflow_id: str, step_id: str, outcome: str, actor: str) -> Workflow:
        if outcome not in {"succeeded", "failed", "cancelled"}:
            raise StoreError("workflow outcome is invalid")
        connection = self._connection()
        with connection.transaction():
            with connection.cursor() as cur:
                cur.execute("SELECT * FROM workbench_workflows WHERE id = %s FOR UPDATE", (workflow_id,))
                row = cur.fetchone()
                if row is None:
                    raise StoreError("unknown workflow")
                workflow = self._workflow(row)
                if workflow.status not in {"running", "waiting_approval"} or step_id not in workflow.cursor:
                    raise StoreError("workflow step is not runnable")
                if outcome == "failed":
                    status_value, cursor = "reconciliation", ()
                elif outcome == "cancelled":
                    status_value, cursor = "cancelled", ()
                else:
                    cursor = advance_cursor(workflow.definition, workflow.cursor, step_id)
                    next_kinds = {step_by_id(workflow.definition, next_step)["kind"] for next_step in cursor}
                    status_value = "completed" if not cursor else "waiting_approval" if "approval_wait" in next_kinds else "reconciliation" if "reconcile" in next_kinds else "running"
                now = now_utc()
                cur.execute(
                    "UPDATE workbench_workflows SET status = %s, cursor = %s, updated_at = %s "
                    "WHERE id = %s AND status = %s AND cursor = %s RETURNING *",
                    (
                        status_value, self._json(list(cursor)), now, workflow_id,
                        workflow.status, self._json(list(workflow.cursor)),
                    ),
                )
                row = cur.fetchone()
                if row is None:
                    raise StoreError("workflow cursor changed; retry the step transition")
        updated = self._workflow(row)
        self._append_workflow_event(updated.session_id, updated.id, "workflow.step.finished", {"step_id": step_id, "outcome": outcome, "next": list(cursor), "actor": actor})
        return updated

    def acquire_lease(self, resource_key: str, session_id: str, ttl_seconds: int) -> ResourceLease:
        session = self.get_session(session_id)
        now = now_utc()
        connection = self._connection()
        with connection.transaction():
            with connection.cursor() as cur:
                cur.execute("SELECT * FROM workbench_resource_leases WHERE resource_key = %s FOR UPDATE", (resource_key,))
                row = cur.fetchone()
                if row is not None and row["expires_at"] > now and row["session_id"] != session.id:
                    raise StoreError("resource is leased by another active session")
                epoch = (int(row["epoch"]) + 1) if row is not None else 1
                lease = ResourceLease(resource_key, session.id, epoch, now + timedelta(seconds=ttl_seconds))
                cur.execute(
                    "INSERT INTO workbench_resource_leases (resource_key,session_id,epoch,expires_at,created_at) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (resource_key) DO UPDATE SET session_id = EXCLUDED.session_id, epoch = EXCLUDED.epoch, expires_at = EXCLUDED.expires_at, created_at = EXCLUDED.created_at",
                    (lease.resource_key, lease.session_id, lease.epoch, lease.expires_at, lease.created_at),
                )
        self._append_workflow_event(session.id, None, "lease.acquired", {"resource_key": resource_key, "epoch": lease.epoch})
        return lease

    def validate_run_lease(self, run_id: str, bridge_id: str) -> Run:
        """Return a run only while its session owns its exact lease epoch."""
        connection = self._connection()
        with connection.transaction():
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT runs.*, projects.bridge_id, sessions.worktree_id FROM workbench_runs runs "
                    "JOIN workbench_projects projects ON projects.id = runs.project_id "
                    "LEFT JOIN workbench_sessions sessions ON sessions.id = runs.session_id "
                    "WHERE runs.id = %s FOR UPDATE",
                    (run_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise StoreError("unknown run")
                if row["bridge_id"] != bridge_id:
                    raise StoreError("bridge does not own this run")
                run = self._run(row)
                if run.session_id is None:
                    return run
                cur.execute(
                    "SELECT * FROM workbench_resource_leases WHERE resource_key = %s FOR UPDATE",
                    ("worktree:" + str(row["worktree_id"]),),
                )
                lease = cur.fetchone()
                if (
                    lease is None
                    or lease["expires_at"] <= now_utc()
                    or lease["session_id"] != run.session_id
                    or int(lease["epoch"]) != run.lease_epoch
                ):
                    raise StoreError("run worktree lease is stale or no longer owned by its session")
                return run

    def renew_run_lease(self, run_id: str, bridge_id: str, ttl_seconds: int = 300) -> Run:
        """Extend only the active run's exact fenced lease epoch."""
        connection = self._connection()
        with connection.transaction():
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT runs.*, projects.bridge_id, sessions.worktree_id FROM workbench_runs runs "
                    "JOIN workbench_projects projects ON projects.id = runs.project_id "
                    "LEFT JOIN workbench_sessions sessions ON sessions.id = runs.session_id "
                    "WHERE runs.id = %s FOR UPDATE",
                    (run_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise StoreError("unknown run")
                if row["bridge_id"] != bridge_id:
                    raise StoreError("bridge does not own this run")
                run = self._run(row)
                if run.session_id is None:
                    return run
                resource_key = "worktree:" + str(row["worktree_id"])
                cur.execute(
                    "UPDATE workbench_resource_leases SET expires_at = %s "
                    "WHERE resource_key = %s AND session_id = %s AND epoch = %s AND expires_at > %s RETURNING epoch",
                    (now_utc() + timedelta(seconds=ttl_seconds), resource_key, run.session_id, run.lease_epoch, now_utc()),
                )
                if cur.fetchone() is None:
                    raise StoreError("run worktree lease is stale or no longer owned by its session")
        self._append_workflow_event(run.session_id, run.workflow_id, "lease.renewed", {
            "resource_key": resource_key, "epoch": run.lease_epoch,
        })
        return run

    def release_run_lease(self, run_id: str, bridge_id: str) -> Run:
        connection = self._connection()
        with connection.transaction():
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT runs.*, projects.bridge_id, sessions.worktree_id FROM workbench_runs runs "
                    "JOIN workbench_projects projects ON projects.id = runs.project_id "
                    "LEFT JOIN workbench_sessions sessions ON sessions.id = runs.session_id "
                    "WHERE runs.id = %s FOR UPDATE",
                    (run_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise StoreError("unknown run")
                if row["bridge_id"] != bridge_id:
                    raise StoreError("bridge does not own this run")
                run = self._run(row)
                if run.session_id is None:
                    return run
                resource_key = "worktree:" + str(row["worktree_id"])
                cur.execute(
                    "UPDATE workbench_resource_leases SET expires_at = %s "
                    "WHERE resource_key = %s AND session_id = %s AND epoch = %s RETURNING epoch",
                    (now_utc(), resource_key, run.session_id, run.lease_epoch),
                )
                released = cur.fetchone()
        if released is not None:
            self._append_workflow_event(run.session_id, run.workflow_id, "lease.released", {
                "resource_key": resource_key, "epoch": run.lease_epoch,
            })
        return run

    def list_approvals(self, project_id: str | None = None) -> list[Approval]:
        query = "SELECT * FROM workbench_approvals"
        values: tuple[Any, ...] = ()
        if project_id is not None:
            query += " WHERE project_id = %s"
            values = (project_id,)
        query += " ORDER BY created_at DESC"
        with self._connection().cursor() as cur:
            cur.execute(query, values)
            return [self._approval(row) for row in cur.fetchall()]

    def register_bridge(self, project_id: str, name: str) -> tuple[Bridge, str]:
        raw = secrets.token_urlsafe(32)
        bridge = Bridge(new_id("bridge"), project_id, name.strip(), token_hash(raw))
        connection = self._connection()
        with connection.transaction():
            with connection.cursor() as cur:
                cur.execute("SELECT id FROM workbench_projects WHERE id = %s FOR UPDATE", (project_id,))
                if cur.fetchone() is None:
                    raise StoreError("unknown project")
                cur.execute(
                    "INSERT INTO workbench_bridges (id,project_id,name,token_hash,created_at,last_seen_at) VALUES (%s,%s,%s,%s,%s,%s)",
                    (bridge.id, bridge.project_id, bridge.name, bridge.token_hash, bridge.created_at, None),
                )
                cur.execute("UPDATE workbench_projects SET bridge_id = %s WHERE id = %s", (bridge.id, project_id))
        self.append_audit("bridge.registered", "owner", project_id, {"bridge_id": bridge.id, "name": bridge.name})
        return bridge, raw

    def authenticate_bridge(self, bridge_id: str, token: str) -> Bridge:
        with self._connection().cursor() as cur:
            cur.execute("SELECT * FROM workbench_bridges WHERE id = %s", (bridge_id,))
            row = cur.fetchone()
            if row is None or not secrets.compare_digest(row["token_hash"], token_hash(token)):
                raise StoreError("invalid bridge credentials")
            last_seen = now_utc()
            cur.execute("UPDATE workbench_bridges SET last_seen_at = %s WHERE id = %s", (last_seen, bridge_id))
            row["last_seen_at"] = last_seen
            return self._bridge(row)

    def replace_bridge_skills(self, bridge_id: str, skills: list[dict[str, str]]) -> list[BridgeSkill]:
        connection = self._connection()
        now = now_utc()
        clean: list[BridgeSkill] = []
        seen: set[str] = set()
        for value in skills:
            skill_id = str(value.get("skill_id", "")).strip()
            description = str(value.get("description", "")).strip()
            digest = str(value.get("content_sha256", "")).strip()
            if not skill_id or not description or len(digest) != 64 or skill_id in seen:
                raise StoreError("invalid bridge skill metadata")
            seen.add(skill_id)
            clean.append(BridgeSkill(bridge_id, skill_id, description, digest, now))
        with connection.transaction():
            with connection.cursor() as cur:
                cur.execute("SELECT project_id FROM workbench_bridges WHERE id = %s FOR UPDATE", (bridge_id,))
                bridge = cur.fetchone()
                if bridge is None:
                    raise StoreError("unknown bridge")
                cur.execute("DELETE FROM workbench_bridge_skills WHERE bridge_id = %s", (bridge_id,))
                for skill in clean:
                    cur.execute(
                        "INSERT INTO workbench_bridge_skills (bridge_id,skill_id,description,content_sha256,updated_at) VALUES (%s,%s,%s,%s,%s)",
                        (skill.bridge_id, skill.skill_id, skill.description, skill.content_sha256, skill.updated_at),
                    )
        self.append_audit("bridge.skills_published", "bridge:" + bridge_id, bridge["project_id"], {"skill_ids": sorted(seen)})
        return clean

    def list_bridge_skills(self, project_id: str | None = None) -> list[BridgeSkill]:
        query = "SELECT skills.* FROM workbench_bridge_skills skills JOIN workbench_bridges bridges ON bridges.id = skills.bridge_id"
        values: tuple[Any, ...] = ()
        if project_id is not None:
            query += " WHERE bridges.project_id = %s"
            values = (project_id,)
        query += " ORDER BY skills.skill_id"
        with self._connection().cursor() as cur:
            cur.execute(query, values)
            return [self._bridge_skill(row) for row in cur.fetchall()]

    def create_run(
        self,
        project_id: str,
        task_id: str | None,
        model: str,
        session_id: str | None = None,
        workflow_id: str | None = None,
        workflow_step_id: str | None = None,
        lease_epoch: int | None = None,
    ) -> Run:
        """Create a bridge command record with a fenced worktree lease.

        The unique-active-run check is intentionally made while the project
        session is locked.  A browser retry cannot therefore create two Codex
        processes for one session, and separate sessions cannot share an
        unexpired worktree lease.
        """
        lease: ResourceLease | None = None
        run_id = new_id("run")
        connection = self._connection()
        with connection.transaction():
            with connection.cursor() as cur:
                cur.execute("SELECT id FROM workbench_projects WHERE id = %s FOR UPDATE", (project_id,))
                if cur.fetchone() is None:
                    raise StoreError("unknown project")
                if session_id is not None:
                    cur.execute("SELECT * FROM workbench_sessions WHERE id = %s FOR UPDATE", (session_id,))
                    session_row = cur.fetchone()
                    if session_row is None or session_row["project_id"] != project_id or session_row["status"] != "active":
                        raise StoreError("session is not active for this project")
                    cur.execute(
                        "SELECT id FROM workbench_runs WHERE session_id = %s AND status IN ('queued', 'running') FOR UPDATE",
                        (session_id,),
                    )
                    if cur.fetchone() is not None:
                        raise StoreError("a session may have only one active run")
                    resource_key = "worktree:" + str(session_row["worktree_id"])
                    cur.execute("SELECT * FROM workbench_resource_leases WHERE resource_key = %s FOR UPDATE", (resource_key,))
                    lease_row = cur.fetchone()
                    now = now_utc()
                    if lease_row is not None and lease_row["expires_at"] > now and lease_row["session_id"] != session_id:
                        raise StoreError("resource is leased by another active session")
                    lease = ResourceLease(
                        resource_key, session_id, (int(lease_row["epoch"]) + 1) if lease_row else 1,
                        now + timedelta(seconds=300), now,
                    )
                    lease_epoch = lease.epoch
                    cur.execute(
                        "INSERT INTO workbench_resource_leases (resource_key,session_id,epoch,expires_at,created_at) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (resource_key) DO UPDATE SET session_id = EXCLUDED.session_id, epoch = EXCLUDED.epoch, expires_at = EXCLUDED.expires_at, created_at = EXCLUDED.created_at",
                        (lease.resource_key, lease.session_id, lease.epoch, lease.expires_at, lease.created_at),
                    )
                run = Run(
                    run_id, project_id, task_id, model.strip(), "queued",
                    session_id=session_id, workflow_id=workflow_id,
                    workflow_step_id=workflow_step_id, lease_epoch=lease_epoch,
                )
                cur.execute(
                    "INSERT INTO workbench_runs (id,project_id,task_id,model,status,created_at,completed_at,session_id,workflow_id,workflow_step_id,lease_epoch) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        run.id, run.project_id, run.task_id, run.model, run.status,
                        run.created_at, None, run.session_id, run.workflow_id,
                        run.workflow_step_id, run.lease_epoch,
                    ),
                )
        if lease is not None:
            self._append_workflow_event(session_id or "", None, "lease.acquired", {
                "resource_key": lease.resource_key, "epoch": lease.epoch,
            })
        self.append_audit("run.created", "operator", project_id, {
            "run_id": run.id, "task_id": task_id, "model": run.model,
            "session_id": session_id, "workflow_id": workflow_id,
            "lease_epoch": lease_epoch,
        })
        return run

    def update_run_status(self, run_id: str, status: str, bridge_id: str) -> Run:
        if status not in _RUN_STATUS_TRANSITIONS:
            raise StoreError("invalid run status")
        connection = self._connection()
        with connection.transaction():
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT runs.*, projects.bridge_id FROM workbench_runs runs JOIN workbench_projects projects ON projects.id = runs.project_id WHERE runs.id = %s FOR UPDATE",
                    (run_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise StoreError("unknown run")
                if row["bridge_id"] != bridge_id:
                    raise StoreError("bridge does not own this run")
                current = row["status"]
                if status == current:
                    return self._run(row)
                if status not in _RUN_STATUS_TRANSITIONS.get(current, frozenset()):
                    raise StoreError(f"invalid run status transition: {current} -> {status}")
                completed_at = now_utc() if status in {"evidenced", "reconciliation"} else None
                cur.execute(
                    "UPDATE workbench_runs SET status = %s, completed_at = %s WHERE id = %s RETURNING *",
                    (status, completed_at, run_id),
                )
                updated = cur.fetchone()
                if status == "reconciliation" and updated["session_id"] is not None:
                    cur.execute("SELECT worktree_id FROM workbench_sessions WHERE id = %s", (updated["session_id"],))
                    session = cur.fetchone()
                    if session is not None:
                        cur.execute(
                            "UPDATE workbench_resource_leases SET expires_at = %s "
                            "WHERE resource_key = %s AND session_id = %s AND epoch = %s",
                            (completed_at, "worktree:" + str(session["worktree_id"]), updated["session_id"], updated["lease_epoch"]),
                        )
                if status == "reconciliation" and updated["workflow_id"] is not None:
                    cur.execute(
                        "UPDATE workbench_workflows SET status = %s, cursor = %s, updated_at = %s "
                        "WHERE id = %s AND status NOT IN ('completed', 'reconciliation', 'cancelled')",
                        ("reconciliation", self._json([]), completed_at, updated["workflow_id"]),
                    )
        run = self._run(updated)
        if status == "reconciliation" and run.session_id is not None:
            session = self.get_session(run.session_id)
            self._append_workflow_event(session.id, None, "lease.released", {
                "resource_key": "worktree:" + session.worktree_id,
                "epoch": run.lease_epoch,
            })
        if status == "reconciliation" and run.workflow_id is not None and run.session_id is not None:
            self._append_workflow_event(run.session_id, run.workflow_id, "workflow.delivery_finished", {
                "run_id": run.id, "status": status,
            })
        self.append_audit("run.status_changed", "bridge:" + bridge_id, run.project_id, {"run_id": run_id, "status": status})
        return run

    def finalize_run_command(
        self, run_id: str, status: str, bridge_id: str, command_id: str,
    ) -> Run:
        """Commit a terminal run/workflow transition and exact command ack together."""
        if status not in {"evidenced", "reconciliation"}:
            raise StoreError("run finalization status is invalid")
        connection = self._connection()
        with connection.transaction():
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT * FROM workbench_commands WHERE id = %s AND bridge_id = %s FOR UPDATE",
                    (command_id, bridge_id),
                )
                command = cur.fetchone()
                if (
                    command is None or command["action_type"] != "run_codex"
                    or command["payload"].get("run_id") != run_id
                ):
                    raise StoreError("run finalization command does not match this run")
                cur.execute(
                    "SELECT runs.*, projects.bridge_id FROM workbench_runs runs "
                    "JOIN workbench_projects projects ON projects.id = runs.project_id "
                    "WHERE runs.id = %s FOR UPDATE OF runs",
                    (run_id,),
                )
                run_row = cur.fetchone()
                if run_row is None:
                    raise StoreError("unknown run")
                if run_row["bridge_id"] != bridge_id:
                    raise StoreError("bridge does not own this run")
                if run_row["status"] != status and status not in _RUN_STATUS_TRANSITIONS.get(run_row["status"], frozenset()):
                    raise StoreError(f"invalid run status transition: {run_row['status']} -> {status}")

                completed_at = run_row["completed_at"] or now_utc()
                if run_row["status"] != status:
                    cur.execute(
                        "UPDATE workbench_runs SET status = %s, completed_at = %s WHERE id = %s RETURNING *",
                        (status, completed_at, run_id),
                    )
                    updated_row = cur.fetchone()
                else:
                    updated_row = run_row

                pending_events: list[tuple[str, str | None, dict[str, Any]]] = []
                workflow_id = run_row["workflow_id"]
                workflow_step_id = run_row["workflow_step_id"]
                session_id = run_row["session_id"]
                session_row = None
                if session_id is not None:
                    # Serialize the append-only event sequence before any
                    # workflow transition contributes terminal events.
                    cur.execute("SELECT * FROM workbench_sessions WHERE id = %s FOR UPDATE", (session_id,))
                    session_row = cur.fetchone()
                    if session_row is None:
                        raise StoreError("unknown session")
                if status == "evidenced" and workflow_id is not None and workflow_step_id is not None:
                    cur.execute("SELECT * FROM workbench_workflows WHERE id = %s FOR UPDATE", (workflow_id,))
                    workflow_row = cur.fetchone()
                    if workflow_row is None:
                        raise StoreError("unknown workflow")
                    workflow = self._workflow(workflow_row)
                    if workflow_step_id in workflow.cursor:
                        if workflow.status not in {"running", "waiting_approval"}:
                            raise StoreError("workflow step is not runnable")
                        cursor = advance_cursor(workflow.definition, workflow.cursor, workflow_step_id)
                        next_kinds = {
                            step_by_id(workflow.definition, next_step)["kind"] for next_step in cursor
                        }
                        workflow_status = "completed" if not cursor else "waiting_approval" if "approval_wait" in next_kinds else "reconciliation" if "reconcile" in next_kinds else "running"
                        cur.execute(
                            "UPDATE workbench_workflows SET status = %s, cursor = %s, updated_at = %s "
                            "WHERE id = %s",
                            (workflow_status, self._json(list(cursor)), completed_at, workflow_id),
                        )
                        pending_events.append(("workflow.step.finished", workflow_id, {
                            "step_id": workflow_step_id, "outcome": "succeeded",
                            "next": list(cursor), "actor": "bridge:" + bridge_id,
                        }))
                elif status == "reconciliation":
                    if session_id is not None:
                        resource_key = "worktree:" + str(session_row["worktree_id"])
                        cur.execute(
                            "UPDATE workbench_resource_leases SET expires_at = %s "
                            "WHERE resource_key = %s AND session_id = %s AND epoch = %s RETURNING epoch",
                            (completed_at, resource_key, session_id, run_row["lease_epoch"]),
                        )
                        released = cur.fetchone()
                        if released is not None:
                            pending_events.append(("lease.released", workflow_id, {
                                "resource_key": resource_key, "epoch": int(released["epoch"]),
                            }))
                    if workflow_id is not None:
                        cur.execute("SELECT * FROM workbench_workflows WHERE id = %s FOR UPDATE", (workflow_id,))
                        workflow_row = cur.fetchone()
                        if workflow_row is None:
                            raise StoreError("unknown workflow")
                        if workflow_row["status"] not in {"completed", "reconciliation", "cancelled"}:
                            cur.execute(
                                "UPDATE workbench_workflows SET status = 'reconciliation', cursor = %s, updated_at = %s "
                                "WHERE id = %s",
                                (self._json([]), completed_at, workflow_id),
                            )
                            pending_events.append(("workflow.delivery_finished", workflow_id, {
                                "run_id": run_id, "status": "reconciliation",
                            }))

                cur.execute(
                    "DELETE FROM workbench_commands WHERE id = %s AND bridge_id = %s RETURNING id",
                    (command_id, bridge_id),
                )
                if cur.fetchone() is None:
                    raise StoreError("run finalization command does not match this run")
                if session_id is not None and pending_events:
                    cur.execute(
                        "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence "
                        "FROM workbench_workflow_events WHERE session_id = %s",
                        (session_id,),
                    )
                    sequence = int(cur.fetchone()["sequence"])
                    for offset, (kind, event_workflow_id, data) in enumerate(pending_events):
                        cur.execute(
                            "INSERT INTO workbench_workflow_events "
                            "(id,session_id,workflow_id,sequence,kind,data,created_at) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                            (
                                new_id("event"), session_id, event_workflow_id, sequence + offset,
                                kind, self._json(redact_value(data)), completed_at,
                            ),
                        )
                    cur.execute(
                        "UPDATE workbench_sessions SET updated_at = %s WHERE id = %s",
                        (completed_at, session_id),
                    )
                cur.execute(
                    "INSERT INTO workbench_audit (id,kind,actor,project_id,data,created_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (
                        new_id("audit"), "run.finalized", "bridge:" + bridge_id,
                        run_row["project_id"], self._json(redact_value({
                            "run_id": run_id, "status": status, "command_id": command_id,
                        })), completed_at,
                    ),
                )
        return self._run(updated_row)

    def add_transcript(self, run_id: str, role: str, content: Any, bridge_id: str | None = None) -> None:
        clean = redact_value(content)
        with self._connection().cursor() as cur:
            cur.execute(
                "SELECT runs.id, projects.bridge_id FROM workbench_runs runs "
                "JOIN workbench_projects projects ON projects.id = runs.project_id WHERE runs.id = %s",
                (run_id,),
            )
            run = cur.fetchone()
            if run is None:
                raise StoreError("unknown run")
            if bridge_id is not None and run["bridge_id"] != bridge_id:
                raise StoreError("bridge does not own this run")
            cur.execute(
                "INSERT INTO workbench_transcripts (run_id,role,content,created_at) VALUES (%s,%s,%s,%s)",
                (run_id, role, self._json(clean), now_utc()),
            )

    def create_approval(self, project_id: str, action_type: str, payload: dict[str, Any], requested_by: str, ttl_seconds: int, bridge_id: str | None) -> Approval:
        clean = redact_value(payload)
        if action_type not in {"commit_pr", "merge_and_accept"}:
            raise StoreError("approval action is not executable by the v1 bridge")
        if action_type in {"commit_pr", "merge_and_accept"}:
            if bridge_id is None:
                raise StoreError("GitHub delivery approval requires a project bridge")
            run_id = payload.get("run_id")
            session_id = payload.get("session_id")
            worktree_id = payload.get("worktree_id")
            lease_epoch = payload.get("lease_epoch")
            if not all(isinstance(value, str) and value for value in (run_id, session_id, worktree_id)) or not isinstance(lease_epoch, int):
                raise StoreError("GitHub delivery approval must bind run, session, worktree, and lease epoch")
            run = self.validate_run_lease(run_id, bridge_id)
            if run.project_id != project_id or run.session_id != session_id or run.lease_epoch != lease_epoch:
                raise StoreError("GitHub delivery approval binding does not match the active run")
            session = self.get_session(session_id)
            if session.worktree_id != worktree_id or run.status != "evidenced":
                raise StoreError("GitHub delivery approval requires an evidenced run in its leased worktree")
            if action_type == "merge_and_accept":
                if payload.get("task_id") != run.task_id:
                    raise StoreError("merge approval task id must match the evidenced run")
                expected_head_sha = payload.get("expected_head_sha")
                if not isinstance(expected_head_sha, str) or _GIT_HEAD_SHA.fullmatch(expected_head_sha) is None:
                    raise StoreError("merge approval requires the expected pull request head SHA")
        approval = Approval(
            new_id("approval"), project_id, action_type, clean, payload_hash(clean), requested_by,
            now_utc() + timedelta(seconds=ttl_seconds), bridge_id=bridge_id,
        )
        with self._connection().cursor() as cur:
            cur.execute("SELECT id FROM workbench_projects WHERE id = %s", (project_id,))
            if cur.fetchone() is None:
                raise StoreError("unknown project")
            if bridge_id is not None:
                cur.execute("SELECT id FROM workbench_bridges WHERE id = %s", (bridge_id,))
                if cur.fetchone() is None:
                    raise StoreError("unknown bridge")
            cur.execute(
                "INSERT INTO workbench_approvals (id,project_id,action_type,payload,payload_hash,requested_by,expires_at,status,approved_by,approved_at,consumed_at,bridge_id,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (approval.id, approval.project_id, approval.action_type, self._json(approval.payload), approval.payload_hash,
                 approval.requested_by, approval.expires_at, approval.status, None, None, None, approval.bridge_id, approval.created_at),
            )
        self.append_audit("approval.requested", requested_by, project_id, {"approval_id": approval.id, "action_type": action_type, "payload_hash": approval.payload_hash})
        return approval

    def get_approval(self, approval_id: str) -> Approval:
        with self._connection().cursor() as cur:
            cur.execute("SELECT * FROM workbench_approvals WHERE id = %s", (approval_id,))
            row = cur.fetchone()
            if row is None:
                raise StoreError("unknown approval")
            return self._approval(row)

    def approve(self, approval_id: str, actor: str, approvers: frozenset[str]) -> Approval:
        if actor not in approvers:
            raise StoreError("actor is not an allowlisted approver")
        approved_at = now_utc()
        with self._connection().cursor() as cur:
            cur.execute(
                "UPDATE workbench_approvals SET status = 'approved', approved_by = %s, approved_at = %s WHERE id = %s AND status = 'pending' AND expires_at > %s RETURNING *",
                (actor, approved_at, approval_id, approved_at),
            )
            row = cur.fetchone()
        if row is None:
            raise StoreError("approval is not pending or has expired")
        approval = self._approval(row)
        self.append_audit("approval.granted", actor, approval.project_id, {"approval_id": approval.id, "payload_hash": approval.payload_hash})
        return approval

    def consume(self, approval_id: str, action_payload_hash: str) -> Approval:
        approval = self.get_approval(approval_id)
        if approval.action_type in {"commit_pr", "merge_and_accept"}:
            raise StoreError("GitHub delivery approvals require an active run lease")
        consumed_at = now_utc()
        with self._connection().cursor() as cur:
            cur.execute(
                "UPDATE workbench_approvals SET status = 'consumed', consumed_at = %s WHERE id = %s AND status = 'approved' AND expires_at > %s AND payload_hash = %s RETURNING *",
                (consumed_at, approval_id, consumed_at, action_payload_hash),
            )
            row = cur.fetchone()
        if row is None:
            raise StoreError("approval is not valid for this action")
        approval = self._approval(row)
        self.append_audit("approval.consumed", approval.approved_by or "unknown", approval.project_id, {"approval_id": approval.id, "payload_hash": approval.payload_hash})
        return approval

    def consume_approval_for_run(self, approval_id: str, action_payload_hash: str, bridge_id: str) -> Approval:
        """Consume a delivery approval and renew its fenced worktree lease atomically."""
        now = now_utc()
        connection = self._connection()
        with connection.transaction():
            with connection.cursor() as cur:
                cur.execute("SELECT * FROM workbench_approvals WHERE id = %s FOR UPDATE", (approval_id,))
                approval_row = cur.fetchone()
                if approval_row is None:
                    raise StoreError("unknown approval")
                approval = self._approval(approval_row)
                if (
                    approval.action_type not in {"commit_pr", "merge_and_accept"}
                    or approval.bridge_id != bridge_id
                    or approval.status != "approved"
                    or approval.expires_at <= now
                    or not secrets.compare_digest(approval.payload_hash, action_payload_hash)
                ):
                    raise StoreError("approval is not valid for this bridge action")
                binding = approval.payload
                run_id = binding.get("run_id")
                session_id = binding.get("session_id")
                worktree_id = binding.get("worktree_id")
                lease_epoch = binding.get("lease_epoch")
                if not all(isinstance(value, str) and value for value in (run_id, session_id, worktree_id)) or not isinstance(lease_epoch, int):
                    raise StoreError("GitHub delivery approval has an invalid run binding")
                cur.execute(
                    "SELECT runs.*, projects.bridge_id, sessions.worktree_id FROM workbench_runs runs "
                    "JOIN workbench_projects projects ON projects.id = runs.project_id "
                    "LEFT JOIN workbench_sessions sessions ON sessions.id = runs.session_id "
                    "WHERE runs.id = %s FOR UPDATE",
                    (run_id,),
                )
                run_row = cur.fetchone()
                if (
                    run_row is None
                    or run_row["bridge_id"] != bridge_id
                    or run_row["project_id"] != approval.project_id
                    or run_row["status"] != "evidenced"
                    or run_row["session_id"] != session_id
                    or run_row["worktree_id"] != worktree_id
                    or run_row["lease_epoch"] != lease_epoch
                ):
                    raise StoreError("GitHub delivery approval binding no longer matches the active evidenced run")
                if approval.action_type == "merge_and_accept" and binding.get("task_id") != run_row["task_id"]:
                    raise StoreError("merge approval task id no longer matches the evidenced run")
                resource_key = "worktree:" + str(worktree_id)
                cur.execute("SELECT * FROM workbench_resource_leases WHERE resource_key = %s FOR UPDATE", (resource_key,))
                lease = cur.fetchone()
                if (
                    lease is None
                    or lease["expires_at"] <= now
                    or lease["session_id"] != session_id
                    or int(lease["epoch"]) != lease_epoch
                ):
                    raise StoreError("run worktree lease is stale or no longer owned by its session")
                cur.execute(
                    "UPDATE workbench_resource_leases SET expires_at = %s WHERE resource_key = %s AND session_id = %s AND epoch = %s",
                    (now + timedelta(seconds=300), resource_key, session_id, lease_epoch),
                )
                cur.execute(
                    "UPDATE workbench_approvals SET status = 'consumed', consumed_at = %s "
                    "WHERE id = %s AND status = 'approved' AND expires_at > %s RETURNING *",
                    (now, approval_id, now),
                )
                consumed_row = cur.fetchone()
                if consumed_row is None:
                    raise StoreError("approval is not valid for execution")
        consumed = self._approval(consumed_row)
        self._append_workflow_event(session_id, run_row["workflow_id"], "lease.renewed", {
            "resource_key": resource_key, "epoch": lease_epoch,
        })
        self.append_audit("approval.consumed", consumed.approved_by or "unknown", consumed.project_id, {
            "approval_id": consumed.id, "payload_hash": consumed.payload_hash,
        })
        return consumed

    def complete_approved_merge(
        self, approval_id: str, action_payload_hash: str, bridge_id: str, command_id: str | None = None,
    ) -> Run:
        """Finalize a consumed merge grant only while its exact lease is live."""
        now = now_utc()
        connection = self._connection()
        with connection.transaction():
            with connection.cursor() as cur:
                cur.execute("SELECT * FROM workbench_approvals WHERE id = %s FOR UPDATE", (approval_id,))
                approval_row = cur.fetchone()
                if approval_row is None:
                    raise StoreError("unknown approval")
                approval = self._approval(approval_row)
                if (
                    approval.action_type != "merge_and_accept"
                    or approval.bridge_id != bridge_id
                    or approval.status != "consumed"
                    or not secrets.compare_digest(approval.payload_hash, action_payload_hash)
                ):
                    raise StoreError("merge approval is not valid for delivery completion")
                binding = approval.payload
                run_id = binding.get("run_id")
                session_id = binding.get("session_id")
                worktree_id = binding.get("worktree_id")
                lease_epoch = binding.get("lease_epoch")
                if not all(isinstance(value, str) and value for value in (run_id, session_id, worktree_id)) or not isinstance(lease_epoch, int):
                    raise StoreError("merge approval has an invalid run binding")
                cur.execute(
                    "SELECT runs.*, projects.bridge_id, sessions.worktree_id FROM workbench_runs runs "
                    "JOIN workbench_projects projects ON projects.id = runs.project_id "
                    "LEFT JOIN workbench_sessions sessions ON sessions.id = runs.session_id "
                    "WHERE runs.id = %s FOR UPDATE",
                    (run_id,),
                )
                run_row = cur.fetchone()
                if (
                    run_row is None
                    or run_row["bridge_id"] != bridge_id
                    or run_row["project_id"] != approval.project_id
                    or run_row["status"] != "evidenced"
                    or run_row["session_id"] != session_id
                    or run_row["worktree_id"] != worktree_id
                    or run_row["lease_epoch"] != lease_epoch
                ):
                    raise StoreError("merge approval binding no longer matches the active evidenced run")
                if binding.get("task_id") != run_row["task_id"]:
                    raise StoreError("merge approval task id no longer matches the evidenced run")
                resource_key = "worktree:" + str(worktree_id)
                cur.execute("SELECT * FROM workbench_resource_leases WHERE resource_key = %s FOR UPDATE", (resource_key,))
                lease = cur.fetchone()
                if (
                    lease is None
                    or lease["expires_at"] <= now
                    or lease["session_id"] != session_id
                    or int(lease["epoch"]) != lease_epoch
                ):
                    raise StoreError("run worktree lease is stale or no longer owned by its session")
                cur.execute(
                    "UPDATE workbench_runs SET status = 'completed', completed_at = %s WHERE id = %s RETURNING *",
                    (now, run_id),
                )
                completed_row = cur.fetchone()
                cur.execute(
                    "UPDATE workbench_resource_leases SET expires_at = %s WHERE resource_key = %s AND session_id = %s AND epoch = %s",
                    (now, resource_key, session_id, lease_epoch),
                )
                workflow_id = completed_row["workflow_id"]
                if workflow_id is not None:
                    cur.execute(
                        "UPDATE workbench_workflows SET status = 'completed', cursor = %s, updated_at = %s "
                        "WHERE id = %s AND status NOT IN ('completed', 'reconciliation', 'cancelled')",
                        (self._json([]), now, workflow_id),
                    )
                if command_id is not None:
                    cur.execute(
                        "DELETE FROM workbench_commands WHERE id = %s AND bridge_id = %s AND approval_id = %s RETURNING id",
                        (command_id, bridge_id, approval_id),
                    )
                    if cur.fetchone() is None:
                        raise StoreError("merge completion command is not owned by this approval")
        completed = self._run(completed_row)
        self._append_workflow_event(session_id, completed.workflow_id, "lease.released", {
            "resource_key": resource_key, "epoch": lease_epoch,
        })
        if completed.workflow_id is not None:
            self._append_workflow_event(session_id, completed.workflow_id, "workflow.delivery_finished", {
                "run_id": completed.id, "status": "completed",
            })
        self.append_audit("run.completed", "bridge:" + bridge_id, completed.project_id, {
            "run_id": completed.id, "approval_id": approval_id,
        })
        return completed

    def enqueue_command(self, bridge_id: str, approval: Approval) -> None:
        if approval.status != "approved":
            raise StoreError("only approved actions may be queued")
        command_id = new_id("command")
        with self._connection().cursor() as cur:
            cur.execute(
                "INSERT INTO workbench_commands (id,bridge_id,approval_id,action_type,payload,payload_hash,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (command_id, bridge_id, approval.id, approval.action_type, self._json(approval.payload), approval.payload_hash, now_utc()),
            )

    def enqueue_run(self, bridge_id: str, run: Run) -> None:
        session = self.get_session(run.session_id) if run.session_id else None
        skills: list[dict[str, str]] = []
        if run.workflow_id and run.workflow_step_id:
            step = step_by_id(self.get_workflow(run.workflow_id).definition, run.workflow_step_id)
            requested = list(step.get("skills", []))
            if requested:
                published = {
                    skill.skill_id: skill for skill in self.list_bridge_skills(run.project_id)
                    if skill.bridge_id == bridge_id
                }
                for skill_id in requested:
                    skill = published.get(skill_id)
                    if skill is None:
                        raise StoreError(f"workflow skill is not published by this bridge: {skill_id}")
                    skills.append({
                        "skill_id": skill.skill_id,
                        "description": skill.description,
                        "content_sha256": skill.content_sha256,
                    })
        directives: list[str] = []
        directive_high_water = 0
        if run.session_id:
            with self._connection().cursor() as cur:
                cur.execute(
                    "SELECT data, sequence FROM workbench_workflow_events WHERE session_id = %s AND kind = 'operator.directive' ORDER BY sequence",
                    (run.session_id,),
                )
                directive_rows = [row for row in cur.fetchall() if isinstance(row["data"], dict)]
            included_rows = directive_rows[-32:]
            directives = [str(row["data"].get("content", "")) for row in included_rows]
            directive_high_water = int(included_rows[-1]["sequence"]) if included_rows else 0
        payload = {
            "run_id": run.id, "project_id": run.project_id, "task_id": run.task_id,
            "model": run.model, "session_id": run.session_id,
            "workflow_id": run.workflow_id, "workflow_step_id": run.workflow_step_id,
            "lease_epoch": run.lease_epoch,
            "worktree_id": session.worktree_id if session else None,
            "skills": skills,
            "directives": directives,
        }
        with self._connection().cursor() as cur:
            cur.execute("SELECT id FROM workbench_bridges WHERE id = %s", (bridge_id,))
            if cur.fetchone() is None:
                raise StoreError("unknown bridge")
            cur.execute(
                "INSERT INTO workbench_commands (id,bridge_id,approval_id,action_type,payload,payload_hash,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (new_id("command"), bridge_id, None, "run_codex", self._json(payload), payload_hash(payload), now_utc()),
            )
        # Record the packet-inclusion marker so a directive carried in this queued
        # packet reports INCLUDED via session_directive_view (T008 criterion 2).
        if run.session_id and directive_high_water > 0:
            self._append_workflow_event(run.session_id, run.workflow_id, "operator.directive_packet", {
                "included_up_to_sequence": directive_high_water,
            })

    def enqueue_skill_probe(self, bridge_id: str) -> None:
        with self._connection().cursor() as cur:
            cur.execute("SELECT project_id FROM workbench_bridges WHERE id = %s", (bridge_id,))
            bridge = cur.fetchone()
        if bridge is None:
            raise StoreError("unknown bridge")
        skills = [skill for skill in self.list_bridge_skills(bridge["project_id"]) if skill.bridge_id == bridge_id]
        if not skills:
            raise StoreError("the bridge has not published any configured skills")
        payload = {"project_id": bridge["project_id"], "skills": [
            {"skill_id": skill.skill_id, "content_sha256": skill.content_sha256} for skill in skills
        ]}
        with self._connection().cursor() as cur:
            cur.execute(
                "INSERT INTO workbench_commands (id,bridge_id,approval_id,action_type,payload,payload_hash,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (new_id("command"), bridge_id, None, "skill_probe", self._json(payload), payload_hash(payload), now_utc()),
            )

    def next_command(self, bridge_id: str) -> dict[str, Any] | None:
        lease_expires_at = now_utc() + timedelta(minutes=5)
        connection = self._connection()
        with connection.transaction():
            with connection.cursor() as cur:
                cur.execute(
                    "WITH next AS ("
                    " SELECT id FROM workbench_commands WHERE bridge_id = %s "
                    " AND (lease_expires_at IS NULL OR lease_expires_at <= %s) "
                    " ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1"
                    ") UPDATE workbench_commands command "
                    "SET delivery_attempts = command.delivery_attempts + 1, lease_expires_at = %s "
                    "FROM next WHERE command.id = next.id "
                    "RETURNING command.id,command.approval_id,command.action_type,command.payload,command.payload_hash,command.delivery_attempts",
                    (bridge_id, now_utc(), lease_expires_at),
                )
                return cur.fetchone()

    def acknowledge_command(self, bridge_id: str, command_id: str) -> None:
        with self._connection().cursor() as cur:
            cur.execute(
                "DELETE FROM workbench_commands WHERE id = %s AND bridge_id = %s "
                "AND action_type <> 'run_codex' RETURNING id",
                (command_id, bridge_id),
            )
            if cur.fetchone() is None:
                cur.execute(
                    "SELECT action_type FROM workbench_commands WHERE id = %s AND bridge_id = %s",
                    (command_id, bridge_id),
                )
                command = cur.fetchone()
                if command is not None and command["action_type"] == "run_codex":
                    raise StoreError("run commands must be acknowledged through terminal finalization")
                raise StoreError("unknown bridge command")


# Backward-compat re-exports: the per-domain stores were extracted into their own
# modules (operation_store, skill_adoption_store, preference_store,
# plugin_preference_store); re-exported here so existing imports keep working.
from .operation_store import (OperationReceiptStoreError, UnknownOutcomeError, OperationOutcome, OperationReceiptRows, MemoryOperationReceiptStore, OperationApprovalGrant, MemoryOperationApprovalStore)  # noqa: E402,F401
from .skill_adoption_store import (SkillAdoptionStoreError, SkillAdoptionRecord, SkillAdoptionRows, MemorySkillAdoptionStore)  # noqa: E402,F401
from .preference_store import (PreferenceStoreError, UnknownPreferenceError, StalePreferenceWriteError, PreferenceRows, MemoryPreferenceStore)  # noqa: E402,F401
from .plugin_preference_store import (PluginPreferenceStoreError, PluginPreferenceRows, MemoryPluginPreferenceService, resolve_plugin_tool_preferences)  # noqa: E402,F401
