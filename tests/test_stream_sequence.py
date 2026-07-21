"""Gap-detectable sequence + state-version stream metadata (chat-first-voice T008).

Each group maps to a binding acceptance criterion:

1. Sequence numbers are strictly monotonic per conversation and survive
   reconnect -- ``test_seq_is_strictly_monotonic_per_conversation``,
   ``test_seq_continues_strictly_above_last_committed_after_reconnect``,
   ``test_seq_is_monotonic_across_two_requests_in_one_conversation``.
2. A dropped frame is detectable client-side and a snapshot refresh returns
   last-committed state without duplicating the response --
   ``test_dropped_frame_is_detectable_and_snapshot_returns_last_committed`` and
   the ``detect_gap`` / ``needs_snapshot_refresh`` contract tests.
3. A terminal lifecycle state cannot be regressed by a stale-sequence frame --
   ``test_stale_sequence_frame_cannot_regress_a_terminal``,
   ``test_stale_sequence_frame_refused_before_a_state_change``,
   ``test_racing_seq_commits_keep_a_stable_terminal``.

Hermetic: no socket is opened; the relay transport is a scripted in-memory
generator and the store is the row-backed memory implementation.
"""
from __future__ import annotations

import sys
import threading

import pytest

from workbench.chat_routes import discover_chat_routes, validate_chat_route_selection
from workbench.chat_stream import (
    ChatStreamRelay,
    RelayEvent,
    StreamOutcome,
)
from workbench.conversation_models import ConversationActor
from workbench.response_lifecycle_store import (
    IN_PROGRESS_STATE,
    LIFECYCLE_STATE_FOR_OUTCOME,
    MAX_SEQUENCE,
    MemoryResponseLifecycleStore,
    ResponseLifecycleError,
    ResponseLifecycleRows,
    ResponseSnapshot,
    SafeUsage,
)
from workbench.stream_sequence import (
    detect_gap,
    is_stale_frame,
    needs_snapshot_refresh,
    sequence_events,
)

ALICE = ConversationActor("operator_alice")
CONV = "conv_seq_fixture_000001"
REQ = "resp_seq_fixture_000001"
REQ2 = "resp_seq_fixture_000002"

_CONFIGURED = {
    "route_id": "chat.heavy",
    "display_name": "Heavy chat",
    "serving_contract_version": "1.2.0",
    "route_digest": "sha256:" + "b" * 64,
    "model_profile": "chat-heavy",
    "controls": ["temperature_milli", "max_output_tokens", "reasoning_effort"],
}


def _selection():
    discovered = discover_chat_routes([dict(_CONFIGURED)])
    return validate_chat_route_selection("chat.heavy", {}, discovered)


def _delta(text: str) -> dict:
    return {"type": "response.output_text.delta", "delta": text}


_COMPLETED = {"type": "response.completed", "response": {"id": "resp_1"}}


class ScriptedTransport:
    """A minimal scripted Serving stream (mirrors the T003.2 test double)."""

    def __init__(self, events):
        self._events = list(events)

    def open(self, request, cancel):
        def _gen():
            for event in self._events:
                if cancel.cancelled:
                    return
                yield event
        return _gen()


def _begun(actor: ConversationActor = ALICE, request_id: str = REQ, conv: str = CONV):
    store = MemoryResponseLifecycleStore()
    store.begin(actor, conv, request_id, usage=SafeUsage(input_tokens=3))
    return store


# --- Criterion 1: strictly-monotonic per-conversation seq, surviving reconnect


def test_seq_is_strictly_monotonic_per_conversation():
    store = _begun()
    allocated = [store.next_seq(ALICE, REQ) for _ in range(5)]
    assert allocated == [1, 2, 3, 4, 5]
    # Strictly increasing with no repeats or resets.
    assert all(b > a for a, b in zip(allocated, allocated[1:]))


def test_seq_continues_strictly_above_last_committed_after_reconnect():
    rows = ResponseLifecycleRows()
    store = MemoryResponseLifecycleStore(rows)
    store.begin(ALICE, CONV, REQ)
    s1, s2, s3 = (store.next_seq(ALICE, REQ) for _ in range(3))
    store.advance(ALICE, REQ, IN_PROGRESS_STATE, seq=s3)  # commit up to seq 3
    assert (s1, s2, s3) == (1, 2, 3)

    # A fresh store over the same rows simulates a hub restart.
    reopened = MemoryResponseLifecycleStore(rows)
    assert reopened.reconnect(ALICE, REQ).last_committed_seq == 3
    # The next allocation continues strictly above the last value, never resets.
    assert reopened.next_seq(ALICE, REQ) == 4
    assert reopened.next_seq(ALICE, REQ) == 5


def test_seq_is_monotonic_across_two_requests_in_one_conversation():
    store = _begun()
    first = [store.next_seq(ALICE, REQ) for _ in range(3)]  # 1,2,3
    store.begin(ALICE, CONV, REQ2)  # a second response in the SAME conversation
    second = [store.next_seq(ALICE, REQ2) for _ in range(2)]  # 4,5
    assert first == [1, 2, 3]
    assert second == [4, 5]  # one ascending per-conversation sequence, not per-request


def test_state_version_bumps_only_on_a_committed_state_change():
    store = _begun()
    assert store.reconnect(ALICE, REQ).state_version == 1  # begun in_progress
    store.advance(ALICE, REQ, IN_PROGRESS_STATE, usage=SafeUsage(output_tokens=2))
    assert store.reconnect(ALICE, REQ).state_version == 1  # heartbeat: no bump
    store.advance(ALICE, REQ, "completed", seq=store.next_seq(ALICE, REQ))
    assert store.reconnect(ALICE, REQ).state_version == 2  # state change: bump


# --- Criterion 2: dropped frame detectable client-side + snapshot refresh ----


def test_detect_gap_flags_a_skipped_frame_only():
    assert detect_gap(1, 2) is False          # contiguous
    assert detect_gap(1, 3) is True           # frame 2 dropped
    assert detect_gap(0, 5) is True           # frames 1-4 dropped
    assert detect_gap(3, 3) is False          # duplicate is stale, not a gap
    assert detect_gap(3, 2) is False          # older is stale, not a gap


def test_stale_and_refresh_predicates_are_consistent():
    assert is_stale_frame(3, 3) is True
    assert is_stale_frame(3, 2) is True
    assert is_stale_frame(3, 4) is False
    # A gap needs a snapshot refresh; a contiguous or stale frame does not.
    assert needs_snapshot_refresh(1, 3) is True
    assert needs_snapshot_refresh(1, 2) is False
    assert needs_snapshot_refresh(3, 3) is False


def test_relay_frames_are_stamped_with_ascending_seq():
    store = _begun()
    relay = ChatStreamRelay(_selection(), "hi", ScriptedTransport([_delta("He"), _delta("llo"), _COMPLETED]))
    frames = list(sequence_events(relay.stream(), lambda: store.next_seq(ALICE, REQ)))
    seqs = [f.seq for f in frames]
    assert seqs == [1, 2, 3]  # two deltas + one terminal, each stamped
    assert all(b > a for a, b in zip(seqs, seqs[1:]))
    assert frames[-1].kind == "terminal" and frames[-1].outcome is StreamOutcome.completed


def test_dropped_frame_is_detectable_and_snapshot_returns_last_committed():
    store = _begun()
    relay = ChatStreamRelay(_selection(), "hi", ScriptedTransport([_delta("A"), _delta("B"), _delta("C"), _COMPLETED]))
    frames = list(sequence_events(relay.stream(), lambda: store.next_seq(ALICE, REQ)))
    # Commit the terminal at its seq: last-committed state + seq are recorded.
    terminal = frames[-1]
    committed_state = LIFECYCLE_STATE_FOR_OUTCOME[terminal.outcome.value]
    store.advance(ALICE, REQ, committed_state, seq=terminal.seq)
    assert terminal.seq == 4 and committed_state == "completed"

    # A client receives frame seq 1, then seq 3 (seq 2 dropped in transit).
    client_last_seq = frames[0].seq  # 1
    arriving = frames[2].seq  # 3
    assert detect_gap(client_last_seq, arriving) is True
    assert needs_snapshot_refresh(client_last_seq, arriving) is True

    # The client refreshes from the snapshot: it returns the last-committed state
    # and seq WITHOUT the response being re-streamed or duplicated.
    audit_before = len(store.rows.audit)
    rows_before = len(store.rows.responses)
    snap = store.snapshot(ALICE, REQ)
    assert isinstance(snap, ResponseSnapshot)
    assert snap.state == "completed" and snap.is_terminal
    assert snap.last_committed_seq == 4
    assert snap.state_version == 2
    # The snapshot is a pure read: no new frames, no new record, no new audit
    # entry -- the response is not duplicated.
    assert len(store.rows.audit) == audit_before
    assert len(store.rows.responses) == rows_before
    # Repeating the snapshot is idempotent and never re-begins the response.
    assert store.snapshot(ALICE, REQ) == snap
    with pytest.raises(ResponseLifecycleError, match="already begun"):
        store.begin(ALICE, CONV, REQ)


def test_reconnect_after_restart_returns_last_committed_seq_and_state():
    rows = ResponseLifecycleRows()
    store = MemoryResponseLifecycleStore(rows)
    store.begin(ALICE, CONV, REQ)
    store.advance(ALICE, REQ, "completed", seq=store.next_seq(ALICE, REQ))
    reopened = MemoryResponseLifecycleStore(rows)
    seen = reopened.reconnect(ALICE, REQ)
    assert seen.state == "completed"
    assert seen.last_committed_seq == 1
    assert reopened.snapshot(ALICE, REQ).last_committed_seq == 1


# --- Criterion 3: a stale-sequence frame cannot regress a terminal -----------


def test_stale_sequence_frame_cannot_regress_a_terminal():
    store = _begun()
    committed = store.advance(ALICE, REQ, "completed", seq=store.next_seq(ALICE, REQ))
    assert committed.last_committed_seq == 1 and committed.state_version == 2
    # A late/stale frame (a lower seq, an earlier state) arriving after the
    # terminal is refused by terminal-immutability; the terminal is unchanged.
    for stale_state, stale_seq in (("in_progress", 1), ("cancelled", 1), ("completed", 1)):
        with pytest.raises(ResponseLifecycleError, match="immutable"):
            store.advance(ALICE, REQ, stale_state, seq=stale_seq)
    settled = store.reconnect(ALICE, REQ)
    assert settled.state == "completed"
    assert settled.last_committed_seq == 1
    assert settled.state_version == 2


def test_stale_sequence_frame_refused_before_a_state_change():
    store = _begun()
    store.advance(ALICE, REQ, IN_PROGRESS_STATE, seq=store.next_seq(ALICE, REQ))  # commit seq 1
    # A frame at or below the last committed seq is stale: it is refused before
    # it can drive any state change (even on a non-terminal record).
    with pytest.raises(ResponseLifecycleError, match="stale sequence"):
        store.advance(ALICE, REQ, "completed", seq=1)
    with pytest.raises(ResponseLifecycleError, match="stale sequence"):
        store.advance(ALICE, REQ, "completed", seq=0)
    # The record is still in_progress; nothing regressed.
    assert store.reconnect(ALICE, REQ).state == IN_PROGRESS_STATE
    # A strictly-higher seq is accepted and settles the terminal.
    settled = store.advance(ALICE, REQ, "completed", seq=2)
    assert settled.state == "completed" and settled.last_committed_seq == 2


def test_racing_seq_commits_keep_a_stable_terminal():
    store = _begun()
    # Force aggressive thread switching so the race is real; restore in finally
    # so the interval never leaks to other tests.
    previous_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def race(name: str, terminal: str, seq: int):
        barrier.wait()
        try:
            results[name] = store.advance(ALICE, REQ, terminal, seq=seq)
        except ResponseLifecycleError as exc:
            results[name] = exc

    try:
        threads = [
            threading.Thread(target=race, args=("a", "completed", 5)),
            threading.Thread(target=race, args=("b", "cancelled", 3)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(previous_interval)

    successes = [name for name, value in results.items() if not isinstance(value, Exception)]
    failures = [value for value in results.values() if isinstance(value, Exception)]
    # Exactly one commit won; the other was refused against the committed terminal.
    assert len(successes) == 1
    assert len(failures) == 1
    settled = store.reconnect(ALICE, REQ)
    assert settled.is_terminal
    assert settled.state == results[successes[0]].state
    assert settled.last_committed_seq == results[successes[0]].last_committed_seq
    assert len(store.rows.responses) == 1


# --- Bounds and validation ---------------------------------------------------


def test_commit_seq_is_bounded_and_typed():
    store = _begun()
    with pytest.raises(ResponseLifecycleError, match="commit seq"):
        store.advance(ALICE, REQ, IN_PROGRESS_STATE, seq=MAX_SEQUENCE + 1)
    with pytest.raises(ResponseLifecycleError, match="commit seq"):
        store.advance(ALICE, REQ, IN_PROGRESS_STATE, seq=True)  # bool is not a seq


def test_relay_event_seq_field_rejects_a_negative_or_non_int():
    from workbench.chat_stream import ChatStreamError

    assert RelayEvent(kind="delta", text="a", seq=1).seq == 1
    assert RelayEvent(kind="delta", text="a").seq is None
    with pytest.raises(ChatStreamError, match="seq"):
        RelayEvent(kind="delta", text="a", seq=-1)
    with pytest.raises(ChatStreamError, match="seq"):
        RelayEvent(kind="delta", text="a", seq=True)


def test_seq_source_is_not_a_cross_actor_oracle():
    store = _begun(ALICE, REQ)
    other = ConversationActor("operator_bob")
    from workbench.response_lifecycle_store import UnknownResponseError

    with pytest.raises(UnknownResponseError, match="unknown response"):
        store.next_seq(other, REQ)
    with pytest.raises(UnknownResponseError, match="unknown response"):
        store.snapshot(other, REQ)


def test_committed_seq_raises_the_high_water_so_next_seq_never_allocates_below():
    # The allocator invariant is self-enforced: a committed seq (even one not
    # drawn from next_seq) raises the per-conversation high-water, so a fresh
    # store over the same rows allocates strictly ABOVE it — never a stale seq.
    store = _begun()
    store.advance(ALICE, REQ, "in_progress", usage=SafeUsage(input_tokens=1), seq=500)
    reopened = MemoryResponseLifecycleStore(store.rows)
    assert reopened.next_seq(ALICE, REQ) == 501
