"""Relay bounded Responses streams from Anvil Serving (chat-first-voice T003.2).

Anvil Serving is the only managed model path and it owns model policy; the
AGENTS.md boundary is explicit: never add a raw-provider fallback.  This module
assembles a *bounded* Responses request from an already-validated
:class:`~workbench.chat_routes.ChatRouteSelection` (T003.1) and relays the
Serving SSE event sequence to the caller as typed relay events, settling into
exactly ONE distinct terminal outcome.

Design contract (the four acceptance criteria this slice binds):

* **Distinct stable outcomes.** Every stream settles into exactly one member of
  :class:`StreamOutcome` — ``completed``, ``cancelled``, ``timed_out``, or
  ``serving_unavailable``.  These are mutually exclusive and never collapse:
  a normal finish is ``completed``; a caller cancel is ``cancelled``; a Serving
  timeout is ``timed_out``; any transport error, Serving 5xx, or a stream that
  ends without a terminal event is ``serving_unavailable``.
* **Cancellation terminates the upstream request and emits no later
  completion.**  A :class:`CancellationToken` is checked before every upstream
  read *and* again immediately after each read, and the relay ``close()``s the
  transport stream on every exit, so the injected transport observes the cancel
  and stops.  Once cancel is seen the relay settles ``cancelled`` *before*
  honoring the frame it just read, so a ``completed`` event the transport had
  queued -- even one delivered in the same read that tripped the cancel -- can
  never be yielded.
* **Timeout or partial output is never rendered as completed.**  The partial
  text delivered so far is preserved on :attr:`ChatStreamRelay.partial_text`,
  but :meth:`ChatStreamRelay.terminal_turn_status` maps a non-``completed``
  outcome to ``interrupted``/``cancelled``/``failed`` — never the ``complete``
  turn status.  The relay is deliberately stateless: it yields typed events and
  returns a lifecycle status; persistence stays in
  :mod:`workbench.conversation_store`, whose ``validate_turn_append`` already
  refuses fabricating completion.
* **Every failure settles through the Serving runtime, no raw-provider
  fallback.**  The transport is injected; this module imports no HTTP client,
  constructs no endpoint, and embeds no URL scheme literal.  There is no
  alternate provider to fall back to — a failure settles as a terminal Serving
  outcome and nothing else.

The transport is an injected :class:`ServingStreamTransport`; in production it
is backed by the operator-configured Anvil Serving Responses stream, and in
tests it is a scripted generator that mimics an SSE sequence (or raises a
Serving failure) without any network.  This keeps the relay hermetically
testable and structurally incapable of a raw-provider path.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterator, Mapping, Protocol, runtime_checkable

from .chat_routes import (
    DECLARED_CHAT_CONTROLS,
    ChatRouteError,
    ChatRouteSelection,
    _validated_control_value,
)

#: Prompt input ceiling; mirrors the durable content-text bound so an assembled
#: request can never carry more prompt than a turn could ever persist.
MAX_PROMPT_CHARS = 20_000
#: Fallback output ceiling when the selection does not pin ``max_output_tokens``.
DEFAULT_MAX_OUTPUT_TOKENS = 1_024
#: Hard bounds on the relayed stream itself, so a misbehaving Serving stream is
#: refused (fail closed as ``serving_unavailable``) rather than relayed forever.
MAX_STREAM_EVENTS = 10_000
MAX_OUTPUT_CHARS = 100_000


class ChatStreamError(RuntimeError):
    """The bounded Responses request cannot be assembled from the selection."""


class ServingStreamTimeout(RuntimeError):
    """Anvil Serving did not deliver the next stream event within the deadline.

    The injected transport raises this to signal a Serving-side timeout; the
    relay settles the stream as :attr:`StreamOutcome.timed_out`.
    """


class ServingStreamUnavailable(RuntimeError):
    """Anvil Serving refused, dropped, or 5xx'd the stream.

    The injected transport raises this for any Serving-runtime failure; the
    relay settles the stream as :attr:`StreamOutcome.serving_unavailable`.
    """


class StreamOutcome(Enum):
    """The exactly-one terminal state a relayed stream settles into."""

    completed = "completed"
    cancelled = "cancelled"
    timed_out = "timed_out"
    serving_unavailable = "serving_unavailable"


#: The turn lifecycle status each terminal outcome maps to.  Only ``completed``
#: reaches the ``complete`` turn status; a cancelled, timed-out, or unavailable
#: stream is interrupted/cancelled/failed and can never be persisted as a
#: completed response (chat-turn.v1 status enum; acceptance criterion 3).
_OUTCOME_TURN_STATUS: dict[StreamOutcome, str] = {
    StreamOutcome.completed: "complete",
    StreamOutcome.cancelled: "cancelled",
    StreamOutcome.timed_out: "interrupted",
    StreamOutcome.serving_unavailable: "failed",
}

#: Responses SSE event types this relay understands.  A text delta streams a
#: token; a completed event is the only path to a ``completed`` outcome; a
#: failed/error/incomplete event is a Serving-runtime failure.
_DELTA_EVENT = "response.output_text.delta"
_COMPLETED_EVENT = "response.completed"
_FAILURE_EVENTS = frozenset({"response.failed", "response.incomplete", "response.error", "error"})


class CancellationToken:
    """A one-way cancel flag the caller trips to stop an in-flight relay.

    The relay checks :attr:`cancelled` before and after every upstream read and
    closes the transport stream on exit, so tripping this token both terminates
    the upstream request and guarantees no later ``completed`` event.
    """

    __slots__ = ("_cancelled",)

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled


@runtime_checkable
class ServingStreamTransport(Protocol):
    """The injected Serving Responses stream.

    :meth:`open` returns an iterator of parsed SSE event objects (each a mapping
    with a ``type`` key).  It receives the :class:`CancellationToken` so it may
    self-observe a cancel, and its iterator is ``close()``d by the relay on any
    non-completed exit so the upstream request is terminated.  It raises
    :class:`ServingStreamTimeout` or :class:`ServingStreamUnavailable` for
    Serving-runtime failures; it never falls back to another provider.
    """

    def open(
        self, request: Mapping[str, Any], cancel: CancellationToken
    ) -> Iterator[Mapping[str, Any]]:
        ...


@dataclass(frozen=True)
class RelayEvent:
    """One typed event the relay yields: a streamed text delta or the terminal.

    A ``delta`` event carries the incremental ``text``; the single ``terminal``
    event carries the settled :class:`StreamOutcome`.  Exactly one terminal
    event is yielded per stream, always last.
    """

    kind: str  # "delta" | "terminal"
    text: str = ""
    outcome: StreamOutcome | None = None
    #: The strictly-monotonic per-conversation sequence number stamped on this
    #: frame (T008).  ``None`` on an un-sequenced frame the relay emits itself;
    #: the sequencing layer (``workbench.stream_sequence``) stamps a bounded
    #: non-negative int drawn from the durable per-conversation allocator so a
    #: client can detect a dropped frame.
    seq: int | None = None

    def __post_init__(self) -> None:
        if self.kind == "delta":
            if self.outcome is not None:
                raise ChatStreamError("a delta relay event cannot carry a terminal outcome")
        elif self.kind == "terminal":
            if not isinstance(self.outcome, StreamOutcome):
                raise ChatStreamError("a terminal relay event requires a StreamOutcome")
        else:
            raise ChatStreamError(f"unknown relay event kind: {self.kind!r}")
        if self.seq is not None:
            if not isinstance(self.seq, int) or isinstance(self.seq, bool) or self.seq < 0:
                raise ChatStreamError("a relay event seq must be a non-negative int")


def build_bounded_request(selection: ChatRouteSelection, prompt: str) -> dict[str, Any]:
    """Assemble the bounded Responses request from a validated selection.

    Every size and control comes only from the T003.1-validated
    :class:`ChatRouteSelection` (which already bounds each control to the
    chat-turn.v1 limits) plus a bounded prompt; a caller-assembled mapping is
    refused by type, and an over-long or empty prompt is refused, so nothing
    unbounded reaches Serving.  The route reference contributes its Serving
    ``model_profile`` only — never an endpoint, URL, or credential.
    """
    if not isinstance(selection, ChatRouteSelection):
        raise ChatStreamError(
            "bounded request requires the validated ChatRouteSelection, "
            "not a caller-assembled mapping"
        )
    if not isinstance(prompt, str) or not prompt:
        raise ChatStreamError("bounded request requires a non-empty prompt string")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ChatStreamError(
            f"bounded request prompt exceeds the {MAX_PROMPT_CHARS}-char limit"
        )
    controls = selection.controls_dict()
    # A ChatRouteSelection is normally produced by T003.1's fail-closed
    # validation, but ``isinstance`` alone does not prove its controls are in
    # range: a hand-constructed selection could carry unbounded values.
    # Re-validate every control against the *same* chat-turn.v1 bounds
    # (reused from chat_routes, never duplicated here) so nothing out-of-range
    # or undeclared reaches Serving at request-build time.
    for name, value in controls.items():
        if name not in DECLARED_CHAT_CONTROLS:
            raise ChatStreamError(
                f"bounded request refuses an undeclared control: {name!r}"
            )
        try:
            _validated_control_value(name, value)
        except ChatRouteError as exc:
            raise ChatStreamError(
                f"bounded request refuses an out-of-range control: {exc}"
            ) from exc
    request: dict[str, Any] = {
        "model": selection.route.model_profile,
        "route_id": selection.route.route_id,
        "input": prompt,
        "stream": True,
        "max_output_tokens": controls.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS),
    }
    if "temperature_milli" in controls:
        # The control is validated to [0, 2000] milli-units; express it as the
        # Responses [0.0, 2.0] temperature.
        request["temperature"] = controls["temperature_milli"] / 1000
    if "reasoning_effort" in controls:
        request["reasoning"] = {"effort": controls["reasoning_effort"]}
    return request


class ChatStreamRelay:
    """Relay one bounded Responses stream to typed events and one terminal state.

    Stateless with respect to persistence: it accumulates the partial text it
    relayed (for the store to persist as an interrupted turn if the stream did
    not complete) but writes nothing itself.
    """

    def __init__(
        self,
        selection: ChatRouteSelection,
        prompt: str,
        transport: ServingStreamTransport,
        cancel: CancellationToken | None = None,
    ) -> None:
        self._init_common(build_bounded_request(selection, prompt), transport, cancel)

    def _init_common(
        self,
        request: dict[str, Any],
        transport: ServingStreamTransport,
        cancel: CancellationToken | None,
    ) -> None:
        self._request = request
        if not isinstance(transport, ServingStreamTransport):
            raise ChatStreamError("relay requires a ServingStreamTransport")
        self._transport = transport
        self._cancel = cancel if cancel is not None else CancellationToken()
        self._outcome: StreamOutcome | None = None
        self._parts: list[str] = []
        self._chars = 0

    @classmethod
    def for_prepared_request(
        cls,
        request: Mapping[str, Any],
        transport: ServingStreamTransport,
        cancel: CancellationToken | None = None,
    ) -> "ChatStreamRelay":
        """Build a relay over an already-assembled bounded request.

        The chat surface assembles its request from a validated
        :class:`ChatRouteSelection` via the normal constructor.  Advanced mode
        (advanced-model-playground T003) assembles an equivalent *bounded*
        request from its own validated route/control selection and reuses this
        exact stream state machine -- the same distinct terminal outcomes,
        cancellation semantics, and upstream teardown -- without duplicating the
        relay loop.  The request must be a mapping of already-bounded Serving ids
        and controls (no endpoint, URL, or credential); this constructor performs
        no control validation of its own, so the caller is responsible for having
        bounded every value first, exactly as ``build_bounded_request`` does.
        """
        if not isinstance(request, Mapping):
            raise ChatStreamError("a prepared relay request must be a mapping")
        relay = cls.__new__(cls)
        relay._init_common(dict(request), transport, cancel)
        return relay

    @property
    def request(self) -> dict[str, Any]:
        return dict(self._request)

    @property
    def cancel_token(self) -> CancellationToken:
        return self._cancel

    @property
    def outcome(self) -> StreamOutcome | None:
        return self._outcome

    @property
    def partial_text(self) -> str:
        return "".join(self._parts)

    def terminal_turn_status(self) -> str:
        """The chat-turn.v1 lifecycle status for the settled outcome.

        Never ``complete`` unless the stream actually completed, so a cancelled,
        timed-out, or unavailable stream (even one with partial text) cannot be
        persisted or rendered as a completed response.
        """
        if self._outcome is None:
            raise ChatStreamError("stream has not settled into a terminal outcome yet")
        return _OUTCOME_TURN_STATUS[self._outcome]

    def _record_delta(self, text: str) -> bool:
        """Accumulate a delta; return False if the bounded output is exceeded."""
        if self._chars + len(text) > MAX_OUTPUT_CHARS:
            return False
        self._parts.append(text)
        self._chars += len(text)
        return True

    def stream(self) -> Iterator[RelayEvent]:
        """Yield the relayed deltas then exactly one terminal event.

        The terminal event's :class:`StreamOutcome` is also recorded on
        :attr:`outcome`.  After a caller cancel, the relay breaks before reading
        another upstream event, so no ``completed`` terminal is ever emitted
        once cancel is observed.
        """
        upstream = self._transport.open(self._request, self._cancel)
        iterator = iter(upstream)
        events_seen = 0
        try:
            while True:
                if self._cancel.cancelled:
                    self._outcome = StreamOutcome.cancelled
                    break
                try:
                    event = next(iterator)
                    # Interpret inside the guard: a malformed non-mapping frame
                    # makes ``_interpret`` raise ServingStreamUnavailable, and it
                    # must settle a terminal outcome here rather than escape the
                    # generator and leave the stream un-settled with no terminal.
                    relayed = self._interpret(event)
                except StopIteration:
                    # The transport stopped.  If the caller cancelled, this is a
                    # torn-down upstream (cancelled); otherwise the Serving
                    # runtime ended the stream without delivering a completion,
                    # which is unavailable -- never a silent success.
                    self._outcome = (
                        StreamOutcome.cancelled
                        if self._cancel.cancelled
                        else StreamOutcome.serving_unavailable
                    )
                    break
                except (ServingStreamTimeout, TimeoutError):
                    # ``TimeoutError`` also covers the stdlib timeout aliases
                    # (``asyncio.TimeoutError`` and the sockets-module timeout are
                    # both ``TimeoutError`` on 3.10+), so a real deadline maps to
                    # ``timed_out``, not unavailable.
                    self._outcome = StreamOutcome.timed_out
                    break
                except ServingStreamUnavailable:
                    self._outcome = StreamOutcome.serving_unavailable
                    break
                except Exception:  # noqa: BLE001 - fail closed through Serving, never fall back
                    self._outcome = StreamOutcome.serving_unavailable
                    break

                # A cancel tripped during the read (e.g. by the transport itself
                # as a browser cancel would) strictly wins over the frame just
                # read -- including a completion the transport had queued.
                if self._cancel.cancelled:
                    self._outcome = StreamOutcome.cancelled
                    break

                events_seen += 1
                if events_seen > MAX_STREAM_EVENTS:
                    self._outcome = StreamOutcome.serving_unavailable
                    break

                if relayed is None:
                    continue
                if relayed.kind == "terminal":
                    # The single terminal event is emitted once after the loop,
                    # uniformly for every outcome.
                    self._outcome = relayed.outcome
                    break
                if not self._record_delta(relayed.text):
                    self._outcome = StreamOutcome.serving_unavailable
                    break
                yield relayed
        finally:
            # Terminate the upstream request on every exit, including a normal
            # completion: a retained/suspended transport generator is left open
            # otherwise, so always close it -- the injected transport observes
            # the close and tears down the upstream request.
            iterator_close = getattr(iterator, "close", None)
            if callable(iterator_close):
                iterator_close()

        assert self._outcome is not None  # every path above sets an outcome
        yield RelayEvent(kind="terminal", outcome=self._outcome)

    def _interpret(self, event: Mapping[str, Any]) -> RelayEvent | None:
        """Map one Serving SSE event to a relay event, or None to ignore it."""
        if not isinstance(event, Mapping):
            # A malformed frame is a Serving-runtime problem, not a completion.
            raise ServingStreamUnavailable("Serving stream event has an unexpected shape")
        event_type = event.get("type")
        if event_type == _DELTA_EVENT:
            delta = event.get("delta")
            text = delta if isinstance(delta, str) else ""
            return RelayEvent(kind="delta", text=text)
        if event_type == _COMPLETED_EVENT:
            return RelayEvent(kind="terminal", outcome=StreamOutcome.completed)
        if event_type in _FAILURE_EVENTS:
            return RelayEvent(kind="terminal", outcome=StreamOutcome.serving_unavailable)
        # Unrecognized control frames (e.g. response.created) are relayed as
        # nothing; they neither complete nor fail the stream.
        return None
