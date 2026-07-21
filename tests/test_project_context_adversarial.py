"""Adversarial qualification of the project-context projection slice.

state-context-operations:T003.4 -- prove, from a hostile posture, the three
binding properties of the derived project-context read path across its store
and its browser surface:

* Criterion 1 (idempotency + owning-project-only supersession):
  ``test_identical_digest_is_idempotent_and_creates_no_duplicate``,
  ``test_forged_digest_collision_fails_closed``,
  ``test_newer_revision_supersedes_only_the_owning_project``,
  ``test_stale_lower_revision_never_clobbers_latest``,
  ``test_concurrent_publishes_serialize_without_corruption``.
* Criterion 2 (cross-project publish / read / overwrite fail closed, no oracle):
  ``test_cross_project_publish_is_refused_non_leaking``,
  ``test_cross_project_overwrite_cannot_touch_a_foreign_namespace``,
  ``test_cross_project_read_is_indistinct_from_missing_at_store_and_api``.
* Criterion 3 (browser responses keep scoped duplicate identities, no
  prohibited fields):
  ``test_api_preserves_scoped_duplicate_task_identities``,
  ``test_api_response_and_error_bodies_carry_no_prohibited_fields``.

The store-level matrix uses synthetic revision-controlled projections; the
API-level checks use a projection derived from the checked-in State snapshot
fixture so the rendered hierarchy is realistic.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workbench.api import create_app
from workbench.config import Settings
from workbench.graph import NullGraph
from workbench.project_context import (
    PROJECT_CONTEXT_SCHEMA_VERSION,
    PrdSummary,
    ProjectContextProjection,
)
from workbench.project_context_store import (
    MemoryProjectContextStore,
    ProjectContextStoreError,
    StaleProjectionError,
    UnknownProjectionError,
)
from workbench.state_manifest import pin_state_read_operations
from workbench.state_snapshot_adapter import validate_snapshot_payload
from workbench.store import MemoryStore

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CATALOG = ROOT / "docs" / "contracts" / "examples" / "anvil-state.catalog.v1.json"
EXAMPLE_SNAPSHOT = ROOT / "docs" / "contracts" / "examples" / "anvil-state.project-snapshot.v1.json"


# --- synthetic revision-controlled projections (store matrix) ---------------


def _digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def synthetic(project_id: str, *, revision: int, seed: str | None = None, name: str = "Example") -> ProjectContextProjection:
    digest = _digest(seed if seed is not None else f"{project_id}:{revision}")
    prd = PrdSummary(
        project_id=project_id, scoped_id="release-alpha", title="Ship the thing",
        status="active", source_revision=revision, source_digest=digest,
    )
    return ProjectContextProjection(
        schema_version=PROJECT_CONTEXT_SCHEMA_VERSION, source_provider="anvil-state",
        source_schema_version="workbench-state-snapshot/v1", source_digest=digest,
        project_id=project_id, project_name=name, prds=(prd,), features=(), tasks=(),
    )


# --- snapshot-derived projection (API surface) ------------------------------


def snapshot_projection(project_id: str | None = None) -> ProjectContextProjection:
    from workbench.contracts import contract_digest

    operation = pin_state_read_operations(json.loads(EXAMPLE_CATALOG.read_text(encoding="utf-8"))).project_snapshot
    payload = json.loads(EXAMPLE_SNAPSHOT.read_text(encoding="utf-8"))
    if project_id is not None:
        payload["project"]["project_id"] = project_id
        payload["snapshot_digest"] = contract_digest("state-snapshot", payload)
    return ProjectContextProjection.from_snapshot(validate_snapshot_payload(payload, operation))


def api_client(store: MemoryProjectContextStore) -> TestClient:
    settings = Settings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator"}), bridge_bootstrap_token="",
        anvil_router_base_url="", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=True,
    )
    return TestClient(create_app(
        settings=settings, store=MemoryStore(), graph=NullGraph(), project_context_store=store,
    ))


ACTOR = {"X-Workbench-Actor": "operator"}


# --- Criterion 1: idempotency + owning-project-only supersession ------------


def test_identical_digest_is_idempotent_and_creates_no_duplicate():
    store = MemoryProjectContextStore()
    first = store.publish("project_a", synthetic("project_a", revision=1))
    second = store.publish("project_a", synthetic("project_a", revision=1))
    assert first is second
    assert list(store.rows.projections["project_a"]) == [first.source_digest]


def test_forged_digest_collision_fails_closed():
    # Same digest seed, different content: a colliding/forged digest must not
    # silently overwrite the committed projection.
    store = MemoryProjectContextStore()
    original = synthetic("project_a", revision=1, seed="collide")
    store.publish("project_a", original)
    forged = synthetic("project_a", revision=1, seed="collide", name="Different")
    assert forged.source_digest == original.source_digest
    with pytest.raises(ProjectContextStoreError):
        store.publish("project_a", forged)
    assert store.get("project_a", original.source_digest) == original


def test_newer_revision_supersedes_only_the_owning_project():
    store = MemoryProjectContextStore()
    a1 = store.publish("project_a", synthetic("project_a", revision=1))
    b1 = store.publish("project_b", synthetic("project_b", revision=1))
    a2 = store.publish("project_a", synthetic("project_a", revision=2))

    assert store.get_latest("project_a") == a2
    # project_b is byte-for-byte and object-identically untouched.
    assert store.get_latest("project_b") is b1
    assert list(store.rows.projections["project_b"]) == [b1.source_digest]
    # a1 stays addressable by its own digest -- historical attribution intact.
    assert store.get("project_a", a1.source_digest) == a1


def test_stale_lower_revision_never_clobbers_latest():
    store = MemoryProjectContextStore()
    v2 = store.publish("project_a", synthetic("project_a", revision=2))
    with pytest.raises(StaleProjectionError):
        store.publish("project_a", synthetic("project_a", revision=1, seed="late"))
    assert store.get_latest("project_a") == v2
    # An equal-revision, different-digest refresh is legitimate (e.g. a status
    # flip that bumps no PRD revision) and moves the latest pointer.
    refreshed = store.publish("project_a", synthetic("project_a", revision=2, seed="refresh"))
    assert store.get_latest("project_a") == refreshed


def test_concurrent_publishes_serialize_without_corruption():
    # This contests the store's check-then-act in publish() -- the
    # ``existing = namespace.get(digest)`` guard read at
    # project_context_store.py just before ``namespace[digest] = projection`` --
    # under maximally aggressive thread switching (setswitchinterval(1e-6)) with
    # a Barrier releasing every racer at once.
    #
    # The prior version of this test raced N DISTINCT digests, which cannot
    # detect lost serialization: distinct keys never contend, so any GIL-atomic
    # interleaving trivially satisfies "all stored". Here we race the SAME
    # digest, which is where the guard actually matters:
    #   (a) N racers publishing the IDENTICAL projection must converge to exactly
    #       one stored record with every caller getting an equal record back.
    #   (b) a forged same-digest/DIFFERENT-content projection racing the honest
    #       one must resolve to exactly one stored winner and one fail-closed
    #       digest-collision error -- never both stored, never a silent overwrite.
    # With the instance lock present both are deterministically correct on every
    # round; with the lock disabled, round (b) loses its fail-closed guarantee
    # (both racers slip past the guard and one silently overwrites the other),
    # so this test genuinely exercises the serialization it asserts.
    previous_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        # (a) Identical projection, N racers -> one record, no duplicate.
        for _ in range(8):
            store = MemoryProjectContextStore()
            count = 16
            identical = synthetic("project_a", revision=1, seed="same")
            barrier = threading.Barrier(count)
            returned: list[ProjectContextProjection] = []
            errors: list[Exception] = []
            collect = threading.Lock()

            def _same_worker() -> None:
                barrier.wait()
                try:
                    result = store.publish("project_a", identical)
                except Exception as exc:  # noqa: BLE001
                    with collect:
                        errors.append(exc)
                else:
                    with collect:
                        returned.append(result)

            threads = [threading.Thread(target=_same_worker) for _ in range(count)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert errors == []
            assert len(returned) == count
            assert all(r == identical for r in returned)  # every caller: same record
            stored = store.rows.projections["project_a"]
            assert list(stored) == [identical.source_digest]  # exactly one, no duplicate
            assert store.get_latest("project_a") == identical

        # (b) Forged same-digest/different-content racing the honest projection.
        # Many threads per round split across the two contents so at least two
        # racers collide in the guard window under aggressive switching. Honest
        # and forged share a digest but differ in content; whichever content the
        # single stored record ends up holding, EVERY successful return must
        # equal that stored record and every other racer must fail closed with
        # the collision error. Without the lock a racer can write-and-return its
        # own content only to be silently overwritten -- leaving a "winner" whose
        # content disagrees with storage -- which the invariants below catch.
        per_content = 16
        for _ in range(60):
            store = MemoryProjectContextStore()
            honest = synthetic("project_a", revision=1, seed="collide", name="Honest")
            forged = synthetic("project_a", revision=1, seed="collide", name="Forged")
            assert honest.source_digest == forged.source_digest
            assert honest != forged

            racers = [honest] * per_content + [forged] * per_content
            barrier = threading.Barrier(len(racers))
            winners: list[ProjectContextProjection] = []
            failures: list[Exception] = []
            collect = threading.Lock()

            def _race(projection: ProjectContextProjection) -> None:
                barrier.wait()
                try:
                    result = store.publish("project_a", projection)
                except ProjectContextStoreError as exc:
                    with collect:
                        failures.append(exc)
                else:
                    with collect:
                        winners.append(result)

            threads = [threading.Thread(target=_race, args=(p,)) for p in racers]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # Exactly one record is stored, and it is exactly one of the two
            # contents (never a corrupted blend).
            stored = store.rows.projections["project_a"]
            assert len(stored) == 1
            stored_proj = next(iter(stored.values()))
            assert stored_proj in (honest, forged)
            # No return was lost, and every successful return equals what is
            # actually stored -- a silent overwrite would leave a winner that
            # disagrees with storage.
            assert len(winners) + len(failures) == len(racers)
            assert all(w == stored_proj for w in winners), "a winner disagrees with the stored record"
            # The losing content's racers all fail closed with the collision
            # error; the winning content's racers all idempotently succeed.
            expected_failures = sum(1 for p in racers if p != stored_proj)
            assert len(failures) == expected_failures
            assert all(isinstance(f, ProjectContextStoreError) for f in failures)
            assert store.get_latest("project_a") == stored_proj
    finally:
        sys.setswitchinterval(previous_interval)


# --- Criterion 2: cross-project publish / read / overwrite fail closed -------


def test_cross_project_publish_is_refused_non_leaking():
    store = MemoryProjectContextStore()
    store.publish("project_b", synthetic("project_b", revision=1))
    intruder = synthetic("project_b", revision=2, seed="intruder")
    # Acting as project_a, a projection OWNED by project_b cannot be published.
    with pytest.raises(UnknownProjectionError):
        store.publish("project_a", intruder)
    assert "project_a" not in store.rows.projections
    assert list(store.rows.projections["project_b"]) == [
        synthetic("project_b", revision=1).source_digest
    ]


def test_cross_project_overwrite_cannot_touch_a_foreign_namespace():
    store = MemoryProjectContextStore()
    b1 = store.publish("project_b", synthetic("project_b", revision=1))
    # Even a higher-revision projection owned by project_b, published while
    # acting as project_a, is refused -- the acting scope writes only its own
    # namespace, so project_b's latest is unchanged.
    with pytest.raises(UnknownProjectionError):
        store.publish("project_a", synthetic("project_b", revision=99, seed="overwrite"))
    assert store.get_latest("project_b") is b1


def test_cross_project_read_is_indistinct_from_missing_at_store_and_api():
    store = MemoryProjectContextStore()
    projection = snapshot_projection()  # owned by "project_example"
    store.publish(projection.project_id, projection)

    # Store level: a foreign digest and a never-published digest raise the same
    # error with the same message.
    with pytest.raises(UnknownProjectionError) as foreign:
        store.get("intruder", projection.source_digest)
    with pytest.raises(UnknownProjectionError) as missing:
        store.get("intruder", _digest("never"))
    assert str(foreign.value) == str(missing.value)

    # API level: byte-identical 404 bodies for foreign vs. never-published.
    # Compare raw response bytes (not parsed JSON) so the assertion proves the
    # "byte-identical" claim -- a foreign digest, a never-published digest, and a
    # missing latest are indistinguishable down to the serialized bytes.
    with api_client(store) as client:
        a = client.get(f"/api/projects/intruder/context/{projection.source_digest}", headers=ACTOR)
        b = client.get("/api/projects/intruder/context/sha256:" + "0" * 64, headers=ACTOR)
        c = client.get("/api/projects/intruder/context", headers=ACTOR)
        assert a.status_code == b.status_code == c.status_code == 404
        assert a.content == b.content == c.content


# --- Criterion 3: browser responses -- duplicate identity + no prohibited fields


def test_api_preserves_scoped_duplicate_task_identities():
    store = MemoryProjectContextStore()
    projection = snapshot_projection()
    store.publish(projection.project_id, projection)
    with api_client(store) as client:
        context = client.get(
            f"/api/projects/{projection.project_id}/context", headers=ACTOR,
        ).json()["context"]
        task_ids = [t["scoped_id"] for t in context["tasks"]]
        # T001 exists under both release-alpha and release-beta and stays distinct.
        assert task_ids.count("release-alpha:T001") == 1
        assert task_ids.count("release-beta:T001") == 1
        assert len(task_ids) == len(set(task_ids))
        # The projection and every summary are explicitly non-canonical.
        assert context["canonical"] is False and context["non_canonical"] is True
        assert all(t["non_canonical"] is True for t in context["tasks"])


def _walk(value, keys: list[str], strings: list[str]) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            keys.append(key)
            _walk(nested, keys, strings)
    elif isinstance(value, list):
        for nested in value:
            _walk(nested, keys, strings)
    elif isinstance(value, str):
        strings.append(value)


def test_api_response_and_error_bodies_carry_no_prohibited_fields():
    store = MemoryProjectContextStore()
    projection = snapshot_projection()
    store.publish(projection.project_id, projection)
    with api_client(store) as client:
        context = client.get(
            f"/api/projects/{projection.project_id}/context", headers=ACTOR,
        ).json()["context"]
        keys: list[str] = []
        strings: list[str] = []
        _walk(context, keys, strings)

        forbidden_key_markers = (
            "state", "sqlite", "journal", "wal", "shm", "path", "mount", "db",
            "token", "secret", "api_key", "apikey", "password", "credential", "bearer",
            "adapter", "command", "argv", "execute", "endpoint", "route", "provider_catalog",
        )
        for key in keys:
            lowered = key.lower()
            for marker in forbidden_key_markers:
                assert marker not in lowered, f"response field {key!r} looks like a {marker!r} surface"
        # Value scan: proves the projection does not SPLICE a State-internal path
        # into any rendered value (the fixture prose is path-free). It is not a
        # path-scrubbing guarantee for user-chosen prose -- display strings are
        # served as-is apart from credential scrubbing.
        for value in strings:
            lowered = value.lower()
            for marker in ("state.db", ".anvil", "-wal", "-shm", "://"):
                assert marker not in lowered, f"response value {value!r} leaked {marker!r}"

        # The non-leaking 404 body itself carries nothing beyond a fixed detail.
        not_found = client.get("/api/projects/ghost/context", headers=ACTOR).json()
        assert set(not_found) == {"detail"}
        assert "state" not in not_found["detail"].lower()
