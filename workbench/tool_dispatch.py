"""Chat capability-profile pinning and typed plugin-tool dispatch.

reviewed-tools-plugins T004 (pin a capability profile per chat session; route
ONLY schema-valid, selected, healthy, in-budget tool requests to typed dispatch)
and T005 (effectful preview/approval grants, invalidate-on-diff, fail-closed
replay/expiry/mismatch/digest-drift, reconcile-not-fabricate on an unknown
outcome).

This lane COMPOSES two merged foundations rather than reinventing them:

* the RTP:T001-T003 plugin authorization boundary -- the reviewed digest-pinned
  catalog, the enable-only capability profile, and :class:`PluginDiscovery`
  (exact ``id``+``digest``, selected-only resolution) plus
  :func:`validate_plugin_request` (schema-valid, digest-consistent, inputs match
  the reviewed tool schema, effectful-requires-approval) -- as the
  REJECT-BEFORE-DISPATCH gate; and
* the SCO:T006 typed-operation spine -- :class:`MemoryOperationApprovalStore`
  (one-time, hash-bound, constant-time approval consumption) and
  :class:`MemoryOperationReceiptStore` (idempotent typed receipts +
  reconciliation-on-unknown) -- as the EXECUTE / APPROVE / RECEIPT machinery,
  reused exactly, never re-implemented.

A plugin ``tool_call`` is dispatched AS a typed operation: a discovered tool maps
to an :class:`OperationRef` (``provider=plugin_id``, ``id=tool_id``,
``contract_version=plugin version``, ``operation_digest=plugin_digest``); the
request digest is the idempotency key; and the tool result becomes an
:class:`OperationOutcome`.  A ``read`` tool dispatches ungated; an
``external_effect``/``state_mutation`` tool consumes a one-time approval bound to
``approval_payload_digest(_plugin_approval_subject(request))`` BEFORE the effect
runs, so a mutated input (a new request digest and a new payload hash) can never
consume a grant minted for the previewed input -- the preview is invalidated by
construction.  An unconfirmed effectful outcome reconciles; it is never retried
or reported as success.

Like the install lane this is hermetic and deliberately NOT wired into the live
bridge poll loop; the browser surface
(:func:`workbench.api.build_chat_tools_router`) stays fail-closed (503) until a
service is injected, and the effectful dispatch/preview entrypoints are
bridge/hub service methods, never a browser mutation path.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .contracts import (
    ContractValidationError,
    _PLUGIN_EFFECTFUL,
    _plugin_approval_subject,
    approval_payload_digest,
    validate_plugin_capability,
    validate_plugin_catalog,
    validate_plugin_preview,
    validate_plugin_request,
)
from .models import OperationRef, OperationRefusal, new_id, safe_receipt_summary
from .plugin_host import DiscoveredPlugin, PluginDiscovery, PluginHostError
from .redaction import redact_config_text
from .store import (
    MemoryOperationApprovalStore,
    MemoryOperationReceiptStore,
    OperationOutcome,
    OperationReceiptStoreError,
    UnknownOutcomeError,
)


# --------------------------------------------------------------------------- #
# Typed reject-before-dispatch reasons.  Every refusal carries a stable ``code``
# so a caller (and a test) asserts the CLAIMED reason, never an incidental
# message match.  A rejected request never runs and never yields a receipt.
# --------------------------------------------------------------------------- #


class ToolDispatchError(RuntimeError):
    """A chat tool request was refused before (or instead of) reaching dispatch.

    ``code`` is a stable machine-checkable reason.  The reject-before-dispatch
    family maps each pre-dispatch guard to its own reason so a caller can tell an
    unknown plugin from a drifted digest from a not-selected tool from an
    actor-mismatched, unhealthy, or over-budget one; each such reason is a
    structural authority fact (not a secret) so distinguishing them leaks nothing.
    The approval-consume family, by contrast, deliberately COLLAPSES every
    one-time-grant failure (replay, expiry, action/hash mismatch, cross-bridge,
    cross-project, digest drift) to a single non-oracular ``approval_invalid`` so a
    probe holding a ``grant_id`` cannot learn which specific grant check failed.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


#: Map a :class:`PluginDiscovery` resolution refusal to this lane's typed reason.
#: Discovery already fails closed on its own code; this only renames it into the
#: chat-dispatch namespace so the reject-before-dispatch reasons are uniform.
_RESOLVE_REASON = {
    "unknown_plugin": "tool.unknown_plugin",
    "digest_drift": "tool.digest_drift",
    "not_enabled": "tool.not_selected",
    "unknown_tool": "tool.unknown_tool",
    "tool_not_enabled": "tool.tool_not_selected",
}


def _safe(text: str) -> str:
    """Scrub free text bound for a preview/refusal that a browser may see."""
    return redact_config_text(str(text)).strip()[:400]


# --------------------------------------------------------------------------- #
# The per-chat-session capability pin (T004).  A chat session pins the reviewed
# catalog + enable-only capability profile the way a run pins a WorkflowSnapshot:
# both are fail-closed validated once at construction and then IMMUTABLE, so the
# tool authority a session can exercise cannot drift underneath it.
# --------------------------------------------------------------------------- #


class ChatToolSession:
    """One chat session's immutable pinned tool capability.

    Pins the reviewed plugin catalog and the enable-only capability profile into
    a :class:`PluginDiscovery`, which fail-closed validates both up front, so an
    unknown/drifted/not-enabled tool request is refused against exactly the
    profile the session was created with -- never a later, live one.  Also pins
    the session's ``actor_id`` (the session is created for exactly one verified
    actor) and ``bridge_id``/``project_id`` (the approval-grant binding scope) and
    a per-session tool-call budget derived from the pinned profile's
    ``limits.max_concurrent_tool_calls``.

    The ``actor_id`` pin is load-bearing authority, not correlation metadata: a
    request whose ``actor.actor_id`` differs from the session's is refused before
    the tool runner (``tool.actor_mismatch``), so a grant minted by one actor's
    session can never be consumed by a dispatch presented under another actor --
    the request's actor block is no longer merely schema-checked.
    """

    def __init__(
        self,
        *,
        session_id: str,
        catalog: Mapping[str, Any],
        capability: Mapping[str, Any],
        actor_id: str,
        bridge_id: str,
        project_id: str,
    ) -> None:
        if not session_id:
            raise ToolDispatchError("tool.invalid_session", "a chat tool session requires an id")
        if not actor_id:
            raise ToolDispatchError("tool.invalid_session", "a chat tool session must pin an actor")
        if not bridge_id or not project_id:
            raise ToolDispatchError("tool.invalid_session", "a chat tool session must pin a bridge and project")
        # Fail closed on either document before the session is usable, exactly as
        # PluginDiscovery does -- a drifted or unsafe catalog/profile is refused
        # at pin time, not first-dispatch time.
        validate_plugin_catalog(catalog)
        validate_plugin_capability(capability)
        self.session_id = str(session_id)
        self.actor_id = str(actor_id)
        self.bridge_id = str(bridge_id)
        self.project_id = str(project_id)
        # run_id/command_id are receipt correlation fields, not authority; derive
        # a stable run id from the session so receipts of one session correlate.
        self.run_id = f"chat_{self.session_id}"
        self.discovery = PluginDiscovery(catalog, capability)
        self._catalog = self.discovery.catalog  # the deep-copied, validated pin
        limits = capability.get("limits") if isinstance(capability.get("limits"), Mapping) else {}
        raw_budget = limits.get("max_concurrent_tool_calls")
        # The pinned profile's tool-call budget.  The pinned
        # ``max_concurrent_tool_calls`` is enforced by the dispatch service as a
        # per-session lifetime ceiling on the number of DISTINCT tool-call
        # dispatches that genuinely execute: admission is reserved ATOMICALLY
        # (check + record under one lock) before any effect, and released if the
        # request is later rejected, so a rejected or replayed request consumes
        # none.  A conservative bound (a runaway chat cannot invoke unbounded
        # tools) that a production async dispatcher would refine into a live
        # in-flight gate.  A profile without an explicit limit gets a single call
        # -- fail safe, never unbounded.
        self.max_tool_calls = raw_budget if isinstance(raw_budget, int) and raw_budget > 0 else 1

    @property
    def catalog(self) -> Mapping[str, Any]:
        return self._catalog

    def list_tools(self) -> list[dict[str, Any]]:
        """The redacted projection of every approved, enabled tool in the pin."""
        return self.discovery.published()


#: A health probe answers whether one pinned tool is currently healthy.  It is a
#: bridge/host concern (a connector down, a plugin unhealthy); the default treats
#: every reviewed+enabled tool as healthy so a session with no probe still routes.
ToolHealthProbe = Callable[[str, str], bool]

#: A tool runner performs the actual (bridge-side) tool invocation and returns a
#: CLASSIFIED :class:`OperationOutcome`.  For an unconfirmed effect it raises
#: :class:`UnknownOutcomeError` (or any exception, which this lane converts to an
#: unknown outcome for an effectful tool) so the effect reconciles rather than
#: being retried or reported as success.
ToolRunner = Callable[[DiscoveredPlugin, Mapping[str, Any]], OperationOutcome]


@dataclass(frozen=True)
class DispatchResult:
    """The typed result of a routed dispatch: a receipt plus whether it replayed."""

    receipt: dict[str, Any]
    replayed: bool


class ChatToolDispatchService:
    """Route pinned chat tool requests to typed dispatch with previews/approvals.

    Composes a :class:`ChatToolSession` pin with the reused typed-operation
    approval + receipt stores.  ``preview`` and ``dispatch`` are the wired
    entrypoints a bridge/hub calls; a rejected request never reaches the tool
    runner, an effectful call consumes a one-time hash-bound approval before it
    runs, and an unconfirmed effectful outcome reconciles.
    """

    def __init__(
        self,
        session: ChatToolSession,
        *,
        health: ToolHealthProbe | None = None,
        receipt_store: MemoryOperationReceiptStore | None = None,
        approval_store: MemoryOperationApprovalStore | None = None,
    ) -> None:
        self._session = session
        self._health: ToolHealthProbe = health or (lambda _p, _t: True)
        self._receipts = receipt_store if receipt_store is not None else MemoryOperationReceiptStore()
        self._approvals = approval_store if approval_store is not None else MemoryOperationApprovalStore()
        # The per-session tool-call budget, tracked as the SET of idempotency keys
        # that hold an admitted slot.  Admission is ATOMIC: :meth:`_reserve_budget`
        # checks the ceiling AND records the key under ONE lock section, BEFORE any
        # approval is consumed.  Two concurrent DISTINCT requests at a ceiling of
        # one therefore can never both be admitted -- exactly one reserves and the
        # other is refused ``tool.over_budget`` -- and an over-budget request never
        # burns a grant or reaches the runner.  Keying admission by idempotency key
        # (not a bare counter) makes a reservation idempotent per request, so a
        # concurrent duplicate of an already-admitted request neither takes a second
        # slot nor is spuriously refused; it goes on to replay the one committed
        # receipt.  A slot is a LIFETIME reservation: a genuinely executed dispatch
        # keeps it, while a request rejected AFTER reserving (bad approval,
        # runner-contract, ...) releases it and a replayed request never takes one,
        # so only real executions count against the ceiling.
        self._budget_lock = threading.Lock()
        self._admitted: set[str] = set()

    # --- read surface -------------------------------------------------------- #

    @property
    def session(self) -> ChatToolSession:
        return self._session

    @property
    def approvals(self) -> MemoryOperationApprovalStore:
        return self._approvals

    def list_tools(self) -> list[dict[str, Any]]:
        return self._session.list_tools()

    def get_receipt(self, idempotency_key: str) -> dict[str, Any] | None:
        return self._receipts.get_receipt(idempotency_key)

    def get_reconciliation(self, idempotency_key: str) -> dict[str, Any] | None:
        return self._receipts.get_reconciliation(idempotency_key)

    def list_reconciliations(self) -> list[dict[str, Any]]:
        return self._receipts.list_reconciliations(self._session.run_id)

    # --- preview (T005) ------------------------------------------------------ #

    def preview(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Return a redacted, hash-bound preview of a pinned tool call.

        Non-mutating: it validates the request against the pinned catalog,
        resolves the exact reviewed+enabled tool, and emits a
        ``plugin-preview/v1`` whose ``approval.payload_hash`` (for an effectful
        tool) binds the exact typed subject.  Because that hash covers the exact
        inputs, a later request with a changed input previews a different hash, so
        an approval minted from this preview cannot authorize the changed call.

        Preview is the step that PRODUCES the payload_hash an approval binds, so it
        does NOT require the request to already carry an approval block (that
        requirement belongs at dispatch): ``require_approval=False``.  The produced
        ``approval.payload_hash`` is the exact subject hash the dispatch will
        recompute and the grant will bind, even though the not-yet-approved
        preview request and the later approved dispatch request have different
        ``request_digest`` values.
        """
        discovered, effect, effectful = self._resolve_for(
            request, catalog_checked=True, require_approval=False
        )
        plugin_ref = dict(request["plugin"])
        preview: dict[str, Any] = {
            "schema_version": "workbench-plugin-preview/v1",
            "preview_id": new_id("plugprev"),
            "request_digest": request["request_digest"],
            "kind": "tool_call",
            "plugin": {
                "plugin_id": plugin_ref["plugin_id"],
                "plugin_digest": plugin_ref["plugin_digest"],
            },
            "effect": effect,
            "summary": _safe(
                f"Invoke tool {discovered.tool['tool_id']} on plugin {discovered.plugin_id}."
            ) or "Invoke the selected reviewed plugin tool.",
            "changes": [
                {
                    "change_kind": "external_call" if effectful else "data_scope",
                    "detail": _safe(
                        f"The {effect} tool {discovered.tool['tool_id']} runs with the previewed typed inputs."
                    ) or "The reviewed tool runs with the previewed typed inputs.",
                }
            ],
            "redaction": {"status": "redacted"},
        }
        if effectful:
            subject_hash = approval_payload_digest(_plugin_approval_subject(request))
            preview["approval"] = {
                "required": True,
                "action": "invoke_effect_tool",
                "payload_hash": subject_hash,
            }
        else:
            preview["approval"] = {"required": False}
        # Validate the preview against the reviewed contract AND check it echoes
        # the request it previews (digest/kind/plugin, and the bound subject hash).
        validate_plugin_preview(preview, request)
        return preview

    # --- dispatch (T004 routing + T005 approval/receipt/reconcile) ----------- #

    def dispatch(self, request: Mapping[str, Any], tool_runner: ToolRunner) -> DispatchResult:
        """Route ONE pinned tool request to typed dispatch, or fail closed.

        Order (every reject happens BEFORE the tool runner is ever called):

        1. structural request validation (schema + recomputed request digest);
        2. idempotent replay -- a stored receipt for this request digest is
           returned without re-consuming an approval, re-running the runner, or
           reserving a budget slot;
        3. session actor identity + resolution against the PINNED catalog+profile
           -- an actor-mismatched request, and unknown / drifted / not-selected /
           unknown-tool / tool-not-selected each fail closed on their own reason;
        4. full request validation against the pinned catalog -- inputs must match
           the reviewed tool schema, an effectful call must carry an approval, and
           a read must NOT carry one;
        5. health -- an unhealthy tool is refused;
        6. budget -- the lifetime tool-call ceiling is RESERVED ATOMICALLY (check
           + record in one lock section) BEFORE any grant is consumed, so two
           concurrent distinct requests at a ceiling of one admit exactly one and
           refuse the other ``tool.over_budget``; an over-budget request never
           burns a grant or reaches the runner;
        7. approval -- an effectful call CONSUMES its one-time hash-bound grant
           before the effect runs; a replayed/expired/mismatched grant fails
           closed on the collapsed ``approval_invalid`` reason;
        8. dispatch through the reused receipt store -- exactly-once execution,
           a redacted typed receipt, and reconciliation (never a fabricated
           success) for an unconfirmed effectful outcome.

        Any rejection AFTER the budget reservation (a bad approval, a
        runner-contract violation) RELEASES the reserved slot in a ``finally``, and
        a replay (a sibling that only replayed a committed receipt) never keeps
        one, so only a genuinely executed dispatch spends budget.
        """
        # 1. structural validation, independent of the pinned catalog.
        try:
            validate_plugin_request(request)
        except ContractValidationError as exc:
            raise ToolDispatchError("tool.invalid_request", _safe(str(exc))) from exc
        if request.get("kind") != "tool_call":
            raise ToolDispatchError("tool.unsupported_kind", "chat dispatch invokes plugin tools only")

        idem = str(request["request_digest"])

        # 2. idempotent replay: never re-consume, re-run, or re-budget.
        existing = self._receipts.get_receipt(idem)
        if existing is not None:
            return DispatchResult(receipt=existing, replayed=True)

        # 3 + 4. resolve against the pin and validate inputs against the pinned
        # catalog (reject-before-dispatch).
        discovered, effect, effectful = self._resolve_for(request, catalog_checked=True)
        tool_id = str(discovered.tool["tool_id"])

        # 5. health.
        if not self._health(discovered.plugin_id, tool_id):
            raise ToolDispatchError("tool.unhealthy", "the selected tool is not healthy")

        # 6. budget: RESERVE the lifetime ceiling ATOMICALLY (check + record under
        #    one lock) before any approval is consumed, so two concurrent distinct
        #    requests at a ceiling of one admit exactly one and refuse the other,
        #    and an over-budget request never burns a grant or reaches the runner.
        #    ``reserved`` is True iff THIS call took a new slot (a concurrent
        #    duplicate of an already-admitted key takes none); the slot is released
        #    below unless the dispatch genuinely executes.
        reserved = self._reserve_budget(idem)

        # A rejection AFTER the reservation (bad approval, runner-contract) or a
        # replay must not spend budget: keep the slot ONLY for a genuine execution.
        keep_slot = False
        try:
            # 7. approval: an effectful call consumes its one-time grant now.
            if effectful:
                self._consume_approval(request)

            # 8. dispatch through the reused typed-operation receipt store.
            operation = _operation_ref(discovered)
            inputs = dict(request["tool_call"].get("inputs") or {})

            def executor() -> OperationOutcome:
                # Reached ONLY on a genuine (non-replay) execution: record_attempt
                # returns a stored receipt without calling the executor.
                try:
                    outcome = tool_runner(discovered, inputs)
                except UnknownOutcomeError:
                    raise
                except Exception as exc:  # noqa: BLE001 - convert to a safe outcome
                    if effectful:
                        # The effect may have taken hold; the outcome is UNKNOWN.
                        # Reconcile -- never retry blindly, never fabricate success.
                        raise UnknownOutcomeError(
                            "the plugin tool effect outcome could not be confirmed",
                            reason="unknown_outcome",
                        ) from exc
                    # A read has no external effect to reconcile: record a retriable
                    # failed receipt on a typed refusal instead of vanishing into a 500.
                    return OperationOutcome(
                        "failed",
                        error=OperationRefusal(
                            "operation.runner_failed",
                            safe_receipt_summary(f"the read tool failed: {exc}"),
                            retryable=True,
                        ),
                    )
                if not isinstance(outcome, OperationOutcome):
                    # A malformed RETURN (not an exception) from the runner.  For an
                    # effectful call the effect may already have taken hold, so treat
                    # it EXACTLY like an unknown outcome -- reconcile (receipt + one
                    # reconciliation item) rather than raising a bare error that would
                    # leave the possibly-applied effect with no durable record.  A read
                    # has nothing to reconcile, so it keeps the typed contract error.
                    if effectful:
                        raise UnknownOutcomeError(
                            "the plugin tool effect outcome could not be confirmed",
                            reason="unknown_outcome",
                        )
                    raise ToolDispatchError(
                        "tool.runner_contract", "a tool runner must return an OperationOutcome"
                    )
                return outcome

            receipt, replayed = self._receipts.record_attempt(
                run_id=self._session.run_id,
                command_id=str(request["request_id"]),
                operation=operation,
                idempotency_key=idem,
                executor=executor,
                request_id=str(request["request_id"]),
                unknown_summary="the plugin tool effect outcome could not be confirmed",
            )
            # This dispatch genuinely executed iff record_attempt ran the executor
            # (``replayed`` is False).  A replay -- a concurrent sibling that only
            # returned the committed receipt -- keeps no slot; the genuine sibling
            # that reserved the key already holds it.
            keep_slot = reserved and not replayed
            return DispatchResult(receipt=receipt, replayed=replayed)
        finally:
            # Release the reserved slot on any post-reserve rejection (a raised
            # approval_invalid / runner_contract) or a replay, so only a genuinely
            # executed dispatch counts against the lifetime ceiling.  A concurrent
            # duplicate that reserved nothing (``reserved`` False) never releases
            # the key the genuine sibling holds.
            if reserved and not keep_slot:
                self._release_budget(idem)

    # --- internals ----------------------------------------------------------- #

    def _resolve_for(
        self, request: Mapping[str, Any], *, catalog_checked: bool, require_approval: bool = True
    ) -> tuple[DiscoveredPlugin, str, bool]:
        """Resolve a request against the pin; raise the typed reject reason.

        Runs the same reject-before-dispatch gate used by both ``preview`` and
        ``dispatch`` so they never diverge: structural validity, the session's
        pinned-actor identity, exact-entry resolution against the pinned
        catalog+profile, and -- when ``catalog_checked`` -- full input-schema +
        (unless ``require_approval`` is False) effectful-approval validation.
        """
        try:
            validate_plugin_request(request)
        except ContractValidationError as exc:
            raise ToolDispatchError("tool.invalid_request", _safe(str(exc))) from exc
        if request.get("kind") != "tool_call":
            raise ToolDispatchError("tool.unsupported_kind", "chat dispatch invokes plugin tools only")
        # The session is created for exactly ONE verified actor.  A request whose
        # actor differs from the session's pin is refused here -- before the
        # runner, before any budget, before any grant is consumed -- so a grant
        # minted under one actor's session cannot be exercised by another actor.
        actor = request.get("actor") if isinstance(request.get("actor"), Mapping) else {}
        if str(actor.get("actor_id")) != self._session.actor_id:
            raise ToolDispatchError(
                "tool.actor_mismatch", "the request actor is not the session's pinned actor"
            )
        plugin_ref = request.get("plugin") if isinstance(request.get("plugin"), Mapping) else {}
        tool_call = request.get("tool_call") if isinstance(request.get("tool_call"), Mapping) else {}
        try:
            discovered = self._session.discovery.resolve(
                str(plugin_ref.get("plugin_id")),
                str(plugin_ref.get("plugin_digest")),
                str(tool_call.get("tool_id")),
            )
        except PluginHostError as exc:
            raise ToolDispatchError(
                _RESOLVE_REASON.get(exc.code, "tool.invalid_request"), _safe(str(exc))
            ) from exc
        if catalog_checked:
            try:
                validate_plugin_request(
                    request, self._session.catalog, require_approval=require_approval
                )
            except ContractValidationError as exc:
                # A schema-invalid input, an effectful call missing its approval
                # (dispatch only), or a read carrying an approval is refused here
                # -- before the tool runner.
                raise ToolDispatchError("tool.input_invalid", _safe(str(exc))) from exc
        effect = str(discovered.tool["effect"])
        return discovered, effect, effect in _PLUGIN_EFFECTFUL

    def _consume_approval(self, request: Mapping[str, Any]) -> None:
        """Consume the effectful call's one-time hash-bound approval grant.

        The grant id and payload hash come from the request's own validated
        approval block; the payload hash is RECOMPUTED from the typed subject
        (defence in depth) so a request whose approval no longer binds its own
        inputs cannot consume.  Every consume failure -- an unknown/replayed/
        expired grant, an action/hash mismatch, a cross-bridge/project attempt --
        collapses to the single non-oracular ``approval_invalid`` reason.
        """
        approval = request.get("approval") if isinstance(request.get("approval"), Mapping) else {}
        grant_id = approval.get("grant_id")
        if not isinstance(grant_id, str) or not grant_id:
            raise ToolDispatchError("tool.approval_required", "an effectful tool call requires an approval grant")
        subject_hash = approval_payload_digest(_plugin_approval_subject(request))
        try:
            self._approvals.consume(
                grant_id,
                "invoke_effect_tool",
                subject_hash,
                self._session.bridge_id,
                self._session.project_id,
            )
        except OperationReceiptStoreError as exc:
            raise ToolDispatchError("tool.approval_invalid", "the tool approval is not valid") from exc

    def _reserve_budget(self, idempotency_key: str) -> bool:
        """Atomically admit one request against the lifetime tool-call ceiling.

        The check and the record happen together under ONE lock section, BEFORE
        any approval is consumed, so admission is atomic: two concurrent DISTINCT
        requests at a ceiling of one can never both pass -- exactly one records its
        key and the other is refused ``tool.over_budget`` -- and an over-budget
        request never burns a grant or reaches the runner.

        Admission is keyed by idempotency key so a reservation is idempotent per
        request: a concurrent duplicate of an ALREADY-admitted key (a racing
        replay that slipped past the step-2 receipt short-circuit) neither takes a
        second slot nor is spuriously refused.  Returns True iff THIS call recorded
        a NEW slot (so the caller knows whether it -- and only it -- must release
        on a later rejection); False for such a duplicate, which holds no slot of
        its own.
        """
        with self._budget_lock:
            if idempotency_key in self._admitted:
                return False
            if len(self._admitted) >= self._session.max_tool_calls:
                raise ToolDispatchError(
                    "tool.over_budget", "the chat session has exhausted its pinned tool-call budget"
                )
            self._admitted.add(idempotency_key)
            return True

    def _release_budget(self, idempotency_key: str) -> None:
        """Return a reserved slot when a dispatch does not genuinely execute.

        Called only for the caller that reserved the key, and only when the
        dispatch was rejected after reserving (a bad approval, a runner-contract
        violation) or merely replayed -- never after a genuine execution -- so the
        lifetime ceiling counts exactly the dispatches that really ran.
        """
        with self._budget_lock:
            self._admitted.discard(idempotency_key)


def _operation_ref(discovered: DiscoveredPlugin) -> OperationRef:
    """Map a discovered reviewed plugin tool to its pinned typed operation ref.

    ``provider``/``id``/``contract_version``/``operation_digest`` come only from
    the reviewed catalog entry (never from the caller), so the receipt's
    operation identity is the exact pinned tool, and the ``operation_digest`` is
    the tamper-evident ``plugin_digest``.
    """
    plugin = discovered.plugin
    tool = discovered.tool
    return OperationRef(
        provider=str(plugin["id"]),
        id=str(tool["tool_id"]),
        contract_version=str(plugin["version"]),
        operation_digest=str(plugin["plugin_digest"]),
    )
