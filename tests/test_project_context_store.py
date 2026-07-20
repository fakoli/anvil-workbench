"""Hermetic tests for the project-scoped context-projection store.

The store persists the T003.1 :class:`ProjectContextProjection` display
read-model.  Each binding acceptance criterion is mapped to a proving test:

* Criterion 1 (re-publishing an identical digest creates no duplicate and
  returns the existing projection):
  ``test_republishing_identical_digest_is_idempotent``.
* Criterion 2 (a newer source revision supersedes only the owning project's
  latest display projection):
  ``test_newer_revision_supersedes_only_this_project`` — asserts a second
  project is left untouched — and ``test_supersede_preserves_historical_attribution``.
* Criterion 3 (Project A cannot publish, read, or overwrite Project B's
  projection): ``test_cross_project_publish_is_refused_non_leaking``,
  ``test_cross_project_read_is_indistinct_from_missing``.

Fail-closed and discipline coverage:
``test_stale_or_equal_revision_never_clobbers_latest``,
``test_restart_over_same_rows_preserves_latest_and_history``,
``test_public_methods_are_synchronized``.
"""

from __future__ import annotations

import hashlib
import threading

import pytest

from workbench.project_context import (
    PROJECT_CONTEXT_SCHEMA_VERSION,
    PrdSummary,
    ProjectContextProjection,
)
from workbench.project_context_store import (
    MemoryProjectContextStore,
    ProjectContextRows,
    ProjectContextStoreError,
    StaleProjectionError,
    UnknownProjectionError,
)


def _digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def make_projection(
    project_id: str,
    *,
    revision: int,
    seed: str | None = None,
    project_name: str = "Example Project",
) -> ProjectContextProjection:
    """A minimal valid projection for one project at a given source revision.

    The digest is derived from ``seed`` (default: project + revision) so a
    re-published identical projection reuses its digest while a fresh revision
    mints a new one.
    """
    digest = _digest(seed if seed is not None else f"{project_id}:{revision}")
    prd = PrdSummary(
        project_id=project_id,
        scoped_id="release-alpha",
        title="Ship the thing",
        status="active",
        source_revision=revision,
        source_digest=digest,
    )
    return ProjectContextProjection(
        schema_version=PROJECT_CONTEXT_SCHEMA_VERSION,
        source_provider="anvil-state",
        source_schema_version="workbench-state-snapshot/v1",
        source_digest=digest,
        project_id=project_id,
        project_name=project_name,
        prds=(prd,),
        features=(),
        tasks=(),
    )


def test_publish_then_get_latest_and_get_by_digest_round_trip() -> None:
    store = MemoryProjectContextStore()
    projection = make_projection("project_a", revision=1)

    published = store.publish("project_a", projection)

    assert published == projection
    assert store.get_latest("project_a") == projection
    assert store.get("project_a", projection.source_digest) == projection


def test_republishing_identical_digest_is_idempotent() -> None:
    # Criterion 1: an identical digest creates no duplicate and returns the
    # already-stored record.
    store = MemoryProjectContextStore()
    projection = make_projection("project_a", revision=1)

    first = store.publish("project_a", projection)
    second = store.publish("project_a", make_projection("project_a", revision=1))

    assert first is second  # the stored record is returned, not a new one
    assert list(store.rows.projections["project_a"]) == [projection.source_digest]
    assert len(store.rows.projections["project_a"]) == 1


def test_republishing_a_digest_with_mismatched_content_fails_closed() -> None:
    # A digest is a content commitment; a colliding/forged digest carrying
    # different content must not silently overwrite the stored projection.
    store = MemoryProjectContextStore()
    original = make_projection("project_a", revision=1)
    store.publish("project_a", original)

    forged = make_projection("project_a", revision=1, project_name="Different Name")
    assert forged.source_digest == original.source_digest  # same seed -> same digest

    with pytest.raises(ProjectContextStoreError):
        store.publish("project_a", forged)
    assert store.get("project_a", original.source_digest) == original


def test_newer_revision_supersedes_only_this_project() -> None:
    # Criterion 2: a newer revision moves this project's latest pointer and
    # leaves any other project entirely untouched.
    store = MemoryProjectContextStore()
    a_v1 = make_projection("project_a", revision=1)
    b_v1 = make_projection("project_b", revision=1)
    store.publish("project_a", a_v1)
    store.publish("project_b", b_v1)

    a_v2 = make_projection("project_a", revision=2)
    store.publish("project_a", a_v2)

    assert store.get_latest("project_a") == a_v2
    # Project B's latest is byte-for-byte and object-identically unchanged.
    assert store.get_latest("project_b") is b_v1
    assert store.rows.latest["project_b"] == b_v1.source_digest
    assert list(store.rows.projections["project_b"]) == [b_v1.source_digest]


def test_supersede_preserves_historical_attribution() -> None:
    # Criterion 2 / historical attribution: superseding creates a new latest;
    # the prior projection stays addressable by digest with its original
    # source_revision and digest intact.
    store = MemoryProjectContextStore()
    v1 = make_projection("project_a", revision=1)
    store.publish("project_a", v1)
    v2 = make_projection("project_a", revision=2)
    store.publish("project_a", v2)

    prior = store.get("project_a", v1.source_digest)
    assert prior == v1
    assert prior.source_revision == 1
    assert prior.source_digest == v1.source_digest
    assert store.get_latest("project_a") == v2


def test_stale_or_equal_revision_never_clobbers_latest() -> None:
    store = MemoryProjectContextStore()
    v2 = make_projection("project_a", revision=2)
    store.publish("project_a", v2)

    # A lower revision under a new digest is refused, latest unchanged.
    stale = make_projection("project_a", revision=1, seed="late-arrival")
    with pytest.raises(StaleProjectionError):
        store.publish("project_a", stale)
    assert store.get_latest("project_a") == v2

    # An equal revision under a different digest also does not supersede.
    equal = make_projection("project_a", revision=2, seed="different-digest-same-rev")
    with pytest.raises(StaleProjectionError):
        store.publish("project_a", equal)
    assert store.get_latest("project_a") == v2
    assert list(store.rows.projections["project_a"]) == [v2.source_digest]


def test_cross_project_publish_is_refused_non_leaking() -> None:
    # Criterion 3: acting as project_a, a projection owned by project_b cannot
    # be published/overwritten; the refusal is the indistinct not-found and
    # project_b's namespace is untouched.
    store = MemoryProjectContextStore()
    b_v1 = make_projection("project_b", revision=1)
    store.publish("project_b", b_v1)

    intruder = make_projection("project_b", revision=2, seed="intruder")
    with pytest.raises(UnknownProjectionError):
        store.publish("project_a", intruder)

    assert store.get_latest("project_b") is b_v1
    assert list(store.rows.projections["project_b"]) == [b_v1.source_digest]
    assert "project_a" not in store.rows.projections


def test_cross_project_read_is_indistinct_from_missing() -> None:
    # Criterion 3: a digest owned by project_b is not in project_a's namespace,
    # so reading it as project_a is byte-identical to reading a missing record.
    store = MemoryProjectContextStore()
    b_v1 = make_projection("project_b", revision=1)
    store.publish("project_b", b_v1)

    with pytest.raises(UnknownProjectionError) as missing:
        store.get("project_a", _digest("never-published"))
    with pytest.raises(UnknownProjectionError) as foreign:
        store.get("project_a", b_v1.source_digest)
    assert str(missing.value) == str(foreign.value)

    with pytest.raises(UnknownProjectionError):
        store.get_latest("project_a")


def test_invalid_scope_and_digest_are_rejected() -> None:
    store = MemoryProjectContextStore()
    with pytest.raises(ProjectContextStoreError):
        store.get_latest("has/slash")
    with pytest.raises(ProjectContextStoreError):
        store.get_latest("")
    with pytest.raises(ProjectContextStoreError):
        store.get("project_a", "not-a-digest")


def test_restart_over_same_rows_preserves_latest_and_history() -> None:
    rows = ProjectContextRows()
    store = MemoryProjectContextStore(rows)
    v1 = make_projection("project_a", revision=1)
    v2 = make_projection("project_a", revision=2)
    store.publish("project_a", v1)
    store.publish("project_a", v2)

    # A fresh instance over the same rows simulates a hub restart.
    reopened = MemoryProjectContextStore(rows)
    assert reopened.get_latest("project_a") == v2
    assert reopened.get("project_a", v1.source_digest) == v1


def test_public_methods_are_synchronized() -> None:
    # The reentrant-lock discipline is present and every public method is
    # wrapped under it.
    store = MemoryProjectContextStore()
    assert isinstance(store._lock, type(threading.RLock()))
    for name in ("publish", "get_latest", "get"):
        assert hasattr(getattr(MemoryProjectContextStore, name), "__wrapped__")

    # Concurrent idempotent republishes serialize to exactly one stored record.
    projection = make_projection("project_a", revision=1)
    results: list[ProjectContextProjection] = []
    barrier = threading.Barrier(8)

    def _worker() -> None:
        barrier.wait()
        results.append(store.publish("project_a", make_projection("project_a", revision=1)))

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(store.rows.projections["project_a"]) == 1
    assert all(result == projection for result in results)
