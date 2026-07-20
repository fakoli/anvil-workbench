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
