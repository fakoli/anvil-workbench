"""Discover and pin Anvil State CLI read operations for Workbench adapters.

``anvil describe`` is the State provider's machine-readable manifest.  This
module resolves the two read-only context operations Workbench may depend on --
``state.project.snapshot`` and ``state.prd.read_content`` -- and pins their
exact descriptors (identifier, contract version, input/output schemas, effect
class, and digests) before any adapter activation or project-data access.

Fail-closed rules:

* Only the live provider manifest is trusted.  There is no fallback discovery
  and no partial activation: a missing operation, a non-``read`` effect class,
  an incompatible contract major, an invalid or non-object schema, or a digest
  that does not recompute aborts discovery with :class:`StateManifestError`
  before any descriptor is exposed.
* Downstream adapters take the immutable :class:`PinnedStateReadOperations`
  set in their constructor and resolve descriptors only through
  :meth:`PinnedStateReadOperations.descriptor`.  A browser- or model-selected
  operation id outside the pinned read set is rejected; nothing re-runs
  discovery per call.

Live qualification is gated on the upstream State contract: the current
``anvil describe`` envelope reports CLI subcommands and MCP tool names but
does not yet advertise an ``anvil-operation-catalog/v1`` catalog containing
these read operations (tracked upstream as fakoli/anvil#178).  Until that
lands, discovery is validated against fixture manifests shaped like
``docs/contracts/examples/anvil-state.catalog.v1.json``.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .contracts import ContractValidationError, validate_catalog


class StateManifestError(RuntimeError):
    """The State provider manifest cannot be trusted for read operations."""


PROJECT_SNAPSHOT_OPERATION_ID = "state.project.snapshot"
PRD_READ_CONTENT_OPERATION_ID = "state.prd.read_content"

STATE_PROVIDER = "anvil-state"
_CATALOG_SCHEMA_VERSION = "anvil-operation-catalog/v1"
_DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
_COMPATIBLE_CONTRACT_MAJOR = 1
_CONTRACT_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


@dataclass(frozen=True)
class PinnedStateOperation:
    """One immutable, digest-attributed State read-operation descriptor."""

    provider: str
    operation_id: str
    title: str
    contract_version: str
    effect: str
    operation_digest: str
    bridge_adapter: str
    input_schema_json: str
    output_schema_json: str

    @property
    def input_schema(self) -> dict[str, Any]:
        return json.loads(self.input_schema_json)

    @property
    def output_schema(self) -> dict[str, Any]:
        return json.loads(self.output_schema_json)


@dataclass(frozen=True)
class PinnedStateReadOperations:
    """The complete pinned read set; the only path to State read descriptors."""

    provider: str
    catalog_version: str
    catalog_digest: str
    project_snapshot: PinnedStateOperation
    prd_read_content: PinnedStateOperation

    @property
    def operations(self) -> tuple[PinnedStateOperation, ...]:
        return (self.project_snapshot, self.prd_read_content)

    def descriptor(self, operation_id: str) -> PinnedStateOperation:
        """Return a pinned descriptor; reject any unpinned/selected operation."""
        for operation in self.operations:
            if operation.operation_id == operation_id:
                return operation
        raise StateManifestError(
            f"operation is not pinned for State reads: {operation_id}"
        )


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
    raise StateManifestError("State describe command must return one JSON object")


def _catalog_from_manifest(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    """Unwrap the State CLI envelope and locate the provider operation catalog."""
    source: Mapping[str, Any] = manifest
    if "ok" in manifest or "data" in manifest:
        if manifest.get("ok") is not True:
            raise StateManifestError("State describe envelope did not report ok")
        command = manifest.get("command")
        if command is not None and command != "describe":
            raise StateManifestError(
                f"State describe envelope reports an unexpected command: {command!r}"
            )
        data = manifest.get("data")
        if not isinstance(data, Mapping):
            raise StateManifestError("State describe envelope has no data object")
        source = data
    if source.get("schema_version") == _CATALOG_SCHEMA_VERSION:
        return source
    nested = source.get("operation_catalog")
    if isinstance(nested, Mapping) and nested.get("schema_version") == _CATALOG_SCHEMA_VERSION:
        return nested
    raise StateManifestError(
        "State describe manifest does not advertise an "
        f"{_CATALOG_SCHEMA_VERSION} operation catalog"
    )


def _compatible_contract_version(operation_id: str, version: Any) -> str:
    match = _CONTRACT_VERSION.fullmatch(str(version))
    if match is None:
        raise StateManifestError(
            f"State operation {operation_id} contract_version is not semantic: {version!r}"
        )
    if int(match.group(1)) != _COMPATIBLE_CONTRACT_MAJOR:
        raise StateManifestError(
            f"State operation {operation_id} has an incompatible contract major: "
            f"{version} (compatible major is {_COMPATIBLE_CONTRACT_MAJOR})"
        )
    return str(version)


def _schema_json(operation_id: str, name: str, schema: Any) -> str:
    if not isinstance(schema, Mapping):
        raise StateManifestError(f"State operation {operation_id} has no {name} object")
    declared = schema.get("$schema")
    if declared is not None and declared != _DRAFT_2020_12:
        raise StateManifestError(
            f"State operation {operation_id} {name} declares an unsupported dialect: {declared!r}"
        )
    if schema.get("type") != "object":
        raise StateManifestError(
            f"State operation {operation_id} {name} must be a typed object schema"
        )
    try:
        Draft202012Validator.check_schema(dict(schema))
    except SchemaError as exc:
        raise StateManifestError(
            f"State operation {operation_id} {name} is not a valid draft 2020-12 schema: "
            f"{exc.message}"
        ) from exc
    return json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _pin_operation(provider: str, catalog: Mapping[str, Any], operation_id: str) -> PinnedStateOperation:
    operations = catalog.get("operations")
    candidates = [
        operation for operation in (operations if isinstance(operations, list) else [])
        if isinstance(operation, Mapping) and operation.get("id") == operation_id
    ]
    if not candidates:
        raise StateManifestError(
            f"State manifest is missing required read operation: {operation_id}"
        )
    if len(candidates) != 1:
        raise StateManifestError(
            f"State manifest declares required read operation ambiguously: {operation_id}"
        )
    operation = candidates[0]
    effect = operation.get("effect")
    if effect != "read":
        raise StateManifestError(
            f"State operation {operation_id} must declare the read effect class, got {effect!r}"
        )
    contract_version = _compatible_contract_version(operation_id, operation.get("contract_version"))
    execution = operation.get("execution")
    if not isinstance(execution, Mapping) or execution.get("kind") != "bridge_adapter":
        raise StateManifestError(
            f"State operation {operation_id} must execute through a bridge adapter"
        )
    if execution.get("transport") != "state_cli":
        raise StateManifestError(
            f"State operation {operation_id} must use the state_cli transport, "
            f"got {execution.get('transport')!r}"
        )
    bridge_adapter = execution.get("bridge_adapter")
    if not isinstance(bridge_adapter, str) or not bridge_adapter:
        raise StateManifestError(
            f"State operation {operation_id} does not name its bridge adapter"
        )
    return PinnedStateOperation(
        provider=provider,
        operation_id=operation_id,
        title=str(operation.get("title", "")),
        contract_version=contract_version,
        effect="read",
        operation_digest=str(operation.get("operation_digest", "")),
        bridge_adapter=bridge_adapter,
        input_schema_json=_schema_json(operation_id, "input_schema", operation.get("input_schema")),
        output_schema_json=_schema_json(operation_id, "output_schema", operation.get("output_schema")),
    )


def pin_state_read_operations(
    manifest: Mapping[str, Any], provider: str = STATE_PROVIDER,
) -> PinnedStateReadOperations:
    """Resolve, validate, and pin both State read operations from one manifest.

    Digest verification (`workbench.contracts.validate_catalog`) runs before
    any per-operation check, so a drifted or unadvertised digest fails closed
    even when the individual descriptors would otherwise look acceptable.
    """
    catalog = _catalog_from_manifest(manifest)
    advertised_provider = catalog.get("provider")
    if advertised_provider != provider:
        raise StateManifestError(
            f"State manifest names an unexpected provider: {advertised_provider!r}"
        )
    try:
        validate_catalog(catalog)
    except ContractValidationError as exc:
        raise StateManifestError(f"State manifest failed digest validation: {exc}") from exc
    return PinnedStateReadOperations(
        provider=provider,
        catalog_version=str(catalog.get("catalog_version", "")),
        catalog_digest=str(catalog.get("catalog_digest", "")),
        project_snapshot=_pin_operation(provider, catalog, PROJECT_SNAPSHOT_OPERATION_ID),
        prd_read_content=_pin_operation(provider, catalog, PRD_READ_CONTENT_OPERATION_ID),
    )


class StateManifestDiscovery:
    """Run the configured State ``describe`` command once and pin its read set.

    The runner is injectable for hermetic tests; the default runner executes
    the bridge-configured CLI argv (never a hardcoded path) exactly like the
    other ``StateReader`` commands.  A successful discovery is cached so
    downstream adapter calls reuse the pinned descriptors instead of
    rediscovering; a failed discovery caches nothing.
    """

    def __init__(
        self,
        describe_command: str,
        runner: Callable[[Sequence[str]], str] | None = None,
        cwd: Path | None = None,
        provider: str = STATE_PROVIDER,
    ) -> None:
        if not describe_command.strip():
            raise StateManifestError("State describe command is not configured")
        self._describe_command = describe_command
        self._runner = runner if runner is not None else self._run_describe
        self._cwd = cwd
        self._provider = provider
        self._pinned: PinnedStateReadOperations | None = None

    @classmethod
    def from_settings(cls, settings: Any) -> StateManifestDiscovery:
        """Build discovery from ``BridgeSettings``-shaped bridge configuration."""
        return cls(settings.state_describe_command, cwd=settings.project_root)

    def pinned(self) -> PinnedStateReadOperations:
        if self._pinned is None:
            args = shlex.split(self._describe_command, posix=os.name != "nt")
            manifest = _json_document(self._runner(args))
            self._pinned = pin_state_read_operations(manifest, self._provider)
        return self._pinned

    def _run_describe(self, args: Sequence[str]) -> str:
        completed = subprocess.run(
            list(args), cwd=self._cwd, capture_output=True, text=True, check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[:500]
            raise StateManifestError(f"State describe command failed: {detail}")
        return completed.stdout
