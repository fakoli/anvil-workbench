"""Workbench operational store.

Postgres is the production source for Workbench-owned records.  The in-memory
implementation exists only for hermetic tests and local API smoke tests; Anvil
State remains the source of truth for delivery state in every mode.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from .models import Approval, AuditEvent, Bridge, Project, Run, new_id, now_utc
from .redaction import redact_value


class StoreError(RuntimeError):
    """A requested Workbench operation violates an immutable audit invariant."""


_RUN_STATUS_TRANSITIONS = {
    "queued": frozenset({"running", "reconciliation"}),
    "running": frozenset({"evidenced", "reconciliation"}),
    "evidenced": frozenset(),
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
    def list_approvals(self, project_id: str | None = None) -> list[Approval]: ...
    def register_bridge(self, project_id: str, name: str) -> tuple[Bridge, str]: ...
    def authenticate_bridge(self, bridge_id: str, token: str) -> Bridge: ...
    def create_run(self, project_id: str, task_id: str | None, model: str) -> Run: ...
    def update_run_status(self, run_id: str, status: str, bridge_id: str) -> Run: ...
    def add_transcript(self, run_id: str, role: str, content: Any) -> None: ...
    def create_approval(self, project_id: str, action_type: str, payload: dict[str, Any], requested_by: str, ttl_seconds: int, bridge_id: str | None) -> Approval: ...
    def get_approval(self, approval_id: str) -> Approval: ...
    def approve(self, approval_id: str, actor: str, approvers: frozenset[str]) -> Approval: ...
    def consume(self, approval_id: str, action_payload_hash: str) -> Approval: ...
    def enqueue_command(self, bridge_id: str, approval: Approval) -> None: ...
    def enqueue_run(self, bridge_id: str, run: Run) -> None: ...
    def next_command(self, bridge_id: str) -> dict[str, Any] | None: ...
    def append_audit(self, kind: str, actor: str, project_id: str | None, data: dict[str, Any]) -> AuditEvent: ...


class MemoryStore:
    """Small, lock-free test store; API requests are serialized in tests."""

    def __init__(self) -> None:
        self.projects: dict[str, Project] = {}
        self.bridges: dict[str, Bridge] = {}
        self.runs: dict[str, Run] = {}
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

    def list_approvals(self, project_id: str | None = None) -> list[Approval]:
        values = list(self.approvals.values())
        return [approval for approval in values if project_id is None or approval.project_id == project_id]

    def register_bridge(self, project_id: str, name: str) -> tuple[Bridge, str]:
        if project_id not in self.projects:
            raise StoreError("unknown project")
        raw = secrets.token_urlsafe(32)
        bridge = Bridge(new_id("bridge"), project_id, name, token_hash(raw))
        self.bridges[bridge.id] = bridge
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

    def create_run(self, project_id: str, task_id: str | None, model: str) -> Run:
        if project_id not in self.projects:
            raise StoreError("unknown project")
        run = Run(new_id("run"), project_id, task_id, model, "queued")
        self.runs[run.id] = run
        self.transcripts[run.id] = []
        self.append_audit("run.created", "operator", project_id, {"run_id": run.id, "task_id": task_id, "model": model})
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
        self.append_audit("run.status_changed", "bridge:" + bridge_id, run.project_id, {"run_id": run_id, "status": status})
        return updated

    def add_transcript(self, run_id: str, role: str, content: Any) -> None:
        if run_id not in self.runs:
            raise StoreError("unknown run")
        self.transcripts[run_id].append({"role": role, "content": redact_value(content), "created_at": now_utc().isoformat()})

    def create_approval(self, project_id: str, action_type: str, payload: dict[str, Any], requested_by: str, ttl_seconds: int, bridge_id: str | None) -> Approval:
        if project_id not in self.projects:
            raise StoreError("unknown project")
        if bridge_id is not None and bridge_id not in self.bridges:
            raise StoreError("unknown bridge")
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
        if approval.status != "approved" or approval.expired:
            raise StoreError("approval is not valid for execution")
        if not secrets.compare_digest(approval.payload_hash, action_payload_hash):
            raise StoreError("action payload differs from the approved hash")
        used = Approval(**{**approval.__dict__, "status": "consumed", "consumed_at": now_utc()})
        self.approvals[approval.id] = used
        self.append_audit("approval.consumed", approval.approved_by or "unknown", approval.project_id, {"approval_id": approval.id, "payload_hash": approval.payload_hash})
        return used

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
        self.commands.setdefault(bridge_id, []).append({
            "id": new_id("command"), "approval_id": None, "action_type": "run_codex",
            "payload": {"run_id": run.id, "project_id": run.project_id, "task_id": run.task_id, "model": run.model},
            "payload_hash": payload_hash({"run_id": run.id, "project_id": run.project_id, "task_id": run.task_id, "model": run.model}),
        })

    def next_command(self, bridge_id: str) -> dict[str, Any] | None:
        commands = self.commands.get(bridge_id, [])
        return commands.pop(0) if commands else None


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
    def _run(row: dict[str, Any]) -> Run:
        return Run(row["id"], row["project_id"], row["task_id"], row["model"], row["status"], row["created_at"], row["completed_at"])

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
                CREATE TABLE IF NOT EXISTS workbench_runs (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES workbench_projects(id),
                    task_id TEXT, model TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL, completed_at TIMESTAMPTZ
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
                    payload JSONB NOT NULL, payload_hash TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workbench_audit (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, actor TEXT NOT NULL,
                    project_id TEXT, data JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL
                );
                CREATE INDEX IF NOT EXISTS workbench_commands_bridge_idx ON workbench_commands (bridge_id, created_at);
                CREATE INDEX IF NOT EXISTS workbench_approvals_project_idx ON workbench_approvals (project_id, created_at DESC);
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

    def create_run(self, project_id: str, task_id: str | None, model: str) -> Run:
        run = Run(new_id("run"), project_id, task_id, model.strip(), "queued")
        with self._connection().cursor() as cur:
            cur.execute("SELECT id FROM workbench_projects WHERE id = %s", (project_id,))
            if cur.fetchone() is None:
                raise StoreError("unknown project")
            cur.execute(
                "INSERT INTO workbench_runs (id,project_id,task_id,model,status,created_at,completed_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (run.id, run.project_id, run.task_id, run.model, run.status, run.created_at, None),
            )
        self.append_audit("run.created", "operator", project_id, {"run_id": run.id, "task_id": task_id, "model": run.model})
        return run

    def update_run_status(self, run_id: str, status: str, bridge_id: str) -> Run:
        if status not in _RUN_STATUS_TRANSITIONS:
            raise StoreError("invalid run status")
        with self._connection().cursor() as cur:
            cur.execute(
                "SELECT runs.*, projects.bridge_id FROM workbench_runs runs JOIN workbench_projects projects ON projects.id = runs.project_id WHERE runs.id = %s",
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
        run = self._run(updated)
        self.append_audit("run.status_changed", "bridge:" + bridge_id, run.project_id, {"run_id": run_id, "status": status})
        return run

    def add_transcript(self, run_id: str, role: str, content: Any) -> None:
        clean = redact_value(content)
        with self._connection().cursor() as cur:
            cur.execute("SELECT id FROM workbench_runs WHERE id = %s", (run_id,))
            if cur.fetchone() is None:
                raise StoreError("unknown run")
            cur.execute(
                "INSERT INTO workbench_transcripts (run_id,role,content,created_at) VALUES (%s,%s,%s,%s)",
                (run_id, role, self._json(clean), now_utc()),
            )

    def create_approval(self, project_id: str, action_type: str, payload: dict[str, Any], requested_by: str, ttl_seconds: int, bridge_id: str | None) -> Approval:
        clean = redact_value(payload)
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
        payload = {"run_id": run.id, "project_id": run.project_id, "task_id": run.task_id, "model": run.model}
        with self._connection().cursor() as cur:
            cur.execute("SELECT id FROM workbench_bridges WHERE id = %s", (bridge_id,))
            if cur.fetchone() is None:
                raise StoreError("unknown bridge")
            cur.execute(
                "INSERT INTO workbench_commands (id,bridge_id,approval_id,action_type,payload,payload_hash,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (new_id("command"), bridge_id, None, "run_codex", self._json(payload), payload_hash(payload), now_utc()),
            )

    def next_command(self, bridge_id: str) -> dict[str, Any] | None:
        connection = self._connection()
        with connection.transaction():
            with connection.cursor() as cur:
                cur.execute(
                    "DELETE FROM workbench_commands WHERE id = (SELECT id FROM workbench_commands WHERE bridge_id = %s ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING id,approval_id,action_type,payload,payload_hash",
                    (bridge_id,),
                )
                return cur.fetchone()
