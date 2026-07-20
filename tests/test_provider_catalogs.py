"""Hermetic tests for reviewed provider-catalog discovery and publication.

Fixtures are the three T001 contract examples under
``docs/contracts/examples/`` (anvil-state, anvil-serving, project-bridge).
No live CLI is executed; the describe runner is injected everywhere and
local-JSON sources read from ``tmp_path`` copies.

Acceptance mapping (state-context-operations:T004.1):

* Criterion 1 (unknown providers, duplicate operations, conflicting
  digests, invalid schema references, unsupported versions fail closed):
  ``test_unknown_provider_*``, ``test_duplicate_operation_*``,
  ``test_conflicting_provider_digests_*``, ``test_invalid_schema_reference_*``,
  ``test_unsupported_versions_*``, plus the digest-drift/contract/transport
  fail-closed tests.
* Criterion 2 (published descriptors expose only safe metadata):
  ``test_published_view_exposes_only_safe_metadata``.
* Criterion 3 (canonicalization determinism):
  ``test_canonicalization_is_order_insensitive``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from workbench.contracts import contract_digest
from workbench.provider_catalogs import (
    DEFAULT_PROVIDER_ALLOWLIST,
    CatalogSource,
    ProviderCatalogError,
    ProviderCatalogRegistry,
    validate_provider_catalog,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "docs" / "contracts" / "examples"


def example(provider: str) -> dict:
    return json.loads((EXAMPLES / f"{provider}.catalog.v1.json").read_text(encoding="utf-8"))


def rehash(catalog: dict) -> dict:
    """Recompute digests after a deliberate fixture mutation.

    This isolates the semantic check under test from the digest-drift check,
    which has its own dedicated test below.
    """
    for operation in catalog["operations"]:
        operation["operation_digest"] = contract_digest("operation", operation)
    catalog["catalog_digest"] = contract_digest("catalog", catalog)
    return catalog


def write_catalog(tmp_path: Path, catalog: dict, name: str | None = None) -> str:
    path = tmp_path / (name or f"{catalog['provider']}.catalog.json")
    path.write_text(json.dumps(catalog), encoding="utf-8")
    return str(path)


def envelope(catalog: dict, prefix: str = "") -> str:
    return prefix + json.dumps({"ok": True, "command": "describe", "data": catalog})


def describe_runner(output: str):
    calls: list[list[str]] = []

    def runner(args) -> str:
        calls.append(list(args))
        return output

    return runner, calls


def operation_named(catalog: dict, operation_id: str) -> dict:
    return next(item for item in catalog["operations"] if item["id"] == operation_id)


def full_registry(tmp_path: Path) -> tuple[ProviderCatalogRegistry, list[list[str]]]:
    """All three configured providers: State via describe, the rest local JSON."""
    runner, calls = describe_runner(envelope(example("anvil-state"), prefix="anvil status line\n"))
    sources = [
        CatalogSource("anvil-state", "state_describe", "anvil describe"),
        CatalogSource("anvil-serving", "local_json", write_catalog(tmp_path, example("anvil-serving"))),
        CatalogSource("project-bridge", "local_json", write_catalog(tmp_path, example("project-bridge"))),
    ]
    return ProviderCatalogRegistry(sources, runner=runner), calls


def test_happy_path_publishes_every_configured_provider(tmp_path: Path) -> None:
    registry, calls = full_registry(tmp_path)

    published = registry.published()

    assert calls == [["anvil", "describe"]]
    assert published.providers == ("anvil-serving", "anvil-state", "project-bridge")
    for provider in DEFAULT_PROVIDER_ALLOWLIST:
        fixture = example(provider)
        catalog = published.catalog(provider)
        assert catalog.catalog_version == fixture["catalog_version"]
        assert catalog.catalog_digest == fixture["catalog_digest"]
        assert [operation.id for operation in catalog.operations] == [
            item["id"] for item in fixture["operations"]
        ]
        for operation, source in zip(catalog.operations, fixture["operations"]):
            assert operation.title == source["title"]
            assert operation.contract_version == source["contract_version"]
            assert operation.operation_digest == source["operation_digest"]
            assert operation.effect == source["effect"]
            assert operation.summary == source["summary"]


def test_published_snapshot_is_cached_and_frozen(tmp_path: Path) -> None:
    registry, calls = full_registry(tmp_path)

    first = registry.published()
    assert registry.published() is first, "a successful load must be cached"
    assert calls == [["anvil", "describe"]], "reuse must not rediscover"
    with pytest.raises(AttributeError):
        first.catalogs = ()  # type: ignore[misc]
    with pytest.raises(AttributeError):
        first.catalog("anvil-state").operations[0].effect = "external_effect"  # type: ignore[misc]
    # The dict projection is a fresh copy; mutating it cannot alter the registry.
    view = first.as_dict()
    view["anvil-state"]["operations"].clear()
    assert first.catalog("anvil-state").operations


def test_unknown_provider_fails_closed(tmp_path: Path) -> None:
    # A source naming a provider outside the allowlist is refused at
    # construction, before any bytes are read.
    with pytest.raises(ProviderCatalogError, match="outside the configured allowlist"):
        ProviderCatalogRegistry(
            [CatalogSource("mystery-provider", "local_json", str(tmp_path / "x.json"))]
        )
    # A catalog advertising a different provider than its reviewed source.
    impostor = example("anvil-serving")
    registry = ProviderCatalogRegistry(
        [CatalogSource("project-bridge", "local_json", write_catalog(tmp_path, impostor, "impostor.json"))]
    )
    with pytest.raises(ProviderCatalogError, match="reviewed for 'project-bridge'"):
        registry.published()
    # The validator itself also refuses a provider outside a narrowed allowlist.
    with pytest.raises(ProviderCatalogError, match="not in the configured allowlist"):
        validate_provider_catalog("anvil-serving", impostor, allowlist=("anvil-state",))


def test_duplicate_operation_declarations_fail_closed(tmp_path: Path) -> None:
    catalog = example("project-bridge")
    catalog["operations"].append(copy.deepcopy(operation_named(catalog, "bridge.github.commit_pr")))
    registry = ProviderCatalogRegistry(
        [CatalogSource("project-bridge", "local_json", write_catalog(tmp_path, rehash(catalog)))]
    )
    with pytest.raises(ProviderCatalogError, match="duplicate operation"):
        registry.published()


def test_conflicting_provider_digests_fail_closed(tmp_path: Path) -> None:
    original = example("anvil-serving")
    revised = example("anvil-serving")
    revised["catalog_version"] = "1.0.1"
    rehash(revised)
    registry = ProviderCatalogRegistry(
        [
            CatalogSource("anvil-serving", "local_json", write_catalog(tmp_path, original, "a.json")),
            CatalogSource("anvil-serving", "local_json", write_catalog(tmp_path, revised, "b.json")),
        ]
    )
    with pytest.raises(ProviderCatalogError, match="conflicting catalogs claim provider anvil-serving"):
        registry.published()

    # Two sources agreeing byte-for-digest on the same provider are one catalog.
    agreeing = ProviderCatalogRegistry(
        [
            CatalogSource("anvil-serving", "local_json", write_catalog(tmp_path, original, "c.json")),
            CatalogSource("anvil-serving", "local_json", write_catalog(tmp_path, original, "d.json")),
        ]
    )
    assert agreeing.published().providers == ("anvil-serving",)


def test_invalid_schema_reference_fails_closed(tmp_path: Path) -> None:
    broken = example("anvil-serving")
    operation_named(broken, "serving.eval.preflight")["input_schema"]["properties"]["model"]["type"] = 123
    with pytest.raises(ProviderCatalogError, match="draft 2020-12"):
        validate_provider_catalog("anvil-serving", rehash(broken))

    untyped = example("anvil-serving")
    operation_named(untyped, "serving.eval.preflight")["output_schema"] = {"type": "string"}
    with pytest.raises(ProviderCatalogError, match="typed object schema"):
        validate_provider_catalog("anvil-serving", rehash(untyped))

    foreign_dialect = example("anvil-serving")
    operation_named(foreign_dialect, "serving.eval.preflight")["input_schema"]["$schema"] = (
        "http://json-schema.org/draft-07/schema#"
    )
    with pytest.raises(ProviderCatalogError, match="unsupported dialect"):
        validate_provider_catalog("anvil-serving", rehash(foreign_dialect))


def test_unsupported_versions_fail_closed(tmp_path: Path) -> None:
    wrong_schema = example("project-bridge")
    wrong_schema["schema_version"] = "anvil-operation-catalog/v2"
    with pytest.raises(ProviderCatalogError, match="unsupported schema_version"):
        validate_provider_catalog("project-bridge", rehash(wrong_schema))

    wrong_major = example("project-bridge")
    operation_named(wrong_major, "bridge.github.commit_pr")["contract_version"] = "2.0.0"
    with pytest.raises(ProviderCatalogError, match="unsupported contract major"):
        validate_provider_catalog("project-bridge", rehash(wrong_major))

    non_semantic = example("project-bridge")
    operation_named(non_semantic, "bridge.github.commit_pr")["contract_version"] = "latest"
    with pytest.raises(ProviderCatalogError, match="operation-catalog contract"):
        validate_provider_catalog("project-bridge", rehash(non_semantic))


def test_digest_drift_fails_closed_before_any_semantic_check() -> None:
    drifted = example("anvil-serving")
    operation_named(drifted, "serving.eval.preflight")["summary"] += "!"
    with pytest.raises(ProviderCatalogError, match="digest validation"):
        validate_provider_catalog("anvil-serving", drifted)


def test_catalog_violating_the_operation_catalog_contract_fails_closed() -> None:
    catalog = example("anvil-state")
    operation_named(catalog, "state.project.snapshot")["execution"]["command"] = "state-cli"
    with pytest.raises(ProviderCatalogError, match="operation-catalog contract"):
        validate_provider_catalog("anvil-state", rehash(catalog))


def test_declared_but_unimplemented_transports_fail_closed(tmp_path: Path) -> None:
    for transport in ("http", "mcp"):
        registry = ProviderCatalogRegistry(
            [CatalogSource("anvil-serving", transport, "https://serving.tailnet/catalog")]
        )
        with pytest.raises(ProviderCatalogError, match="transport is not implemented"):
            registry.published()
    with pytest.raises(ProviderCatalogError, match="transport is not declared"):
        CatalogSource("anvil-serving", "carrier_pigeon", "somewhere")
    with pytest.raises(ProviderCatalogError, match="does not name a provider"):
        CatalogSource("  ", "local_json", "catalog.json")
    with pytest.raises(ProviderCatalogError, match="has no location"):
        CatalogSource("anvil-serving", "local_json", "  ")


def test_malformed_sources_fail_closed_and_cache_nothing(tmp_path: Path) -> None:
    missing = ProviderCatalogRegistry(
        [CatalogSource("anvil-serving", "local_json", str(tmp_path / "absent.json"))]
    )
    with pytest.raises(ProviderCatalogError, match="unreadable"):
        missing.published()

    garbage_path = tmp_path / "garbage.json"
    garbage_path.write_text("not json at all", encoding="utf-8")
    garbage = ProviderCatalogRegistry(
        [CatalogSource("anvil-serving", "local_json", str(garbage_path))]
    )
    with pytest.raises(ProviderCatalogError, match="not valid JSON"):
        garbage.published()

    non_object_path = tmp_path / "list.json"
    non_object_path.write_text("[1, 2, 3]", encoding="utf-8")
    non_object = ProviderCatalogRegistry(
        [CatalogSource("anvil-serving", "local_json", str(non_object_path))]
    )
    with pytest.raises(ProviderCatalogError, match="not a JSON object"):
        non_object.published()

    runner, calls = describe_runner("this is not a manifest")
    failing = ProviderCatalogRegistry(
        [CatalogSource("anvil-state", "state_describe", "anvil describe")], runner=runner
    )
    with pytest.raises(ProviderCatalogError, match="describe source for anvil-state failed"):
        failing.published()
    with pytest.raises(ProviderCatalogError, match="describe source for anvil-state failed"):
        failing.published()
    assert calls == [["anvil", "describe"]] * 2, "a failed load must not cache"

    refusing_envelope, _ = describe_runner(json.dumps({"ok": False, "command": "describe", "data": {}}))
    refused = ProviderCatalogRegistry(
        [CatalogSource("anvil-state", "state_describe", "anvil describe")], runner=refusing_envelope
    )
    with pytest.raises(ProviderCatalogError, match="did not report ok"):
        refused.published()


def test_one_bad_source_prevents_any_publication(tmp_path: Path) -> None:
    good = write_catalog(tmp_path, example("anvil-serving"))
    drifted = example("project-bridge")
    drifted["catalog_version"] = "tampered"
    registry = ProviderCatalogRegistry(
        [
            CatalogSource("anvil-serving", "local_json", good),
            CatalogSource("project-bridge", "local_json", write_catalog(tmp_path, drifted)),
        ]
    )
    with pytest.raises(ProviderCatalogError, match="digest validation"):
        registry.published()
    assert registry._published is None, "no partial publication after a failure"


def test_published_view_exposes_only_safe_metadata(tmp_path: Path) -> None:
    registry, _calls = full_registry(tmp_path)

    view = registry.published().as_dict()

    assert set(view) == set(DEFAULT_PROVIDER_ALLOWLIST)
    for provider, catalog in view.items():
        assert set(catalog) == {"provider", "catalog_version", "catalog_digest", "operations"}
        assert catalog["provider"] == provider
        for operation in catalog["operations"]:
            assert set(operation) == {
                "id", "title", "contract_version", "operation_digest", "effect", "summary",
            }
    serialized = json.dumps(view)
    for forbidden in (
        '"execution"', "bridge_adapter", "transport", '"command"', "path",
        "precondition", "idempotency", "receipts", "gates", '"failure"',
        "input_schema", "output_schema", "deadline", "docs",
        "state_cli", "serving_mcp", "bridge_local", "token", "secret",
    ):
        assert forbidden not in serialized, f"published view leaked {forbidden!r}"


def test_canonicalization_is_order_insensitive(tmp_path: Path) -> None:
    original = example("project-bridge")

    def reorder(value):
        if isinstance(value, dict):
            return {key: reorder(value[key]) for key in reversed(list(value))}
        if isinstance(value, list):
            return [reorder(item) for item in value]
        return value

    reordered = reorder(original)
    reordered["operations"] = list(reversed(reordered["operations"]))
    assert reordered != original or json.dumps(reordered) != json.dumps(original)

    # Criterion 3: semantically identical input produces the identical digest.
    assert contract_digest("catalog", reordered) == original["catalog_digest"]

    registry = ProviderCatalogRegistry(
        [CatalogSource("project-bridge", "local_json", write_catalog(tmp_path, reordered))]
    )
    published = registry.published().catalog("project-bridge")
    assert published.catalog_digest == original["catalog_digest"]


def test_from_settings_builds_state_describe_plus_reviewed_local_files(tmp_path: Path) -> None:
    serving_path = tmp_path / "serving.catalog.json"
    settings = SimpleNamespace(
        state_describe_command="custom-state describe --json",
        project_root=tmp_path,
        provider_catalog_files={"anvil-serving": serving_path},
    )
    registry = ProviderCatalogRegistry.from_settings(settings)
    assert registry._sources == (
        CatalogSource("anvil-state", "state_describe", "custom-state describe --json"),
        CatalogSource("anvil-serving", "local_json", str(serving_path)),
    )
    assert registry._cwd == tmp_path

    # Settings without configured files still cover the State describe path.
    bare = ProviderCatalogRegistry.from_settings(
        SimpleNamespace(state_describe_command="anvil describe", project_root=tmp_path)
    )
    assert [source.transport for source in bare._sources] == ["state_describe"]


def test_bridge_parser_accepts_reviewed_provider_catalog_files() -> None:
    from workbench.bridge import build_parser

    args = build_parser().parse_args(
        [
            "--hub", "http://hub", "--bridge-id", "b1", "--project-root", ".",
            "--project-id", "p1", "--router-base-url", "http://router",
            "--provider-catalog", "anvil-serving=/reviewed/serving.catalog.json",
        ]
    )
    assert args.provider_catalog == ["anvil-serving=/reviewed/serving.catalog.json"]


def test_relative_local_source_resolves_against_the_configured_root(tmp_path: Path) -> None:
    write_catalog(tmp_path, example("anvil-serving"), "serving.catalog.json")
    registry = ProviderCatalogRegistry(
        [CatalogSource("anvil-serving", "local_json", "serving.catalog.json")], cwd=tmp_path
    )
    assert registry.published().catalog("anvil-serving").provider == "anvil-serving"
