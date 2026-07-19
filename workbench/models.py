"""Durable Workbench domain values.

These records are intentionally separate from Anvil State's canonical task and
evidence models.  A Workbench action stores links to State event ids; it never
reimplements State transitions.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    state_root: str
    bridge_id: str | None = None
    created_at: datetime = field(default_factory=now_utc)


@dataclass(frozen=True)
class Run:
    id: str
    project_id: str
    task_id: str | None
    model: str
    status: str
    created_at: datetime = field(default_factory=now_utc)
    completed_at: datetime | None = None


@dataclass(frozen=True)
class Approval:
    id: str
    project_id: str
    action_type: str
    payload: dict[str, Any]
    payload_hash: str
    requested_by: str
    expires_at: datetime
    status: str = "pending"
    approved_by: str | None = None
    approved_at: datetime | None = None
    consumed_at: datetime | None = None
    bridge_id: str | None = None
    created_at: datetime = field(default_factory=now_utc)

    @property
    def expired(self) -> bool:
        return now_utc() >= self.expires_at


@dataclass(frozen=True)
class Bridge:
    id: str
    project_id: str
    name: str
    token_hash: str
    created_at: datetime = field(default_factory=now_utc)
    last_seen_at: datetime | None = None


@dataclass(frozen=True)
class AuditEvent:
    id: str
    kind: str
    actor: str
    project_id: str | None
    data: dict[str, Any]
    created_at: datetime = field(default_factory=now_utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def as_json(value: object) -> dict[str, Any]:
    """Render a dataclass as JSON-compatible API data."""
    result = asdict(value)
    for key, item in list(result.items()):
        if isinstance(item, datetime):
            result[key] = item.isoformat()
    return result

