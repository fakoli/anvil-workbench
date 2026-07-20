"""Schema-versioned project-snapshot adapter over the pinned State read set.

This adapter turns the pinned ``state.project.snapshot`` descriptor from
:mod:`workbench.state_manifest` into a validated, publishable projection of one
project's PRD, plan/feature, and task summaries.  It is the only supported way
for Workbench to obtain that projection: the State CLI is the transport, the
``workbench-state-snapshot/v1`` contract is the payload shape, and the snapshot
digest is the hub's idempotent publication key.

Fail-closed rules:

* The adapter takes the immutable :class:`PinnedStateReadOperations` set in its
  constructor and uses only the pinned ``state.project.snapshot`` descriptor.
  No method accepts a caller-selected operation id, so a browser- or
  model-chosen operation cannot reach the State CLI through this surface.
* A payload is validated in order -- contract schema (closed objects, bounded
  prose), pinned output schema, source provenance, then the reference
  validator (:func:`workbench.contracts.validate_state_snapshot`: digest
  recompute, owning-PRD references, ``scoped_id`` equality, uniqueness).  Any
  failure raises :class:`StateSnapshotError` before a result exists; there is
  no partial :class:`PublishableSnapshot`.
* The bounded summaries never carry full PRD Markdown.  The contract schema
  closes every object (``additionalProperties: false``) and bounds every prose
  field, and this module refuses to run against a drifted on-disk schema that
  no longer closes those objects.  Full PRD content is a separate bounded
  ``state.prd.read_content`` read, never part of this response.

Like discovery, this adapter is validated hermetically against fixtures shaped
like ``docs/contracts/examples/anvil-state.project-snapshot.v1.json``; live
qualification stays gated on the upstream State CLI advertising the operation
catalog from ``anvil describe`` (fakoli/anvil#178).  It is deliberately not
wired into the live bridge poll loop yet.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .contracts import ContractValidationError, validate_state_snapshot
from .state_manifest import (
    PROJECT_SNAPSHOT_OPERATION_ID,
    PinnedStateOperation,
    PinnedStateReadOperations,
)


class StateSnapshotError(RuntimeError):
    """The State project snapshot cannot be trusted for publication."""


SNAPSHOT_SCHEMA_VERSION = "workbench-state-snapshot/v1"

_SNAPSHOT_CONTRACT_SCHEMA = (
    Path(__file__).resolve().parents[1] / "docs" / "contracts" / "schemas" / "state-snapshot.v1.schema.json"
)
_snapshot_contract_validator_cache: Draft202012Validator | None = None

#: Object paths in the contract schema that must stay closed so a payload
#: cannot smuggle Markdown-scale content in an undeclared field.
_CLOSED_OBJECT_PATHS: tuple[tuple[str, ...], ...] = (
    (),
    ("properties", "source"),
    ("properties", "project"),
    ("properties", "prds", "items"),
    ("properties", "tasks", "items"),
)


def _schema_node(schema: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = schema
    for name in path:
        node = node.get(name) if isinstance(node, Mapping) else None
    return node


def _snapshot_contract_validator() -> Draft202012Validator:
    """Load the snapshot contract schema once; refuse a drifted or open schema."""
    global _snapshot_contract_validator_cache
    if _snapshot_contract_validator_cache is None:
        try:
            schema = json.loads(_SNAPSHOT_CONTRACT_SCHEMA.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateSnapshotError(
                "state-snapshot contract schema is unavailable; refusing to publish snapshots"
            ) from exc
        for path in _CLOSED_OBJECT_PATHS:
            node = _schema_node(schema, path)
            if not isinstance(node, Mapping) or node.get("additionalProperties") is not False:
                raise StateSnapshotError(
                    "state-snapshot contract schema no longer closes its objects "
                    f"(additionalProperties must be false at {'/'.join(path) or '<root>'}); "
                    "refusing to publish snapshots"
                )
        _snapshot_contract_validator_cache = Draft202012Validator(schema)
    return _snapshot_contract_validator_cache


@dataclass(frozen=True)
class PublishableSnapshot:
    """One fully validated snapshot; ``snapshot_digest`` is the publication key."""

    provider: str
    schema_version: str
    snapshot_digest: str
    operation_id: str
    operation_digest: str
    project_id: str
    scoped_task_ids: tuple[str, ...]
    payload_json: str

    @property
    def payload(self) -> dict[str, Any]:
        """A fresh parsed copy; mutating it cannot alter the validated snapshot."""
        return json.loads(self.payload_json)


def _json_document(raw: str) -> dict[str, Any]:
    """Parse a JSON document even when the State CLI emits a status line first.

    Mirrors ``StateReader._json_document`` in :mod:`workbench.bridge`.
    """
    decoder = json.JSONDecoder()
    for position, character in enumerate(raw):
        if character not in "[{":
            continue
        try:
            value, end = decoder.raw_decode(raw[position:])
        except json.JSONDecodeError:
            continue
        if raw[position + end :].strip():
            continue
        if isinstance(value, dict):
            return value
    raise StateSnapshotError("State snapshot command must return one JSON object")


def _payload_from_document(document: Mapping[str, Any]) -> Mapping[str, Any]:
    """Unwrap the State CLI envelope, if present, and return the payload object."""
    if "ok" in document or "data" in document:
        if document.get("ok") is not True:
            raise StateSnapshotError("State snapshot envelope did not report ok")
        data = document.get("data")
        if not isinstance(data, Mapping):
            raise StateSnapshotError("State snapshot envelope has no data object")
        return data
    return document


def validate_snapshot_payload(
    payload: Mapping[str, Any], operation: PinnedStateOperation,
) -> PublishableSnapshot:
    """Fail-closed validate one snapshot payload against the pinned descriptor.

    Order matters: the contract schema rejects schema drift, unbounded prose,
    and smuggled fields first; the pinned output schema then binds the payload
    to the descriptor the manifest advertised; source provenance binds it to
    the pinned operation identity; and the reference validator finally checks
    what schemas cannot -- digest recompute, owning-PRD references, scoped-id
    equality, and uniqueness.  Nothing is returned unless every check passes.
    """
    if operation.operation_id != PROJECT_SNAPSHOT_OPERATION_ID:
        raise StateSnapshotError(
            f"snapshot adapter only executes {PROJECT_SNAPSHOT_OPERATION_ID}, "
            f"got {operation.operation_id}"
        )
    materialized = dict(payload)
    try:
        _snapshot_contract_validator().validate(materialized)
    except ValidationError as exc:
        raise StateSnapshotError(
            f"State snapshot payload does not conform to the {SNAPSHOT_SCHEMA_VERSION} "
            f"contract: {exc.message}"
        ) from exc
    try:
        Draft202012Validator(operation.output_schema).validate(materialized)
    except ValidationError as exc:
        raise StateSnapshotError(
            f"State snapshot payload does not match the pinned output schema: {exc.message}"
        ) from exc
    source = materialized["source"]
    if source.get("read_operation_id") != operation.operation_id:
        raise StateSnapshotError(
            "State snapshot names a read operation other than the pinned one: "
            f"{source.get('read_operation_id')!r}"
        )
    if source.get("provider_contract_version") != operation.contract_version:
        raise StateSnapshotError(
            "State snapshot declares a provider contract version other than the pinned "
            f"{operation.contract_version}: {source.get('provider_contract_version')!r}"
        )
    if materialized.get("provider") != operation.provider:
        raise StateSnapshotError(
            f"State snapshot names an unexpected provider: {materialized.get('provider')!r}"
        )
    try:
        validate_state_snapshot(materialized)
    except ContractValidationError as exc:
        raise StateSnapshotError(f"State snapshot failed reference validation: {exc}") from exc
    scoped_task_ids = tuple(str(task["scoped_id"]) for task in materialized["tasks"])
    return PublishableSnapshot(
        provider=str(materialized["provider"]),
        schema_version=str(materialized["schema_version"]),
        snapshot_digest=str(materialized["snapshot_digest"]),
        operation_id=operation.operation_id,
        operation_digest=operation.operation_digest,
        project_id=str(materialized["project"]["project_id"]),
        scoped_task_ids=scoped_task_ids,
        payload_json=json.dumps(
            materialized, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ),
    )


class StateSnapshotAdapter:
    """Execute the pinned project-snapshot read and validate it for publication.

    The runner is injectable for hermetic tests; the default runner executes
    the bridge-configured snapshot command argv (never a hardcoded path).  The
    runner always receives the pinned descriptor, so the transport can attest
    exactly which advertised operation it is executing -- there is no
    parameter anywhere on this class through which a caller could name a
    different operation.
    """

    def __init__(
        self,
        pinned: PinnedStateReadOperations,
        snapshot_command: str,
        runner: Callable[[PinnedStateOperation, Sequence[str]], str] | None = None,
        cwd: Path | None = None,
    ) -> None:
        if not snapshot_command.strip():
            raise StateSnapshotError("State snapshot command is not configured")
        self._operation = pinned.descriptor(PROJECT_SNAPSHOT_OPERATION_ID)
        self._snapshot_command = snapshot_command
        self._runner = runner if runner is not None else self._run_snapshot
        self._cwd = cwd

    def fetch(self) -> PublishableSnapshot:
        """Run the pinned read once and return the validated publishable result."""
        args = shlex.split(self._snapshot_command, posix=os.name != "nt")
        document = _json_document(self._runner(self._operation, args))
        return validate_snapshot_payload(_payload_from_document(document), self._operation)

    def _run_snapshot(self, operation: PinnedStateOperation, args: Sequence[str]) -> str:
        completed = subprocess.run(
            list(args), cwd=self._cwd, capture_output=True, text=True, check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[:500]
            raise StateSnapshotError(
                f"State snapshot command failed ({operation.bridge_adapter}): {detail}"
            )
        return completed.stdout
