"""Plugin catalog, capability, request, preview, and receipt contracts (reviewed-tools-plugins T001).

These tests bind the three T001 acceptance criteria to concrete proofs over the
proposed ``plugin-catalog``, ``plugin-capability``, ``plugin-request``,
``plugin-preview``, and ``plugin-receipt`` contract resources:

1. A plugin manifest cannot express a raw shell command, an arbitrary URL fetch,
   a local path, generic code execution, a credential value, or undeclared
   host/data access — ``test_criterion1_*``.
2. Every tool's effect class and gate set are mandatory and machine-checkable,
   and an effect-capable tool is preview/approval-shaped — ``test_criterion2_*``.
3. Plugin tools carry an explicit type that is non-equivalent to an Anvil
   provider operation or a bridge skill — ``test_criterion3_*``.

The resources are *proposed* operation-layer resources: no live endpoint reads
them yet. These tests pin the authority rules a later plugin host must inherit so
the shape cannot quietly drift into granting a privilege.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from workbench import contracts as contracts_module
from workbench.contracts import (
    ContractValidationError,
    approval_payload_digest,
    contract_digest,
    validate_plugin_capability,
    validate_plugin_catalog,
    validate_plugin_preview,
    validate_plugin_receipt,
    validate_plugin_request,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "docs" / "contracts" / "schemas"
EXAMPLES = ROOT / "docs" / "contracts" / "examples"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _validator(schema_name: str) -> Draft202012Validator:
    schema = _load(SCHEMAS / schema_name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _catalog() -> dict:
    return _load(EXAMPLES / "plugin.catalog.v1.json")


def _capability() -> dict:
    return _load(EXAMPLES / "plugin.capability.v1.json")


def _tool_call_request() -> dict:
    return _load(EXAMPLES / "plugin.request.tool-call.v1.json")


def _install_request() -> dict:
    return _load(EXAMPLES / "plugin.request.install.v1.json")


def _preview() -> dict:
    return _load(EXAMPLES / "plugin.preview.v1.json")


def _receipt() -> dict:
    return _load(EXAMPLES / "plugin.receipt.v1.json")


def _refusal_receipt() -> dict:
    return _load(EXAMPLES / "plugin.receipt.refusal.v1.json")


def _rehash_catalog(catalog: dict) -> dict:
    for plugin in catalog["plugins"]:
        plugin["plugin_digest"] = contract_digest("plugin", plugin)
    catalog["catalog_digest"] = contract_digest("plugin-catalog", catalog)
    return catalog


def _rehash_request(request: dict) -> dict:
    request["request_digest"] = contract_digest("plugin-request", request)
    return request


def _viewer_tool(catalog: dict, tool_id: str) -> dict:
    plugin = next(p for p in catalog["plugins"] if p["id"] == "anvil-tasks-viewer")
    return next(t for t in plugin["tools"] if t["tool_id"] == tool_id)


def _notifier_tool(catalog: dict) -> dict:
    plugin = next(p for p in catalog["plugins"] if p["id"] == "deploy-notifier")
    return plugin["tools"][0]


# --------------------------------------------------------------------------- #
# Criterion 1: a manifest cannot express raw shell, arbitrary URL fetch, local
# path, generic code execution, a credential value, or undeclared host/data
# access.
# --------------------------------------------------------------------------- #


def test_criterion1_baseline_catalog_validates() -> None:
    validate_plugin_catalog(_catalog())


def test_criterion1_tool_rejects_shell_url_path_code_and_credential_fields() -> None:
    validator = _validator("plugin-catalog.v1.schema.json")
    for smuggled in (
        "command", "shell", "argv", "script", "entrypoint", "code", "exec",
        "url", "endpoint", "base_url", "host", "port", "file_path", "path",
        "cwd", "token", "secret", "api_key", "password", "credential_value",
    ):
        catalog = _catalog()
        _viewer_tool(catalog, "tasks.list")[smuggled] = "x"
        with pytest.raises(ValidationError):
            validator.validate(catalog)  # the closed tool object refuses the field


def test_criterion1_host_access_cannot_be_a_url_or_path() -> None:
    validator = _validator("plugin-catalog.v1.schema.json")
    for hostile in ("https://evil.example/webhook", "http://10.0.0.7:8000", "/etc/passwd", "c:/creds"):
        catalog = _catalog()
        _notifier_tool(catalog)["host_access"][0]["scope_id"] = hostile
        with pytest.raises(ValidationError):
            validator.validate(catalog)  # a declared scope is a name, never a URL or path


def test_criterion1_undeclared_host_or_data_access_is_unrepresentable() -> None:
    validator = _validator("plugin-catalog.v1.schema.json")
    for field in ("data_access", "host_access"):
        catalog = _catalog()
        del _notifier_tool(catalog)[field]
        with pytest.raises(ValidationError):
            validator.validate(catalog)  # every tool must declare its access scopes


def test_criterion1_egress_is_host_mediated_never_a_direct_arbitrary_url() -> None:
    validator = _validator("plugin-catalog.v1.schema.json")
    catalog = _catalog()
    _notifier_tool(catalog)["host_access"][0]["egress"] = "direct_internet"
    with pytest.raises(ValidationError):
        validator.validate(catalog)  # egress is none or host_mediated only


def test_criterion1_credential_is_reference_only_never_a_value() -> None:
    validator = _validator("plugin-catalog.v1.schema.json")
    notifier = lambda cat: next(p for p in cat["plugins"] if p["id"] == "deploy-notifier")

    with_value = _catalog()
    notifier(with_value)["credential"]["value"] = "hunter2"
    with pytest.raises(ValidationError):
        validator.validate(with_value)  # no credential value field exists

    orphan_owner = _catalog()
    notifier(orphan_owner)["credential"] = {"requirement": "none", "owner_host": "anvil-connector-host"}
    with pytest.raises(ValidationError):
        validator.validate(orphan_owner)  # requirement:none cannot name an owner or refs

    missing_owner = _catalog()
    notifier(missing_owner)["credential"] = {"requirement": "host_owned"}
    with pytest.raises(ValidationError):
        validator.validate(missing_owner)  # host_owned must name its owner host and refs


def test_criterion1_typed_io_is_required_and_must_be_a_self_contained_object_schema() -> None:
    schema_validator = _validator("plugin-catalog.v1.schema.json")
    no_schema = _catalog()
    del _viewer_tool(no_schema, "tasks.list")["input_schema"]
    with pytest.raises(ValidationError):
        schema_validator.validate(no_schema)  # input/output schemas are mandatory

    non_object = _rehash_catalog(_catalog())
    _viewer_tool(non_object, "tasks.list")["input_schema"] = {"type": "string"}
    _rehash_catalog(non_object)
    with pytest.raises(ContractValidationError, match="must be a typed object schema"):
        validate_plugin_catalog(non_object)  # a generic non-object input is refused

    remote_ref = _catalog()
    _viewer_tool(remote_ref, "tasks.list")["input_schema"] = {
        "type": "object", "properties": {"x": {"$ref": "https://evil.example/schema.json"}},
    }
    _rehash_catalog(remote_ref)
    with pytest.raises(ContractValidationError, match="non-local"):
        validate_plugin_catalog(remote_ref)  # a tool schema cannot fetch a remote $ref


# --------------------------------------------------------------------------- #
# Criterion 2: effect classes and gates are mandatory and machine-checkable for
# every tool; an effect-capable tool is preview/approval-shaped.
# --------------------------------------------------------------------------- #


def test_criterion2_effect_and_gates_are_mandatory_on_every_tool() -> None:
    validator = _validator("plugin-catalog.v1.schema.json")
    for field in ("effect", "gates"):
        catalog = _catalog()
        del _viewer_tool(catalog, "tasks.list")[field]
        with pytest.raises(ValidationError):
            validator.validate(catalog)


def test_criterion2_no_policy_or_code_execution_effect_class_exists() -> None:
    validator = _validator("plugin-catalog.v1.schema.json")
    for forbidden in ("policy_mutation", "code_execution", "bounded_execution", "shell"):
        catalog = _catalog()
        _viewer_tool(catalog, "tasks.list")["effect"] = forbidden
        with pytest.raises(ValidationError):
            validator.validate(catalog)  # a plugin tool can never change policy or run code


def test_criterion2_effect_capable_tool_must_be_preview_and_approval_shaped() -> None:
    unapproved = _catalog()
    gates = _notifier_tool(unapproved)["gates"]
    gates["human_approval"] = "not_required"
    gates["approval_action"] = None
    _rehash_catalog(unapproved)
    with pytest.raises(ContractValidationError, match="must require a hash-bound approval"):
        validate_plugin_catalog(unapproved)

    unpreviewable = _catalog()
    _notifier_tool(unpreviewable)["gates"]["preview"] = "not_supported"
    _rehash_catalog(unpreviewable)
    with pytest.raises(ContractValidationError, match="must support a preview"):
        validate_plugin_catalog(unpreviewable)


def test_criterion2_read_tool_cannot_silently_carry_an_effect_gate() -> None:
    gated_read = _catalog()
    gates = _viewer_tool(gated_read, "tasks.list")["gates"]
    gates["human_approval"] = "required"
    gates["approval_action"] = "invoke_effect_tool"
    _rehash_catalog(gated_read)
    with pytest.raises(ContractValidationError, match="read plugin tool must be ungated"):
        validate_plugin_catalog(gated_read)


def test_criterion2_gate_set_is_internally_coherent() -> None:
    validator = _validator("plugin-catalog.v1.schema.json")
    # approval required but no action -> schema-invalid (machine-checkable gate).
    incoherent = _catalog()
    incoherent_gates = _notifier_tool(incoherent)["gates"]
    incoherent_gates["approval_action"] = None
    with pytest.raises(ValidationError):
        validator.validate(incoherent)
    # a not-required approval cannot name an action.
    misgated = _catalog()
    misgated_gates = _viewer_tool(misgated, "tasks.list")["gates"]
    misgated_gates["approval_action"] = "invoke_effect_tool"
    with pytest.raises(ValidationError):
        validator.validate(misgated)


# --------------------------------------------------------------------------- #
# Criterion 3: skills and provider operations have explicit non-equivalent types
# to plugin tools.
# --------------------------------------------------------------------------- #


def test_criterion3_plugin_tool_has_an_explicit_type_discriminator() -> None:
    validator = _validator("plugin-catalog.v1.schema.json")
    no_kind = _catalog()
    del _viewer_tool(no_kind, "tasks.list")["tool_kind"]
    with pytest.raises(ValidationError):
        validator.validate(no_kind)  # tool_kind is mandatory

    for foreign in ("skill", "operation", "provider_operation", "bridge_adapter"):
        catalog = _catalog()
        _viewer_tool(catalog, "tasks.list")["tool_kind"] = foreign
        with pytest.raises(ValidationError):
            validator.validate(catalog)  # a plugin tool is not a skill or an operation


def test_criterion3_a_provider_operation_is_not_a_valid_plugin_tool() -> None:
    plugin_catalog_validator = _validator("plugin-catalog.v1.schema.json")
    operation = _load(EXAMPLES / "anvil-state.catalog.v1.json")["operations"][2]
    catalog = _catalog()
    # Splicing a provider operation in where a plugin tool belongs is refused:
    # the operation carries execution/operation_digest and no tool_kind.
    next(p for p in catalog["plugins"] if p["id"] == "anvil-tasks-viewer")["tools"][0] = operation
    with pytest.raises(ValidationError):
        plugin_catalog_validator.validate(catalog)


def test_criterion3_a_plugin_tool_is_not_a_valid_provider_operation() -> None:
    operation_validator = _validator("operation-catalog.v1.schema.json")
    tool = _viewer_tool(_catalog(), "tasks.list")
    operation_catalog = copy.deepcopy(_load(EXAMPLES / "anvil-state.catalog.v1.json"))
    operation_catalog["operations"][0] = tool
    with pytest.raises(ValidationError):
        operation_validator.validate(operation_catalog)  # a plugin tool has no execution/operation_digest


def test_criterion3_schema_version_namespaces_are_distinct() -> None:
    plugin_catalog = _load(SCHEMAS / "plugin-catalog.v1.schema.json")
    operation_catalog = _load(SCHEMAS / "operation-catalog.v1.schema.json")
    assert plugin_catalog["properties"]["schema_version"]["const"] == "workbench-plugin-catalog/v1"
    assert operation_catalog["properties"]["schema_version"]["const"] == "anvil-operation-catalog/v1"


def test_criterion3_read_only_connector_is_constrained_to_read_and_pins_openapi() -> None:
    effectful_connector = _catalog()
    _viewer_tool(effectful_connector, "issues.read")["effect"] = "external_effect"
    # keep it schema-valid: an effect-capable tool needs a preview+approval gate.
    connector_gates = _viewer_tool(effectful_connector, "issues.read")["gates"]
    connector_gates["preview"] = "required"
    connector_gates["human_approval"] = "required"
    connector_gates["approval_action"] = "invoke_effect_tool"
    _rehash_catalog(effectful_connector)
    with pytest.raises(ContractValidationError, match="read-only connector tool must declare the read effect"):
        validate_plugin_catalog(effectful_connector)

    unpinned = _catalog()
    del next(p for p in unpinned["plugins"] if p["id"] == "anvil-tasks-viewer")["openapi_source"]
    _rehash_catalog(unpinned)
    with pytest.raises(ContractValidationError, match="must pin its openapi_source"):
        validate_plugin_catalog(unpinned)


# --------------------------------------------------------------------------- #
# Catalog digesting: tamper-evident, order-independent across plugins.
# --------------------------------------------------------------------------- #


def test_catalog_digest_is_deterministic_order_independent_and_content_sensitive() -> None:
    catalog = _catalog()
    advertised = catalog["catalog_digest"]
    assert contract_digest("plugin-catalog", catalog) == advertised

    reordered = copy.deepcopy(catalog)
    reordered["plugins"] = list(reversed(reordered["plugins"]))
    reordered["generated_at"] = "2027-01-01T00:00:00Z"
    assert contract_digest("plugin-catalog", reordered) == advertised

    changed = copy.deepcopy(catalog)
    changed["plugins"][0]["tools"][0]["summary"] += "!"
    assert contract_digest("plugin-catalog", changed) != advertised


def test_catalog_validator_fails_closed_on_a_tampered_plugin_or_catalog_digest() -> None:
    tampered_plugin = _catalog()
    tampered_plugin["plugins"][0]["tools"][0]["summary"] += "!"
    with pytest.raises(ContractValidationError, match="plugin digest mismatch"):
        validate_plugin_catalog(tampered_plugin)

    tampered_catalog = _catalog()
    for plugin in tampered_catalog["plugins"]:
        plugin["plugin_digest"] = contract_digest("plugin", plugin)
    tampered_catalog["catalog_digest"] = "sha256:" + "0" * 64
    with pytest.raises(ContractValidationError, match="plugin catalog digest mismatch"):
        validate_plugin_catalog(tampered_catalog)


def test_catalog_refuses_duplicate_plugin_and_tool_ids() -> None:
    dup_plugin = _catalog()
    dup_plugin["plugins"].append(copy.deepcopy(dup_plugin["plugins"][0]))
    _rehash_catalog(dup_plugin)
    with pytest.raises(ContractValidationError, match="duplicate plugin id"):
        validate_plugin_catalog(dup_plugin)

    dup_tool = _catalog()
    viewer = next(p for p in dup_tool["plugins"] if p["id"] == "anvil-tasks-viewer")
    viewer["tools"].append(copy.deepcopy(viewer["tools"][0]))
    _rehash_catalog(dup_tool)
    with pytest.raises(ContractValidationError, match="duplicate tool id"):
        validate_plugin_catalog(dup_tool)


# --------------------------------------------------------------------------- #
# Capability profile: an enable-only, digest-bound allowlist of installed plugins.
# --------------------------------------------------------------------------- #


def test_capability_profile_is_enable_only_and_digest_bound() -> None:
    profile = _capability()
    validate_plugin_capability(profile)
    assert profile["binding"] == "enable_only"
    assert contract_digest("plugin-capability", profile) == profile["digest"]

    reordered = copy.deepcopy(profile)
    reordered["plugins"] = list(reversed(reordered["plugins"]))
    reordered["plugins"][0]["enabled_tools"] = list(reversed(reordered["plugins"][0]["enabled_tools"]))
    assert contract_digest("plugin-capability", reordered) == profile["digest"]


def test_capability_profile_binding_cannot_be_widened_to_install_or_grant() -> None:
    validator = _validator("plugin-capability.v1.schema.json")
    for widened in ("install", "grant", "install_or_enable"):
        profile = _capability()
        profile["binding"] = widened
        with pytest.raises(ValidationError):
            validator.validate(profile)  # binding is the const enable_only


def test_capability_profile_fails_closed_on_tamper_and_over_limit() -> None:
    tampered = _capability()
    tampered["plugins"][0]["enabled_tools"] = ["tasks.list"]
    with pytest.raises(ContractValidationError, match="digest mismatch"):
        validate_plugin_capability(tampered)

    over_limit = _capability()
    over_limit["limits"]["max_enabled_tools"] = 1
    over_limit["digest"] = contract_digest("plugin-capability", over_limit)
    with pytest.raises(ContractValidationError, match="more tools than its declared limit"):
        validate_plugin_capability(over_limit)

    duplicated = _capability()
    duplicated["plugins"].append(copy.deepcopy(duplicated["plugins"][0]))
    duplicated["digest"] = contract_digest("plugin-capability", duplicated)
    with pytest.raises(ContractValidationError, match="duplicate plugin in capability profile"):
        validate_plugin_capability(duplicated)


# --------------------------------------------------------------------------- #
# Requests: ids/typed-inputs only, digest idempotency key, R003 approval floor.
# --------------------------------------------------------------------------- #


def test_request_carries_ids_and_typed_inputs_only() -> None:
    validator = _validator("plugin-request.v1.schema.json")
    for smuggled in ("worktree_path", "command", "credential_value", "source_url"):
        request = _tool_call_request()
        request[smuggled] = "x"
        with pytest.raises(ValidationError):
            validator.validate(request)  # the closed root refuses a path/command/token/source

    lifecycle_source = _install_request()
    lifecycle_source["lifecycle"]["source_url"] = "https://evil.example/plugin.tgz"
    with pytest.raises(ValidationError):
        validator.validate(lifecycle_source)  # a lifecycle target is resolved from the catalog, never fetched


def test_request_digest_is_the_idempotency_key_and_tamper_evident() -> None:
    request = _tool_call_request()
    validate_plugin_request(request)
    assert contract_digest("plugin-request", request) == request["request_digest"]

    mutated = copy.deepcopy(request)
    mutated["tool_call"]["inputs"]["status"] = "done"
    assert contract_digest("plugin-request", mutated) != request["request_digest"]

    tampered = _tool_call_request()
    tampered["tool_call"]["inputs"]["status"] = "done"  # digest no longer recomputes
    with pytest.raises(ContractValidationError, match="digest mismatch"):
        validate_plugin_request(tampered)


def test_install_request_requires_a_hash_bound_approval_binding_the_exact_subject() -> None:
    validator = _validator("plugin-request.v1.schema.json")
    no_approval = _install_request()
    del no_approval["approval"]
    with pytest.raises(ValidationError):
        validator.validate(no_approval)  # R003 floor: install always carries an approval

    validate_plugin_request(_install_request())  # baseline binds its subject

    wrong_action = _install_request()
    wrong_action["approval"]["action"] = "upgrade_plugin"
    _rehash_request(wrong_action)
    with pytest.raises(ContractValidationError, match="approval action does not match the lifecycle kind"):
        validate_plugin_request(wrong_action)

    wrong_hash = _install_request()
    wrong_hash["approval"]["payload_hash"] = "sha256:" + "0" * 64
    _rehash_request(wrong_hash)
    with pytest.raises(ContractValidationError, match="does not bind the exact plugin/version subject"):
        validate_plugin_request(wrong_hash)

    # The binding is content-sensitive: a different target version breaks it.
    retargeted = _install_request()
    retargeted["lifecycle"]["target_version"] = "9.9.9"
    _rehash_request(retargeted)
    with pytest.raises(ContractValidationError, match="does not bind the exact plugin/version subject"):
        validate_plugin_request(retargeted)


def test_tool_call_inputs_are_validated_against_the_reviewed_tool_schema() -> None:
    catalog = _catalog()
    validate_plugin_request(_tool_call_request(), catalog)  # baseline resolves and type-checks

    bad_value = _tool_call_request()
    bad_value["tool_call"]["inputs"]["status"] = "not_a_status"
    _rehash_request(bad_value)
    with pytest.raises(ContractValidationError, match="do not match the selected tool schema"):
        validate_plugin_request(bad_value, catalog)

    smuggled_field = _tool_call_request()
    smuggled_field["tool_call"]["inputs"]["command"] = "do-a-thing"
    _rehash_request(smuggled_field)
    with pytest.raises(ContractValidationError, match="do not match the selected tool schema"):
        validate_plugin_request(smuggled_field, catalog)  # the tool input schema is closed

    drifted_digest = _tool_call_request()
    drifted_digest["plugin"]["plugin_digest"] = "sha256:" + "0" * 64
    _rehash_request(drifted_digest)
    with pytest.raises(ContractValidationError, match="not present at the pinned catalog digest"):
        validate_plugin_request(drifted_digest, catalog)

    unknown_tool = _tool_call_request()
    unknown_tool["tool_call"]["tool_id"] = "tasks.delete"
    _rehash_request(unknown_tool)
    with pytest.raises(ContractValidationError, match="tool is not present in the pinned plugin"):
        validate_plugin_request(unknown_tool, catalog)


# --------------------------------------------------------------------------- #
# Preview: echoes its request, carries the hash-bound approval, redaction-only.
# --------------------------------------------------------------------------- #


def test_preview_echoes_its_request_and_binds_the_approval_subject() -> None:
    preview = _preview()
    request = _install_request()
    validate_plugin_preview(preview, request)
    assert preview["request_digest"] == request["request_digest"]
    assert preview["approval"]["payload_hash"] == approval_payload_digest(
        contracts_module._plugin_approval_subject(request)
    )

    wrong_request = _preview()
    wrong_request["request_digest"] = "sha256:" + "0" * 64
    with pytest.raises(ContractValidationError, match="does not echo the previewed request digest"):
        validate_plugin_preview(wrong_request, request)

    wrong_hash = _preview()
    wrong_hash["approval"]["payload_hash"] = "sha256:" + "0" * 64
    with pytest.raises(ContractValidationError, match="does not bind the exact request subject"):
        validate_plugin_preview(wrong_hash, request)


def test_lifecycle_preview_must_require_a_hash_bound_approval() -> None:
    validator = _validator("plugin-preview.v1.schema.json")
    # approval.required:true with no action/payload_hash is schema-invalid.
    incomplete = _preview()
    del incomplete["approval"]["payload_hash"]
    with pytest.raises(ValidationError):
        validator.validate(incomplete)

    # a lifecycle preview that declares approval optional is refused by the validator.
    waived = _preview()
    waived["approval"] = {"required": False}
    with pytest.raises(ContractValidationError, match="preview must require a hash-bound approval"):
        validate_plugin_preview(waived)


def test_preview_prose_is_bounded_and_non_leaking() -> None:
    validator = _validator("plugin-preview.v1.schema.json")
    for leak in (
        "install from https://evil.example/x",
        "writes to c:/creds/store",
        "authorization: Bearer sk-ant-api03-REALKEY",
    ):
        preview = _preview()
        preview["summary"] = leak
        with pytest.raises(ValidationError):
            validator.validate(preview)


# --------------------------------------------------------------------------- #
# Receipt: opaque credential refs only, echoes its request, typed outcome union.
# --------------------------------------------------------------------------- #


def test_receipt_reports_credentials_by_reference_only_never_a_value() -> None:
    validator = _validator("plugin-receipt.v1.schema.json")
    with_value = _receipt()
    with_value["credential_use"]["value"] = "hunter2"
    with pytest.raises(ValidationError):
        validator.validate(with_value)  # credential_use is a closed reference block


def test_receipt_echoes_its_request_and_scopes_to_the_same_plugin_and_tool() -> None:
    receipt = _receipt()
    request = _tool_call_request()
    validate_plugin_receipt(receipt, request)
    assert receipt["request_digest"] == request["request_digest"]

    wrong_digest = _receipt()
    wrong_digest["request_digest"] = "sha256:" + "0" * 64
    with pytest.raises(ContractValidationError, match="does not echo the request digest"):
        validate_plugin_receipt(wrong_digest, request)

    wrong_tool = _receipt()
    wrong_tool["tool_id"] = "issues.read"
    with pytest.raises(ContractValidationError, match="names a different tool than the request"):
        validate_plugin_receipt(wrong_tool, request)

    wrong_plugin = _receipt()
    wrong_plugin["plugin"]["plugin_id"] = "deploy-notifier"
    with pytest.raises(ContractValidationError, match="scopes to a different plugin"):
        validate_plugin_receipt(wrong_plugin, request)


def test_receipt_outcome_union_is_typed_and_a_denied_start_is_a_refusal() -> None:
    validator = _validator("plugin-receipt.v1.schema.json")
    contradictory = _receipt()
    contradictory["error"] = {"code": "x.y", "safe_summary": "no", "retryable": False}
    with pytest.raises(ValidationError):
        validator.validate(contradictory)  # an accepted receipt cannot also be an error

    refusal = _refusal_receipt()
    validate_plugin_receipt(refusal)
    assert refusal["status"] == "denied"
    assert refusal["error"]["retryable"] is False
    assert refusal["redaction"]["status"] == "metadata_only"

    with_result = _refusal_receipt()
    with_result["result"] = {"output_digest": "sha256:" + "a" * 64}
    with pytest.raises(ValidationError):
        validator.validate(with_result)  # a denied receipt carries no result


def test_receipt_reconcile_status_reports_an_unknown_outcome() -> None:
    validator = _validator("plugin-receipt.v1.schema.json")
    reconcile = _refusal_receipt()
    reconcile["status"] = "reconcile"
    del reconcile["error"]
    reconcile["reconciliation"] = {"code": "external.unknown", "safe_summary": "The external call outcome is unknown; reconciling."}
    validator.validate(reconcile)  # R012: an in-flight external effect routes to reconciliation


# --------------------------------------------------------------------------- #
# Digest-kind registration and float-domain guards, mirroring siblings.
# --------------------------------------------------------------------------- #


def test_plugin_digest_kinds_are_registered() -> None:
    assert contracts_module._PREFIXES["plugin-catalog"] == b"anvil-workbench/plugin-catalog/v1\0"
    assert contracts_module._PREFIXES["plugin"] == b"anvil-workbench/plugin/v1\0"
    assert contracts_module._PREFIXES["plugin-capability"] == b"anvil-workbench/plugin-capability/v1\0"
    assert contracts_module._PREFIXES["plugin-request"] == b"anvil-workbench/plugin-request/v1\0"


def test_plugin_digest_domain_rejects_floats() -> None:
    with pytest.raises(ContractValidationError, match="floating-point"):
        contract_digest("plugin-request", {"tool_call": {"inputs": {"weight": 0.5}}})
    with pytest.raises(ContractValidationError, match="floating-point"):
        contract_digest("plugin-catalog", {"plugins": [{"weight": 0.25}]})


# --------------------------------------------------------------------------- #
# Trust-root guards: the loaders fail closed when the on-disk schema is absent
# or has drifted open. Cache resets run in try/finally so a drifted validator
# never cascades into unrelated tests.
# --------------------------------------------------------------------------- #


def test_plugin_catalog_validator_trust_root_fails_closed(monkeypatch, tmp_path) -> None:
    contracts_module._reset_plugin_catalog_contract_validator_cache()
    try:
        monkeypatch.setattr(
            contracts_module, "_PLUGIN_CATALOG_CONTRACT_SCHEMA_PATH", tmp_path / "absent.schema.json"
        )
        with pytest.raises(ContractValidationError, match="schema is unavailable"):
            contracts_module.plugin_catalog_contract_validator()

        base = _load(SCHEMAS / "plugin-catalog.v1.schema.json")
        del base["$defs"]["tool"]["additionalProperties"]
        drifted = tmp_path / "drifted-catalog.schema.json"
        drifted.write_text(json.dumps(base), encoding="utf-8")
        contracts_module._reset_plugin_catalog_contract_validator_cache()
        monkeypatch.setattr(contracts_module, "_PLUGIN_CATALOG_CONTRACT_SCHEMA_PATH", drifted)
        with pytest.raises(ContractValidationError, match="no longer closes its tool object"):
            contracts_module.plugin_catalog_contract_validator()
    finally:
        contracts_module._reset_plugin_catalog_contract_validator_cache()


def test_plugin_request_validator_trust_root_fails_closed(monkeypatch, tmp_path) -> None:
    contracts_module._reset_plugin_request_contract_validator_cache()
    try:
        base = _load(SCHEMAS / "plugin-request.v1.schema.json")
        del base["properties"]["approval"]["additionalProperties"]
        drifted = tmp_path / "drifted-request.schema.json"
        drifted.write_text(json.dumps(base), encoding="utf-8")
        monkeypatch.setattr(contracts_module, "_PLUGIN_REQUEST_CONTRACT_SCHEMA_PATH", drifted)
        with pytest.raises(ContractValidationError, match="no longer closes its approval object"):
            contracts_module.plugin_request_contract_validator()
    finally:
        contracts_module._reset_plugin_request_contract_validator_cache()


def test_plugin_receipt_validator_trust_root_fails_closed(monkeypatch, tmp_path) -> None:
    contracts_module._reset_plugin_receipt_contract_validator_cache()
    try:
        base = _load(SCHEMAS / "plugin-receipt.v1.schema.json")
        del base["properties"]["credential_use"]["additionalProperties"]
        drifted = tmp_path / "drifted-receipt.schema.json"
        drifted.write_text(json.dumps(base), encoding="utf-8")
        monkeypatch.setattr(contracts_module, "_PLUGIN_RECEIPT_CONTRACT_SCHEMA_PATH", drifted)
        with pytest.raises(ContractValidationError, match="no longer closes its credential_use object"):
            contracts_module.plugin_receipt_contract_validator()
    finally:
        contracts_module._reset_plugin_receipt_contract_validator_cache()


# --------------------------------------------------------------------------- #
# Digest-bearing timestamps and identifier fields must be pinned, not free text:
# the production validators install no FormatChecker, so `format` alone enforces
# nothing and maxLength+pattern must carry the whole load.
# --------------------------------------------------------------------------- #


def _errors_targeting(validator: Draft202012Validator, instance: dict, prop: str) -> list[ValidationError]:
    return [err for err in validator.iter_errors(instance) if prop in list(err.absolute_path)]


def test_catalog_generated_at_pinned_without_formatchecker() -> None:
    prod = contracts_module.plugin_catalog_contract_validator()
    assert prod.format_checker is None  # the hole the fix closes: no FormatChecker

    for bad in (
        "2026-07-19T00:00:00Z Bearer sk-ant-api03-REALKEY",
        "2026-07-19T00:00:00" + "0" * 5073 + "Z",
        "not-a-timestamp",
    ):
        catalog = _catalog()
        catalog["generated_at"] = bad
        errors = _errors_targeting(prod, catalog, "generated_at")
        assert errors, f"generated_at {bad!r} was not refused"
        assert all(err.validator in {"pattern", "maxLength"} for err in errors)


def test_request_created_at_pinned_without_formatchecker() -> None:
    prod = contracts_module.plugin_request_contract_validator()
    assert prod.format_checker is None

    for bad in (
        "2026-07-20T12:00:00Z Bearer sk-ant-api03-REALKEY; do-a-thing",
        "2026-07-20T12:00:00" + "0" * 5073 + "Z",
        "not-a-timestamp",
    ):
        request = _tool_call_request()
        request["created_at"] = bad
        request["request_digest"] = contract_digest("plugin-request", request)
        errors = _errors_targeting(prod, request, "created_at")
        assert errors, f"created_at {bad!r} was not refused"
        assert all(err.validator in {"pattern", "maxLength"} for err in errors)
        with pytest.raises(ContractValidationError, match="not schema valid"):
            validate_plugin_request(request)
