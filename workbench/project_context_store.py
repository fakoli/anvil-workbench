"""Project-scoped, idempotent store for context display projections.

This module persists :class:`~workbench.project_context.ProjectContextProjection`
records — the explicitly non-canonical display read-model derived by T003.1 —
for one project at a time.  It is the hub-side persistence slice that keeps a
project's *latest* display projection addressable while preserving the source
attribution of every projection it has ever held.

Authority boundary (AGENTS.md): Anvil State remains canonical for project
lifecycle.  Everything this store holds is a display derivative.  Persisting or
reading a projection grants no claim, lease, evidence, or effect, and the store
never mints an authority digest of its own — it keys on the projection's own
``source_digest`` (the pinned snapshot attribution).

Project scoping is a hard boundary.  Every operation takes the acting project
scope and touches only that project's namespace:

* ``publish`` is idempotent by ``(project_id, source_digest)`` — re-publishing an
  identical digest returns the already-stored record and creates no duplicate.
* A projection carrying a *newer* ``source_revision`` supersedes the acting
  project's latest display projection: the latest pointer moves, but the prior
  projection stays addressable by its own digest so historical source
  attribution is never rewritten.
* A new distinct digest supersedes the latest when it is *at least as recent*
  by source revision -- a strictly-newer revision, or an equal revision
  carrying different content (e.g. a task-status flip that bumps no PRD
  revision). Only a strictly-LOWER revision under a new digest fails closed and
  never clobbers the latest.
* A cross-project publish, read, or overwrite is refused with the same
  ``UnknownProjectionError`` a genuinely missing record raises, so one project
  can never learn whether another project's projection exists — the indistinct
  not-found mirrors :mod:`workbench.conversation_store`'s cross-actor probe.

``MemoryProjectContextStore`` is the hermetic row-backed implementation in the
``MemoryStore`` idiom: the persisted values are frozen projection dataclasses
and the row containers can be handed to a fresh instance to simulate a hub
restart over the same persisted records.  Every public method runs under a
reentrant instance lock, matching the conversation store's single-writer
serialization discipline (a production backend uses row-level transactions
instead).
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from functools import wraps
from typing import Protocol

from .project_context import ProjectContextProjection
from .store import StoreError

_PROJECT_ID = re.compile(r"^[a-zA-Z0-9._-]{1,128}$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_UNKNOWN_PROJECTION = "unknown projection"


class ProjectContextStoreError(StoreError):
    """A project-context store operation violates its scoping/idempotency contract."""


class UnknownProjectionError(ProjectContextStoreError):
    """No such projection for this project.

    Raised identically for a genuinely missing digest and for another project's
    projection, so a cross-project probe cannot learn whether the record exists.
    """


class StaleProjectionError(ProjectContextStoreError):
    """A new-digest publish would not advance the acting project's latest.

    A projection supersedes when it carries a source revision at least as recent
    as the current latest (a strictly-newer revision, or an equal revision under
    a different digest).  Only a strictly-LOWER revision under a new digest fails
    closed rather than clobbering the latest display projection.
    """


@dataclass
class ProjectContextRows:
    """The persisted row containers shared by store instances.

    ``projections`` maps ``project_id -> {source_digest -> projection}`` (every
    projection this project has held, addressable by its own digest).
    ``latest`` maps ``project_id -> source_digest`` of the current display
    projection.  Handing the same rows to a fresh
    :class:`MemoryProjectContextStore` simulates a hub restart over the same
    persisted records.
    """

    projections: dict[str, dict[str, ProjectContextProjection]] = field(default_factory=dict)
    latest: dict[str, str] = field(default_factory=dict)


class ProjectContextStore(Protocol):
    def publish(self, acting_project_id: str, projection: ProjectContextProjection) -> ProjectContextProjection: ...
    def get_latest(self, acting_project_id: str) -> ProjectContextProjection: ...
    def get(self, acting_project_id: str, source_digest: str) -> ProjectContextProjection: ...


class MemoryProjectContextStore:
    """Hermetic row-backed project-context store; requests are serialized."""

    def __init__(self, rows: ProjectContextRows | None = None) -> None:
        # Single-writer serialization for the in-memory backend: every public
        # method runs under this reentrant lock so concurrent threadpool
        # requests cannot interleave a mutation (a durable backend will use
        # row-level transactions instead).
        self._lock = threading.RLock()
        self.rows = rows if rows is not None else ProjectContextRows()

    # -- internal helpers -------------------------------------------------

    @staticmethod
    def _require_scope(acting_project_id: str) -> str:
        """Validate and return the acting project scope."""
        if not isinstance(acting_project_id, str) or not _PROJECT_ID.match(acting_project_id):
            raise ProjectContextStoreError("a store operation requires a valid acting project scope")
        return acting_project_id

    def _namespace(self, project_id: str) -> dict[str, ProjectContextProjection]:
        return self.rows.projections.setdefault(project_id, {})

    # -- publish (idempotent by (project_id, source_digest)) --------------

    def publish(self, acting_project_id: str, projection: ProjectContextProjection) -> ProjectContextProjection:
        """Persist ``projection`` into the acting project's namespace.

        Idempotent by ``(project_id, source_digest)``: re-publishing an
        identical digest returns the already-stored record and creates no
        duplicate (criterion 1).  A projection carrying a source revision at
        least as recent as the current latest (strictly newer, or equal under a
        different digest) supersedes the acting project's latest display
        projection while leaving every prior projection addressable by its own
        digest (criterion 2, and historical attribution is never rewritten).  A
        projection whose ``project_id`` differs from the acting scope is refused
        with the indistinct not-found, so one project can neither publish into
        nor overwrite another's namespace (criterion 3).
        """
        scope = self._require_scope(acting_project_id)
        if not isinstance(projection, ProjectContextProjection):
            raise ProjectContextStoreError("publish requires a ProjectContextProjection")
        # Cross-project publish/overwrite is indistinguishable from not-found:
        # the acting scope may only write into its own namespace.
        if projection.project_id != scope:
            raise UnknownProjectionError(_UNKNOWN_PROJECTION)

        revision = projection.source_revision
        if revision < 1:
            raise ProjectContextStoreError("a published projection must reflect a source revision")

        digest = projection.source_digest
        namespace = self._namespace(scope)

        existing = namespace.get(digest)
        if existing is not None:
            # Idempotent republish: the digest is a content commitment, so an
            # identical digest must present the identical projection.  A
            # mismatch is a forged/colliding digest and fails closed.
            if existing != projection:
                raise ProjectContextStoreError("a re-published digest does not match the stored projection")
            return existing

        latest_digest = self.rows.latest.get(scope)
        if latest_digest is not None:
            latest = namespace[latest_digest]
            # A new distinct digest supersedes when it is at least as recent as
            # the latest by source revision: a strictly-newer revision, or an
            # equal revision carrying different content (e.g. a task-status
            # flip that bumps no PRD revision) legitimately refreshes the
            # display.  A strictly-LOWER revision is stale and must not clobber
            # the current latest.
            if revision < latest.source_revision:
                raise StaleProjectionError(
                    "a new projection must be at least as recent as the latest source revision to supersede it"
                )

        namespace[digest] = projection
        # The prior projection stays addressable by its digest; only the latest
        # pointer moves to the freshly superseding record.
        self.rows.latest[scope] = digest
        return projection

    # -- reads ------------------------------------------------------------

    def get_latest(self, acting_project_id: str) -> ProjectContextProjection:
        """Return the acting project's current latest display projection."""
        scope = self._require_scope(acting_project_id)
        latest_digest = self.rows.latest.get(scope)
        namespace = self.rows.projections.get(scope)
        if latest_digest is None or namespace is None or latest_digest not in namespace:
            raise UnknownProjectionError(_UNKNOWN_PROJECTION)
        return namespace[latest_digest]

    def get(self, acting_project_id: str, source_digest: str) -> ProjectContextProjection:
        """Return one of the acting project's projections by its source digest.

        A digest belonging to another project is not in this project's
        namespace, so a cross-project read returns the indistinct not-found.
        """
        scope = self._require_scope(acting_project_id)
        if not isinstance(source_digest, str) or not _DIGEST.match(source_digest):
            raise ProjectContextStoreError("source_digest is invalid")
        namespace = self.rows.projections.get(scope)
        if namespace is None or source_digest not in namespace:
            raise UnknownProjectionError(_UNKNOWN_PROJECTION)
        return namespace[source_digest]


def _synchronize_memory_store() -> None:
    """Wrap every public MemoryProjectContextStore method under its instance lock."""

    def _guard(method):
        @wraps(method)
        def _locked(self, *args, **kwargs):
            with self._lock:
                return method(self, *args, **kwargs)
        return _locked

    for _name in ("publish", "get_latest", "get"):
        setattr(MemoryProjectContextStore, _name, _guard(getattr(MemoryProjectContextStore, _name)))


_synchronize_memory_store()
