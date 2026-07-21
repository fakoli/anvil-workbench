"""Project-scoped, immutable store for queue-time run-context snapshots.

This module persists :class:`~workbench.models.RunContext` records — the
frozen, immutable run context captured for one delivery run BEFORE the bridge
is dispatched (state-context-operations:T005.2).  It is the hub-side
persistence slice that makes a run's queue-time context durably readable
without ever rewriting it when the underlying task, PRD, catalog, route, or
skill later changes.

Authority boundary (AGENTS.md): Anvil State remains canonical for project
lifecycle.  A persisted run context is a supervision record, not authority:
storing or reading it grants no claim, lease, evidence, or effect.

Immutability is the load-bearing invariant.  A run context is captured once per
``(project_id, run_id)`` and is thereafter frozen:

* ``capture`` is idempotent by ``(project_id, run_id)`` for a byte-identical
  context — re-capturing the identical snapshot returns the stored record.
* Re-capturing a DIFFERENT context for a run that already has one fails closed
  with :class:`RunContextImmutableError`: a later task/PRD/catalog/route/skill
  change can never rewrite the stored queue-time snapshot.
* A cross-project read or capture is refused with the same
  :class:`UnknownRunContextError` a genuinely missing record raises, so one
  project can never learn whether another project's run context exists — the
  indistinct not-found mirrors :mod:`workbench.project_context_store`.

The capture-before-dispatch ordering is enforced by
:func:`dispatch_with_run_context`: the run context is resolved and persisted
first, and the bridge dispatch callable runs ONLY after a successful persist.
A failure to resolve any required context field (a :class:`RunContextError`
from :meth:`RunContext.capture`) or to persist it prevents the dispatch from
ever being invoked.

``MemoryRunContextStore`` is the hermetic row-backed implementation in the
``MemoryStore`` idiom; every public method runs under a reentrant instance lock,
matching the project-context store's single-writer serialization (a production
backend uses row-level transactions instead).
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from functools import wraps
from typing import Callable, Protocol

from .models import RunContext
from .store import StoreError

_PROJECT_ID = re.compile(r"^[a-zA-Z0-9._-]{1,128}$")
_UNKNOWN_RUN_CONTEXT = "unknown run context"


class RunContextStoreError(StoreError):
    """A run-context store operation violates its scoping/immutability contract."""


class UnknownRunContextError(RunContextStoreError):
    """No such run context for this project.

    Raised identically for a genuinely missing run and for another project's
    run, so a cross-project probe cannot learn whether the record exists.
    """


class RunContextImmutableError(RunContextStoreError):
    """A run already has a captured context and a different one would rewrite it.

    The queue-time snapshot is immutable: once captured for a run, a later
    task/PRD/catalog/route/skill change that would produce a different context
    must fail closed rather than overwrite the stored record.
    """


@dataclass
class RunContextRows:
    """The persisted row containers shared by store instances.

    ``contexts`` maps ``project_id -> {run_id -> RunContext}``.  Handing the
    same rows to a fresh :class:`MemoryRunContextStore` simulates a hub restart
    over the same persisted records.
    """

    contexts: dict[str, dict[str, RunContext]] = field(default_factory=dict)


class RunContextStore(Protocol):
    def capture(self, acting_project_id: str, run_context: RunContext) -> RunContext: ...
    def get(self, acting_project_id: str, run_id: str) -> RunContext: ...


class MemoryRunContextStore:
    """Hermetic row-backed run-context store; requests are serialized."""

    def __init__(self, rows: RunContextRows | None = None) -> None:
        self._lock = threading.RLock()
        self.rows = rows if rows is not None else RunContextRows()

    @staticmethod
    def _require_scope(acting_project_id: str) -> str:
        if not isinstance(acting_project_id, str) or not _PROJECT_ID.match(acting_project_id):
            raise RunContextStoreError("a store operation requires a valid acting project scope")
        return acting_project_id

    def capture(self, acting_project_id: str, run_context: RunContext) -> RunContext:
        """Persist ``run_context`` into the acting project's namespace.

        Idempotent by ``(project_id, run_id)`` for a byte-identical context; a
        DIFFERENT context for a run that already has one fails closed with
        :class:`RunContextImmutableError` so the queue-time snapshot is never
        rewritten.  The stored value is the frozen record itself (its
        serialization holds only scalar copies), so a later change to any source
        document cannot reinterpret it.
        """
        scope = self._require_scope(acting_project_id)
        if not isinstance(run_context, RunContext):
            raise RunContextStoreError("capture requires a RunContext")
        run_id = run_context.run_id
        namespace = self.rows.contexts.setdefault(scope, {})
        existing = namespace.get(run_id)
        if existing is not None:
            # The queue-time snapshot is immutable. An identical re-capture is a
            # harmless idempotent retry; a differing one is a rewrite attempt and
            # fails closed. Compare on the deterministic serialization.
            if existing.as_dict() != run_context.as_dict():
                raise RunContextImmutableError(
                    "a run context is already captured for this run and cannot be rewritten"
                )
            return existing
        namespace[run_id] = run_context
        return run_context

    def get(self, acting_project_id: str, run_id: str) -> RunContext:
        """Return the acting project's captured run context by run id.

        A run belonging to another project is not in this project's namespace,
        so a cross-project read returns the indistinct not-found.
        """
        scope = self._require_scope(acting_project_id)
        if not isinstance(run_id, str) or not run_id:
            raise RunContextStoreError("run_id is invalid")
        namespace = self.rows.contexts.get(scope)
        if namespace is None or run_id not in namespace:
            raise UnknownRunContextError(_UNKNOWN_RUN_CONTEXT)
        return namespace[run_id]


def dispatch_with_run_context(
    store: RunContextStore,
    acting_project_id: str,
    build_run_context: Callable[[], RunContext],
    dispatch: Callable[[RunContext], object],
) -> RunContext:
    """Resolve, persist, then dispatch — in that strict order.

    ``build_run_context`` resolves the complete run context (its component
    constructors raise :class:`~workbench.models.RunContextError` if any
    required authority or human-readable field is unresolved).  The context is
    then persisted with :meth:`RunContextStore.capture`.  Only after a
    SUCCESSFUL persist is ``dispatch`` invoked with the stored context.  A
    failure in either the resolve or the persist step raises before ``dispatch``
    is ever called, so no bridge invocation can begin without a complete,
    durably-stored queue-time run context (T005.2 criteria 1 and 2).
    """
    run_context = build_run_context()
    persisted = store.capture(acting_project_id, run_context)
    dispatch(persisted)
    return persisted


def _synchronize_memory_store() -> None:
    """Wrap every public MemoryRunContextStore method under its instance lock."""

    def _guard(method):
        @wraps(method)
        def _locked(self, *args, **kwargs):
            with self._lock:
                return method(self, *args, **kwargs)
        return _locked

    for _name in ("capture", "get"):
        setattr(MemoryRunContextStore, _name, _guard(getattr(MemoryRunContextStore, _name)))


_synchronize_memory_store()
