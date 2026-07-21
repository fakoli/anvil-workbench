"""Deterministic mock tools + reviewed read tools for the Advanced playground (AMP T004).

Advanced mode lets an operator exercise a reviewed route AND a bounded set of
tools without a live connector: a *mock* tool returns a fixed, fully-recorded
result, and a *read_only* reviewed tool (``tasks.list``, ``issues.read``) is
dispatched through the SAME capability boundary a live tool would be.  Both are
run for one purpose -- to make a route's tool-using behaviour observable and
repeatable in the redacted advanced trace -- and neither may become a new
privilege.

This slice reuses two merged foundations rather than reinventing either:

* **The reviewed-tools-plugins tool-dispatch spine**
  (:class:`~workbench.tool_dispatch.ChatToolDispatchService`).  Every scripted
  tool call is routed through the SAME ``dispatch`` entrypoint the runtime uses,
  so ALL of its reject-before-dispatch gates hold unchanged: an effectful call
  without a one-time hash-bound approval, an unselected/unknown tool, a drifted
  plugin digest, an arbitrary/invalid-schema input, an over-budget call, and an
  unhealthy tool each fail closed on their own typed reason BEFORE the tool
  runner is ever reached.  This module never re-implements a gate and never
  weakens one; a refused step records a ``tool_refusal`` event carrying the
  spine's typed code, and the mock runner is provably never called for it.

* **The advanced-trace.v1 served-record contract**
  (:func:`~workbench.contracts.validate_advanced_trace`).  The schema already
  models ``tool_request`` / ``tool_result`` / ``tool_refusal`` events whose
  ``tool_kind`` is exactly ``mock`` or ``read_only``, and a ``tool_result``
  carries ONLY an ``output_digest`` + ``output_chars`` + a scrubbed
  ``safe_summary`` -- never the raw tool output.  The finished trace is validated
  against that closed, redaction-only schema, so a tool output can never ride out
  as a raw payload, and no event field can express a credential, endpoint, path,
  or hidden-reasoning string.

Three invariants this module is responsible for:

1. **Determinism + full visibility.**  A fixed script over a
   :class:`DeterministicMockTools` registry yields a fixed, ordered, fully
   recorded event sequence: same input -> same ``input_digest`` /
   ``output_digest`` / ``output_chars`` / kind / order.
   :func:`canonical_tool_events` strips only the wall-clock ``at`` stamps, so two
   independent runs of the same script produce byte-identical canonical events.
   No step is hidden: every scripted call appears as exactly one
   ``tool_request`` followed by exactly one ``tool_result`` or ``tool_refusal``.

2. **Reject-before-dispatch is surfaced, never bypassed.**  The sequence is a
   FIXED, pre-declared script; the runtime never reads a tool's output to choose
   the next tool.  Each refused step is recorded from the real
   :class:`~workbench.tool_dispatch.ToolDispatchError` the spine raised, with the
   mock runner unreached (enforced by :data:`ToolStepRecord.runner_reached`).

3. **Tool output is delimited untrusted data and cannot escalate.**  A tool's
   output is captured only as an inert, JSON-stringified
   :func:`delimit_untrusted_output` envelope (``content_trust`` =
   ``untrusted_task_data``) and reduced to a digest in the served trace.  It is
   never parsed to select a tool, mint a privilege, or alter the session's PINNED
   capability profile (which :class:`~workbench.tool_dispatch.ChatToolSession`
   fail-closed validates once and then holds IMMUTABLE).  So a mock output that
   contains a fake capability profile, a "you are now allowed to X" instruction,
   or a nested tool-select changes nothing.

Everything is hermetic: there is no HTTP client, endpoint, or credential here.
Like the spine, the mock/read surface is NOT wired into the live bridge poll
loop; the browser projection is served only through the injected,
otherwise-503 :func:`~workbench.api.build_chat_tools_router` surface.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .advanced_routes import AdvancedRouteCapability
from .contracts import validate_advanced_trace
from .models import new_id, now_utc
from .redaction import redact_config_text
from .tool_dispatch import ChatToolDispatchService, ToolDispatchError
from .advanced_runtime import SAFE_SUMMARY_RE, SAFE_SUMMARY_FALLBACK

#: The two execution kinds the advanced-trace.v1 schema allows for a tool event.
#: ``mock`` is a deterministic fixture; ``read_only`` is a reviewed read tool
#: exercised through the same capability boundary.  There is no effectful kind:
#: an effectful tool is not executed by the playground -- it is refused before
#: dispatch unless it carries a one-time hash-bound approval, and even then the
#: playground never mints that grant.
TOOL_KINDS = frozenset({"mock", "read_only"})

#: The advanced-trace.v1 ``output_chars`` / ``input`` counters are bounded.
_MAX_TRACE_CHARS = 100_000


class AdvancedToolError(RuntimeError):
    """A deterministic mock-tool run violated its own construction invariant.

    This is NOT a dispatch refusal (those are surfaced as ``tool_refusal`` events
    carrying the spine's typed code); it signals a misuse of THIS module -- an
    unknown tool kind, a non-``ChatToolDispatchService`` dispatch, or the
    should-be-impossible case of the runner having been reached for a refused
    step.
    """


def _canonical_json(payload: Any) -> str:
    """A canonical, key-sorted JSON encoding used for digests and char counts."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(domain: str, payload: Any) -> str:
    """A deterministic ``sha256:<hex>`` digest over a canonical payload.

    Domain-separated so a mock input digest can never collide with an output
    digest of the same bytes.  Deterministic across processes, so the same
    scripted input always yields the same digest -- the backbone of the
    determinism guarantee.
    """
    blob = ("anvil-workbench/advanced-tool/" + domain + "\0" + _canonical_json(payload)).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def _safe_summary(text: str) -> str:
    """Scrub and bound one summary for the served trace's ``safe_summary``.

    Runs the shared config-text scrub, clamps to the schema's 200-char ceiling,
    and then enforces the advanced-trace.v1 ``safe_summary`` pattern (the served
    schema's own last-line defence).  A residual leak shape returns the fixed safe
    marker instead of failing the whole trace -- the summary is always both
    scrubbed and schema-valid.
    """
    scrubbed = redact_config_text(text)[:200]
    return scrubbed if SAFE_SUMMARY_RE.fullmatch(scrubbed) else SAFE_SUMMARY_FALLBACK


def _ts() -> str:
    return now_utc().isoformat()


#: The advanced-trace.v1 event ``tool_id`` uses the closed ``token`` grammar
#: (``^[a-z][a-z0-9_]{0,63}$``) -- deliberately dot-free so no event field can
#: express a dotted host-like shape.  A reviewed plugin/read tool id is dotted
#: (``tasks.list``), so the served trace carries a normalized display token
#: (``tasks_list``).  This is a faithful 1:1 transform: the EXACT dotted id is
#: kept on :class:`ToolStepRecord` and, more importantly, is bound into the
#: content-sensitive ``input_digest`` / ``output_digest``, so fidelity is never
#: lost -- only the display token is normalized for the closed schema.
_NON_TOKEN = re.compile(r"[^a-z0-9_]+")


def _trace_tool_token(tool_id: str) -> str:
    """Normalize a (possibly dotted) tool id to the trace's closed token grammar."""
    token = _NON_TOKEN.sub("_", str(tool_id).lower()).strip("_")[:64]
    if not token or not token[0].isalpha():
        token = f"tool_{token}" if token else "tool"
    return token[:64]


# --------------------------------------------------------------------------- #
# Delimited untrusted tool output.
# --------------------------------------------------------------------------- #


def delimit_untrusted_output(payload: Any) -> dict[str, Any]:
    """Wrap a tool's output as INERT, clearly-delimited untrusted task data.

    The payload is JSON-STRINGIFIED, not carried as a live mapping: the envelope
    is a string field, so nothing downstream can spread it into a capability
    profile, a tool selection, or a privilege grant.  ``content_trust`` marks it
    ``untrusted_task_data`` -- the same class the advanced-trace request card
    uses -- so a UI renders it as data, never as control instructions.  This
    envelope is deliberately NOT part of the served trace (the trace keeps only
    the output digest + char count); it is the playground-side record of exactly
    what the mock/read tool returned, held inert.
    """
    return {
        "content_trust": "untrusted_task_data",
        "delimited": True,
        # A STRING, never a live structure: an injected {"capability_profile": ...}
        # or {"select_tool": ...} is inert text here, incapable of escalation.
        "payload_json": _canonical_json(payload),
    }


# --------------------------------------------------------------------------- #
# The deterministic mock-tool registry.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MockToolFixture:
    """One deterministic tool fixture: a tool id, its trace kind, and its output.

    ``output`` is either a fixed value or a PURE callable of the canonical inputs;
    either way the same input yields the same output, so a run is repeatable.  The
    ``tool_kind`` must be ``mock`` or ``read_only`` -- there is no effectful
    fixture, because the playground never executes an effect.
    """

    tool_id: str
    tool_kind: str
    output: Any

    def __post_init__(self) -> None:
        if self.tool_kind not in TOOL_KINDS:
            raise AdvancedToolError(
                f"mock tool {self.tool_id!r} declares an unsupported tool_kind: {self.tool_kind!r}"
            )

    def produce(self, inputs: Mapping[str, Any]) -> Any:
        """The deterministic output for these canonical inputs."""
        if callable(self.output):
            return self.output(dict(inputs))
        return self.output


class DeterministicMockTools:
    """A fixed registry of deterministic mock/read tool fixtures, keyed by tool id.

    Holds no mutable state between calls, so a run is a pure function of the
    script and the registry -- the determinism guarantee's other half.
    """

    def __init__(self, fixtures: Sequence[MockToolFixture]) -> None:
        by_id: dict[str, MockToolFixture] = {}
        for fixture in fixtures:
            if not isinstance(fixture, MockToolFixture):
                raise AdvancedToolError("every mock fixture must be a MockToolFixture")
            if fixture.tool_id in by_id:
                raise AdvancedToolError(f"duplicate mock fixture for tool {fixture.tool_id!r}")
            by_id[fixture.tool_id] = fixture
        self._by_id = by_id

    def fixture(self, tool_id: str) -> MockToolFixture:
        fixture = self._by_id.get(tool_id)
        if fixture is None:
            raise AdvancedToolError(f"no mock fixture is registered for tool {tool_id!r}")
        return fixture

    def kind_for(self, tool_id: str) -> str:
        return self.fixture(tool_id).tool_kind


# --------------------------------------------------------------------------- #
# One recorded step + the whole run.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ToolStepRecord:
    """The recorded outcome of one scripted tool call.

    ``outcome`` is ``result`` (the tool ran and returned a delimited untrusted
    output) or ``refusal`` (the spine rejected the call before dispatch).
    ``runner_reached`` is ``False`` for EVERY refusal -- a proof the mock runner
    was never invoked for a rejected request.  ``delimited_output`` is present
    only for a result and is the inert untrusted-data envelope.
    """

    index: int
    tool_id: str
    tool_kind: str
    outcome: str
    input_digest: str
    output_digest: str | None
    output_chars: int | None
    refusal_code: str | None
    receipt_status: str | None
    runner_reached: bool
    delimited_output: dict[str, Any] | None

    @property
    def refused(self) -> bool:
        return self.outcome == "refusal"


@dataclass(frozen=True)
class AdvancedToolRun:
    """The full result of running one deterministic tool script.

    ``trace`` is a validated advanced-trace.v1 record; ``steps`` is the per-call
    record; ``tools_pin_before`` / ``tools_pin_after`` are the session's pinned,
    capability-enabled tool projection captured around the run -- their equality
    is the profile-immutability proof.
    """

    steps: tuple[ToolStepRecord, ...]
    trace: dict[str, Any]
    tools_pin_before: list[dict[str, Any]]
    tools_pin_after: list[dict[str, Any]]

    @property
    def capability_pin_immutable(self) -> bool:
        """True iff the pinned capability profile did not change across the run."""
        return self.tools_pin_before == self.tools_pin_after

    def canonical_events(self) -> list[dict[str, Any]]:
        """The trace events with wall-clock ``at`` stamps stripped (deterministic)."""
        return canonical_tool_events(self.trace["events"])

    def step_signature(self) -> list[tuple[Any, ...]]:
        """A deterministic per-step signature for byte-stable run comparison."""
        return [
            (s.index, s.tool_id, s.tool_kind, s.outcome, s.input_digest, s.output_digest,
             s.output_chars, s.refusal_code)
            for s in self.steps
        ]


def canonical_tool_events(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return the events with only the non-deterministic ``at`` field removed.

    Everything that determinism must pin -- ``seq``, ``kind``, ``tool_id``,
    ``tool_kind``, ``input_digest``, ``output_digest``, ``output_chars``,
    ``error.code`` -- survives, so two runs of the same script over the same
    registry produce byte-identical canonical events.
    """
    return [{key: value for key, value in event.items() if key != "at"} for event in events]


# --------------------------------------------------------------------------- #
# The wired runtime: drive the fixed script through the real dispatch spine.
# --------------------------------------------------------------------------- #


_UNSET = object()


def _make_recording_runner(
    fixture: MockToolFixture, holder: dict[str, Any]
) -> Callable[[Any, Mapping[str, Any]], Any]:
    """A tool runner that produces the fixture's deterministic output and records it.

    Reached ONLY when the spine admits a call past every reject-before-dispatch
    gate.  It marks ``holder['reached']`` so a refused step can prove the runner
    was never invoked, stashes the deterministic payload, and returns a
    ``succeeded`` outcome whose external ref carries only the output digest -- a
    safe token, never a raw payload.
    """
    from .store import OperationOutcome

    def runner(_discovered: Any, inputs: Mapping[str, Any]) -> Any:
        holder["reached"] = True
        payload = fixture.produce(inputs)
        holder["payload"] = payload
        holder["output_digest"] = _digest("output", payload)
        return OperationOutcome("succeeded", external_ref={"output_digest": holder["output_digest"]})

    return runner


def run_mock_tool_sequence(
    *,
    dispatch: ChatToolDispatchService,
    tools: DeterministicMockTools,
    script: Sequence[Mapping[str, Any]],
    route: AdvancedRouteCapability,
    branch_id: str,
    conversation_id: str,
    turn_id: str,
    control_values: Sequence[tuple[str, Any]] = (),
    input_chars: int = 0,
) -> AdvancedToolRun:
    """Run one FIXED tool script through the real dispatch spine and record it.

    ``script`` is an ordered list of ``plugin-request`` ``tool_call`` dicts.  Each
    is routed through :meth:`ChatToolDispatchService.dispatch` -- the same wired
    entrypoint the runtime uses -- so every reject-before-dispatch gate holds.  A
    dispatched call records a ``tool_result`` event (digest + char count only) and
    an inert delimited untrusted-output envelope; a refused call records a
    ``tool_refusal`` event carrying the spine's typed refusal code, with the mock
    runner provably unreached.  The sequence never consults a tool's output to
    choose the next call, and the session's pinned capability profile is immutable
    -- so a tool output can never escalate.  The assembled advanced-trace.v1 record
    is validated against its closed, redaction-only schema before returning.
    """
    if not isinstance(dispatch, ChatToolDispatchService):
        raise AdvancedToolError("a mock tool run requires the wired ChatToolDispatchService")
    if not isinstance(tools, DeterministicMockTools):
        raise AdvancedToolError("a mock tool run requires a DeterministicMockTools registry")
    if not isinstance(route, AdvancedRouteCapability):
        raise AdvancedToolError("a mock tool run requires a discovered AdvancedRouteCapability")

    # The pinned, capability-enabled tool projection BEFORE the run.  Compared to
    # the post-run projection below, its equality proves the profile is immutable.
    pin_before = dispatch.list_tools()

    events: list[dict[str, Any]] = [{"seq": 0, "kind": "request_start", "at": _ts()}]
    seq = 1
    steps: list[ToolStepRecord] = []

    for index, request in enumerate(script):
        tool_call = request.get("tool_call") if isinstance(request.get("tool_call"), Mapping) else {}
        tool_id = str(tool_call.get("tool_id"))
        inputs = dict(tool_call.get("inputs") or {})
        fixture = tools.fixture(tool_id)
        tool_kind = fixture.tool_kind
        input_digest = _digest("input", {"tool_id": tool_id, "inputs": inputs})
        tool_token = _trace_tool_token(tool_id)

        events.append({
            "seq": seq, "kind": "tool_request", "at": _ts(),
            "tool_id": tool_token, "tool_kind": tool_kind, "input_digest": input_digest,
        })
        seq += 1

        holder: dict[str, Any] = {"reached": False, "payload": _UNSET, "output_digest": None}
        runner = _make_recording_runner(fixture, holder)
        try:
            result = dispatch.dispatch(request, runner)
        except ToolDispatchError as exc:
            # Reject-before-dispatch: the spine refused the call on its own typed
            # reason and the runner was never reached.  We surface -- never
            # swallow -- that refusal as a typed tool_refusal event.
            if holder["reached"]:  # pragma: no cover - the spine guarantees this
                raise AdvancedToolError(
                    "the mock runner was reached for a refused request; the reject-before-dispatch "
                    "guarantee was violated"
                ) from exc
            events.append({
                "seq": seq, "kind": "tool_refusal", "at": _ts(),
                "tool_id": tool_token, "tool_kind": tool_kind,
                "error": {"code": exc.code, "retryable": False},
            })
            seq += 1
            steps.append(ToolStepRecord(
                index=index, tool_id=tool_id, tool_kind=tool_kind, outcome="refusal",
                input_digest=input_digest, output_digest=None, output_chars=None,
                refusal_code=exc.code, receipt_status=None, runner_reached=False,
                delimited_output=None,
            ))
            continue

        # A dispatched call: reduce the output to a digest + char count for the
        # served trace, and keep the raw output only as an inert delimited envelope.
        #
        # A byte-identical repeat of an earlier call (same tool + same inputs ->
        # same request_digest) hits the spine's idempotent-replay path: it returns
        # the stored receipt WITHOUT invoking the runner (``result.replayed`` is
        # True), so the holder stays unset.  A "fixed sequence" legitimately
        # includes such a repeat, so it must NOT crash on ``_UNSET`` reaching
        # ``_canonical_json``.  The mock fixture is a pure function of tool+inputs,
        # so recomputing it here reproduces the SAME output the first identical
        # call produced -- the replay's output_digest / output_chars / safe_summary
        # are byte-identical, keeping the trace deterministic.  ``holder['reached']``
        # (never ``_UNSET``) is the source of truth for whether the runner ran.
        runner_reached = holder["reached"]
        if runner_reached:
            payload = holder["payload"]
            output_digest = str(holder["output_digest"])
        else:  # idempotent replay: resolve the deterministic fixture output afresh
            payload = fixture.produce(inputs)
            output_digest = _digest("output", payload)
        output_chars = min(len(_canonical_json(payload)), _MAX_TRACE_CHARS)
        delimited = delimit_untrusted_output(payload)
        events.append({
            "seq": seq, "kind": "tool_result", "at": _ts(),
            "tool_id": tool_token, "tool_kind": tool_kind,
            "output_digest": output_digest, "output_chars": output_chars,
            "safe_summary": _safe_summary(f"{tool_kind} tool {tool_id} returned a bounded result"),
        })
        seq += 1
        steps.append(ToolStepRecord(
            index=index, tool_id=tool_id, tool_kind=tool_kind, outcome="result",
            input_digest=input_digest, output_digest=output_digest, output_chars=output_chars,
            refusal_code=None, receipt_status=str(result.receipt.get("status")),
            runner_reached=runner_reached, delimited_output=delimited,
        ))

    events.append({
        "seq": seq, "kind": "response_complete", "at": _ts(),
        "usage": {"input_tokens": 0, "output_tokens": 0},
    })

    # The pinned projection AFTER the run.  A tool output that tried to inject a
    # fake capability profile cannot have changed this: the session validated and
    # froze its profile at construction and never re-reads a tool's output.
    pin_after = dispatch.list_tools()

    trace = _build_tool_trace(
        route=route, branch_id=branch_id, conversation_id=conversation_id,
        turn_id=turn_id, control_values=control_values, input_chars=input_chars, events=events,
    )
    return AdvancedToolRun(
        steps=tuple(steps), trace=trace, tools_pin_before=pin_before, tools_pin_after=pin_after,
    )


def _build_tool_trace(
    *,
    route: AdvancedRouteCapability,
    branch_id: str,
    conversation_id: str,
    turn_id: str,
    control_values: Sequence[tuple[str, Any]],
    input_chars: int,
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Assemble and fail-closed validate the advanced-trace.v1 record for a run.

    The route decision carries Anvil Serving ids/digests only; the request card is
    reduced to bounded counters and declared control values; every event is a
    closed card whose tool result holds only a digest + char count.  The finished
    record is validated against the closed, redaction-only advanced-trace.v1
    schema, so no field can carry a raw output, credential, endpoint, or path.
    """
    route_decision: dict[str, Any] = {
        "provider": "anvil-serving",
        "route_id": route.route_id,
        "route_digest": route.route_digest,
        "profile_digest": route.profile_digest,
        "model_profile": route.model_profile,
    }
    request: dict[str, Any] = {
        "content_trust": "untrusted_task_data",
        "redacted": True,
        "input_chars": min(max(int(input_chars), 0), _MAX_TRACE_CHARS),
        "structured_output_mode": "text",
        "control_values": [{"name": name, "value": value} for name, value in control_values],
    }
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
        "events": [dict(event) for event in events],
        "status": "complete",
        "redaction": {"status": "redacted", "ruleset": "advanced-trace-v1"},
        "created_at": _ts(),
        "completed_at": _ts(),
    }
    validate_advanced_trace(trace)  # SERVED-record gate: fail closed on any leak/shape drift
    return trace
