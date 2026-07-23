"""Idempotent typed operation receipts, reconciliation records, and one-time
operation approval grants.

Extracted verbatim from ``workbench.store``; re-exported there for backward
compatibility.  See ``docs`` (state-context-operations:T006.3) for the contract.
"""
from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping

from .contracts import validate_operation_receipt
from .models import (
    OperationRef, OperationReceipt, OperationRefusal, RECONCILIATION_REASONS,
    ReconciliationItem, new_receipt_id, new_reconciliation_id, now_utc,
)
from .store_base import StoreError


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

