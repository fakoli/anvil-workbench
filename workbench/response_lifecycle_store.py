"""Reconnect-safe response lifecycle persistence (chat-first-voice T003.3).

The bounded Responses stream relay (:mod:`workbench.chat_stream`, T003.2) is
deliberately stateless: it yields typed events and settles into exactly one
:class:`~workbench.chat_stream.StreamOutcome`, but writes nothing durable.  This
module is the durable half that mirror lets a *reconnecting* client observe the
last committed lifecycle of an in-flight or finished response request — without
restarting a settled response, duplicating one that is already recorded, or
fabricating a completion the relay never produced.

State machine (the four acceptance criteria this slice binds):

* A response request is persisted once with :meth:`begin` in the
  ``in_progress`` state (the streaming phase).  It then advances, via
  :meth:`advance`, to exactly ONE terminal state — ``completed``, ``cancelled``,
  ``timed_out``, or ``interrupted`` — after which the record is IMMUTABLE.
  ``interrupted`` is reached two ways: a direct :meth:`advance` (the relay
  bridges its ``serving_unavailable`` outcome to ``interrupted``), and
  reload recovery — a record still ``in_progress`` after a hub restart is
  surfaced by :meth:`recover_interrupted` as ``interrupted`` (its stream is
  gone), never silently completed and never restarted.  This mirrors the streaming ->
  ``interrupted`` recovery of :mod:`workbench.conversation_store`.
* :meth:`reconnect` returns the last persisted state (criterion 1) and NEVER
  mutates, restarts, or re-streams: a terminal response reconnects to its
  terminal record; an in-progress response reconnects to its in-progress record
  (criterion 2).
* Lifecycle is monotonic (criterion 3): ``in_progress -> terminal`` is allowed
  exactly once; ``terminal -> anything`` (an earlier state, a different
  terminal, or the same terminal again) fails closed.  Every public method runs
  under a reentrant lock, so two racing advances cannot both win — the first
  commits the terminal, the second observes it and is refused, and the terminal
  is stable.
* Only bounded SAFE usage metadata is persisted (criterion 4): token counts,
  timing, and the state token — never a credential, bearer token, authorization
  header, or any server-held authentication.  The record shape structurally
  cannot carry one; :class:`SafeUsage` admits only non-negative bounded integers
  and there is no free-form string field anywhere on a persisted row.

``MemoryResponseLifecycleStore`` is the hermetic row-backed implementation in
the ``MemoryConversationStore``/``MemoryStore`` idiom: all persisted values are
frozen dataclasses, and the row containers can be handed to a fresh instance to
simulate a hub restart over the same durable records.  Records are actor-scoped
exactly like the conversation store: a reconnect (or advance) against another
actor's request raises the same ``unknown response`` error as a missing request
id, so record existence never leaks across owners.  A production Postgres
projection follows the ``PostgresStore`` idiom and lands with the API slice; it
is not implemented here.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import wraps
from typing import Protocol

from .conversation_models import ConversationActor
from .models import new_id, now_utc
from .store import StoreError

#: The single non-terminal lifecycle state: the streaming phase of a response.
IN_PROGRESS_STATE = "in_progress"
#: The mutually-exclusive terminal states.  ``interrupted`` covers both an
#: explicit cut-off and the post-restart recovery of a stream that never
#: settled; it is never rendered as a completion.  These mirror the relay's
#: ``StreamOutcome`` terminal set (T003.2), with a Serving-unavailable/failed
#: stream persisted as ``interrupted`` (see ``LIFECYCLE_STATE_FOR_OUTCOME``): it
#: was not completed and must never be restarted as a duplicate.
TERMINAL_LIFECYCLE_STATES = frozenset({"completed", "cancelled", "timed_out", "interrupted"})
LIFECYCLE_STATES = frozenset({IN_PROGRESS_STATE}) | TERMINAL_LIFECYCLE_STATES

#: Mapping from a settled relay ``StreamOutcome`` value (T003.2) to the durable
#: lifecycle terminal.  Kept as plain string keys so this module never imports
#: the relay (it stays hermetic and decoupled).  ``serving_unavailable`` has no
#: dedicated lifecycle terminal — a failed/dropped stream did not complete and
#: must not be restarted, so it is persisted as ``interrupted``.
LIFECYCLE_STATE_FOR_OUTCOME: dict[str, str] = {
    "completed": "completed",
    "cancelled": "cancelled",
    "timed_out": "timed_out",
    "serving_unavailable": "interrupted",
}

#: Bounds on the safe usage counters, so a misbehaving caller cannot persist an
#: unbounded or negative counter.
MAX_USAGE_TOKENS = 100_000_000
MAX_USAGE_DURATION_MS = 86_400_000  # 24h; a bound, not a policy

#: Hard bound on a per-conversation sequence number and a state-version, so a
#: misbehaving caller cannot allocate or commit an unbounded counter.  Ample for
#: any real conversation while remaining a fail-closed ceiling.
MAX_SEQUENCE = 1_000_000_000

_UNKNOWN_RESPONSE = "unknown response"


class ResponseLifecycleError(StoreError):
    """A response lifecycle operation violates the reconnect-safe contract."""


class UnknownResponseError(ResponseLifecycleError):
    """The response request does not exist for this actor.

    Raised identically for a missing request id and for another actor's
    request, so a cross-actor probe cannot learn whether the id exists.
    """


@dataclass(frozen=True)
class SafeUsage:
    """Bounded, content-free usage metadata for one response request.

    Only non-negative integer counters and an optional bounded duration are
    representable — token counts, timing, nothing else.  There is deliberately
    no string field, so this record cannot carry a credential, bearer token,
    authorization header, or any other server-held authentication (criterion 4).
    """

    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int | None = None

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ResponseLifecycleError(f"usage {name} must be an int")
            if value < 0 or value > MAX_USAGE_TOKENS:
                raise ResponseLifecycleError(f"usage {name} is out of the bounded range")
        if self.duration_ms is not None:
            if not isinstance(self.duration_ms, int) or isinstance(self.duration_ms, bool):
                raise ResponseLifecycleError("usage duration_ms must be an int or None")
            if self.duration_ms < 0 or self.duration_ms > MAX_USAGE_DURATION_MS:
                raise ResponseLifecycleError("usage duration_ms is out of the bounded range")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class ResponseLifecycle:
    """One actor-owned, conversation-scoped response lifecycle record.

    Immutable once its ``state`` is terminal.  Carries only ids, the state
    token, bounded :class:`SafeUsage`, and timestamps — never message content
    and never authentication.
    """

    request_id: str
    conversation_id: str
    actor: ConversationActor
    state: str
    usage: SafeUsage = field(default_factory=SafeUsage)
    #: Monotonic version that bumps on every committed *state change* (T008).  A
    #: reconnecting client reads it to render a stable, monotonically-versioned
    #: view; ordering and terminal-regression protection are enforced by ``seq``
    #: (stale frames refused at/below ``last_committed_seq``) and terminal
    #: immutability, not by a version comparison in this slice.
    state_version: int = 1
    #: The highest per-conversation sequence number committed for this response
    #: (T008).  A reconnecting client resyncs to this seq and refuses any stale
    #: frame at or below it, so a dropped frame never duplicates the response.
    last_committed_seq: int = 0
    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not (4 <= len(self.request_id) <= 256):
            raise ResponseLifecycleError("response request id is invalid")
        if not isinstance(self.conversation_id, str) or not (4 <= len(self.conversation_id) <= 256):
            raise ResponseLifecycleError("response conversation id is invalid")
        if not isinstance(self.actor, ConversationActor):
            raise ResponseLifecycleError("a response lifecycle record requires the owning ConversationActor")
        if self.state not in LIFECYCLE_STATES:
            raise ResponseLifecycleError(f"response lifecycle state is not allowlisted: {self.state!r}")
        if not isinstance(self.usage, SafeUsage):
            raise ResponseLifecycleError("response usage must be a SafeUsage")
        for name in ("state_version", "last_committed_seq"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ResponseLifecycleError(f"response {name} must be an int")
            if value < 0 or value > MAX_SEQUENCE:
                raise ResponseLifecycleError(f"response {name} is out of the bounded range")

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_LIFECYCLE_STATES


@dataclass(frozen=True)
class ResponseLifecycleAudit:
    """One content-free store audit entry (lifecycle metadata only)."""

    id: str
    kind: str
    request_id: str
    conversation_id: str
    actor_id: str
    state: str
    usage: SafeUsage
    state_version: int = 1
    last_committed_seq: int = 0
    created_at: datetime = field(default_factory=now_utc)


@dataclass(frozen=True)
class ResponseSnapshot:
    """The last-committed lifecycle state a reconnecting client resyncs to (T008).

    A snapshot is a pure read of the durable record: it carries only the settled
    state, its monotonic ``state_version``, and the highest committed
    per-conversation sequence number.  Returning it lets a client that detected a
    dropped frame refresh to the last-committed state WITHOUT the response being
    re-streamed or duplicated — there is no message content and no frame replay
    here, only the position a client resyncs to.
    """

    request_id: str
    conversation_id: str
    state: str
    state_version: int
    last_committed_seq: int
    is_terminal: bool


@dataclass
class ResponseLifecycleRows:
    """The persisted row containers shared by store instances.

    Values are frozen dataclasses; the dict/list containers stand in for the
    durable tables, so a fresh ``MemoryResponseLifecycleStore`` over the same
    rows simulates a hub restart over the same persisted records.  Records are
    keyed by ``(actor_id, request_id)`` so two actors' identical request ids
    live in disjoint namespaces — a cross-actor insert refusal can never become
    an existence oracle.
    """

    responses: dict[tuple[str, str], ResponseLifecycle] = field(default_factory=dict)
    audit: list[ResponseLifecycleAudit] = field(default_factory=list)
    #: The per-conversation strictly-monotonic sequence high-water mark (T008),
    #: keyed by ``conversation_id`` so every response in one conversation draws
    #: from a single ascending sequence.  It lives in the shared row container,
    #: so a fresh store over the same rows (a hub restart) continues allocating
    #: strictly above the last committed seq and never resets.
    conversation_seq: dict[str, int] = field(default_factory=dict)


class ResponseLifecycleStore(Protocol):
    def begin(
        self, actor: ConversationActor, conversation_id: str, request_id: str,
        usage: SafeUsage | None = None,
    ) -> ResponseLifecycle: ...
    def advance(
        self, actor: ConversationActor, request_id: str, state: str,
        usage: SafeUsage | None = None, *, seq: int | None = None,
    ) -> ResponseLifecycle: ...
    def next_seq(self, actor: ConversationActor, request_id: str) -> int: ...
    def snapshot(self, actor: ConversationActor, request_id: str) -> ResponseSnapshot: ...
    def reconnect(self, actor: ConversationActor, request_id: str) -> ResponseLifecycle: ...
    # HUB-INTERNAL / SYSTEM-ONLY: spans all actors' records; run after a restart
    # before serving reconnects, never wired to an actor-facing endpoint.
    def recover_interrupted(self) -> tuple[ResponseLifecycleAudit, ...]: ...
    def list_audit(self, limit: int = 20) -> list[ResponseLifecycleAudit]: ...


class MemoryResponseLifecycleStore:
    """Hermetic row-backed response lifecycle store; requests are serialized."""

    def __init__(
        self,
        rows: ResponseLifecycleRows | None = None,
        *,
        recover_on_open: bool = False,
    ) -> None:
        """Open the store over ``rows``.

        After a restart over persisted rows, ``recover_interrupted()`` SHOULD
        run before serving reconnects, or a stale ``in_progress`` record can be
        mistaken for a live stream to resume; pass ``recover_on_open=True`` to
        bind that recovery to construction.  This store holds no authentication
        and no message content — only ids, the state token, bounded usage
        counters, and timestamps — so, unlike the conversation store, it needs
        no server-held key.
        """
        # Single-writer serialization for the in-memory backend: every public
        # method runs under this reentrant lock so concurrent threadpool
        # requests cannot interleave a mutation (the Postgres backend will use
        # row-level transactions instead).  This is what makes a terminal
        # stable under a race (criterion 3).
        self._lock = threading.RLock()
        self.rows = rows if rows is not None else ResponseLifecycleRows()
        if recover_on_open:
            self.recover_interrupted()

    # -- internal helpers -------------------------------------------------

    @staticmethod
    def _require_actor(actor: ConversationActor) -> ConversationActor:
        if not isinstance(actor, ConversationActor):
            raise ResponseLifecycleError("a store operation requires the acting ConversationActor")
        return actor

    @staticmethod
    def _key(actor: ConversationActor, request_id: str) -> tuple[str, str]:
        return (actor.actor_id, request_id)

    def _owned(self, actor: ConversationActor, request_id: str) -> ResponseLifecycle:
        """Resolve a lifecycle record through the acting actor's ownership only.

        A missing request id and another actor's request id both raise the same
        ``UnknownResponseError``, so a cross-actor probe cannot distinguish
        them (no existence oracle across owners).
        """
        self._require_actor(actor)
        if not isinstance(request_id, str):
            raise ResponseLifecycleError("response request id must be a string")
        record = self.rows.responses.get(self._key(actor, request_id))
        if record is None:
            raise UnknownResponseError(_UNKNOWN_RESPONSE)
        return record

    def _store(self, kind: str, record: ResponseLifecycle) -> ResponseLifecycle:
        self.rows.responses[self._key(record.actor, record.request_id)] = record
        self.rows.audit.append(
            ResponseLifecycleAudit(
                id=new_id("rlaudit"),
                kind=kind,
                request_id=record.request_id,
                conversation_id=record.conversation_id,
                actor_id=record.actor.actor_id,
                state=record.state,
                usage=record.usage,
                state_version=record.state_version,
                last_committed_seq=record.last_committed_seq,
            )
        )
        return record

    # -- lifecycle operations ---------------------------------------------

    def begin(
        self, actor: ConversationActor, conversation_id: str, request_id: str,
        usage: SafeUsage | None = None,
    ) -> ResponseLifecycle:
        """Persist a new response request in the ``in_progress`` state.

        A response request begins exactly once.  Beginning an already-persisted
        request (in-progress or terminal) fails closed, so a reconnect-driven
        retry can never duplicate or restart a response by re-issuing ``begin``.
        """
        self._require_actor(actor)
        if usage is not None and type(usage) is not SafeUsage:
            # Exactly SafeUsage, not a subclass: a subclass could add a free-form
            # string field and defeat the 'no credential can be persisted' shape.
            raise ResponseLifecycleError("usage must be exactly a SafeUsage or None")
        if self.rows.responses.get(self._key(actor, request_id)) is not None:
            raise ResponseLifecycleError("response request has already begun; it cannot be restarted")
        now = now_utc()
        record = ResponseLifecycle(
            request_id=request_id,
            conversation_id=conversation_id,
            actor=actor,
            state=IN_PROGRESS_STATE,
            usage=usage if usage is not None else SafeUsage(),
            created_at=now,
            updated_at=now,
        )
        return self._store("response.begun", record)

    def advance(
        self, actor: ConversationActor, request_id: str, state: str,
        usage: SafeUsage | None = None, *, seq: int | None = None,
    ) -> ResponseLifecycle:
        """Advance a response's lifecycle monotonically toward its terminal.

        Allowed transitions: ``in_progress -> in_progress`` (a usage/seq
        heartbeat while still streaming) and ``in_progress -> terminal``
        (exactly once).  A terminal record is committed and IMMUTABLE: any
        advance from a terminal state — to an earlier state, a different
        terminal, or the same terminal again — fails closed, so a late or racing
        update can never replace a terminal with an earlier state.

        ``seq`` optionally commits the highest per-conversation sequence number
        the client should have observed at this point (T008).  A committed seq
        must STRICTLY advance ``last_committed_seq``; a stale frame (``seq`` at or
        below the last committed) is refused before any state change, so a
        dropped/replayed/out-of-order frame can never regress the lifecycle nor
        regress a terminal.  A committed state change bumps ``state_version`` so a
        reconnecting client can order versions and reject a lower one.
        """
        record = self._owned(actor, request_id)
        if state not in LIFECYCLE_STATES:
            raise ResponseLifecycleError(f"response lifecycle state is not allowlisted: {state!r}")
        if usage is not None and type(usage) is not SafeUsage:
            # Exactly SafeUsage, not a subclass: a subclass could add a free-form
            # string field and defeat the 'no credential can be persisted' shape.
            raise ResponseLifecycleError("usage must be exactly a SafeUsage or None")
        if seq is not None:
            if not isinstance(seq, int) or isinstance(seq, bool):
                raise ResponseLifecycleError("commit seq must be an int")
            if seq < 0 or seq > MAX_SEQUENCE:
                raise ResponseLifecycleError("commit seq is out of the bounded range")
        if record.is_terminal:
            # A terminal is immutable: a stale-sequence frame arriving after the
            # terminal is committed is refused here and can never regress it.
            raise ResponseLifecycleError(
                "committed lifecycle is immutable; a terminal response cannot advance"
            )
        # record.state is in_progress here.
        if seq is not None and seq <= record.last_committed_seq:
            # A frame at or below the last committed seq is stale/out-of-order;
            # it carries no forward progress and must never drive a state change.
            raise ResponseLifecycleError(
                "stale sequence frame refused; it cannot regress the lifecycle"
            )
        if state == IN_PROGRESS_STATE and usage is None and seq is None:
            # A no-op heartbeat with neither new usage nor a new seq carries no
            # information.
            raise ResponseLifecycleError("an in_progress advance must carry updated usage or seq")
        advanced = replace(
            record,
            state=state,
            usage=usage if usage is not None else record.usage,
            # A committed *state change* bumps the version; a same-state heartbeat
            # does not (it advances seq/usage only).
            state_version=record.state_version + (1 if state != record.state else 0),
            last_committed_seq=seq if seq is not None else record.last_committed_seq,
            updated_at=now_utc(),
        )
        if seq is not None:
            # Self-enforce the allocator invariant: a committed seq raises the
            # conversation high-water, so next_seq always allocates strictly
            # above any committed value — even across a restart and regardless
            # of whether the seq came from next_seq or a caller.
            current_hw = self.rows.conversation_seq.get(advanced.conversation_id, 0)
            if seq > current_hw:
                self.rows.conversation_seq[advanced.conversation_id] = seq
        kind = "response.terminated" if advanced.is_terminal else "response.progressed"
        return self._store(kind, advanced)

    def next_seq(self, actor: ConversationActor, request_id: str) -> int:
        """Allocate the next strictly-monotonic per-conversation sequence number.

        Every emitted stream frame draws its ``seq`` here.  The counter is scoped
        to the response's conversation and lives in the shared row container, so
        it is strictly increasing across every response in the conversation and
        continues strictly above the last value after a hub restart over the same
        rows (criterion 1) — it never resets.  Allocation requires owning a
        response in the conversation, so it is not a cross-actor oracle.
        """
        record = self._owned(actor, request_id)
        current = self.rows.conversation_seq.get(record.conversation_id, 0)
        nxt = current + 1
        if nxt > MAX_SEQUENCE:
            raise ResponseLifecycleError("conversation sequence is exhausted")
        self.rows.conversation_seq[record.conversation_id] = nxt
        return nxt

    def snapshot(self, actor: ConversationActor, request_id: str) -> ResponseSnapshot:
        """Return the last-committed lifecycle position for a resync (criterion 2).

        A pure read: it returns the settled state, its ``state_version``, and the
        highest committed seq so a client that detected a dropped frame can
        refresh to last-committed state WITHOUT the response being re-streamed or
        duplicated.  It never mutates, re-begins, or replays frames.
        """
        record = self._owned(actor, request_id)
        return ResponseSnapshot(
            request_id=record.request_id,
            conversation_id=record.conversation_id,
            state=record.state,
            state_version=record.state_version,
            last_committed_seq=record.last_committed_seq,
            is_terminal=record.is_terminal,
        )

    def reconnect(self, actor: ConversationActor, request_id: str) -> ResponseLifecycle:
        """Return the last persisted lifecycle for the response request.

        This is the reconnect path (criterion 1): it returns the last committed
        state — ``in_progress`` for a response still streaming, or its single
        terminal for one already settled — and NEVER mutates, restarts, or
        re-streams a response (criterion 2).  A cross-scope reconnect (another
        actor's request id) is an indistinct ``UnknownResponseError``, identical
        to a genuinely missing id, so one actor cannot probe another's records.
        """
        return self._owned(actor, request_id)

    def recover_interrupted(self) -> tuple[ResponseLifecycleAudit, ...]:
        """Post-restart recovery: flip every persisted ``in_progress`` record to
        ``interrupted``.

        A response found ``in_progress`` after a reload was streaming when the
        hub stopped; its in-memory relay is gone, so the durable truth is that
        it was interrupted before it settled.  It is surfaced as ``interrupted``
        (a terminal), never silently completed and never restarted — mirroring
        the conversation store's streaming -> ``interrupted`` reload recovery.
        HUB-INTERNAL: spans all actors' records.
        """
        recovered: list[ResponseLifecycleAudit] = []
        for key, record in list(self.rows.responses.items()):
            if record.state != IN_PROGRESS_STATE:
                continue
            interrupted = replace(record, state="interrupted", updated_at=now_utc())
            self.rows.responses[key] = interrupted
            audit = ResponseLifecycleAudit(
                id=new_id("rlaudit"),
                kind="response.recovered_interrupted",
                request_id=interrupted.request_id,
                conversation_id=interrupted.conversation_id,
                actor_id=interrupted.actor.actor_id,
                state=interrupted.state,
                usage=interrupted.usage,
            )
            self.rows.audit.append(audit)
            recovered.append(audit)
        return tuple(recovered)

    def list_audit(self, limit: int = 20) -> list[ResponseLifecycleAudit]:
        return list(reversed(self.rows.audit[-max(1, min(limit, 100)):]))


def _synchronize_memory_store() -> None:
    """Wrap every public MemoryResponseLifecycleStore method under its lock."""

    def _guard(method):
        @wraps(method)
        def _locked(self, *args, **kwargs):
            with self._lock:
                return method(self, *args, **kwargs)
        return _locked

    for _name in (
        "begin",
        "advance",
        "next_seq",
        "snapshot",
        "reconnect",
        "recover_interrupted",
        "list_audit",
    ):
        setattr(MemoryResponseLifecycleStore, _name, _guard(getattr(MemoryResponseLifecycleStore, _name)))


_synchronize_memory_store()
