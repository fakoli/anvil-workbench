"""Actor-scoped idempotency keys for side-effecting chat APIs (chat-first-voice T007).

The side-effecting chat endpoints (:mod:`workbench.conversation_api`) each
create or advance exactly one durable record.  A dropped response or an
over-eager client retry must not turn one intended effect into two records.
This module is the durable dedup layer that makes a repeated side-effecting
request converge on ONE record and ONE response, without ever letting one
actor observe or replay another actor's result.

The discipline it reuses (it does not invent a parallel one):

* The dedup identity is the triple ``(actor_id, operation, key)`` — the acting
  identity, the named endpoint/operation, and the caller-supplied idempotency
  key.  ``operation`` scopes the key per endpoint, and ``actor_id`` scopes it
  per owner, so reusing another actor's key lands in a disjoint namespace and
  can never read or replay their result (criterion 2).
* The stored result is bound to a ``request_hash`` — the canonical
  :func:`workbench.store.payload_hash` (sorted-key SHA-256) of the material
  request, exactly the hashing the delivery path already uses to bind an
  approval to its bridge-side action.  A key reused with the SAME request hash
  replays the stored response WITHOUT re-executing (criterion 1); a key reused
  with a DIFFERENT request hash is a typed :class:`IdempotencyConflictError`,
  never a silent dedup of a mismatched payload (criterion 3).  The hash is
  compared in constant time, matching the approval-consume discipline.
* :meth:`MemoryIdempotencyStore.run` executes the caller's operation INSIDE the
  per-instance reentrant lock, so two concurrent same-key requests resolve to
  exactly one execution and one record: the first commits under the lock, the
  second observes the committed record and replays it.  A failed execution
  (an exception from the operation) stores nothing and propagates, so a genuine
  error stays retriable rather than being memoized as a fake success.

``MemoryIdempotencyStore`` is the hermetic row-backed implementation in the
``MemoryResponseLifecycleStore``/``MemoryConversationStore`` idiom: persisted
values are frozen dataclasses, records are keyed by ``(actor_id, operation,
key)`` in a plain dict container, and stored responses are deep-copied in and
out so a persisted record can never be mutated through a returned reference.
A production Postgres projection follows the ``PostgresStore`` idiom (a unique
constraint on the triple plus a row lock) and lands with the API slice; it is
not implemented here.
"""
from __future__ import annotations

import copy
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime
from functools import wraps
from typing import Any, Callable, Protocol

from .conversation_models import ConversationActor
from .models import new_id, now_utc
from .store import StoreError, payload_hash

#: Bounds on a caller-supplied idempotency key, so a missing or absurd key is
#: refused rather than silently accepted.
MIN_KEY_CHARS = 1
MAX_KEY_CHARS = 200


class IdempotencyError(StoreError):
    """A side-effecting request violates the idempotency-key contract."""


class IdempotencyConflictError(IdempotencyError):
    """The same ``(actor, operation, key)`` was reused with a different payload.

    Raised instead of silently returning the stored result, so a key can never
    dedup a materially different request into an earlier response (criterion 3).
    """


@dataclass(frozen=True)
class IdempotencyRecord:
    """One committed side-effecting result, bound to its request hash.

    Immutable once stored: ``response`` is the JSON-able body the endpoint
    returned, held for replay.  ``request_hash`` is the canonical hash of the
    material request; a later request under the same scope must match it or be
    refused.
    """

    id: str
    actor_id: str
    operation: str
    key: str
    request_hash: str
    response: dict[str, Any]
    created_at: datetime = field(default_factory=now_utc)


@dataclass
class IdempotencyRows:
    """The persisted row container shared by store instances.

    Records are keyed by ``(actor_id, operation, key)`` so two actors' identical
    keys live in disjoint namespaces — a cross-actor lookup can never become an
    existence oracle or a replay of a foreign result.
    """

    records: dict[tuple[str, str, str], IdempotencyRecord] = field(default_factory=dict)


class IdempotencyStore(Protocol):
    def run(
        self,
        actor: ConversationActor,
        operation: str,
        key: str,
        request_hash: str,
        executor: Callable[[], dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]: ...


class MemoryIdempotencyStore:
    """Hermetic row-backed idempotency store; requests are serialized."""

    def __init__(self, rows: IdempotencyRows | None = None) -> None:
        # Single-writer serialization for the in-memory backend: ``run`` executes
        # the wrapped operation under this reentrant lock so two concurrent
        # same-key requests cannot both execute — the first commits the record,
        # the second observes it and replays (the Postgres backend will use a
        # unique constraint plus a row lock instead).
        self._lock = threading.RLock()
        self.rows = rows if rows is not None else IdempotencyRows()

    @staticmethod
    def _require_actor(actor: ConversationActor) -> ConversationActor:
        if not isinstance(actor, ConversationActor):
            raise IdempotencyError("a store operation requires the acting ConversationActor")
        return actor

    @staticmethod
    def _require_key(key: str) -> str:
        if not isinstance(key, str):
            raise IdempotencyError("an idempotency key must be a string")
        stripped = key.strip()
        if not (MIN_KEY_CHARS <= len(stripped) <= MAX_KEY_CHARS):
            raise IdempotencyError("an idempotency key must be 1..200 characters")
        return stripped

    @staticmethod
    def _require_hash(request_hash: str) -> str:
        if not isinstance(request_hash, str) or not request_hash:
            raise IdempotencyError("a request hash is required to bind an idempotency key")
        return request_hash

    def run(
        self,
        actor: ConversationActor,
        operation: str,
        key: str,
        request_hash: str,
        executor: Callable[[], dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        """Execute ``executor`` at most once per ``(actor, operation, key)``.

        Returns ``(response, replayed)``.  On the first use for a scope the
        operation runs exactly once, its JSON-able result is persisted bound to
        ``request_hash``, and ``replayed`` is ``False``.  A later request in the
        same scope with the SAME hash returns the stored response with
        ``replayed`` True and never re-executes; with a DIFFERENT hash it raises
        :class:`IdempotencyConflictError`.  The whole check-execute-store runs
        under the instance lock, so concurrent same-key requests yield exactly
        one record.  If ``executor`` raises, nothing is stored and the error
        propagates, so a failed effect stays retriable.
        """
        self._require_actor(actor)
        if not isinstance(operation, str) or not operation:
            raise IdempotencyError("an idempotency operation name is required")
        key = self._require_key(key)
        request_hash = self._require_hash(request_hash)
        scope = (actor.actor_id, operation, key)
        existing = self.rows.records.get(scope)
        if existing is not None:
            if not secrets.compare_digest(existing.request_hash, request_hash):
                raise IdempotencyConflictError(
                    "idempotency key reused with a materially different payload"
                )
            return copy.deepcopy(existing.response), True
        response = executor()
        if not isinstance(response, dict):  # pragma: no cover - guards the contract
            raise IdempotencyError("an idempotent operation must return a JSON object")
        record = IdempotencyRecord(
            id=new_id("idem"),
            actor_id=actor.actor_id,
            operation=operation,
            key=key,
            request_hash=request_hash,
            response=copy.deepcopy(response),
        )
        self.rows.records[scope] = record
        return copy.deepcopy(response), False


def request_hash_for(operation: str, material: dict[str, Any]) -> str:
    """Canonical hash of one material request, reusing the delivery-path hash.

    ``material`` is the JSON-able slice of the request that must match on a
    retry (path identifiers and the validated body).  The operation name is
    folded in so an identical body under two different operations can never
    collide on one hash.
    """
    return payload_hash({"operation": operation, "material": material})


def _synchronize_memory_store() -> None:
    """Wrap every public MemoryIdempotencyStore method under its lock."""

    def _guard(method):
        @wraps(method)
        def _locked(self, *args, **kwargs):
            with self._lock:
                return method(self, *args, **kwargs)
        return _locked

    for _name in ("run",):
        setattr(MemoryIdempotencyStore, _name, _guard(getattr(MemoryIdempotencyStore, _name)))


_synchronize_memory_store()
