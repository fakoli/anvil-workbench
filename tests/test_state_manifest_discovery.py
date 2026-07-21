"""Hermetic tests for State read-operation discovery and pinning.

Fixture manifests are built from the T001 contract example
(``docs/contracts/examples/anvil-state.catalog.v1.json``) wrapped in the real
``anvil describe`` JSON envelope shape.  No live State CLI is executed; the
runner is injected everywhere.
"""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from workbench import state_manifest as state_manifest_module
from workbench.contracts import contract_digest
from workbench.state_manifest import (
    PRD_READ_CONTENT_OPERATION_ID,
    PROJECT_SNAPSHOT_OPERATION_ID,
    StateManifestDiscovery,
    StateManifestError,
    pin_state_read_operations,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CATALOG = ROOT / "docs" / "contracts" / "examples" / "anvil-state.catalog.v1.json"


def example_catalog() -> dict[str, object]:
    return json.loads(EXAMPLE_CATALOG.read_text(encoding="utf-8"))


def rehash(catalog: dict) -> dict:
    """Recompute digests after a deliberate fixture mutation.

    This isolates the semantic check under test from the digest check, which
    has its own dedicated drift test below.
    """
    for operation in catalog["operations"]:
        operation["operation_digest"] = contract_digest("operation", operation)
    catalog["catalog_digest"] = contract_digest("catalog", catalog)
    return catalog


def envelope(catalog: dict, prefix: str = "") -> str:
    return prefix + json.dumps({"ok": True, "command": "describe", "data": catalog})


def discovery_for(output: str, command: str = "anvil describe") -> tuple[StateManifestDiscovery, list[list[str]]]:
    calls: list[list[str]] = []

    def runner(args) -> str:
        calls.append(list(args))
        return output

    return StateManifestDiscovery(command, runner=runner), calls


def operation_named(catalog: dict, operation_id: str) -> dict:
    return next(item for item in catalog["operations"] if item["id"] == operation_id)


def test_happy_path_pins_both_read_operations_with_digests() -> None:
    catalog = example_catalog()
    discovery, calls = discovery_for(envelope(catalog, prefix="anvil workbench fixture\n"))

    pinned = discovery.pinned()

    assert calls == [["anvil", "describe"]]
    assert pinned.provider == "anvil-state"
    assert pinned.catalog_digest == catalog["catalog_digest"]
    assert pinned.project_snapshot.operation_id == PROJECT_SNAPSHOT_OPERATION_ID
    assert pinned.prd_read_content.operation_id == PRD_READ_CONTENT_OPERATION_ID
    for pinned_operation in pinned.operations:
        source = operation_named(catalog, pinned_operation.operation_id)
        assert pinned_operation.effect == "read"
        assert pinned_operation.contract_version == source["contract_version"]
        assert pinned_operation.operation_digest == source["operation_digest"]
        assert pinned_operation.bridge_adapter == source["execution"]["bridge_adapter"]
        assert pinned_operation.input_schema == source["input_schema"]
        assert pinned_operation.output_schema == source["output_schema"]
    assert pinned.prd_read_content.input_schema["required"] == ["prd_id"]


def test_downstream_calls_reuse_the_pinned_descriptors_without_rediscovery() -> None:
    discovery, calls = discovery_for(envelope(example_catalog()))

    first = discovery.pinned()
    assert discovery.pinned() is first
    assert first.descriptor(PROJECT_SNAPSHOT_OPERATION_ID) is first.project_snapshot
    assert first.descriptor(PRD_READ_CONTENT_OPERATION_ID) is first.prd_read_content
    assert calls == [["anvil", "describe"]], "descriptor reuse must not rediscover"


def test_descriptor_rejects_a_selected_operation_outside_the_pinned_read_set() -> None:
    discovery, _calls = discovery_for(envelope(example_catalog()))
    pinned = discovery.pinned()

    # A browser/model may name any catalog operation; only the pinned read
    # set resolves.  state.task.claim exists in the manifest but is a
    # state_mutation and was never pinned.
    with pytest.raises(StateManifestError, match="not pinned"):
        pinned.descriptor("state.task.claim")
    with pytest.raises(StateManifestError, match="not pinned"):
        pinned.descriptor("state.db.read")


def test_missing_read_operation_fails_before_activation() -> None:
    catalog = example_catalog()
    catalog["operations"] = [
        item for item in catalog["operations"] if item["id"] != PRD_READ_CONTENT_OPERATION_ID
    ]
    discovery, _calls = discovery_for(envelope(rehash(catalog)))

    with pytest.raises(StateManifestError, match="missing required read operation"):
        discovery.pinned()


def test_non_read_effect_class_fails_closed() -> None:
    catalog = example_catalog()
    operation_named(catalog, PROJECT_SNAPSHOT_OPERATION_ID)["effect"] = "state_mutation"
    discovery, _calls = discovery_for(envelope(rehash(catalog)))

    with pytest.raises(StateManifestError, match="read effect class"):
        discovery.pinned()


def test_incompatible_contract_major_fails_closed() -> None:
    catalog = example_catalog()
    operation_named(catalog, PRD_READ_CONTENT_OPERATION_ID)["contract_version"] = "2.0.0"
    discovery, _calls = discovery_for(envelope(rehash(catalog)))

    with pytest.raises(StateManifestError, match="incompatible contract major"):
        discovery.pinned()

    malformed = example_catalog()
    operation_named(malformed, PRD_READ_CONTENT_OPERATION_ID)["contract_version"] = "latest"
    with pytest.raises(StateManifestError, match="operation-catalog contract"):
        pin_state_read_operations({"ok": True, "command": "describe", "data": rehash(malformed)})


def test_digest_drift_fails_closed_before_any_semantic_check() -> None:
    catalog = example_catalog()
    operation_named(catalog, PROJECT_SNAPSHOT_OPERATION_ID)["summary"] += "!"
    discovery, _calls = discovery_for(envelope(catalog))

    with pytest.raises(StateManifestError, match="digest validation"):
        discovery.pinned()


def test_invalid_or_non_object_schema_fails_closed() -> None:
    broken = example_catalog()
    operation_named(broken, PRD_READ_CONTENT_OPERATION_ID)["input_schema"]["properties"]["prd_id"]["type"] = 123
    with pytest.raises(StateManifestError, match="draft 2020-12"):
        pin_state_read_operations({"ok": True, "command": "describe", "data": rehash(broken)})

    untyped = example_catalog()
    operation_named(untyped, PROJECT_SNAPSHOT_OPERATION_ID)["output_schema"] = {"type": "string"}
    with pytest.raises(StateManifestError, match="typed object schema"):
        pin_state_read_operations({"ok": True, "command": "describe", "data": rehash(untyped)})

    missing = example_catalog()
    del operation_named(missing, PROJECT_SNAPSHOT_OPERATION_ID)["input_schema"]
    with pytest.raises(StateManifestError, match="operation-catalog contract"):
        pin_state_read_operations({"ok": True, "command": "describe", "data": rehash(missing)})


def test_unresolvable_schema_reference_fails_closed() -> None:
    # check_schema alone never resolves $ref; a dangling pointer previously
    # passed pinning and crashed only at input-evaluation time.
    dangling = example_catalog()
    operation_named(dangling, PRD_READ_CONTENT_OPERATION_ID)["input_schema"]["properties"]["prd_id"] = {
        "$ref": "#/$defs/does_not_exist"
    }
    with pytest.raises(StateManifestError, match="unresolvable"):
        pin_state_read_operations({"ok": True, "command": "describe", "data": rehash(dangling)})

    remote = example_catalog()
    operation_named(remote, PRD_READ_CONTENT_OPERATION_ID)["input_schema"]["properties"]["prd_id"] = {
        "$ref": "https://evil.example.com/schemas/anything.json"
    }
    with pytest.raises(StateManifestError, match="non-local"):
        pin_state_read_operations({"ok": True, "command": "describe", "data": rehash(remote)})

    # Positive control: a resolvable intra-document pointer still pins.
    local = example_catalog()
    operation = operation_named(local, PRD_READ_CONTENT_OPERATION_ID)
    operation["input_schema"]["$defs"] = {"prd_ref": {"type": "string", "minLength": 1}}
    operation["input_schema"]["properties"]["prd_id"] = {"$ref": "#/$defs/prd_ref"}
    pinned = pin_state_read_operations({"ok": True, "command": "describe", "data": rehash(local)})
    assert pinned.prd_read_content.input_schema["properties"]["prd_id"] == {"$ref": "#/$defs/prd_ref"}


def test_malformed_manifest_fails_closed_and_caches_nothing() -> None:
    discovery, calls = discovery_for("this is not a manifest")

    with pytest.raises(StateManifestError, match="one JSON object"):
        discovery.pinned()
    with pytest.raises(StateManifestError, match="one JSON object"):
        discovery.pinned()
    assert calls == [["anvil", "describe"]] * 2, "a failed discovery must not cache"

    with pytest.raises(StateManifestError, match="did not report ok"):
        pin_state_read_operations({"ok": False, "command": "describe", "data": {}})
    with pytest.raises(StateManifestError, match="unexpected command"):
        pin_state_read_operations({"ok": True, "command": "status", "data": {}})
    with pytest.raises(StateManifestError, match="no data object"):
        pin_state_read_operations({"ok": True, "command": "describe", "data": "text"})


def test_current_upstream_describe_shape_without_a_catalog_fails_closed() -> None:
    # The real `anvil describe` envelope today (upstream gap fakoli/anvil#178):
    # CLI/MCP surface metadata, but no anvil-operation-catalog/v1.
    live_shape = {
        "ok": True, "command": "describe",
        "data": {
            "api_version": "4", "engine_version": "0.6.0", "schema_version": 16,
            "envelope": "v1.24",
            "cli": {"commands": [], "count": 0},
            "mcp": {"tools": ["get_task", "list_tasks"], "count": 2},
        },
    }
    with pytest.raises(StateManifestError, match="operation catalog"):
        pin_state_read_operations(live_shape)


def test_manifest_naming_a_foreign_provider_fails_closed() -> None:
    catalog = example_catalog()
    catalog["provider"] = "anvil-serving"
    with pytest.raises(StateManifestError, match="unexpected provider"):
        pin_state_read_operations({"ok": True, "command": "describe", "data": rehash(catalog)})


def test_ambiguous_duplicate_operation_declarations_fail_closed() -> None:
    catalog = example_catalog()
    duplicate = copy.deepcopy(operation_named(catalog, PROJECT_SNAPSHOT_OPERATION_ID))
    duplicate["contract_version"] = "1.1.0"
    catalog["operations"].append(duplicate)
    with pytest.raises(StateManifestError, match="ambiguously"):
        pin_state_read_operations({"ok": True, "command": "describe", "data": rehash(catalog)})


def test_default_runner_executes_the_configured_cli_argv(monkeypatch, tmp_path: Path) -> None:
    observed: list[tuple[list[str], Path | None]] = []

    def fake_run(args, **kwargs):
        observed.append((list(args), kwargs["cwd"]))
        return subprocess.CompletedProcess(args, 0, envelope(example_catalog()), "")

    monkeypatch.setattr(state_manifest_module.subprocess, "run", fake_run)
    discovery = StateManifestDiscovery("custom-state describe --json", cwd=tmp_path)

    assert discovery.pinned().project_snapshot.operation_id == PROJECT_SNAPSHOT_OPERATION_ID
    assert observed == [(["custom-state", "describe", "--json"], tmp_path)]

    def failing_run(args, **_kwargs):
        return subprocess.CompletedProcess(args, 3, "", "state exploded")

    monkeypatch.setattr(state_manifest_module.subprocess, "run", failing_run)
    with pytest.raises(StateManifestError, match="state exploded"):
        StateManifestDiscovery("custom-state describe --json", cwd=tmp_path).pinned()


def test_discovery_reads_the_describe_command_from_bridge_settings(tmp_path: Path) -> None:
    settings = SimpleNamespace(
        state_describe_command="anvil describe --json", project_root=tmp_path,
    )
    discovery = StateManifestDiscovery.from_settings(settings)
    assert discovery._describe_command == "anvil describe --json"
    assert discovery._cwd == tmp_path

    with pytest.raises(StateManifestError, match="not configured"):
        StateManifestDiscovery("   ")


def test_pinned_descriptor_set_is_immutable() -> None:
    discovery, _calls = discovery_for(envelope(example_catalog()))
    pinned = discovery.pinned()

    with pytest.raises(AttributeError):
        pinned.project_snapshot = pinned.prd_read_content  # type: ignore[misc]
    with pytest.raises(AttributeError):
        pinned.project_snapshot.operation_digest = "sha256:" + "0" * 64  # type: ignore[misc]
    # Schemas are exposed as parsed copies; mutating one cannot alter the pin.
    schema = pinned.prd_read_content.input_schema
    schema["properties"]["injected"] = {"type": "string"}
    assert "injected" not in pinned.prd_read_content.input_schema["properties"]


def test_gated_read_operation_fails_closed_instead_of_pinning_ungated() -> None:
    catalog = example_catalog()
    operation_named(catalog, "state.prd.read_content")["gates"]["human_approval"] = "required"
    operation_named(catalog, "state.prd.read_content")["gates"]["approval_action"] = "commit_pr"
    discovery, _ = discovery_for(envelope(rehash(catalog)))
    with pytest.raises(StateManifestError, match="active gate"):
        discovery.pinned()


def test_catalog_violating_the_operation_catalog_contract_fails_closed() -> None:
    catalog = example_catalog()
    operation_named(catalog, "state.project.snapshot")["execution"]["command"] = "state-cli"
    discovery, _ = discovery_for(envelope(rehash(catalog)))
    with pytest.raises(StateManifestError, match="operation-catalog contract"):
        discovery.pinned()


def test_incompatible_schema_dialect_fails_closed() -> None:
    catalog = example_catalog()
    operation_named(catalog, "state.project.snapshot")["input_schema"]["$schema"] = (
        "http://json-schema.org/draft-07/schema#"
    )
    discovery, _ = discovery_for(envelope(rehash(catalog)))
    with pytest.raises(StateManifestError, match="dialect"):
        discovery.pinned()


def test_catalog_generated_at_cannot_smuggle_markdown_scale_content() -> None:
    catalog = example_catalog()
    catalog["generated_at"] = "# Full PRD\n\n" + ("Thousands of words of requirements. " * 3000)
    discovery, _ = discovery_for(envelope(catalog))
    with pytest.raises(StateManifestError, match="operation-catalog contract"):
        discovery.pinned()
