"""Keep proposed contract resources schema-valid, linked, and redacted.

The JSON resources are implementation inputs for later Workbench work. These
tests protect the model-facing examples from quietly drifting away from their
schemas, catalog/profile allowlists, and hard authority boundaries.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from workbench.contracts import (
    ContractValidationError,
    approval_payload_digest,
    contract_digest,
    validate_bridge_command_snapshot,
    validate_catalog,
    validate_profile,
)

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs" / "contracts"
EXAMPLES = CONTRACTS / "examples"
SCHEMAS = CONTRACTS / "schemas"
DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
SCHEMA_FOR_EXAMPLE = {
    "anvil-state.catalog.v1.json": "operation-catalog.v1.schema.json",
    "anvil-serving.catalog.v1.json": "operation-catalog.v1.schema.json",
    "project-bridge.catalog.v1.json": "operation-catalog.v1.schema.json",
    "project-capability-profile.v1.json": "capability-profile.v1.schema.json",
    "delivery.workflow.v2.json": "workflow.v2.schema.json",
    "run-context.v1.json": "run-context.v1.schema.json",
    "model-proposal.operation-request.v1.json": "model-proposal.v1.schema.json",
    "bridge-command.invoke-operation.v1.json": "bridge-command.v1.schema.json",
    "operation-receipt.v1.json": "operation-receipt.v1.schema.json",
    "operation-receipt.refusal.v1.json": "operation-receipt.v1.schema.json",
    "anvil-state.project-snapshot.v1.json": "state-snapshot.v1.schema.json",
    "anvil-state.prd-content.v1.json": "prd-content.v1.schema.json",
}


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _operation_key(operation: dict[str, object]) -> tuple[str, str, str, str]:
    return (
        str(operation["provider"]),
        str(operation["id"]),
        str(operation["contract_version"]),
        str(operation["operation_digest"]),
    )


def _validator(schema_name: str) -> Draft202012Validator:
    schema = _load(SCHEMAS / schema_name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def test_contract_schemas_and_examples_are_valid_json_schema_documents() -> None:
    paths = sorted((*SCHEMAS.glob("*.json"), *EXAMPLES.glob("*.json")))
    assert paths
    for path in paths:
        payload = _load(path)
        assert payload, path
        if path.parent == SCHEMAS:
            assert payload["$schema"] == "https://json-schema.org/draft/2020-12/schema"

    assert set(path.name for path in EXAMPLES.glob("*.json")) == set(SCHEMA_FOR_EXAMPLE)
    for example_name, schema_name in SCHEMA_FOR_EXAMPLE.items():
        _validator(schema_name).validate(_load(EXAMPLES / example_name))


def test_catalog_profile_workflow_and_context_examples_reference_declared_operations() -> None:
    catalogs = tuple(
        _load(EXAMPLES / name)
        for name in (
            "anvil-state.catalog.v1.json",
            "anvil-serving.catalog.v1.json",
            "project-bridge.catalog.v1.json",
        )
    )
    for catalog in catalogs:
        validate_catalog(catalog)
    operation_keys = {
        (str(catalog["provider"]), str(operation["id"]), str(operation["contract_version"]), str(operation["operation_digest"]))
        for catalog in catalogs
        for operation in catalog["operations"]
    }

    profile = _load(EXAMPLES / "project-capability-profile.v1.json")
    validate_profile(profile)
    profile_keys = {_operation_key(operation) for operation in profile["operations"]}
    assert profile_keys <= operation_keys

    workflow = _load(EXAMPLES / "delivery.workflow.v2.json")
    assert contract_digest("workflow", workflow) == "sha256:debbde33e9826df965d26112fc3ea785ccaf3b456e115dd18d8afa8459a58994"
    workflow_keys = {_operation_key(step["operation"]) for step in workflow["steps"] if step["kind"] == "operation"}
    assert workflow_keys <= profile_keys

    proposal = _load(EXAMPLES / "model-proposal.operation-request.v1.json")
    assert _operation_key(proposal["operation"]) in profile_keys

    context = _load(EXAMPLES / "run-context.v1.json")
    context_keys = {
        (str(item["provider"]), str(item["operation_id"]), str(item["contract_version"]), str(item["operation_digest"]))
        for item in context["capabilities"]
    }
    assert context_keys <= profile_keys


def test_contract_digests_and_cross_resource_snapshot_rules_fail_closed() -> None:
    catalogs = {
        catalog["provider"]: catalog
        for catalog in (
            _load(EXAMPLES / "anvil-state.catalog.v1.json"),
            _load(EXAMPLES / "anvil-serving.catalog.v1.json"),
            _load(EXAMPLES / "project-bridge.catalog.v1.json"),
        )
    }
    profile = _load(EXAMPLES / "project-capability-profile.v1.json")
    command = _load(EXAMPLES / "bridge-command.invoke-operation.v1.json")
    validate_bridge_command_snapshot(command, catalogs, profile)

    changed = copy.deepcopy(catalogs["anvil-state"])
    changed["operations"][0]["summary"] += "!"
    with pytest.raises(ContractValidationError, match="operation digest mismatch"):
        validate_catalog(changed)

    for mutation in (
        lambda value: value["workflow_snapshot"].__setitem__("catalogs", []),
        lambda value: value["workflow_snapshot"].__setitem__(
            "catalogs", value["workflow_snapshot"]["catalogs"] * 2,
        ),
        lambda value: value["workflow_snapshot"]["catalogs"][0].__setitem__("digest", "sha256:" + "0" * 64),
    ):
        invalid = copy.deepcopy(command)
        mutation(invalid)
        with pytest.raises(ContractValidationError):
            validate_bridge_command_snapshot(invalid, catalogs, profile)


def test_approval_gated_operation_requires_matching_grant_action_and_exact_inputs() -> None:
    class AtomicApprovalConsumer:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str, str, str]] = []

        def consume(
            self, grant_id: str, action: str, payload_hash: str, bridge_id: str, project_id: str,
        ) -> None:
            self.calls.append((grant_id, action, payload_hash, bridge_id, project_id))

    catalogs = {
        catalog["provider"]: catalog
        for catalog in (
            _load(EXAMPLES / "anvil-state.catalog.v1.json"),
            _load(EXAMPLES / "anvil-serving.catalog.v1.json"),
            _load(EXAMPLES / "project-bridge.catalog.v1.json"),
        )
    }
    profile = _load(EXAMPLES / "project-capability-profile.v1.json")
    operation = catalogs["project-bridge"]["operations"][0]
    inputs = {
        "diff_hash": "a" * 64,
        "branch": "codex/delivery",
        "title": "Anvil Workbench delivery",
        "base": "main",
    }
    command = {
        **_load(EXAMPLES / "bridge-command.invoke-operation.v1.json"),
        "workflow_snapshot": {
            "workflow_digest": contract_digest("workflow", _load(EXAMPLES / "delivery.workflow.v2.json")),
            "catalogs": [{"provider": "project-bridge", "digest": catalogs["project-bridge"]["catalog_digest"]}],
            "capability_profile_digest": profile["digest"],
        },
        "payload": {
            "operation": {
                "provider": "project-bridge", "id": operation["id"],
                "contract_version": operation["contract_version"], "operation_digest": operation["operation_digest"],
            },
            "inputs": inputs,
        },
    }
    with pytest.raises(ContractValidationError, match="requires typed approval"):
        validate_bridge_command_snapshot(command, catalogs, profile)

    command["approval_grant_id"] = "approval_example_0001"
    command["payload"]["approval"] = {
        "grant_id": "approval_example_0001",
        "action": "commit_pr",
        "payload_hash": approval_payload_digest(inputs),
    }
    with pytest.raises(ContractValidationError, match="atomic approval consumer"):
        validate_bridge_command_snapshot(command, catalogs, profile)

    consumer = AtomicApprovalConsumer()
    validate_bridge_command_snapshot(command, catalogs, profile, consumer)
    assert consumer.calls == [
        (
            "approval_example_0001", "commit_pr", approval_payload_digest(inputs),
            "bridge_example", "project_example",
        )
    ]

    raw_command = copy.deepcopy(command)
    raw_command["payload"]["inputs"] = {"raw_command": "git push --force"}
    raw_command["payload"]["approval"]["payload_hash"] = approval_payload_digest(
        raw_command["payload"]["inputs"],
    )
    with pytest.raises(ContractValidationError, match="operation inputs do not match"):
        validate_bridge_command_snapshot(raw_command, catalogs, profile, consumer)

    command["payload"]["approval"]["action"] = "merge_and_accept"
    with pytest.raises(ContractValidationError, match="approval action"):
        validate_bridge_command_snapshot(command, catalogs, profile, consumer)


def test_delivery_fixture_has_a_terminal_success_path_and_reconciliation_for_effect_failures() -> None:
    workflow = _load(EXAMPLES / "delivery.workflow.v2.json")
    completed_step = next(step for step in workflow["steps"] if step["id"] == "merge_and_accept")
    assert completed_step["next"] == []
    catalogs = [
        _load(EXAMPLES / "anvil-state.catalog.v1.json"),
        _load(EXAMPLES / "anvil-serving.catalog.v1.json"),
        _load(EXAMPLES / "project-bridge.catalog.v1.json"),
    ]
    assert all(
        operation["failure"] == "reconcile"
        for catalog in catalogs
        for operation in catalog["operations"]
        if operation["effect"] in {"external_effect", "state_mutation"}
    )


def test_contract_examples_are_redacted_and_digest_shaped() -> None:
    example_text = "\n".join(path.read_text(encoding="utf-8").lower() for path in EXAMPLES.glob("*.json"))
    for forbidden in ("api_key", "authorization", "password", "secret", "github_token", "state.db"):
        assert forbidden not in example_text

    for path in EXAMPLES.glob("*.json"):
        for value in _walk(_load(path)):
            if isinstance(value, str) and value.startswith("sha256:"):
                assert DIGEST.fullmatch(value), (path, value)


def test_contract_schemas_reject_privilege_and_binding_shortcuts() -> None:
    workflow = copy.deepcopy(_load(EXAMPLES / "delivery.workflow.v2.json"))
    workflow["steps"][0]["inputs"]["task_id"] = {"from": "run.task_id"}
    with pytest.raises(ValidationError):
        _validator("workflow.v2.schema.json").validate(workflow)

    command = copy.deepcopy(_load(EXAMPLES / "bridge-command.invoke-operation.v1.json"))
    command["kind"] = "commit_pr"
    command["payload"] = {
        "action": "commit_pr",
        "approval_payload_hash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "operation": {"provider": "project-bridge", "id": "bridge.github.commit_pr", "contract_version": "1.0.0", "operation_digest": "sha256:8aef716c52d7bf33f19a2829b56e7ad8f19cd5f1cce6cc6d5a10320410e512c3"},
    }
    with pytest.raises(ValidationError):
        _validator("bridge-command.v1.schema.json").validate(command)

    command["approval_grant_id"] = "approval_example_0001"
    command["payload"]["action"] = "merge_and_accept"
    with pytest.raises(ValidationError):
        _validator("bridge-command.v1.schema.json").validate(command)

    receipt = copy.deepcopy(_load(EXAMPLES / "operation-receipt.v1.json"))
    receipt["error"] = {"code": "failure", "safe_summary": "Bearer token", "retryable": False}
    with pytest.raises(ValidationError):
        _validator("operation-receipt.v1.schema.json").validate(receipt)


def test_state_snapshot_digest_is_deterministic_order_independent_and_content_sensitive() -> None:
    snapshot = _load(EXAMPLES / "anvil-state.project-snapshot.v1.json")
    advertised = snapshot["snapshot_digest"]
    assert contract_digest("state-snapshot", snapshot) == advertised

    reordered = copy.deepcopy(snapshot)
    reordered["prds"] = list(reversed(reordered["prds"]))
    reordered["tasks"] = list(reversed(reordered["tasks"]))
    reordered["generated_at"] = "2027-01-01T00:00:00Z"
    assert contract_digest("state-snapshot", reordered) == advertised

    changed = copy.deepcopy(snapshot)
    changed["tasks"][0]["title"] += "!"
    assert contract_digest("state-snapshot", changed) != advertised


def test_state_snapshot_task_references_require_an_owning_prd_and_scope_duplicate_ids() -> None:
    snapshot = _load(EXAMPLES / "anvil-state.project-snapshot.v1.json")
    prd_ids = {prd["prd_id"] for prd in snapshot["prds"]}
    task_ids = [task["ref"]["task_id"] for task in snapshot["tasks"]]
    assert len(set(task_ids)) < len(task_ids), "example must exercise duplicate task IDs across PRDs"
    scoped = set()
    for task in snapshot["tasks"]:
        ref = task["ref"]
        assert ref["prd_id"] in prd_ids
        assert task["scoped_id"] == f"{ref['prd_id']}:{ref['task_id']}"
        scoped.add(task["scoped_id"])
        for dependency in task.get("depends_on", ()):
            assert dependency["prd_id"] in prd_ids
    assert len(scoped) == len(snapshot["tasks"])

    validator = _validator("state-snapshot.v1.schema.json")
    orphan = copy.deepcopy(snapshot)
    del orphan["tasks"][0]["ref"]["prd_id"]
    with pytest.raises(ValidationError):
        validator.validate(orphan)

    run_context_validator = _validator("run-context.v1.schema.json")
    context = copy.deepcopy(_load(EXAMPLES / "run-context.v1.json"))
    del context["task"]["ref"]
    with pytest.raises(ValidationError):
        run_context_validator.validate(context)


def test_prd_content_read_is_bounded_digest_stable_and_rejects_path_smuggling() -> None:
    document = _load(EXAMPLES / "anvil-state.prd-content.v1.json")
    assert contract_digest("prd-content", document) == document["content_digest"]
    assert len(document["content"]["body"].encode("utf-8")) <= 65536
    assert document["content"]["truncated"] is True
    assert document["content"]["total_bytes"] > len(document["content"]["body"].encode("utf-8"))

    validator = _validator("prd-content.v1.schema.json")
    smuggled = copy.deepcopy(document)
    smuggled["content"]["file_path"] = "C:/projects/prd.md"
    with pytest.raises(ValidationError):
        validator.validate(smuggled)

    unbounded = copy.deepcopy(document)
    unbounded["content"]["body"] = "x" * 65537
    with pytest.raises(ValidationError):
        validator.validate(unbounded)


def test_refusal_receipt_example_is_a_typed_preflight_denial() -> None:
    receipt = _load(EXAMPLES / "operation-receipt.refusal.v1.json")
    assert receipt["status"] == "denied"
    assert receipt["error"]["retryable"] is False
    assert receipt["error"]["code"] == "catalog.digest_drift"
    assert receipt["redaction"]["status"] == "metadata_only"


def _walk(value: object):
    if isinstance(value, dict):
        for nested in value.values():
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)
    else:
        yield value
