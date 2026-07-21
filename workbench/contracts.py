"""Deterministic helpers for proposed Workbench operation-layer resources.

The v1 bridge does not dispatch these resources yet.  Keeping the digest and
snapshot checks in a small stdlib-only module gives the V2 implementation one
authoritative, testable interpretation instead of each adapter inventing one.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError


class ContractValidationError(ValueError):
    """A proposed contract resource or immutable execution snapshot is unsafe."""


_CATALOG_CONTRACT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "contracts" / "schemas" / "operation-catalog.v1.schema.json"
)
_catalog_contract_validator_cache: Draft202012Validator | None = None


def catalog_contract_validator() -> Draft202012Validator:
    """Load the operation-catalog contract schema once; fail closed if absent.

    Shared by every catalog consumer (State manifest discovery, the provider
    catalog registry) so there is exactly one interpretation of the contract
    schema.  The ``generated_at`` bound guard refuses a schema edit that would
    let a provider smuggle unbounded content through the timestamp field.
    """
    global _catalog_contract_validator_cache
    if _catalog_contract_validator_cache is None:
        try:
            schema = json.loads(_CATALOG_CONTRACT_SCHEMA_PATH.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ContractValidationError(
                "operation-catalog contract schema is unavailable; refusing to validate catalogs"
            ) from exc
        bound = schema.get("properties", {}).get("generated_at", {})
        if not isinstance(bound.get("maxLength"), int) or "pattern" not in bound:
            raise ContractValidationError(
                "operation-catalog contract schema no longer bounds generated_at; "
                "refusing to validate catalogs"
            )
        _catalog_contract_validator_cache = Draft202012Validator(schema)
    return _catalog_contract_validator_cache


_PROFILE_CONTRACT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "contracts" / "schemas" / "capability-profile.v1.schema.json"
)
_profile_contract_validator_cache: Draft202012Validator | None = None


def profile_contract_validator() -> Draft202012Validator:
    """Load the capability-profile contract schema once; fail closed if absent.

    Shared by every profile consumer so there is exactly one interpretation of
    the contract schema.  The closed-object guard refuses a schema edit that
    would reopen the profile to unreviewed extension fields: a profile must
    stay a closed allowlist, never an extensible envelope.
    """
    global _profile_contract_validator_cache
    if _profile_contract_validator_cache is None:
        try:
            schema = json.loads(_PROFILE_CONTRACT_SCHEMA_PATH.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ContractValidationError(
                "capability-profile contract schema is unavailable; refusing to validate profiles"
            ) from exc
        closed_paths = (
            (),
            ("properties", "operations", "items"),
            ("properties", "skills", "items"),
            ("properties", "limits"),
        )
        for path in closed_paths:
            node = schema
            for name in path:
                node = node.get(name) if isinstance(node, dict) else None
            if not isinstance(node, dict) or node.get("additionalProperties") is not False:
                raise ContractValidationError(
                    "capability-profile contract schema no longer closes its objects "
                    f"(additionalProperties must be false at {'/'.join(path) or '<root>'}); "
                    "refusing to validate profiles"
                )
        _profile_contract_validator_cache = Draft202012Validator(schema)
    return _profile_contract_validator_cache


def _reset_profile_contract_validator_cache() -> None:
    """Test hook: force the next profile validation to reload the on-disk schema."""
    global _profile_contract_validator_cache
    _profile_contract_validator_cache = None


_WORKFLOW_CONTRACT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "contracts" / "schemas" / "workflow.v2.schema.json"
)
_workflow_contract_validator_cache: Draft202012Validator | None = None


def workflow_contract_validator() -> Draft202012Validator:
    """Load the workflow v2 contract schema once; fail closed if absent.

    Shared by every workflow consumer (snapshot compilation, future queueing)
    so there is exactly one interpretation of the contract schema.  The
    closed-root and bounded-steps guards refuse a schema edit that would let a
    workflow smuggle unreviewed extension fields or an unbounded step list.
    """
    global _workflow_contract_validator_cache
    if _workflow_contract_validator_cache is None:
        try:
            schema = json.loads(_WORKFLOW_CONTRACT_SCHEMA_PATH.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ContractValidationError(
                "workflow contract schema is unavailable; refusing to validate workflows"
            ) from exc
        steps = schema.get("properties", {}).get("steps", {})
        if schema.get("additionalProperties") is not False or not isinstance(steps.get("maxItems"), int):
            raise ContractValidationError(
                "workflow contract schema no longer closes its root object or bounds steps; "
                "refusing to validate workflows"
            )
        defs = schema.get("$defs", {})
        for name in ("operation_ref", "operation_step", "agent_step", "approval_step", "control_step"):
            node = defs.get(name)
            if not isinstance(node, dict) or node.get("additionalProperties") is not False:
                raise ContractValidationError(
                    f"workflow contract schema no longer closes its {name} object; "
                    "refusing to validate workflows"
                )
        _workflow_contract_validator_cache = Draft202012Validator(schema)
    return _workflow_contract_validator_cache


def _reset_workflow_contract_validator_cache() -> None:
    """Test hook: force the next workflow validation to reload the on-disk schema."""
    global _workflow_contract_validator_cache
    _workflow_contract_validator_cache = None


_SETTINGS_DESCRIPTOR_CONTRACT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "contracts" / "schemas" / "settings-descriptor.v1.schema.json"
)
_settings_descriptor_contract_validator_cache: Draft202012Validator | None = None


def settings_descriptor_contract_validator() -> Draft202012Validator:
    """Load the settings-descriptor contract schema once; fail closed if absent.

    Shared by every settings-descriptor consumer (the resolver, the actor/export
    projection, future Settings APIs) so there is exactly one interpretation of
    the contract schema.  The closed-root and closed-descriptor guards refuse a
    schema edit that would reopen the catalog or a descriptor to unreviewed
    extension fields: a descriptor must stay a closed, typed record, never an
    extensible envelope through which a secret or a raw path could ride in.
    """
    global _settings_descriptor_contract_validator_cache
    if _settings_descriptor_contract_validator_cache is None:
        try:
            schema = json.loads(_SETTINGS_DESCRIPTOR_CONTRACT_SCHEMA_PATH.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ContractValidationError(
                "settings-descriptor contract schema is unavailable; refusing to validate descriptors"
            ) from exc
        if schema.get("additionalProperties") is not False:
            raise ContractValidationError(
                "settings-descriptor contract schema no longer closes its root object; "
                "refusing to validate descriptors"
            )
        descriptor = schema.get("$defs", {}).get("descriptor")
        if not isinstance(descriptor, dict) or descriptor.get("additionalProperties") is not False:
            raise ContractValidationError(
                "settings-descriptor contract schema no longer closes its descriptor object; "
                "refusing to validate descriptors"
            )
        settings = schema.get("properties", {}).get("settings", {})
        if not isinstance(settings.get("maxItems"), int):
            raise ContractValidationError(
                "settings-descriptor contract schema no longer bounds its settings list; "
                "refusing to validate descriptors"
            )
        _settings_descriptor_contract_validator_cache = Draft202012Validator(schema)
    return _settings_descriptor_contract_validator_cache


def _reset_settings_descriptor_contract_validator_cache() -> None:
    """Test hook: force the next descriptor validation to reload the on-disk schema."""
    global _settings_descriptor_contract_validator_cache
    _settings_descriptor_contract_validator_cache = None


_ADVANCED_BRANCH_CONTRACT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "contracts" / "schemas" / "advanced-branch.v1.schema.json"
)
_advanced_branch_contract_validator_cache: Draft202012Validator | None = None


def advanced_branch_contract_validator() -> Draft202012Validator:
    """Load the advanced-branch contract schema once; fail closed if absent.

    Shared by every Advanced-branch consumer so there is exactly one
    interpretation of the contract schema.  The closed-root and
    closed-control-descriptor guards refuse a schema edit that would reopen the
    branch or a control descriptor to unreviewed extension fields: an Advanced
    branch must stay a closed reference-and-control record, never an extensible
    envelope through which a raw endpoint, a second conversation identity, or an
    undeclared control could ride in.
    """
    global _advanced_branch_contract_validator_cache
    if _advanced_branch_contract_validator_cache is None:
        try:
            schema = json.loads(_ADVANCED_BRANCH_CONTRACT_SCHEMA_PATH.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ContractValidationError(
                "advanced-branch contract schema is unavailable; refusing to validate branches"
            ) from exc
        if schema.get("additionalProperties") is not False:
            raise ContractValidationError(
                "advanced-branch contract schema no longer closes its root object; "
                "refusing to validate branches"
            )
        descriptor = schema.get("$defs", {}).get("controlDescriptor")
        if not isinstance(descriptor, dict) or descriptor.get("additionalProperties") is not False:
            raise ContractValidationError(
                "advanced-branch contract schema no longer closes its control descriptor; "
                "refusing to validate branches"
            )
        _advanced_branch_contract_validator_cache = Draft202012Validator(schema)
    return _advanced_branch_contract_validator_cache


def _reset_advanced_branch_contract_validator_cache() -> None:
    """Test hook: force the next branch validation to reload the on-disk schema."""
    global _advanced_branch_contract_validator_cache
    _advanced_branch_contract_validator_cache = None


_ADVANCED_PRESET_CONTRACT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "contracts" / "schemas" / "advanced-preset.v1.schema.json"
)
_advanced_preset_contract_validator_cache: Draft202012Validator | None = None


def advanced_preset_contract_validator() -> Draft202012Validator:
    """Load the advanced-preset contract schema once; fail closed if absent.

    Shared by every Advanced-preset consumer so there is exactly one
    interpretation of the contract schema.  The closed-root and closed-repair
    guards refuse a schema edit that would reopen the preset or its repair block
    to unreviewed extension fields: a preset must stay a closed, digest-pinned
    record whose drift/repair state is a bounded typed enumeration, never an
    extensible envelope that could silently substitute a route or tool.
    """
    global _advanced_preset_contract_validator_cache
    if _advanced_preset_contract_validator_cache is None:
        try:
            schema = json.loads(_ADVANCED_PRESET_CONTRACT_SCHEMA_PATH.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ContractValidationError(
                "advanced-preset contract schema is unavailable; refusing to validate presets"
            ) from exc
        if schema.get("additionalProperties") is not False:
            raise ContractValidationError(
                "advanced-preset contract schema no longer closes its root object; "
                "refusing to validate presets"
            )
        repair = schema.get("properties", {}).get("repair", {})
        if not isinstance(repair, dict) or repair.get("additionalProperties") is not False:
            raise ContractValidationError(
                "advanced-preset contract schema no longer closes its repair block; "
                "refusing to validate presets"
            )
        _advanced_preset_contract_validator_cache = Draft202012Validator(schema)
    return _advanced_preset_contract_validator_cache


def _reset_advanced_preset_contract_validator_cache() -> None:
    """Test hook: force the next preset validation to reload the on-disk schema."""
    global _advanced_preset_contract_validator_cache
    _advanced_preset_contract_validator_cache = None


_TASK_REFERENCE_CONTRACT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "contracts" / "schemas" / "task-reference.v1.schema.json"
)
_task_reference_contract_validator_cache: Draft202012Validator | None = None


def task_reference_contract_validator() -> Draft202012Validator:
    """Load the task-reference contract schema once; fail closed if absent.

    Shared by every task-reference consumer (the Project surface, a task row or
    detail view, the Deliver flow) so there is exactly one interpretation of the
    contract schema.  The closed-root and closed-reference guards refuse a schema
    edit that would reopen the reference or its owning-PRD/source blocks to
    unreviewed extension fields: a task reference must stay a closed record that
    always names its owning PRD and pinned source, never an extensible envelope
    through which a bare task id or a raw path could ride in.
    """
    global _task_reference_contract_validator_cache
    if _task_reference_contract_validator_cache is None:
        try:
            schema = json.loads(_TASK_REFERENCE_CONTRACT_SCHEMA_PATH.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ContractValidationError(
                "task-reference contract schema is unavailable; refusing to validate references"
            ) from exc
        if schema.get("additionalProperties") is not False:
            raise ContractValidationError(
                "task-reference contract schema no longer closes its root object; "
                "refusing to validate references"
            )
        for name in ("taskRef", "source"):
            node = schema.get("$defs", {}).get(name)
            if not isinstance(node, dict) or node.get("additionalProperties") is not False:
                raise ContractValidationError(
                    f"task-reference contract schema no longer closes its {name} object; "
                    "refusing to validate references"
                )
        _task_reference_contract_validator_cache = Draft202012Validator(schema)
    return _task_reference_contract_validator_cache


def _reset_task_reference_contract_validator_cache() -> None:
    """Test hook: force the next task-reference validation to reload the on-disk schema."""
    global _task_reference_contract_validator_cache
    _task_reference_contract_validator_cache = None


_DELIVERY_ELIGIBILITY_CONTRACT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "contracts" / "schemas" / "delivery-eligibility.v1.schema.json"
)
_delivery_eligibility_contract_validator_cache: Draft202012Validator | None = None


def delivery_eligibility_contract_validator() -> Draft202012Validator:
    """Load the delivery-eligibility contract schema once; fail closed if absent.

    Shared by every eligibility consumer so there is exactly one interpretation
    of the contract schema.  The closed-root and closed-reason guards refuse a
    schema edit that would reopen the verdict or a reason to unreviewed extension
    fields: an eligibility verdict must stay a closed record whose blocked/stale
    reasons are a bounded typed enumeration with a human-safe explanation, never
    an extensible envelope through which a raw error string could ride in.
    """
    global _delivery_eligibility_contract_validator_cache
    if _delivery_eligibility_contract_validator_cache is None:
        try:
            schema = json.loads(_DELIVERY_ELIGIBILITY_CONTRACT_SCHEMA_PATH.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ContractValidationError(
                "delivery-eligibility contract schema is unavailable; refusing to validate verdicts"
            ) from exc
        if schema.get("additionalProperties") is not False:
            raise ContractValidationError(
                "delivery-eligibility contract schema no longer closes its root object; "
                "refusing to validate verdicts"
            )
        reason = schema.get("properties", {}).get("reasons", {}).get("items", {})
        if not isinstance(reason, dict) or reason.get("additionalProperties") is not False:
            raise ContractValidationError(
                "delivery-eligibility contract schema no longer closes its reason object; "
                "refusing to validate verdicts"
            )
        _delivery_eligibility_contract_validator_cache = Draft202012Validator(schema)
    return _delivery_eligibility_contract_validator_cache


def _reset_delivery_eligibility_contract_validator_cache() -> None:
    """Test hook: force the next eligibility validation to reload the on-disk schema."""
    global _delivery_eligibility_contract_validator_cache
    _delivery_eligibility_contract_validator_cache = None


_DELIVER_INTENT_CONTRACT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "contracts" / "schemas" / "deliver-intent.v1.schema.json"
)
_deliver_intent_contract_validator_cache: Draft202012Validator | None = None


def deliver_intent_contract_validator() -> Draft202012Validator:
    """Load the Deliver-intent contract schema once; fail closed if absent.

    Shared by every Deliver-intent consumer so there is exactly one
    interpretation of the contract schema.  The closed-root and closed-selections
    guards refuse a schema edit that would reopen the intent or its selections to
    unreviewed extension fields: a Deliver intent must stay a closed ids-only
    record, never an extensible envelope through which a path, a raw command, a
    token, or an executable workflow body could ride in.
    """
    global _deliver_intent_contract_validator_cache
    if _deliver_intent_contract_validator_cache is None:
        try:
            schema = json.loads(_DELIVER_INTENT_CONTRACT_SCHEMA_PATH.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ContractValidationError(
                "deliver-intent contract schema is unavailable; refusing to validate intents"
            ) from exc
        if schema.get("additionalProperties") is not False:
            raise ContractValidationError(
                "deliver-intent contract schema no longer closes its root object; "
                "refusing to validate intents"
            )
        selections = schema.get("properties", {}).get("selections", {})
        if not isinstance(selections, dict) or selections.get("additionalProperties") is not False:
            raise ContractValidationError(
                "deliver-intent contract schema no longer closes its selections object; "
                "refusing to validate intents"
            )
        task_ref = schema.get("$defs", {}).get("taskRef")
        if not isinstance(task_ref, dict) or task_ref.get("additionalProperties") is not False:
            raise ContractValidationError(
                "deliver-intent contract schema no longer closes its taskRef object; "
                "refusing to validate intents"
            )
        _deliver_intent_contract_validator_cache = Draft202012Validator(schema)
    return _deliver_intent_contract_validator_cache


def _reset_deliver_intent_contract_validator_cache() -> None:
    """Test hook: force the next Deliver-intent validation to reload the on-disk schema."""
    global _deliver_intent_contract_validator_cache
    _deliver_intent_contract_validator_cache = None


_DELIVER_START_RECEIPT_CONTRACT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "contracts" / "schemas" / "deliver-start-receipt.v1.schema.json"
)
_deliver_start_receipt_contract_validator_cache: Draft202012Validator | None = None


def deliver_start_receipt_contract_validator() -> Draft202012Validator:
    """Load the Deliver start-receipt contract schema once; fail closed if absent.

    Shared by every start-receipt consumer so there is exactly one
    interpretation of the contract schema.  The closed-root guard refuses a
    schema edit that would reopen the receipt to unreviewed extension fields: a
    start receipt must stay a closed redacted record, never an extensible
    envelope through which an endpoint, a raw command, or a token could ride in.
    """
    global _deliver_start_receipt_contract_validator_cache
    if _deliver_start_receipt_contract_validator_cache is None:
        try:
            schema = json.loads(_DELIVER_START_RECEIPT_CONTRACT_SCHEMA_PATH.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ContractValidationError(
                "deliver-start-receipt contract schema is unavailable; refusing to validate receipts"
            ) from exc
        if schema.get("additionalProperties") is not False:
            raise ContractValidationError(
                "deliver-start-receipt contract schema no longer closes its root object; "
                "refusing to validate receipts"
            )
        task_ref = schema.get("$defs", {}).get("taskRef")
        if not isinstance(task_ref, dict) or task_ref.get("additionalProperties") is not False:
            raise ContractValidationError(
                "deliver-start-receipt contract schema no longer closes its taskRef object; "
                "refusing to validate receipts"
            )
        _deliver_start_receipt_contract_validator_cache = Draft202012Validator(schema)
    return _deliver_start_receipt_contract_validator_cache


def _reset_deliver_start_receipt_contract_validator_cache() -> None:
    """Test hook: force the next start-receipt validation to reload the on-disk schema."""
    global _deliver_start_receipt_contract_validator_cache
    _deliver_start_receipt_contract_validator_cache = None


_PLUGIN_CATALOG_CONTRACT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "contracts" / "schemas" / "plugin-catalog.v1.schema.json"
)
_plugin_catalog_contract_validator_cache: Draft202012Validator | None = None


def plugin_catalog_contract_validator() -> Draft202012Validator:
    """Load the plugin-catalog contract schema once; fail closed if absent.

    Shared by every plugin-catalog consumer so there is exactly one
    interpretation of the contract schema.  The closed-root, closed-plugin,
    closed-tool, and closed-gates guards refuse a schema edit that would reopen
    the catalog, a plugin, a tool descriptor, or its gate set to unreviewed
    extension fields: a plugin catalog must stay a closed, typed registry
    through which a raw command, endpoint, path, or credential value can never
    ride in, and every tool must keep its mandatory machine-checkable gate set.
    The ``generated_at`` bound guard refuses an edit that would let a provider
    smuggle unbounded content through the timestamp field.
    """
    global _plugin_catalog_contract_validator_cache
    if _plugin_catalog_contract_validator_cache is None:
        try:
            schema = json.loads(_PLUGIN_CATALOG_CONTRACT_SCHEMA_PATH.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ContractValidationError(
                "plugin-catalog contract schema is unavailable; refusing to validate catalogs"
            ) from exc
        if schema.get("additionalProperties") is not False:
            raise ContractValidationError(
                "plugin-catalog contract schema no longer closes its root object; "
                "refusing to validate catalogs"
            )
        defs = schema.get("$defs", {})
        for name in ("plugin", "tool", "gates", "credential", "hostAccess"):
            node = defs.get(name)
            if not isinstance(node, dict) or node.get("additionalProperties") is not False:
                raise ContractValidationError(
                    f"plugin-catalog contract schema no longer closes its {name} object; "
                    "refusing to validate catalogs"
                )
        # Extend the tripwire to the remaining closed objects nested inside the
        # root/plugin/tool, so a schema edit that reopened any of them (letting an
        # unreviewed field ride in) also fails closed.
        _plugin_def = defs.get("plugin", {}).get("properties", {}) if isinstance(defs.get("plugin"), dict) else {}
        _tool_def = defs.get("tool", {}).get("properties", {}) if isinstance(defs.get("tool"), dict) else {}
        _nested_closed = (
            ("provenance", schema.get("properties", {}).get("provenance")),
            ("publisher", _plugin_def.get("publisher")),
            ("runtime", _plugin_def.get("runtime")),
            ("openapi_source", _plugin_def.get("openapi_source")),
            ("idempotency", _tool_def.get("idempotency")),
        )
        for name, node in _nested_closed:
            if not isinstance(node, dict) or node.get("additionalProperties") is not False:
                raise ContractValidationError(
                    f"plugin-catalog contract schema no longer closes its {name} object; "
                    "refusing to validate catalogs"
                )
        bound = defs.get("rfc3339", {})
        if not isinstance(bound.get("maxLength"), int) or "pattern" not in bound:
            raise ContractValidationError(
                "plugin-catalog contract schema no longer bounds its timestamps; "
                "refusing to validate catalogs"
            )
        _plugin_catalog_contract_validator_cache = Draft202012Validator(schema)
    return _plugin_catalog_contract_validator_cache


def _reset_plugin_catalog_contract_validator_cache() -> None:
    """Test hook: force the next plugin-catalog validation to reload the on-disk schema."""
    global _plugin_catalog_contract_validator_cache
    _plugin_catalog_contract_validator_cache = None


_PLUGIN_CAPABILITY_CONTRACT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "contracts" / "schemas" / "plugin-capability.v1.schema.json"
)
_plugin_capability_contract_validator_cache: Draft202012Validator | None = None


def plugin_capability_contract_validator() -> Draft202012Validator:
    """Load the plugin-capability contract schema once; fail closed if absent.

    Shared by every plugin-capability consumer so there is exactly one
    interpretation of the contract schema.  The closed-root and closed-entry
    guards refuse a schema edit that would reopen the profile or a plugin
    allowlist entry to unreviewed extension fields: a plugin capability profile
    must stay a closed enable-only allowlist of installed, digest-pinned
    plugins, never an extensible envelope that could grant a new privilege.
    """
    global _plugin_capability_contract_validator_cache
    if _plugin_capability_contract_validator_cache is None:
        try:
            schema = json.loads(_PLUGIN_CAPABILITY_CONTRACT_SCHEMA_PATH.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ContractValidationError(
                "plugin-capability contract schema is unavailable; refusing to validate profiles"
            ) from exc
        if schema.get("additionalProperties") is not False:
            raise ContractValidationError(
                "plugin-capability contract schema no longer closes its root object; "
                "refusing to validate profiles"
            )
        entry = schema.get("properties", {}).get("plugins", {}).get("items", {})
        if not isinstance(entry, dict) or entry.get("additionalProperties") is not False:
            raise ContractValidationError(
                "plugin-capability contract schema no longer closes its plugin entry object; "
                "refusing to validate profiles"
            )
        _plugin_capability_contract_validator_cache = Draft202012Validator(schema)
    return _plugin_capability_contract_validator_cache


def _reset_plugin_capability_contract_validator_cache() -> None:
    """Test hook: force the next plugin-capability validation to reload the on-disk schema."""
    global _plugin_capability_contract_validator_cache
    _plugin_capability_contract_validator_cache = None


_PLUGIN_REQUEST_CONTRACT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "contracts" / "schemas" / "plugin-request.v1.schema.json"
)
_plugin_request_contract_validator_cache: Draft202012Validator | None = None


def plugin_request_contract_validator() -> Draft202012Validator:
    """Load the plugin-request contract schema once; fail closed if absent.

    Shared by every plugin-request consumer so there is exactly one
    interpretation of the contract schema.  The closed-root, closed-tool-call,
    and closed-approval guards refuse a schema edit that would reopen the
    request, its tool-call block, or its approval binding to unreviewed
    extension fields: a plugin request must stay a closed ids/typed-inputs
    record, never an extensible envelope through which a path, a raw command, a
    credential value, or an executable body could ride in.
    """
    global _plugin_request_contract_validator_cache
    if _plugin_request_contract_validator_cache is None:
        try:
            schema = json.loads(_PLUGIN_REQUEST_CONTRACT_SCHEMA_PATH.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ContractValidationError(
                "plugin-request contract schema is unavailable; refusing to validate requests"
            ) from exc
        if schema.get("additionalProperties") is not False:
            raise ContractValidationError(
                "plugin-request contract schema no longer closes its root object; "
                "refusing to validate requests"
            )
        props = schema.get("properties", {})
        for name, node in (
            ("tool_call", props.get("tool_call")),
            ("approval", props.get("approval")),
            ("lifecycle", props.get("lifecycle")),
            ("actor", props.get("actor")),
            ("pluginRef", schema.get("$defs", {}).get("pluginRef")),
        ):
            if not isinstance(node, dict) or node.get("additionalProperties") is not False:
                raise ContractValidationError(
                    f"plugin-request contract schema no longer closes its {name} object; "
                    "refusing to validate requests"
                )
        _plugin_request_contract_validator_cache = Draft202012Validator(schema)
    return _plugin_request_contract_validator_cache


def _reset_plugin_request_contract_validator_cache() -> None:
    """Test hook: force the next plugin-request validation to reload the on-disk schema."""
    global _plugin_request_contract_validator_cache
    _plugin_request_contract_validator_cache = None


_PLUGIN_PREVIEW_CONTRACT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "contracts" / "schemas" / "plugin-preview.v1.schema.json"
)
_plugin_preview_contract_validator_cache: Draft202012Validator | None = None


def plugin_preview_contract_validator() -> Draft202012Validator:
    """Load the plugin-preview contract schema once; fail closed if absent.

    Shared by every plugin-preview consumer so there is exactly one
    interpretation of the contract schema.  The closed-root, closed-change, and
    closed-approval guards refuse a schema edit that would reopen the preview, a
    change item, or its approval binding to unreviewed extension fields: a
    preview must stay a closed, redacted, hash-bound artifact, never an
    extensible envelope through which a raw endpoint, path, or credential could
    ride in.
    """
    global _plugin_preview_contract_validator_cache
    if _plugin_preview_contract_validator_cache is None:
        try:
            schema = json.loads(_PLUGIN_PREVIEW_CONTRACT_SCHEMA_PATH.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ContractValidationError(
                "plugin-preview contract schema is unavailable; refusing to validate previews"
            ) from exc
        if schema.get("additionalProperties") is not False:
            raise ContractValidationError(
                "plugin-preview contract schema no longer closes its root object; "
                "refusing to validate previews"
            )
        change = schema.get("properties", {}).get("changes", {}).get("items", {})
        approval = schema.get("properties", {}).get("approval")
        for name, node in (("change", change), ("approval", approval)):
            if not isinstance(node, dict) or node.get("additionalProperties") is not False:
                raise ContractValidationError(
                    f"plugin-preview contract schema no longer closes its {name} object; "
                    "refusing to validate previews"
                )
        _plugin_preview_contract_validator_cache = Draft202012Validator(schema)
    return _plugin_preview_contract_validator_cache


def _reset_plugin_preview_contract_validator_cache() -> None:
    """Test hook: force the next plugin-preview validation to reload the on-disk schema."""
    global _plugin_preview_contract_validator_cache
    _plugin_preview_contract_validator_cache = None


_PLUGIN_RECEIPT_CONTRACT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "contracts" / "schemas" / "plugin-receipt.v1.schema.json"
)
_plugin_receipt_contract_validator_cache: Draft202012Validator | None = None


def plugin_receipt_contract_validator() -> Draft202012Validator:
    """Load the plugin-receipt contract schema once; fail closed if absent.

    Shared by every plugin-receipt consumer so there is exactly one
    interpretation of the contract schema.  The closed-root, closed-result, and
    closed-credential guards refuse a schema edit that would reopen the receipt,
    its result block, or its credential-use block to unreviewed extension
    fields: a receipt must stay a closed, redacted audit record whose credential
    use is reported by opaque reference only, never an extensible envelope that
    could leak a raw payload or a credential value.
    """
    global _plugin_receipt_contract_validator_cache
    if _plugin_receipt_contract_validator_cache is None:
        try:
            schema = json.loads(_PLUGIN_RECEIPT_CONTRACT_SCHEMA_PATH.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ContractValidationError(
                "plugin-receipt contract schema is unavailable; refusing to validate receipts"
            ) from exc
        if schema.get("additionalProperties") is not False:
            raise ContractValidationError(
                "plugin-receipt contract schema no longer closes its root object; "
                "refusing to validate receipts"
            )
        for name in ("result", "credential_use"):
            node = schema.get("properties", {}).get(name)
            if not isinstance(node, dict) or node.get("additionalProperties") is not False:
                raise ContractValidationError(
                    f"plugin-receipt contract schema no longer closes its {name} object; "
                    "refusing to validate receipts"
                )
        _plugin_receipt_contract_validator_cache = Draft202012Validator(schema)
    return _plugin_receipt_contract_validator_cache


def _reset_plugin_receipt_contract_validator_cache() -> None:
    """Test hook: force the next plugin-receipt validation to reload the on-disk schema."""
    global _plugin_receipt_contract_validator_cache
    _plugin_receipt_contract_validator_cache = None


_DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"


def _iter_schema_refs(value: Any) -> Iterator[tuple[str, str]]:
    """Yield every ``(keyword, target)`` reference pair anywhere in the tree.

    The walk is deliberately over-broad: any ``$ref``/``$dynamicRef`` key with
    a string value is treated as a reference, even inside annotation values
    such as ``examples``.  A reviewed operation schema has no legitimate need
    for such a key elsewhere, and an ambiguous document must be rejected
    rather than partially trusted.
    """
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if key in ("$ref", "$dynamicRef") and isinstance(nested, str):
                yield (str(key), nested)
            yield from _iter_schema_refs(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _iter_schema_refs(nested)


def _resolve_local_pointer(root: Mapping[str, Any], keyword: str, target: str) -> None:
    """Prove one intra-document ``#``-pointer fragment resolves; fail closed."""
    fragment = target[1:]
    if not fragment:
        return
    if not fragment.startswith("/"):
        raise ContractValidationError(
            f"declares a non-pointer {keyword} fragment (anchors are not supported): {target!r}"
        )
    node: Any = root
    for raw_token in fragment[1:].split("/"):
        token = unquote(raw_token).replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping) and token in node:
            node = node[token]
        elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
            node = node[int(token)]
        else:
            raise ContractValidationError(f"declares an unresolvable {keyword}: {target!r}")


def check_operation_schema(schema: Any) -> None:
    """Fail closed unless ``schema`` is a self-contained draft 2020-12 object schema.

    ``Draft202012Validator.check_schema`` never resolves references, so a
    dangling local pointer or a remote/``file:`` ``$ref`` would pass a bare
    well-formedness check and only surface at evaluation time -- as a
    referencing error that is not a :class:`jsonschema.ValidationError`, or as
    an implicit fetch.  Beyond the well-formedness, dialect, and object-type
    checks, this helper therefore (a) rejects every ``$ref``/``$dynamicRef``
    that is not an intra-document ``#``-pointer fragment and (b) proves each
    local pointer resolves.  Error messages are predicate fragments so callers
    can prefix their own provider/operation context.
    """
    if not isinstance(schema, Mapping):
        raise ContractValidationError("is not a schema object")
    declared = schema.get("$schema")
    if declared is not None and declared != _DRAFT_2020_12:
        raise ContractValidationError(f"declares an unsupported dialect: {declared!r}")
    if schema.get("type") != "object":
        raise ContractValidationError("must be a typed object schema")
    try:
        Draft202012Validator.check_schema(dict(schema))
    except SchemaError as exc:
        raise ContractValidationError(
            f"is not a valid draft 2020-12 schema: {exc.message}"
        ) from exc
    for keyword, target in _iter_schema_refs(schema):
        if not target.startswith("#"):
            raise ContractValidationError(f"declares a non-local {keyword}: {target!r}")
        _resolve_local_pointer(schema, keyword, target)


_PLUGIN_TOOL_SCHEMA_MAX_PROPERTIES = 64
_PLUGIN_TOOL_SCHEMA_MAX_OBJECT_DEPTH = 8

# JSON Schema applicator keywords whose values are (or contain) subschemas. The
# walk is confined to these so a data-bearing keyword (enum/const/default/
# examples) can never be mistaken for a schema to close or count.
_SCHEMA_SUBSCHEMA_KEYWORDS = (
    "additionalProperties", "unevaluatedProperties", "propertyNames",
    "items", "contains", "not", "if", "then", "else",
)
_SCHEMA_SUBSCHEMA_LIST_KEYWORDS = ("allOf", "anyOf", "oneOf", "prefixItems")
_SCHEMA_SUBSCHEMA_MAP_KEYWORDS = ("properties", "patternProperties", "$defs", "dependentSchemas")


def _is_object_schema(node: Mapping[str, Any]) -> bool:
    """True when a subschema constrains an object (by ``type`` or by properties)."""
    declared = node.get("type")
    if declared == "object" or (isinstance(declared, list) and "object" in declared):
        return True
    return isinstance(node.get("properties"), Mapping) or isinstance(node.get("patternProperties"), Mapping)


def _check_plugin_schema_closed_and_bounded(node: Any, object_depth: int) -> None:
    """Recursively enforce closed, size-bounded object schemas; fail closed.

    Every object schema anywhere in the tree must declare
    ``additionalProperties: false`` (an open nested object is the same smuggle
    hole as an open root, one level down); no object may declare more than
    :data:`_PLUGIN_TOOL_SCHEMA_MAX_PROPERTIES` properties; and object schemas may
    not nest deeper than :data:`_PLUGIN_TOOL_SCHEMA_MAX_OBJECT_DEPTH` levels.
    """
    if not isinstance(node, Mapping):
        return
    depth = object_depth
    if _is_object_schema(node):
        if node.get("additionalProperties") is not False:
            raise ContractValidationError(
                "must close every object with additionalProperties:false "
                "(an open nested object schema is a smuggle hole)"
            )
        if "patternProperties" in node:
            # additionalProperties:false only governs keys NOT matched by
            # patternProperties, so a pattern-keyed open string map still
            # accepts arbitrary fields ({"command": "...| sh"}) through the
            # "closed" boundary. A plugin tool's typed boundary must enumerate
            # its fields via `properties` only.
            raise ContractValidationError(
                "must enumerate object fields via properties only; a plugin tool "
                "schema may not use patternProperties (additionalProperties:false "
                "does not close keys matched by patternProperties — a smuggle hole)"
            )
        depth += 1
        if depth > _PLUGIN_TOOL_SCHEMA_MAX_OBJECT_DEPTH:
            raise ContractValidationError(
                f"nests object schemas deeper than the {_PLUGIN_TOOL_SCHEMA_MAX_OBJECT_DEPTH}-level bound"
            )
        properties = node.get("properties")
        if isinstance(properties, Mapping) and len(properties) > _PLUGIN_TOOL_SCHEMA_MAX_PROPERTIES:
            raise ContractValidationError(
                f"declares more than the {_PLUGIN_TOOL_SCHEMA_MAX_PROPERTIES}-property bound on one object"
            )
    for keyword in _SCHEMA_SUBSCHEMA_KEYWORDS:
        _check_plugin_schema_closed_and_bounded(node.get(keyword), depth)
    for keyword in _SCHEMA_SUBSCHEMA_LIST_KEYWORDS:
        seq = node.get(keyword)
        if isinstance(seq, (list, tuple)):
            for item in seq:
                _check_plugin_schema_closed_and_bounded(item, depth)
    for keyword in _SCHEMA_SUBSCHEMA_MAP_KEYWORDS:
        mapping = node.get(keyword)
        if isinstance(mapping, Mapping):
            for item in mapping.values():
                _check_plugin_schema_closed_and_bounded(item, depth)


def check_plugin_tool_schema(schema: Any) -> None:
    """Fail closed unless a plugin tool I/O schema is a *closed, bounded* object schema.

    Extends :func:`check_operation_schema` (well-formedness, draft-2020-12
    dialect, object root, intra-document ``#``-pointer refs only) with the two
    properties a plugin tool's typed I/O boundary needs that a provider operation
    schema does not guarantee: the root object *and every locally-reachable
    nested object schema* must declare ``additionalProperties: false``, and the
    schema is size-bounded (at most 64 properties per object, object nesting no
    deeper than 8).  Without recursive closure an open ``{"type":"object"}``
    field would let ``{"command":"curl … | sh","cwd":"/etc"}`` ride through the
    typed boundary a reviewer thought was closed.

    Kept plugin-specific rather than folded into ``check_operation_schema``: the
    provider operation contract applies that helper only to operation *inputs*
    and deliberately leaves operation *outputs* open, so recursive closure and
    the size bound are plugin criterion-1 properties, not shared ones.  Hardening
    the shared helper would change the operation contract's guarantees and its
    review boundary for no plugin benefit.
    """
    check_operation_schema(schema)
    _check_plugin_schema_closed_and_bounded(schema, 0)


class ApprovalConsumer(Protocol):
    """Bridge-side authority check for one approval-gated V2 operation."""

    def consume(
        self, grant_id: str, action: str, payload_hash: str, bridge_id: str, project_id: str,
    ) -> None: ...


_PREFIXES = {
    "operation": b"anvil-workbench/operation/v1\0",
    "catalog": b"anvil-workbench/catalog/v1\0",
    "profile": b"anvil-workbench/capability-profile/v1\0",
    "workflow": b"anvil-workbench/workflow/v2\0",
    "workflow-snapshot": b"anvil-workbench/workflow-snapshot/v1\0",
    "skill": b"anvil-workbench/skill/v1\0",
    "approval-payload": b"anvil-workbench/approval-payload/v1\0",
    "state-snapshot": b"anvil-workbench/state-snapshot/v1\0",
    "prd-content": b"anvil-workbench/prd-content/v1\0",
    "settings-descriptor": b"anvil-workbench/settings-descriptor/v1\0",
    "advanced-preset": b"anvil-workbench/advanced-preset/v1\0",
    "deliver-intent": b"anvil-workbench/deliver-intent/v1\0",
    "plugin-catalog": b"anvil-workbench/plugin-catalog/v1\0",
    "plugin": b"anvil-workbench/plugin/v1\0",
    "plugin-capability": b"anvil-workbench/plugin-capability/v1\0",
    "plugin-request": b"anvil-workbench/plugin-request/v1\0",
}


def _reject_floats(value: Any) -> None:
    """Enforce the DIGESTING.md domain: string keys only, no floats anywhere."""
    if isinstance(value, float):
        raise ContractValidationError("floating-point values are not permitted in digest-bearing resource fields")
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ContractValidationError("non-string object keys are not permitted in digest-bearing resources")
            _reject_floats(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_floats(nested)


def _canonical_json(value: Any) -> bytes:
    """Encode the restricted JSON contract domain in the documented canonical form."""
    _reject_floats(value)
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def canonical_json_bytes(value: Any) -> bytes:
    """Public canonical encoding for other digest consumers (e.g. chat content).

    Same restricted domain as contract digests: string keys, no floats, sorted
    keys, compact separators, UTF-8.  Callers add their own domain-separation
    prefix so a chat-content hash can never collide with a contract digest.
    """
    return _canonical_json(value)


def _without(value: Mapping[str, Any], *names: str) -> dict[str, Any]:
    return {key: copy.deepcopy(item) for key, item in value.items() if key not in names}


def _operation_sort_key(value: Mapping[str, Any]) -> tuple[str, str, str]:
    return (str(value.get("id", "")), str(value.get("contract_version", "")), str(value.get("operation_digest", "")))


def _profile_operation_sort_key(value: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(value.get("provider", "")), str(value.get("id", "")),
        str(value.get("contract_version", "")), str(value.get("operation_digest", "")),
    )


def canonical_contract_payload(kind: str, value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy normalized according to ``docs/contracts/DIGESTING.md``."""
    if kind not in _PREFIXES:
        raise ContractValidationError(f"unsupported contract digest kind: {kind}")
    payload = _without(value, "operation_digest") if kind == "operation" else _without(value)
    if kind == "catalog":
        payload = _without(value, "catalog_digest", "generated_at")
        operations = payload.get("operations")
        if isinstance(operations, list):
            payload["operations"] = sorted(copy.deepcopy(operations), key=_operation_sort_key)
    elif kind == "profile":
        payload = _without(value, "digest")
        operations = payload.get("operations")
        if isinstance(operations, list):
            payload["operations"] = sorted(copy.deepcopy(operations), key=_profile_operation_sort_key)
        for field in ("model_profiles", "approval_actions"):
            if isinstance(payload.get(field), list):
                payload[field] = sorted(copy.deepcopy(payload[field]))
        skills = payload.get("skills")
        if isinstance(skills, list):
            payload["skills"] = sorted(copy.deepcopy(skills), key=lambda item: (str(item.get("id", "")), str(item.get("digest", ""))))
    elif kind == "workflow":
        payload = _without(value, "digest")
    elif kind == "workflow-snapshot":
        payload = _without(value, "snapshot_digest")
        catalogs = payload.get("catalogs")
        if isinstance(catalogs, list):
            payload["catalogs"] = sorted(
                copy.deepcopy(catalogs), key=lambda item: str(item.get("provider", ""))
            )
        operations = payload.get("operations")
        if isinstance(operations, list):
            payload["operations"] = sorted(copy.deepcopy(operations), key=_profile_operation_sort_key)
        skills = payload.get("skills")
        if isinstance(skills, list):
            payload["skills"] = sorted(
                copy.deepcopy(skills), key=lambda item: (str(item.get("id", "")), str(item.get("digest", "")))
            )
        for field in ("model_profiles", "approval_actions"):
            if isinstance(payload.get(field), list):
                payload[field] = sorted(copy.deepcopy(payload[field]))
    elif kind == "skill":
        payload = _without(value, "digest")
    elif kind == "state-snapshot":
        payload = _without(value, "snapshot_digest", "generated_at")
        prds = payload.get("prds")
        if isinstance(prds, list):
            payload["prds"] = sorted(copy.deepcopy(prds), key=lambda item: str(item.get("prd_id", "")))
        tasks = payload.get("tasks")
        if isinstance(tasks, list):
            payload["tasks"] = sorted(
                copy.deepcopy(tasks),
                key=lambda item: (
                    str(item.get("ref", {}).get("prd_id", "")) if isinstance(item.get("ref"), Mapping) else "",
                    str(item.get("ref", {}).get("task_id", "")) if isinstance(item.get("ref"), Mapping) else "",
                ),
            )
    elif kind == "prd-content":
        payload = _without(value, "content_digest", "generated_at")
    elif kind == "settings-descriptor":
        payload = _without(value, "catalog_digest")
        settings = payload.get("settings")
        if isinstance(settings, list):
            payload["settings"] = sorted(copy.deepcopy(settings), key=lambda item: str(item.get("id", "")))
    elif kind == "advanced-preset":
        # Exclude the digest and the volatile repair block: the same preset
        # content must hash identically regardless of the current live-drift
        # state, so drift is detected by comparing pinned references to live
        # digests, never by the digest changing.
        payload = _without(value, "preset_digest", "repair")
        control_values = payload.get("control_values")
        if isinstance(control_values, list):
            payload["control_values"] = sorted(
                copy.deepcopy(control_values), key=lambda item: str(item.get("name", ""))
            )
        tools = payload.get("tools")
        if isinstance(tools, list):
            payload["tools"] = sorted(
                copy.deepcopy(tools), key=lambda item: str(item.get("tool_id", ""))
            )
    elif kind == "deliver-intent":
        # Exclude the digest: the same intent content must hash identically so
        # the idempotency key is stable, and replaying an identical intent
        # starts the same run rather than a second one.  Sort the selection
        # lists so their order never changes the key.
        payload = _without(value, "intent_digest")
        selections = payload.get("selections")
        if isinstance(selections, Mapping):
            selections = copy.deepcopy(selections)
            payload["selections"] = selections
            catalogs = selections.get("catalogs")
            if isinstance(catalogs, list):
                # Sort by the full (provider, digest) tuple, mirroring skills'
                # (id, digest) key.  Two same-provider catalog entries are
                # schema-legal, so sorting on provider alone would leave their
                # relative order to decide the digest and break the idempotency
                # key; the full tuple makes the key order-independent.
                selections["catalogs"] = sorted(
                    catalogs, key=lambda item: (str(item.get("provider", "")), str(item.get("digest", "")))
                )
            skills = selections.get("skills")
            if isinstance(skills, list):
                selections["skills"] = sorted(
                    skills, key=lambda item: (str(item.get("id", "")), str(item.get("digest", "")))
                )
    elif kind == "plugin-catalog":
        # Exclude the digest and the volatile generated_at; sort plugins by the
        # full (id, plugin_digest) tuple so a reorder can never change the
        # catalog digest.  A plugin's own tool order is part of its identity and
        # is preserved: reordering tools is a content change to that plugin and
        # legitimately changes its plugin_digest.
        payload = _without(value, "catalog_digest", "generated_at")
        plugins = payload.get("plugins")
        if isinstance(plugins, list):
            payload["plugins"] = sorted(
                copy.deepcopy(plugins),
                key=lambda item: (str(item.get("id", "")), str(item.get("plugin_digest", ""))),
            )
    elif kind == "plugin":
        # Exclude only the plugin's own digest; the tool list order is preserved
        # so the plugin_digest is tamper-evidence over the exact reviewed tools.
        payload = _without(value, "plugin_digest")
    elif kind == "plugin-capability":
        # Exclude the digest; sort the plugin allowlist by the full
        # (plugin_id, plugin_digest) tuple and each entry's enabled_tools
        # lexicographically, so neither a reorder of entries nor of a tool list
        # can change the profile digest.
        payload = _without(value, "digest")
        plugins = payload.get("plugins")
        if isinstance(plugins, list):
            sorted_plugins = sorted(
                copy.deepcopy(plugins),
                key=lambda item: (str(item.get("plugin_id", "")), str(item.get("plugin_digest", ""))),
            )
            for entry in sorted_plugins:
                tools = entry.get("enabled_tools") if isinstance(entry, Mapping) else None
                if isinstance(tools, list):
                    entry["enabled_tools"] = sorted(tools, key=str)
            payload["plugins"] = sorted_plugins
    elif kind == "plugin-request":
        # Exclude the digest so the same request content hashes identically and
        # the idempotency key is stable; a mutated request recomputes to a
        # different key.  The tool-call inputs are an object, so canonical JSON
        # already sorts their keys — there is no list needing an explicit sort.
        payload = _without(value, "request_digest")
    return payload


def contract_digest(kind: str, value: Mapping[str, Any]) -> str:
    """Return the domain-separated SHA-256 digest for one contract resource."""
    try:
        prefix = _PREFIXES[kind]
    except KeyError as exc:  # defensive even though canonical_contract_payload checks it
        raise ContractValidationError(f"unsupported contract digest kind: {kind}") from exc
    return "sha256:" + hashlib.sha256(prefix + _canonical_json(canonical_contract_payload(kind, value))).hexdigest()


def approval_payload_digest(value: Mapping[str, Any]) -> str:
    """Hash the exact approved operation input object, never an arbitrary command."""
    return contract_digest("approval-payload", value)


def validate_catalog(catalog: Mapping[str, Any]) -> None:
    """Fail closed when a catalog or any advertised operation has drifted."""
    for operation in catalog.get("operations", []):
        if not isinstance(operation, Mapping):
            raise ContractValidationError("catalog operation is not an object")
        actual = contract_digest("operation", operation)
        if operation.get("operation_digest") != actual:
            raise ContractValidationError(f"operation digest mismatch: {operation.get('id', '<unknown>')}")
    actual_catalog = contract_digest("catalog", catalog)
    if catalog.get("catalog_digest") != actual_catalog:
        raise ContractValidationError(f"catalog digest mismatch: {catalog.get('provider', '<unknown>')}")


def validate_profile(profile: Mapping[str, Any]) -> None:
    """Fail closed when a project capability profile has drifted."""
    if profile.get("digest") != contract_digest("profile", profile):
        raise ContractValidationError("capability profile digest mismatch")


def validate_state_snapshot(snapshot: Mapping[str, Any]) -> None:
    """Fail closed when a State project snapshot is internally inconsistent.

    Schema validation cannot express these rules: the advertised digest must
    recompute, every task must reference a PRD present in the snapshot, the
    display ``scoped_id`` must equal its typed reference, and references must
    be unique so the digest sort order is total and publication is idempotent.
    """
    if snapshot.get("snapshot_digest") != contract_digest("state-snapshot", snapshot):
        raise ContractValidationError("state snapshot digest mismatch")
    prds = snapshot.get("prds")
    tasks = snapshot.get("tasks")
    if not isinstance(prds, list) or not isinstance(tasks, list):
        raise ContractValidationError("state snapshot prds/tasks are invalid")
    prd_ids = {prd.get("prd_id") for prd in prds if isinstance(prd, Mapping)}
    if len(prd_ids) != len(prds):
        raise ContractValidationError("state snapshot PRD ids must be unique")
    seen_refs: set[tuple[str, str]] = set()
    for task in tasks:
        ref = task.get("ref") if isinstance(task, Mapping) else None
        if not isinstance(ref, Mapping):
            raise ContractValidationError("state snapshot task has no typed reference")
        key = (str(ref.get("prd_id")), str(ref.get("task_id")))
        if key[0] not in prd_ids:
            raise ContractValidationError(f"task reference names an unknown PRD: {key[0]}")
        if key in seen_refs:
            raise ContractValidationError(f"duplicate task reference: {key[0]}:{key[1]}")
        seen_refs.add(key)
        if task.get("scoped_id") != f"{key[0]}:{key[1]}":
            raise ContractValidationError(f"scoped_id does not match its typed reference: {task.get('scoped_id')}")
    # Second pass: the snapshot is the complete bounded projection, so every
    # dependency edge must resolve to a task in this snapshot and no task may
    # depend on itself; a dangling or reflexive edge is never legitimate.
    for task in tasks:
        ref = task["ref"]
        key = (str(ref.get("prd_id")), str(ref.get("task_id")))
        for dependency in task.get("depends_on", ()):
            if not isinstance(dependency, Mapping):
                raise ContractValidationError("task dependency is not a typed reference")
            dep_key = (str(dependency.get("prd_id")), str(dependency.get("task_id")))
            if dep_key not in seen_refs:
                raise ContractValidationError(
                    f"task dependency names a task absent from the snapshot: {dep_key[0]}:{dep_key[1]}"
                )
            if dep_key == key:
                raise ContractValidationError(f"task cannot depend on itself: {key[0]}:{key[1]}")


def validate_prd_content(document: Mapping[str, Any]) -> None:
    """Fail closed when a bounded PRD-content read breaks its own bounds."""
    if document.get("content_digest") != contract_digest("prd-content", document):
        raise ContractValidationError("prd content digest mismatch")
    content = document.get("content")
    if not isinstance(content, Mapping) or not isinstance(content.get("body"), str):
        raise ContractValidationError("prd content body is invalid")
    body_bytes = len(content["body"].encode("utf-8"))
    if body_bytes > 65536:
        raise ContractValidationError("prd content body exceeds the 64 KiB byte bound")
    total = content.get("total_bytes")
    truncated = content.get("truncated")
    if truncated is False and total != body_bytes:
        raise ContractValidationError("untruncated prd content must declare total_bytes equal to the body byte length")
    if truncated is True and (not isinstance(total, int) or total <= body_bytes):
        raise ContractValidationError("truncated prd content must declare total_bytes greater than the body byte length")


_SETTINGS_SCOPES = ("personal", "project", "deployment", "policy")
_SETTINGS_ACTOR_SCOPES = ("personal", "project")
_SETTINGS_AUTHORITY_SCOPES = ("deployment", "policy")
_SETTINGS_REFERENCE_KINDS = ("route", "worktree", "workflow", "skill", "plugin", "capability")
_SETTINGS_ACTOR_VIEW_FIELDS = frozenset({
    "id", "title", "description", "type", "scope", "sensitivity", "mutability",
    "application_timing", "ref_kind", "allowed_values", "bounds", "default",
    "depends_on", "migration", "policy_ceiling",
})


def validate_settings_descriptor(catalog: Mapping[str, Any]) -> None:
    """Fail closed when a settings-descriptor catalog is internally inconsistent.

    JSON Schema pins each descriptor's shape; these are the cross-field rules it
    cannot express and that the three acceptance criteria depend on:

    * the advertised ``catalog_digest`` must recompute (tamper-evident catalog);
    * ``scope_precedence`` is a total order over every scope, so effective-value
      resolution is deterministic (criterion 1);
    * every descriptor owns exactly one scope, a ``policy_ceiling`` must be owned
      by a strictly higher-authority scope (this pins the scope RANKING; the
      numeric value clamp of a lower-scope bound against its ceiling is the
      T002 resolver's job, not the descriptor's), so a personal value can never exceed
      a project/deployment/policy bound (criterion 1 + PRD non-goal);
    * a ``secret`` or path-like descriptor stays authority-owned with no default
      (criterion 2, defence-in-depth behind the schema guard);
    * every reference kind is present and typed as an id/digest reference rather
      than free text (criterion 3).
    """
    try:
        settings_descriptor_contract_validator().validate(dict(catalog))
    except ValidationError as exc:
        raise ContractValidationError(f"settings descriptor catalog is not schema valid: {exc.message}") from exc

    if catalog.get("catalog_digest") != contract_digest("settings-descriptor", catalog):
        raise ContractValidationError("settings descriptor catalog digest mismatch")

    precedence = catalog.get("scope_precedence")
    if not isinstance(precedence, list) or set(precedence) != set(_SETTINGS_SCOPES) or len(precedence) != len(_SETTINGS_SCOPES):
        raise ContractValidationError("scope_precedence must be a total order over every scope")
    rank = {scope: index for index, scope in enumerate(precedence)}
    # Authority direction is pinned, not merely declared: every authority scope
    # must outrank (precede) every actor scope, so an inverted permutation
    # (personal above policy) cannot pass validation and a personal value can
    # never be declared to outrank a policy bound. A lower rank index = higher
    # authority.
    for _authority in _SETTINGS_AUTHORITY_SCOPES:
        for _actor in _SETTINGS_ACTOR_SCOPES:
            if rank[_authority] >= rank[_actor]:
                raise ContractValidationError(
                    "scope_precedence must rank authority scopes above actor scopes "
                    f"(got {_authority!r} not above {_actor!r})"
                )

    settings = catalog.get("settings")
    if not isinstance(settings, list):
        raise ContractValidationError("settings descriptor catalog has no settings list")
    by_id: dict[str, Mapping[str, Any]] = {}
    for setting in settings:
        if not isinstance(setting, Mapping):
            raise ContractValidationError("settings descriptor is not an object")
        setting_id = str(setting.get("id"))
        if setting_id in by_id:
            raise ContractValidationError(f"duplicate setting id: {setting_id}")
        by_id[setting_id] = setting

    present_reference_kinds: set[str] = set()
    for setting_id, setting in by_id.items():
        scope = setting.get("scope")
        if scope not in _SETTINGS_SCOPES:
            raise ContractValidationError(f"setting names an unknown scope: {setting_id}")

        is_secret = setting.get("sensitivity") == "secret" or setting.get("path_like") is True
        if is_secret:
            if scope not in _SETTINGS_AUTHORITY_SCOPES:
                raise ContractValidationError(f"secret or path-like setting must be authority-owned: {setting_id}")
            if "default" in setting:
                raise ContractValidationError(f"secret or path-like setting must not carry a default: {setting_id}")

        ref_kind = setting.get("ref_kind")
        if ref_kind is not None:
            if setting.get("type") not in ("id_ref", "digest_ref"):
                raise ContractValidationError(f"reference-kind setting must be an id/digest reference: {setting_id}")
            present_reference_kinds.add(str(ref_kind))

        if setting.get("type") == "enum":
            allowed = setting.get("allowed_values")
            default = setting.get("default")
            if default is not None and default not in (allowed or ()):
                raise ContractValidationError(f"enum default is not one of allowed_values: {setting_id}")

        bounds = setting.get("bounds")
        default = setting.get("default")
        if isinstance(bounds, Mapping) and isinstance(default, int) and not isinstance(default, bool):
            if not (bounds.get("min", default) <= default <= bounds.get("max", default)):
                raise ContractValidationError(f"int default is outside its bounds: {setting_id}")

        for dependency in setting.get("depends_on", ()):
            dep = str(dependency)
            if dep == setting_id:
                raise ContractValidationError(f"setting cannot depend on itself: {setting_id}")
            if dep not in by_id:
                raise ContractValidationError(f"setting dependency names an unknown setting: {dep}")

        ceiling = setting.get("policy_ceiling")
        if isinstance(ceiling, Mapping):
            ceiling_id = str(ceiling.get("ceiling_setting"))
            ceiling_setting = by_id.get(ceiling_id)
            if ceiling_setting is None:
                raise ContractValidationError(f"policy_ceiling names an unknown setting: {ceiling_id}")
            if rank[ceiling_setting.get("scope")] >= rank[scope]:
                raise ContractValidationError(
                    f"policy_ceiling must be owned by a strictly higher-authority scope: {setting_id}"
                )

    missing = set(_SETTINGS_REFERENCE_KINDS) - present_reference_kinds
    if missing:
        raise ContractValidationError(f"settings catalog omits reference-kind defaults: {sorted(missing)}")


def settings_actor_view(catalog: Mapping[str, Any]) -> dict[str, Any]:
    """Project the actor/project-facing serialization of a descriptor catalog.

    This is the only shape a preference API or a redacted export may serialize.
    It keeps personal- and project-owned descriptors and drops every
    authority-owned, ``secret``, or path-like descriptor -- defence-in-depth
    behind the schema guard so a secret value or its default can never reach a
    browser payload or an export even if a malformed catalog slips through.
    """
    view: dict[str, Any] = {
        "schema_version": catalog.get("schema_version"),
        "catalog_id": catalog.get("catalog_id"),
        "revision": catalog.get("revision"),
        "settings": [],
    }
    for setting in catalog.get("settings", []):
        if not isinstance(setting, Mapping):
            continue
        if setting.get("scope") not in _SETTINGS_ACTOR_SCOPES:
            continue
        if setting.get("sensitivity") == "secret" or setting.get("path_like") is True:
            continue
        projected = {}
        for key, item in setting.items():
            if key not in _SETTINGS_ACTOR_VIEW_FIELDS:
                continue
            # Defense-in-depth: even a field declared non-secret is scrubbed for
            # secret/path shapes before it can reach a browser/export, so a
            # mis-declared sensitivity cannot leak a token or path.
            projected[key] = _redact_settings_value(copy.deepcopy(item)) if key in _SETTINGS_SCRUBBED_FIELDS else copy.deepcopy(item)
        view["settings"].append(projected)
    return view


_SETTINGS_SCRUBBED_FIELDS = frozenset({"default", "title", "description", "allowed_values"})


def _redact_settings_value(value):
    """Recursively scrub secret/path shapes from an actor-facing settings value."""
    from workbench.redaction import redact_value

    return redact_value(value)


def _check_advanced_control_value(descriptor: Mapping[str, Any], value: Any, name: str) -> None:
    """Refuse a submitted control value that violates its declared descriptor."""
    control_type = descriptor.get("type")
    if control_type == "int":
        bounds = descriptor.get("bounds")
        if not isinstance(bounds, Mapping):
            raise ContractValidationError(f"declared int control has no bounds: {name}")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ContractValidationError(f"control {name} must be an integer: {value!r}")
        if not (bounds.get("min", value) <= value <= bounds.get("max", value)):
            raise ContractValidationError(
                f"control {name} is outside its declared bounds "
                f"[{bounds.get('min')}, {bounds.get('max')}]: {value!r}"
            )
    elif control_type == "enum":
        allowed = descriptor.get("allowed_values")
        if not isinstance(allowed, (list, tuple)) or value not in allowed:
            raise ContractValidationError(f"control {name} is not one of its declared allowed values: {value!r}")
    elif control_type == "bool":
        if not isinstance(value, bool):
            raise ContractValidationError(f"control {name} must be a boolean: {value!r}")
    else:  # pragma: no cover - schema pins the type enum
        raise ContractValidationError(f"control {name} declares an unsupported type: {control_type!r}")


def validate_advanced_branch(branch: Mapping[str, Any]) -> None:
    """Fail closed when an Advanced-mode branch violates a cross-field rule.

    JSON Schema pins each shape; these are the rules it cannot express and the
    first acceptance criterion depends on:

    * a submitted control MUST name a control the pinned route capability
      declares (with a type, bounds/allowed values, and default) and stay within
      those declared bounds — an undeclared or out-of-bounds control is refused
      before it could ever reach a Serving request (criterion 1); the route and
      profile digests are schema-required on ``route_capability`` so a control is
      never submittable without them;
    * a ``policy_owned`` control is read-only: a submitted value must carry
      ``policy_override`` provenance and equal the declared default, so a crafted
      request cannot override a policy-owned value (R006).
    """
    try:
        advanced_branch_contract_validator().validate(dict(branch))
    except ValidationError as exc:
        raise ContractValidationError(f"advanced branch is not schema valid: {exc.message}") from exc

    route_capability = branch.get("route_capability")
    if not isinstance(route_capability, Mapping):
        raise ContractValidationError("advanced branch has no route capability")
    declared: dict[str, Mapping[str, Any]] = {}
    for descriptor in route_capability.get("supported_controls", []):
        if isinstance(descriptor, Mapping):
            declared[str(descriptor.get("name"))] = descriptor

    for submitted in branch.get("submitted_controls", []):
        if not isinstance(submitted, Mapping):
            raise ContractValidationError("submitted control is not an object")
        name = str(submitted.get("name"))
        descriptor = declared.get(name)
        if descriptor is None:
            raise ContractValidationError(
                f"submitted control is not declared by the route capability: {name}"
            )
        value = submitted.get("value")
        _check_advanced_control_value(descriptor, value, name)
        if descriptor.get("policy_owned") is True:
            if submitted.get("provenance") != "policy_override" or value != descriptor.get("default"):
                raise ContractValidationError(
                    f"policy-owned control is read-only and cannot be overridden: {name}"
                )


def _advanced_preset_drift(
    preset: Mapping[str, Any], live_digests: Mapping[str, Mapping[str, str]],
) -> dict[tuple[str, str], str]:
    """Return the deterministic set of drifted preset references.

    A reference drifts when the live digest for its id is missing or differs
    from the digest the preset pinned.  The key is ``(ref_kind, id)`` and the
    value is the pinned digest, so the caller can compare it byte-for-byte with
    the preset's declared ``repair.drifted_refs``.
    """
    drift: dict[tuple[str, str], str] = {}

    def _check(ref_kind: str, ref_id: str, pinned: Any) -> None:
        if not isinstance(pinned, str):
            return
        live_for_kind = live_digests.get(ref_kind, {})
        live = live_for_kind.get(ref_id) if isinstance(live_for_kind, Mapping) else None
        if live != pinned:
            drift[(ref_kind, ref_id)] = pinned

    route = preset.get("route", {})
    if isinstance(route, Mapping):
        route_id = str(route.get("route_id"))
        _check("route", route_id, route.get("route_digest"))
        _check("profile", route_id, route.get("profile_digest"))
    for tool in preset.get("tools", []):
        if isinstance(tool, Mapping):
            _check("tool", str(tool.get("tool_id")), tool.get("tool_digest"))
    response_format = preset.get("response_format", {})
    if isinstance(response_format, Mapping) and response_format.get("mode") == "json_schema":
        schema_ref = response_format.get("schema_ref")
        if not isinstance(schema_ref, str) or not schema_ref:
            # A json_schema preset MUST name a keyable schema_ref so its pinned
            # digest is always drift-checked; an unkeyed digest is unmonitored.
            raise ContractValidationError(
                "a json_schema preset must reference a schema_ref so its pinned digest is drift-checked"
            )
        _check("response_schema", schema_ref, response_format.get("schema_digest"))
    return drift


def validate_advanced_preset(
    preset: Mapping[str, Any], live_digests: Mapping[str, Mapping[str, str]],
) -> None:
    """Fail closed when an Advanced preset is tampered with or misreports drift.

    Criterion 3: a preset pins exact route/tool digests, so drift against the
    current live digests is deterministic.  This validator (a) recomputes the
    tamper-evident ``preset_digest`` over the content minus the volatile repair
    block, and (b) requires the preset's declared repair state to equal the
    computed drift exactly — a drifted preset MUST be ``repair_required`` with
    precisely the drifted references listed, and an undrifted preset MUST be
    ``ready``.  The validator never chooses a substitute route or tool; a
    drifted preset opens in repair mode instead of silently changing values.

    ``live_digests`` is a mapping ``{ref_kind: {id: digest}}`` for ``route``,
    ``profile`` (keyed by route id), ``tool`` (keyed by tool id), and
    ``response_schema`` (keyed by schema ref).
    """
    try:
        advanced_preset_contract_validator().validate(dict(preset))
    except ValidationError as exc:
        raise ContractValidationError(f"advanced preset is not schema valid: {exc.message}") from exc

    if preset.get("preset_digest") != contract_digest("advanced-preset", preset):
        raise ContractValidationError("advanced preset digest mismatch")

    drift = _advanced_preset_drift(preset, live_digests)
    repair = preset.get("repair", {})
    status = repair.get("status") if isinstance(repair, Mapping) else None
    declared: dict[tuple[str, str], str] = {}
    for entry in repair.get("drifted_refs", []) if isinstance(repair, Mapping) else []:
        if isinstance(entry, Mapping):
            declared[(str(entry.get("ref_kind")), str(entry.get("id")))] = str(entry.get("pinned_digest"))

    if drift:
        if status != "repair_required":
            raise ContractValidationError(
                "advanced preset with drifted route/tool/profile digests must open in repair mode"
            )
        if declared != drift:
            raise ContractValidationError(
                "advanced preset repair drifted_refs do not match the computed digest drift"
            )
    elif status != "ready":
        raise ContractValidationError("advanced preset without digest drift must be ready")


_DELIVERY_ELIGIBILITY_STATES = ("eligible", "blocked", "stale")
_DELIVERY_REASON_CLASSES = ("blocked", "stale", "info")


def validate_task_reference(reference: Mapping[str, Any]) -> None:
    """Fail closed when a scoped task reference is internally inconsistent.

    JSON Schema pins the shape and requires the owning PRD, the pinned revision,
    and the source snapshot digest (criterion 1: a reference cannot validate
    without its owning PRD and source digest/revision).  These are the
    cross-field rules it cannot express:

    * the display ``scoped_id`` must equal ``<prd_id>:<task_id>`` so two PRDs'
      ``T001`` tasks can never collapse into one row or run (R004);
    * the immutable ``run_label`` must equal ``<scoped_id>@r<prd_revision>``, so
      the label is derived from the pinned revision and cannot silently drift;
    * an optional ``hierarchy`` block must name the same owning PRD.
    """
    try:
        task_reference_contract_validator().validate(dict(reference))
    except ValidationError as exc:
        raise ContractValidationError(f"task reference is not schema valid: {exc.message}") from exc

    ref = reference.get("ref")
    if not isinstance(ref, Mapping):
        raise ContractValidationError("task reference has no typed reference")
    prd_id = str(ref.get("prd_id"))
    task_id = str(ref.get("task_id"))
    scoped_id = f"{prd_id}:{task_id}"
    if reference.get("scoped_id") != scoped_id:
        raise ContractValidationError(f"scoped_id does not match its typed reference: {reference.get('scoped_id')}")
    expected_label = f"{scoped_id}@r{ref.get('prd_revision')}"
    if reference.get("run_label") != expected_label:
        raise ContractValidationError(
            f"run_label is not the immutable <scoped_id>@r<prd_revision> label: {reference.get('run_label')}"
        )
    hierarchy = reference.get("hierarchy")
    if isinstance(hierarchy, Mapping) and hierarchy.get("prd_id") != prd_id:
        raise ContractValidationError("hierarchy names a different owning PRD than the reference")


def validate_delivery_eligibility(verdict: Mapping[str, Any]) -> None:
    """Fail closed when a delivery-eligibility verdict is internally inconsistent.

    JSON Schema pins each reason's shape and enumerates the stable codes and the
    human-safe explanation pattern (criterion 3).  These are the cross-field
    rules it cannot express:

    * the ``state`` is derived, not free: ``blocked`` when any reason is blocked,
      else ``stale`` when any reason is stale, else ``eligible``;
    * ``eligible`` is true only when the derived state is ``eligible``;
    * each reason's ``code`` prefix must match its declared ``class`` so a stale
      code can never be filed under a blocked reason.
    """
    try:
        delivery_eligibility_contract_validator().validate(dict(verdict))
    except ValidationError as exc:
        raise ContractValidationError(f"delivery eligibility is not schema valid: {exc.message}") from exc

    scoped_id = verdict.get("scoped_id")
    ref = verdict.get("ref")
    if isinstance(ref, Mapping) and scoped_id != f"{ref.get('prd_id')}:{ref.get('task_id')}":
        raise ContractValidationError(f"scoped_id does not match its typed reference: {scoped_id}")

    reasons = verdict.get("reasons")
    if not isinstance(reasons, list) or not reasons:
        raise ContractValidationError("delivery eligibility has no reasons")
    classes: set[str] = set()
    for reason in reasons:
        if not isinstance(reason, Mapping):
            raise ContractValidationError("delivery eligibility reason is not an object")
        reason_class = str(reason.get("class"))
        code = str(reason.get("code"))
        if not code.startswith(f"{reason_class}."):
            raise ContractValidationError(f"reason code does not match its class: {code}")
        classes.add(reason_class)

    expected_state = "blocked" if "blocked" in classes else "stale" if "stale" in classes else "eligible"
    if verdict.get("state") != expected_state:
        raise ContractValidationError(
            f"eligibility state must be the derived {expected_state!r}, not {verdict.get('state')!r}"
        )
    if bool(verdict.get("eligible")) != (expected_state == "eligible"):
        raise ContractValidationError("eligible flag disagrees with the derived state")


def validate_deliver_intent(intent: Mapping[str, Any]) -> None:
    """Fail closed when a Deliver intent is tampered with or inconsistent.

    JSON Schema pins the ids-only shape and, because every object is closed,
    already makes a path, raw command, token, or executable workflow body
    unrepresentable (criterion 2).  These are the cross-field rules it cannot
    express:

    * the advertised ``intent_digest`` must recompute over the canonical content,
      so it is a tamper-evident idempotency key — replaying an identical intent
      starts the same run, and a mutated intent fails closed;
    * the pinned ``task_ref.scoped_id`` must equal ``<prd_id>:<task_id>`` so the
      intent binds one unambiguous task (R004).
    """
    try:
        deliver_intent_contract_validator().validate(dict(intent))
    except ValidationError as exc:
        raise ContractValidationError(f"deliver intent is not schema valid: {exc.message}") from exc

    if intent.get("intent_digest") != contract_digest("deliver-intent", intent):
        raise ContractValidationError("deliver intent digest mismatch")

    task_ref = intent.get("task_ref")
    if not isinstance(task_ref, Mapping):
        raise ContractValidationError("deliver intent has no typed task reference")
    if task_ref.get("scoped_id") != f"{task_ref.get('prd_id')}:{task_ref.get('task_id')}":
        raise ContractValidationError(f"deliver intent scoped_id does not match its reference: {task_ref.get('scoped_id')}")


def validate_deliver_start_receipt(
    receipt: Mapping[str, Any], intent: Mapping[str, Any] | None = None,
) -> None:
    """Fail closed when a Deliver start receipt is inconsistent with its intent.

    JSON Schema pins the accepted/duplicate/denied shapes (an accepted or
    duplicate start carries a bounded run, a denied start carries a stable error
    code and human-safe summary).  These are the cross-field rules it cannot
    express:

    * a start receipt echoes the intent's ``intent_digest`` idempotency key, so a
      receipt can never be bound to a different intent than the one presented;
    * when the originating intent is supplied, the receipt's ``task_ref`` scopes
      to the same task, and any run block must report the workflow and
      capability-profile digests the intent actually selected — a run cannot
      claim to have started under a different workflow or profile than approved.
    """
    try:
        deliver_start_receipt_contract_validator().validate(dict(receipt))
    except ValidationError as exc:
        raise ContractValidationError(f"deliver start receipt is not schema valid: {exc.message}") from exc

    task_ref = receipt.get("task_ref")
    if not isinstance(task_ref, Mapping):
        raise ContractValidationError("deliver start receipt has no typed task reference")
    if task_ref.get("scoped_id") != f"{task_ref.get('prd_id')}:{task_ref.get('task_id')}":
        raise ContractValidationError(
            f"deliver start receipt scoped_id does not match its reference: {task_ref.get('scoped_id')}"
        )

    if intent is not None:
        if receipt.get("intent_digest") != intent.get("intent_digest"):
            raise ContractValidationError("deliver start receipt does not echo the intent idempotency key")
        intent_ref = intent.get("task_ref")
        if isinstance(intent_ref, Mapping) and task_ref.get("scoped_id") != intent_ref.get("scoped_id"):
            raise ContractValidationError("deliver start receipt scopes to a different task than the intent")
        run = receipt.get("run")
        if isinstance(run, Mapping):
            selections = intent.get("selections")
            selections = selections if isinstance(selections, Mapping) else {}
            workflow = selections.get("workflow")
            expected_workflow_digest = workflow.get("digest") if isinstance(workflow, Mapping) else None
            if run.get("workflow_digest") != expected_workflow_digest:
                raise ContractValidationError(
                    "deliver start receipt run claims a different workflow digest than the intent selected"
                )
            if run.get("capability_profile_digest") != selections.get("capability_profile_digest"):
                raise ContractValidationError(
                    "deliver start receipt run claims a different capability-profile digest than the intent selected"
                )


def validate_bridge_command_snapshot(
    command: Mapping[str, Any], catalogs: Mapping[str, Mapping[str, Any]], profile: Mapping[str, Any],
    approval_consumer: ApprovalConsumer | None = None,
) -> None:
    """Validate the cross-resource rules JSON Schema cannot express.

    This reference validator is deliberately strict: a bridge needs a locally
    configured catalog for the requested provider, a matching immutable
    snapshot entry, a profile allowlist entry, and an approval bound to the
    exact input object for any approval-gated operation.
    """
    validate_profile(profile)
    payload = command.get("payload")
    if not isinstance(payload, Mapping) or command.get("kind") != "invoke_operation":
        return
    operation_ref = payload.get("operation")
    snapshot = command.get("workflow_snapshot")
    if not isinstance(operation_ref, Mapping) or not isinstance(snapshot, Mapping):
        raise ContractValidationError("invoke operation requires an operation and workflow snapshot")
    if snapshot.get("capability_profile_digest") != profile.get("digest"):
        raise ContractValidationError("workflow snapshot capability profile digest differs from the local profile")
    provider = str(operation_ref.get("provider", ""))
    snapshots = snapshot.get("catalogs")
    if not isinstance(snapshots, list):
        raise ContractValidationError("workflow snapshot catalogs are invalid")
    matching = [entry for entry in snapshots if isinstance(entry, Mapping) and entry.get("provider") == provider]
    if len(matching) != 1:
        raise ContractValidationError("operation provider must occur exactly once in the workflow snapshot")
    if len({str(entry.get("provider", "")) for entry in snapshots if isinstance(entry, Mapping)}) != len(snapshots):
        raise ContractValidationError("workflow snapshot catalog providers must be unique")
    catalog = catalogs.get(provider)
    if catalog is None:
        raise ContractValidationError(f"operation provider is not locally configured: {provider}")
    validate_catalog(catalog)
    if matching[0].get("digest") != catalog.get("catalog_digest"):
        raise ContractValidationError("workflow snapshot catalog digest differs from the local catalog")
    operation = next(
        (
            item for item in catalog.get("operations", [])
            if isinstance(item, Mapping)
            and item.get("id") == operation_ref.get("id")
            and item.get("contract_version") == operation_ref.get("contract_version")
            and item.get("operation_digest") == operation_ref.get("operation_digest")
        ),
        None,
    )
    if operation is None:
        raise ContractValidationError("operation is not present at the pinned local catalog revision")
    inputs = payload.get("inputs")
    if not isinstance(inputs, Mapping):
        raise ContractValidationError("operation inputs must be an object")
    input_schema = operation.get("input_schema")
    if not isinstance(input_schema, Mapping):
        raise ContractValidationError("selected operation has no object input schema")
    try:
        check_operation_schema(input_schema)
    except ContractValidationError as exc:
        raise ContractValidationError(f"selected operation input schema {exc}") from exc
    try:
        Draft202012Validator(input_schema).validate(dict(inputs))
    except ValidationError as exc:
        raise ContractValidationError(f"operation inputs do not match the selected schema: {exc.message}") from exc
    except Exception as exc:
        # A referencing/registry failure raised while evaluating the pinned
        # schema is not a ValidationError; it must still fail closed instead
        # of escaping as an unhandled crash.
        raise ContractValidationError(f"operation input schema cannot be evaluated: {exc}") from exc
    profile_operation = {
        (item.get("provider"), item.get("id"), item.get("contract_version"), item.get("operation_digest"))
        for item in profile.get("operations", []) if isinstance(item, Mapping)
    }
    operation_key = (
        operation_ref.get("provider"), operation_ref.get("id"),
        operation_ref.get("contract_version"), operation_ref.get("operation_digest"),
    )
    if operation_key not in profile_operation:
        raise ContractValidationError("operation is not allowlisted by the pinned capability profile")
    gates = operation.get("gates")
    if not isinstance(gates, Mapping) or gates.get("human_approval") != "required":
        return
    approval = payload.get("approval")
    if not isinstance(approval, Mapping):
        raise ContractValidationError("approval-gated operation requires typed approval and inputs")
    if not command.get("approval_grant_id") or approval.get("grant_id") != command.get("approval_grant_id"):
        raise ContractValidationError("approval-gated operation has no matching approval grant")
    if approval.get("action") != gates.get("approval_action"):
        raise ContractValidationError("approval action does not match the operation gate")
    if approval.get("payload_hash") != approval_payload_digest(inputs):
        raise ContractValidationError("approval hash does not bind the exact operation inputs")
    if approval_consumer is None:
        raise ContractValidationError("approval-gated operation requires an atomic approval consumer")
    try:
        approval_consumer.consume(
            str(approval["grant_id"]), str(approval["action"]), str(approval["payload_hash"]),
            str(command.get("bridge_id", "")), str(command.get("project_id", "")),
        )
    except ContractValidationError:
        raise
    except Exception as exc:
        raise ContractValidationError("approval grant is missing, expired, replayed, or not bound to this bridge/project") from exc


_PLUGIN_EFFECTFUL = ("external_effect", "state_mutation")
_PLUGIN_LIFECYCLE_ACTION = {
    "install": "install_plugin",
    "upgrade": "upgrade_plugin",
    "downgrade": "downgrade_plugin",
}
# disable/remove are management actions: an approval is not mandatory (a
# descriptor MAY require one to tighten the gate per R003), but when present its
# action must correspond to the kind and its hash must bind the subject.
_PLUGIN_MANAGEMENT_ACTION = {
    "disable": "disable_plugin",
    "remove": "remove_plugin",
}
# The single source of truth for kind->approval_action correspondence, so any
# attached approval is validated fail-closed on EVERY kind, never only lifecycle.
_PLUGIN_KIND_APPROVAL_ACTION = {
    "tool_call": "invoke_effect_tool",
    **_PLUGIN_LIFECYCLE_ACTION,
    **_PLUGIN_MANAGEMENT_ACTION,
}


def _plugin_approval_subject(request: Mapping[str, Any]) -> dict[str, Any]:
    """Build the exact typed subject a plugin request's approval must bind.

    For a ``tool_call`` the subject is the target tool plus its typed inputs; for
    a lifecycle or management action it is the pinned plugin and (for a lifecycle
    action) the selected version.  Hashing this subject (never the whole request,
    never a raw command) is what binds a one-time approval to the precise effect
    the owner reviewed.

    The subject intentionally pins ``plugin_digest`` (the installed/target
    entry's tamper-evident identity) and, for a lifecycle action, only the
    ``target_version``; ``lifecycle.from_version`` is deliberately omitted because
    the pinned ``plugin_digest`` already fixes the exact entry the effect acts on,
    so a replayed or drifted ``from_version`` cannot change what was approved.
    """
    plugin = request.get("plugin") if isinstance(request.get("plugin"), Mapping) else {}
    kind = request.get("kind")
    if kind == "tool_call":
        tool_call = request.get("tool_call") if isinstance(request.get("tool_call"), Mapping) else {}
        return {
            "plugin_id": plugin.get("plugin_id"),
            "plugin_digest": plugin.get("plugin_digest"),
            "tool_id": tool_call.get("tool_id"),
            "inputs": tool_call.get("inputs"),
        }
    lifecycle = request.get("lifecycle") if isinstance(request.get("lifecycle"), Mapping) else {}
    subject: dict[str, Any] = {
        "kind": kind,
        "plugin_id": plugin.get("plugin_id"),
        "plugin_digest": plugin.get("plugin_digest"),
    }
    if "target_version" in lifecycle:
        subject["target_version"] = lifecycle["target_version"]
    return subject


def validate_plugin_catalog(catalog: Mapping[str, Any]) -> None:
    """Fail closed when a reviewed plugin catalog has drifted or is unsafe.

    JSON Schema pins each shape and, because every object is closed, already
    makes a raw shell command, arbitrary URL/endpoint, local path, generic code
    body, or credential value unrepresentable, and makes the effect class and
    gate set mandatory (criteria 1 and 2).  These are the cross-field rules it
    cannot express:

    * every ``plugin_digest`` and the enclosing ``catalog_digest`` must
      recompute, so a tampered manifest or tool descriptor fails closed (R002);
    * plugin ids are unique, and tool ids are unique within a plugin;
    * each tool's ``input_schema``/``output_schema`` is a self-contained draft
      2020-12 object schema — a typed I/O boundary, never a generic executable
      input (criterion 1);
    * an effect-capable (non-read) tool is preview/approval-shaped: it supports a
      preview and requires a hash-bound approval (criterion 2);
    * a ``read`` tool is ungated, so a read can never silently carry an effect;
    * a ``read_only_connector`` tool is constrained to the read effect and its
      plugin pins the reviewed OpenAPI document digest it was compiled from — it
      is never ingested live or from a browser-supplied URL (R016).
    """
    try:
        plugin_catalog_contract_validator().validate(dict(catalog))
    except ValidationError as exc:
        raise ContractValidationError(f"plugin catalog is not schema valid: {exc.message}") from exc

    plugins = catalog.get("plugins")
    if not isinstance(plugins, list):
        raise ContractValidationError("plugin catalog has no plugins list")
    seen_plugins: set[str] = set()
    for plugin in plugins:
        if not isinstance(plugin, Mapping):
            raise ContractValidationError("plugin catalog entry is not an object")
        plugin_id = str(plugin.get("id"))
        if plugin_id in seen_plugins:
            raise ContractValidationError(f"duplicate plugin id: {plugin_id}")
        seen_plugins.add(plugin_id)
        if plugin.get("plugin_digest") != contract_digest("plugin", plugin):
            raise ContractValidationError(f"plugin digest mismatch: {plugin_id}")

        tools = plugin.get("tools")
        if not isinstance(tools, list):
            raise ContractValidationError(f"plugin has no tools list: {plugin_id}")
        seen_tools: set[str] = set()
        for tool in tools:
            if not isinstance(tool, Mapping):
                raise ContractValidationError(f"plugin tool is not an object: {plugin_id}")
            tool_id = str(tool.get("tool_id"))
            if tool_id in seen_tools:
                raise ContractValidationError(f"duplicate tool id in plugin {plugin_id}: {tool_id}")
            seen_tools.add(tool_id)

            for field in ("input_schema", "output_schema"):
                try:
                    check_plugin_tool_schema(tool.get(field))
                except ContractValidationError as exc:
                    raise ContractValidationError(
                        f"plugin tool {plugin_id}:{tool_id} {field} {exc}"
                    ) from exc

            effect = tool.get("effect")
            gates = tool.get("gates")
            if not isinstance(gates, Mapping):
                raise ContractValidationError(f"plugin tool has no gate set: {plugin_id}:{tool_id}")
            if effect in _PLUGIN_EFFECTFUL:
                if gates.get("preview") not in ("optional", "required"):
                    raise ContractValidationError(
                        f"effect-capable plugin tool must support a preview: {plugin_id}:{tool_id}"
                    )
                if gates.get("human_approval") != "required" or not gates.get("approval_action"):
                    raise ContractValidationError(
                        f"effect-capable plugin tool must require a hash-bound approval: {plugin_id}:{tool_id}"
                    )
                # A tool gate binds the tool-invocation action only. A catalog
                # tool cannot declare a lifecycle approval_action (install/
                # upgrade/downgrade_plugin): validate_plugin_request hardcodes
                # invoke_effect_tool for a tool_call, so any other declared gate
                # would be dead text a reviewer might trust. Refuse it here as
                # defence-in-depth behind the schema enum.
                if gates.get("approval_action") != "invoke_effect_tool":
                    raise ContractValidationError(
                        f"plugin tool approval gate must be invoke_effect_tool: {plugin_id}:{tool_id}"
                    )
            elif effect == "read":
                if gates.get("human_approval") != "not_required":
                    raise ContractValidationError(
                        f"read plugin tool must be ungated: {plugin_id}:{tool_id}"
                    )

            if tool.get("tool_kind") == "read_only_connector":
                if effect != "read":
                    raise ContractValidationError(
                        f"read-only connector tool must declare the read effect: {plugin_id}:{tool_id}"
                    )
                if not isinstance(plugin.get("openapi_source"), Mapping):
                    raise ContractValidationError(
                        f"plugin with a read-only connector tool must pin its openapi_source: {plugin_id}"
                    )

    if catalog.get("catalog_digest") != contract_digest("plugin-catalog", catalog):
        raise ContractValidationError("plugin catalog digest mismatch")


def _resolve_catalog_plugin(
    catalog: Mapping[str, Any], plugin_ref: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Return the catalog plugin at the reference's exact id and pinned digest."""
    for plugin in catalog.get("plugins", []):
        if (
            isinstance(plugin, Mapping)
            and plugin.get("id") == plugin_ref.get("plugin_id")
            and plugin.get("plugin_digest") == plugin_ref.get("plugin_digest")
        ):
            return plugin
    return None


def validate_plugin_capability(profile: Mapping[str, Any]) -> None:
    """Fail closed when a plugin capability profile has drifted or over-enables.

    JSON Schema pins the enable-only allowlist shape.  These are the cross-field
    rules it cannot express:

    * the advertised ``digest`` must recompute, so a tampered allowlist fails
      closed;
    * a plugin appears at most once, so its enabled-tool set is unambiguous;
    * the total enabled-tool count never exceeds a declared ``max_enabled_tools``
      limit.
    """
    try:
        plugin_capability_contract_validator().validate(dict(profile))
    except ValidationError as exc:
        raise ContractValidationError(f"plugin capability profile is not schema valid: {exc.message}") from exc

    if profile.get("digest") != contract_digest("plugin-capability", profile):
        raise ContractValidationError("plugin capability profile digest mismatch")

    plugins = profile.get("plugins")
    if not isinstance(plugins, list):
        raise ContractValidationError("plugin capability profile has no plugins list")
    seen: set[str] = set()
    total_tools = 0
    for entry in plugins:
        if not isinstance(entry, Mapping):
            raise ContractValidationError("plugin capability entry is not an object")
        plugin_id = str(entry.get("plugin_id"))
        if plugin_id in seen:
            raise ContractValidationError(f"duplicate plugin in capability profile: {plugin_id}")
        seen.add(plugin_id)
        tools = entry.get("enabled_tools")
        total_tools += len(tools) if isinstance(tools, list) else 0

    limits = profile.get("limits")
    if isinstance(limits, Mapping) and isinstance(limits.get("max_enabled_tools"), int):
        if total_tools > limits["max_enabled_tools"]:
            raise ContractValidationError(
                "plugin capability profile enables more tools than its declared limit"
            )


def validate_plugin_request(
    request: Mapping[str, Any], catalog: Mapping[str, Any] | None = None,
) -> None:
    """Fail closed when a plugin request is tampered with, unauthorized, or unsafe.

    JSON Schema pins the ids/typed-inputs shape and, because every object is
    closed, already makes a path, raw command, credential value, or executable
    body unrepresentable (criterion 1).  These are the cross-field rules it
    cannot express:

    * the advertised ``request_digest`` must recompute over the canonical
      content, so it is a tamper-evident idempotency key — replaying an
      identical request is the same action, and a mutated request fails closed
      (R003 idempotency);
    * an ``install``/``upgrade``/``downgrade`` carries both a ``preview_ref`` and
      a hash-bound approval whose action matches the kind and whose
      ``payload_hash`` binds the exact plugin/version subject (the R003 floor: a
      preview AND an approval);
    * an attached approval is validated fail-closed on EVERY kind — including
      ``disable``/``remove`` and a ``tool_call`` — so a bogus or mismatched
      action (e.g. ``install_plugin`` on a ``disable``) is refused, never
      silently ignored; when present its ``payload_hash`` binds the exact typed
      subject, never a raw command;
    * when a trusted ``catalog`` is supplied for a ``tool_call``, the plugin
      must be present at its pinned digest, the tool must exist, the typed
      inputs must validate against that tool's reviewed input schema, and an
      effect-capable tool call must carry an approval.
    """
    try:
        plugin_request_contract_validator().validate(dict(request))
    except ValidationError as exc:
        raise ContractValidationError(f"plugin request is not schema valid: {exc.message}") from exc

    if request.get("request_digest") != contract_digest("plugin-request", request):
        raise ContractValidationError("plugin request digest mismatch")

    kind = request.get("kind")
    approval = request.get("approval")

    # R003 floor: an install/upgrade/downgrade always carries BOTH a preview_ref
    # and a hash-bound approval. The schema already requires both, but the
    # validator enforces it independently so a request never reaches an effect
    # path on schema drift alone.
    if kind in _PLUGIN_LIFECYCLE_ACTION:
        if not isinstance(approval, Mapping):
            raise ContractValidationError(f"lifecycle {kind} requires a hash-bound approval")
        if not isinstance(request.get("preview_ref"), Mapping):
            raise ContractValidationError(f"lifecycle {kind} requires a preview_ref (R003: a preview AND an approval)")

    # Fail-closed on EVERY kind: an attached approval must name the action that
    # corresponds to the request kind and its payload_hash must bind the exact
    # typed subject. A bogus {action: install_plugin} on a disable, or an
    # unexpected approval on a read tool_call, is refused rather than ignored.
    if isinstance(approval, Mapping):
        expected_action = _PLUGIN_KIND_APPROVAL_ACTION.get(kind)
        if approval.get("action") != expected_action:
            if kind in _PLUGIN_LIFECYCLE_ACTION:
                raise ContractValidationError(f"approval action does not match the lifecycle kind: {kind}")
            if kind == "tool_call":
                raise ContractValidationError("tool-call approval action must be invoke_effect_tool")
            raise ContractValidationError(f"approval action does not match the request kind: {kind}")
        subject_hash = approval_payload_digest(_plugin_approval_subject(request))
        if approval.get("payload_hash") != subject_hash:
            if kind == "tool_call":
                raise ContractValidationError("approval hash does not bind the exact tool inputs")
            if kind in _PLUGIN_LIFECYCLE_ACTION:
                raise ContractValidationError("approval hash does not bind the exact plugin/version subject")
            raise ContractValidationError("approval hash does not bind the exact plugin subject")

    if kind == "tool_call":
        if catalog is not None:
            validate_plugin_catalog(catalog)
            plugin_ref = request.get("plugin")
            if not isinstance(plugin_ref, Mapping):
                raise ContractValidationError("plugin request has no typed plugin reference")
            plugin = _resolve_catalog_plugin(catalog, plugin_ref)
            if plugin is None:
                raise ContractValidationError("plugin is not present at the pinned catalog digest")
            tool_call = request.get("tool_call")
            if not isinstance(tool_call, Mapping):
                raise ContractValidationError("tool-call request has no tool_call block")
            tool = next(
                (item for item in plugin.get("tools", [])
                 if isinstance(item, Mapping) and item.get("tool_id") == tool_call.get("tool_id")),
                None,
            )
            if tool is None:
                raise ContractValidationError("tool is not present in the pinned plugin")
            inputs = tool_call.get("inputs")
            if not isinstance(inputs, Mapping):
                raise ContractValidationError("tool-call inputs must be an object")
            input_schema = tool.get("input_schema")
            try:
                check_plugin_tool_schema(input_schema)
            except ContractValidationError as exc:
                raise ContractValidationError(f"selected tool input schema {exc}") from exc
            try:
                Draft202012Validator(input_schema).validate(dict(inputs))
            except ValidationError as exc:
                raise ContractValidationError(
                    f"tool-call inputs do not match the selected tool schema: {exc.message}"
                ) from exc
            except Exception as exc:
                raise ContractValidationError(f"tool input schema cannot be evaluated: {exc}") from exc
            if tool.get("effect") in _PLUGIN_EFFECTFUL and not isinstance(approval, Mapping):
                raise ContractValidationError("effect-capable tool call requires an approval")


def validate_plugin_preview(
    preview: Mapping[str, Any], request: Mapping[str, Any] | None = None,
) -> None:
    """Fail closed when a plugin preview is unsafe or inconsistent with its request.

    JSON Schema pins the redacted, hash-bound preview shape.  These are the
    cross-field rules it cannot express:

    * an ``install``/``upgrade``/``downgrade`` preview must declare approval
      required with a bound ``payload_hash`` — the R003 floor is never waived by
      a preview;
    * when the originating request is supplied, the preview echoes its
      ``request_digest``, scopes to the same plugin and kind, and — when approval
      is required — its ``payload_hash`` binds the exact request subject.
    """
    try:
        plugin_preview_contract_validator().validate(dict(preview))
    except ValidationError as exc:
        raise ContractValidationError(f"plugin preview is not schema valid: {exc.message}") from exc

    kind = preview.get("kind")
    approval = preview.get("approval") if isinstance(preview.get("approval"), Mapping) else {}
    if kind in _PLUGIN_LIFECYCLE_ACTION and approval.get("required") is not True:
        raise ContractValidationError(f"lifecycle {kind} preview must require a hash-bound approval")

    if request is not None:
        if preview.get("request_digest") != request.get("request_digest"):
            raise ContractValidationError("plugin preview does not echo the previewed request digest")
        if preview.get("kind") != request.get("kind"):
            raise ContractValidationError("plugin preview kind differs from the request")
        if preview.get("plugin") != request.get("plugin"):
            raise ContractValidationError("plugin preview scopes to a different plugin than the request")
        if approval.get("required") is True:
            if approval.get("payload_hash") != approval_payload_digest(_plugin_approval_subject(request)):
                raise ContractValidationError("plugin preview approval hash does not bind the exact request subject")


def validate_plugin_receipt(
    receipt: Mapping[str, Any], request: Mapping[str, Any] | None = None,
) -> None:
    """Fail closed when a plugin receipt is unsafe or inconsistent with its request.

    JSON Schema pins the accepted/duplicate/denied/reconcile shapes and, because
    every object is closed and credential use is reference-only, already makes a
    credential value or raw tool payload unrepresentable (R004).  These are the
    cross-field rules it cannot express:

    * when the originating request is supplied, the receipt echoes its
      ``request_digest``, scopes to the same plugin and kind, and — for a
      ``tool_call`` — names the same tool, so a receipt can never be attributed
      to a different request, plugin, or tool than the one presented.
    """
    try:
        plugin_receipt_contract_validator().validate(dict(receipt))
    except ValidationError as exc:
        raise ContractValidationError(f"plugin receipt is not schema valid: {exc.message}") from exc

    if request is not None:
        if receipt.get("request_digest") != request.get("request_digest"):
            raise ContractValidationError("plugin receipt does not echo the request digest")
        if receipt.get("kind") != request.get("kind"):
            raise ContractValidationError("plugin receipt kind differs from the request")
        if receipt.get("plugin") != request.get("plugin"):
            raise ContractValidationError("plugin receipt scopes to a different plugin than the request")
        if request.get("kind") == "tool_call":
            requested_tool = request.get("tool_call", {})
            requested_tool = requested_tool.get("tool_id") if isinstance(requested_tool, Mapping) else None
            if receipt.get("tool_id") != requested_tool:
                raise ContractValidationError("plugin receipt names a different tool than the request")
