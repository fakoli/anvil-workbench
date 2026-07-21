"""Integrate-and-qualify the derived project-context projection end to end.

state-context-operations:T003 -- the capstone fixture for feature F002's
display-projection path.  Unlike the focused unit suites, this exercises the
WHOLE chain from a validated State snapshot through to a browser response:

    State snapshot fixture
        -> validate_snapshot_payload  (workbench.state_snapshot_adapter)
        -> ProjectContextProjection.from_snapshot  (workbench.project_context)
        -> MemoryProjectContextStore.publish  (workbench.project_context_store)
        -> GET /api/projects/{id}/context  (workbench.api)

Everything is hermetic and derived from the checked-in snapshot example; no
live State CLI runs.  The projection remains a display read-model that is NOT
wired into the live bridge poll loop -- this fixture proves the integrated
read/publish/render path, not live activation.

Acceptance-criterion map:

* Criterion 1 (publish a State-derived hierarchy, read it through the API,
  preserve scoped PRD/feature/task identity + source revision + digest):
  ``test_state_derived_hierarchy_round_trips_through_the_browser_api``.
* Criterion 2 (identical digest idempotent; newer revision supersedes only the
  owning project's latest):
  ``test_republish_is_idempotent_and_newer_revision_supersedes_only_owner``.
* Criterion 3 (cross-project publish/read/overwrite fail closed):
  ``test_cross_project_publish_read_and_overwrite_fail_closed``.
* Criterion 4 (rendered responses stay explicitly non-canonical, no prohibited
  fields): ``test_rendered_response_is_non_canonical_and_leaks_nothing``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workbench.api import create_app
from workbench.config import Settings
from workbench.contracts import contract_digest
from workbench.graph import NullGraph
from workbench.project_context import ProjectContextProjection
from workbench.project_context_store import (
    MemoryProjectContextStore,
    StaleProjectionError,
    UnknownProjectionError,
)
from workbench.state_manifest import pin_state_read_operations
from workbench.state_snapshot_adapter import validate_snapshot_payload
from workbench.store import MemoryStore

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CATALOG = ROOT / "docs" / "contracts" / "examples" / "anvil-state.catalog.v1.json"
EXAMPLE_SNAPSHOT = ROOT / "docs" / "contracts" / "examples" / "anvil-state.project-snapshot.v1.json"

ACTOR = {"X-Workbench-Actor": "operator"}


def _snapshot_operation():
    return pin_state_read_operations(json.loads(EXAMPLE_CATALOG.read_text(encoding="utf-8"))).project_snapshot


def derive(project_id: str | None = None, *, revision_bump: int = 0) -> ProjectContextProjection:
    """Derive a projection from the snapshot fixture, end to end.

    ``project_id`` re-homes the hierarchy under a different project; each
    ``revision_bump`` raises every PRD's revision so the projection's source
    revision (its max PRD revision) advances and a fresh snapshot digest is
    minted -- a realistic newer-snapshot input for the supersession path.
    """
    payload = json.loads(EXAMPLE_SNAPSHOT.read_text(encoding="utf-8"))
    if project_id is not None:
        payload["project"]["project_id"] = project_id
    if revision_bump:
        for prd in payload["prds"]:
            prd["revision"] += revision_bump
    payload["snapshot_digest"] = contract_digest("state-snapshot", payload)
    snapshot = validate_snapshot_payload(payload, _snapshot_operation())
    return ProjectContextProjection.from_snapshot(snapshot)


def client(store: MemoryProjectContextStore) -> TestClient:
    settings = Settings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator"}), bridge_bootstrap_token="",
        anvil_router_base_url="", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=True,
    )
    return TestClient(create_app(
        settings=settings, store=MemoryStore(), graph=NullGraph(), project_context_store=store,
    ))


def test_state_derived_hierarchy_round_trips_through_the_browser_api():
    """Criterion 1: publish a State-derived hierarchy and read it back intact."""
    store = MemoryProjectContextStore()
    projection = derive()  # project_example, from the real snapshot
    store.publish(projection.project_id, projection)

    with client(store) as api:
        context = api.get(
            f"/api/projects/{projection.project_id}/context", headers=ACTOR,
        ).json()["context"]

    # Scoped PRD, feature, and task identity survive the whole chain.
    assert {p["scoped_id"] for p in context["prds"]} == {"release-alpha", "release-beta"}
    assert {f["scoped_id"] for f in context["features"]} == {
        "release-alpha:F001", "release-beta:F001", "release-beta:F002",
    }
    assert {t["scoped_id"] for t in context["tasks"]} == {
        "release-alpha:T001", "release-beta:T001", "release-beta:T002.2",
    }

    # Source revision + digest are preserved and attributed per summary.
    assert context["source_digest"] == projection.source_digest
    alpha = next(p for p in context["prds"] if p["scoped_id"] == "release-alpha")
    beta_task = next(t for t in context["tasks"] if t["scoped_id"] == "release-beta:T002.2")
    assert alpha["source_revision"] == 4  # release-alpha PRD revision
    assert beta_task["source_revision"] == 5  # inherits owning release-beta PRD revision
    assert beta_task["owning_prd_id"] == "release-beta"
    for summary in context["prds"] + context["features"] + context["tasks"]:
        assert summary["source_digest"] == projection.source_digest


def test_republish_is_idempotent_and_newer_revision_supersedes_only_owner():
    """Criterion 2: idempotent republish; a newer revision supersedes one owner."""
    store = MemoryProjectContextStore()
    a_v1 = derive("project_a")
    b_v1 = derive("project_b")
    store.publish("project_a", a_v1)
    store.publish("project_b", b_v1)

    # Re-publishing the identical digest is idempotent: same stored record, no
    # duplicate row.
    again = store.publish("project_a", derive("project_a"))
    assert again is store.get("project_a", a_v1.source_digest)
    assert list(store.rows.projections["project_a"]) == [a_v1.source_digest]

    # A newer revision (all PRDs bumped -> higher max revision, new digest)
    # supersedes project_a's latest only.
    a_v2 = derive("project_a", revision_bump=1)
    assert a_v2.source_revision > a_v1.source_revision
    assert a_v2.source_digest != a_v1.source_digest
    store.publish("project_a", a_v2)

    with client(store) as api:
        latest_a = api.get("/api/projects/project_a/context", headers=ACTOR).json()["context"]
        latest_b = api.get("/api/projects/project_b/context", headers=ACTOR).json()["context"]
        # The prior projection stays addressable by its own digest through the API.
        prior_a = api.get(
            f"/api/projects/project_a/context/{a_v1.source_digest}", headers=ACTOR,
        )

    assert latest_a["source_digest"] == a_v2.source_digest
    # project_b's latest is entirely untouched by project_a's supersession.
    assert latest_b["source_digest"] == b_v1.source_digest
    assert prior_a.status_code == 200
    assert prior_a.json()["context"]["source_digest"] == a_v1.source_digest


def test_cross_project_publish_read_and_overwrite_fail_closed():
    """Criterion 3: cross-project publish, read, and overwrite all fail closed."""
    store = MemoryProjectContextStore()
    store.publish("project_b", derive("project_b"))

    # Publish: acting as project_a, a projection owned by project_b is refused
    # with the indistinct not-found; project_a's namespace is never created.
    with pytest.raises(UnknownProjectionError):
        store.publish("project_a", derive("project_b", revision_bump=5))
    assert "project_a" not in store.rows.projections

    # Overwrite: a same-project higher revision under a foreign acting scope is
    # likewise refused (the acting scope only writes its own namespace).
    b_latest = store.get_latest("project_b")
    with pytest.raises(UnknownProjectionError):
        store.publish("intruder", derive("project_b", revision_bump=9))
    assert store.get_latest("project_b") is b_latest

    # Read: project_b's real digest is unreadable under any other project scope,
    # at the store and through the API, indistinguishable from a missing record.
    foreign_digest = b_latest.source_digest
    with pytest.raises(UnknownProjectionError):
        store.get("project_a", foreign_digest)
    with client(store) as api:
        foreign = api.get(f"/api/projects/project_a/context/{foreign_digest}", headers=ACTOR)
        missing = api.get("/api/projects/project_a/context/sha256:" + "0" * 64, headers=ACTOR)
        assert foreign.status_code == missing.status_code == 404
        assert foreign.json() == missing.json()

    # Only a STRICTLY-LOWER revision under a new digest fails closed; an equal
    # revision under a new digest legitimately supersedes (see the store's
    # inline rule at project_context_store.py). Here revision_bump=2 sets the
    # latest, so the un-bumped derive() is strictly lower and never clobbers it.
    store_stale = MemoryProjectContextStore()
    store_stale.publish("project_a", derive("project_a", revision_bump=2))
    with pytest.raises(StaleProjectionError):
        store_stale.publish("project_a", derive("project_a"))


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


def test_rendered_response_is_non_canonical_and_leaks_nothing():
    """Criterion 4: rendered response is non-canonical and carries no leak."""
    store = MemoryProjectContextStore()
    projection = derive()
    store.publish(projection.project_id, projection)
    with client(store) as api:
        context = api.get(
            f"/api/projects/{projection.project_id}/context", headers=ACTOR,
        ).json()["context"]

    assert context["canonical"] is False and context["non_canonical"] is True
    for summary in context["prds"] + context["features"] + context["tasks"]:
        assert summary["non_canonical"] is True

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
