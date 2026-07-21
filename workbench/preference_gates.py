"""Policy-operation gate: route project/system policy changes through the typed
operation spine (preferences-configuration:T004.2 / T004.3 / T004).

A policy-changing form never mutates effective configuration directly.  It names
a typed, versioned :class:`~workbench.models.PolicyOperation` (never a generic
command), and this gate drives it through the SAME reviewed spine the rest of the
product uses:

* previewing and requesting are distinct from performing -- :meth:`preview`
  is a pure function of the operation payload and touches no store (T004.2 #1);
* an approval binds the FULL canonical operation payload hash
  (:attr:`PolicyOperation.digest`) and is consumed exactly once, constant-time,
  cross-actor / cross-scope / replay / expiry all fail closed
  (reuses :class:`~workbench.store.MemoryOperationApprovalStore`, T004.2 #2);
* a hub-local change commits ATOMICALLY through the optimistic-concurrency
  preference store and never touches a bridge worktree lease (T004.2 #3);
* an operation naming an EXTERNAL provider's policy returns a truthful
  read-only / disabled result unless that provider declared the exact allowed
  operation (T004.2 #4 / T004 #4);
* every completed attempt records ONE redacted, schema-valid typed receipt and,
  for an unknown external outcome, exactly one reconciliation item -- and the
  effective value changes only after a matching successful receipt is recorded
  (reuses :class:`~workbench.store.MemoryOperationReceiptStore`, T004.2 #5 /
  T004.3).

Nothing here is wired into the live bridge poll loop.  Like the other supervision
read/write models it is exercised only through the injectable service, which the
hub app leaves ``None`` (fail-closed 503) until an operator-reviewed policy path
is separately enabled.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .models import (
    OperationRef,
    OperationRefusal,
    PolicyOperation,
    PolicyOperationError,
    PolicyOperationPreview,
    PreferenceValidationError,
    build_policy_operation,
)
from .redaction import redact_config_text
from .store import (
    MemoryOperationApprovalStore,
    MemoryOperationReceiptStore,
    MemoryPreferenceStore,
    OperationOutcome,
    OperationReceiptStoreError,
    PreferenceStoreError,
    StalePreferenceWriteError,
    UnknownOutcomeError,
    UnknownPreferenceError,
)

#: The provider identity the hub owns.  A policy operation naming this provider
#: is a HUB-LOCAL change committed atomically through the preference store; any
#: other provider is an external effect the hub does not own.
HUB_POLICY_PROVIDER = "anvil-preferences"

#: The pinned contract version the synthesized operation reference carries.  The
#: real authority binding is the payload hash (``operation_digest``), not this.
POLICY_OPERATION_CONTRACT_VERSION = "1.0.0"

_ACTOR_SCOPES = ("personal", "project")


class PolicyGateError(ValueError):
    """A policy-operation request is structurally malformed before any effect."""


@dataclass(frozen=True)
class PolicyOperationRequest:
    """One typed policy-change request a form submits (never a raw command).

    Closed and validated: the only way to name an effect is a declared
    ``operation`` kind against a declared ``setting_id`` at its owning ``scope``.
    ``provider`` selects the effect target -- the hub (a local atomic commit) or
    an external provider (read-only unless the provider declared the operation).
    """

    setting_id: str
    scope: str
    operation: str
    op_version: int
    value: Any = None
    project_id: str | None = None
    provider: str = HUB_POLICY_PROVIDER

    def __post_init__(self) -> None:
        if self.operation not in ("preference.set", "preference.reset"):
            raise PolicyGateError(f"unknown policy operation kind: {self.operation!r}")
        if self.scope not in ("personal", "project", "policy"):
            raise PolicyGateError(f"a policy operation cannot target scope {self.scope!r}")
        if not isinstance(self.op_version, int) or isinstance(self.op_version, bool) or self.op_version < 1:
            raise PolicyGateError("op_version must be an integer >= 1")
        if self.scope == "project" and not self.project_id:
            raise PolicyGateError("a project-scope policy operation requires a project_id")

    @property
    def is_hub_local(self) -> bool:
        return self.provider == HUB_POLICY_PROVIDER


@dataclass(frozen=True)
class ExternalPolicyDeclaration:
    """A provider's explicit declaration that ONE external policy op is allowed.

    Absent a matching declaration, an external policy operation is refused
    read-only.  ``executor`` is the hermetic provider adapter the gate invokes
    when (and only when) the declaration matches; it returns an
    :class:`OperationOutcome` or raises :class:`UnknownOutcomeError` for an
    ambiguous/interrupted provider result (which the gate reconciles).
    """

    provider: str
    setting_id: str
    action: str
    executor: Callable[[PolicyOperation], OperationOutcome]

    @property
    def key(self) -> tuple[str, str]:
        return (self.provider, self.setting_id)


def _redacted(text: str, limit: int = 200) -> str:
    return redact_config_text(str(text))[:limit]


class PolicyGateService:
    """The wired policy-operation entrypoint the browser policy forms route through.

    Composes the reviewed spine primitives rather than re-implementing them:
    :class:`MemoryPreferenceStore` (hub-local atomic commit + optimistic
    versioning), :class:`MemoryOperationApprovalStore` (one-time, hash-bound
    approval consumption), and :class:`MemoryOperationReceiptStore` (idempotent
    typed receipts + exactly-one reconciliation).
    """

    def __init__(
        self,
        catalog: Mapping[str, Any],
        *,
        preference_store: MemoryPreferenceStore | None = None,
        receipt_store: MemoryOperationReceiptStore | None = None,
        approval_store: MemoryOperationApprovalStore | None = None,
        external_declarations: tuple[ExternalPolicyDeclaration, ...] = (),
        live_valid_refs: Mapping[str, Any] | None = None,
    ) -> None:
        self.catalog = catalog
        self._by_id: dict[str, Mapping[str, Any]] = {
            str(s.get("id")): s for s in catalog.get("settings", []) if isinstance(s, Mapping)
        }
        self.preferences = preference_store or MemoryPreferenceStore(catalog)
        self.receipts = receipt_store or MemoryOperationReceiptStore()
        self.approvals = approval_store or MemoryOperationApprovalStore()
        self._external: dict[tuple[str, str], ExternalPolicyDeclaration] = {
            declaration.key: declaration for declaration in external_declarations
        }
        self._live_valid_refs = live_valid_refs

    # -- shared construction (pure) -----------------------------------------

    def _build_operation(self, request: PolicyOperationRequest) -> PolicyOperation:
        descriptor = self._by_id.get(request.setting_id)
        if descriptor is None:
            # The same indistinct not-found the read surface uses: a policy-change
            # form can never learn whether an unknown/foreign setting id exists.
            raise UnknownPreferenceError("unknown preference")
        if descriptor.get("scope") != request.scope:
            # The named scope must match the descriptor's owning scope; a mismatch
            # is refused as the indistinct not-found, not a distinguishable error.
            raise UnknownPreferenceError("unknown preference")
        # build_policy_operation fails closed on a secret/path-like/deployment-only
        # descriptor and typed-validates a `set` value BEFORE anything is hashed.
        return build_policy_operation(
            descriptor,
            operation=request.operation,
            op_version=request.op_version,
            value=request.value if request.operation == "preference.set" else None,
        )

    def _scope_key(self, request: PolicyOperationRequest, actor: str) -> str:
        if request.scope == "personal":
            return actor
        if request.scope == "project":
            return str(request.project_id)
        return "policy"

    def _effect_summary(self, request: PolicyOperationRequest, op: PolicyOperation) -> str:
        if op.operation == "preference.reset":
            return _redacted(f"reset {op.setting_id} ({op.scope}) to its inherited default")
        target = "hub-local" if request.is_hub_local else f"external provider {request.provider}"
        return _redacted(f"set {op.setting_id} ({op.scope}) via {target}")

    def _operation_ref(self, request: PolicyOperationRequest, op: PolicyOperation) -> OperationRef:
        # The synthesized reference carries the OPERATION TYPE (setting id) and the
        # PAYLOAD HASH (operation_digest); provider distinguishes a hub-local from
        # an external target.  It carries no adapter, command, value, or path.
        return OperationRef(
            provider=request.provider,
            id=op.setting_id,
            contract_version=POLICY_OPERATION_CONTRACT_VERSION,
            operation_digest=op.digest,
        )

    @staticmethod
    def _idempotency_key(scope_key: str, op: PolicyOperation) -> str:
        # Stable per operation IDENTITY (scope + full payload hash) so a retry of
        # the same operation replays the stored receipt and NEVER consumes a new
        # approval implicitly (T004.3 #3).
        return f"policyop:{scope_key}:{op.digest}"

    def _scope_external_ref(self, request: PolicyOperationRequest) -> dict[str, str]:
        ref = {"scope": request.scope}
        if request.scope == "project" and request.project_id:
            ref["project"] = str(request.project_id)
        return ref

    # -- preview (pure; cannot mutate) --------------------------------------

    def preview(self, request: PolicyOperationRequest, actor: str) -> dict[str, Any]:
        """Return a read-only preview of the operation; mutate NOTHING.

        Builds the typed operation (which validates the value) and a
        :class:`PolicyOperationPreview` sharing its canonical digest, so an
        approval bound to this preview commits to exactly the applied effect.
        No store is touched: previewing is distinct from requesting or performing.

        The previewed ``idempotency_key`` is computed with the REAL ``actor`` (the
        same value ``apply`` uses), so for a personal-scope operation the previewed
        key equals the apply-time receipt key -- a status GET against it is not a
        perpetual 404.  For a project/policy-scope operation the scope key is
        actor-independent, so threading the actor is simply harmless.
        """
        op = self._build_operation(request)
        preview = PolicyOperationPreview(op, self._effect_summary(request, op))
        return {
            "preview": preview.as_dict(),
            "target": request.provider,
            "hub_local": request.is_hub_local,
            "requires_approval": True,
            "idempotency_key": self._idempotency_key(self._scope_key(request, actor), op),
        }

    def approval_binding(self, request: PolicyOperationRequest, actor: str) -> dict[str, Any]:
        """The exact (action, payload_hash, actor, scope_key) an approval must bind.

        Exposed so the operator's approval-granting surface (out of band, a human
        decision) can mint a grant bound to precisely this effect.  The gate never
        mints its own approval -- ``apply`` only CONSUMES one.
        """
        op = self._build_operation(request)
        return {
            "action": op.operation,
            "payload_hash": op.digest,
            "actor": actor,
            "scope_key": self._scope_key(request, actor),
        }

    # -- perform (approval-gated, atomic-or-preserved) ----------------------

    def apply(
        self, request: PolicyOperationRequest, *, actor: str, grant_id: str,
    ) -> tuple[dict[str, Any], bool]:
        """Perform the operation once: consume the approval, commit or refuse, record.

        Returns ``(receipt, replayed)``.  The whole approval-consume -> effect ->
        receipt path runs INSIDE the idempotent receipt store, so a replay of the
        same operation identity returns the stored receipt without re-consuming an
        approval or repeating an effect.  A refused/failed attempt is not persisted
        under its key (it stays retriable) and leaves the prior value intact.
        """
        op = self._build_operation(request)
        scope_key = self._scope_key(request, actor)
        ref = self._operation_ref(request, op)
        idempotency_key = self._idempotency_key(scope_key, op)
        run_id = f"policyrun_{op.digest[7:19]}"
        command_id = f"policycmd_{op.digest[7:19]}"

        def executor() -> OperationOutcome:
            # 1. Consume the one-time, hash-bound approval FIRST.  A replayed,
            #    expired, payload-changed, cross-actor, or cross-scope grant fails
            #    closed here -- before any effect -- collapsing to a single typed
            #    reason so the failure leaks no oracle about which dimension missed.
            try:
                self.approvals.consume(grant_id, op.operation, op.digest, actor, scope_key)
            except OperationReceiptStoreError:
                # Narrow to the approval store's typed refusal (missing / replayed /
                # expired / payload-changed / cross-actor / cross-scope).  A genuine
                # programming defect is NOT collapsed into a denied approval: it
                # propagates so it surfaces instead of masquerading as fail-closed.
                return OperationOutcome(
                    "denied",
                    error=OperationRefusal(
                        "approval.invalid",
                        "the policy approval is missing, replayed, expired, changed, "
                        "or bound to another actor or scope",
                    ),
                )

            # 2. External provider target: truthful read-only unless the owning
            #    provider declared the exact allowed operation.  The hub never
            #    pretends to have mutated Serving/State policy it does not own, and
            #    the prior effective value is untouched (T004.2 #4 / T004 #4).
            if not request.is_hub_local:
                declaration = self._external.get((request.provider, op.setting_id))
                if declaration is None or declaration.action != op.operation:
                    return OperationOutcome(
                        "denied",
                        error=OperationRefusal(
                            "policy.external_read_only",
                            f"{request.provider} policy is read-only from the hub; "
                            "no allowed operation is declared for this setting",
                        ),
                    )
                # Declared allowed: dispatch to the provider's hermetic adapter.
                # An UnknownOutcomeError it raises becomes exactly one reconciliation
                # item + a reconciliation_required receipt inside record_attempt.  A
                # GENERIC exception (e.g. a RuntimeError mid-push) is ALSO an unknown
                # outcome: the external effect may have PARTIALLY landed at the
                # provider, so it must reach the SAME reconciliation path rather than
                # propagating to a bare 500 with zero receipt and zero reconciliation
                # item.  Convert it to an UnknownOutcomeError (redacted summary) so
                # record_attempt records a reconciliation_required receipt + exactly
                # one reconciliation item.  Fail closed: never a fabricated success.
                try:
                    return declaration.executor(op)
                except UnknownOutcomeError:
                    raise
                except Exception as exc:
                    raise UnknownOutcomeError(
                        _redacted(
                            f"{request.provider} policy adapter raised mid-effect; the "
                            "external outcome is unknown and may be partially applied"
                        ),
                        external_ref=self._scope_external_ref(request),
                        reason="interrupted",
                    ) from exc

            # 3. Hub-local atomic commit through the optimistic-concurrency store.
            #    NO worktree lease is consulted: a hub-owned setting is not a bridge
            #    effect (T004.2 #3).  A stale version preserves the prior value
            #    (T004.3 #2), but the SAME grant cannot drive a successful retry:
            #    ``op_version`` is part of the canonical payload (and thus the
            #    digest the approval binds), so a correctly-versioned re-submit has a
            #    DIFFERENT digest and requires a new approval.  ``retryable`` is the
            #    same-grant/same-identity signal, so it is False here -- a True value
            #    would falsely imply the burned grant can be replayed (it cannot; a
            #    same-grant retry always fails ``approval.invalid``).  The message
            #    states the honest recovery path.
            try:
                self._commit_hub_local(request, op, scope_key, actor)
            except StalePreferenceWriteError:
                return OperationOutcome(
                    "failed",
                    error=OperationRefusal(
                        "policy.stale_version",
                        "the stored policy value moved since this operation was built; "
                        "reload, rebuild at the current op_version, and obtain a new "
                        "approval (the prior grant is bound to the stale digest)",
                        retryable=False,
                    ),
                )
            except (PreferenceValidationError, PreferenceStoreError, PolicyOperationError) as exc:
                # A pre-effect refusal: the store rejected the write and nothing
                # was committed, so the prior value is intact and the attempt is
                # retriable.  The reason is redacted before it reaches the receipt.
                return OperationOutcome(
                    "denied",
                    error=OperationRefusal("operation.input_invalid", _redacted(str(exc))),
                )
            return OperationOutcome("succeeded", external_ref=self._scope_external_ref(request))

        return self.receipts.record_attempt(
            run_id=run_id,
            command_id=command_id,
            operation=ref,
            idempotency_key=idempotency_key,
            executor=executor,
        )

    def _commit_hub_local(
        self, request: PolicyOperationRequest, op: PolicyOperation, scope_key: str, actor: str,
    ) -> None:
        # The operation's ``op_version`` is the write version it produces, so the
        # optimistic expected (current) version is one less.  Every branch is a
        # single lock-guarded compare-and-write in the store: it commits fully or
        # raises and leaves the prior value untouched.
        expected_version = op.op_version - 1
        if request.scope in _ACTOR_SCOPES:
            if op.operation == "preference.set":
                self.preferences.set_preference(
                    request.scope, scope_key, op.setting_id, op.value, expected_version, actor,
                )
            else:
                self.preferences.reset_preference(
                    request.scope, scope_key, op.setting_id, expected_version, actor,
                    live_valid_refs=self._live_valid_refs,
                )
            return
        # policy scope (approval_gated): the approval was consumed above, so this
        # is the already-authorized authority write, optimistic-version guarded.
        if op.operation == "preference.set":
            self.preferences.seed_authority_value(
                "policy", op.setting_id, op.value, updated_by=actor, expected_version=expected_version,
            )
        else:
            self.preferences.clear_authority_value(
                "policy", op.setting_id, expected_version=expected_version, updated_by=actor,
            )

    # -- browser-safe status ------------------------------------------------

    def receipt(self, idempotency_key: str) -> dict[str, Any] | None:
        """The stored terminal receipt for an operation identity, or ``None``."""
        return self.receipts.get_receipt(idempotency_key)

    def reconciliation(self, idempotency_key: str) -> dict[str, Any] | None:
        """The reconciliation item for an unknown-outcome operation, or ``None``."""
        return self.receipts.get_reconciliation(idempotency_key)

    def reconciliations(self) -> list[dict[str, Any]]:
        """All open reconciliation items (redacted, browser-safe)."""
        return self.receipts.list_reconciliations()
