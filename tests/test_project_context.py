"""Hermetic tests for the derived project-context display projection.

The projection is built from a real :class:`PublishableSnapshot` validated out
of the checked-in T001 contract example
(``docs/contracts/examples/anvil-state.project-snapshot.v1.json``), so the shape
is grounded in the actual snapshot contract and no live State CLI is executed.

Each acceptance criterion is mapped:

* Criterion 1 (readable summaries, non-canonical labeling, scoped identifiers,
  source revision/digest, project ownership):
  ``test_projection_carries_readable_attributed_non_canonical_summaries``.
* Criterion 2 (serialization round-trips without losing source attribution):
  ``test_serialization_round_trips_without_losing_attribution``.
* Criterion 3 (no serialized field can expose a State path, credential, token,
  or raw executable provider payload):
  ``test_no_serialized_field_exposes_forbidden_markers`` and
  ``test_undeclared_fields_cannot_be_reconstructed``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from workbench.project_context import (
    PROJECT_CONTEXT_SCHEMA_VERSION,
    FeatureSummary,
    PrdSummary,
    ProjectContextError,
    ProjectContextProjection,
    TaskSummary,
)
from workbench.state_manifest import pin_state_read_operations
from workbench.state_snapshot_adapter import PublishableSnapshot, validate_snapshot_payload

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CATALOG = ROOT / "docs" / "contracts" / "examples" / "anvil-state.catalog.v1.json"
EXAMPLE_SNAPSHOT = ROOT / "docs" / "contracts" / "examples" / "anvil-state.project-snapshot.v1.json"


def pinned_snapshot_operation():
    catalog = json.loads(EXAMPLE_CATALOG.read_text(encoding="utf-8"))
    return pin_state_read_operations(catalog).project_snapshot


def publishable_snapshot() -> PublishableSnapshot:
    payload = json.loads(EXAMPLE_SNAPSHOT.read_text(encoding="utf-8"))
    return validate_snapshot_payload(payload, pinned_snapshot_operation())


def projection() -> ProjectContextProjection:
    return ProjectContextProjection.from_snapshot(publishable_snapshot())


def test_from_snapshot_grounds_the_projection_in_the_validated_snapshot() -> None:
    snapshot = publishable_snapshot()
    proj = ProjectContextProjection.from_snapshot(snapshot)

    assert proj.schema_version == PROJECT_CONTEXT_SCHEMA_VERSION
    assert proj.source_provider == "anvil-state"
    assert proj.source_schema_version == "workbench-state-snapshot/v1"
    assert proj.source_digest == snapshot.snapshot_digest
    assert proj.project_id == snapshot.project_id == "project_example"
    assert proj.project_name == "Anvil Workbench"
    # Every source record is projected: 2 PRDs, 3 tasks, and the distinct
    # feature groupings derived from the tasks' feature_id references.
    assert {p.scoped_id for p in proj.prds} == {"release-alpha", "release-beta"}
    assert {t.scoped_id for t in proj.tasks} == {
        "release-alpha:T001", "release-beta:T001", "release-beta:T002.2",
    }
    assert {f.scoped_id for f in proj.features} == {
        "release-alpha:F001", "release-beta:F001", "release-beta:F002",
    }


def test_projection_carries_readable_attributed_non_canonical_summaries() -> None:
    """Criterion 1."""
    proj = projection()

    # Non-canonical labeling, at the projection level and on every summary.
    assert proj.canonical is False and proj.non_canonical is True
    for summary in (*proj.prds, *proj.features, *proj.tasks):
        assert summary.non_canonical is True
        # Scoped identifier + source digest + project ownership form the key.
        assert summary.key == (proj.project_id, summary.source_kind, summary.scoped_id, proj.source_digest)
        assert summary.project_id == proj.project_id
        assert summary.source_digest == proj.source_digest
        assert summary.source_kind in {"prd", "feature", "task"}
        # Source revision attribution is a positive integer everywhere.
        assert isinstance(summary.source_revision, int) and summary.source_revision >= 1

    # Readable titles survive on the summaries that have them in the snapshot.
    alpha_prd = next(p for p in proj.prds if p.scoped_id == "release-alpha")
    assert alpha_prd.title == "Chat-first Workbench"
    assert alpha_prd.source_revision == 4  # PRD's own revision
    assert alpha_prd.content_trust == "untrusted_task_data"

    beta_task = next(t for t in proj.tasks if t.scoped_id == "release-beta:T002.2")
    assert beta_task.title == "Implement the schema-versioned project-snapshot adapter"
    assert beta_task.owning_prd_id == "release-beta"
    assert beta_task.source_revision == 5  # inherits owning PRD (release-beta) revision
    assert beta_task.priority == "critical"
    assert beta_task.content_trust == "untrusted_task_data"


def test_same_numbered_tasks_in_different_prds_stay_distinct() -> None:
    proj = projection()
    keys = proj.summary_keys
    assert len(set(keys)) == len(keys), "summary keys must be unique"
    task_scoped = {t.scoped_id for t in proj.tasks}
    assert "release-alpha:T001" in task_scoped and "release-beta:T001" in task_scoped


def test_feature_summaries_are_derived_not_invented() -> None:
    proj = projection()
    beta_f002 = next(f for f in proj.features if f.scoped_id == "release-beta:F002")
    assert beta_f002.owning_prd_id == "release-beta"
    assert beta_f002.source_revision == 5
    assert beta_f002.task_count == 1
    # A feature summary deliberately carries no fabricated free-text title.
    assert not hasattr(beta_f002, "title")


def test_serialization_round_trips_without_losing_attribution() -> None:
    """Criterion 2."""
    proj = projection()
    payload = proj.as_dict()

    # A parse-back reconstructs an equal projection: no attribution is lost.
    reparsed = ProjectContextProjection.from_dict(payload)
    assert reparsed == proj
    assert reparsed.as_dict() == payload

    # Survives a JSON encode/decode boundary unchanged (deterministic display shape).
    assert ProjectContextProjection.from_dict(json.loads(json.dumps(payload))) == proj

    # Field-level attribution is explicitly present in the serialized form.
    for entry in payload["prds"] + payload["features"] + payload["tasks"]:
        assert entry["project_id"] == proj.project_id
        assert entry["source_digest"] == proj.source_digest
        assert entry["source_revision"] >= 1
        assert entry["source_kind"] in {"prd", "feature", "task"}
        assert entry["non_canonical"] is True
    assert payload["canonical"] is False and payload["non_canonical"] is True
    # to_display_dict is the same display serialization.
    assert proj.to_display_dict() == payload


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


def test_no_serialized_field_exposes_forbidden_markers() -> None:
    """Criterion 3: scan the full serialized output for forbidden markers.

    Word-shaped markers (adapter/command/token/route...) are scanned against
    field NAMES, because free-text titles legitimately contain such substrings
    ("...project-snapshot adapter", "Add routed chat"). Concrete injection
    strings (state.db, path separators, a URL scheme) are scanned against every
    serialized value — no readable title in a real snapshot carries them.
    """
    payload = projection().as_dict()
    keys: list[str] = []
    strings: list[str] = []
    _walk(payload, keys, strings)

    # No serialized KEY names a State-storage, credential, or execution surface.
    forbidden_key_markers = (
        "state", "sqlite", "journal", "wal", "shm", "path", "mount", "db",
        "token", "secret", "api_key", "apikey", "password", "credential", "bearer",
        "adapter", "command", "argv", "execute", "endpoint", "route", "provider_catalog",
    )
    for key in keys:
        lowered = key.lower()
        for marker in forbidden_key_markers:
            assert marker not in lowered, f"serialized field name {key!r} looks like a {marker!r} surface"

    # No serialized VALUE carries a concrete storage path, workspace, or URL.
    for value in strings:
        lowered = value.lower()
        for marker in ("state.db", ".anvil", "-wal", "-shm", "://"):
            assert marker not in lowered, f"serialized value {value!r} leaked injection marker {marker!r}"

    # Identifier and title fields specifically carry no path separators.
    def check_ids(entry: dict) -> None:
        for field_name in ("project_id", "scoped_id", "owning_prd_id", "source_provider", "title", "project_name"):
            v = entry.get(field_name)
            if isinstance(v, str):
                assert "/" not in v and "\\" not in v, f"{field_name} carries a path separator"

    check_ids(payload)
    for entry in payload["prds"] + payload["features"] + payload["tasks"]:
        check_ids(entry)


def test_undeclared_fields_cannot_be_reconstructed() -> None:
    """Criterion 3: the closed field set refuses smuggled fields on parse-back."""
    payload = projection().as_dict()

    smuggled_top = copy.deepcopy(payload)
    smuggled_top["state_db_path"] = "/var/anvil/state.db"
    with pytest.raises(ProjectContextError, match="undeclared fields"):
        ProjectContextProjection.from_dict(smuggled_top)

    smuggled_task = copy.deepcopy(payload)
    smuggled_task["tasks"][0]["command"] = "rm -rf /"
    with pytest.raises(ProjectContextError, match="undeclared fields"):
        ProjectContextProjection.from_dict(smuggled_task)

    smuggled_prd = copy.deepcopy(payload)
    smuggled_prd["prds"][0]["api_key"] = "sk-secret"
    with pytest.raises(ProjectContextError, match="undeclared fields"):
        ProjectContextProjection.from_dict(smuggled_prd)


def test_projection_cannot_be_marked_canonical() -> None:
    proj = projection()
    with pytest.raises(ProjectContextError, match="never canonical"):
        ProjectContextProjection.from_dict({**proj.as_dict(), "canonical": True})
    with pytest.raises(ProjectContextError, match="non-canonical"):
        ProjectContextProjection.from_dict({**proj.as_dict(), "non_canonical": False})


def test_frozen_summaries_and_deep_copy_isolation() -> None:
    proj = projection()

    # Frozen dataclasses reject in-place mutation.
    with pytest.raises((AttributeError, TypeError)):
        proj.source_digest = "sha256:" + "0" * 64  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        proj.prds[0].title = "rewritten"  # type: ignore[misc]

    # as_dict returns a fresh structure; mutating it cannot alter the projection.
    payload = proj.as_dict()
    payload["tasks"].clear()
    payload["project_name"] = "hijacked"
    assert proj.as_dict()["tasks"], "as_dict must return an independent copy"
    assert proj.project_name == "Anvil Workbench"

    # A deep copy is equal but independent.
    clone = copy.deepcopy(proj)
    assert clone == proj and clone is not proj


def test_summary_construction_rejects_forbidden_shapes() -> None:
    valid_digest = "sha256:" + "a" * 64

    # A scoped id can never smuggle a path separator (pattern-closed).
    with pytest.raises(ProjectContextError, match="scoped_id"):
        PrdSummary(
            project_id="project_example",
            scoped_id="../etc/passwd",
            title="x",
            status="approved",
            source_revision=1,
            source_digest=valid_digest,
        )
    # A task scoped id must be owned by its owning_prd_id.
    with pytest.raises(ProjectContextError, match="owned by owning_prd_id"):
        TaskSummary(
            project_id="project_example",
            scoped_id="release-alpha:T001",
            title="x",
            status="ready",
            owning_prd_id="release-beta",
            source_revision=1,
            source_digest=valid_digest,
        )
    # A display summary is always non-canonical.
    with pytest.raises(ProjectContextError, match="non-canonical"):
        FeatureSummary(
            project_id="project_example",
            scoped_id="release-alpha:F001",
            owning_prd_id="release-alpha",
            task_count=1,
            source_revision=1,
            source_digest=valid_digest,
            non_canonical=False,
        )


def test_feature_spanning_multiple_prds_fails_closed() -> None:
    snapshot_payload = json.loads(EXAMPLE_SNAPSHOT.read_text(encoding="utf-8"))
    # Reuse one feature_id across tasks owned by two different PRDs.
    snapshot_payload["tasks"][0]["feature_id"] = "shared-feature"
    snapshot_payload["tasks"][1]["feature_id"] = "shared-feature"
    from workbench.contracts import contract_digest

    snapshot_payload["snapshot_digest"] = contract_digest("state-snapshot", snapshot_payload)
    snapshot = validate_snapshot_payload(snapshot_payload, pinned_snapshot_operation())
    with pytest.raises(ProjectContextError, match="spans more than one owning PRD"):
        ProjectContextProjection.from_snapshot(snapshot)


def test_secrets_in_untrusted_display_text_are_scrubbed_on_the_last_hop() -> None:
    from workbench.contracts import contract_digest

    payload = json.loads(EXAMPLE_SNAPSHOT.read_text(encoding="utf-8"))
    payload["project"]["name"] = "Bearer sk-live-abc123DEADBEEF secret project"
    payload["tasks"][0]["title"] = "Fix token=supersecretvalue in the api_key=leak path"
    payload["snapshot_digest"] = contract_digest("state-snapshot", payload)
    snapshot = validate_snapshot_payload(payload, pinned_snapshot_operation())

    serialized = json.dumps(ProjectContextProjection.from_snapshot(snapshot).as_dict())
    for leaked in ("sk-live-abc123DEADBEEF", "supersecretvalue", "Bearer sk-live"):
        assert leaked not in serialized
    assert "[REDACTED]" in serialized
