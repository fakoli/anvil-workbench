"""Deterministic helpers for proposed Workbench operation-layer resources.

The v1 bridge does not dispatch these resources yet.  Keeping the digest and
snapshot checks in a small stdlib-only module gives the V2 implementation one
authoritative, testable interpretation instead of each adapter inventing one.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


class ContractValidationError(ValueError):
    """A proposed contract resource or immutable execution snapshot is unsafe."""


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
    "skill": b"anvil-workbench/skill/v1\0",
    "approval-payload": b"anvil-workbench/approval-payload/v1\0",
    "state-snapshot": b"anvil-workbench/state-snapshot/v1\0",
    "prd-content": b"anvil-workbench/prd-content/v1\0",
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
        for dependency in task.get("depends_on", ()):
            if not isinstance(dependency, Mapping) or dependency.get("prd_id") not in prd_ids:
                raise ContractValidationError("task dependency names an unknown PRD")


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
        Draft202012Validator(input_schema).validate(dict(inputs))
    except ValidationError as exc:
        raise ContractValidationError(f"operation inputs do not match the selected schema: {exc.message}") from exc
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
