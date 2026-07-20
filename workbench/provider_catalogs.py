"""Discover and validate reviewed provider operation catalogs for publication.

T002.1 (:mod:`workbench.state_manifest`) pins the two State read operations
from one provider's live manifest.  This module generalizes that discovery to
the configured provider set (``anvil-state``, ``anvil-serving``,
``project-bridge``): a :class:`ProviderCatalogRegistry` loads each provider's
``anvil-operation-catalog/v1`` descriptor from an operator-reviewed local
source, fail-closed validates identity, versions, effect classes, schema
references, and canonical digests, and exposes only a frozen, safe public
projection.

Fail-closed rules:

* Only operator-configured sources are trusted.  A source naming a provider
  outside the configured allowlist, a catalog advertising a provider other
  than the one its source was reviewed for, a digest that does not recompute,
  a duplicate ``(id, contract_version)`` operation, two catalogs claiming the
  same provider with different digests, an invalid, non-object, remote, or
  unresolvable draft 2020-12 schema reference, or an unsupported
  schema/contract version aborts
  the whole load with :class:`ProviderCatalogError`; nothing is published and
  nothing is cached.
* The implemented transports are ``local_json`` (an operator-reviewed catalog
  file) and ``state_describe`` (the existing bridge-configured State describe
  command).  ``http`` and ``mcp`` are declared source transports but are not
  implemented; selecting one fails closed instead of stubbing network code.
* The published projection contains identifiers, titles, versions, effect
  classes, summaries, digests, and the validated input/output schemas -- never
  execution blocks, adapters, transports, commands, paths, preconditions,
  credentials, or raw provider payloads.  Execution, adapter, and transport
  blocks stay private to the bridge; the schemas are published because
  preflight and typed model proposals need the exact pinned input contract.

Like the T002.1 discovery, this registry is implemented and hermetically
tested but not wired into the live bridge poll loop; live qualification stays
gated on providers actually serving these catalogs (fakoli/anvil#178).
"""
from __future__ import annotations

import copy
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from jsonschema.exceptions import ValidationError

from .contracts import (
    ContractValidationError,
    catalog_contract_validator,
    check_operation_schema,
    validate_catalog,
)
from .state_manifest import StateManifestError, _catalog_from_manifest, _json_document


class ProviderCatalogError(RuntimeError):
    """A provider catalog source or descriptor cannot be trusted for publication."""


CATALOG_SCHEMA_VERSION = "anvil-operation-catalog/v1"
DEFAULT_PROVIDER_ALLOWLIST = ("anvil-state", "anvil-serving", "project-bridge")

#: Declared source transports.  ``http`` and ``mcp`` are reserved for later
#: reviewed implementations; selecting one fails closed at load time.
SOURCE_TRANSPORTS = ("local_json", "state_describe", "http", "mcp")
_IMPLEMENTED_TRANSPORTS = frozenset({"local_json", "state_describe"})

_COMPATIBLE_CONTRACT_MAJOR = 1
_CONTRACT_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


@dataclass(frozen=True)
class CatalogSource:
    """One operator-reviewed local origin for a single provider's catalog.

    ``location`` is a catalog file path for ``local_json`` and a CLI command
    string for ``state_describe``.  It is bridge configuration, never a
    browser- or model-supplied value, and it is not part of any published
    projection.
    """

    provider: str
    transport: str
    location: str

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise ProviderCatalogError("catalog source does not name a provider")
        if self.transport not in SOURCE_TRANSPORTS:
            raise ProviderCatalogError(
                f"catalog source transport is not declared: {self.transport!r}"
            )
        if not self.location.strip():
            raise ProviderCatalogError(
                f"catalog source for {self.provider} has no location"
            )


@dataclass(frozen=True)
class PublishedOperation:
    """The safe public metadata for one validated catalog operation.

    ``input_schema``/``output_schema`` are deep copies of the validated,
    self-contained draft 2020-12 schemas: preflight and typed model proposals
    need the exact pinned input contract (acceptance criterion 2).  Execution,
    adapter, transport, gate, and receipt blocks remain bridge-private.
    """

    id: str
    title: str
    contract_version: str
    operation_digest: str
    effect: str
    summary: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "contract_version": self.contract_version,
            "operation_digest": self.operation_digest,
            "effect": self.effect,
            "summary": self.summary,
            "input_schema": copy.deepcopy(dict(self.input_schema)),
            "output_schema": copy.deepcopy(dict(self.output_schema)),
        }


@dataclass(frozen=True)
class PublishedCatalog:
    """The safe public projection of one provider's validated catalog."""

    provider: str
    catalog_version: str
    catalog_digest: str
    operations: tuple[PublishedOperation, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "catalog_version": self.catalog_version,
            "catalog_digest": self.catalog_digest,
            "operations": [operation.as_dict() for operation in self.operations],
        }


@dataclass(frozen=True)
class PublishedCatalogSet:
    """The frozen registry snapshot: every validated provider catalog."""

    catalogs: tuple[PublishedCatalog, ...]

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(catalog.provider for catalog in self.catalogs)

    def catalog(self, provider: str) -> PublishedCatalog:
        for catalog in self.catalogs:
            if catalog.provider == provider:
                return catalog
        raise ProviderCatalogError(f"provider has no published catalog: {provider}")

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return {catalog.provider: catalog.as_dict() for catalog in self.catalogs}


def _validated_contract_version(provider: str, operation_id: str, version: Any) -> str:
    match = _CONTRACT_VERSION.fullmatch(str(version))
    if match is None:
        raise ProviderCatalogError(
            f"{provider} operation {operation_id} contract_version is not semantic: {version!r}"
        )
    if int(match.group(1)) != _COMPATIBLE_CONTRACT_MAJOR:
        raise ProviderCatalogError(
            f"{provider} operation {operation_id} has an unsupported contract major: "
            f"{version} (supported major is {_COMPATIBLE_CONTRACT_MAJOR})"
        )
    return str(version)


def _check_operation_schema(provider: str, operation_id: str, name: str, schema: Any) -> None:
    if not isinstance(schema, Mapping):
        raise ProviderCatalogError(f"{provider} operation {operation_id} has no {name} object")
    try:
        check_operation_schema(schema)
    except ContractValidationError as exc:
        raise ProviderCatalogError(
            f"{provider} operation {operation_id} {name} {exc}"
        ) from exc


def validate_provider_catalog(
    provider: str, catalog: Any, allowlist: frozenset[str] | Sequence[str] = DEFAULT_PROVIDER_ALLOWLIST,
) -> PublishedCatalog:
    """Fail-closed validate one provider catalog and return its safe projection.

    Digest verification runs before any per-operation check, mirroring
    :func:`workbench.state_manifest.pin_state_read_operations`, so a drifted
    catalog fails closed even when its descriptors would otherwise pass.
    """
    if provider not in set(allowlist):
        raise ProviderCatalogError(f"provider is not in the configured allowlist: {provider!r}")
    if not isinstance(catalog, Mapping):
        raise ProviderCatalogError(f"catalog for {provider} is not a JSON object")
    advertised = catalog.get("provider")
    if advertised != provider:
        raise ProviderCatalogError(
            f"catalog advertises provider {advertised!r} but its source is reviewed for {provider!r}"
        )
    schema_version = catalog.get("schema_version")
    if schema_version != CATALOG_SCHEMA_VERSION:
        raise ProviderCatalogError(
            f"catalog for {provider} declares an unsupported schema_version: {schema_version!r}"
        )
    try:
        validate_catalog(catalog)
    except ContractValidationError as exc:
        raise ProviderCatalogError(f"catalog for {provider} failed digest validation: {exc}") from exc
    try:
        validator = catalog_contract_validator()
    except ContractValidationError as exc:
        raise ProviderCatalogError(str(exc)) from exc
    try:
        validator.validate(dict(catalog))
    except ValidationError as exc:
        raise ProviderCatalogError(
            f"catalog for {provider} does not conform to the operation-catalog contract: {exc.message}"
        ) from exc
    published: list[PublishedOperation] = []
    seen: set[tuple[str, str]] = set()
    for operation in catalog["operations"]:
        operation_id = str(operation["id"])
        contract_version = _validated_contract_version(provider, operation_id, operation.get("contract_version"))
        key = (operation_id, contract_version)
        if key in seen:
            raise ProviderCatalogError(
                f"catalog for {provider} declares a duplicate operation: {operation_id} {contract_version}"
            )
        seen.add(key)
        _check_operation_schema(provider, operation_id, "input_schema", operation.get("input_schema"))
        _check_operation_schema(provider, operation_id, "output_schema", operation.get("output_schema"))
        published.append(
            PublishedOperation(
                id=operation_id,
                title=str(operation["title"]),
                contract_version=contract_version,
                operation_digest=str(operation["operation_digest"]),
                effect=str(operation["effect"]),
                summary=str(operation["summary"]),
                input_schema=copy.deepcopy(dict(operation["input_schema"])),
                output_schema=copy.deepcopy(dict(operation["output_schema"])),
            )
        )
    return PublishedCatalog(
        provider=provider,
        catalog_version=str(catalog["catalog_version"]),
        catalog_digest=str(catalog["catalog_digest"]),
        operations=tuple(published),
    )


class ProviderCatalogRegistry:
    """Load and publish reviewed provider catalogs from configured sources.

    The runner is injectable for hermetic tests; the default runner executes
    the configured describe argv exactly like :class:`StateManifestDiscovery`.
    A successful load caches the frozen published set; a failed load caches
    nothing and publishes nothing (no partial publication).
    """

    def __init__(
        self,
        sources: Sequence[CatalogSource],
        providers: Sequence[str] = DEFAULT_PROVIDER_ALLOWLIST,
        runner: Callable[[Sequence[str]], str] | None = None,
        cwd: Path | None = None,
    ) -> None:
        self._sources = tuple(sources)
        self._providers = frozenset(providers)
        for source in self._sources:
            if source.provider not in self._providers:
                raise ProviderCatalogError(
                    f"catalog source names a provider outside the configured allowlist: {source.provider!r}"
                )
        self._runner = runner if runner is not None else self._run_command
        self._cwd = cwd
        self._published: PublishedCatalogSet | None = None

    @classmethod
    def from_settings(cls, settings: Any) -> ProviderCatalogRegistry:
        """Build the registry from ``BridgeSettings``-shaped configuration.

        The anvil-state catalog always comes from the bridge-configured
        describe command; any additional provider catalogs come from the
        operator's ``--provider-catalog PROVIDER=PATH`` reviewed local files.
        """
        sources: list[CatalogSource] = [
            CatalogSource("anvil-state", "state_describe", settings.state_describe_command)
        ]
        for provider, path in sorted(getattr(settings, "provider_catalog_files", {}).items()):
            sources.append(CatalogSource(provider, "local_json", str(path)))
        return cls(sources, cwd=settings.project_root)

    def published(self) -> PublishedCatalogSet:
        if self._published is None:
            digests: dict[str, str] = {}
            catalogs: dict[str, PublishedCatalog] = {}
            for source in self._sources:
                document = self._load_source(source)
                validated = validate_provider_catalog(source.provider, document, self._providers)
                prior = digests.get(validated.provider)
                if prior is not None:
                    if prior != validated.catalog_digest:
                        raise ProviderCatalogError(
                            f"conflicting catalogs claim provider {validated.provider}: "
                            f"{prior} vs {validated.catalog_digest}"
                        )
                    continue
                digests[validated.provider] = validated.catalog_digest
                catalogs[validated.provider] = validated
            self._published = PublishedCatalogSet(
                catalogs=tuple(catalogs[provider] for provider in sorted(catalogs))
            )
        return self._published

    def _load_source(self, source: CatalogSource) -> Any:
        if source.transport not in _IMPLEMENTED_TRANSPORTS:
            raise ProviderCatalogError(
                f"catalog source transport is not implemented: {source.transport} "
                f"(provider {source.provider}); refusing to load"
            )
        if source.transport == "local_json":
            path = Path(source.location)
            if not path.is_absolute() and self._cwd is not None:
                path = self._cwd / path
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ProviderCatalogError(
                    f"catalog file for {source.provider} is unreadable: {path.name}"
                ) from exc
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ProviderCatalogError(
                    f"catalog file for {source.provider} is not valid JSON"
                ) from exc
        args = shlex.split(source.location, posix=os.name != "nt")
        try:
            manifest = _json_document(self._runner(args))
            return _catalog_from_manifest(manifest)
        except StateManifestError as exc:
            raise ProviderCatalogError(
                f"describe source for {source.provider} failed: {exc}"
            ) from exc

    def _run_command(self, args: Sequence[str]) -> str:
        completed = subprocess.run(
            list(args), cwd=self._cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[:500]
            raise ProviderCatalogError(f"catalog describe command failed: {detail}")
        return completed.stdout
