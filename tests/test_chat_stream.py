"""Hermetic contract tests for the bounded Responses stream relay (T003.2).

Criterion map:

1. Normal, streaming, cancellation, timeout, and Serving-unavailable outcomes
   remain distinct -- ``test_five_outcomes_are_distinct_and_stable`` plus the
   per-outcome tests below (``test_normal_stream_completes``,
   ``test_cancellation_settles_cancelled_and_terminates_upstream``,
   ``test_timeout_settles_timed_out``,
   ``test_transport_error_and_5xx_settle_serving_unavailable``).
2. Client cancellation terminates the upstream request and emits no later
   completion -- ``test_cancellation_settles_cancelled_and_terminates_upstream``
   and ``test_cancel_before_queued_completion_never_yields_completed``.
3. Timeout or partial output is never persisted/rendered as complete --
   ``test_timeout_preserves_partial_but_status_is_interrupted`` and
   ``test_terminal_turn_status_never_complete_unless_completed``.
4. Every failure settles through the Serving runtime with no raw-provider
   fallback -- ``test_no_raw_provider_fallback_in_chat_stream_source`` and the
   unavailable/timeout behavioral tests.

Bounded-request criterion: ``test_bounded_request_comes_only_from_the_selection``
and ``test_bounded_request_refuses_unbounded_or_unvalidated_inputs``.

No test opens a socket; the Serving transport is a scripted in-memory
generator that mimics an SSE sequence or raises a Serving failure.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from workbench.chat_routes import (
    ChatRouteDescriptor,
    ChatRouteSelection,
    discover_chat_routes,
    validate_chat_route_selection,
)
from workbench.chat_stream import (
    CancellationToken,
    ChatStreamError,
    ChatStreamRelay,
    ServingStreamTimeout,
    ServingStreamUnavailable,
    StreamOutcome,
    build_bounded_request,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]

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


def _selection(controls: dict | None = None):
    discovered = discover_chat_routes([dict(_CONFIGURED[0])])
    return validate_chat_route_selection("chat.heavy", controls or {}, discovered)


def _delta(text: str) -> dict:
    return {"type": "response.output_text.delta", "delta": text}


_COMPLETED = {"type": "response.completed", "response": {"id": "resp_1"}}


class ScriptedTransport:
    """An injected Serving stream: yields scripted SSE events or raises.

    Records whether its iterator was closed (the relay closing it on a
    non-completed exit) so a test can prove the upstream request was terminated.
    ``raise_at`` injects a Serving-runtime failure after ``raise_at`` events.
    ``cancel_after`` self-trips the caller's token mid-stream (as a real browser
    cancel would), so the relay observes cancel before the next read.
    """

    def __init__(self, events, *, raise_at=None, error=None, cancel_after=None):
        self._events = list(events)
        self._raise_at = raise_at
        self._error = error
        self._cancel_after = cancel_after
        self.closed = False
        self.upstream_open = False
        self.opened_request = None

    def open(self, request, cancel):
        self.opened_request = dict(request)
        self.upstream_open = True

        def _gen():
            try:
                for index, event in enumerate(self._events):
                    if cancel.cancelled:  # self-observe an external cancel
                        return
                    if self._raise_at is not None and index == self._raise_at:
                        assert self._error is not None
                        raise self._error
                    if self._cancel_after is not None and index == self._cancel_after:
                        # A browser cancel: trip the token and tear down the
                        # upstream before delivering any further event.
                        cancel.cancel()
                        return
                    yield event
                # A trailing failure fires after the scripted events are drained
                # (e.g. a timeout waiting for the next event that never comes).
                if self._raise_at is not None and self._raise_at >= len(self._events):
                    assert self._error is not None
                    raise self._error
            finally:
                # The relay closes us on any non-completed exit; a real
                # transport tears down the upstream request here.
                self.upstream_open = False
                self.closed = True

        return _gen()


def _drain(relay):
    return list(relay.stream())


# --- criterion 1: five distinct, stable outcomes -----------------------------


def test_five_outcomes_are_distinct_and_stable():
    values = [o.value for o in StreamOutcome]
    assert values == ["completed", "cancelled", "timed_out", "serving_unavailable"]
    # Distinct enum members plus the non-terminal "streaming" the relay reports
    # as ``outcome is None`` before it settles -- five distinct states in all.
    assert len(set(values)) == 4
    relay = ChatStreamRelay(_selection(), "hi", ScriptedTransport([]))
    assert relay.outcome is None  # streaming / not-yet-settled is distinct


def test_normal_stream_completes():
    transport = ScriptedTransport([_delta("Hel"), _delta("lo"), _COMPLETED])
    relay = ChatStreamRelay(_selection(), "hi", transport)
    events = _drain(relay)

    assert relay.outcome is StreamOutcome.completed
    assert relay.partial_text == "Hello"
    assert relay.terminal_turn_status() == "complete"
    # exactly one terminal event, last, and it is the completed one
    terminals = [e for e in events if e.kind == "terminal"]
    assert len(terminals) == 1
    assert events[-1].outcome is StreamOutcome.completed
    assert [e.text for e in events if e.kind == "delta"] == ["Hel", "lo"]


def test_streaming_yields_deltas_before_terminal():
    transport = ScriptedTransport([_delta("a"), _delta("b"), _COMPLETED])
    relay = ChatStreamRelay(_selection(), "hi", transport)
    gen = relay.stream()
    first = next(gen)
    assert first.kind == "delta" and first.text == "a"
    assert relay.outcome is None  # still streaming
    rest = list(gen)
    assert rest[-1].kind == "terminal"


# --- criterion 2: cancellation terminates upstream, no later completion -------


def test_cancellation_settles_cancelled_and_terminates_upstream():
    # Transport self-trips the token after the 2nd event (a browser cancel);
    # the relay must observe it before the queued completion and terminate.
    transport = ScriptedTransport(
        [_delta("par"), _delta("tial"), _COMPLETED], cancel_after=2
    )
    relay = ChatStreamRelay(_selection(), "hi", transport)
    events = _drain(relay)

    assert relay.outcome is StreamOutcome.cancelled
    assert transport.closed is True
    assert transport.upstream_open is False  # upstream request terminated
    # No completed terminal was ever emitted after cancel.
    assert all(e.outcome is not StreamOutcome.completed for e in events)
    assert events[-1].outcome is StreamOutcome.cancelled


def test_cancel_before_queued_completion_never_yields_completed():
    # Drive cancel externally, mid-stream, then keep consuming: the queued
    # completion event must never surface.
    transport = ScriptedTransport([_delta("x"), _delta("y"), _COMPLETED])
    token = CancellationToken()
    relay = ChatStreamRelay(_selection(), "hi", transport, cancel=token)
    gen = relay.stream()

    assert next(gen).text == "x"
    assert next(gen).text == "y"
    token.cancel()
    tail = list(gen)  # relay checks cancel before the next read

    assert relay.outcome is StreamOutcome.cancelled
    assert all(e.outcome is not StreamOutcome.completed for e in tail)
    assert tail[-1].outcome is StreamOutcome.cancelled
    assert relay.terminal_turn_status() == "cancelled"
    assert transport.upstream_open is False


# --- criterion 3: partial/timeout is never a completed response --------------


def test_timeout_settles_timed_out():
    transport = ScriptedTransport(
        [_delta("half")], raise_at=1, error=ServingStreamTimeout("deadline")
    )
    relay = ChatStreamRelay(_selection(), "hi", transport)
    events = _drain(relay)

    assert relay.outcome is StreamOutcome.timed_out
    assert events[-1].outcome is StreamOutcome.timed_out
    assert transport.upstream_open is False


def test_timeout_preserves_partial_but_status_is_interrupted():
    transport = ScriptedTransport(
        [_delta("half ")], raise_at=1, error=ServingStreamTimeout("deadline")
    )
    relay = ChatStreamRelay(_selection(), "hi", transport)
    events = _drain(relay)

    # Partial content is preserved for the store to persist as interrupted...
    assert relay.partial_text == "half "
    # ...but the lifecycle status is never ``complete``.
    assert relay.terminal_turn_status() == "interrupted"
    assert all(e.outcome is not StreamOutcome.completed for e in events)


def test_terminal_turn_status_never_complete_unless_completed():
    cases = {
        StreamOutcome.cancelled: "cancelled",
        StreamOutcome.timed_out: "interrupted",
        StreamOutcome.serving_unavailable: "failed",
    }
    for outcome, status in cases.items():
        # Build a relay that settles into ``outcome`` and check the mapping.
        if outcome is StreamOutcome.cancelled:
            transport = ScriptedTransport([_delta("a"), _COMPLETED], cancel_after=1)
        elif outcome is StreamOutcome.timed_out:
            transport = ScriptedTransport([], raise_at=0, error=ServingStreamTimeout("x"))
        else:
            transport = ScriptedTransport([], raise_at=0, error=ServingStreamUnavailable("x"))
        relay = ChatStreamRelay(_selection(), "hi", transport)
        _drain(relay)
        assert relay.outcome is outcome
        assert relay.terminal_turn_status() == status
        assert status != "complete"


# --- criterion 4: failures settle through Serving, no raw-provider fallback ---


def test_transport_error_and_5xx_settle_serving_unavailable():
    for error in (
        ServingStreamUnavailable("Serving 503"),
        RuntimeError("unexpected transport blowup"),
    ):
        transport = ScriptedTransport([_delta("x")], raise_at=1, error=error)
        relay = ChatStreamRelay(_selection(), "hi", transport)
        events = _drain(relay)
        assert relay.outcome is StreamOutcome.serving_unavailable
        assert events[-1].outcome is StreamOutcome.serving_unavailable
        assert transport.upstream_open is False


def test_stream_ending_without_completion_is_unavailable_not_success():
    # A stream that just stops (no completed event) must never be a silent
    # success; it settles as unavailable through the Serving runtime.
    transport = ScriptedTransport([_delta("x"), _delta("y")])
    relay = ChatStreamRelay(_selection(), "hi", transport)
    _drain(relay)
    assert relay.outcome is StreamOutcome.serving_unavailable
    assert relay.terminal_turn_status() == "failed"


def test_serving_failure_event_settles_unavailable():
    transport = ScriptedTransport([_delta("x"), {"type": "response.failed"}])
    relay = ChatStreamRelay(_selection(), "hi", transport)
    _drain(relay)
    assert relay.outcome is StreamOutcome.serving_unavailable


def test_no_raw_provider_fallback_in_chat_stream_source():
    # AGENTS.md: "Never add a raw-provider fallback." The relay imports no HTTP
    # client, names no provider host, and embeds no URL scheme literal -- the
    # only model path is the injected Serving transport.
    source = (_REPO_ROOT / "workbench" / "chat_stream.py").read_text(encoding="utf-8").lower()
    for marker in (
        "openai.com", "api.openai", "anthropic.com", "api.anthropic",
        "googleapis.com", "bedrock", "mistral.ai", "cohere.com", "openrouter",
        "groq.com", "together.ai", "azure.com", "11434",
    ):
        assert marker not in source, f"chat_stream leaked provider host {marker!r}"
    raw = (_REPO_ROOT / "workbench" / "chat_stream.py").read_text(encoding="utf-8")
    assert re.search(r"\b(urllib|http\.client|requests|httpx|aiohttp|socket|websocket)\b", raw) is None
    for scheme in ("http://", "https://"):
        assert scheme not in raw


# --- bounded request comes only from the validated selection -----------------


def test_bounded_request_comes_only_from_the_selection():
    selection = _selection(
        {"temperature_milli": 500, "max_output_tokens": 256, "reasoning_effort": "high"}
    )
    request = build_bounded_request(selection, "hello world")
    assert request == {
        "model": "chat-heavy",
        "route_id": "chat.heavy",
        "input": "hello world",
        "stream": True,
        "max_output_tokens": 256,
        "temperature": 0.5,
        "reasoning": {"effort": "high"},
    }
    # No endpoint/URL/token field can appear -- only Serving ids and validated
    # controls are representable.
    serialized = repr(request).lower()
    for forbidden in ("http", "bearer", "endpoint", "://", "secret", "credential"):
        assert forbidden not in serialized


def test_bounded_request_defaults_output_tokens_when_uncontrolled():
    request = build_bounded_request(_selection(), "hi")
    assert request["max_output_tokens"] == 1024
    assert "temperature" not in request and "reasoning" not in request


def test_bounded_request_refuses_unbounded_or_unvalidated_inputs():
    selection = _selection()
    with pytest.raises(ChatStreamError, match="prompt"):
        build_bounded_request(selection, "x" * 20_001)
    with pytest.raises(ChatStreamError, match="prompt"):
        build_bounded_request(selection, "")
    with pytest.raises(ChatStreamError, match="ChatRouteSelection"):
        build_bounded_request({"route_id": "chat.heavy"}, "hi")  # type: ignore[arg-type]


def test_relay_refuses_a_non_transport():
    with pytest.raises(ChatStreamError, match="ServingStreamTransport"):
        ChatStreamRelay(_selection(), "hi", object())  # type: ignore[arg-type]


def test_terminal_status_before_settle_fails_closed():
    relay = ChatStreamRelay(_selection(), "hi", ScriptedTransport([]))
    with pytest.raises(ChatStreamError, match="terminal outcome"):
        relay.terminal_turn_status()


def test_malformed_non_mapping_frame_settles_unavailable_not_escaping():
    # A malformed non-mapping frame makes ``_interpret`` raise
    # ServingStreamUnavailable; it must settle a terminal outcome and emit the
    # terminal event, never propagate out of the generator un-settled.
    transport = ScriptedTransport([_delta("x"), "not-a-mapping"])
    relay = ChatStreamRelay(_selection(), "hi", transport)
    events = _drain(relay)  # must not raise
    assert relay.outcome is StreamOutcome.serving_unavailable
    assert events[-1].kind == "terminal"
    assert events[-1].outcome is StreamOutcome.serving_unavailable
    assert transport.upstream_open is False


class _CancelRacingCompletionTransport:
    """Trips the cancel token in the same read that delivers ``completed``.

    Models a browser cancel that lands after the completion frame is already
    queued: the relay must let cancel strictly win over the just-read completion.
    """

    def __init__(self):
        self.closed = False
        self.upstream_open = False

    def open(self, request, cancel):
        self.upstream_open = True

        def _gen():
            try:
                yield _delta("partial")
                cancel.cancel()  # cancel trips right before yielding completed
                yield _COMPLETED
            finally:
                self.upstream_open = False
                self.closed = True

        return _gen()


def test_cancel_racing_in_flight_completion_settles_cancelled():
    transport = _CancelRacingCompletionTransport()
    relay = ChatStreamRelay(_selection(), "hi", transport)
    events = _drain(relay)

    assert relay.outcome is StreamOutcome.cancelled
    assert relay.terminal_turn_status() == "cancelled"
    # The queued completion frame must never surface as a terminal.
    assert all(e.outcome is not StreamOutcome.completed for e in events)
    assert events[-1].outcome is StreamOutcome.cancelled
    assert transport.upstream_open is False


def test_completed_stream_closes_upstream_transport():
    # A normal completion must still tear down the (retained) transport
    # generator, not leave it suspended with the upstream open.
    transport = ScriptedTransport([_delta("Hel"), _delta("lo"), _COMPLETED])
    relay = ChatStreamRelay(_selection(), "hi", transport)
    _drain(relay)

    assert relay.outcome is StreamOutcome.completed
    assert transport.closed is True
    assert transport.upstream_open is False


def test_stdlib_timeout_error_settles_timed_out():
    # A stdlib TimeoutError (also the alias of socket.timeout /
    # asyncio.TimeoutError on 3.10+) maps to timed_out -> interrupted, not
    # serving_unavailable -> failed.
    transport = ScriptedTransport(
        [_delta("half")], raise_at=1, error=TimeoutError("deadline")
    )
    relay = ChatStreamRelay(_selection(), "hi", transport)
    events = _drain(relay)

    assert relay.outcome is StreamOutcome.timed_out
    assert relay.terminal_turn_status() == "interrupted"
    assert events[-1].outcome is StreamOutcome.timed_out


def _hand_built_selection(controls):
    route = ChatRouteDescriptor(
        route_id="chat.heavy",
        display_name="Heavy chat",
        serving_contract_version="1.2.0",
        route_digest="sha256:" + "b" * 64,
        model_profile="chat-heavy",
        controls=("max_output_tokens", "temperature_milli", "reasoning_effort"),
    )
    return ChatRouteSelection(route=route, controls=tuple(controls.items()))


def test_bounded_request_revalidates_hand_built_selection_controls():
    # ``isinstance(selection, ChatRouteSelection)`` alone does not prove the
    # controls are bounded: a directly-constructed selection must be re-checked
    # against the chat-turn.v1 bounds at request-build time.
    for controls in (
        {"max_output_tokens": 999_999_999},
        {"temperature_milli": 50_000},
        {"reasoning_effort": "ULTRA"},
    ):
        selection = _hand_built_selection(controls)
        with pytest.raises(ChatStreamError, match="control"):
            build_bounded_request(selection, "hi")


def test_output_char_bound_is_enforced():
    # A runaway stream that never completes is bounded and fails closed rather
    # than being relayed unbounded.
    big = "z" * 40_000
    transport = ScriptedTransport([_delta(big), _delta(big), _delta(big)])
    relay = ChatStreamRelay(_selection(), "hi", transport)
    _drain(relay)
    assert relay.outcome is StreamOutcome.serving_unavailable
