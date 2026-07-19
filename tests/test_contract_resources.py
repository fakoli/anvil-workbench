"""Keep proposed contract resources parseable and internally connected.

The resources deliberately use JSON Schema 2020-12, but the production package
does not need a JSON Schema dependency merely to protect documentation fixtures.
These hermetic tests cover the invariants a next implementation session needs:
valid JSON, versioned schema markers, and catalog/profile/workflow references.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs" / "contracts"
EXAMPLES = CONTRACTS / "examples"
SCHEMAS = CONTRACTS / "schemas"
DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _operation_key(operation: dict[str, object]) -> tuple[str, str, str]:
    return (
        str(operation["provider"]),
        str(operation["id"]),
        str(operation["contract_version"]),
    )


def test_contract_schemas_and_examples_are_valid_json() -> None:
    paths = sorted((*SCHEMAS.glob("*.json"), *EXAMPLES.glob("*.json")))
    assert paths
    for path in paths:
        payload = _load(path)
        assert payload, path
        if path.parent == SCHEMAS:
            assert payload["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_catalog_profile_and_workflow_examples_reference_declared_operations() -> None:
    catalogs = (
        _load(EXAMPLES / "anvil-state.catalog.v1.json"),
        _load(EXAMPLES / "anvil-serving.catalog.v1.json"),
    )
    operation_keys = {
        (str(catalog["provider"]), str(operation["id"]), str(operation["contract_version"]))
        for catalog in catalogs
        for operation in catalog["operations"]
    }

    profile = _load(EXAMPLES / "project-capability-profile.v1.json")
    profile_keys = {_operation_key(operation) for operation in profile["operations"]}
    assert profile_keys <= operation_keys

    workflow = _load(EXAMPLES / "delivery.workflow.v2.json")
    workflow_keys = {
        _operation_key(step["operation"])
        for step in workflow["steps"]
        if step["kind"] == "operation"
    }
    assert workflow_keys <= operation_keys

    proposal = _load(EXAMPLES / "model-proposal.operation-request.v1.json")
    proposal_key = _operation_key(proposal["operation"])
    assert proposal_key in profile_keys


def test_contract_examples_are_redacted_and_digest_shaped() -> None:
    example_text = "\n".join(path.read_text(encoding="utf-8").lower() for path in EXAMPLES.glob("*.json"))
    for forbidden in ("api_key", "authorization", "password", "secret", "github_token", "state.db"):
        assert forbidden not in example_text

    for path in EXAMPLES.glob("*.json"):
        for value in _walk(_load(path)):
            if isinstance(value, str) and value.startswith("sha256:"):
                assert DIGEST.fullmatch(value), (path, value)


def _walk(value: object):
    if isinstance(value, dict):
        for nested in value.values():
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)
    else:
        yield value
