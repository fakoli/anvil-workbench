"""Advanced mode in the Chat runtime (advanced-model-playground T003).

Advanced mode is a per-turn/per-branch experiment surface: an operator forks an
EXISTING conversation at an existing parent turn, tunes a reviewed route's
declared controls, and streams a non-authoritative attempt.  It is deliberately
built ON the merged chat runtime rather than beside it:

* **No second conversation or turn store.**  A fork is an ordinary
  ``branch_turn(mode="advanced")`` on the SAME
  :class:`~workbench.conversation_store.ConversationStore`; the advanced turn is
  a normal ``chat-turn.v1`` sibling under the shared parent, and its terminal
  state advances through the same ``advance_turn_status`` path.  There is no
  parallel identity: a branch references ``conversation_id`` and cannot mint a
  new one (:func:`open_advanced_branch` refuses a branch whose
  ``conversation_ref`` names any other conversation).
* **The same bounded stream state machine.**  Streaming reuses
  :class:`~workbench.chat_stream.ChatStreamRelay` (via its prepared-request
  constructor) so the distinct terminal outcomes, cancellation semantics, and
  upstream teardown are shared, and the durable reconnect-safe lifecycle reuses
  :class:`~workbench.response_lifecycle_store.ResponseLifecycleStore`.
* **Distinct, durable states.**  :class:`AdvancedState` refines the relay outcome
  into the seven states the criterion enumerates -- ``complete`` (a normal,
  non-streamed completion), ``streamed`` (an SSE completion), ``cancelled``,
  ``timed_out``, ``schema_invalid`` (a completed response whose structured output
  failed validation), ``malformed_stream`` (an un-parseable frame), and
  ``serving_unavailable`` -- and maps each to exactly one terminal turn status.
  A cancelled, timed-out, malformed, unavailable, or schema-invalid attempt is
  NEVER ``complete``.
* **Non-authoritative isolation.**  An advanced attempt is preference/experiment
  evidence only; :func:`refuse_advanced_evidence` fails closed if a caller tries
  to submit an advanced record as State evidence or attach it to a delivery run,
  and the redacted :func:`build_advanced_trace` record (advanced-trace.v1) has no
  field able to express a delivery claim.

Every free-text field that reaches the served trace is scrubbed through the
config-text redaction and then re-validated against the closed advanced-trace.v1
schema, so a credential, endpoint, path, or hidden-reasoning string can never
ride out inside a trace.  Everything here is hermetic: the Serving transport is
injected, no HTTP client is imported, and no endpoint or credential is
representable.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterator, Mapping

from .advanced_routes import AdvancedRouteSelection
from .chat_stream import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    MAX_PROMPT_CHARS,
    CancellationToken,
    ChatStreamRelay,
    ServingStreamTransport,
    ServingStreamUnavailable,
    StreamOutcome,
)
from .contracts import validate_advanced_trace
from .conversation_models import ConversationActor, TurnRedaction
from .models import new_id, now_utc
from .redaction import redact_config_text
from .response_lifecycle_store import (
    LIFECYCLE_STATE_FOR_OUTCOME,
    SafeUsage,
)

#: A per-attempt advanced trace is preference/experiment evidence only, never a
#: model qualification or a delivery claim.  This module refuses to let one cross
#: into the authoritative evidence path.
NON_AUTHORITATIVE = True

#: Instruction ceiling (mirrors the prompt ceiling); the advanced surface keeps
#: instructions separate from the user input (R003) but both are bounded.
MAX_INSTRUCTIONS_CHARS = MAX_PROMPT_CHARS


class AdvancedRuntimeError(RuntimeError):
    """An Advanced-mode runtime operation violates the chat-runtime contract."""


class AdvancedState(Enum):
    """The exactly-one durable state one Advanced attempt settles into."""

    complete = "complete"  # a normal, non-streamed completion (no deltas seen)
    streamed = "streamed"  # an SSE completion (deltas then a completion)
    cancelled = "cancelled"
    timed_out = "timed_out"
    schema_invalid = "schema_invalid"  # completed, but the structured output failed validation
    malformed_stream = "malformed_stream"  # an un-parseable frame from the stream
    serving_unavailable = "serving_unavailable"


#: The chat-turn.v1 terminal status each advanced state maps to.  Only a genuine
#: completion reaches ``complete``; every other state is interrupted/cancelled/
#: failed so a cancelled or partial attempt can never render as complete.
_TURN_STATUS_FOR_STATE: dict[AdvancedState, str] = {
    AdvancedState.complete: "complete",
    AdvancedState.streamed: "complete",
    AdvancedState.cancelled: "cancelled",
    AdvancedState.timed_out: "interrupted",
    AdvancedState.schema_invalid: "failed",
    AdvancedState.malformed_stream: "failed",
    AdvancedState.serving_unavailable: "failed",
}

#: The advanced-trace.v1 ``status`` each advanced state maps to.
_TRACE_STATUS_FOR_STATE: dict[AdvancedState, str] = {
    AdvancedState.complete: "complete",
    AdvancedState.streamed: "complete",
    AdvancedState.cancelled: "cancelled",
    AdvancedState.timed_out: "timed_out",
    AdvancedState.schema_invalid: "failed",
    AdvancedState.malformed_stream: "failed",
    AdvancedState.serving_unavailable: "serving_unavailable",
}

#: The relay ``StreamOutcome`` -> lifecycle terminal (reused from the store) so a
#: cancelled/timed-out/failed stream persists the same durable terminal the chat
#: runtime does, never a fabricated completion.
_TERMINAL_ERROR_CODE: dict[AdvancedState, str] = {
    AdvancedState.timed_out: "serving_timeout",
    AdvancedState.schema_invalid: "output_schema_invalid",
    AdvancedState.malformed_stream: "malformed_stream",
    AdvancedState.serving_unavailable: "serving_unavailable",
}
_RETRYABLE_STATE = {AdvancedState.timed_out, AdvancedState.serving_unavailable}


def _ts() -> str:
    return now_utc().isoformat()


def build_advanced_request(
    selection: AdvancedRouteSelection, prompt: str, *, instructions: str | None = None,
) -> dict[str, Any]:
    """Assemble the bounded Advanced Responses request from a validated selection.

    Every control comes only from the fail-closed
    :class:`~workbench.advanced_routes.AdvancedRouteSelection` (already bounded to
    its route's declared controls); the route contributes its Serving
    ``model_profile`` and ``route_id`` only -- never an endpoint, URL, or
    credential.  Instructions are kept separate from the user input (R003) and
    both are length-bounded, so nothing unbounded reaches Serving.
    """
    if not isinstance(selection, AdvancedRouteSelection):
        raise AdvancedRuntimeError(
            "bounded request requires the validated AdvancedRouteSelection, "
            "not a caller-assembled mapping"
        )
    if not isinstance(prompt, str) or not prompt:
        raise AdvancedRuntimeError("bounded request requires a non-empty prompt string")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise AdvancedRuntimeError(f"bounded request prompt exceeds the {MAX_PROMPT_CHARS}-char limit")
    if instructions is not None:
        if not isinstance(instructions, str) or len(instructions) > MAX_INSTRUCTIONS_CHARS:
            raise AdvancedRuntimeError("bounded request instructions exceed the declared limit")
    controls = selection.controls_dict()
    request: dict[str, Any] = {
        "model": selection.route.model_profile,
        "route_id": selection.route.route_id,
        "input": prompt,
        "stream": bool(controls.get("response_streaming", True)),
        "max_output_tokens": controls.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS),
    }
    if instructions is not None:
        request["instructions"] = instructions
    if "temperature_milli" in controls:
        request["temperature"] = controls["temperature_milli"] / 1000
    if "reasoning_effort" in controls:
        request["reasoning"] = {"effort": controls["reasoning_effort"]}
    return request


class _ClassifyingTransport:
    """Wrap the injected transport to distinguish a malformed frame from a drop.

    The relay collapses an un-parseable frame and a transport failure into the
    single ``serving_unavailable`` outcome.  Advanced mode must keep
    ``malformed_stream`` and ``serving_unavailable`` distinct and durable, so this
    thin wrapper flags a non-mapping frame (and raises the same
    :class:`ServingStreamUnavailable` the relay would) while passing every real
    frame and every transport-raised failure through untouched.
    """

    def __init__(self, inner: ServingStreamTransport) -> None:
        if not isinstance(inner, ServingStreamTransport):
            raise AdvancedRuntimeError("advanced runtime requires a ServingStreamTransport")
        self._inner = inner
        self.saw_malformed = False

    def open(self, request: Mapping[str, Any], cancel: CancellationToken) -> Iterator[Mapping[str, Any]]:
        upstream = iter(self._inner.open(request, cancel))

        def _gen() -> Iterator[Mapping[str, Any]]:
            for frame in upstream:
                if not isinstance(frame, Mapping):
                    self.saw_malformed = True
                    raise ServingStreamUnavailable("advanced stream frame has an unexpected shape")
                yield frame

        return _gen()


def _default_json_valid(text: str) -> bool:
    try:
        json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return False
    return True


@dataclass(frozen=True)
class AdvancedTurnResult:
    """The settled outcome of one Advanced attempt: durable state + redacted trace.

    ``authoritative`` is always ``False``: an advanced attempt is preference
    evidence, never a delivery claim.
    """

    conversation_id: str
    branch_id: str
    turn_id: str
    state: AdvancedState
    turn_status: str
    lifecycle_state: str
    partial_text: str
    streamed: bool
    structured_output_valid: bool | None
    usage: SafeUsage
    trace: dict[str, Any]

    @property
    def authoritative(self) -> bool:
        return False

    @property
    def is_complete(self) -> bool:
        return self.state in (AdvancedState.complete, AdvancedState.streamed)


def run_advanced_stream(
    *,
    selection: AdvancedRouteSelection,
    prompt: str,
    transport: ServingStreamTransport,
    branch_id: str,
    conversation_id: str,
    turn_id: str,
    lifecycle_store: Any,
    actor: ConversationActor,
    request_id: str,
    instructions: str | None = None,
    cancel: CancellationToken | None = None,
    structured_output_mode: str = "text",
    output_validator: Callable[[str], bool] | None = None,
    route_request_id: str | None = None,
    usage: SafeUsage | None = None,
    summary: str | None = None,
) -> AdvancedTurnResult:
    """Stream one bounded Advanced attempt and settle its durable state + trace.

    Reuses the chat relay's bounded stream state machine and the reconnect-safe
    lifecycle store, refining the settled relay outcome into exactly one
    :class:`AdvancedState`.  A ``json_schema`` structured-output mode runs
    ``output_validator`` (default: the output must parse as JSON) over the
    completed text, so a completed-but-invalid response settles ``schema_invalid``
    -- distinct from a genuine completion and never rendered as complete.  Builds
    and schema-validates the redacted advanced-trace.v1 record before returning.
    """
    if structured_output_mode not in ("text", "json_schema"):
        raise AdvancedRuntimeError(f"unsupported structured output mode: {structured_output_mode!r}")
    request = build_advanced_request(selection, prompt, instructions=instructions)
    classifier = _ClassifyingTransport(transport)
    relay = ChatStreamRelay.for_prepared_request(request, classifier, cancel)

    lifecycle_store.begin(actor, conversation_id, request_id)
    delta_count = 0
    terminal_seq = 0
    for event in relay.stream():
        seq = lifecycle_store.next_seq(actor, request_id)
        if event.kind == "delta":
            delta_count += 1
            # Heartbeat the durable lifecycle so a reconnecting client can resync
            # to the last committed seq without duplicating the response.
            lifecycle_store.advance(actor, request_id, "in_progress", seq=seq)
        else:  # the single terminal frame draws the highest seq for the terminal commit
            terminal_seq = seq

    outcome = relay.outcome
    if outcome is None:  # pragma: no cover - stream() always settles an outcome
        raise AdvancedRuntimeError("advanced stream did not settle a terminal outcome")

    streamed = delta_count > 0
    structured_valid: bool | None = None
    state = _classify_state(outcome, classifier.saw_malformed)
    if state in (AdvancedState.complete, AdvancedState.streamed):
        state = AdvancedState.streamed if streamed else AdvancedState.complete
        if structured_output_mode == "json_schema":
            validator = output_validator if output_validator is not None else _default_json_valid
            structured_valid = bool(validator(relay.partial_text))
            if not structured_valid:
                state = AdvancedState.schema_invalid

    lifecycle_state = LIFECYCLE_STATE_FOR_OUTCOME[outcome.value]
    settled_usage = usage if usage is not None else SafeUsage()
    lifecycle_store.advance(
        actor, request_id, lifecycle_state, usage=settled_usage, seq=terminal_seq,
    )

    trace = build_advanced_trace(
        selection=selection,
        branch_id=branch_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        state=state,
        partial_text=relay.partial_text,
        streamed=streamed,
        structured_output_mode=structured_output_mode,
        structured_valid=structured_valid,
        usage=settled_usage,
        instructions=instructions,
        route_request_id=route_request_id,
        summary=summary,
    )
    return AdvancedTurnResult(
        conversation_id=conversation_id,
        branch_id=branch_id,
        turn_id=turn_id,
        state=state,
        turn_status=_TURN_STATUS_FOR_STATE[state],
        lifecycle_state=lifecycle_state,
        partial_text=relay.partial_text,
        streamed=streamed,
        structured_output_valid=structured_valid,
        usage=settled_usage,
        trace=trace,
    )


def _classify_state(outcome: StreamOutcome, saw_malformed: bool) -> AdvancedState:
    if outcome is StreamOutcome.completed:
        return AdvancedState.complete  # refined to streamed / schema_invalid by the caller
    if outcome is StreamOutcome.cancelled:
        return AdvancedState.cancelled
    if outcome is StreamOutcome.timed_out:
        return AdvancedState.timed_out
    # serving_unavailable: distinguish an un-parseable frame from a transport drop.
    return AdvancedState.malformed_stream if saw_malformed else AdvancedState.serving_unavailable


def build_advanced_trace(
    *,
    selection: AdvancedRouteSelection,
    branch_id: str,
    conversation_id: str,
    turn_id: str,
    state: AdvancedState,
    partial_text: str,
    streamed: bool,
    structured_output_mode: str,
    structured_valid: bool | None,
    usage: SafeUsage,
    instructions: str | None = None,
    route_request_id: str | None = None,
    summary: str | None = None,
) -> dict[str, Any]:
    """Build the redacted, schema-valid advanced-trace.v1 record for one attempt.

    Every free-text field (the terminal ``safe_summary``) is scrubbed through the
    config-text redaction, and the finished record is validated against the
    closed advanced-trace.v1 schema, so the SERVED trace can never carry a
    credential, endpoint, path, header, or hidden-reasoning string -- the schema
    has no field able to hold one and the summary pattern refuses the shapes the
    scrub might miss.
    """
    route = selection.route
    route_decision: dict[str, Any] = {
        "provider": "anvil-serving",
        "route_id": route.route_id,
        "route_digest": route.route_digest,
        "profile_digest": route.profile_digest,
        "model_profile": route.model_profile,
    }
    if route_request_id is not None:
        route_decision["request_id"] = route_request_id

    control_values = [{"name": name, "value": value} for name, value, _ in selection.controls]
    request: dict[str, Any] = {
        "content_trust": "untrusted_task_data",
        "redacted": True,
        "input_chars": 0 if partial_text is None else min(len(partial_text), 100_000),
        "structured_output_mode": structured_output_mode,
        "control_values": control_values,
    }
    if instructions is not None:
        request["instructions_chars"] = min(len(instructions), 100_000)

    usage_dict = _usage_dict(usage)
    events: list[dict[str, Any]] = [{"seq": 0, "kind": "request_start", "at": _ts()}]
    seq = 1
    if streamed:
        events.append({
            "seq": seq, "kind": "response_delta_meta", "at": _ts(),
            "text_chars": min(len(partial_text), 100_000),
        })
        seq += 1
    if structured_output_mode == "json_schema" and structured_valid is not None:
        events.append({"seq": seq, "kind": "schema_validation", "at": _ts(), "schema_valid": structured_valid})
        seq += 1
    events.append(_terminal_event(seq, state, usage_dict, summary))

    trace: dict[str, Any] = {
        "schema_version": "workbench-advanced-trace/v1",
        "trace_id": new_id("advtrace"),
        "branch_ref": {
            "branch_id": branch_id,
            "conversation_id": conversation_id,
            "turn_id": turn_id,
        },
        "route_decision": route_decision,
        "request": request,
        "events": events,
        "usage": usage_dict,
        "status": _TRACE_STATUS_FOR_STATE[state],
        "redaction": {"status": "redacted", "ruleset": "advanced-trace-v1"},
        "created_at": _ts(),
        "completed_at": _ts(),
    }
    validate_advanced_trace(trace)  # SERVED-record gate: fail closed on any leak/shape drift
    return trace


def _usage_dict(usage: SafeUsage) -> dict[str, int]:
    result = {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}
    if usage.duration_ms is not None:
        result["latency_ms"] = usage.duration_ms
    return result


def _terminal_event(
    seq: int, state: AdvancedState, usage_dict: dict[str, int], summary: str | None,
) -> dict[str, Any]:
    event: dict[str, Any] = {"seq": seq, "at": _ts()}
    if state in (AdvancedState.complete, AdvancedState.streamed):
        event["kind"] = "response_complete"
        event["usage"] = usage_dict
        return event
    if state is AdvancedState.cancelled:
        event["kind"] = "cancellation"
        if summary is not None:
            event["safe_summary"] = _safe_summary(summary)
        return event
    event["kind"] = "error"
    event["error"] = {"code": _TERMINAL_ERROR_CODE[state], "retryable": state in _RETRYABLE_STATE}
    if summary is not None:
        event["safe_summary"] = _safe_summary(summary)
    return event


#: Mirror of the advanced-trace.v1 ``safe_summary`` pattern (the schema's own
#: last-line defense).  A scrubbed summary that still trips it is not emitted at
#: all -- it is replaced by the fixed marker -- so the served ``safe_summary`` is
#: always both scrubbed and schema-valid, and a leak can never fail the whole
#: trace nor ride out.
_SAFE_SUMMARY_RE = re.compile(
    r"^(?!.*(?:://|[A-Za-z]:[\\/]|/(?:home|users|etc|var|tmp)/|\\|[Bb]earer\s|\bsk-|"
    r"\bghp_|\bxox|authorization|api[_-]?key|secret|token\s*[:=])).{0,200}$"
)
_SAFE_SUMMARY_FALLBACK = "[redacted summary]"


def _safe_summary(summary: str) -> str:
    """Scrub and bound one untrusted free-text summary for the served trace.

    Runs the config-text redaction (credentials, endpoints, paths), clamps to the
    schema's 200-char ceiling, and then checks the result against the
    advanced-trace.v1 ``safe_summary`` pattern.  A residual leak shape the scrub
    leaves behind (a bare ``authorization``/``Bearer`` word, a ``token=`` residue)
    would make the whole trace fail its SERVED schema validation, so instead of
    emitting it this returns a fixed safe marker -- the summary is always both
    scrubbed and schema-valid, and no leak reaches the browser.
    """
    scrubbed = redact_config_text(summary)[:200]
    return scrubbed if _SAFE_SUMMARY_RE.fullmatch(scrubbed) else _SAFE_SUMMARY_FALLBACK


# --- Forking over the SAME conversation store --------------------------------


def open_advanced_branch(
    store: Any,
    actor: ConversationActor,
    conversation_id: str,
    branch: Mapping[str, Any],
    *,
    redaction: TurnRedaction | None = None,
    validate_branch: Callable[[Mapping[str, Any]], None] | None = None,
) -> Any:
    """Fork an advanced attempt as a streaming sibling on the SAME turn store.

    ``branch`` is an advanced-branch.v1 record (validated first: a control it
    submits must be declared by its pinned route capability).  It must reference
    the SAME ``conversation_id`` and an existing parent turn -- a branch whose
    ``conversation_ref`` names any other conversation is refused, so a fork can
    never mint or cross a conversation identity.  Returns the new streaming
    advanced turn appended under the shared parent via
    ``ConversationStore.branch_turn(mode="advanced")``; prior turns are untouched.
    """
    if validate_branch is None:
        from .contracts import validate_advanced_branch as validate_branch  # local import: avoid a cycle at import time
    try:
        validate_branch(branch)
    except Exception as exc:  # noqa: BLE001 - surface any contract refusal as a runtime refusal
        raise AdvancedRuntimeError(f"advanced branch is not valid: {exc}") from exc

    ref = branch.get("conversation_ref")
    if not isinstance(ref, Mapping):
        raise AdvancedRuntimeError("advanced branch has no conversation reference")
    if ref.get("binding") != "existing_conversation":
        raise AdvancedRuntimeError("advanced branch must bind to an existing conversation")
    branch_conversation = ref.get("conversation_id")
    if branch_conversation != conversation_id:
        raise AdvancedRuntimeError(
            "advanced branch references a different conversation; a fork cannot mint or cross identity"
        )
    fork_point = ref.get("fork_point")
    if not isinstance(fork_point, Mapping):
        raise AdvancedRuntimeError("advanced branch has no fork point")
    parent_turn_id = fork_point.get("parent_turn_id")
    if not isinstance(parent_turn_id, str):
        raise AdvancedRuntimeError("advanced branch fork point has no parent turn id")

    redaction = redaction if redaction is not None else TurnRedaction("redacted", "advanced-trace-v1")
    # branch_turn appends a new sibling under the existing parent in the SAME
    # store, in mode="advanced"; the store's validate_turn_append proves the
    # parent exists in this conversation and prior turns are not mutated.
    return store.branch_turn(
        actor, conversation_id, parent_turn_id,
        role="assistant", status="streaming", redaction=redaction, mode="advanced",
    )


def settle_advanced_branch(
    store: Any, actor: ConversationActor, conversation_id: str, turn_id: str, result: AdvancedTurnResult,
) -> Any:
    """Advance the streaming advanced turn to its single terminal status.

    Reuses ``ConversationStore.advance_turn_status``; the mapping guarantees a
    cancelled/timed-out/malformed/unavailable/schema-invalid attempt advances to
    interrupted/cancelled/failed -- never ``complete`` (criterion: a cancelled or
    partial advanced turn never renders as complete).
    """
    if not isinstance(result, AdvancedTurnResult):
        raise AdvancedRuntimeError("settle requires an AdvancedTurnResult")
    return store.advance_turn_status(actor, conversation_id, turn_id, result.turn_status)


# --- Non-authoritative isolation ---------------------------------------------


def refuse_advanced_evidence(record: Any) -> None:
    """Fail closed if an advanced record is being used as authoritative evidence.

    An advanced turn, trace, or result is preference/experiment evidence only
    (R016/R018): it can never be submitted as State acceptance evidence or
    attached to a delivery run as authoritative proof.  This is the guard the
    evidence boundary calls; it refuses anything advanced-shaped.
    """
    mode: str | None = None
    if isinstance(record, AdvancedTurnResult):
        mode = "advanced"
    elif isinstance(record, Mapping):
        version = str(record.get("schema_version", ""))
        if version.startswith("workbench-advanced-"):
            mode = "advanced"
        elif record.get("mode") == "advanced":
            mode = "advanced"
    elif getattr(record, "mode", None) == "advanced":
        mode = "advanced"
    if mode == "advanced":
        raise AdvancedRuntimeError(
            "advanced-mode traffic is non-authoritative; it cannot be submitted as State "
            "evidence or attached to a delivery run as authoritative proof"
        )
