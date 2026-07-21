"""Durable Workbench domain values.

These records are intentionally separate from Anvil State's canonical task and
evidence models.  A Workbench action stores links to State event ids; it never
reimplements State transitions.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .redaction import redact_config_text, redact_text


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
    session_id: str | None = None
    workflow_id: str | None = None
    workflow_step_id: str | None = None
    lease_epoch: int | None = None


@dataclass(frozen=True)
class Session:
    """A resumable harness context, independent from Anvil State task authority."""

    id: str
    project_id: str
    title: str
    worktree_id: str
    status: str = "active"
    voice_enabled: bool = False
    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)


@dataclass(frozen=True)
class Workflow:
    """A version-pinned, allowlisted workflow graph for one Workbench session."""

    id: str
    project_id: str
    session_id: str
    version: int
    definition: dict[str, Any]
    status: str = "draft"
    cursor: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)


@dataclass(frozen=True)
class WorkflowEvent:
    """Append-only redacted session event used for browser catch-up and audit."""

    id: str
    session_id: str
    workflow_id: str | None
    sequence: int
    kind: str
    data: dict[str, Any]
    created_at: datetime = field(default_factory=now_utc)


@dataclass(frozen=True)
class ResourceLease:
    """A fenced single-writer lease for a worktree or other mutable resource."""

    resource_key: str
    session_id: str
    epoch: int
    expires_at: datetime
    created_at: datetime = field(default_factory=now_utc)


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
class BridgeSkill:
    """Safe metadata for one explicitly configured bridge skill."""

    bridge_id: str
    skill_id: str
    description: str
    content_sha256: str
    updated_at: datetime = field(default_factory=now_utc)


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


# ---------------------------------------------------------------------------
# Immutable run context (state-context-operations:T005.1)
# ---------------------------------------------------------------------------
#
# The run context is the bounded, frozen record captured at queue time for one
# delivery run, BEFORE the bridge is dispatched.  It deliberately separates two
# trust domains into two labeled top-level structures:
#
# * ``trusted`` -- the execution policy the hub pins: run/session/bridge/worktree
#   identity, the immutable workflow snapshot pins (workflow + catalog +
#   capability-profile digests), the capability grants (with effect + gate), the
#   pinned skills, the run constraints, and the workflow cursor.  These are exact
#   authority fields (``sha256:`` digests, semantic versions, enumerations); a
#   model turn may read them but cannot widen them.
# * ``untrusted`` -- PRD/task-derived prose: the task reference, title,
#   acceptance criteria, scope, verification plan, work-packet digest, and the
#   evidence citations.  Every prose field here is ``untrusted_task_data`` --
#   readable for display, NEVER a control instruction.
#
# The shapes are frozen and their prose is credential-scrubbed on construction
# (defense in depth on the last hop before the browser/model context).  The
# closed field set means a State storage path, a credential, a raw command, or a
# provider payload has no field to arrive through: a run context that omits a
# required authority or human-readable field cannot be constructed, so a
# dispatch that depends on it fails closed (T005.2).

#: The internal serialization identifier for :meth:`RunContext.as_dict`.  It is
#: DELIBERATELY DISTINCT from the published flat contract const
#: ``workbench-run-context/v1`` (``docs/contracts/schemas/run-context.v1.schema
#: .json``).  ``as_dict`` emits a two-structure ``trusted``/``untrusted`` split
#: (the accepted internal design), which does not validate against the flat
#: published schema; stamping the flat const on it would let a consumer that
#: dispatches on the version string validate the split shape against the flat
#: schema and reject every response.  A separate ``-internal`` const makes the
#: two shapes unmistakable and non-substitutable.
RUN_CONTEXT_SCHEMA_VERSION = "workbench-run-context-internal/v1"

#: The two trust labels the run context serializes under.  ``trusted`` carries
#: pinned execution policy; ``untrusted`` carries PRD/task-derived prose.
TRUSTED_POLICY_LABEL = "trusted_execution_policy"
UNTRUSTED_TASK_LABEL = "untrusted_task_data"

#: Closed enumerations mirrored from ``run-context.v1.schema.json`` so an
#: unknown effect/gate fails closed rather than riding through as opaque text.
RUN_EFFECTS = frozenset(
    {"read", "bounded_execution", "state_mutation", "external_effect", "policy_mutation"}
)
RUN_GATES = frozenset({"none", "preview", "approval"})

#: Gate strictness order (weakest -> strongest).  A per-operation gate override
#: supplied to :func:`run_capabilities_from_snapshot` may only STRENGTHEN the
#: conservative default (raise the gate); an attempt to weaken it below the
#: default -- e.g. downgrading an ``external_effect`` from ``approval`` to
#: ``none`` -- fails closed rather than silently widening authority.
_GATE_STRICTNESS = {"none": 0, "preview": 1, "approval": 2}

#: The conservative default gate policy: an effect that leaves the local sandbox
#: (an external side effect or a policy mutation) requires an approval gate;
#: everything else is ungated.  This is the reviewed default a snapshot-derived
#: capability inherits when no explicit gate is supplied; it is intentionally
#: fail-safe (approval, not none) for the effects that can escape the worktree.
_DEFAULT_GATE_FOR_EFFECT = {
    "read": "none",
    "bounded_execution": "none",
    "state_mutation": "none",
    "external_effect": "approval",
    "policy_mutation": "approval",
}

_RC_CONTEXT_ID = re.compile(r"^ctx_[a-zA-Z0-9_-]{8,128}$")
_RC_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_RC_PRD_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_RC_TASK_ID = re.compile(r"^T[0-9]{3}(\.[0-9]{1,3})?$")
_RC_CONTRACT_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_RC_WORKTREE = re.compile(r"^[a-zA-Z0-9._-]{1,128}$")
_RC_IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


class RunContextError(ValueError):
    """A run context would omit or corrupt a required trusted/untrusted field."""


def _rc_require(condition: bool, message: str) -> None:
    if not condition:
        raise RunContextError(message)


def _rc_prose(value: Any, limit: int, label: str, *, allow_empty: bool = False) -> str:
    lower = 0 if allow_empty else 1
    _rc_require(
        isinstance(value, str) and lower <= len(value) <= limit,
        f"{label} must be bounded readable text",
    )
    # Defense in depth: scrub any credential the prose might carry before it is
    # ever persisted or rendered.  Trusted-side prose (capability summaries,
    # skill purposes, stop conditions, receipt summaries) is State-backed
    # guidance, so the transcript credential scrub is the right, non-destructive
    # domain here.
    return redact_text(value)


def _rc_untrusted_prose(value: Any, limit: int, label: str, *, allow_empty: bool = False) -> str:
    """Bound + scrub PRD/task-derived prose with the wider config-class scrubber.

    The untrusted channel carries whatever a PRD title, acceptance criterion,
    scope entry, verification step, or evidence citation/summary happens to say.
    That prose can contain a credential shape the transcript scrub misses (a bare
    ``AKIA…`` key, a JWT, a PEM block), a raw endpoint/DB URL, an ``ip:port``, or
    a State-storage/host path.  Routing it through
    :func:`~workbench.redaction.redact_config_text` closes every one of those
    classes at construction time, so the persisted snapshot never holds the
    secret; the API boundary re-scrubs the same channel as a last hop.
    """
    lower = 0 if allow_empty else 1
    _rc_require(
        isinstance(value, str) and lower <= len(value) <= limit,
        f"{label} must be bounded readable text",
    )
    return redact_config_text(value)


def _rc_digest(value: Any, label: str) -> str:
    _rc_require(isinstance(value, str) and bool(_RC_DIGEST.match(value)), f"{label} must be a sha256 digest")
    return value


def _rc_ident(value: Any, label: str) -> str:
    _rc_require(isinstance(value, str) and bool(_RC_IDENT.match(value)), f"{label} is not a valid identifier")
    return value


def _rc_int(value: Any, label: str, *, minimum: int, maximum: int | None = None) -> int:
    _rc_require(
        isinstance(value, int) and not isinstance(value, bool) and value >= minimum,
        f"{label} must be an integer >= {minimum}",
    )
    if maximum is not None:
        _rc_require(value <= maximum, f"{label} must be <= {maximum}")
    return value


def _rc_reject_unknown(data: Mapping[str, Any], allowed: set[str], label: str) -> None:
    _rc_require(isinstance(data, Mapping), f"{label} must be an object")
    unknown = set(data) - allowed
    _rc_require(not unknown, f"{label} carries undeclared fields: {sorted(unknown)}")


@dataclass(frozen=True)
class RunIdentity:
    """Trusted run/session/bridge/worktree identity captured at queue time."""

    run_id: str
    session_id: str
    bridge_id: str
    worktree_name: str
    task_id: str | None = None
    request_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _rc_ident(self.run_id, "identity run_id"))
        object.__setattr__(self, "session_id", _rc_ident(self.session_id, "identity session_id"))
        object.__setattr__(self, "bridge_id", _rc_ident(self.bridge_id, "identity bridge_id"))
        _rc_require(
            isinstance(self.worktree_name, str) and bool(_RC_WORKTREE.match(self.worktree_name)),
            "identity worktree_name is invalid",
        )
        if self.task_id is not None:
            object.__setattr__(self, "task_id", _rc_ident(self.task_id, "identity task_id"))
        if self.request_id is not None:
            object.__setattr__(self, "request_id", _rc_ident(self.request_id, "identity request_id"))

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "bridge_id": self.bridge_id,
            "worktree_name": self.worktree_name,
        }
        if self.task_id is not None:
            data["task_id"] = self.task_id
        if self.request_id is not None:
            data["request_id"] = self.request_id
        return data


@dataclass(frozen=True)
class RunCatalogPin:
    """One provider catalog pinned into the run context at its exact digest."""

    provider: str
    digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _rc_ident(self.provider, "catalog pin provider"))
        object.__setattr__(self, "digest", _rc_digest(self.digest, "catalog pin digest"))

    def as_dict(self) -> dict[str, str]:
        return {"provider": self.provider, "digest": self.digest}


@dataclass(frozen=True)
class RunWorkflowPin:
    """The immutable workflow snapshot pins the run context is bound to."""

    workflow_id: str
    workflow_revision: str
    workflow_digest: str
    catalogs: tuple[RunCatalogPin, ...]
    capability_profile_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "catalogs", tuple(self.catalogs))
        object.__setattr__(self, "workflow_id", _rc_ident(self.workflow_id, "workflow id"))
        _rc_require(
            isinstance(self.workflow_revision, str) and bool(self.workflow_revision),
            "workflow revision is required",
        )
        object.__setattr__(self, "workflow_digest", _rc_digest(self.workflow_digest, "workflow digest"))
        _rc_require(len(self.catalogs) >= 1, "workflow pin must carry at least one catalog digest")
        for catalog in self.catalogs:
            _rc_require(isinstance(catalog, RunCatalogPin), "workflow catalogs must be typed catalog pins")
        providers = [catalog.provider for catalog in self.catalogs]
        _rc_require(len(providers) == len(set(providers)), "workflow pin has duplicate catalog providers")
        object.__setattr__(
            self, "capability_profile_digest",
            _rc_digest(self.capability_profile_digest, "capability_profile_digest"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.workflow_id,
            "revision": self.workflow_revision,
            "digest": self.workflow_digest,
            "catalogs": [catalog.as_dict() for catalog in self.catalogs],
            "capability_profile_digest": self.capability_profile_digest,
        }

    @classmethod
    def from_snapshot(cls, snapshot: Any) -> "RunWorkflowPin":
        """Derive the trusted workflow pin from a compiled ``WorkflowSnapshot``.

        Only the immutable, source-attributed pins cross over — the workflow
        identity/digest, each provider catalog digest, and the capability
        profile digest — never an execution block, command, or path.
        """
        from .workflow_snapshot import WorkflowSnapshot

        _rc_require(isinstance(snapshot, WorkflowSnapshot), "from_snapshot requires a WorkflowSnapshot")
        return cls(
            workflow_id=snapshot.workflow_id,
            workflow_revision=snapshot.workflow_revision,
            workflow_digest=snapshot.workflow_digest,
            catalogs=tuple(
                RunCatalogPin(provider=catalog.provider, digest=catalog.catalog_digest)
                for catalog in snapshot.catalogs
            ),
            capability_profile_digest=snapshot.capability_profile_digest,
        )


@dataclass(frozen=True)
class RunCapability:
    """One pinned, gated operation grant the run may propose."""

    operation_id: str
    provider: str
    contract_version: str
    operation_digest: str
    effect: str
    gate: str
    summary: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation_id", _rc_ident(self.operation_id, "capability operation_id"))
        object.__setattr__(self, "provider", _rc_ident(self.provider, "capability provider"))
        _rc_require(
            isinstance(self.contract_version, str) and bool(_RC_CONTRACT_VERSION.match(self.contract_version)),
            "capability contract_version is not semantic",
        )
        object.__setattr__(self, "operation_digest", _rc_digest(self.operation_digest, "capability operation_digest"))
        _rc_require(self.effect in RUN_EFFECTS, f"capability effect is not declared: {self.effect!r}")
        _rc_require(self.gate in RUN_GATES, f"capability gate is not declared: {self.gate!r}")
        if self.summary is not None:
            object.__setattr__(self, "summary", _rc_prose(self.summary, 500, "capability summary"))

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "operation_id": self.operation_id,
            "provider": self.provider,
            "contract_version": self.contract_version,
            "operation_digest": self.operation_digest,
            "effect": self.effect,
            "gate": self.gate,
        }
        if self.summary is not None:
            data["summary"] = self.summary
        return data


@dataclass(frozen=True)
class RunSkill:
    """One pinned skill the run may invoke, with a human-readable purpose."""

    id: str
    digest: str
    purpose: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _rc_ident(self.id, "skill id"))
        object.__setattr__(self, "digest", _rc_digest(self.digest, "skill digest"))
        object.__setattr__(self, "purpose", _rc_prose(self.purpose, 500, "skill purpose"))

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "digest": self.digest, "purpose": self.purpose}


@dataclass(frozen=True)
class RunConstraints:
    """Trusted run budget and stop conditions."""

    turn_limit: int
    tool_limit: int
    stop_conditions: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "stop_conditions", tuple(self.stop_conditions))
        _rc_int(self.turn_limit, "turn_limit", minimum=1, maximum=100)
        _rc_int(self.tool_limit, "tool_limit", minimum=0, maximum=100)
        _rc_require(len(self.stop_conditions) >= 1, "constraints require at least one stop condition")
        object.__setattr__(
            self, "stop_conditions",
            tuple(_rc_prose(item, 500, "stop condition") for item in self.stop_conditions),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "turn_limit": self.turn_limit,
            "tool_limit": self.tool_limit,
            "stop_conditions": list(self.stop_conditions),
        }


@dataclass(frozen=True)
class RunReceipt:
    """One completed-step receipt reference on the workflow cursor."""

    receipt_id: str
    summary: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "receipt_id", _rc_ident(self.receipt_id, "receipt_id"))
        object.__setattr__(self, "summary", _rc_prose(self.summary, 1000, "receipt summary"))

    def as_dict(self) -> dict[str, str]:
        return {"receipt_id": self.receipt_id, "summary": self.summary}


@dataclass(frozen=True)
class RunCursor:
    """Trusted workflow cursor at capture time."""

    step_id: str
    attempt: int
    completed_receipts: tuple[RunReceipt, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "completed_receipts", tuple(self.completed_receipts))
        object.__setattr__(self, "step_id", _rc_ident(self.step_id, "cursor step_id"))
        _rc_int(self.attempt, "cursor attempt", minimum=1)
        for receipt in self.completed_receipts:
            _rc_require(isinstance(receipt, RunReceipt), "cursor receipts must be typed receipts")

    def as_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "attempt": self.attempt,
            "completed_receipts": [receipt.as_dict() for receipt in self.completed_receipts],
        }


@dataclass(frozen=True)
class TrustedRunPolicy:
    """The pinned execution policy: exact authority, model-readable but unwidenable."""

    identity: RunIdentity
    workflow: RunWorkflowPin
    capabilities: tuple[RunCapability, ...]
    skills: tuple[RunSkill, ...]
    constraints: RunConstraints
    cursor: RunCursor

    def __post_init__(self) -> None:
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        object.__setattr__(self, "skills", tuple(self.skills))
        _rc_require(isinstance(self.identity, RunIdentity), "policy identity must be a RunIdentity")
        _rc_require(isinstance(self.workflow, RunWorkflowPin), "policy workflow must be a RunWorkflowPin")
        _rc_require(isinstance(self.constraints, RunConstraints), "policy constraints must be RunConstraints")
        _rc_require(isinstance(self.cursor, RunCursor), "policy cursor must be a RunCursor")
        for capability in self.capabilities:
            _rc_require(isinstance(capability, RunCapability), "policy capabilities must be typed grants")
        seen_caps = {
            (c.provider, c.operation_id, c.contract_version, c.operation_digest) for c in self.capabilities
        }
        _rc_require(len(seen_caps) == len(self.capabilities), "policy declares a duplicate capability grant")
        for skill in self.skills:
            _rc_require(isinstance(skill, RunSkill), "policy skills must be typed skills")
        _rc_require(
            len({skill.id for skill in self.skills}) == len(self.skills),
            "policy declares a duplicate skill",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "trust": TRUSTED_POLICY_LABEL,
            "identity": self.identity.as_dict(),
            "workflow": self.workflow.as_dict(),
            "capabilities": [capability.as_dict() for capability in self.capabilities],
            "skills": [skill.as_dict() for skill in self.skills],
            "constraints": self.constraints.as_dict(),
            "cursor": self.cursor.as_dict(),
        }


@dataclass(frozen=True)
class UntrustedTaskRef:
    """The typed task reference (untrusted project data)."""

    prd_id: str
    task_id: str
    prd_revision: int

    def __post_init__(self) -> None:
        _rc_require(isinstance(self.prd_id, str) and bool(_RC_PRD_ID.match(self.prd_id)), "task ref prd_id is invalid")
        _rc_require(isinstance(self.task_id, str) and bool(_RC_TASK_ID.match(self.task_id)), "task ref task_id is invalid")
        _rc_int(self.prd_revision, "task ref prd_revision", minimum=1)

    def as_dict(self) -> dict[str, Any]:
        return {"prd_id": self.prd_id, "task_id": self.task_id, "prd_revision": self.prd_revision}


@dataclass(frozen=True)
class UntrustedTask:
    """PRD/task-derived prose captured for display; never a control instruction."""

    ref: UntrustedTaskRef
    title: str
    acceptance_criteria: tuple[str, ...]
    work_packet_digest: str
    scope: tuple[str, ...] = ()
    verification_plan: tuple[str, ...] = ()
    content_trust: str = UNTRUSTED_TASK_LABEL

    def __post_init__(self) -> None:
        object.__setattr__(self, "acceptance_criteria", tuple(self.acceptance_criteria))
        object.__setattr__(self, "scope", tuple(self.scope))
        object.__setattr__(self, "verification_plan", tuple(self.verification_plan))
        _rc_require(isinstance(self.ref, UntrustedTaskRef), "task ref must be an UntrustedTaskRef")
        object.__setattr__(self, "title", _rc_untrusted_prose(self.title, 500, "task title"))
        _rc_require(len(self.acceptance_criteria) >= 1, "task must carry at least one acceptance criterion")
        object.__setattr__(
            self, "acceptance_criteria",
            tuple(_rc_untrusted_prose(item, 2000, "acceptance criterion") for item in self.acceptance_criteria),
        )
        object.__setattr__(self, "work_packet_digest", _rc_digest(self.work_packet_digest, "work_packet_digest"))
        object.__setattr__(self, "scope", tuple(_rc_untrusted_prose(item, 500, "scope entry") for item in self.scope))
        object.__setattr__(
            self, "verification_plan",
            tuple(_rc_untrusted_prose(item, 1000, "verification step") for item in self.verification_plan),
        )
        _rc_require(self.content_trust == UNTRUSTED_TASK_LABEL, "task prose is always untrusted task data")

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "content_trust": self.content_trust,
            "ref": self.ref.as_dict(),
            "title": self.title,
            "acceptance_criteria": list(self.acceptance_criteria),
            "work_packet_digest": self.work_packet_digest,
        }
        if self.scope:
            data["scope"] = list(self.scope)
        if self.verification_plan:
            data["verification_plan"] = list(self.verification_plan)
        return data


@dataclass(frozen=True)
class UntrustedEvidence:
    """One evidence citation (untrusted project data)."""

    citation: str
    summary: str
    content_trust: str = UNTRUSTED_TASK_LABEL

    def __post_init__(self) -> None:
        object.__setattr__(self, "citation", _rc_untrusted_prose(self.citation, 500, "evidence citation"))
        object.__setattr__(self, "summary", _rc_untrusted_prose(self.summary, 2000, "evidence summary"))
        _rc_require(self.content_trust == UNTRUSTED_TASK_LABEL, "evidence prose is always untrusted task data")

    def as_dict(self) -> dict[str, str]:
        return {"content_trust": self.content_trust, "citation": self.citation, "summary": self.summary}


@dataclass(frozen=True)
class UntrustedProjectData:
    """The untrusted PRD/task structure, labeled separately from trusted policy."""

    task: UntrustedTask
    evidence: tuple[UntrustedEvidence, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", tuple(self.evidence))
        _rc_require(isinstance(self.task, UntrustedTask), "untrusted data task must be an UntrustedTask")
        for item in self.evidence:
            _rc_require(isinstance(item, UntrustedEvidence), "untrusted evidence must be typed evidence")

    def as_dict(self) -> dict[str, Any]:
        return {
            "content_trust": UNTRUSTED_TASK_LABEL,
            "task": self.task.as_dict(),
            "evidence": [item.as_dict() for item in self.evidence],
        }


@dataclass(frozen=True)
class RunContext:
    """The bounded, immutable run context captured at queue time.

    Serializes into two separately labeled structures: ``trusted`` (pinned
    execution policy) and ``untrusted`` (PRD/task-derived prose).  Frozen at
    every level; its closed field set structurally cannot carry a State-storage
    path, a credential, a raw command, or a provider payload.

    This internal split shape is intentionally NOT the published flat run-context
    contract (``docs/contracts/schemas/run-context.v1.schema.json``): it stamps
    the distinct ``schema_version`` :data:`RUN_CONTEXT_SCHEMA_VERSION`
    (``workbench-run-context-internal/v1``) so a consumer can never mistake it
    for the flat contract and validate the split against the wrong schema.
    """

    context_id: str
    trusted: TrustedRunPolicy
    untrusted: UntrustedProjectData
    schema_version: str = RUN_CONTEXT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _rc_require(
            isinstance(self.context_id, str) and bool(_RC_CONTEXT_ID.match(self.context_id)),
            "run context_id is invalid",
        )
        _rc_require(isinstance(self.trusted, TrustedRunPolicy), "run context trusted must be a TrustedRunPolicy")
        _rc_require(isinstance(self.untrusted, UntrustedProjectData), "run context untrusted must be UntrustedProjectData")
        _rc_require(self.schema_version == RUN_CONTEXT_SCHEMA_VERSION, "run context schema_version is unexpected")

    @property
    def run_id(self) -> str:
        return self.trusted.identity.run_id

    def as_dict(self) -> dict[str, Any]:
        """Deterministic serialization; round-trips via :meth:`from_dict`."""
        return {
            "schema_version": self.schema_version,
            "context_id": self.context_id,
            "trusted": self.trusted.as_dict(),
            "untrusted": self.untrusted.as_dict(),
        }

    @classmethod
    def capture(
        cls,
        *,
        context_id: str,
        identity: RunIdentity,
        workflow: RunWorkflowPin,
        capabilities: Sequence[RunCapability],
        skills: Sequence[RunSkill],
        constraints: RunConstraints,
        cursor: RunCursor,
        task: UntrustedTask,
        evidence: Sequence[UntrustedEvidence] = (),
    ) -> "RunContext":
        """Assemble and freeze a run context from typed trusted/untrusted parts.

        Every required authority field (digests, versions, enums) and every
        required human-readable field (title, acceptance criteria, skill
        purposes, stop conditions) is validated by the component constructors;
        a missing or malformed field raises :class:`RunContextError` here, so a
        dispatch that captures this context first fails closed before any bridge
        effect (T005.2).
        """
        return cls(
            context_id=context_id,
            trusted=TrustedRunPolicy(
                identity=identity,
                workflow=workflow,
                capabilities=tuple(capabilities),
                skills=tuple(skills),
                constraints=constraints,
                cursor=cursor,
            ),
            untrusted=UntrustedProjectData(task=task, evidence=tuple(evidence)),
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RunContext":
        _rc_reject_unknown(data, {"schema_version", "context_id", "trusted", "untrusted"}, "run context")
        trusted = data["trusted"]
        untrusted = data["untrusted"]
        _rc_reject_unknown(
            trusted,
            {"trust", "identity", "workflow", "capabilities", "skills", "constraints", "cursor"},
            "trusted policy",
        )
        _rc_reject_unknown(untrusted, {"content_trust", "task", "evidence"}, "untrusted data")
        _rc_require(trusted.get("trust") == TRUSTED_POLICY_LABEL, "trusted policy label is wrong")
        _rc_require(untrusted.get("content_trust") == UNTRUSTED_TASK_LABEL, "untrusted data label is wrong")

        identity_data = trusted["identity"]
        _rc_reject_unknown(
            identity_data,
            {"run_id", "session_id", "bridge_id", "worktree_name", "task_id", "request_id"},
            "identity",
        )
        workflow_data = trusted["workflow"]
        _rc_reject_unknown(
            workflow_data, {"id", "revision", "digest", "catalogs", "capability_profile_digest"}, "workflow pin"
        )
        constraints_data = trusted["constraints"]
        _rc_reject_unknown(constraints_data, {"turn_limit", "tool_limit", "stop_conditions"}, "constraints")
        cursor_data = trusted["cursor"]
        _rc_reject_unknown(cursor_data, {"step_id", "attempt", "completed_receipts"}, "cursor")
        task_data = untrusted["task"]
        _rc_reject_unknown(
            task_data,
            {"content_trust", "ref", "title", "acceptance_criteria", "work_packet_digest", "scope", "verification_plan"},
            "task",
        )
        # A nested content_trust, when present, must be the untrusted label. The
        # constructor would otherwise silently normalize a mislabeled value; the
        # closed-set posture rejects it instead of quietly rewriting trust.
        if "content_trust" in task_data:
            _rc_require(task_data["content_trust"] == UNTRUSTED_TASK_LABEL, "task content_trust label is wrong")
        for capability in trusted["capabilities"]:
            _rc_reject_unknown(
                capability,
                {"operation_id", "provider", "contract_version", "operation_digest", "effect", "gate", "summary"},
                "capability",
            )
        for skill in trusted["skills"]:
            _rc_reject_unknown(skill, {"id", "digest", "purpose"}, "skill")
        for receipt in cursor_data.get("completed_receipts", ()):
            _rc_reject_unknown(receipt, {"receipt_id", "summary"}, "receipt")
        for item in untrusted.get("evidence", ()):
            _rc_reject_unknown(item, {"content_trust", "citation", "summary"}, "evidence")
            if "content_trust" in item:
                _rc_require(item["content_trust"] == UNTRUSTED_TASK_LABEL, "evidence content_trust label is wrong")
        _rc_reject_unknown(task_data["ref"], {"prd_id", "task_id", "prd_revision"}, "task ref")
        for catalog in workflow_data["catalogs"]:
            _rc_reject_unknown(catalog, {"provider", "digest"}, "catalog pin")

        return cls(
            context_id=str(data["context_id"]),
            schema_version=str(data.get("schema_version", RUN_CONTEXT_SCHEMA_VERSION)),
            trusted=TrustedRunPolicy(
                identity=RunIdentity(
                    run_id=str(identity_data["run_id"]),
                    session_id=str(identity_data["session_id"]),
                    bridge_id=str(identity_data["bridge_id"]),
                    worktree_name=str(identity_data["worktree_name"]),
                    task_id=str(identity_data["task_id"]) if identity_data.get("task_id") is not None else None,
                    request_id=str(identity_data["request_id"]) if identity_data.get("request_id") is not None else None,
                ),
                workflow=RunWorkflowPin(
                    workflow_id=str(workflow_data["id"]),
                    workflow_revision=str(workflow_data["revision"]),
                    workflow_digest=str(workflow_data["digest"]),
                    catalogs=tuple(
                        RunCatalogPin(provider=str(c["provider"]), digest=str(c["digest"]))
                        for c in workflow_data["catalogs"]
                    ),
                    capability_profile_digest=str(workflow_data["capability_profile_digest"]),
                ),
                capabilities=tuple(
                    RunCapability(
                        operation_id=str(c["operation_id"]),
                        provider=str(c["provider"]),
                        contract_version=str(c["contract_version"]),
                        operation_digest=str(c["operation_digest"]),
                        effect=str(c["effect"]),
                        gate=str(c["gate"]),
                        summary=str(c["summary"]) if c.get("summary") is not None else None,
                    )
                    for c in trusted["capabilities"]
                ),
                skills=tuple(
                    RunSkill(id=str(s["id"]), digest=str(s["digest"]), purpose=str(s["purpose"]))
                    for s in trusted["skills"]
                ),
                constraints=RunConstraints(
                    turn_limit=constraints_data["turn_limit"],
                    tool_limit=constraints_data["tool_limit"],
                    stop_conditions=tuple(str(item) for item in constraints_data["stop_conditions"]),
                ),
                cursor=RunCursor(
                    step_id=str(cursor_data["step_id"]),
                    attempt=cursor_data["attempt"],
                    completed_receipts=tuple(
                        RunReceipt(receipt_id=str(r["receipt_id"]), summary=str(r["summary"]))
                        for r in cursor_data.get("completed_receipts", ())
                    ),
                ),
            ),
            untrusted=UntrustedProjectData(
                task=UntrustedTask(
                    ref=UntrustedTaskRef(
                        prd_id=str(task_data["ref"]["prd_id"]),
                        task_id=str(task_data["ref"]["task_id"]),
                        prd_revision=task_data["ref"]["prd_revision"],
                    ),
                    title=str(task_data["title"]),
                    acceptance_criteria=tuple(str(item) for item in task_data["acceptance_criteria"]),
                    work_packet_digest=str(task_data["work_packet_digest"]),
                    scope=tuple(str(item) for item in task_data.get("scope", ())),
                    verification_plan=tuple(str(item) for item in task_data.get("verification_plan", ())),
                ),
                evidence=tuple(
                    UntrustedEvidence(citation=str(e["citation"]), summary=str(e["summary"]))
                    for e in untrusted.get("evidence", ())
                ),
            ),
        )


def run_capabilities_from_snapshot(
    snapshot: Any, *, gates: Mapping[tuple[str, str], str] | None = None,
    summaries: Mapping[tuple[str, str], str] | None = None,
) -> tuple[RunCapability, ...]:
    """Derive gated run capabilities from a compiled ``WorkflowSnapshot``.

    Each pinned operation becomes a :class:`RunCapability` carrying the exact
    ``(provider, id, contract_version, operation_digest, effect)`` from the
    snapshot.  The gate defaults from :data:`_DEFAULT_GATE_FOR_EFFECT` (approval
    for effects that leave the sandbox) and may be overridden per
    ``(provider, operation_id)``; an override to an undeclared gate fails closed.

    An override may only STRENGTHEN the conservative default -- raise the gate up
    the ``none < preview < approval`` order.  An override that would WEAKEN the
    default (e.g. downgrade an ``external_effect`` from ``approval`` to ``none``)
    fails closed with :class:`RunContextError`, so a caller can never quietly
    widen authority by supplying a laxer gate than the reviewed default.
    """
    from .workflow_snapshot import WorkflowSnapshot

    _rc_require(isinstance(snapshot, WorkflowSnapshot), "run_capabilities_from_snapshot requires a WorkflowSnapshot")
    gates = gates or {}
    summaries = summaries or {}
    capabilities: list[RunCapability] = []
    for operation in snapshot.operations:
        key = (operation.provider, operation.id)
        default_gate = _DEFAULT_GATE_FOR_EFFECT.get(operation.effect, "approval")
        gate = gates.get(key, default_gate)
        if key in gates:
            _rc_require(gate in RUN_GATES, f"capability gate is not declared: {gate!r}")
            _rc_require(
                _GATE_STRICTNESS[gate] >= _GATE_STRICTNESS[default_gate],
                f"gate override for {key} would weaken the default {default_gate!r} to {gate!r}",
            )
        capabilities.append(
            RunCapability(
                operation_id=operation.id,
                provider=operation.provider,
                contract_version=operation.contract_version,
                operation_digest=operation.operation_digest,
                effect=operation.effect,
                gate=gate,
                summary=summaries.get(key),
            )
        )
    return tuple(capabilities)


def run_skills_from_snapshot(snapshot: Any, purposes: Mapping[str, str]) -> tuple[RunSkill, ...]:
    """Derive pinned run skills from a snapshot, requiring a purpose per skill.

    A skill selected into the snapshot with no reviewed purpose is a missing
    required human-readable field and fails closed, so it can never be captured
    (and therefore never dispatched) without one.
    """
    from .workflow_snapshot import WorkflowSnapshot

    _rc_require(isinstance(snapshot, WorkflowSnapshot), "run_skills_from_snapshot requires a WorkflowSnapshot")
    skills: list[RunSkill] = []
    for skill in snapshot.skills:
        purpose = purposes.get(skill.id)
        _rc_require(
            isinstance(purpose, str) and bool(purpose),
            f"run skill is missing a required human-readable purpose: {skill.id}",
        )
        skills.append(RunSkill(id=skill.id, digest=skill.digest, purpose=purpose))
    return tuple(skills)


# ---------------------------------------------------------------------------
# Typed operation spine (state-context-operations:T006.1 / T006.2 / T006.3)
# ---------------------------------------------------------------------------
#
# These are the shared, frozen value objects for the typed-operation critical
# path: hub-side descriptor resolution of a model/workflow operation request
# (T006.1), the bridge's immediate authority preflight (T006.2), and the durable
# idempotent receipts + reconciliation records (T006.3).  They deliberately
# mirror the published contract shapes -- ``model-proposal.operation-request``,
# ``bridge-command.invoke-operation``, and ``operation-receipt`` -- so a typed
# refusal carries a stable code (like :data:`workflow_snapshot.DRIFT_KINDS`) and
# a receipt is a closed, redacted record with no field a secret, path, or raw
# command could ride through.

OPERATION_RECEIPT_SCHEMA_VERSION = "workbench-operation-receipt/v1"

#: Terminal receipt statuses, mirrored from ``operation-receipt.v1.schema.json``.
#: ``unknown``/``reconciliation_required`` mean an external effect MAY have
#: occurred and the outcome must be reconciled, never blindly retried.
OPERATION_RECEIPT_STATUSES = frozenset(
    {"succeeded", "failed", "denied", "unknown", "reconciliation_required"}
)

#: The closed set of stable typed refusal codes for the whole spine.  Like
#: ``DRIFT_KINDS`` these strings are durable receipt/reconciliation metadata:
#: extend the set, never rename or repurpose a member.  The ``operation.*``
#: family is raised by hub-side resolution (T006.1); the ``command.*``,
#: ``lease.*``, ``work_packet.*``, and ``approval.*`` families by the bridge's
#: immediate preflight (T006.2).
OPERATION_REFUSAL_CODES = frozenset({
    # --- T006.1 hub-side descriptor resolution ---
    "proposal.malformed",
    "operation.provider_unknown",
    "operation.unknown",
    "operation.digest_drift",
    "operation.unprofiled",
    "operation.input_not_object",
    "operation.input_invalid",
    "operation.schema_unresolvable",
    # A declared operation runner (e.g. a plugin read tool) raised at execution
    # time rather than at resolution: a genuine, retriable runtime failure that is
    # neither a schema nor a resolution problem.  Extends the set (never renames a
    # member) so a read-tool runner crash records an ACCURATE typed refusal.
    "operation.runner_failed",
    # --- T006.2 bridge-side immediate authority preflight ---
    "command.malformed",
    "command.expired",
    "lease.missing",
    "lease.expired",
    "lease.epoch_mismatch",
    "work_packet.digest_changed",
    "approval.missing",
    "approval.action_mismatch",
    "approval.hash_mismatch",
    "approval.invalid",
})

#: The credential-class token guard mirrored from the ``error.safe_summary``
#: ``not`` clause in ``operation-receipt.v1.schema.json``.  A summary that
#: literally names one of these classes is refused by the schema, so a receipt
#: can never be constructed carrying it (defence in depth behind redaction).
_RECEIPT_FORBIDDEN_SUMMARY = re.compile(
    r"(?i)(authorization|bearer|api[_ -]?key|github[_ -]?token|password|secret)"
)
_RECEIPT_ID = re.compile(r"^rcpt_[a-zA-Z0-9_-]{8,128}$")
_RECEIPT_TASK_REF = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}:T[0-9]{3}(\.[0-9]{1,3})?$")
_RECEIPT_EXTERNAL_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RECEIPT_EXTERNAL_VALUE = re.compile(r"^[A-Za-z0-9._:/-]{1,160}$")
_RECEIPT_EVIDENCE_REF = re.compile(
    r"^(?:evidence|artifact|state_event|route|verification)_[A-Za-z0-9._-]{1,128}$"
)
_OP_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._][a-z0-9]+)*$")


def new_receipt_id() -> str:
    return f"rcpt_{uuid4().hex}"


def safe_receipt_summary(text: Any, limit: int = 500) -> str:
    """Scrub a refusal/error summary and fail closed on a forbidden token.

    The summary is first run through :func:`redact_config_text` (the wider
    config-class scrubber that removes credentials, endpoints, and paths), then
    bounded, then checked against the schema's forbidden credential-class token
    guard.  A summary that would still name a credential class is replaced with
    a fixed safe sentence rather than persisted, so no receipt can ever carry a
    leaky summary even if a caller passed one.
    """
    scrubbed = redact_config_text(str(text))[:limit]
    if _RECEIPT_FORBIDDEN_SUMMARY.search(scrubbed):
        return "operation refused; consult the typed refusal code"
    return scrubbed


#: The schema-valid opaque token an ``external_ref`` value collapses to when the
#: last-hop scrub finds a credential, endpoint, or path shape inside it.  Chosen
#: to stay within :data:`_RECEIPT_EXTERNAL_VALUE` so a redacted receipt or
#: reconciliation record still serializes and validates.
_REDACTED_EXTERNAL_REF_VALUE = "redacted"


def safe_external_ref_value(key: Any, value: Any) -> str:
    """Scrub one ``external_ref`` value with the same last-hop guard as a summary.

    ``external_ref`` is a bounded opaque-token map (``owner/repo``, ``gh:1``, a
    ``state_event`` id).  The bounded :data:`_RECEIPT_EXTERNAL_VALUE` pattern is
    kept as a STRUCTURAL BACKSTOP -- it still refuses a space/``=``/``?``/``@``
    free-text shape -- but it deliberately admits ``/`` and ``:`` so a legit
    ``owner/repo`` ref survives, which means a slash-free token or a path built
    only from ``[A-Za-z0-9._:/-]`` (``sk-proj-AbC123def456xyz789``,
    ``/etc/anvil/secrets.env``, ``C:/Users/x/.aws/credentials``,
    ``/home/deploy/.ssh/id_rsa``) would otherwise ride through verbatim.  So on
    top of the backstop every value is routed through
    :func:`redact_config_text` and the forbidden-credential-token guard, exactly
    like :func:`safe_receipt_summary` scrubs a summary.  A value carrying any
    credential, endpoint, or path shape collapses to
    :data:`_REDACTED_EXTERNAL_REF_VALUE` instead of being persisted, closing the
    asymmetry where ``safe_summary`` was scrubbed but the sibling ``external_ref``
    on the same record was not.
    """
    _rc_require(bool(_RECEIPT_EXTERNAL_KEY.match(str(key))), f"external_ref key is invalid: {key!r}")
    raw = str(value)
    _rc_require(bool(_RECEIPT_EXTERNAL_VALUE.match(raw)), f"external_ref value is invalid: {key!r}")
    scrubbed = redact_config_text(raw)
    if scrubbed != raw or _RECEIPT_FORBIDDEN_SUMMARY.search(scrubbed):
        return _REDACTED_EXTERNAL_REF_VALUE
    return scrubbed


@dataclass(frozen=True)
class OperationRef:
    """A pinned operation descriptor reference (provider/id/version/digest).

    The exact four-tuple that identifies one reviewed operation at its pinned
    catalog revision.  Frozen and validated; it never carries an adapter, a
    command, a path, or a credential -- only the identifying digest.
    """

    provider: str
    id: str
    contract_version: str
    operation_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _rc_ident(self.provider, "operation provider"))
        _rc_require(
            isinstance(self.id, str) and bool(_OP_ID.match(self.id)),
            "operation id is not a valid dotted identifier",
        )
        _rc_require(
            isinstance(self.contract_version, str) and bool(_RC_CONTRACT_VERSION.match(self.contract_version)),
            "operation contract_version is not semantic",
        )
        object.__setattr__(self, "operation_digest", _rc_digest(self.operation_digest, "operation_digest"))

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.provider, self.id, self.contract_version, self.operation_digest)

    @property
    def versioned_key(self) -> tuple[str, str, str]:
        return (self.provider, self.id, self.contract_version)

    def as_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "id": self.id,
            "contract_version": self.contract_version,
            "operation_digest": self.operation_digest,
        }

    @classmethod
    def from_mapping(cls, value: Any, label: str = "operation") -> "OperationRef":
        if not isinstance(value, Mapping):
            raise TypedOperationError(
                OperationRefusal("proposal.malformed", f"{label} reference is not an object")
            )
        allowed = {"provider", "id", "contract_version", "operation_digest"}
        if set(value) - allowed:
            raise TypedOperationError(
                OperationRefusal("proposal.malformed", f"{label} reference carries undeclared fields")
            )
        try:
            return cls(
                provider=str(value.get("provider", "")),
                id=str(value.get("id", "")),
                contract_version=str(value.get("contract_version", "")),
                operation_digest=str(value.get("operation_digest", "")),
            )
        except RunContextError as exc:
            raise TypedOperationError(OperationRefusal("proposal.malformed", str(exc))) from exc


@dataclass(frozen=True)
class OperationRefusal:
    """One stable, redacted typed refusal for the operation spine.

    ``code`` is a member of :data:`OPERATION_REFUSAL_CODES`; ``safe_summary`` is
    scrubbed and forbidden-token-guarded on construction.  A refusal is the unit
    a denied receipt or a reconciliation item records, so it must never leak.
    """

    code: str
    safe_summary: str
    retryable: bool = False

    def __post_init__(self) -> None:
        _rc_require(self.code in OPERATION_REFUSAL_CODES, f"operation refusal code is not declared: {self.code!r}")
        _rc_require(isinstance(self.retryable, bool), "refusal retryable must be a boolean")
        object.__setattr__(self, "safe_summary", safe_receipt_summary(self.safe_summary))

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "safe_summary": self.safe_summary, "retryable": self.retryable}


class TypedOperationError(ValueError):
    """A typed operation refusal carrying a stable code and a redacted summary.

    Raised by hub-side resolution (T006.1) and bridge preflight (T006.2) so a
    caller asserts on the CLAIMED reason (``err.code``) rather than incidentally
    on a message; the carried :class:`OperationRefusal` is what a denied receipt
    or reconciliation item persists.
    """

    def __init__(self, refusal: OperationRefusal) -> None:
        self.refusal = refusal
        super().__init__(f"{refusal.code}: {refusal.safe_summary}")

    @property
    def code(self) -> str:
        return self.refusal.code


@dataclass(frozen=True)
class ResolvedOperation:
    """A hub-resolved operation: the pinned descriptor plus validated inputs.

    The output of T006.1 resolution.  ``effect`` and ``gate_required`` come from
    the pinned descriptor (never from the caller), and ``inputs`` has already
    been validated against the descriptor's pinned input schema.  It carries no
    adapter, transport, command, or path -- the bridge resolves those locally.

    ``gate_required`` is ADVISORY hub metadata: it is derived from the effect
    class (``effect in {external_effect, policy_mutation}``), NOT from the
    descriptor's ``gates.human_approval``.  It is deliberately conservative --
    it may over-report a gate -- and exists only so the hub/browser can preview
    that an approval is likely needed.  It is NOT the authority gate: the bridge
    preflight (:func:`workbench.bridge.preflight_operation`) reads the pinned
    descriptor's ``gates.human_approval`` and binds/consumes the approval there.
    A downstream consumer must treat the bridge preflight, never this field, as
    the gate that decides whether an approval is required.
    """

    operation: OperationRef
    effect: str
    gate_required: bool
    approval_action: str | None
    inputs: Mapping[str, Any]

    def __post_init__(self) -> None:
        _rc_require(isinstance(self.operation, OperationRef), "resolved operation requires an OperationRef")
        _rc_require(self.effect in RUN_EFFECTS, f"resolved operation effect is not declared: {self.effect!r}")
        _rc_require(isinstance(self.gate_required, bool), "resolved operation gate_required must be a boolean")
        object.__setattr__(self, "inputs", dict(self.inputs))

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation.as_dict(),
            "effect": self.effect,
            "gate_required": self.gate_required,
            "approval_action": self.approval_action,
            "inputs": dict(self.inputs),
        }


@dataclass(frozen=True)
class OperationReceipt:
    """One redacted, typed terminal receipt for an operation attempt (T006.3).

    Serializes to a payload valid against ``operation-receipt.v1.schema.json``.
    Every human-readable field is scrubbed, the error summary is
    forbidden-token-guarded, and the closed field set means a secret, a raw
    command, a path, or a provider payload has no field to arrive through.
    """

    receipt_id: str
    command_id: str
    run_id: str
    operation: OperationRef
    status: str
    idempotency_key: str
    started_at: datetime
    finished_at: datetime
    redaction_status: str = "redacted"
    redaction_ruleset: str = "workbench-default-v1"
    error: OperationRefusal | None = None
    external_ref: Mapping[str, str] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    task_ref: str | None = None
    request_id: str | None = None

    def __post_init__(self) -> None:
        _rc_require(bool(_RECEIPT_ID.match(self.receipt_id)), "receipt_id is invalid")
        _rc_require(isinstance(self.operation, OperationRef), "receipt requires an OperationRef")
        _rc_require(self.status in OPERATION_RECEIPT_STATUSES, f"receipt status is not declared: {self.status!r}")
        _rc_require(self.redaction_status in ("redacted", "metadata_only"), "receipt redaction status is invalid")
        _rc_require(bool(self.idempotency_key) and len(self.idempotency_key) <= 256, "receipt idempotency_key is invalid")
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        for ref in self.evidence_refs:
            _rc_require(bool(_RECEIPT_EVIDENCE_REF.match(str(ref))), f"receipt evidence ref is invalid: {ref!r}")
        clean_external: dict[str, str] = {}
        for key, value in dict(self.external_ref).items():
            clean_external[str(key)] = safe_external_ref_value(key, value)
        object.__setattr__(self, "external_ref", clean_external)
        if self.task_ref is not None:
            _rc_require(bool(_RECEIPT_TASK_REF.match(self.task_ref)), "receipt task_ref is invalid")
        if self.error is not None:
            _rc_require(isinstance(self.error, OperationRefusal), "receipt error must be an OperationRefusal")

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": OPERATION_RECEIPT_SCHEMA_VERSION,
            "receipt_id": self.receipt_id,
            "command_id": self.command_id,
            "run_id": self.run_id,
            "operation": self.operation.as_dict(),
            "status": self.status,
            "idempotency_key": self.idempotency_key,
            "redaction": {"status": self.redaction_status, "ruleset": self.redaction_ruleset},
            "started_at": _rfc3339(self.started_at),
            "finished_at": _rfc3339(self.finished_at),
        }
        if self.external_ref:
            data["external_ref"] = dict(self.external_ref)
        if self.evidence_refs:
            data["evidence_refs"] = list(self.evidence_refs)
        correlation: dict[str, str] = {}
        if self.task_ref is not None:
            correlation["task_id"] = self.task_ref
        if self.request_id is not None:
            correlation["request_id"] = self.request_id
        if correlation:
            data["correlation"] = correlation
        if self.error is not None:
            data["error"] = {
                "code": self.error.code,
                "safe_summary": self.error.safe_summary,
                "retryable": self.error.retryable,
            }
        return data


#: The closed set of reconciliation reasons for an unknown/interrupted outcome.
RECONCILIATION_REASONS = frozenset({
    "unknown_outcome",
    "lost_lease",
    "digest_drift",
    "approval_replay",
    "verification_failed",
    "provider_failure",
    "interrupted",
})
_RECONCILE_ID = re.compile(r"^recon_[a-zA-Z0-9_-]{8,128}$")


def new_reconciliation_id() -> str:
    return f"recon_{uuid4().hex}"


@dataclass(frozen=True)
class ReconciliationItem:
    """One durable unknown-outcome record for an operation that may have run.

    Created exactly once per unresolved attempt (T006.3): an external effect
    whose outcome is unknown must be reconciled, never silently retried.  It is
    a closed, redacted record; ``external_ref`` names only opaque references to
    a possibly-partial effect, never a raw payload.
    """

    id: str
    run_id: str
    command_id: str
    operation: OperationRef
    reason: str
    idempotency_key: str
    safe_summary: str
    external_ref: Mapping[str, str] = field(default_factory=dict)
    resolved: bool = False
    created_at: datetime = field(default_factory=now_utc)

    def __post_init__(self) -> None:
        _rc_require(bool(_RECONCILE_ID.match(self.id)), "reconciliation id is invalid")
        _rc_require(isinstance(self.operation, OperationRef), "reconciliation requires an OperationRef")
        _rc_require(self.reason in RECONCILIATION_REASONS, f"reconciliation reason is not declared: {self.reason!r}")
        _rc_require(bool(self.idempotency_key) and len(self.idempotency_key) <= 256, "reconciliation idempotency_key is invalid")
        object.__setattr__(self, "safe_summary", safe_receipt_summary(self.safe_summary))
        clean_external: dict[str, str] = {}
        for key, value in dict(self.external_ref).items():
            clean_external[str(key)] = safe_external_ref_value(key, value)
        object.__setattr__(self, "external_ref", clean_external)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "command_id": self.command_id,
            "operation": self.operation.as_dict(),
            "reason": self.reason,
            "idempotency_key": self.idempotency_key,
            "safe_summary": self.safe_summary,
            "external_ref": dict(self.external_ref),
            "resolved": self.resolved,
            "created_at": _rfc3339(self.created_at),
        }


def _rfc3339(value: datetime) -> str:
    """Render a UTC datetime as a compact RFC 3339 ``...Z`` timestamp."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Preference configuration: typed policy operations, durable records, schema
# migration, and the shared effective-value resolver
# (preferences-configuration: T004.1 / T002.1 / T002.3)
# ---------------------------------------------------------------------------
#
# These values back the preference store slice.  They deliberately mirror the
# typed-operation spine above: a mutable setting is changed only by naming one
# closed, versioned :class:`PolicyOperation` (never a generic command), whose
# canonical payload digest binds the exact effect (:func:`preference_operation
# _digest`).  A durable :class:`PreferenceRecord` carries the monotonic write
# and schema-version fields optimistic concurrency and migration depend on, and
# a single shared resolver (:func:`resolve_effective_settings`) gives every
# consumer one deterministic precedence + policy-ceiling + capability-fallback
# interpretation.  Nothing here is wired into the live loop; it is exercised
# only through the injectable store, mirroring the run-context read-model.

_PREF_SCOPES = ("personal", "project", "deployment", "policy")
_PREF_ACTOR_SCOPES = ("personal", "project")
_PREF_AUTHORITY_SCOPES = ("deployment", "policy")
_PREF_SETTING_ID = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
_PREF_ID_REF = re.compile(r"^[a-z][a-z0-9._:-]{1,127}$")
_PREF_DIGEST_REF = re.compile(r"^sha256:[a-f0-9]{64}$")
_PREF_SCOPE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}$")

#: Sentinel distinguishing "no stored value" from a legitimately stored ``None``
#: in the resolver, so a caller can never confuse an unset setting with one set
#: to a falsy value.
_PREF_MISSING: Any = object()

#: Domain separation + minimum key length for the KEYED scope-key audit
#: fingerprint, mirroring the chat-content fingerprint idiom
#: (:func:`workbench.conversation_models.turn_content_hash`).  The scope key is
#: an actor/project identity (often a guessable email or slug); an UNSALTED
#: ``sha256(scope:scope_key)`` of it is dictionary-recoverable, so the audit
#: fingerprint is a keyed HMAC-SHA256 whose server-held key never sits beside
#: the fingerprints it protects.  Without the key a holder of audit metadata
#: cannot run a dictionary of candidate actors against the tag.
_PREF_AUDIT_FINGERPRINT_PREFIX = b"anvil-workbench/preference-scope-key/v1\0"
MIN_PREF_AUDIT_KEY_BYTES = 16


def require_pref_audit_key(key: bytes) -> bytes:
    """Fail closed unless ``key`` is a usable server-held audit-fingerprint key."""
    if not isinstance(key, (bytes, bytearray)) or len(key) < MIN_PREF_AUDIT_KEY_BYTES:
        raise PreferenceValidationError(
            f"preference audit fingerprint key must be bytes of at least {MIN_PREF_AUDIT_KEY_BYTES} octets"
        )
    return bytes(key)


class PreferenceValidationError(ValueError):
    """A preference value is malformed or out of range for its descriptor.

    Raised BEFORE persistence (T002.1 criterion 4) so a wrong-type or
    out-of-bounds value never reaches the durable store, and mapped to a typed
    422 by the API — deliberately distinct from the stale-write reload-required
    conflict.
    """


def validate_setting_value(descriptor: Mapping[str, Any], value: Any) -> Any:
    """Return ``value`` when it satisfies its descriptor; else raise a typed error.

    Enforces the descriptor's declared ``type`` and (for ints) ``bounds``, (for
    enums) ``allowed_values``, and (for references) the id/digest grammar.  This
    is the single typed-validation entry point shared by the operation builder
    and the durable store, so a malformed value is refused identically wherever
    it enters.
    """
    if not isinstance(descriptor, Mapping):
        raise PreferenceValidationError("a setting value requires a typed descriptor")
    setting_type = descriptor.get("type")
    setting_id = descriptor.get("id", "<unknown>")
    if setting_type == "bool":
        if not isinstance(value, bool):
            raise PreferenceValidationError(f"{setting_id} requires a boolean value")
    elif setting_type == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise PreferenceValidationError(f"{setting_id} requires an integer value")
        bounds = descriptor.get("bounds")
        if isinstance(bounds, Mapping):
            low = bounds.get("min")
            high = bounds.get("max")
            if (isinstance(low, int) and value < low) or (isinstance(high, int) and value > high):
                raise PreferenceValidationError(
                    f"{setting_id} value {value} is outside its declared bounds "
                    f"[{bounds.get('min')}, {bounds.get('max')}]"
                )
    elif setting_type == "enum":
        allowed = descriptor.get("allowed_values") or ()
        if value not in allowed:
            raise PreferenceValidationError(f"{setting_id} value is not one of its allowed values")
    elif setting_type == "string":
        if not isinstance(value, str) or len(value) > 200:
            raise PreferenceValidationError(f"{setting_id} requires a string of at most 200 characters")
    elif setting_type == "id_ref":
        if not isinstance(value, str) or not _PREF_ID_REF.match(value):
            raise PreferenceValidationError(f"{setting_id} requires a valid id reference")
    elif setting_type == "digest_ref":
        if not isinstance(value, str) or not _PREF_DIGEST_REF.match(value):
            raise PreferenceValidationError(f"{setting_id} requires a sha256 digest reference")
    else:
        raise PreferenceValidationError(f"{setting_id} declares an unsupported type: {setting_type!r}")
    return value


# --- T004.1 typed, versioned policy operations + canonical payload hashing ---

PREFERENCE_OPERATION_SCHEMA_VERSION = "workbench-preference-operation/v1"

#: The closed set of typed policy-operation kinds.  A mutable setting is changed
#: only by naming one of these; a model or browser can never mint a new
#: privilege by emitting a fresh command name or arbitrary JSON.
PREFERENCE_OPERATION_KINDS = frozenset({"preference.set", "preference.reset"})


class PolicyOperationError(ValueError):
    """A typed policy operation would carry an unsafe or undeclared payload."""


@dataclass(frozen=True)
class PolicyOperation:
    """One typed, versioned, owner-specific mutation of a single policy/setting.

    Every mutable policy maps to one of these — bound to exactly one setting
    descriptor id and its owning scope, and carrying a monotonic ``op_version``
    — rather than a generic bridge command.  :attr:`digest` binds the full
    canonical payload, so an approval commits to the precise effect and a
    :class:`PolicyOperationPreview` sharing the payload shares the digest.
    """

    operation: str
    setting_id: str
    scope: str
    op_version: int
    value: Any = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.operation not in PREFERENCE_OPERATION_KINDS:
            raise PolicyOperationError(f"unknown policy operation: {self.operation!r}")
        if not isinstance(self.setting_id, str) or not _PREF_SETTING_ID.match(self.setting_id):
            raise PolicyOperationError(f"policy operation names an invalid setting id: {self.setting_id!r}")
        if self.scope not in _PREF_SCOPES:
            raise PolicyOperationError(f"policy operation names an unknown scope: {self.scope!r}")
        # Fail closed on the frozen value itself, not only in the builder: a
        # ``deployment`` scope is owner/environment-managed and can never be
        # represented as an actor/model-proposable policy operation.  A direct
        # construction that names it is refused here so no code path (test,
        # deserialization, or a future caller) can mint a deployment-scoped
        # operation that bypasses :func:`build_policy_operation`.
        if self.scope == "deployment":
            raise PolicyOperationError(
                "a deployment-owned setting is owner-managed and cannot be a policy operation"
            )
        if not isinstance(self.op_version, int) or isinstance(self.op_version, bool) or self.op_version < 1:
            raise PolicyOperationError("policy operation op_version must be an integer >= 1")
        if self.operation == "preference.reset" and self.value is not None:
            raise PolicyOperationError("a reset operation carries no value")
        if self.expires_at is not None and not isinstance(self.expires_at, datetime):
            raise PolicyOperationError("policy operation expires_at must be a datetime")

    def payload(self) -> dict[str, Any]:
        """The closed, canonical payload the digest binds.

        Deliberately closed: only the declared fields appear, so no undeclared
        JSON can ride into the hashed payload.  A ``preference.set`` carries its
        value; a ``preference.reset`` never does.
        """
        data: dict[str, Any] = {
            "schema_version": PREFERENCE_OPERATION_SCHEMA_VERSION,
            "operation": self.operation,
            "setting_id": self.setting_id,
            "scope": self.scope,
            "op_version": self.op_version,
        }
        if self.operation == "preference.set":
            data["value"] = self.value
        if self.expires_at is not None:
            data["expires_at"] = _rfc3339(self.expires_at)
        return data

    @property
    def digest(self) -> str:
        # Function-local import purely to defer loading the heavier ``contracts``
        # module (jsonschema + schema files) until a digest is actually taken.
        # It is NOT breaking an import cycle: ``contracts`` imports nothing from
        # ``models``, so a module-level import here would be acyclic — the
        # laziness is a load-cost choice, not a correctness one.
        from .contracts import preference_operation_digest

        return preference_operation_digest(self.payload())

    def as_dict(self) -> dict[str, Any]:
        """Serialize the operation with its bound digest, scrubbing free text."""
        payload = self.payload()
        if isinstance(payload.get("value"), str):
            payload["value"] = redact_config_text(payload["value"])
        return {"digest": self.digest, "payload": payload}


@dataclass(frozen=True)
class PolicyOperationPreview:
    """A read-only preview of a typed policy operation.

    Constructing or storing a preview is a pure function of the operation
    payload: it exposes the same canonical :attr:`digest` the applied operation
    binds and a redacted human-readable effect summary, but NO path that writes
    a stored value.  A preview and its operation share a digest, so an approval
    bound to the preview commits to exactly the effect that applies — and a
    preview can never change the effective setting.
    """

    operation: PolicyOperation
    effect_summary: str

    def __post_init__(self) -> None:
        if not isinstance(self.operation, PolicyOperation):
            raise PolicyOperationError("a preview requires a PolicyOperation")
        object.__setattr__(self, "effect_summary", redact_config_text(str(self.effect_summary)))

    @property
    def digest(self) -> str:
        return self.operation.digest

    def as_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "operation": self.operation.as_dict()["payload"],
            "effect_summary": self.effect_summary,
        }


def build_policy_operation(
    descriptor: Mapping[str, Any],
    *,
    operation: str,
    op_version: int,
    value: Any = None,
    expires_at: datetime | None = None,
) -> PolicyOperation:
    """Build a typed operation for one setting descriptor, or fail closed.

    Criterion 4: a ``secret`` / path-like descriptor and a deployment-only
    (``deployment`` scope or ``env_only`` mutability) descriptor are
    authority-owned; their values cannot enter an operation payload, so the
    build is refused before anything is hashed or applied.  A ``preference.set``
    value is typed-validated against the descriptor first, so an out-of-range or
    wrong-type value never enters the payload either.
    """
    if not isinstance(descriptor, Mapping):
        raise PolicyOperationError("a policy operation requires a typed descriptor")
    setting_id = str(descriptor.get("id"))
    scope = descriptor.get("scope")
    if descriptor.get("sensitivity") == "secret" or descriptor.get("path_like") is True:
        raise PolicyOperationError(
            f"a secret or path-like setting cannot enter an operation payload: {setting_id}"
        )
    if scope == "deployment" or descriptor.get("mutability") == "env_only":
        raise PolicyOperationError(
            f"a deployment-only setting is owner-managed and cannot enter an operation payload: {setting_id}"
        )
    if operation == "preference.set":
        validate_setting_value(descriptor, value)
    return PolicyOperation(
        operation=operation,
        setting_id=setting_id,
        scope=str(scope),
        op_version=op_version,
        value=value if operation == "preference.set" else None,
        expires_at=expires_at,
    )


# --- T002.1 durable preference record + optimistic versioning + migration ---

#: The current durable record schema version.  A stored row at any supported
#: prior version migrates up to this shape (:func:`migrate_preference_record`).
PREFERENCE_RECORD_SCHEMA_VERSION = 2
_SUPPORTED_PREFERENCE_SCHEMA_VERSIONS = frozenset({1, 2})


class PreferenceMigrationError(ValueError):
    """A stored preference row is at an unsupported or malformed schema version."""


def _pref_parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PreferenceMigrationError(f"unparseable updated_at: {value!r}") from exc
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return now_utc()


@dataclass(frozen=True)
class PreferenceRecord:
    """One durable, scoped preference value with optimistic-concurrency metadata.

    ``write_version`` is the monotonically increasing optimistic-concurrency
    counter (a valid write increments it by one); ``schema_version`` is the
    record shape version a migration upgrades.  The record is frozen: a stored
    value is replaced, never mutated in place.
    """

    setting_id: str
    scope: str
    scope_key: str
    value: Any
    write_version: int
    updated_by: str
    schema_version: int = PREFERENCE_RECORD_SCHEMA_VERSION
    updated_at: datetime = field(default_factory=now_utc)

    def __post_init__(self) -> None:
        if not isinstance(self.setting_id, str) or not _PREF_SETTING_ID.match(self.setting_id):
            raise PreferenceValidationError(f"preference record has an invalid setting id: {self.setting_id!r}")
        if self.scope not in _PREF_SCOPES:
            raise PreferenceValidationError(f"preference record names an unknown scope: {self.scope!r}")
        if not isinstance(self.scope_key, str) or not _PREF_SCOPE_KEY.match(self.scope_key):
            raise PreferenceValidationError("preference record has an invalid scope key")
        if not isinstance(self.write_version, int) or isinstance(self.write_version, bool) or self.write_version < 1:
            raise PreferenceValidationError("preference record write_version must be an integer >= 1")
        if self.schema_version != PREFERENCE_RECORD_SCHEMA_VERSION:
            raise PreferenceValidationError(
                f"preference record is not at the current schema version {PREFERENCE_RECORD_SCHEMA_VERSION}"
            )
        if not isinstance(self.updated_by, str) or not self.updated_by:
            raise PreferenceValidationError("preference record requires an updater identity")

    def audit_metadata(self, *, key: bytes) -> dict[str, Any]:
        """Non-identifying audit metadata (T002.1 criterion 3).

        Excludes the raw ``value`` (which may be personally identifying) and the
        raw updater/scope-key identity; the scope key is reduced to a short,
        one-way fingerprint so a record can be correlated in an audit log without
        exposing who owns it.

        The fingerprint is a KEYED HMAC-SHA256 over ``scope:scope_key`` (the
        server-held ``key`` is required and never stored beside the tag), so a
        guessable actor identity cannot be recovered by hashing a dictionary of
        candidates — the same protection the chat-content fingerprint uses.  An
        unsalted digest of a known email/slug would be trivially reversible; this
        is not.
        """
        material = _PREF_AUDIT_FINGERPRINT_PREFIX + f"{self.scope}:{self.scope_key}".encode("utf-8")
        fingerprint = hmac.new(require_pref_audit_key(key), material, hashlib.sha256).hexdigest()[:12]
        return {
            "setting_id": self.setting_id,
            "scope": self.scope,
            "scope_key_fingerprint": fingerprint,
            "write_version": self.write_version,
            "schema_version": self.schema_version,
            "updated_at": _rfc3339(self.updated_at),
        }

    def as_dict(self) -> dict[str, Any]:
        """Serialize the record, scrubbing a free-text value on the last hop."""
        value = redact_config_text(self.value) if isinstance(self.value, str) else self.value
        return {
            "setting_id": self.setting_id,
            "scope": self.scope,
            "value": value,
            "write_version": self.write_version,
            "schema_version": self.schema_version,
            "updated_at": _rfc3339(self.updated_at),
        }


def migrate_preference_record(raw: Mapping[str, Any]) -> PreferenceRecord:
    """Upgrade a stored preference row at any supported prior version to current.

    * v1 shape ``{setting_id, scope, actor, value, version, updated_at}`` is
      upgraded by renaming ``actor`` -> ``scope_key``/``updated_by`` and
      ``version`` -> ``write_version`` and stamping the current schema version.
    * v2 is already the current shape and round-trips unchanged.

    An unknown/malformed version fails closed with :class:`PreferenceMigration
    Error`, so a corrupt row never silently loads as the current shape.
    """
    if not isinstance(raw, Mapping):
        raise PreferenceMigrationError("a preference row must be a mapping")
    version = raw.get("schema_version", 1)
    if version not in _SUPPORTED_PREFERENCE_SCHEMA_VERSIONS:
        raise PreferenceMigrationError(f"unsupported preference schema version: {version!r}")
    if version == 1:
        actor = raw.get("actor")
        upgraded = {
            "setting_id": raw.get("setting_id"),
            "scope": raw.get("scope"),
            "scope_key": actor,
            "value": raw.get("value"),
            "write_version": raw.get("version"),
            "updated_by": actor,
            "updated_at": raw.get("updated_at"),
        }
    else:  # version == 2
        upgraded = dict(raw)
    write_version = upgraded.get("write_version")
    if not isinstance(write_version, int) or isinstance(write_version, bool):
        raise PreferenceMigrationError("a preference row must carry an integer write version")
    return PreferenceRecord(
        setting_id=str(upgraded.get("setting_id")),
        scope=str(upgraded.get("scope")),
        scope_key=str(upgraded.get("scope_key")),
        value=upgraded.get("value"),
        write_version=write_version,
        updated_by=str(upgraded.get("updated_by")),
        schema_version=PREFERENCE_RECORD_SCHEMA_VERSION,
        updated_at=_pref_parse_dt(upgraded.get("updated_at")),
    )


# --- T002.3 shared effective-value resolver (precedence + ceiling + fallback) ---


@dataclass(frozen=True)
class EffectiveValue:
    """One resolved effective setting value and how it was derived.

    ``source`` is one of ``stored`` (an actor/project/authority value was set),
    ``default`` (the reviewed descriptor default), ``clamped`` (a value bounded
    down to a policy ceiling), ``repaired`` (an invalidated capability reference
    fell back to a safe state), or ``unset`` (no stored value and no default).
    """

    setting_id: str
    scope: str
    value: Any
    source: str
    repair: str | None = None

    def as_dict(self) -> dict[str, Any]:
        value = redact_config_text(self.value) if isinstance(self.value, str) else self.value
        data: dict[str, Any] = {
            "setting_id": self.setting_id,
            "scope": self.scope,
            "value": value,
            "source": self.source,
        }
        if self.repair is not None:
            data["repair"] = self.repair
        return data


def resolve_effective_value(
    descriptor: Mapping[str, Any],
    stored_value: Any = _PREF_MISSING,
    *,
    ceiling_value: Any = _PREF_MISSING,
    live_valid_refs: Mapping[str, Any] | None = None,
) -> EffectiveValue:
    """Resolve one descriptor's effective value deterministically.

    Order: take the stored value if present else the descriptor default; if the
    setting is a capability reference and its value is not in the current live
    valid set, fall back to a safe state (the default when it is itself valid,
    otherwise unset) with a repair notice — never a hard failure; finally, if the
    setting declares a policy ceiling and the value exceeds the (higher-authority)
    ceiling, clamp it down.

    The numeric ceiling clamp below is INT-ONLY BY DESIGN: it bounds a scalar
    value against a scalar ceiling (e.g. a retention day count against the
    operator maximum).  A ``policy_ceiling`` whose values are non-scalar
    references — such as ``personal.default_chat_route`` bounded by the
    ``policy.route_allowlist_profile`` capability digest — is NOT numerically
    clamped here; its membership is enforced through ``live_valid_refs``.  The
    live consumer scopes the reference-validity set for the setting's
    ``ref_kind`` to exactly the routes/ids the approved profile admits, so an
    out-of-profile reference falls out of that set and is repaired to the safe
    default above — the same fail-safe path an invalidated reference takes.  A
    ref-kind setting therefore stays within its profile via the ref-validity
    path, never via a silent no-op numeric clamp.
    """
    setting_id = str(descriptor.get("id"))
    scope = str(descriptor.get("scope"))
    setting_type = descriptor.get("type")

    if stored_value is not _PREF_MISSING:
        value: Any = stored_value
        source = "stored"
    elif "default" in descriptor:
        value = descriptor["default"]
        source = "default"
    else:
        return EffectiveValue(setting_id, scope, None, "unset")

    ref_kind = descriptor.get("ref_kind")
    if setting_type in ("id_ref", "digest_ref") and ref_kind and live_valid_refs is not None:
        valid = live_valid_refs.get(str(ref_kind))
        if valid is not None and value not in valid:
            default = descriptor.get("default")
            if default is not None and default in valid:
                return EffectiveValue(
                    setting_id, scope, default, "repaired",
                    repair=f"{setting_id} referenced an unavailable {ref_kind}; reset to the reviewed default",
                )
            return EffectiveValue(
                setting_id, scope, None, "repaired",
                repair=f"{setting_id} referenced an unavailable {ref_kind}; unset to a safe state",
            )

    # Int-only clamp: a ref/non-scalar ceiling (e.g. a route bounded by a
    # capability-profile digest) is intentionally NOT handled here — its
    # enforcement is the ref-validity path above, not this scalar bound.
    if (
        isinstance(descriptor.get("policy_ceiling"), Mapping)
        and ceiling_value is not _PREF_MISSING
        and isinstance(value, int)
        and not isinstance(value, bool)
        and isinstance(ceiling_value, int)
        and not isinstance(ceiling_value, bool)
        and value > ceiling_value
    ):
        return EffectiveValue(
            setting_id, scope, ceiling_value, "clamped",
            repair=f"{setting_id} exceeded the policy ceiling and was clamped to {ceiling_value}",
        )

    return EffectiveValue(setting_id, scope, value, source)


def resolve_effective_settings(
    catalog: Mapping[str, Any],
    stored: Mapping[str, Any],
    *,
    live_valid_refs: Mapping[str, Any] | None = None,
) -> dict[str, EffectiveValue]:
    """The single shared effective-value resolver every consumer calls.

    ``stored`` maps ``setting_id -> value`` and is already scope/actor-scoped by
    the caller from the durable store, so this function never crosses a scope
    boundary itself.  Resolution is deterministic: a policy ceiling owned by a
    strictly higher-authority scope clamps a lower-scope value, and an
    invalidated capability reference falls back to a safe state with a repair
    notice.  Because the result is a pure function of ``(catalog, stored,
    live_valid_refs)``, every consumer that passes identical inputs resolves
    identical effective values.
    """
    by_id = {
        str(setting.get("id")): setting
        for setting in catalog.get("settings", [])
        if isinstance(setting, Mapping)
    }
    result: dict[str, EffectiveValue] = {}
    for setting_id, descriptor in sorted(by_id.items()):
        stored_value = stored[setting_id] if setting_id in stored else _PREF_MISSING
        ceiling_value: Any = _PREF_MISSING
        ceiling = descriptor.get("policy_ceiling")
        if isinstance(ceiling, Mapping):
            ceiling_desc = by_id.get(str(ceiling.get("ceiling_setting")))
            if ceiling_desc is not None:
                ceiling_stored = (
                    stored[ceiling_desc["id"]] if ceiling_desc["id"] in stored else _PREF_MISSING
                )
                ceiling_value = resolve_effective_value(
                    ceiling_desc, ceiling_stored, live_valid_refs=live_valid_refs
                ).value
        result[setting_id] = resolve_effective_value(
            descriptor, stored_value, ceiling_value=ceiling_value, live_valid_refs=live_valid_refs
        )
    return result


def reviewed_catalog_valid_refs(catalog: Mapping[str, Any]) -> dict[str, set[Any]]:
    """The conservative reference-validity baseline derived from the catalog.

    Every reference-kind setting in the reviewed descriptor catalog declares a
    reviewed default reference (a route id, a capability digest, …).  Those
    declared defaults are, by construction, the references the reviewer vouched
    for; nothing else is known-valid without a live source.  This returns
    ``{ref_kind -> {reviewed default reference values}}`` so a resolver given
    this baseline serves an unset (default) reference normally while REPAIRING a
    stored reference that is not among the reviewed defaults — i.e. a stale or
    since-removed reference falls back to the safe default instead of being
    served verbatim.

    It is the documented default source :func:`resolve_effective_settings`
    consumers use when no richer LIVE validity source (e.g. the chat-first-voice
    route discovery / the profile-scoped route allowlist) is injected.  A live
    provider, when supplied, replaces this baseline with the actual live set so
    the enforcement tracks the operator's current profile exactly.
    """
    valid: dict[str, set[Any]] = {}
    for setting in catalog.get("settings", []):
        if not isinstance(setting, Mapping):
            continue
        ref_kind = setting.get("ref_kind")
        if ref_kind is None or setting.get("type") not in ("id_ref", "digest_ref"):
            continue
        if "default" in setting:
            valid.setdefault(str(ref_kind), set()).add(setting["default"])
    return valid
