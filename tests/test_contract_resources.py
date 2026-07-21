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
    validate_prd_content,
    validate_profile,
    validate_state_snapshot,
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
    "chat.conversation.v1.json": "chat-conversation.v1.schema.json",
    "chat.turn.user-voice.v1.json": "chat-turn.v1.schema.json",
    "chat.turn.assistant-interrupted.v1.json": "chat-turn.v1.schema.json",
    "settings-descriptor.v1.json": "settings-descriptor.v1.schema.json",
    "advanced-branch.v1.json": "advanced-branch.v1.schema.json",
    "advanced-trace.v1.json": "advanced-trace.v1.schema.json",
    "advanced-preset.v1.json": "advanced-preset.v1.schema.json",
    "advanced-comparison.v1.json": "advanced-comparison.v1.schema.json",
    "task-reference.v1.json": "task-reference.v1.schema.json",
    "delivery-eligibility.v1.json": "delivery-eligibility.v1.schema.json",
    "deliver-intent.v1.json": "deliver-intent.v1.schema.json",
    "deliver-start-receipt.v1.json": "deliver-start-receipt.v1.schema.json",
    "deliver-start-receipt.refusal.v1.json": "deliver-start-receipt.v1.schema.json",
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
    assert contract_digest("workflow", workflow) == "sha256:08eb89de05b27a1d22db4b26ca743f4e5cf60b47efca0f5ff0d5fc65d868e73a"
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


def test_snapshot_validation_refuses_an_unresolvable_pinned_input_schema() -> None:
    # A pinned schema whose $ref cannot resolve used to escape as a raw
    # jsonschema referencing error (not a ValidationError) at evaluation time;
    # it must fail closed as a ContractValidationError instead.
    catalogs = {
        catalog["provider"]: catalog
        for catalog in (
            _load(EXAMPLES / "anvil-state.catalog.v1.json"),
            _load(EXAMPLES / "anvil-serving.catalog.v1.json"),
            _load(EXAMPLES / "project-bridge.catalog.v1.json"),
        )
    }
    profile = _load(EXAMPLES / "project-capability-profile.v1.json")
    command = copy.deepcopy(_load(EXAMPLES / "bridge-command.invoke-operation.v1.json"))
    reference = command["payload"]["operation"]
    catalog = copy.deepcopy(catalogs[reference["provider"]])
    operation = next(
        item for item in catalog["operations"]
        if item["id"] == reference["id"] and item["contract_version"] == reference["contract_version"]
    )
    operation["input_schema"].setdefault("properties", {})["poisoned"] = {
        "$ref": "#/$defs/does_not_exist"
    }
    operation["operation_digest"] = contract_digest("operation", operation)
    catalog["catalog_digest"] = contract_digest("catalog", catalog)
    catalogs[reference["provider"]] = catalog
    reference["operation_digest"] = operation["operation_digest"]
    for entry in command["workflow_snapshot"]["catalogs"]:
        if entry["provider"] == reference["provider"]:
            entry["digest"] = catalog["catalog_digest"]

    with pytest.raises(ContractValidationError, match="unresolvable"):
        validate_bridge_command_snapshot(command, catalogs, profile)


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


COMMAND_LIKE = re.compile(
    r"(?i)(?:^|\s)(?:python|pytest|git|npm|npx|pip|uv|bash|sh|pwsh|powershell|curl|wget|docker|rm|del)\s+[-.\w]"
)
HOST_PATH_LIKE = re.compile(r"(?i)(?:[A-Za-z]:[\\/]|/(?:home|users|tmp|var|etc)/|\\\\)")


def test_contract_examples_are_redacted_and_digest_shaped() -> None:
    example_text = "\n".join(path.read_text(encoding="utf-8").lower() for path in EXAMPLES.glob("*.json"))
    for forbidden in ("api_key", "authorization", "password", "secret", "github_token", "state.db"):
        assert forbidden not in example_text

    for path in EXAMPLES.glob("*.json"):
        for value in _walk(_load(path)):
            if not isinstance(value, str):
                continue
            if value.startswith("sha256:"):
                assert DIGEST.fullmatch(value), (path, value)
            assert not COMMAND_LIKE.search(value), (path, value)
            assert not HOST_PATH_LIKE.search(value), (path, value)


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


def test_digest_domain_rejects_empty_key_stripping_and_floats() -> None:
    assert approval_payload_digest({"": "smuggled", "a": 1}) != approval_payload_digest({"a": 1})

    with pytest.raises(ContractValidationError, match="floating-point"):
        contract_digest("approval-payload", {"amount": 1.5})
    with pytest.raises(ContractValidationError, match="floating-point"):
        contract_digest("state-snapshot", {"prds": [{"weight": 0.25}]})


def test_state_snapshot_reference_validator_fails_closed() -> None:
    snapshot = _load(EXAMPLES / "anvil-state.project-snapshot.v1.json")
    validate_state_snapshot(snapshot)

    drifted = copy.deepcopy(snapshot)
    drifted["tasks"][0]["title"] += "!"
    with pytest.raises(ContractValidationError, match="digest mismatch"):
        validate_state_snapshot(drifted)

    def rehash(payload):
        payload["snapshot_digest"] = contract_digest("state-snapshot", payload)
        return payload

    mismatched = rehash(copy.deepcopy(snapshot))
    mismatched["tasks"][0]["scoped_id"] = "release-alpha:T999"
    with pytest.raises(ContractValidationError, match="scoped_id"):
        validate_state_snapshot(rehash(mismatched))

    orphaned = copy.deepcopy(snapshot)
    orphaned["tasks"][0]["ref"]["prd_id"] = "release-gamma"
    orphaned["tasks"][0]["scoped_id"] = "release-gamma:T001"
    with pytest.raises(ContractValidationError, match="unknown PRD"):
        validate_state_snapshot(rehash(orphaned))

    duplicated = copy.deepcopy(snapshot)
    duplicated["tasks"].append(copy.deepcopy(duplicated["tasks"][0]))
    with pytest.raises(ContractValidationError, match="duplicate task reference"):
        validate_state_snapshot(rehash(duplicated))


def test_prd_content_reference_validator_enforces_byte_bounds() -> None:
    document = _load(EXAMPLES / "anvil-state.prd-content.v1.json")
    validate_prd_content(document)

    def rehash(payload):
        payload["content_digest"] = contract_digest("prd-content", payload)
        return payload

    lying = copy.deepcopy(document)
    lying["content"]["truncated"] = False
    with pytest.raises(ContractValidationError, match="total_bytes"):
        validate_prd_content(rehash(lying))

    shrunk = copy.deepcopy(document)
    shrunk["content"]["total_bytes"] = 1
    with pytest.raises(ContractValidationError, match="total_bytes"):
        validate_prd_content(rehash(shrunk))


def test_state_catalog_declares_both_required_read_surfaces_with_scoped_mutation_refs() -> None:
    catalog = _load(EXAMPLES / "anvil-state.catalog.v1.json")
    operations = {operation["id"]: operation for operation in catalog["operations"]}
    assert {"state.project.snapshot", "state.prd.read_content"} <= set(operations)
    for read_id in ("state.project.snapshot", "state.prd.read_content"):
        assert operations[read_id]["effect"] == "read"

    snapshot = _load(EXAMPLES / "anvil-state.project-snapshot.v1.json")
    assert snapshot["source"]["read_operation_id"] in operations

    scoped = re.compile(r"^\^\[a-z0-9\]")
    for mutation_id in ("state.task.claim", "state.evidence.submit"):
        properties = operations[mutation_id]["input_schema"]["properties"]
        assert "task_id" not in properties, "bare task_id must not appear at a state-mutation boundary"
        assert scoped.match(properties["task_ref"]["pattern"])

    merge = next(
        operation for operation in _load(EXAMPLES / "project-bridge.catalog.v1.json")["operations"]
        if operation["id"] == "bridge.github.merge_and_accept"
    )
    assert "task_ref" in merge["input_schema"]["required"]


def test_refusal_receipt_example_is_a_typed_preflight_denial() -> None:
    receipt = _load(EXAMPLES / "operation-receipt.refusal.v1.json")
    assert receipt["status"] == "denied"
    assert receipt["error"]["retryable"] is False
    assert receipt["error"]["code"] == "catalog.digest_drift"
    assert receipt["redaction"]["status"] == "metadata_only"


def test_chat_turn_lineage_is_append_only_rooted_and_unambiguous() -> None:
    validator = _validator("chat-turn.v1.schema.json")
    root = _load(EXAMPLES / "chat.turn.user-voice.v1.json")
    reply = _load(EXAMPLES / "chat.turn.assistant-interrupted.v1.json")
    assert root["lineage"]["parent_turn_id"] is None
    assert root["lineage"] == {"parent_turn_id": None, "sibling_index": 0, "kind": "initial"}
    assert reply["lineage"]["parent_turn_id"] == root["turn_id"]
    assert reply["conversation_id"] == root["conversation_id"]

    unlinked = copy.deepcopy(reply)
    del unlinked["lineage"]["parent_turn_id"]
    with pytest.raises(ValidationError):
        validator.validate(unlinked)
    orphan = copy.deepcopy(reply)
    del orphan["lineage"]
    with pytest.raises(ValidationError):
        validator.validate(orphan)
    unordered = copy.deepcopy(reply)
    unordered["lineage"]["sibling_index"] = -1
    with pytest.raises(ValidationError):
        validator.validate(unordered)

    retry = copy.deepcopy(reply)
    retry["turn_id"] = "turn_assistant_0002"
    retry["lineage"]["sibling_index"] = 1
    retry["lineage"]["kind"] = "retry"
    validator.validate(retry)

    fabricated = copy.deepcopy(reply)
    fabricated["status"] = "partially_complete"
    with pytest.raises(ValidationError):
        validator.validate(fabricated)
    assert reply["status"] == "interrupted", "interrupted responses must be representable as-is"
    streaming = copy.deepcopy(reply)
    streaming["status"] = "streaming"
    with pytest.raises(ValidationError):
        validator.validate(streaming)  # a streaming turn cannot already claim completed_at
    del streaming["completed_at"]
    validator.validate(streaming)


def test_chat_route_reference_rejects_endpoints_credentials_and_unknown_providers() -> None:
    validator = _validator("chat-turn.v1.schema.json")
    reply = _load(EXAMPLES / "chat.turn.assistant-interrupted.v1.json")
    assert reply["route"]["provider"] == "anvil-serving"

    for endpoint_like in ("https://serving.internal/v1/responses", "serving.internal:8000", "10.0.0.7"):
        smuggled = copy.deepcopy(reply)
        smuggled["route"]["route_id"] = endpoint_like
        with pytest.raises(ValidationError):
            validator.validate(smuggled)
    for extra_field in ("endpoint", "base_url", "token"):
        smuggled = copy.deepcopy(reply)
        smuggled["route"][extra_field] = "opaque"
        with pytest.raises(ValidationError):
            validator.validate(smuggled)

    raw_provider = copy.deepcopy(reply)
    raw_provider["route"]["provider"] = "openai"
    with pytest.raises(ValidationError):
        validator.validate(raw_provider)
    unrouted = copy.deepcopy(reply)
    del unrouted["route"]
    with pytest.raises(ValidationError):
        validator.validate(unrouted)  # every assistant turn pins its route
    user_routed = copy.deepcopy(_load(EXAMPLES / "chat.turn.user-voice.v1.json"))
    user_routed["route"] = copy.deepcopy(reply["route"])
    with pytest.raises(ValidationError):
        validator.validate(user_routed)


def test_chat_context_reference_is_display_only_and_pins_titles_with_canonical_ids() -> None:
    validator = _validator("chat-conversation.v1.schema.json")
    conversation = _load(EXAMPLES / "chat.conversation.v1.json")
    context = conversation["context"]
    assert context["binding"] == "display_only"
    assert context["prd"]["prd_revision"] >= 1
    for task in context["tasks"]:
        assert task["scoped_id"] == f"{task['ref']['prd_id']}:{task['ref']['task_id']}"

    unpinned = copy.deepcopy(conversation)
    del unpinned["context"]["prd"]["prd_revision"]
    with pytest.raises(ValidationError):
        validator.validate(unpinned)
    unowned = copy.deepcopy(conversation)
    del unowned["context"]["tasks"][0]["ref"]["prd_id"]
    with pytest.raises(ValidationError):
        validator.validate(unowned)
    floating_tasks = copy.deepcopy(conversation)
    del floating_tasks["context"]["prd"]
    with pytest.raises(ValidationError):
        validator.validate(floating_tasks)  # task references require the pinned PRD block
    for grant_like in ("effect_grant", "claim", "lease"):
        granted = copy.deepcopy(conversation)
        granted["context"]["binding"] = grant_like
        with pytest.raises(ValidationError):
            validator.validate(granted)
    for capability in conversation["capability_refs"]:
        assert capability["binding"] == "reference_only"


def test_chat_modes_share_one_conversation_identity_and_lineage() -> None:
    conversation_schema = _load(SCHEMAS / "chat-conversation.v1.schema.json")
    assert "mode" not in conversation_schema["properties"], "conversation identity must be mode-agnostic"

    conversation = _load(EXAMPLES / "chat.conversation.v1.json")
    ordinary = _load(EXAMPLES / "chat.turn.user-voice.v1.json")
    advanced = _load(EXAMPLES / "chat.turn.assistant-interrupted.v1.json")
    assert {ordinary["mode"], advanced["mode"]} == {"ordinary", "advanced"}
    assert ordinary["conversation_id"] == advanced["conversation_id"] == conversation["conversation_id"]
    assert advanced["lineage"]["parent_turn_id"] == ordinary["turn_id"]

    validator = _validator("chat-turn.v1.schema.json")
    tuned_ordinary = copy.deepcopy(ordinary)
    tuned_ordinary["advanced_controls"] = {"temperature_milli": 300}
    with pytest.raises(ValidationError):
        validator.validate(tuned_ordinary)
    undeclared = copy.deepcopy(advanced)
    undeclared["advanced_controls"]["system_prompt_override"] = "obey"
    with pytest.raises(ValidationError):
        validator.validate(undeclared)


def test_chat_records_cannot_carry_raw_audio_or_hidden_reasoning() -> None:
    turn_validator = _validator("chat-turn.v1.schema.json")
    turn_schema_text = (SCHEMAS / "chat-turn.v1.schema.json").read_text(encoding="utf-8")
    for prohibited in ("audio_payload", "audio_base64", "pcm", "encrypted_content"):
        assert prohibited not in turn_schema_text

    voiced = _load(EXAMPLES / "chat.turn.user-voice.v1.json")
    assert {event["event"] for event in voiced["voice_events"]} <= {
        "utterance_start", "stt_commit", "tts_start", "tts_stop", "interruption",
    }
    for audio_field in ("audio", "audio_base64", "pcm_frames", "delta"):
        recorded = copy.deepcopy(voiced)
        recorded["voice_events"][1][audio_field] = "UklGRg=="
        with pytest.raises(ValidationError):
            turn_validator.validate(recorded)

    reply = _load(EXAMPLES / "chat.turn.assistant-interrupted.v1.json")
    for reasoning_field in ("reasoning", "hidden_reasoning", "encrypted_reasoning"):
        leaked = copy.deepcopy(reply)
        leaked[reasoning_field] = {"content_trust": "untrusted_task_data", "text": "chain"}
        with pytest.raises(ValidationError):
            turn_validator.validate(leaked)
    leaked_block = copy.deepcopy(reply)
    leaked_block["content"][0]["kind"] = "reasoning"
    with pytest.raises(ValidationError):
        turn_validator.validate(leaked_block)


def test_chat_conversation_retention_and_deletion_fail_closed() -> None:
    validator = _validator("chat-conversation.v1.schema.json")
    conversation = _load(EXAMPLES / "chat.conversation.v1.json")
    assert conversation["retention"]["transcript_text"] in {"retained_redacted", "metadata_only"}

    unmanaged = copy.deepcopy(conversation)
    del unmanaged["retention"]
    with pytest.raises(ValidationError):
        validator.validate(unmanaged)
    forever = copy.deepcopy(conversation)
    forever["retention"]["transcript_text"] = "retained_raw"
    with pytest.raises(ValidationError):
        validator.validate(forever)

    deleting = copy.deepcopy(conversation)
    deleting["status"] = "deletion_pending"
    with pytest.raises(ValidationError):
        validator.validate(deleting)  # a deletion state requires its typed deletion record
    deleting["deletion"] = {"requested_at": "2026-07-19T01:00:00Z", "mode": "purge_content_keep_tombstone"}
    validator.validate(deleting)
    haunted = copy.deepcopy(conversation)
    haunted["deletion"] = {"requested_at": "2026-07-19T01:00:00Z", "mode": "purge_all_records"}
    with pytest.raises(ValidationError):
        validator.validate(haunted)  # an active conversation cannot carry a deletion record


def _chat_retention_violations(
    turn: dict[str, object], conversation: dict[str, object],
) -> list[tuple[str, str]]:
    """Return (retention_field, content_kind) pairs the conversation policy forbids.

    README convention 11: `transcript_text` governs persisted transcript content
    on text turns, `voice_transcript_text` governs persisted transcript content
    on voice-input turns, and `metadata_only` means no transcript content block
    may persist for that kind — only bounded counters/metadata survive.
    """
    retention = conversation["retention"]
    is_voice_input = turn["role"] == "user" and any(
        event["event"] in {"utterance_start", "stt_commit"}
        for event in turn.get("voice_events", ())
    )
    field = "voice_transcript_text" if is_voice_input else "transcript_text"
    return [
        (field, str(block["kind"]))
        for block in turn["content"]
        if block["kind"] == "transcript" and retention[field] == "metadata_only"
    ]


def test_chat_turn_examples_respect_their_conversations_retention_policy() -> None:
    payloads = [_load(EXAMPLES / name) for name in SCHEMA_FOR_EXAMPLE]
    conversations = {
        payload["conversation_id"]: payload
        for payload in payloads
        if payload.get("schema_version") == "workbench-chat-conversation/v1"
    }
    turns = [
        payload for payload in payloads
        if payload.get("schema_version") == "workbench-chat-turn/v1"
    ]
    assert conversations and turns
    for turn in turns:
        conversation = conversations[turn["conversation_id"]]
        assert _chat_retention_violations(turn, conversation) == [], turn["turn_id"]

    voiced = _load(EXAMPLES / "chat.turn.user-voice.v1.json")
    owner = conversations[voiced["conversation_id"]]
    assert owner["retention"]["voice_transcript_text"] == "retained_redacted", (
        "the registered voice turn persists transcript text, so its conversation "
        "must retain voice transcripts"
    )
    restricted = copy.deepcopy(owner)
    restricted["retention"]["voice_transcript_text"] = "metadata_only"
    assert _chat_retention_violations(voiced, restricted) == [
        ("voice_transcript_text", "transcript")
    ], "a metadata_only voice policy must flag the persisted voice transcript"


def test_metadata_only_turn_redaction_cannot_carry_text_bearing_content() -> None:
    validator = _validator("chat-turn.v1.schema.json")
    voiced = _load(EXAMPLES / "chat.turn.user-voice.v1.json")

    stripped = copy.deepcopy(voiced)
    stripped["redaction"]["status"] = "metadata_only"
    with pytest.raises(ValidationError):
        validator.validate(stripped)  # transcript text may not survive metadata_only
    stripped["content"] = []
    validator.validate(stripped)  # metadata-only record keeps voice_events counters only


def test_assistant_turn_records_bounded_serving_request_id() -> None:
    validator = _validator("chat-turn.v1.schema.json")
    reply = _load(EXAMPLES / "chat.turn.assistant-interrupted.v1.json")
    assert re.fullmatch(r"[A-Za-z0-9._-]{1,128}", reply["route"]["request_id"])

    unrecorded = copy.deepcopy(reply)
    del unrecorded["route"]["request_id"]
    with pytest.raises(ValidationError):
        validator.validate(unrecorded)  # R006: assistant turns record the Serving request ID
    for hostile in ("", "req 001", "https://serving.internal/req/1", "r" * 129):
        smuggled = copy.deepcopy(reply)
        smuggled["route"]["request_id"] = hostile
        with pytest.raises(ValidationError):
            validator.validate(smuggled)


def _walk(value: object):
    if isinstance(value, dict):
        for nested in value.values():
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)
    else:
        yield value
