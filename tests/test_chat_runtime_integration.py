"""End-to-end integration of the streaming Chat runtime (chat-first-voice T003).

This fixture integrates the already-implemented streaming-chat slices
(`workbench.chat_routes`, `chat_stream`, `response_lifecycle_store`,
`stream_sequence`) into ONE runtime path and drives it through every terminal
outcome, plus reconnect and an invalid-route refusal.  It is deliberately
hermetic: the Anvil Serving transport is a scripted in-memory SSE generator, no
HTTP client is imported, and no socket is opened.  The runtime is NOT wired into
`create_app`'s live loop — this qualifies the composed parts, preserving that
boundary (the browser endpoint over this path is a later slice).

The one runtime driver `run_chat_turn` below is the "same runtime path" the
acceptance criteria require: route validation → lifecycle begin → bounded relay
→ per-frame sequence stamping/commit → truthful terminal persistence → a
browser-safe projection.  Every outcome test calls it.

Acceptance-criteria map:

1. Normal / cancellation / timeout / reconnect / invalid-route / Serving-
   unavailable through the same runtime path → the per-outcome tests plus
   `test_invalid_route_is_refused_before_any_serving_request` and
   `test_reconnect_and_snapshot_follow_the_sequence_contract`.
2. Every terminal state persisted truthfully; no partial/interrupted rendered
   as complete → `test_terminal_states_are_persisted_truthfully_never_complete`.
3. Only declared controls and safe route/usage metadata cross the browser
   boundary → `test_browser_projection_carries_only_safe_route_and_usage_metadata`.
4. No failure path reaches a raw provider or exposes server-held auth →
   `test_no_failure_path_reaches_a_raw_provider_or_exposes_auth`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from workbench.chat_routes import (
    ChatRouteError,
    discover_chat_routes,
    validate_chat_route_selection,
)
from workbench.chat_stream import (
    CancellationToken,
    ChatStreamRelay,
    ServingStreamTimeout,
    ServingStreamUnavailable,
    StreamOutcome,
)
from workbench.conversation_models import ConversationActor
from workbench.response_lifecycle_store import (
    IN_PROGRESS_STATE,
    LIFECYCLE_STATE_FOR_OUTCOME,
    MemoryResponseLifecycleStore,
    ResponseLifecycleError,
    ResponseLifecycleRows,
    UnknownResponseError,
)
from workbench.stream_sequence import (
    detect_gap,
    is_stale_frame,
    needs_snapshot_refresh,
    sequence_events,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]

ACTOR = ConversationActor("operator")
CONVERSATION_ID = "conv_runtime_integration"

_CONFIGURED = [
    {
        "route_id": "chat.heavy",
        "display_name": "Heavy chat",
        "serving_contract_version": "1.2.0",
        "route_digest": "sha256:" + "b" * 64,
        "model_profile": "chat-heavy",
        "controls": ["temperature_milli", "max_output_tokens", "reasoning_effort"],
    },
]


def _discovered():
    return discover_chat_routes([dict(_CONFIGURED[0])])


def _delta(text: str) -> dict:
    return {"type": "response.output_text.delta", "delta": text}


_COMPLETED = {"type": "response.completed", "response": {"id": "resp_1"}}


class ScriptedTransport:
    """Injected Serving stream: scripted SSE events or a Serving failure.

    Records whether ``open`` was called so a test can prove an invalid route is
    refused BEFORE any Serving request, and whether the iterator was closed so a
    non-completed exit is proven to tear down the upstream request.  No network.
    """

    def __init__(self, events, *, raise_at=None, error=None, cancel_after=None):
        self._events = list(events)
        self._raise_at = raise_at
        self._error = error
        self._cancel_after = cancel_after
        self.opened = False
        self.closed = False

    def open(self, request, cancel):
        self.opened = True

        def _gen():
            try:
                for index, event in enumerate(self._events):
                    if cancel.cancelled:
                        return
                    if self._raise_at is not None and index == self._raise_at:
                        raise self._error
                    if self._cancel_after is not None and index == self._cancel_after:
                        cancel.cancel()  # a browser cancel mid-stream
                        return
                    yield event
                if self._raise_at is not None and self._raise_at >= len(self._events):
                    raise self._error
            finally:
                self.closed = True

        return _gen()


@dataclass
class RuntimeResult:
    """What one runtime turn produced, for a test to assert against."""

    outcome: StreamOutcome
    turn_status: str
    lifecycle_state: str
    partial_text: str
    seqs: list[int]
    browser_view: dict
    transport: ScriptedTransport


def run_chat_turn(
    store: MemoryResponseLifecycleStore,
    *,
    route_id: str,
    controls: dict,
    prompt: str,
    transport: ScriptedTransport,
    request_id: str,
    cancel: CancellationToken | None = None,
    discovered=None,
) -> RuntimeResult:
    """The single Chat runtime path all outcome tests exercise.

    Route validation happens FIRST and fails closed before the lifecycle begins
    or the transport is ever opened, so an invalid route can never reach Serving.
    The relay's frames are sequence-stamped from the durable per-conversation
    allocator and each committed seq advances the lifecycle; the settled outcome
    is persisted TRUTHFULLY via ``LIFECYCLE_STATE_FOR_OUTCOME`` — a non-completed
    stream is never persisted as ``completed``.
    """
    discovered = discovered if discovered is not None else _discovered()
    # (1) fail-closed route validation, strictly before any Serving request.
    selection = validate_chat_route_selection(route_id, controls, discovered)

    # (2) begin the durable lifecycle in the streaming phase.
    store.begin(ACTOR, CONVERSATION_ID, request_id)

    # (3) drive the bounded relay, stamping and committing each frame's seq.
    relay = ChatStreamRelay(selection, prompt, transport, cancel)
    seqs: list[int] = []
    for frame in sequence_events(relay.stream(), lambda: store.next_seq(ACTOR, request_id)):
        seqs.append(frame.seq)
        if frame.kind == "delta":
            store.advance(ACTOR, request_id, IN_PROGRESS_STATE, seq=frame.seq)
        else:  # the single terminal frame → persist the settled outcome truthfully
            terminal_state = LIFECYCLE_STATE_FOR_OUTCOME[frame.outcome.value]
            store.advance(ACTOR, request_id, terminal_state, seq=frame.seq)

    record = store.reconnect(ACTOR, request_id)
    snapshot = store.snapshot(ACTOR, request_id)
    # (4) the browser-safe projection: declared controls + safe route/usage only.
    browser_view = {
        "route": selection.route.as_dict(),
        "controls": selection.controls_dict(),
        "usage": {
            "input_tokens": record.usage.input_tokens,
            "output_tokens": record.usage.output_tokens,
            "duration_ms": record.usage.duration_ms,
        },
        "lifecycle": {
            "state": snapshot.state,
            "state_version": snapshot.state_version,
            "last_committed_seq": snapshot.last_committed_seq,
            "is_terminal": snapshot.is_terminal,
        },
    }
    return RuntimeResult(
        outcome=relay.outcome,
        turn_status=relay.terminal_turn_status(),
        lifecycle_state=record.state,
        partial_text=relay.partial_text,
        seqs=seqs,
        browser_view=browser_view,
        transport=transport,
    )


def _store() -> MemoryResponseLifecycleStore:
    return MemoryResponseLifecycleStore()


# --- Criterion 1 + 2: every terminal outcome, persisted truthfully -----------


def test_normal_streaming_completes_and_persists_completed():
    store = _store()
    result = run_chat_turn(
        store, route_id="chat.heavy", controls={"max_output_tokens": 256},
        prompt="plan the demo", request_id="req_completed",
        transport=ScriptedTransport([_delta("Hel"), _delta("lo"), _COMPLETED]),
    )
    assert result.outcome is StreamOutcome.completed
    assert result.turn_status == "complete"
    assert result.lifecycle_state == "completed"
    assert result.partial_text == "Hello"
    assert result.transport.closed is True
    # The lifecycle is terminal at the highest committed seq.
    assert result.browser_view["lifecycle"]["is_terminal"] is True
    assert result.browser_view["lifecycle"]["last_committed_seq"] == result.seqs[-1]


def test_terminal_states_are_persisted_truthfully_never_complete():
    # Cancellation, timeout, and Serving-unavailable each settle a DISTINCT
    # terminal that is persisted truthfully and never rendered as complete —
    # even though every one of them relayed partial text first.
    cases = {
        "cancelled": (
            ScriptedTransport([_delta("par"), _delta("tial"), _COMPLETED], cancel_after=2),
            StreamOutcome.cancelled, "cancelled", "cancelled",
        ),
        "timed_out": (
            ScriptedTransport([_delta("half")], raise_at=1, error=ServingStreamTimeout("deadline")),
            StreamOutcome.timed_out, "interrupted", "timed_out",
        ),
        "serving_unavailable": (
            ScriptedTransport([_delta("part")], raise_at=1, error=ServingStreamUnavailable("503")),
            StreamOutcome.serving_unavailable, "failed", "interrupted",
        ),
    }
    for name, (transport, outcome, turn_status, lifecycle_state) in cases.items():
        store = _store()
        result = run_chat_turn(
            store, route_id="chat.heavy", controls={}, prompt="hi",
            request_id=f"req_{name}", transport=transport,
        )
        assert result.outcome is outcome, name
        # Partial text was relayed, but the turn status is NEVER "complete"...
        assert result.partial_text != ""
        assert result.turn_status == turn_status
        assert result.turn_status != "complete"
        # ...and the persisted lifecycle terminal is never "completed".
        assert result.lifecycle_state == lifecycle_state
        assert result.lifecycle_state != "completed"
        assert result.browser_view["lifecycle"]["is_terminal"] is True
        # The upstream request was torn down on the non-completed exit.
        assert result.transport.closed is True


def test_distinct_outcomes_do_not_collapse():
    # The four terminal outcomes are mutually exclusive across the runtime.
    seen = set()
    for request_id, transport in {
        "d_completed": ScriptedTransport([_delta("x"), _COMPLETED]),
        "d_cancelled": ScriptedTransport([_delta("x"), _COMPLETED], cancel_after=1),
        "d_timeout": ScriptedTransport([], raise_at=0, error=ServingStreamTimeout("x")),
        "d_unavailable": ScriptedTransport([], raise_at=0, error=ServingStreamUnavailable("x")),
    }.items():
        result = run_chat_turn(
            _store(), route_id="chat.heavy", controls={}, prompt="hi",
            request_id=request_id, transport=transport,
        )
        seen.add(result.outcome)
    assert seen == set(StreamOutcome)


# --- Criterion 1: invalid route refused before any Serving request -----------


def test_invalid_route_is_refused_before_any_serving_request():
    store = _store()
    transport = ScriptedTransport([_delta("x"), _COMPLETED])
    with pytest.raises(ChatRouteError):
        run_chat_turn(
            store, route_id="chat.unknown", controls={}, prompt="hi",
            request_id="req_invalid", transport=transport,
        )
    # The Serving transport was never opened and no lifecycle record began, so
    # the refusal is strictly upstream of any Serving request.
    assert transport.opened is False
    with pytest.raises(UnknownResponseError):
        store.reconnect(ACTOR, "req_invalid")


def test_undeclared_control_is_refused_before_any_serving_request():
    store = _store()
    transport = ScriptedTransport([_delta("x"), _COMPLETED])
    # A control the route does not declare (or an out-of-range value) fails
    # closed at validation, before the transport is opened.
    with pytest.raises(ChatRouteError):
        run_chat_turn(
            store, route_id="chat.heavy", controls={"temperature_milli": 999_999},
            prompt="hi", request_id="req_badcontrol", transport=transport,
        )
    assert transport.opened is False


# --- Criterion 1: reconnect + snapshot via the sequence contract -------------


def test_reconnect_and_snapshot_follow_the_sequence_contract():
    store = _store()
    result = run_chat_turn(
        store, route_id="chat.heavy", controls={}, prompt="hi",
        request_id="req_reconnect", transport=ScriptedTransport([_delta("a"), _delta("b"), _COMPLETED]),
    )
    # seqs are strictly monotonic across the whole stream (deltas + terminal).
    assert result.seqs == sorted(result.seqs) and len(set(result.seqs)) == len(result.seqs)

    # A reconnect returns the last-committed terminal WITHOUT re-streaming.
    reconnected = store.reconnect(ACTOR, "req_reconnect")
    assert reconnected.state == "completed" and reconnected.is_terminal
    snapshot = store.snapshot(ACTOR, "req_reconnect")
    assert snapshot.last_committed_seq == result.seqs[-1]

    # Gap detection is the client's dropped-frame signal; a stale/duplicate
    # frame is flagged AND refused by the store (it cannot regress a terminal).
    last = result.seqs[-1]
    assert detect_gap(result.seqs[0], result.seqs[0] + 2) is True
    assert needs_snapshot_refresh(result.seqs[0], result.seqs[0] + 2) is True
    assert is_stale_frame(last, last) is True
    with pytest.raises(ResponseLifecycleError):
        store.advance(ACTOR, "req_reconnect", "completed", seq=last)  # terminal is immutable


def test_reconnect_after_restart_recovers_interrupted_never_completed():
    # A response still streaming when the hub stops is a persisted in_progress
    # record; a fresh store over the same rows recovers it as interrupted — a
    # reconnect never fabricates a completion the relay never produced.
    rows = ResponseLifecycleRows()
    live = MemoryResponseLifecycleStore(rows)
    live.begin(ACTOR, CONVERSATION_ID, "req_restart")
    live.advance(ACTOR, "req_restart", IN_PROGRESS_STATE, seq=live.next_seq(ACTOR, "req_restart"))
    assert live.reconnect(ACTOR, "req_restart").state == IN_PROGRESS_STATE

    restarted = MemoryResponseLifecycleStore(rows, recover_on_open=True)
    recovered = restarted.reconnect(ACTOR, "req_restart")
    assert recovered.state == "interrupted"
    assert recovered.is_terminal and recovered.state != "completed"


# --- Criterion 3: only declared controls + safe route/usage cross the boundary


def test_browser_projection_carries_only_safe_route_and_usage_metadata():
    store = _store()
    result = run_chat_turn(
        store, route_id="chat.heavy",
        controls={"temperature_milli": 500, "reasoning_effort": "high"},
        prompt="hi", request_id="req_browser",
        transport=ScriptedTransport([_delta("x"), _COMPLETED]),
    )
    view = result.browser_view
    # Declared controls only (the exact ones the route declares and the caller
    # selected), nothing undeclared.
    assert set(view["controls"]) == {"temperature_milli", "reasoning_effort"}
    # Safe route metadata: provider const, ids, digest, model profile, control
    # NAMES — never an endpoint, URL, token, or credential.
    assert view["route"]["provider"] == "anvil-serving"
    assert set(view["route"]) == {
        "provider", "route_id", "display_name", "serving_contract_version",
        "route_digest", "model_profile", "controls",
    }
    # Usage is bounded integer/None counters only — no free-form string field.
    assert set(view["usage"]) == {"input_tokens", "output_tokens", "duration_ms"}
    # Nothing secret or endpoint-shaped is representable anywhere in the view.
    serialized = repr(view).lower()
    for forbidden in (
        "http", "://", "bearer", "endpoint", "secret", "credential",
        "authorization", "password", "api_key", "apikey", "token=",
    ):
        assert forbidden not in serialized, forbidden


# --- Criterion 4: no raw-provider path, no server-held auth ------------------


def test_no_failure_path_reaches_a_raw_provider_or_exposes_auth():
    # The composed runtime modules import no HTTP client, name no provider host,
    # and embed no URL scheme — a failure settles as a Serving outcome only.
    hosts = (
        "openai.com", "api.openai", "anthropic.com", "api.anthropic",
        "googleapis.com", "bedrock", "mistral.ai", "cohere.com", "openrouter",
        "groq.com", "together.ai", "azure.com", "11434",
    )
    for module in ("chat_routes.py", "chat_stream.py", "response_lifecycle_store.py", "stream_sequence.py"):
        raw = (_REPO_ROOT / "workbench" / module).read_text(encoding="utf-8")
        lowered = raw.lower()
        for host in hosts:
            assert host not in lowered, f"{module} leaked provider host {host!r}"
        # No HTTP-client library is imported (checked on import statements, so an
        # English word like "requests" in prose is not a false positive).
        assert re.search(
            r"(?m)^\s*(?:import|from)\s+(urllib|http\.client|requests|httpx|aiohttp|socket|websockets?)\b",
            raw,
        ) is None, module
        for scheme in ("http://", "https://"):
            assert scheme not in raw, module

    # The persisted lifecycle record structurally cannot carry authentication:
    # its usage shape admits only bounded integer/None counters.
    store = _store()
    result = run_chat_turn(
        store, route_id="chat.heavy", controls={}, prompt="hi",
        request_id="req_noauth", transport=ScriptedTransport([_delta("x"), _COMPLETED]),
    )
    record = store.reconnect(ACTOR, "req_noauth")
    for value in vars(record.usage).values():
        assert value is None or isinstance(value, int)
    # And the audit stream (hub-internal lifecycle metadata) is content/auth-free.
    for event in store.list_audit(limit=100):
        blob = repr(vars(event)).lower()
        for forbidden in ("bearer", "authorization", "secret", "password", "://"):
            assert forbidden not in blob
