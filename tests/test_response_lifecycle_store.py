"""Reconnect-safe response lifecycle store: persistence, monotonicity, scope.

Each test group maps to an acceptance criterion of chat-first-voice:T003.3:

1. Reconnect returns the last persisted in-progress or terminal state for the
   response request (and never mutates or restarts it).
2. Completed, cancelled, timed-out, and interrupted responses are not restarted
   on reconnect; an in-progress record recovered after a reload is surfaced as
   interrupted, never silently completed.
3. Concurrent (and sequential) lifecycle updates cannot replace a terminal
   state with an earlier state — a terminal is immutable and stable under a
   race.
4. Persisted lifecycle and usage records contain no server-held authentication.
"""
from __future__ import annotations

import dataclasses
import threading

import pytest

from workbench.conversation_models import ConversationActor
from workbench.response_lifecycle_store import (
    IN_PROGRESS_STATE,
    LIFECYCLE_STATE_FOR_OUTCOME,
    TERMINAL_LIFECYCLE_STATES,
    MemoryResponseLifecycleStore,
    ResponseLifecycleError,
    ResponseLifecycleRows,
    SafeUsage,
    UnknownResponseError,
)

ALICE = ConversationActor("operator_alice")
BOB = ConversationActor("operator_bob")
CONV = "conv_lifecycle_fixture_01"
REQ = "resp_lifecycle_fixture_01"


def begun_store(actor: ConversationActor = ALICE, request_id: str = REQ):
    store = MemoryResponseLifecycleStore()
    record = store.begin(actor, CONV, request_id, usage=SafeUsage(input_tokens=7))
    return store, record


# --- Criterion 1: reconnect returns the last persisted state ---------------


def test_reconnect_returns_the_persisted_in_progress_state():
    store, record = begun_store()
    seen = store.reconnect(ALICE, REQ)
    assert seen.state == IN_PROGRESS_STATE
    assert seen.request_id == REQ
    assert seen.conversation_id == CONV
    assert seen.usage.input_tokens == 7


def test_reconnect_returns_the_last_committed_terminal_state():
    store, _ = begun_store()
    store.advance(ALICE, REQ, "completed", usage=SafeUsage(input_tokens=7, output_tokens=42, duration_ms=1200))
    seen = store.reconnect(ALICE, REQ)
    assert seen.state == "completed"
    assert seen.usage.output_tokens == 42
    assert seen.is_terminal


def test_reconnect_does_not_mutate_or_restart():
    store, _ = begun_store()
    store.advance(ALICE, REQ, "completed", usage=SafeUsage(output_tokens=5))
    before = store.reconnect(ALICE, REQ)
    after = store.reconnect(ALICE, REQ)
    # Repeated reconnect is a pure read: same terminal, no new record, no audit
    # entry created by the read itself.
    assert before == after
    assert len(store.rows.responses) == 1
    kinds = [entry.kind for entry in store.rows.audit]
    assert "response.begun" in kinds and "response.terminated" in kinds
    assert not any(kind.startswith("response.reconnect") for kind in kinds)


def test_reconnect_reflects_the_latest_state_after_a_reload():
    rows = ResponseLifecycleRows()
    store = MemoryResponseLifecycleStore(rows)
    store.begin(ALICE, CONV, REQ)
    store.advance(ALICE, REQ, "cancelled")
    # A fresh instance over the same rows simulates a hub restart.
    reopened = MemoryResponseLifecycleStore(rows)
    assert reopened.reconnect(ALICE, REQ).state == "cancelled"


# --- Criterion 2: terminal responses are not restarted on reconnect --------


@pytest.mark.parametrize("terminal", sorted(TERMINAL_LIFECYCLE_STATES))
def test_reconnect_never_restarts_a_terminal_response(terminal):
    store, _ = begun_store()
    if terminal == "interrupted":
        # ``interrupted`` is reached through the reload-recovery path, not a
        # direct caller advance of a live stream.
        store.recover_interrupted()
    else:
        store.advance(ALICE, REQ, terminal)
    settled = store.reconnect(ALICE, REQ)
    assert settled.state == terminal
    # Reconnecting again yields the identical terminal record: no new stream,
    # no duplicate row, no state change.
    again = store.reconnect(ALICE, REQ)
    assert again == settled
    assert len(store.rows.responses) == 1
    # A terminal response can never be re-begun (would be a duplicate/restart).
    with pytest.raises(ResponseLifecycleError, match="already begun"):
        store.begin(ALICE, CONV, REQ)


def test_reload_surfaces_in_progress_as_interrupted_never_completed():
    rows = ResponseLifecycleRows()
    MemoryResponseLifecycleStore(rows).begin(ALICE, CONV, REQ)
    # Restart with recover-on-open: the streaming response is finalized as
    # interrupted, never silently completed and never restarted.
    reopened = MemoryResponseLifecycleStore(rows, recover_on_open=True)
    settled = reopened.reconnect(ALICE, REQ)
    assert settled.state == "interrupted"
    assert settled.state != "completed"
    # And it is now terminal, so it cannot advance to a fabricated completion.
    with pytest.raises(ResponseLifecycleError, match="immutable"):
        reopened.advance(ALICE, REQ, "completed")


def test_begin_cannot_restart_an_in_progress_response():
    store, _ = begun_store()
    with pytest.raises(ResponseLifecycleError, match="already begun"):
        store.begin(ALICE, CONV, REQ)


# --- Criterion 3: a terminal cannot regress to an earlier state ------------


@pytest.mark.parametrize("later", ["in_progress", "completed", "timed_out", "cancelled", "interrupted"])
def test_terminal_state_is_immutable_against_any_later_advance(later):
    store, _ = begun_store()
    store.advance(ALICE, REQ, "completed", usage=SafeUsage(output_tokens=9))
    with pytest.raises(ResponseLifecycleError, match="immutable"):
        store.advance(ALICE, REQ, later, usage=SafeUsage(output_tokens=1))
    # The committed terminal and its usage are unchanged.
    settled = store.reconnect(ALICE, REQ)
    assert settled.state == "completed"
    assert settled.usage.output_tokens == 9


def test_in_progress_may_carry_a_usage_heartbeat_before_terminal():
    store, _ = begun_store()
    store.advance(ALICE, REQ, IN_PROGRESS_STATE, usage=SafeUsage(output_tokens=3))
    assert store.reconnect(ALICE, REQ).usage.output_tokens == 3
    store.advance(ALICE, REQ, "completed", usage=SafeUsage(output_tokens=10))
    assert store.reconnect(ALICE, REQ).state == "completed"


def test_concurrent_advances_cannot_regress_a_terminal():
    store, _ = begun_store()
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def race(name: str, terminal: str):
        barrier.wait()
        try:
            results[name] = store.advance(ALICE, REQ, terminal)
        except ResponseLifecycleError as exc:  # the loser sees an immutable terminal
            results[name] = exc

    threads = [
        threading.Thread(target=race, args=("a", "completed")),
        threading.Thread(target=race, args=("b", "timed_out")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    successes = [name for name, value in results.items() if not isinstance(value, Exception)]
    failures = [value for value in results.values() if isinstance(value, Exception)]
    # Exactly one advance won; the other was refused against the terminal.
    assert len(successes) == 1
    assert len(failures) == 1
    assert "immutable" in str(failures[0])
    # The persisted terminal is stable and matches the winner.
    settled = store.reconnect(ALICE, REQ)
    assert settled.is_terminal
    assert settled.state == results[successes[0]].state
    assert len(store.rows.responses) == 1


def test_advance_rejects_an_unknown_state_and_an_unbegun_request():
    store, _ = begun_store()
    with pytest.raises(ResponseLifecycleError, match="not allowlisted"):
        store.advance(ALICE, REQ, "streaming")  # turn vocabulary, not a lifecycle state
    with pytest.raises(UnknownResponseError, match="unknown response"):
        store.advance(ALICE, "resp_never_begun_00", "completed")


# --- Criterion 4: no server-held authentication in persisted records -------

_AUTH_MARKERS = (
    "token=",
    "bearer",
    "authorization",
    "secret",
    "password",
    "passwd",
    "credential",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "cookie",
    "session_id",
    "private_key",
    "signature",
)

# The exact, closed set of fields a persisted lifecycle/usage row may carry.
_SAFE_LIFECYCLE_FIELDS = {"request_id", "conversation_id", "actor", "state", "usage", "created_at", "updated_at"}
_SAFE_USAGE_FIELDS = {"input_tokens", "output_tokens", "duration_ms"}


def test_persisted_records_carry_no_authentication_marker():
    store = MemoryResponseLifecycleStore()
    store.begin(ALICE, CONV, REQ, usage=SafeUsage(input_tokens=11))
    store.advance(ALICE, REQ, "completed", usage=SafeUsage(input_tokens=11, output_tokens=88, duration_ms=900))
    store.begin(BOB, "conv_other_scope_02", "resp_other_scope_02")

    haystack = repr(store.rows).lower() + repr(store.list_audit(limit=100)).lower()
    for marker in _AUTH_MARKERS:
        assert marker not in haystack, f"persisted rows leaked an auth marker: {marker!r}"


def test_persisted_record_shape_is_a_closed_safe_field_set():
    store, _ = begun_store()
    store.advance(ALICE, REQ, "completed", usage=SafeUsage(output_tokens=4, duration_ms=100))
    record = store.reconnect(ALICE, REQ)
    assert {f.name for f in dataclasses.fields(record)} == _SAFE_LIFECYCLE_FIELDS
    assert {f.name for f in dataclasses.fields(record.usage)} == _SAFE_USAGE_FIELDS
    # There is no string field on the usage record able to hold a credential.
    assert isinstance(record.usage.input_tokens, int)
    assert isinstance(record.usage.output_tokens, int)


def test_usage_rejects_out_of_bound_and_non_integer_counters():
    with pytest.raises(ResponseLifecycleError, match="input_tokens"):
        SafeUsage(input_tokens=-1)
    with pytest.raises(ResponseLifecycleError, match="output_tokens"):
        SafeUsage(output_tokens=10**12)
    with pytest.raises(ResponseLifecycleError, match="duration_ms"):
        SafeUsage(duration_ms=-5)
    with pytest.raises(ResponseLifecycleError, match="input_tokens"):
        SafeUsage(input_tokens=True)  # bool is not an accepted counter


# --- Actor/scope ownership: cross-scope reconnect is a non-leaking 404 ------


def test_cross_actor_reconnect_is_indistinct_from_missing():
    store, _ = begun_store(ALICE)
    # Bob cannot observe Alice's response; the error is identical to a truly
    # missing request id, so existence never leaks across owners.
    with pytest.raises(UnknownResponseError, match="unknown response"):
        store.reconnect(BOB, REQ)
    with pytest.raises(UnknownResponseError, match="unknown response"):
        store.reconnect(BOB, "resp_nonexistent_99")
    # And Bob cannot advance Alice's response either.
    with pytest.raises(UnknownResponseError, match="unknown response"):
        store.advance(BOB, REQ, "cancelled")


def test_same_request_id_in_two_actor_scopes_is_isolated():
    store, _ = begun_store(ALICE)
    # The same request id under Bob is a disjoint namespace: begin succeeds and
    # advancing one scope never touches the other.
    store.begin(BOB, "conv_bob_01", REQ)
    store.advance(BOB, REQ, "cancelled")
    assert store.reconnect(ALICE, REQ).state == IN_PROGRESS_STATE
    assert store.reconnect(BOB, REQ).state == "cancelled"


def test_every_operation_requires_a_typed_actor():
    store, _ = begun_store()
    with pytest.raises(ResponseLifecycleError, match="acting ConversationActor"):
        store.begin("operator_alice", CONV, "resp_x_01")  # type: ignore[arg-type]
    with pytest.raises(ResponseLifecycleError, match="acting ConversationActor"):
        store.reconnect(None, REQ)  # type: ignore[arg-type]
    with pytest.raises(ResponseLifecycleError, match="acting ConversationActor"):
        store.advance(object(), REQ, "completed")  # type: ignore[arg-type]


# --- Relay outcome bridge (builds on chat_stream T003.2) -------------------


def test_stream_outcome_maps_to_a_lifecycle_terminal():
    # Every settled StreamOutcome value maps to a terminal lifecycle state; a
    # serving-unavailable stream is persisted as interrupted (not completed).
    assert set(LIFECYCLE_STATE_FOR_OUTCOME.values()) <= TERMINAL_LIFECYCLE_STATES
    assert LIFECYCLE_STATE_FOR_OUTCOME["serving_unavailable"] == "interrupted"
    assert LIFECYCLE_STATE_FOR_OUTCOME["completed"] == "completed"

    from workbench.chat_stream import StreamOutcome

    # The bridge covers exactly the relay's terminal outcome set.
    assert set(LIFECYCLE_STATE_FOR_OUTCOME) == {o.value for o in StreamOutcome}
