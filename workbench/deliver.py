"""Atomic, idempotent Deliver-from-a-task coordinator (plan-task-delivery T005).

A Deliver turns a validated :mod:`workbench.contracts` Deliver *intent* into a
typed :mod:`~workbench.contracts` Deliver *start receipt*.  It reuses the
typed-operation spine's receipt/idempotency discipline (a lock-serialized,
at-most-once effect keyed by the intent's ``intent_digest``) so that:

* **Preconditions run before any effect.**  A stale snapshot, changed
  dependency, active run, invalid worktree, lost lease, missing capability, or
  unapproved PRD is refused with a typed, ordered code BEFORE the launch
  callable (the State claim / Codex launch) is ever invoked.  A refused start
  stores no receipt and leaves no partial effect — it stays retriable.
* **The start is atomic.**  The whole check→act path holds the store lock, so
  two concurrent or retried starts with the same idempotency key can never both
  launch: the first commits the accepted receipt, the second observes it and
  replays it as a ``duplicate`` without re-executing.
* **A State acceptance never precedes a successful approved merge.**  The launch
  callable only *starts* a run (a claim + Codex launch); it does not accept.

The coordinator is deliberately NOT wired into the live bridge poll loop; the
``launch`` effect is injected, so the whole surface is hermetically testable and
the not-wired gate holds.  A production caller supplies a launch callable that
performs the real State claim and Codex launch under a fenced worktree lease.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import timedelta
from functools import wraps
from typing import Any, Callable, Mapping

from .contracts import ContractValidationError, validate_deliver_start_receipt, validate_deliver_intent
from .models import now_utc
from .redaction import redact_config_text
from .store import StoreError


class DeliverError(StoreError):
    """A Deliver start violates its atomicity/idempotency/validity contract."""


def _rfc3339(dt) -> str:
    # RFC3339 with a 'Z' zulu suffix, matching the receipt schema's pinned
    # pattern (no FormatChecker runs, so the string shape must be exact). The
    # coordinator's timestamps are always UTC (``now_utc``), so a literal 'Z'
    # is correct.
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class DeliverRefusal:
    """A typed, human-safe preflight refusal (mirrors the operation-refusal shape)."""

    code: str
    safe_summary: str
    retryable: bool = False


# The precondition order is normative: each check dominates the ones below it,
# so the FIRST failing precondition is the reported reason (T005 criterion 2).
_PRECONDITION_ORDER: tuple[tuple[str, str, str, bool], ...] = (
    ("stale_snapshot", "deliver.stale_snapshot",
     "The task snapshot advanced since this Deliver was prepared; refresh and retry.", True),
    ("dependency_changed", "deliver.dependency_changed",
     "A dependency task changed since this Deliver was prepared; refresh and retry.", True),
    ("active_run", "deliver.active_run",
     "A delivery run is already active for this task; wait for it to finish.", True),
    ("invalid_worktree", "deliver.invalid_worktree",
     "The selected worktree is not valid for this task.", False),
    ("lease_lost", "deliver.lease_unavailable",
     "The worktree lease could not be acquired or was lost; retry when it is free.", True),
    ("capability_missing", "deliver.capability_missing",
     "A required capability is not in the approved profile for this delivery.", False),
    ("prd_unapproved", "deliver.prd_unapproved",
     "The owning PRD is not approved for delivery.", False),
)


@dataclass(frozen=True)
class DeliverPreconditions:
    """The ordered, typed preconditions a Deliver must satisfy before any effect.

    Each flag is ``True`` when that precondition is VIOLATED.  :meth:`check`
    returns the first violated precondition's :class:`DeliverRefusal` in the
    normative order, or ``None`` when all pass — so the reported reason is always
    the dominant one and a start proceeds only when every precondition holds.
    """

    stale_snapshot: bool = False
    dependency_changed: bool = False
    active_run: bool = False
    invalid_worktree: bool = False
    lease_lost: bool = False
    capability_missing: bool = False
    prd_unapproved: bool = False

    def check(self) -> DeliverRefusal | None:
        for attr, code, summary, retryable in _PRECONDITION_ORDER:
            if getattr(self, attr):
                return DeliverRefusal(code=code, safe_summary=summary, retryable=retryable)
        return None


@dataclass
class DeliverStartRows:
    """The persisted receipt container shared by store instances (restart sim)."""

    receipts: dict[str, dict[str, Any]] = field(default_factory=dict)


def _receipt_task_ref(intent: Mapping[str, Any]) -> dict[str, Any]:
    task_ref = intent["task_ref"]
    return {
        "prd_id": task_ref["prd_id"],
        "task_id": task_ref["task_id"],
        "prd_revision": task_ref["prd_revision"],
        "scoped_id": task_ref["scoped_id"],
    }


def _denied_receipt(intent: Mapping[str, Any], refusal: DeliverRefusal) -> dict[str, Any]:
    return {
        "schema_version": "workbench-deliver-start-receipt/v1",
        "intent_digest": intent["intent_digest"],
        "status": "denied",
        "task_ref": _receipt_task_ref(intent),
        "error": {
            "code": refusal.code,
            # Scrub the free-text summary as a last-hop defense before it enters a
            # served record; a safe summary is untouched, a leaky one is neutered
            # rather than raising, and the schema safeText pattern still gates it.
            "safe_summary": redact_config_text(refusal.safe_summary),
            "retryable": bool(refusal.retryable),
        },
        "redaction": {"status": "metadata_only"},
    }


def _accepted_receipt(intent: Mapping[str, Any], run: Mapping[str, Any]) -> dict[str, Any]:
    selections = intent["selections"]
    workflow = selections["workflow"]
    run_block = {
        # The run block's workflow/profile digests are taken from the INTENT's
        # approved selections, never from the launch callable, so the receipt
        # provably reflects exactly the workflow and profile that were approved.
        "run_id": run["run_id"],
        "workflow_digest": workflow["digest"],
        "capability_profile_digest": selections["capability_profile_digest"],
        "started_at": run["started_at"],
        "deadline": run["deadline"],
    }
    if run.get("traceparent"):
        run_block["traceparent"] = str(run["traceparent"])
    return {
        "schema_version": "workbench-deliver-start-receipt/v1",
        "intent_digest": intent["intent_digest"],
        "status": "accepted",
        "task_ref": _receipt_task_ref(intent),
        "run": run_block,
        "redaction": {"status": "redacted"},
    }


class MemoryDeliverStartStore:
    """Hermetic, lock-serialized atomic idempotent Deliver-start store."""

    def __init__(self, rows: DeliverStartRows | None = None, deadline_seconds: int = 3600) -> None:
        # The whole precondition->launch->store path runs under this reentrant
        # lock so two concurrent same-key starts cannot both launch: the first
        # commits the accepted receipt, the second observes it and replays.
        self._lock = threading.RLock()
        self.rows = rows if rows is not None else DeliverStartRows()
        self._deadline_seconds = deadline_seconds

    def start(
        self,
        intent: Mapping[str, Any],
        *,
        launch: Callable[[], Mapping[str, Any]],
        preconditions: Callable[[], DeliverRefusal | None] | DeliverPreconditions | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Start a Deliver run at most once per ``intent_digest``.

        Returns ``(receipt, replayed)``.  A tamper-evident intent is validated
        first.  A key that already started replays its accepted receipt as a
        ``duplicate`` (``replayed=True``) and never re-launches.  Otherwise the
        ordered preconditions run; the FIRST violated one denies the start
        (no receipt stored, no effect, retriable).  Only when every precondition
        holds is ``launch`` invoked exactly once, and its run turned into a
        stored accepted receipt.  If ``launch`` raises, nothing is stored and the
        start stays retriable — no fabricated success.
        """
        try:
            validate_deliver_intent(intent)
        except ContractValidationError as exc:
            raise DeliverError(f"deliver intent is invalid: {exc}") from exc

        key = str(intent["intent_digest"])
        with self._lock:
            existing = self.rows.receipts.get(key)
            if existing is not None:
                replay = dict(existing)
                replay["status"] = "duplicate"
                validate_deliver_start_receipt(replay, intent)
                return replay, True

            refusal = self._resolve_preconditions(preconditions)
            if refusal is not None:
                receipt = _denied_receipt(intent, refusal)
                validate_deliver_start_receipt(receipt, intent)
                # Deliberately NOT stored: a denied start left no run and stays
                # retriable once the precondition is satisfied.
                return receipt, False

            run = launch()
            if not isinstance(run, Mapping) or not run.get("run_id"):
                raise DeliverError("a Deliver launch must return a run block with a run_id")
            receipt = _accepted_receipt(intent, run)
            validate_deliver_start_receipt(receipt, intent)
            self.rows.receipts[key] = receipt
            return receipt, False

    def _resolve_preconditions(
        self, preconditions: Callable[[], DeliverRefusal | None] | DeliverPreconditions | None,
    ) -> DeliverRefusal | None:
        if preconditions is None:
            return None
        if isinstance(preconditions, DeliverPreconditions):
            return preconditions.check()
        return preconditions()

    def default_run_block(self, run_id: str, *, traceparent: str | None = None) -> dict[str, Any]:
        """A convenience run block with a bounded deadline for a launch callable."""
        started = now_utc()
        deadline = started + timedelta(seconds=self._deadline_seconds)
        block: dict[str, Any] = {
            "run_id": run_id,
            "started_at": _rfc3339(started),
            "deadline": _rfc3339(deadline),
        }
        if traceparent:
            block["traceparent"] = traceparent
        return block

    def get_receipt(self, intent_digest: str) -> dict[str, Any] | None:
        with self._lock:
            receipt = self.rows.receipts.get(intent_digest)
            return dict(receipt) if receipt is not None else None


def _synchronize_memory_store() -> None:
    """Wrap the mutating/idempotent methods under the instance lock.

    ``start`` already takes the lock explicitly (it must span the whole
    check->launch->store window); the wrapper is idempotent with the reentrant
    lock, but we only guard the auxiliary readers/writers here.
    """

    def _guard(method):
        @wraps(method)
        def _locked(self, *args, **kwargs):
            with self._lock:
                return method(self, *args, **kwargs)

        return _locked

    for _name in ("get_receipt",):
        setattr(MemoryDeliverStartStore, _name, _guard(getattr(MemoryDeliverStartStore, _name)))


_synchronize_memory_store()
