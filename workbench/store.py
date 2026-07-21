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
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Protocol

from .contracts import validate_operation_receipt
from .models import (
    Approval, AuditEvent, Bridge, BridgeSkill, EffectiveValue, OperationRef, OperationReceipt,
    OperationRefusal, PreferenceRecord, PreferenceValidationError, Project,
    RECONCILIATION_REASONS, ReconciliationItem, ResourceLease,
    Run, Session, Workflow, WorkflowEvent,
    new_id, new_receipt_id, new_reconciliation_id, now_utc,
    resolve_effective_settings, reviewed_catalog_valid_refs,
    validate_setting_value,
)
from .redaction import redact_config_text, redact_value
from .workflows import WorkflowError, advance_cursor, step_by_id, validate_definition


class StoreError(RuntimeError):
    """A requested Workbench operation violates an immutable audit invariant."""


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


# ---------------------------------------------------------------------------
# Idempotent typed operation receipts and reconciliation records
# (state-context-operations:T006.3)
# ---------------------------------------------------------------------------
#
# Every typed operation attempt must reach a durable terminal: a redacted typed
# receipt or, when an external effect's outcome is UNKNOWN, exactly one durable
# reconciliation item.  The discipline mirrors ``MemoryIdempotencyStore``:
#
# * The idempotency key is the dedup identity.  A key with a stored receipt
#   replays that receipt WITHOUT re-executing the effect (criterion 3), and the
#   whole check-execute-store runs under the instance lock, so two concurrent
#   same-key attempts resolve to exactly ONE record.
# * A ``succeeded`` outcome and an ``unknown`` outcome are the two PERSISTED
#   terminals: an effect happened, or one may have happened and must be
#   reconciled -- a replay of either returns the stored receipt and never
#   repeats the effect (criterion 4).  An ``unknown`` outcome also files exactly
#   one reconciliation item.
# * A ``failed`` or ``denied`` (pre-effect) outcome returns a typed receipt for
#   the attempt but is NOT stored under the key, so a genuine transient failure
#   stays retriable and never fabricates a stored success (criterion 4).  Any
#   OTHER exception from the executor propagates and stores nothing.
# * Every receipt is redacted and validated against ``operation-receipt.v1``
#   before it is returned or persisted, so no secret or raw credential can ride
#   in a receipt or reconciliation record (criterion 2).


class OperationReceiptStoreError(StoreError):
    """A typed operation receipt or reconciliation record could not be persisted."""


class UnknownOutcomeError(RuntimeError):
    """Signal that an external operation effect's outcome is UNKNOWN.

    Raised by an executor when it cannot confirm whether the effect took hold
    (an interrupted external call, an ambiguous provider result).  The store
    turns it into a durable reconciliation item and a ``reconciliation_required``
    receipt; the effect is never silently retried.
    """

    def __init__(
        self, safe_summary: str = "the external operation outcome is unknown",
        *, external_ref: Mapping[str, str] | None = None, reason: str = "unknown_outcome",
    ) -> None:
        self.safe_summary = safe_summary
        self.external_ref = dict(external_ref or {})
        self.reason = reason
        super().__init__(safe_summary)


@dataclass(frozen=True)
class OperationOutcome:
    """The classified result an operation executor returns to the receipt store."""

    status: str  # succeeded | failed | denied | unknown
    external_ref: Mapping[str, str] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    error: OperationRefusal | None = None
    reconciliation_reason: str = "unknown_outcome"

    def __post_init__(self) -> None:
        if self.status not in {"succeeded", "failed", "denied", "unknown"}:
            raise OperationReceiptStoreError(f"invalid operation outcome status: {self.status!r}")


@dataclass
class OperationReceiptRows:
    """The persisted row container shared by receipt-store instances."""

    receipts: dict[str, OperationReceipt] = field(default_factory=dict)
    reconciliations: dict[str, ReconciliationItem] = field(default_factory=dict)


class MemoryOperationReceiptStore:
    """Hermetic, lock-serialized idempotent typed-receipt + reconciliation store."""

    def __init__(self, rows: OperationReceiptRows | None = None) -> None:
        # The whole check-execute-store path runs under this reentrant lock so two
        # concurrent same-key attempts cannot both execute the effect: the first
        # commits the receipt, the second observes it and replays.
        self._lock = threading.RLock()
        self.rows = rows if rows is not None else OperationReceiptRows()

    def record_attempt(
        self,
        *,
        run_id: str,
        command_id: str,
        operation: OperationRef,
        idempotency_key: str,
        executor: Callable[[], OperationOutcome],
        task_ref: str | None = None,
        request_id: str | None = None,
        unknown_summary: str = "the external operation outcome is unknown",
    ) -> tuple[dict[str, Any], bool]:
        """Execute one operation at most once per idempotency key and record it.

        Returns ``(receipt_dict, replayed)``.  A key that already has a stored
        terminal receipt (``succeeded`` or ``reconciliation_required``) replays
        it with ``replayed=True`` and never re-executes.  Otherwise the executor
        runs once and its outcome is turned into a redacted, schema-validated
        receipt; a ``succeeded``/``unknown`` outcome is persisted (an ``unknown``
        outcome also files exactly one reconciliation item), while a
        ``failed``/``denied`` outcome returns a receipt but stays retriable.
        """
        if not isinstance(operation, OperationRef):
            raise OperationReceiptStoreError("record_attempt requires an OperationRef")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise OperationReceiptStoreError("record_attempt requires an idempotency key")
        with self._lock:
            existing = self.rows.receipts.get(idempotency_key)
            if existing is not None:
                return existing.as_dict(), True
            started = now_utc()
            try:
                outcome = executor()
            except UnknownOutcomeError as exc:
                outcome = OperationOutcome(
                    status="unknown", external_ref=exc.external_ref, reconciliation_reason=exc.reason,
                )
                unknown_summary = exc.safe_summary
            # Any OTHER exception is not caught here: it propagates, nothing is
            # stored, and the attempt stays retriable (no fabricated success).
            if not isinstance(outcome, OperationOutcome):
                raise OperationReceiptStoreError("an operation executor must return an OperationOutcome")
            finished = now_utc()
            status = outcome.status
            if status == "succeeded":
                receipt = OperationReceipt(
                    new_receipt_id(), command_id, run_id, operation, "succeeded",
                    idempotency_key, started, finished, redaction_status="redacted",
                    external_ref=outcome.external_ref, evidence_refs=outcome.evidence_refs,
                    task_ref=task_ref, request_id=request_id,
                )
                validate_operation_receipt(receipt.as_dict())
                self.rows.receipts[idempotency_key] = receipt
                return receipt.as_dict(), False
            if status in ("failed", "denied"):
                if outcome.error is None:
                    raise OperationReceiptStoreError(f"a {status} outcome must carry a typed refusal")
                receipt = OperationReceipt(
                    new_receipt_id(), command_id, run_id, operation, status,
                    idempotency_key, started, finished,
                    redaction_status="metadata_only" if status == "denied" else "redacted",
                    error=outcome.error, task_ref=task_ref, request_id=request_id,
                )
                validate_operation_receipt(receipt.as_dict())
                # Deliberately NOT persisted under the idempotency key: a
                # pre-terminal failed/denied attempt stays retriable.
                return receipt.as_dict(), False
            # status == "unknown"
            reason = outcome.reconciliation_reason if outcome.reconciliation_reason in RECONCILIATION_REASONS else "unknown_outcome"
            item = ReconciliationItem(
                new_reconciliation_id(), run_id, command_id, operation, reason,
                idempotency_key, unknown_summary, external_ref=outcome.external_ref,
            )
            receipt = OperationReceipt(
                new_receipt_id(), command_id, run_id, operation, "reconciliation_required",
                idempotency_key, started, finished, redaction_status="redacted",
                external_ref=outcome.external_ref, task_ref=task_ref, request_id=request_id,
            )
            validate_operation_receipt(receipt.as_dict())
            # Persist BOTH so a replay returns the reconciliation receipt and the
            # unknown external effect is never silently retried; exactly one item
            # per key because the whole path holds the lock and the key is unique.
            self.rows.reconciliations[idempotency_key] = item
            self.rows.receipts[idempotency_key] = receipt
            return receipt.as_dict(), False

    def get_receipt(self, idempotency_key: str) -> dict[str, Any] | None:
        with self._lock:
            receipt = self.rows.receipts.get(idempotency_key)
            return receipt.as_dict() if receipt is not None else None

    def get_reconciliation(self, idempotency_key: str) -> dict[str, Any] | None:
        with self._lock:
            item = self.rows.reconciliations.get(idempotency_key)
            return item.as_dict() if item is not None else None

    def list_reconciliations(self, run_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return [
                item.as_dict() for item in self.rows.reconciliations.values()
                if run_id is None or item.run_id == run_id
            ]


@dataclass(frozen=True)
class OperationApprovalGrant:
    """One hash-bound, one-time approval grant for an approval-gated operation."""

    grant_id: str
    action: str
    payload_hash: str
    bridge_id: str
    project_id: str
    expires_at: datetime
    consumed_at: datetime | None = None


class MemoryOperationApprovalStore:
    """Hermetic one-time approval consumer for the typed operation preflight.

    Implements the :class:`workbench.contracts.ApprovalConsumer` protocol.  A
    grant is bound to an exact ``(action, payload_hash, bridge_id, project_id)``
    and consumed at most once: a replayed grant, an expired grant, a payload-hash
    mismatch (constant-time compare), or a cross-bridge/cross-project attempt
    fails closed.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.grants: dict[str, OperationApprovalGrant] = {}

    def grant(
        self, grant_id: str, action: str, payload_hash: str, bridge_id: str, project_id: str,
        ttl_seconds: int = 300,
    ) -> OperationApprovalGrant:
        with self._lock:
            if grant_id in self.grants:
                raise OperationReceiptStoreError("approval grant id already exists")
            grant = OperationApprovalGrant(
                grant_id, action, payload_hash, bridge_id, project_id,
                now_utc() + timedelta(seconds=ttl_seconds),
            )
            self.grants[grant_id] = grant
            return grant

    def consume(
        self, grant_id: str, action: str, payload_hash: str, bridge_id: str, project_id: str,
    ) -> None:
        with self._lock:
            grant = self.grants.get(grant_id)
            if grant is None:
                raise OperationReceiptStoreError("approval grant is unknown")
            if grant.consumed_at is not None:
                raise OperationReceiptStoreError("approval grant was already consumed (replay refused)")
            if now_utc() >= grant.expires_at:
                raise OperationReceiptStoreError("approval grant expired")
            if grant.bridge_id != bridge_id or grant.project_id != project_id:
                raise OperationReceiptStoreError("approval grant is not bound to this bridge and project")
            if grant.action != action:
                raise OperationReceiptStoreError("approval action does not match the grant")
            if not secrets.compare_digest(grant.payload_hash, payload_hash):
                raise OperationReceiptStoreError("approval payload hash does not match the grant")
            self.grants[grant_id] = replace(grant, consumed_at=now_utc())


# ---------------------------------------------------------------------------
# Owner skill-digest adoption ledger (reviewed-tools-plugins: T008)
# ---------------------------------------------------------------------------


class SkillAdoptionStoreError(StoreError):
    """A skill acknowledgment violated its safe-metadata or digest contract."""


_ADOPTION_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_ADOPTION_CONTENT_SHA = re.compile(r"^[a-f0-9]{64}$")
_ADOPTION_MAX_DESCRIPTION = 500


@dataclass(frozen=True)
class SkillAdoptionRecord:
    """A durable owner acknowledgment that a skill was reviewed AT ONE digest.

    Carries the skill id, the exact acknowledged ``sha256:`` digest, and SAFE
    metadata only -- a short scrubbed description and the bare content hash.
    There is deliberately NO ``instructions``/body field and NO local ``path``
    field: a skill body or filesystem path is not representable in the record,
    so neither can ever enter the acknowledgment ledger or a browser projection
    built from it (T008: records carry digest + safe metadata only).
    """

    skill_id: str
    digest: str
    description: str
    content_sha256: str
    acknowledged_by: str
    acknowledged_at: datetime

    def metadata(self) -> dict[str, str]:
        """Digest + safe metadata projection -- never a body or a path."""
        return {
            "skill_id": self.skill_id,
            "digest": self.digest,
            "description": self.description,
            "content_sha256": self.content_sha256,
            "acknowledged_by": self.acknowledged_by,
            "acknowledged_at": self.acknowledged_at.isoformat(),
        }


@dataclass
class SkillAdoptionRows:
    """The persisted acknowledgment container, keyed by skill id.

    Exactly one acknowledgment per skill id pins the exact digest the owner
    reviewed; a fresh acknowledgment of a different digest replaces it.  Handing
    the same rows to a fresh :class:`MemorySkillAdoptionStore` simulates a hub
    restart over the same ledger.
    """

    records: dict[str, SkillAdoptionRecord] = field(default_factory=dict)


class MemorySkillAdoptionStore:
    """Hermetic owner acknowledgment ledger for reviewed skill digests (T008).

    One acknowledgment record per skill id pins the EXACT digest the owner
    reviewed.  Acknowledging one digest never implicitly acknowledges a later
    change: a query for a different digest reports ``digest_changed`` (a prior
    acknowledgment exists at another digest) or ``unacknowledged`` (no
    acknowledgment at all), so a changed skill body always requires a fresh
    acknowledgment.  Every stored record and every projection carries the digest
    and SAFE metadata only -- never a skill body or a local path; a description
    is scrubbed through :func:`~workbench.redaction.redact_config_text` and
    bounded before it is stored, so even a mis-supplied description cannot ferry
    a path or a credential into the ledger.
    """

    def __init__(self, rows: SkillAdoptionRows | None = None) -> None:
        self._lock = threading.RLock()
        self.rows = rows if rows is not None else SkillAdoptionRows()

    def acknowledge(
        self,
        skill_id: str,
        digest: str,
        *,
        description: str = "",
        content_sha256: str = "",
        acknowledged_by: str = "operator",
    ) -> SkillAdoptionRecord:
        """Acknowledge one skill at one exact reviewed digest, storing safe metadata."""
        with self._lock:
            skill_id = str(skill_id)
            digest = str(digest)
            if not skill_id:
                raise SkillAdoptionStoreError("a skill acknowledgment requires a skill id")
            if not _ADOPTION_DIGEST.fullmatch(digest):
                raise SkillAdoptionStoreError("a skill acknowledgment requires a sha256: digest")
            content_sha256 = str(content_sha256)
            if content_sha256 and not _ADOPTION_CONTENT_SHA.fullmatch(content_sha256):
                raise SkillAdoptionStoreError("a skill acknowledgment content hash must be a bare sha256 hex")
            record = SkillAdoptionRecord(
                skill_id=skill_id,
                digest=digest,
                # Scrub + bound the description so a body/path/credential shape can
                # never enter the ledger even through a mis-supplied field.
                description=redact_config_text(str(description)).strip()[:_ADOPTION_MAX_DESCRIPTION],
                content_sha256=content_sha256,
                acknowledged_by=str(acknowledged_by),
                acknowledged_at=now_utc(),
            )
            self.rows.records[skill_id] = record
            return record

    def acknowledgment_status(self, skill_id: str, digest: str) -> str:
        """Return ``acknowledged`` / ``digest_changed`` / ``unacknowledged``.

        ``digest_changed`` means a prior acknowledgment exists for the skill but
        at a DIFFERENT digest (a re-acknowledgment is required); ``unacknowledged``
        means no acknowledgment exists for the skill at all.
        """
        with self._lock:
            record = self.rows.records.get(str(skill_id))
            if record is None:
                return "unacknowledged"
            if not secrets.compare_digest(record.digest, str(digest)):
                return "digest_changed"
            return "acknowledged"

    def is_acknowledged(self, skill_id: str, digest: str) -> bool:
        return self.acknowledgment_status(skill_id, digest) == "acknowledged"

    def get(self, skill_id: str) -> dict[str, str] | None:
        """The safe-metadata projection for one skill's acknowledgment, or None."""
        with self._lock:
            record = self.rows.records.get(str(skill_id))
            return record.metadata() if record is not None else None

    def list_acknowledgments(self) -> list[dict[str, str]]:
        """Every acknowledgment's safe-metadata projection, id-sorted."""
        with self._lock:
            return [self.rows.records[key].metadata() for key in sorted(self.rows.records)]


# ---------------------------------------------------------------------------
# Scoped durable preference storage + stale-write rejection
# (preferences-configuration: T002.2)
# ---------------------------------------------------------------------------


class PreferenceStoreError(StoreError):
    """A preference store operation violates its scoping or concurrency contract."""


class UnknownPreferenceError(PreferenceStoreError):
    """No such stored preference for this scope.

    Raised identically for a genuinely missing preference and for another
    actor's or project's preference, so a cross-scope probe can never learn
    whether the record exists — the indistinct not-found mirrors the
    run-context and project-context stores.
    """


class StalePreferenceWriteError(PreferenceStoreError):
    """An optimistic write lost a version race; the caller must reload.

    Deliberately distinct from :class:`~workbench.models.PreferenceValidationError`
    (a malformed value): a stale write is a reload-required conflict, not a bad
    request, and the stored value is left unchanged.  Carries the current stored
    ``write_version`` so the caller can reload and retry against it.
    """

    def __init__(self, current_version: int) -> None:
        self.current_version = current_version
        self.reload_required = True
        super().__init__("a newer version exists; reload required before writing")


@dataclass
class PreferenceRows:
    """The persisted row container shared by preference-store instances.

    ``records`` maps ``(scope, scope_key) -> {setting_id -> PreferenceRecord}``.
    The ``(scope, scope_key)`` namespace is the hard cross-scope boundary:
    ``(personal, alice)`` and ``(personal, bob)`` and ``(project, proj_1)`` are
    three disjoint namespaces.  Handing the same rows to a fresh
    :class:`MemoryPreferenceStore` simulates a hub restart over the same records.
    """

    records: dict[tuple[str, str], dict[str, PreferenceRecord]] = field(default_factory=dict)


class MemoryPreferenceStore:
    """Hermetic, lock-serialized scoped preference store with stale-write rejection.

    Every mutation runs the read-current-version check and the write under one
    reentrant lock, so two concurrent same-version writers cannot both commit:
    the first increments the version, the second observes it and is rejected as
    stale.  A setting is writable only at the scope its descriptor owns, so a
    personal actor can never write a project/deployment/policy value, and each
    ``(scope, scope_key)`` namespace is isolated so a cross-actor or cross-project
    read returns the indistinct not-found.
    """

    def __init__(self, catalog: Mapping[str, Any], rows: PreferenceRows | None = None) -> None:
        self._lock = threading.RLock()
        self.catalog = catalog
        self._by_id: dict[str, Mapping[str, Any]] = {
            str(setting.get("id")): setting
            for setting in catalog.get("settings", [])
            if isinstance(setting, Mapping)
        }
        self.rows = rows if rows is not None else PreferenceRows()

    @staticmethod
    def _require_scope(scope: str, scope_key: str) -> tuple[str, str]:
        if scope not in ("personal", "project", "deployment", "policy"):
            raise PreferenceStoreError(f"unknown preference scope: {scope!r}")
        if not isinstance(scope_key, str) or not scope_key:
            raise PreferenceStoreError("a preference operation requires a scope key")
        return scope, scope_key

    def _descriptor(self, setting_id: str) -> Mapping[str, Any]:
        descriptor = self._by_id.get(setting_id)
        if descriptor is None:
            raise UnknownPreferenceError("unknown preference")
        return descriptor

    def _writable_descriptor(self, scope: str, setting_id: str) -> Mapping[str, Any]:
        """Return the descriptor only if this scope may write it; else fail closed."""
        descriptor = self._descriptor(setting_id)
        if descriptor.get("scope") != scope:
            # A setting is owned by exactly one scope. Writing it from another
            # scope is a cross-scope write attempt and must be INDISTINGUISHABLE
            # from an unknown id: raising a distinct "not owned by this scope"
            # error made the write surface an existence oracle (a probe could
            # tell that an authority setting id exists — the very ids the read
            # surface hides). Raise the SAME indistinct not-found so a
            # cross-scope write leaks neither the id's existence nor its value.
            raise UnknownPreferenceError("unknown preference")
        if descriptor.get("mutability") == "env_only":
            raise PreferenceStoreError("setting is environment-managed and not writable through the store")
        if descriptor.get("mutability") == "approval_gated":
            # An approval-gated setting (a policy) requires a bound, consumed
            # approval before it commits. That approval layer is not wired into
            # this store, so a direct actor write must FAIL CLOSED rather than
            # commit unapproved. Authority values are seeded via
            # :meth:`seed_authority_value`, which represents the already-approved
            # / environment-derived write, never an actor-proposed one.
            raise PreferenceStoreError("setting is approval-gated and cannot be written without an approval")
        return descriptor

    def get(self, scope: str, scope_key: str, setting_id: str) -> PreferenceRecord:
        """Return one stored preference record, or the indistinct not-found.

        A record in another actor's or project's namespace is not in this
        namespace, so a cross-scope read raises the same
        :class:`UnknownPreferenceError` a genuinely missing record raises.
        """
        scope, scope_key = self._require_scope(scope, scope_key)
        namespace = self.rows.records.get((scope, scope_key))
        if namespace is None or setting_id not in namespace:
            raise UnknownPreferenceError("unknown preference")
        return namespace[setting_id]

    def current_version(self, scope: str, scope_key: str, setting_id: str) -> int:
        """The stored write version for a setting in this namespace, or 0 if unset."""
        scope, scope_key = self._require_scope(scope, scope_key)
        namespace = self.rows.records.get((scope, scope_key))
        record = namespace.get(setting_id) if namespace is not None else None
        return record.write_version if record is not None else 0

    def stored_values(self, scope: str, scope_key: str) -> dict[str, Any]:
        """The ``{setting_id: value}`` map for one namespace, for the resolver.

        Returns only this ``(scope, scope_key)`` namespace's own values; the
        caller merges the scopes it is authorized to read.
        """
        scope, scope_key = self._require_scope(scope, scope_key)
        namespace = self.rows.records.get((scope, scope_key), {})
        return {setting_id: record.value for setting_id, record in namespace.items()}

    def owned_values(self, scope: str, scope_key: str) -> dict[str, Any]:
        """Namespace values RESTRICTED to setting ids this scope actually owns.

        A durable row is keyed by ``(scope, scope_key)`` but its ``setting_id``
        is not otherwise pinned to that scope, so a corrupt or injected row could
        carry a foreign-scope id (e.g. a ``policy.*`` id sitting in a personal
        namespace). Merging such a row would let a lower-authority scope override
        a higher-authority value against the declared ``scope_precedence``. This
        filters to only the ids whose descriptor is owned by ``scope``, so a
        mis-scoped row is dropped at the merge boundary and cannot escalate.
        """
        return {
            setting_id: value
            for setting_id, value in self.stored_values(scope, scope_key).items()
            if self._by_id.get(setting_id, {}).get("scope") == scope
        }

    def seed_authority_value(
        self, scope: str, setting_id: str, value: Any, *,
        updated_by: str = "authority", expected_version: int | None = None,
    ) -> PreferenceRecord:
        """Seed a deployment/policy authority value, bypassing the actor gate.

        Actor writes (:meth:`set_preference`) fail closed for authority scopes:
        an ``env_only`` deployment value comes from the environment and an
        ``approval_gated`` policy value requires a consumed approval. This method
        represents that already-authorized authority write (the output of the
        environment/approval layer) so the hub — and tests standing in for it —
        can establish a ceiling/allowlist without minting an unapproved actor
        write. It refuses any actor scope, and still typed-validates the value.

        When ``expected_version`` is supplied the write is optimistic-concurrency
        guarded exactly like :meth:`set_preference`: the read-current-version
        check and the write run under one lock, so a stale authority commit
        (e.g. a policy operation whose bound version has been overtaken) raises
        :class:`StalePreferenceWriteError` and leaves the stored value UNCHANGED
        rather than double-applying. ``None`` keeps the unguarded seed behaviour
        for the environment/ceiling-seeding callers that do not race.
        """
        if scope not in ("deployment", "policy"):
            raise PreferenceStoreError("seed_authority_value is only for deployment/policy scopes")
        descriptor = self._descriptor(setting_id)
        if descriptor.get("scope") != scope:
            raise PreferenceStoreError("authority seed setting is not owned by this scope")
        validate_setting_value(descriptor, value)
        if expected_version is not None and (
            not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 0
        ):
            raise PreferenceStoreError("expected_version must be a non-negative integer")
        with self._lock:
            namespace = self.rows.records.setdefault((scope, scope), {})
            existing = namespace.get(setting_id)
            current = existing.write_version if existing is not None else 0
            if expected_version is not None and expected_version != current:
                # Reload-required: the stored authority value is left untouched.
                raise StalePreferenceWriteError(current)
            record = PreferenceRecord(
                setting_id=setting_id,
                scope=scope,
                scope_key=scope,
                value=value,
                write_version=current + 1,
                updated_by=updated_by,
            )
            namespace[setting_id] = record
            return record

    def clear_authority_value(
        self, scope: str, setting_id: str, *, expected_version: int, updated_by: str = "authority",
    ) -> None:
        """Remove a deployment/policy authority override under an optimistic guard.

        The authority-scope counterpart to :meth:`reset_preference`: it drops the
        stored override so the setting falls back to its descriptor default. Used
        by the policy-operation gate for an approved ``preference.reset`` of a
        policy setting. A stale ``expected_version`` raises
        :class:`StalePreferenceWriteError` and leaves the stored value unchanged.
        """
        if scope not in ("deployment", "policy"):
            raise PreferenceStoreError("clear_authority_value is only for deployment/policy scopes")
        descriptor = self._descriptor(setting_id)
        if descriptor.get("scope") != scope:
            raise PreferenceStoreError("authority setting is not owned by this scope")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 0:
            raise PreferenceStoreError("expected_version must be a non-negative integer")
        with self._lock:
            namespace = self.rows.records.get((scope, scope))
            existing = namespace.get(setting_id) if namespace is not None else None
            current = existing.write_version if existing is not None else 0
            if expected_version != current:
                raise StalePreferenceWriteError(current)
            if namespace is not None and setting_id in namespace:
                del namespace[setting_id]

    def _resolved_effective(
        self, scope: str, scope_key: str, setting_id: str, *, live_valid_refs: Mapping[str, Any] | None,
    ) -> EffectiveValue:
        """Resolve one setting's effective value through the SHARED resolver.

        Builds the same merged view the GET endpoint resolves for this setting —
        the authority namespaces (deployment/policy, the source of any ceiling)
        plus the setting's own ``(scope, scope_key)`` namespace — ownership
        filtered so a mis-scoped row cannot cross over, then runs the one shared
        :func:`resolve_effective_settings` with the same ceiling + ref-validity
        inputs. Because each setting is single-scope and every ceiling is
        authority-owned, this agrees byte-for-byte with the API GET effective
        value for the same setting (T002.3 criterion 3).
        """
        refs = live_valid_refs if live_valid_refs is not None else reviewed_catalog_valid_refs(self.catalog)
        merged: dict[str, Any] = {}
        merged.update(self.owned_values("deployment", "deployment"))
        merged.update(self.owned_values("policy", "policy"))
        merged.update(self.owned_values(scope, scope_key))
        resolved = resolve_effective_settings(self.catalog, merged, live_valid_refs=refs)
        return resolved[setting_id]

    def set_preference(
        self,
        scope: str,
        scope_key: str,
        setting_id: str,
        value: Any,
        expected_version: int,
        actor: str,
    ) -> PreferenceRecord:
        """Commit one scoped preference write under optimistic concurrency.

        The value is typed-validated against its descriptor BEFORE any version
        check, so a malformed value raises :class:`PreferenceValidationError`
        (a 422) rather than a stale-write conflict.  A stale ``expected_version``
        raises :class:`StalePreferenceWriteError` (a reload-required 409) and
        leaves the stored value unchanged.  A valid write commits atomically and
        increments the version by exactly one.
        """
        scope, scope_key = self._require_scope(scope, scope_key)
        descriptor = self._writable_descriptor(scope, setting_id)
        # Typed value validation is first and is NOT a concurrency conflict.
        validate_setting_value(descriptor, value)
        if not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 0:
            raise PreferenceStoreError("expected_version must be a non-negative integer")
        with self._lock:
            namespace = self.rows.records.get((scope, scope_key))
            existing = namespace.get(setting_id) if namespace is not None else None
            current = existing.write_version if existing is not None else 0
            if expected_version != current:
                # Reload-required: the stored value is left exactly as it was.
                raise StalePreferenceWriteError(current)
            record = PreferenceRecord(
                setting_id=setting_id,
                scope=scope,
                scope_key=scope_key,
                value=value,
                write_version=current + 1,
                updated_by=actor,
            )
            self.rows.records.setdefault((scope, scope_key), {})[setting_id] = record
            return record

    def reset_preference(
        self,
        scope: str,
        scope_key: str,
        setting_id: str,
        expected_version: int,
        actor: str,
        *,
        live_valid_refs: Mapping[str, Any] | None = None,
    ) -> EffectiveValue:
        """Reset one preference to its declared inherited/default state.

        Subject to the same optimistic check as a write (a stale reset is
        reload-required and leaves the stored value untouched).  On success the
        stored override is removed, so the setting falls back to its descriptor
        default (or unset).

        The returned effective value is resolved through the SAME shared resolver
        the GET endpoint uses — applying the policy ceiling and the ref-validity
        set — so ``reset`` and ``GET /api/preferences`` report the identical
        effective value for the identical state (T002.3 criterion 3). Reporting a
        bare descriptor default here (ignoring the ceiling/refs) made the two
        surfaces disagree — e.g. reset saying 30/default while GET said the
        clamped value. The API passes the same ``live_valid_refs`` it resolves
        GET with; ``None`` falls back to the reviewed-catalog baseline.
        """
        scope, scope_key = self._require_scope(scope, scope_key)
        # Ownership/gate is still enforced (a cross-scope reset is the indistinct
        # not-found, an authority reset fails closed) exactly like a write.
        self._writable_descriptor(scope, setting_id)
        if not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 0:
            raise PreferenceStoreError("expected_version must be a non-negative integer")
        with self._lock:
            namespace = self.rows.records.get((scope, scope_key))
            existing = namespace.get(setting_id) if namespace is not None else None
            current = existing.write_version if existing is not None else 0
            if expected_version != current:
                raise StalePreferenceWriteError(current)
            if namespace is not None and setting_id in namespace:
                del namespace[setting_id]
            return self._resolved_effective(
                scope, scope_key, setting_id, live_valid_refs=live_valid_refs
            )

    def apply_batch(
        self, operations: "list[Mapping[str, Any]]", actor: str,
    ) -> list[dict[str, Any]]:
        """Atomically apply a set of scoped set/reset operations, all-or-nothing.

        Each operation is ``{scope, scope_key, setting_id, op, value?, expected_version}``
        where ``op`` is ``"set"`` or ``"reset"``.  This is the batch counterpart of
        :meth:`set_preference` / :meth:`reset_preference` and reuses the SAME typed
        validation and optimistic-concurrency rules: under one lock it first
        validates every writable descriptor + set value AND checks every
        ``expected_version``, and only then commits every operation.  If ANY check
        fails — a malformed value (:class:`~workbench.models.PreferenceValidationError`),
        a cross-scope/unknown id (:class:`UnknownPreferenceError`), or a stale version
        (:class:`StalePreferenceWriteError`) — nothing is committed, so a batch import
        or a scoped reset lands entirely or not at all (T006.2 / T006.3 atomicity).

        Every operation is bound to exactly its own ``(scope, scope_key)`` namespace,
        so a batch touches ONLY the named scopes and can never mutate another actor's
        namespace, a project it did not name, or a deployment/policy authority value
        (scope isolation).  Returns one result per applied op with its committed
        record (a ``set``) or ``None`` (a ``reset``) for the caller's audit trail.
        """
        prepared: list[tuple[str, str, str, str, Any, int]] = []
        with self._lock:
            # Phase 1 -- validate everything and check every version. NO mutation.
            for op in operations:
                scope, scope_key = self._require_scope(op["scope"], op["scope_key"])
                setting_id = op["setting_id"]
                # Ownership/writability gate (cross-scope -> indistinct not-found,
                # authority/env_only -> fail closed) exactly like a single write.
                descriptor = self._writable_descriptor(scope, setting_id)
                kind = op["op"]
                if kind == "set":
                    validate_setting_value(descriptor, op.get("value"))
                elif kind != "reset":
                    raise PreferenceStoreError(f"unknown batch operation: {kind!r}")
                expected = op["expected_version"]
                if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
                    raise PreferenceStoreError("expected_version must be a non-negative integer")
                namespace = self.rows.records.get((scope, scope_key))
                existing = namespace.get(setting_id) if namespace is not None else None
                current = existing.write_version if existing is not None else 0
                if expected != current:
                    # Reload-required: NOTHING in the batch is committed.
                    raise StalePreferenceWriteError(current)
                prepared.append((scope, scope_key, setting_id, kind, op.get("value"), current))
            # Phase 2 -- commit every op; all validation + version checks passed.
            results: list[dict[str, Any]] = []
            for scope, scope_key, setting_id, kind, value, current in prepared:
                if kind == "set":
                    record = PreferenceRecord(
                        setting_id=setting_id, scope=scope, scope_key=scope_key,
                        value=value, write_version=current + 1, updated_by=actor,
                    )
                    self.rows.records.setdefault((scope, scope_key), {})[setting_id] = record
                    results.append({
                        "setting_id": setting_id, "scope": scope, "scope_key": scope_key,
                        "op": "set", "record": record,
                    })
                else:  # reset
                    namespace = self.rows.records.get((scope, scope_key))
                    if namespace is not None and setting_id in namespace:
                        del namespace[setting_id]
                    results.append({
                        "setting_id": setting_id, "scope": scope, "scope_key": scope_key,
                        "op": "reset", "record": None,
                    })
            return results


# ---------------------------------------------------------------------------
# Non-secret plugin preference field resolution (reviewed-tools-plugins T011)
# ---------------------------------------------------------------------------

from .contracts import (
    ContractValidationError,
    plugin_preference_actor_view as _plugin_pref_actor_view,
    validate_plugin_catalog as _validate_plugin_catalog_for_prefs,
    validate_plugin_preference_value as _validate_plugin_pref_value,
)

#: The standard actor-selectable precedence order for a plugin preference field:
#: a per-turn override wins, then the actor's own value, then the project value,
#: then the field's safe default.
_PLUGIN_PREF_PRECEDENCE = ("per_turn", "actor", "project")


def resolve_plugin_tool_preferences(
    fields,
    *,
    per_turn: Mapping[str, Any] | None = None,
    actor: Mapping[str, Any] | None = None,
    project: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve a tool's preference fields through the STANDARD precedence (T011).

    Precedence is ``per_turn -> actor -> project -> safe default``.  Only DECLARED
    fields are resolved (an unknown stored key is ignored -- a caller cannot mint
    a new field), and each candidate value is typed-checked against its field
    descriptor; an invalid stored value falls back to the safe default rather than
    reaching dispatch.  The returned mapping is the ONLY thing a dispatch uses --
    there is no other channel by which a browser-supplied value could ride in.

    NOTE (T011): the actual dispatch-side CONSUMPTION of these resolved
    preferences awaits the operation-layer integration (a bridge/operation that
    reads the resolved mapping).  Today this resolver is the ONLY channel that
    produces them, so its typed-check + safe-default fallback is the single place
    an actor value is validated before it could reach an effect; a secret-shaped
    value can never reach here because :func:`workbench.contracts.validate_plugin_catalog`
    already refuses a secret name/default AND a secret-shaped ``allowed_values``
    option at review time.
    """
    scopes = {
        "per_turn": dict(per_turn or {}),
        "actor": dict(actor or {}),
        "project": dict(project or {}),
    }
    resolved: dict[str, Any] = {}
    for field in fields or []:
        if not isinstance(field, Mapping):
            continue
        name = str(field.get("name"))
        value = field.get("default")
        for scope in _PLUGIN_PREF_PRECEDENCE:
            if name in scopes[scope]:
                candidate = scopes[scope][name]
                try:
                    _validate_plugin_pref_value(field, candidate)
                except ContractValidationError:
                    # An invalid stored value never reaches dispatch: fall back to
                    # the safe default (keep scanning lower-precedence scopes).
                    continue
                value = candidate
                break
        resolved[name] = value
    return resolved


class PluginPreferenceStoreError(StoreError):
    """A plugin preference operation violated its scoping or descriptor contract."""


@dataclass
class PluginPreferenceRows:
    """Per-(scope, scope_key) stored plugin preference values.

    ``values`` maps ``(scope, scope_key) -> {(plugin_id, tool_id, field_name) -> value}``.
    The ``(scope, scope_key)`` namespace is the hard isolation boundary, mirroring
    :class:`PreferenceRows`: ``(actor, alice)`` and ``(project, proj_1)`` are
    disjoint namespaces, so a cross-actor read is structurally impossible.
    """

    values: dict[tuple[str, str], dict[tuple[str, str, str], Any]] = field(default_factory=dict)


class MemoryPluginPreferenceService:
    """Resolve a tool's actor-selectable preferences for the hub/browser (T011).

    Holds the reviewed plugin catalog (fail-closed validated at construction) and
    the per-scope stored values.  ``effective`` returns the actor-view field
    descriptors (never a secret one -- the catalog validator refused those) and
    the resolved effective values for one actor, resolved through the standard
    ``per_turn -> actor -> project -> default`` precedence.  A connector-host
    configuration value is never accepted from nor returned to a browser: only the
    declared NON-SECRET fields exist, and ``set_value`` refuses any value that is
    not a declared actor-selectable field.
    """

    def __init__(self, catalog: Mapping[str, Any], rows: PluginPreferenceRows | None = None) -> None:
        _validate_plugin_catalog_for_prefs(catalog)
        self._lock = threading.RLock()
        self._catalog = catalog
        self.rows = rows if rows is not None else PluginPreferenceRows()
        self._tools: dict[tuple[str, str], Mapping[str, Any]] = {}
        for plugin in catalog.get("plugins", []):
            for tool in plugin.get("tools", []) if isinstance(plugin, Mapping) else []:
                if isinstance(tool, Mapping):
                    self._tools[(str(plugin["id"]), str(tool["tool_id"]))] = tool

    def _tool(self, plugin_id: str, tool_id: str) -> Mapping[str, Any]:
        tool = self._tools.get((str(plugin_id), str(tool_id)))
        if tool is None:
            raise PluginPreferenceStoreError("unknown plugin tool")
        return tool

    def _field(self, tool: Mapping[str, Any], name: str) -> Mapping[str, Any]:
        for field_descriptor in tool.get("preference_fields", []) or []:
            if isinstance(field_descriptor, Mapping) and str(field_descriptor.get("name")) == str(name):
                return field_descriptor
        raise PluginPreferenceStoreError("unknown preference field")

    def set_value(
        self, scope: str, scope_key: str, plugin_id: str, tool_id: str, name: str, value: Any,
    ) -> None:
        """Store one actor/project preference value; refuse anything undeclared.

        A value is accepted only for a DECLARED actor-selectable field at the
        field's own scope (or a broader actor/project scope), and only when it
        typed-validates -- so a browser cannot smuggle a connector-host config
        value in under a made-up field name.
        """
        if scope not in ("actor", "project"):
            raise PluginPreferenceStoreError("a plugin preference value is stored only at actor or project scope")
        tool = self._tool(plugin_id, tool_id)
        descriptor = self._field(tool, name)
        try:
            _validate_plugin_pref_value(descriptor, value)
        except ContractValidationError as exc:
            raise PluginPreferenceStoreError(f"invalid plugin preference value: {exc}") from exc
        with self._lock:
            bucket = self.rows.values.setdefault((scope, str(scope_key)), {})
            bucket[(str(plugin_id), str(tool_id), str(name))] = value

    def _stored(self, scope: str, scope_key: str, plugin_id: str, tool_id: str) -> dict[str, Any]:
        bucket = self.rows.values.get((scope, str(scope_key)), {})
        return {
            fname: value
            for (pid, tid, fname), value in bucket.items()
            if pid == str(plugin_id) and tid == str(tool_id)
        }

    def effective(
        self,
        plugin_id: str,
        tool_id: str,
        *,
        actor: str,
        project_id: str | None = None,
        per_turn: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return ``{fields: actor-view, effective: {name: resolved value}}``.

        The actor's own namespace is keyed by ``actor``; the project namespace by
        ``project_id``.  ``per_turn`` is a runtime override supplied at resolution
        time (never a stored, browser-round-tripped connector-host config).
        """
        tool = self._tool(plugin_id, tool_id)
        with self._lock:
            actor_values = self._stored("actor", actor, plugin_id, tool_id)
            project_values = self._stored("project", project_id, plugin_id, tool_id) if project_id else {}
        resolved = resolve_plugin_tool_preferences(
            tool.get("preference_fields", []) or [],
            per_turn=per_turn, actor=actor_values, project=project_values,
        )
        return {"fields": _plugin_pref_actor_view(tool), "effective": resolved}
