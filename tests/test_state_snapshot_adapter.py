"""Hermetic tests for the schema-versioned project-snapshot adapter.

Fixture payloads are built from the T001 contract example
(``docs/contracts/examples/anvil-state.project-snapshot.v1.json``) wrapped in
the real State CLI JSON envelope shape, and the pinned descriptor set comes
from the T002.1 catalog example.  No live State CLI is executed; the runner is
injected everywhere.
"""

from __future__ import annotations

import copy
import inspect
import json
import subprocess
from pathlib import Path

import pytest

from workbench import state_snapshot_adapter as adapter_module
from workbench.contracts import contract_digest
from workbench.state_manifest import (
    PROJECT_SNAPSHOT_OPERATION_ID,
    PinnedStateOperation,
    pin_state_read_operations,
)
from workbench.state_snapshot_adapter import (
    PublishableSnapshot,
    StateSnapshotAdapter,
    StateSnapshotError,
    validate_snapshot_payload,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CATALOG = ROOT / "docs" / "contracts" / "examples" / "anvil-state.catalog.v1.json"
EXAMPLE_SNAPSHOT = ROOT / "docs" / "contracts" / "examples" / "anvil-state.project-snapshot.v1.json"


def pinned_operations():
    return pin_state_read_operations(json.loads(EXAMPLE_CATALOG.read_text(encoding="utf-8")))


def example_snapshot() -> dict:
    return json.loads(EXAMPLE_SNAPSHOT.read_text(encoding="utf-8"))


def rehash(snapshot: dict) -> dict:
    """Recompute the digest after a deliberate fixture mutation.

    This isolates the semantic check under test from the digest check, which
    has its own dedicated drift test below.
    """
    snapshot["snapshot_digest"] = contract_digest("state-snapshot", snapshot)
    return snapshot


def envelope(payload: dict, prefix: str = "") -> str:
    return prefix + json.dumps({"ok": True, "command": "snapshot", "data": payload})


def adapter_for(
    output: str, command: str = "anvil snapshot --json",
) -> tuple[StateSnapshotAdapter, list[tuple[PinnedStateOperation, list[str]]]]:
    calls: list[tuple[PinnedStateOperation, list[str]]] = []

    def runner(operation: PinnedStateOperation, args) -> str:
        calls.append((operation, list(args)))
        return output

    return StateSnapshotAdapter(pinned_operations(), command, runner=runner), calls


def task_named(snapshot: dict, scoped_id: str) -> dict:
    return next(task for task in snapshot["tasks"] if task["scoped_id"] == scoped_id)


def test_happy_path_returns_bounded_publishable_snapshot() -> None:
    snapshot = example_snapshot()
    adapter, calls = adapter_for(envelope(snapshot, prefix="anvil workbench fixture\n"))

    result = adapter.fetch()

    assert isinstance(result, PublishableSnapshot)
    assert calls == [(pinned_operations().project_snapshot, ["anvil", "snapshot", "--json"])]
    assert result.provider == "anvil-state"
    assert result.schema_version == "workbench-state-snapshot/v1"
    assert result.snapshot_digest == snapshot["snapshot_digest"]
    assert result.operation_id == PROJECT_SNAPSHOT_OPERATION_ID
    assert result.operation_digest == pinned_operations().project_snapshot.operation_digest
    assert result.project_id == "project_example"
    assert result.payload == snapshot
    # Bounded summaries only: the payload carries titles and typed references,
    # never a PRD body/content field or Markdown-scale prose.
    for entry in result.payload["prds"] + result.payload["tasks"]:
        assert not {"body", "content", "markdown", "description"} & set(entry)
        assert len(entry["title"]) <= 500


def test_same_numbered_tasks_in_different_prds_stay_distinct() -> None:
    adapter, _calls = adapter_for(envelope(example_snapshot()))

    result = adapter.fetch()

    assert "release-alpha:T001" in result.scoped_task_ids
    assert "release-beta:T001" in result.scoped_task_ids
    assert len(set(result.scoped_task_ids)) == len(result.scoped_task_ids)
    refs = {
        (task["ref"]["prd_id"], task["ref"]["task_id"]) for task in result.payload["tasks"]
    }
    assert ("release-alpha", "T001") in refs and ("release-beta", "T001") in refs


def test_digest_drift_fails_before_publication() -> None:
    snapshot = example_snapshot()
    snapshot["prds"][0]["title"] += "!"  # deliberate drift, digest NOT recomputed
    adapter, _calls = adapter_for(envelope(snapshot))

    with pytest.raises(StateSnapshotError, match="digest mismatch"):
        adapter.fetch()


def test_task_referencing_an_unknown_prd_fails_closed() -> None:
    snapshot = example_snapshot()
    task = task_named(snapshot, "release-alpha:T001")
    task["ref"]["prd_id"] = "release-ghost"
    task["scoped_id"] = "release-ghost:T001"
    adapter, _calls = adapter_for(envelope(rehash(snapshot)))

    with pytest.raises(StateSnapshotError, match="unknown PRD"):
        adapter.fetch()


def test_spoofed_parent_dependency_fails_closed() -> None:
    snapshot = example_snapshot()
    dependency = task_named(snapshot, "release-beta:T002.2")["depends_on"][0]
    dependency["prd_id"] = "release-gamma"
    adapter, _calls = adapter_for(envelope(rehash(snapshot)))

    with pytest.raises(StateSnapshotError, match="absent from the snapshot"):
        adapter.fetch()


def test_scoped_id_that_contradicts_its_typed_reference_fails_closed() -> None:
    snapshot = example_snapshot()
    task_named(snapshot, "release-beta:T001")["scoped_id"] = "release-alpha:T001.1"
    adapter, _calls = adapter_for(envelope(rehash(snapshot)))

    with pytest.raises(StateSnapshotError, match="scoped_id does not match"):
        adapter.fetch()


def test_duplicate_task_reference_fails_closed() -> None:
    snapshot = example_snapshot()
    snapshot["tasks"].append(copy.deepcopy(task_named(snapshot, "release-alpha:T001")))
    adapter, _calls = adapter_for(envelope(rehash(snapshot)))

    with pytest.raises(StateSnapshotError, match="duplicate task reference"):
        adapter.fetch()


def test_incompatible_schema_version_fails_closed() -> None:
    snapshot = example_snapshot()
    snapshot["schema_version"] = "workbench-state-snapshot/v2"
    adapter, _calls = adapter_for(envelope(rehash(snapshot)))

    with pytest.raises(StateSnapshotError, match="contract"):
        adapter.fetch()


def test_undeclared_fields_cannot_smuggle_prd_markdown() -> None:
    top_level = example_snapshot()
    top_level["prd_markdown"] = "# Full PRD\n\nThousands of words of requirements..."
    adapter, _calls = adapter_for(envelope(rehash(top_level)))
    with pytest.raises(StateSnapshotError, match="contract"):
        adapter.fetch()

    task_level = example_snapshot()
    task_named(task_level, "release-alpha:T001")["body"] = "# Task spec\n\nFull Markdown body"
    adapter, _calls = adapter_for(envelope(rehash(task_level)))
    with pytest.raises(StateSnapshotError, match="contract"):
        adapter.fetch()

    prd_level = example_snapshot()
    prd_level["prds"][0]["content"] = "# Chat-first Workbench\n\nEntire PRD text"
    adapter, _calls = adapter_for(envelope(rehash(prd_level)))
    with pytest.raises(StateSnapshotError, match="contract"):
        adapter.fetch()


def test_oversize_prose_fails_the_schema_bound() -> None:
    snapshot = example_snapshot()
    task_named(snapshot, "release-alpha:T001")["title"] = "x" * 501
    adapter, _calls = adapter_for(envelope(rehash(snapshot)))

    with pytest.raises(StateSnapshotError, match="contract"):
        adapter.fetch()


def test_spoofed_source_provenance_fails_closed() -> None:
    wrong_operation = example_snapshot()
    wrong_operation["source"]["read_operation_id"] = "state.prd.read_content"
    adapter, _calls = adapter_for(envelope(rehash(wrong_operation)))
    with pytest.raises(StateSnapshotError, match="other than the pinned"):
        adapter.fetch()

    wrong_version = example_snapshot()
    wrong_version["source"]["provider_contract_version"] = "9.9.9"
    adapter, _calls = adapter_for(envelope(rehash(wrong_version)))
    with pytest.raises(StateSnapshotError, match="contract version"):
        adapter.fetch()


def test_adapter_uses_only_the_pinned_descriptor() -> None:
    adapter, calls = adapter_for(envelope(example_snapshot()))

    adapter.fetch()
    adapter.fetch()

    for operation, _args in calls:
        assert operation.operation_id == PROJECT_SNAPSHOT_OPERATION_ID
        assert operation.bridge_adapter == "state.cli.project_snapshot"
        assert operation.operation_digest == pinned_operations().project_snapshot.operation_digest
    # A caller-supplied operation id is impossible by construction: no public
    # method takes an operation parameter anywhere on the adapter surface.
    assert list(inspect.signature(StateSnapshotAdapter.fetch).parameters) == ["self"]
    init_parameters = inspect.signature(StateSnapshotAdapter.__init__).parameters
    assert "operation_id" not in init_parameters and "operation" not in init_parameters
    # The reference validator itself refuses a non-snapshot descriptor.
    with pytest.raises(StateSnapshotError, match="only executes"):
        validate_snapshot_payload(example_snapshot(), pinned_operations().prd_read_content)


def test_malformed_command_output_and_envelope_fail_closed() -> None:
    adapter, _calls = adapter_for("this is not a snapshot")
    with pytest.raises(StateSnapshotError, match="one JSON object"):
        adapter.fetch()

    refused, _calls = adapter_for(json.dumps({"ok": False, "command": "snapshot", "data": {}}))
    with pytest.raises(StateSnapshotError, match="did not report ok"):
        refused.fetch()

    dataless, _calls = adapter_for(json.dumps({"ok": True, "command": "snapshot", "data": "text"}))
    with pytest.raises(StateSnapshotError, match="no data object"):
        dataless.fetch()

    with pytest.raises(StateSnapshotError, match="not configured"):
        StateSnapshotAdapter(pinned_operations(), "   ")


def test_default_runner_executes_the_configured_cli_argv(monkeypatch, tmp_path: Path) -> None:
    observed: list[tuple[list[str], Path | None]] = []

    def fake_run(args, **kwargs):
        observed.append((list(args), kwargs["cwd"]))
        return subprocess.CompletedProcess(args, 0, envelope(example_snapshot()), "")

    monkeypatch.setattr(adapter_module.subprocess, "run", fake_run)
    adapter = StateSnapshotAdapter(
        pinned_operations(), "custom-state snapshot --json", cwd=tmp_path,
    )

    assert adapter.fetch().snapshot_digest == example_snapshot()["snapshot_digest"]
    assert observed == [(["custom-state", "snapshot", "--json"], tmp_path)]

    def failing_run(args, **_kwargs):
        return subprocess.CompletedProcess(args, 3, "", "state exploded")

    monkeypatch.setattr(adapter_module.subprocess, "run", failing_run)
    with pytest.raises(StateSnapshotError, match="state exploded"):
        StateSnapshotAdapter(pinned_operations(), "custom-state snapshot --json", cwd=tmp_path).fetch()


def test_publishable_snapshot_is_immutable() -> None:
    adapter, _calls = adapter_for(envelope(example_snapshot()))
    result = adapter.fetch()

    with pytest.raises((AttributeError, TypeError)):
        result.snapshot_digest = "sha256:" + "0" * 64  # type: ignore[misc]
    # The payload is exposed as a parsed copy; mutating it cannot alter the result.
    mutated = result.payload
    mutated["tasks"].clear()
    assert result.payload["tasks"], "payload accessor must return an untouched copy"


def test_generated_at_cannot_smuggle_markdown_scale_content() -> None:
    snapshot = example_snapshot()
    snapshot["generated_at"] = "# Full PRD\n\n" + ("Thousands of words of requirements. " * 3000)
    adapter, _calls = adapter_for(envelope(snapshot))

    with pytest.raises(StateSnapshotError, match="contract"):
        adapter.fetch()


def test_dangling_task_dependency_fails_closed() -> None:
    snapshot = example_snapshot()
    task_named(snapshot, "release-beta:T002.2")["depends_on"] = [
        {"prd_id": "release-beta", "task_id": "T999"}
    ]
    adapter, _calls = adapter_for(envelope(rehash(snapshot)))

    with pytest.raises(StateSnapshotError, match="absent from the snapshot"):
        adapter.fetch()


def test_self_dependency_fails_closed() -> None:
    snapshot = example_snapshot()
    task_named(snapshot, "release-beta:T002.2")["depends_on"] = [
        {"prd_id": "release-beta", "task_id": "T002.2"}
    ]
    adapter, _calls = adapter_for(envelope(rehash(snapshot)))

    with pytest.raises(StateSnapshotError, match="depend on itself"):
        adapter.fetch()


def test_missing_contract_schema_file_fails_closed(monkeypatch, tmp_path: Path) -> None:
    adapter_module._reset_snapshot_contract_validator_cache()
    monkeypatch.setattr(
        adapter_module, "_SNAPSHOT_CONTRACT_SCHEMA", tmp_path / "absent.schema.json"
    )
    adapter, _calls = adapter_for(envelope(example_snapshot()))
    with pytest.raises(StateSnapshotError, match="schema is unavailable"):
        adapter.fetch()
    adapter_module._reset_snapshot_contract_validator_cache()


def test_drifted_open_or_unbounded_schema_fails_closed(monkeypatch, tmp_path: Path) -> None:
    base = json.loads(
        (ROOT / "docs" / "contracts" / "schemas" / "state-snapshot.v1.schema.json").read_text(encoding="utf-8")
    )
    opened = copy.deepcopy(base)
    del opened["$defs"]["taskRef"]["additionalProperties"]
    drifted = tmp_path / "drifted.schema.json"
    drifted.write_text(json.dumps(opened), encoding="utf-8")
    adapter_module._reset_snapshot_contract_validator_cache()
    monkeypatch.setattr(adapter_module, "_SNAPSHOT_CONTRACT_SCHEMA", drifted)
    adapter, _calls = adapter_for(envelope(example_snapshot()))
    with pytest.raises(StateSnapshotError, match="no longer closes its objects"):
        adapter.fetch()

    unbounded = copy.deepcopy(base)
    del unbounded["properties"]["tasks"]["items"]["properties"]["title"]["maxLength"]
    drifted.write_text(json.dumps(unbounded), encoding="utf-8")
    adapter_module._reset_snapshot_contract_validator_cache()
    with pytest.raises(StateSnapshotError, match="no longer bounds its prose"):
        adapter.fetch()
    adapter_module._reset_snapshot_contract_validator_cache()


def test_oversize_validation_error_is_truncated() -> None:
    snapshot = example_snapshot()
    snapshot["tasks"][0]["title"] = "SECRETISH " * 2000
    adapter, _calls = adapter_for(envelope(rehash(snapshot)))
    with pytest.raises(StateSnapshotError) as excinfo:
        adapter.fetch()
    assert len(str(excinfo.value)) < 800
