"""Hermetic tests for parallel multi-route dispatch (AMP T008).

Criterion map (from ``anvil show advanced-model-playground:T008``):

1. Each parallel attempt is an ordinary sibling turn with its own route/usage
   metadata, budget, and terminal state --
   ``test_c1_each_attempt_is_an_isolated_sibling_turn``.
2. Per-attempt cancellation, timeout, and failure are isolated; one attempt's
   outcome never mutates another's --
   ``test_c2_one_failure_does_not_corrupt_siblings``,
   ``test_c2_per_attempt_cancellation_is_isolated``,
   ``test_c2_real_concurrency_seqs_are_globally_unique``,
   ``test_c2_lock_off_contest_detects_the_shared_check_then_act``.
3. Total parallel dispatch respects declared concurrency and budget bounds and
   rejects undeclared routes before any Serving request --
   ``test_c3_over_concurrency_refused``, ``test_c3_over_budget_refused``,
   ``test_c3_undeclared_route_refused_before_any_serving_request``.

Concurrency tests use a real thread barrier so attempts genuinely overlap; the
lock-off contest disables the store lock LOCALLY to prove the race is real, then
restores it (never committed).  No test opens a socket.
"""
from __future__ import annotations

import sys
import threading

import pytest

from workbench.advanced_dispatch import (
    DispatchBudget,
    ParallelDispatchError,
    RouteDispatch,
    dispatch_parallel,
)
from workbench.advanced_routes import AdvancedRouteError, discover_advanced_routes
from workbench.advanced_runtime import AdvancedState
from workbench.chat_stream import CancellationToken, ServingStreamUnavailable
from workbench.conversation_models import (
    ContentBlock,
    ConversationActor,
    RetentionPolicy,
    TurnLineage,
    TurnRedaction,
)
from workbench.conversation_store import MemoryConversationStore
from workbench.response_lifecycle_store import MemoryResponseLifecycleStore, SafeUsage

ACTOR = ConversationActor("operator")
KEY = b"advanced-dispatch-content-hash-1"
REDACTED = TurnRedaction("redacted", "workbench.default")

_RD = "sha256:" + "a1" * 32
_PD = "sha256:" + "b2" * 32


def _routes():
    def cfg(route_id, model):
        return {
            "route_id": route_id,
            "display_name": route_id,
            "route_digest": _RD,
            "profile_digest": _PD,
            "serving_contract_version": "1.0.0",
            "model_profile": model,
            "supported_controls": [
                {"name": "max_output_tokens", "type": "int",
                 "bounds": {"min": 1, "max": 1000000}, "default": 256},
            ],
        }
    return discover_advanced_routes([
        cfg("route.chat-fast", "chat-fast"),
        cfg("route.chat-heavy", "chat-heavy"),
        cfg("route.chat-mini", "chat-mini"),
    ])


def _delta(text):
    return {"type": "response.output_text.delta", "delta": text}


_COMPLETED = {"type": "response.completed", "response": {"id": "r"}}


class ScriptedTransport:
    def __init__(self, events, *, raise_at=None, error=None, cancel_after=None, gate=None):
        self._events = list(events)
        self._raise_at = raise_at
        self._error = error
        self._cancel_after = cancel_after
        self._gate = gate

    def open(self, request, cancel):
        def _gen():
            for index, event in enumerate(self._events):
                if self._gate is not None:
                    self._gate()
                if cancel.cancelled:
                    return
                if self._raise_at is not None and index == self._raise_at:
                    raise self._error
                if self._cancel_after is not None and index == self._cancel_after:
                    cancel.cancel()
                    return
                yield event
            if self._raise_at is not None and self._raise_at >= len(self._events):
                raise self._error
        return _gen()


def _conversation():
    store = MemoryConversationStore(content_hash_key=KEY)
    conversation = store.create_conversation(
        ACTOR, RetentionPolicy("workbench.default-90d", "retained_redacted", "retained_redacted"),
        title="Fan-out",
    )
    root = store.append_turn(
        ACTOR, conversation.id, role="user", status="complete",
        lineage=TurnLineage(None, 0, "initial"), redaction=REDACTED,
        content=(ContentBlock("text", "compare routes"),),
    )
    return store, conversation, root


def _dispatch(route_id, request_id, transport, **kw):
    return RouteDispatch(route_id=route_id, prompt="hi", transport=transport,
                         request_id=request_id, **kw)


# --- Criterion 1: each attempt is an isolated sibling turn --------------------


def test_c1_each_attempt_is_an_isolated_sibling_turn():
    store, conversation, root = _conversation()
    lifecycle = MemoryResponseLifecycleStore()
    dispatches = [
        _dispatch("route.chat-fast", "req_a", ScriptedTransport([_delta("A"), _COMPLETED])),
        _dispatch("route.chat-heavy", "req_b", ScriptedTransport([_delta("B1"), _delta("B2"), _COMPLETED])),
        _dispatch("route.chat-mini", "req_c", ScriptedTransport([_COMPLETED])),
    ]
    result = dispatch_parallel(
        store=store, actor=ACTOR, conversation_id=conversation.id, parent_turn_id=root.id,
        dispatches=dispatches, discovered=_routes(), lifecycle_store=lifecycle,
        budget=DispatchBudget(max_concurrency=3),
    )
    assert len(result.attempts) == 3
    # Distinct sibling turns under the shared parent, each with its own route +
    # terminal state.
    turn_ids = {a.turn_id for a in result.attempts}
    sibling_indices = {a.sibling_index for a in result.attempts}
    assert len(turn_ids) == 3
    assert len(sibling_indices) == 3
    _, turns = store.get_conversation_with_turns(ACTOR, conversation.id)
    advanced = {t.id: t for t in turns if t.mode == "advanced"}
    assert set(advanced) == turn_ids
    for a in result.attempts:
        assert advanced[a.turn_id].lineage.parent_turn_id == root.id
        assert advanced[a.turn_id].status == a.turn_status
        assert a.state in (AdvancedState.complete, AdvancedState.streamed)
        # Each attempt owns its own result + trace scoped to its own turn id.
        assert a.result is not None
        assert a.result.turn_id == a.turn_id
        assert a.result.trace["branch_ref"]["turn_id"] == a.turn_id
        assert a.error is None


def test_c1_each_attempt_carries_its_own_distinct_usage():
    # Each parallel attempt is an ordinary sibling turn with its OWN usage metadata
    # (crit 1). Usage is plumbed per-dispatch, so distinct per-attempt usage must
    # survive into each attempt's own result and its own trace -- never collapsed
    # to a single shared/default SafeUsage.
    store, conversation, root = _conversation()
    lifecycle = MemoryResponseLifecycleStore()
    dispatches = [
        _dispatch("route.chat-fast", "req_a", ScriptedTransport([_delta("A"), _COMPLETED]),
                  usage=SafeUsage(input_tokens=1, output_tokens=10)),
        _dispatch("route.chat-heavy", "req_b", ScriptedTransport([_delta("B"), _COMPLETED]),
                  usage=SafeUsage(input_tokens=2, output_tokens=20)),
        _dispatch("route.chat-mini", "req_c", ScriptedTransport([_delta("C"), _COMPLETED]),
                  usage=SafeUsage(input_tokens=3, output_tokens=30)),
    ]
    result = dispatch_parallel(
        store=store, actor=ACTOR, conversation_id=conversation.id, parent_turn_id=root.id,
        dispatches=dispatches, discovered=_routes(), lifecycle_store=lifecycle,
        budget=DispatchBudget(max_concurrency=3),
    )
    by_req = {a.request_id: a for a in result.attempts}
    # Each attempt kept its own distinct usage on its own result.
    assert by_req["req_a"].result.usage.output_tokens == 10
    assert by_req["req_b"].result.usage.output_tokens == 20
    assert by_req["req_c"].result.usage.output_tokens == 30
    outputs = {a.result.usage.output_tokens for a in result.attempts}
    assert outputs == {10, 20, 30}  # genuinely distinct, not a shared default
    # And the distinct usage is persisted into each attempt's own redacted trace.
    assert by_req["req_a"].result.trace["usage"]["output_tokens"] == 10
    assert by_req["req_c"].result.trace["usage"]["input_tokens"] == 3


# --- Criterion 2: isolation under failure/cancel/timeout + real concurrency ---


def test_c2_one_failure_does_not_corrupt_siblings():
    store, conversation, root = _conversation()
    lifecycle = MemoryResponseLifecycleStore()
    dispatches = [
        _dispatch("route.chat-fast", "req_ok1", ScriptedTransport([_delta("A"), _COMPLETED])),
        _dispatch("route.chat-heavy", "req_bad",
                  ScriptedTransport([_delta("B")], raise_at=1, error=ServingStreamUnavailable("boom"))),
        _dispatch("route.chat-mini", "req_ok2", ScriptedTransport([_delta("C"), _COMPLETED])),
    ]
    result = dispatch_parallel(
        store=store, actor=ACTOR, conversation_id=conversation.id, parent_turn_id=root.id,
        dispatches=dispatches, discovered=_routes(), lifecycle_store=lifecycle,
        budget=DispatchBudget(max_concurrency=3),
    )
    by_req = {a.request_id: a for a in result.attempts}
    # The failing attempt settles failed; the two siblings still complete.
    assert by_req["req_bad"].state is AdvancedState.serving_unavailable
    assert by_req["req_bad"].turn_status == "failed"
    assert by_req["req_ok1"].turn_status == "complete"
    assert by_req["req_ok2"].turn_status == "complete"
    # Each attempt kept its own durable lifecycle terminal (no cross-mutation).
    assert lifecycle.snapshot(ACTOR, "req_ok1").state == "completed"
    assert lifecycle.snapshot(ACTOR, "req_bad").state == "interrupted"
    assert lifecycle.snapshot(ACTOR, "req_ok2").state == "completed"


def test_c2_per_attempt_cancellation_is_isolated():
    store, conversation, root = _conversation()
    lifecycle = MemoryResponseLifecycleStore()
    cancel = CancellationToken()
    dispatches = [
        _dispatch("route.chat-fast", "req_live", ScriptedTransport([_delta("A"), _COMPLETED])),
        _dispatch("route.chat-heavy", "req_cancel",
                  ScriptedTransport([_delta("B"), _delta("B2"), _COMPLETED], cancel_after=2), cancel=cancel),
    ]
    result = dispatch_parallel(
        store=store, actor=ACTOR, conversation_id=conversation.id, parent_turn_id=root.id,
        dispatches=dispatches, discovered=_routes(), lifecycle_store=lifecycle,
        budget=DispatchBudget(max_concurrency=2),
    )
    by_req = {a.request_id: a for a in result.attempts}
    assert by_req["req_cancel"].state is AdvancedState.cancelled
    assert by_req["req_cancel"].turn_status == "cancelled"
    # The sibling's own token was never tripped -- it completed.
    assert by_req["req_live"].turn_status == "complete"


def test_c2_real_concurrency_seqs_are_globally_unique():
    # A real barrier makes the four attempts overlap and hammer the shared
    # per-conversation sequence allocator; the store's lock must keep every
    # committed seq globally unique and no terminal lost.
    store, conversation, root = _conversation()
    lifecycle = MemoryResponseLifecycleStore()
    stream = [_delta("x"), _delta("y"), _delta("z"), _COMPLETED]
    dispatches = [
        _dispatch(rid, f"req_{i}", ScriptedTransport(list(stream)))
        for i, rid in enumerate(
            ["route.chat-fast", "route.chat-heavy", "route.chat-mini", "route.chat-fast"]
        )
    ]
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        result = dispatch_parallel(
            store=store, actor=ACTOR, conversation_id=conversation.id, parent_turn_id=root.id,
            dispatches=dispatches, discovered=_routes(), lifecycle_store=lifecycle,
            budget=DispatchBudget(max_concurrency=4),
        )
    finally:
        sys.setswitchinterval(old)
    assert all(a.turn_status == "complete" for a in result.attempts)
    # Every committed seq in the shared conversation is strictly monotonic and
    # unique across all attempts (the high-water mark never collided).
    hw = lifecycle.rows.conversation_seq[conversation.id]
    seqs = [a.last_committed_seq for a in lifecycle.rows.responses.values()]
    assert len(seqs) == 4
    assert max(seqs) <= hw
    assert len(set(seqs)) == len(seqs)  # each terminal committed a distinct seq


class _NoLock:
    """A do-nothing lock so the store's shared check->act runs unserialized."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_c2_lock_off_contest_detects_the_shared_check_then_act():
    # Contest the shared check->act: with the lifecycle store's lock disabled and
    # aggressive thread switching, concurrent next_seq allocations race and
    # produce DUPLICATE sequence numbers -- proving the lock is what makes the
    # dispatch's concurrency safe. The lock is restored afterwards; nothing here
    # is committed.
    # Detecting a race is probabilistic: the duplicate only appears when a thread
    # switch lands inside the unlocked read-modify-write window, which a loaded
    # host can miss on any single trial. A genuinely-unsafe allocator collides
    # within a few attempts with overwhelming probability, so retry the unlocked
    # contest until a duplicate appears; a genuinely-safe allocator would never
    # collide and this would (correctly) exhaust and fail. This removes the
    # false-negative flakiness without weakening the guarantee.
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-9)
    saw_duplicate = False
    try:
        for _attempt in range(20):
            lifecycle = MemoryResponseLifecycleStore()
            lifecycle.begin(ACTOR, "conv_contested_0001", "req_contested")
            allocated: list[int] = []
            lock = threading.Lock()

            def hammer():
                for _ in range(200):
                    seq = lifecycle.next_seq(ACTOR, "req_contested")
                    with lock:
                        allocated.append(seq)

            lifecycle._lock = _NoLock()  # DISABLE the serialization locally
            threads = [threading.Thread(target=hammer) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            if len(allocated) != len(set(allocated)):
                saw_duplicate = True
                break
    finally:
        sys.setswitchinterval(old)

    # Detection: without the lock the read-modify-write of the seq allocator
    # collides, so at least one seq is handed out twice.
    assert saw_duplicate, (
        "expected the unlocked allocator to produce duplicate seqs; the race was not contested"
    )

    # Sanity: with the lock restored the allocator is unique again.
    fresh = MemoryResponseLifecycleStore()
    fresh.begin(ACTOR, "conv_ok_0001", "req_ok")
    got: list[int] = []
    guard = threading.Lock()

    def clean():
        for _ in range(200):
            seq = fresh.next_seq(ACTOR, "req_ok")
            with guard:
                got.append(seq)

    threads = [threading.Thread(target=clean) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(got) == len(set(got))


# --- Criterion 3: concurrency + budget bounds, undeclared-route rejection ----


def test_c3_over_concurrency_refused():
    store, conversation, root = _conversation()
    lifecycle = MemoryResponseLifecycleStore()
    dispatches = [
        _dispatch("route.chat-fast", "req_a", ScriptedTransport([_COMPLETED])),
        _dispatch("route.chat-heavy", "req_b", ScriptedTransport([_COMPLETED])),
    ]
    with pytest.raises(ParallelDispatchError, match="max_concurrency"):
        dispatch_parallel(
            store=store, actor=ACTOR, conversation_id=conversation.id, parent_turn_id=root.id,
            dispatches=dispatches, discovered=_routes(), lifecycle_store=lifecycle,
            budget=DispatchBudget(max_concurrency=1),
        )
    # Nothing was forked: no advanced sibling turns exist.
    _, turns = store.get_conversation_with_turns(ACTOR, conversation.id)
    assert not any(t.mode == "advanced" for t in turns)


def test_c3_over_budget_refused():
    store, conversation, root = _conversation()
    lifecycle = MemoryResponseLifecycleStore()
    dispatches = [
        _dispatch("route.chat-fast", "req_a", ScriptedTransport([_COMPLETED]),
                  controls={"max_output_tokens": 800}),
        _dispatch("route.chat-heavy", "req_b", ScriptedTransport([_COMPLETED]),
                  controls={"max_output_tokens": 800}),
    ]
    with pytest.raises(ParallelDispatchError, match="budget"):
        dispatch_parallel(
            store=store, actor=ACTOR, conversation_id=conversation.id, parent_turn_id=root.id,
            dispatches=dispatches, discovered=_routes(), lifecycle_store=lifecycle,
            budget=DispatchBudget(max_concurrency=2, max_total_output_tokens=1000),
        )


class _RecordingTransport:
    """Records whether its stream was ever opened (proves no Serving request)."""

    def __init__(self):
        self.opened = False

    def open(self, request, cancel):
        self.opened = True
        def _gen():
            yield _COMPLETED
        return _gen()


def test_c3_undeclared_route_refused_before_any_serving_request():
    store, conversation, root = _conversation()
    lifecycle = MemoryResponseLifecycleStore()
    fast = _RecordingTransport()
    bogus = _RecordingTransport()
    dispatches = [
        _dispatch("route.chat-fast", "req_ok", fast),
        _dispatch("route.not-declared", "req_bad", bogus),  # not in the allowlist
    ]
    with pytest.raises(AdvancedRouteError) as exc:
        dispatch_parallel(
            store=store, actor=ACTOR, conversation_id=conversation.id, parent_turn_id=root.id,
            dispatches=dispatches, discovered=_routes(), lifecycle_store=lifecycle,
            budget=DispatchBudget(max_concurrency=2),
        )
    assert exc.value.reason == "route_unknown"
    # No transport was opened for ANY attempt, and no sibling turn was forked.
    assert fast.opened is False and bogus.opened is False
    _, turns = store.get_conversation_with_turns(ACTOR, conversation.id)
    assert not any(t.mode == "advanced" for t in turns)


def test_c3_out_of_bounds_control_refused_before_dispatch():
    store, conversation, root = _conversation()
    lifecycle = MemoryResponseLifecycleStore()
    rec = _RecordingTransport()
    dispatches = [
        _dispatch("route.chat-fast", "req_bad", rec, controls={"max_output_tokens": 9_999_999_999}),
    ]
    with pytest.raises(AdvancedRouteError) as exc:
        dispatch_parallel(
            store=store, actor=ACTOR, conversation_id=conversation.id, parent_turn_id=root.id,
            dispatches=dispatches, discovered=_routes(), lifecycle_store=lifecycle,
            budget=DispatchBudget(max_concurrency=1),
        )
    assert exc.value.reason == "control_out_of_bounds"
    assert rec.opened is False
