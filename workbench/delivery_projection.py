"""Project-scoped delivery display read-models (plan-task-delivery T002 / T004).

This module is the hub-durable, browser-facing projection layer for the
PRD-to-Deliver loop.  It holds four display read-models, each scoped to one
project and validated against a merged contract on capture:

* **PRD content** (``workbench-prd-content/v1``) — one PRD's bounded, untrusted
  body for redacted rendering.
* **Task references** (``workbench-task-reference/v1``) — a scoped, display-only
  reference to one task inside its owning PRD's plan/feature hierarchy.  Keyed by
  ``(prd_id, task_id)`` so a ``T001`` from two PRDs can never collapse into one
  row (R004, T002 criterion 1).
* **Delivery eligibility** (``workbench-delivery-eligibility/v1``) — a verdict on
  whether one task may enter a Deliver flow now, bound to the source snapshot
  digest it was computed against.  When the task reference's snapshot advances,
  the stored eligibility is stale and cannot be reused for start (T002 criterion
  2); :meth:`MemoryDeliveryProjectionStore.eligibility_for_start` fails closed.
* **Run display rows** and **approval bindings** (T004) — the pinned, immutable
  operational-surface projection so a run list and an approval review show the
  pinned task/PRD titles and the exact authorization bindings, never a bare id,
  and never re-written when the live State projection later changes.

Authority boundary (AGENTS.md): these are supervision display records, not
canonical State.  Storing or reading one grants no claim, lease, evidence, or
effect.  Every browser response is scrubbed on the API last hop with
:func:`workbench.redaction.scrub_config_payload`, so the untrusted PRD body /
task title / attempt label can never ferry a secret, endpoint, or path to the
UI even though the display record deliberately carries free prose.

Scoping is a hard boundary: a record owned by another project is not in this
project's namespace, so a cross-project read raises the same
:class:`UnknownDeliveryRecordError` a genuinely missing record raises — one
project can never learn whether another project's record exists.  This mirrors
:mod:`workbench.run_context_store` and :mod:`workbench.project_context_store`.

``MemoryDeliveryProjectionStore`` is the hermetic, lock-serialized in-memory
implementation.  It is deliberately NOT wired into the live bridge poll loop; a
production backend uses row-level transactions instead of the instance lock.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field, replace
from functools import wraps
from typing import Any, Mapping, Protocol

from .contracts import (
    ContractValidationError,
    validate_delivery_eligibility,
    validate_prd_content,
    validate_task_reference,
)
from .store import StoreError

_PROJECT_ID = re.compile(r"^[a-zA-Z0-9._-]{1,128}$")
_PRD_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_TASK_ID = re.compile(r"^T[0-9]{3}(\.[0-9]{1,3})?$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_STATUS_TOKEN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_RUN_ID = re.compile(r"^run_[a-zA-Z0-9_-]{8,128}$")
_APPROVAL_ID = re.compile(r"^approval_[a-zA-Z0-9_-]{4,128}$")
_RFC3339 = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,9})?(Z|[+-][0-9]{2}:[0-9]{2})$"
)
#: Human attempt labels and titles are bounded free prose; safety is enforced by
#: redaction on the API last hop, not by a pattern here (repo convention).
_ATTEMPT_LABEL = re.compile(r"^.{1,120}$", re.DOTALL)

_UNKNOWN = "unknown delivery record"


class DeliveryProjectionError(StoreError):
    """A delivery projection operation violates its scoping/validity contract."""


class UnknownDeliveryRecordError(DeliveryProjectionError):
    """No such delivery record for this project.

    Raised identically for a genuinely missing record and for another project's
    record, so a cross-project probe cannot learn whether the record exists.
    """


class DeliveryImmutableError(DeliveryProjectionError):
    """A pinned run row or approval binding cannot be rewritten.

    A run display row and an approval binding are captured once and are
    thereafter frozen: a later task/PRD rename or profile refresh can never
    rewrite the pinned historical names (T004 criterion 2).
    """


class StaleEligibilityError(DeliveryProjectionError):
    """The stored eligibility was computed against a superseded snapshot.

    The task reference's source snapshot advanced after eligibility was
    computed, so the verdict cannot be reused to start a run (T002 criterion 2).
    """


class NotEligibleError(DeliveryProjectionError):
    """The stored eligibility verdict is blocked or stale; a start is refused."""


def derive_stale_eligibility(reference: Mapping[str, Any]) -> dict[str, Any]:
    """Build a fresh, valid ``stale.snapshot_superseded`` verdict for a reference.

    Used when a stored verdict's bound snapshot no longer matches the current
    task reference: the served eligibility is re-derived as stale rather than
    replaying a now-superseded ``eligible`` verdict.
    """
    ref = reference["ref"]
    verdict = {
        "schema_version": "workbench-delivery-eligibility/v1",
        "ref": {"prd_id": ref["prd_id"], "task_id": ref["task_id"], "prd_revision": ref["prd_revision"]},
        "scoped_id": reference["scoped_id"],
        "eligible": False,
        "state": "stale",
        "reasons": [
            {
                "class": "stale",
                "code": "stale.snapshot_superseded",
                "content_trust": "untrusted_task_data",
                "explanation": "The task snapshot advanced since eligibility was computed; refresh to re-check.",
            }
        ],
    }
    validate_delivery_eligibility(verdict)
    return verdict


@dataclass(frozen=True)
class RunDisplayRow:
    """One pinned, immutable operational-surface run row (T004).

    The ``headline`` is the pinned task title when present, so no primary
    operational list falls back to a bare scoped id when a title exists
    (criterion 1).  Every field is a scalar copy captured at start time; a later
    change to the live State projection cannot reinterpret it (criterion 2).
    """

    run_id: str
    run_label: str
    scoped_id: str
    prd_id: str
    task_id: str
    prd_revision: int
    task_title: str
    prd_title: str
    status: str
    attempt_label: str
    started_at: str
    workflow_digest: str
    capability_profile_digest: str
    route_digest: str | None = None

    def __post_init__(self) -> None:
        _require(bool(_RUN_ID.match(self.run_id)), "run row run_id is invalid")
        _require(bool(_PRD_ID.match(self.prd_id)), "run row prd_id is invalid")
        _require(bool(_TASK_ID.match(self.task_id)), "run row task_id is invalid")
        _require(self.scoped_id == f"{self.prd_id}:{self.task_id}", "run row scoped_id does not match its reference")
        _require(
            self.run_label == f"{self.scoped_id}@r{self.prd_revision}",
            "run row run_label is not the immutable <scoped_id>@r<prd_revision> label",
        )
        _require(isinstance(self.prd_revision, int) and self.prd_revision >= 1, "run row prd_revision is invalid")
        _require(isinstance(self.task_title, str) and 0 < len(self.task_title) <= 500, "run row task_title is invalid")
        _require(isinstance(self.prd_title, str) and 0 < len(self.prd_title) <= 500, "run row prd_title is invalid")
        _require(bool(_STATUS_TOKEN.match(self.status)), "run row status is invalid")
        _require(bool(_ATTEMPT_LABEL.match(self.attempt_label or "")), "run row attempt_label is invalid")
        _require(bool(_RFC3339.match(self.started_at)), "run row started_at is not RFC3339")
        _require(bool(_DIGEST.match(self.workflow_digest)), "run row workflow_digest is invalid")
        _require(bool(_DIGEST.match(self.capability_profile_digest)), "run row capability_profile_digest is invalid")
        _require(self.route_digest is None or bool(_DIGEST.match(self.route_digest)), "run row route_digest is invalid")

    @property
    def headline(self) -> str:
        """The pinned task title, or the scoped id only when no title exists."""
        return self.task_title if self.task_title.strip() else self.scoped_id

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "run_id": self.run_id,
            "run_label": self.run_label,
            "scoped_id": self.scoped_id,
            "prd_id": self.prd_id,
            "task_id": self.task_id,
            "prd_revision": self.prd_revision,
            "headline": self.headline,
            "task_title": self.task_title,
            "prd_title": self.prd_title,
            "status": self.status,
            "attempt_label": self.attempt_label,
            "started_at": self.started_at,
            "workflow_digest": self.workflow_digest,
            "capability_profile_digest": self.capability_profile_digest,
        }
        if self.route_digest is not None:
            data["route_digest"] = self.route_digest
        return data


@dataclass(frozen=True)
class ApprovalBinding:
    """One immutable approval-review binding (T004 criterion 4).

    Exposes every exact, safe binding an operator needs to authorize a delivery
    action — the scoped task, the run label, the action and its canonical
    payload hash, the pinned workflow/profile digests, and the expiry — by
    reference and digest only.  It carries no raw command, path, credential, or
    diff body.
    """

    approval_id: str
    scoped_id: str
    run_label: str
    action: str
    payload_hash: str
    bridge_id: str
    expires_at: str
    workflow_digest: str
    capability_profile_digest: str

    def __post_init__(self) -> None:
        _require(bool(_APPROVAL_ID.match(self.approval_id)), "approval binding id is invalid")
        _require(
            bool(re.match(r"^[a-z0-9][a-z0-9._-]{0,63}:T[0-9]{3}(\.[0-9]{1,3})?$", self.scoped_id)),
            "approval binding scoped_id is invalid",
        )
        _require(
            bool(re.match(r"^[a-z0-9][a-z0-9._-]{0,63}:T[0-9]{3}(\.[0-9]{1,3})?@r[0-9]+$", self.run_label)),
            "approval binding run_label is invalid",
        )
        _require(bool(re.match(r"^[a-z][a-z0-9_.]{0,63}$", self.action)), "approval binding action is invalid")
        _require(
            bool(re.match(r"^(sha256:[a-f0-9]{64}|[a-f0-9]{64})$", self.payload_hash)),
            "approval binding payload_hash is invalid",
        )
        _require(bool(re.match(r"^[a-zA-Z0-9._-]{1,128}$", self.bridge_id)), "approval binding bridge_id is invalid")
        _require(bool(_RFC3339.match(self.expires_at)), "approval binding expires_at is not RFC3339")
        _require(bool(_DIGEST.match(self.workflow_digest)), "approval binding workflow_digest is invalid")
        _require(
            bool(_DIGEST.match(self.capability_profile_digest)),
            "approval binding capability_profile_digest is invalid",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "scoped_id": self.scoped_id,
            "run_label": self.run_label,
            "action": self.action,
            "payload_hash": self.payload_hash,
            "bridge_id": self.bridge_id,
            "expires_at": self.expires_at,
            "workflow_digest": self.workflow_digest,
            "capability_profile_digest": self.capability_profile_digest,
        }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DeliveryProjectionError(message)


@dataclass
class DeliveryProjectionRows:
    """The persisted row containers shared by store instances (hub-restart sim)."""

    prd_content: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    references: dict[str, dict[tuple[str, str], dict[str, Any]]] = field(default_factory=dict)
    eligibility: dict[str, dict[tuple[str, str], tuple[dict[str, Any], str]]] = field(default_factory=dict)
    run_rows: dict[str, dict[str, RunDisplayRow]] = field(default_factory=dict)
    approvals: dict[str, dict[str, ApprovalBinding]] = field(default_factory=dict)


class DeliveryProjectionStore(Protocol):
    def get_task_reference(self, project_id: str, prd_id: str, task_id: str) -> dict[str, Any]: ...
    def get_eligibility(self, project_id: str, prd_id: str, task_id: str) -> dict[str, Any]: ...


class MemoryDeliveryProjectionStore:
    """Hermetic, lock-serialized delivery display projection store."""

    def __init__(self, rows: DeliveryProjectionRows | None = None) -> None:
        self._lock = threading.RLock()
        self.rows = rows if rows is not None else DeliveryProjectionRows()

    @staticmethod
    def _scope(project_id: str) -> str:
        if not isinstance(project_id, str) or not _PROJECT_ID.match(project_id):
            raise DeliveryProjectionError("a store operation requires a valid acting project scope")
        return project_id

    # --- PRD content --------------------------------------------------------

    def capture_prd_content(self, project_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        scope = self._scope(project_id)
        try:
            validate_prd_content(payload)
        except ContractValidationError as exc:
            raise DeliveryProjectionError(f"prd content is not valid: {exc}") from exc
        prd_id = str(payload["prd"]["prd_id"])
        stored = dict(payload)
        self.rows.prd_content.setdefault(scope, {})[prd_id] = stored
        return dict(stored)

    def get_prd_content(self, project_id: str, prd_id: str) -> dict[str, Any]:
        scope = self._scope(project_id)
        namespace = self.rows.prd_content.get(scope)
        if namespace is None or prd_id not in namespace:
            raise UnknownDeliveryRecordError(_UNKNOWN)
        return dict(namespace[prd_id])

    # --- Task references ----------------------------------------------------

    def capture_task_reference(self, project_id: str, reference: Mapping[str, Any]) -> dict[str, Any]:
        scope = self._scope(project_id)
        try:
            validate_task_reference(reference)
        except ContractValidationError as exc:
            raise DeliveryProjectionError(f"task reference is not valid: {exc}") from exc
        ref = reference["ref"]
        key = (str(ref["prd_id"]), str(ref["task_id"]))
        stored = dict(reference)
        self.rows.references.setdefault(scope, {})[key] = stored
        return dict(stored)

    def list_task_references(self, project_id: str, prd_id: str) -> list[dict[str, Any]]:
        scope = self._scope(project_id)
        namespace = self.rows.references.get(scope, {})
        rows = [dict(value) for (pid, _tid), value in namespace.items() if pid == prd_id]
        # Sort on the full (prd_id, task_id) key so ordering is total and stable.
        rows.sort(key=lambda r: (r["ref"]["prd_id"], r["ref"]["task_id"]))
        return rows

    def get_task_reference(self, project_id: str, prd_id: str, task_id: str) -> dict[str, Any]:
        scope = self._scope(project_id)
        namespace = self.rows.references.get(scope)
        key = (prd_id, task_id)
        if namespace is None or key not in namespace:
            raise UnknownDeliveryRecordError(_UNKNOWN)
        return dict(namespace[key])

    # --- Delivery eligibility ----------------------------------------------

    def capture_eligibility(self, project_id: str, verdict: Mapping[str, Any]) -> dict[str, Any]:
        scope = self._scope(project_id)
        try:
            validate_delivery_eligibility(verdict)
        except ContractValidationError as exc:
            raise DeliveryProjectionError(f"delivery eligibility is not valid: {exc}") from exc
        ref = verdict["ref"]
        key = (str(ref["prd_id"]), str(ref["task_id"]))
        # Bind the verdict to the snapshot digest of the CURRENT task reference
        # so a later snapshot advance is detectable as staleness. A verdict with
        # no reference to bind against is refused: eligibility is never free of
        # its pinned source.
        references = self.rows.references.get(scope, {})
        reference = references.get(key)
        if reference is None:
            raise DeliveryProjectionError(
                "cannot capture eligibility without a task reference to bind its source snapshot"
            )
        bound_digest = str(reference["source"]["snapshot_digest"])
        self.rows.eligibility.setdefault(scope, {})[key] = (dict(verdict), bound_digest)
        return dict(verdict)

    def _current_snapshot_digest(self, scope: str, key: tuple[str, str]) -> str | None:
        reference = self.rows.references.get(scope, {}).get(key)
        return str(reference["source"]["snapshot_digest"]) if reference is not None else None

    def get_eligibility(self, project_id: str, prd_id: str, task_id: str) -> dict[str, Any]:
        scope = self._scope(project_id)
        key = (prd_id, task_id)
        namespace = self.rows.eligibility.get(scope)
        if namespace is None or key not in namespace:
            raise UnknownDeliveryRecordError(_UNKNOWN)
        verdict, bound_digest = namespace[key]
        current = self._current_snapshot_digest(scope, key)
        if current is not None and current != bound_digest:
            # The source snapshot advanced: serve a freshly derived stale verdict
            # rather than replaying the now-superseded stored one.
            return derive_stale_eligibility(self.rows.references[scope][key])
        return dict(verdict)

    def eligibility_for_start(
        self, project_id: str, prd_id: str, task_id: str, expected_snapshot_digest: str,
    ) -> dict[str, Any]:
        """Return the verdict only if it may start a run; fail closed otherwise.

        A stored verdict that is not ``eligible``, whose bound snapshot differs
        from the current task reference, or whose current reference differs from
        the caller's pinned ``expected_snapshot_digest`` cannot be reused for
        start (T002 criterion 2). This is the wired staleness gate T005 calls.
        """
        scope = self._scope(project_id)
        key = (prd_id, task_id)
        namespace = self.rows.eligibility.get(scope)
        if namespace is None or key not in namespace:
            raise UnknownDeliveryRecordError(_UNKNOWN)
        verdict, bound_digest = namespace[key]
        current = self._current_snapshot_digest(scope, key)
        if current is None or current != bound_digest:
            raise StaleEligibilityError("eligibility was computed against a superseded snapshot")
        if expected_snapshot_digest != current:
            raise StaleEligibilityError("the pinned intent snapshot no longer matches the current task reference")
        if verdict.get("state") != "eligible" or not verdict.get("eligible"):
            raise NotEligibleError("the task is not eligible to start a delivery run")
        return dict(verdict)

    # --- Run display rows (T004) -------------------------------------------

    def capture_run_row(self, project_id: str, row: RunDisplayRow) -> RunDisplayRow:
        scope = self._scope(project_id)
        _require(isinstance(row, RunDisplayRow), "capture_run_row requires a RunDisplayRow")
        namespace = self.rows.run_rows.setdefault(scope, {})
        existing = namespace.get(row.run_id)
        if existing is not None:
            if existing.as_dict() != row.as_dict():
                raise DeliveryImmutableError("a run display row is already pinned for this run and cannot be rewritten")
            return existing
        namespace[row.run_id] = row
        return row

    def get_run_row(self, project_id: str, run_id: str) -> RunDisplayRow:
        scope = self._scope(project_id)
        namespace = self.rows.run_rows.get(scope)
        if namespace is None or run_id not in namespace:
            raise UnknownDeliveryRecordError(_UNKNOWN)
        return namespace[run_id]

    def list_run_rows(
        self,
        project_id: str,
        *,
        prd_id: str | None = None,
        task_id: str | None = None,
        status: str | None = None,
        route_digest: str | None = None,
        capability_profile_digest: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[RunDisplayRow]:
        """Group and filter pinned run rows by project, PRD, task, status,
        route/profile, and time (T004 criterion 3).

        Sorted on the full ``(started_at, run_id)`` tuple so repeated attempts
        for one task are ordered deterministically and distinguished by their
        human attempt label and start time.
        """
        scope = self._scope(project_id)
        rows = list(self.rows.run_rows.get(scope, {}).values())
        selected = [
            row
            for row in rows
            if (prd_id is None or row.prd_id == prd_id)
            and (task_id is None or row.task_id == task_id)
            and (status is None or row.status == status)
            and (route_digest is None or row.route_digest == route_digest)
            and (capability_profile_digest is None or row.capability_profile_digest == capability_profile_digest)
            and (since is None or row.started_at >= since)
            and (until is None or row.started_at <= until)
        ]
        selected.sort(key=lambda row: (row.started_at, row.run_id))
        return selected

    # --- Approval bindings (T004) ------------------------------------------

    def capture_approval_binding(self, project_id: str, binding: ApprovalBinding) -> ApprovalBinding:
        scope = self._scope(project_id)
        _require(isinstance(binding, ApprovalBinding), "capture_approval_binding requires an ApprovalBinding")
        namespace = self.rows.approvals.setdefault(scope, {})
        existing = namespace.get(binding.approval_id)
        if existing is not None:
            if existing.as_dict() != binding.as_dict():
                raise DeliveryImmutableError("an approval binding is already pinned and cannot be rewritten")
            return existing
        namespace[binding.approval_id] = binding
        return binding

    def get_approval_binding(self, project_id: str, approval_id: str) -> ApprovalBinding:
        scope = self._scope(project_id)
        namespace = self.rows.approvals.get(scope)
        if namespace is None or approval_id not in namespace:
            raise UnknownDeliveryRecordError(_UNKNOWN)
        return namespace[approval_id]


def _synchronize_memory_store() -> None:
    """Wrap every public MemoryDeliveryProjectionStore method under its lock."""

    def _guard(method):
        @wraps(method)
        def _locked(self, *args, **kwargs):
            with self._lock:
                return method(self, *args, **kwargs)

        return _locked

    for _name in (
        "capture_prd_content", "get_prd_content",
        "capture_task_reference", "list_task_references", "get_task_reference",
        "capture_eligibility", "get_eligibility", "eligibility_for_start",
        "capture_run_row", "get_run_row", "list_run_rows",
        "capture_approval_binding", "get_approval_binding",
    ):
        setattr(MemoryDeliveryProjectionStore, _name, _guard(getattr(MemoryDeliveryProjectionStore, _name)))


_synchronize_memory_store()
