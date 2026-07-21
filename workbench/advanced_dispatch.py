"""Parallel multi-route dispatch onto sibling turns (advanced-model-playground T008).

Advanced mode can fan one prompt out across several reviewed routes at once to
compare them.  Each parallel attempt must be an ORDINARY sibling turn under a
shared parent -- not a special merged record -- with its own route, controls,
budget, and single terminal state, and the attempts must be isolated: one
attempt cancelling, timing out, or failing can never mutate another's outcome.

This slice builds strictly on the T003 runtime and the merged chat store:

* **Fan-out onto sibling turns.**  :func:`dispatch_parallel` forks N streaming
  ``mode="advanced"`` sibling turns under the shared parent through the SAME
  :class:`~workbench.conversation_store.ConversationStore` (sequentially, so each
  gets a distinct ``sibling_index``), runs each attempt's bounded stream
  concurrently via :func:`~workbench.advanced_runtime.run_advanced_stream`, and
  settles each turn to its own terminal status.  No parallel identity is minted;
  every attempt is a normal ``chat-turn.v1`` sibling.
* **Reject undeclared routes before any Serving request.**  Every dispatch is
  preflight-validated against the frozen discovery snapshot
  (:func:`~workbench.advanced_routes.validate_advanced_selection`) and against the
  declared concurrency/budget bounds BEFORE a single transport is opened or a
  single turn is forked; an undeclared route (or an out-of-bounds control, or a
  batch over budget) fails the whole dispatch closed, so nothing partial runs.
* **Per-attempt isolation.**  Each attempt owns a distinct request id (its own
  durable lifecycle record), a distinct cancellation token, and a distinct result
  slot; a thread captures its own exception into its slot so one attempt's crash
  never propagates to a sibling.  The only shared mutable state under concurrency
  is the reconnect-safe lifecycle store, whose per-conversation sequence
  allocator is lock-serialized, so concurrent attempts draw globally-unique,
  strictly-monotonic sequence numbers and no terminal is lost.

Everything is hermetic: the Serving transport is injected per attempt and no HTTP
client or endpoint is representable anywhere in the dispatch path.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from .advanced_routes import (
    AdvancedRouteSelection,
    DiscoveredAdvancedRoutes,
    validate_advanced_selection,
)
from .advanced_runtime import (
    AdvancedState,
    AdvancedTurnResult,
    run_advanced_stream,
)
from .chat_stream import DEFAULT_MAX_OUTPUT_TOKENS, CancellationToken, ServingStreamTransport
from .conversation_models import ConversationActor, TurnRedaction
from .models import new_id
from .response_lifecycle_store import SafeUsage

#: Hard ceiling on how many attempts one dispatch may fan out, independent of the
#: declared concurrency, so a misdeclared budget cannot request an unbounded fan.
MAX_DISPATCH_ATTEMPTS = 16


class ParallelDispatchError(RuntimeError):
    """A parallel dispatch violates its declared concurrency or budget bounds."""


@dataclass(frozen=True)
class DispatchBudget:
    """The declared concurrency and total-output bounds for one dispatch.

    ``max_concurrency`` bounds how many sibling attempts may run at once (the
    advanced-branch.v1 budget caps this at 4; this slice enforces whatever is
    declared, up to :data:`MAX_DISPATCH_ATTEMPTS`).  ``max_total_output_tokens``
    optionally bounds the sum of every attempt's requested output so a fan-out
    cannot exceed the operator's total token budget.
    """

    max_concurrency: int
    max_total_output_tokens: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.max_concurrency, bool) or not isinstance(self.max_concurrency, int):
            raise ParallelDispatchError("max_concurrency must be an int")
        if not 1 <= self.max_concurrency <= MAX_DISPATCH_ATTEMPTS:
            raise ParallelDispatchError(
                f"max_concurrency must be in [1, {MAX_DISPATCH_ATTEMPTS}]: {self.max_concurrency}"
            )
        if self.max_total_output_tokens is not None:
            if isinstance(self.max_total_output_tokens, bool) or not isinstance(self.max_total_output_tokens, int):
                raise ParallelDispatchError("max_total_output_tokens must be an int or None")
            if self.max_total_output_tokens < 1:
                raise ParallelDispatchError("max_total_output_tokens must be positive")


@dataclass(frozen=True)
class RouteDispatch:
    """One attempt in a parallel dispatch: a route + prompt + injected transport."""

    route_id: str
    prompt: str
    transport: ServingStreamTransport
    request_id: str
    controls: Any = field(default_factory=dict)
    instructions: str | None = None
    structured_output_mode: str = "text"
    output_validator: Callable[[str], bool] | None = None
    cancel: CancellationToken | None = None
    usage: SafeUsage | None = None
    route_request_id: str | None = None
    summary: str | None = None


@dataclass(frozen=True)
class SiblingAttempt:
    """The isolated outcome of one parallel attempt: its own sibling turn + state."""

    index: int
    route_id: str
    request_id: str
    turn_id: str
    sibling_index: int
    state: AdvancedState | None
    turn_status: str
    result: AdvancedTurnResult | None
    error: str | None


@dataclass(frozen=True)
class ParallelDispatchResult:
    """The per-attempt results of one parallel dispatch, in declared order."""

    attempts: tuple[SiblingAttempt, ...]

    @property
    def authoritative(self) -> bool:
        # A parallel comparison is preference evidence only, never delivery proof.
        return False


def _preflight(
    dispatches: list[RouteDispatch],
    discovered: DiscoveredAdvancedRoutes,
    budget: DispatchBudget,
) -> list[AdvancedRouteSelection]:
    """Validate every dispatch against the allowlist and budget before any effect.

    Runs BEFORE a single transport is opened or turn forked: an undeclared route,
    an out-of-bounds control, an over-concurrency batch, or an over-budget total
    output fails the whole dispatch closed.
    """
    if not dispatches:
        raise ParallelDispatchError("a parallel dispatch requires at least one route")
    if len(dispatches) > budget.max_concurrency:
        raise ParallelDispatchError(
            f"parallel dispatch of {len(dispatches)} exceeds declared max_concurrency {budget.max_concurrency}"
        )
    seen_request_ids: set[str] = set()
    selections: list[AdvancedRouteSelection] = []
    total_output = 0
    for dispatch in dispatches:
        if not isinstance(dispatch, RouteDispatch):
            raise ParallelDispatchError("each dispatch must be a RouteDispatch")
        if dispatch.request_id in seen_request_ids:
            raise ParallelDispatchError(f"duplicate dispatch request id: {dispatch.request_id}")
        seen_request_ids.add(dispatch.request_id)
        # Fail closed on an undeclared route / out-of-bounds control BEFORE any
        # Serving request; the typed AdvancedRouteError reason names the cause.
        selection = validate_advanced_selection(dispatch.route_id, dispatch.controls, discovered)
        selections.append(selection)
        total_output += selection.controls_dict().get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)
    if budget.max_total_output_tokens is not None and total_output > budget.max_total_output_tokens:
        raise ParallelDispatchError(
            f"parallel dispatch total output {total_output} exceeds budget {budget.max_total_output_tokens}"
        )
    return selections


def dispatch_parallel(
    *,
    store: Any,
    actor: ConversationActor,
    conversation_id: str,
    parent_turn_id: str,
    dispatches: list[RouteDispatch],
    discovered: DiscoveredAdvancedRoutes,
    lifecycle_store: Any,
    budget: DispatchBudget,
    redaction: TurnRedaction | None = None,
) -> ParallelDispatchResult:
    """Fan a prompt out across N routes as isolated sibling turns.

    Preflights every dispatch against the frozen discovery snapshot and the
    declared concurrency/budget bounds (rejecting an undeclared route before any
    Serving request), forks a distinct streaming ``mode="advanced"`` sibling turn
    per attempt on the SAME store, runs the attempts concurrently with per-attempt
    isolation, and settles each turn to its own terminal status.  One attempt's
    cancellation, timeout, failure, or crash never mutates another's outcome.
    """
    if not isinstance(discovered, DiscoveredAdvancedRoutes):
        raise ParallelDispatchError("discovered advanced routes must be the module's own frozen snapshot")
    if not isinstance(budget, DispatchBudget):
        raise ParallelDispatchError("dispatch requires a DispatchBudget")
    selections = _preflight(dispatches, discovered, budget)  # raises before any effect

    redaction = redaction if redaction is not None else TurnRedaction("redacted", "advanced-trace-v1")
    # Fork the sibling turns sequentially so each draws a distinct sibling_index
    # from the single-threaded store; only the streaming runs concurrently.
    turns = [
        store.branch_turn(
            actor, conversation_id, parent_turn_id,
            role="assistant", status="streaming", redaction=redaction, mode="advanced",
        )
        for _ in dispatches
    ]

    results: list[AdvancedTurnResult | None] = [None] * len(dispatches)
    errors: list[str | None] = [None] * len(dispatches)
    barrier = threading.Barrier(len(dispatches))

    def _run_attempt(index: int) -> None:
        dispatch = dispatches[index]
        try:
            # Line every thread up so the attempts genuinely overlap and the
            # shared lifecycle allocator is actually contended.
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:  # pragma: no cover - a sibling crashed before the barrier
            pass
        try:
            results[index] = run_advanced_stream(
                selection=selections[index],
                prompt=dispatch.prompt,
                transport=dispatch.transport,
                branch_id=new_id("advbranch"),
                conversation_id=conversation_id,
                turn_id=turns[index].id,
                lifecycle_store=lifecycle_store,
                actor=actor,
                request_id=dispatch.request_id,
                instructions=dispatch.instructions,
                cancel=dispatch.cancel,
                structured_output_mode=dispatch.structured_output_mode,
                output_validator=dispatch.output_validator,
                route_request_id=dispatch.route_request_id,
                usage=dispatch.usage,
                summary=dispatch.summary,
            )
        except Exception as exc:  # noqa: BLE001 - isolate this attempt's failure to its own slot
            errors[index] = f"{type(exc).__name__}: {exc}"

    threads = [threading.Thread(target=_run_attempt, args=(i,)) for i in range(len(dispatches))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    attempts: list[SiblingAttempt] = []
    for index, dispatch in enumerate(dispatches):
        result = results[index]
        error = errors[index]
        turn = turns[index]
        if result is not None:
            settled = store.advance_turn_status(actor, conversation_id, turn.id, result.turn_status)
            turn_status = settled.status
            state = result.state
        else:
            # An attempt that crashed outright still settles its own sibling turn
            # to a terminal ``failed`` -- isolated from the others.
            settled = store.advance_turn_status(actor, conversation_id, turn.id, "failed")
            turn_status = settled.status
            state = None
        attempts.append(
            SiblingAttempt(
                index=index,
                route_id=dispatch.route_id,
                request_id=dispatch.request_id,
                turn_id=turn.id,
                sibling_index=turn.lineage.sibling_index,
                state=state,
                turn_status=turn_status,
                result=result,
                error=error,
            )
        )
    return ParallelDispatchResult(attempts=tuple(attempts))
