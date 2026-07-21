"""Hermetic tests for the bounded PRD-content read adapter.

Fixture payloads are built from the T001 contract example
(``docs/contracts/examples/anvil-state.prd-content.v1.json``) wrapped in the
real State CLI JSON envelope shape, and the pinned descriptor set comes from
the T002.1 catalog example.  No live State CLI is executed; the runner is
injected everywhere.
"""

from __future__ import annotations

import copy
import inspect
import json
import subprocess
from pathlib import Path

import pytest

from workbench import prd_content_adapter as adapter_module
from workbench.contracts import contract_digest
from workbench.state_manifest import (
    PRD_READ_CONTENT_OPERATION_ID,
    PinnedStateOperation,
    pin_state_read_operations,
)
from workbench.prd_content_adapter import (
    PrdContentAdapter,
    PrdContentError,
    PublishablePrdContent,
    validate_prd_content_payload,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CATALOG = ROOT / "docs" / "contracts" / "examples" / "anvil-state.catalog.v1.json"
EXAMPLE_CONTENT = ROOT / "docs" / "contracts" / "examples" / "anvil-state.prd-content.v1.json"


def pinned_operations():
    return pin_state_read_operations(json.loads(EXAMPLE_CATALOG.read_text(encoding="utf-8")))


def example_content() -> dict:
    return json.loads(EXAMPLE_CONTENT.read_text(encoding="utf-8"))


def rehash(document: dict) -> dict:
    """Recompute the digest after a deliberate fixture mutation.

    This isolates the semantic check under test from the digest check, which
    has its own dedicated drift test below.
    """
    document["content_digest"] = contract_digest("prd-content", document)
    return document


def envelope(payload: dict, prefix: str = "") -> str:
    return prefix + json.dumps({"ok": True, "command": "prd-content", "data": payload})


def adapter_for(
    output: str, command: str = "anvil prd show --json",
) -> tuple[PrdContentAdapter, list[tuple[PinnedStateOperation, list[str]]]]:
    calls: list[tuple[PinnedStateOperation, list[str]]] = []

    def runner(operation: PinnedStateOperation, args) -> str:
        calls.append((operation, list(args)))
        return output

    return PrdContentAdapter(pinned_operations(), command, runner=runner), calls


def test_happy_path_returns_bounded_publishable_content() -> None:
    document = example_content()
    adapter, calls = adapter_for(envelope(document, prefix="anvil workbench fixture\n"))

    result = adapter.fetch("release-beta")

    assert isinstance(result, PublishablePrdContent)
    # The scoped prd_id is the only transmitted input, appended to the argv.
    assert calls == [
        (pinned_operations().prd_read_content, ["anvil", "prd", "show", "--json", "release-beta"])
    ]
    assert result.provider == "anvil-state"
    assert result.schema_version == "workbench-prd-content/v1"
    assert result.content_digest == document["content_digest"]
    assert result.operation_id == PRD_READ_CONTENT_OPERATION_ID
    assert result.operation_digest == pinned_operations().prd_read_content.operation_digest
    # Criterion 2: exact source revision/digest, encoding-checked sizes, and
    # explicit bound/truncation metadata are typed fields on the result.
    assert result.prd_id == "release-beta"
    assert result.prd_revision == 5
    assert result.prd_status == "approved"
    assert result.content_format == "markdown"
    assert result.body_bytes == len(document["content"]["body"].encode("utf-8"))
    assert result.body_bytes <= 65536
    assert result.total_bytes == 18211
    assert result.truncated is True
    assert result.payload == document
    assert result.body == document["content"]["body"]


def test_matching_expected_revision_is_accepted() -> None:
    adapter, _calls = adapter_for(envelope(example_content()))

    result = adapter.fetch("release-beta", expected_revision=5)

    assert result.prd_revision == 5


def test_stale_expected_revision_fails_before_publication() -> None:
    adapter, calls = adapter_for(envelope(example_content()))

    with pytest.raises(PrdContentError, match="stale or unexpected"):
        adapter.fetch("release-beta", expected_revision=4)
    # The read itself ran (the catalog input schema takes prd_id only); the
    # freshness refusal happens after the response, before publication.
    assert len(calls) == 1


def test_response_for_a_different_prd_fails_closed() -> None:
    document = example_content()
    document["prd"]["prd_id"] = "release-alpha"
    adapter, _calls = adapter_for(envelope(rehash(document)))

    with pytest.raises(PrdContentError, match="out-of-scope content dump"):
        adapter.fetch("release-beta")


def test_oversized_body_fails_the_byte_bound() -> None:
    document = example_content()
    # 40000 two-byte code points: within the schema's 65536-code-point ceiling
    # but 80000 UTF-8 bytes, beyond the normative 64 KiB byte bound.
    body = "é" * 40000
    document["content"]["body"] = body
    document["content"]["truncated"] = False
    document["content"]["total_bytes"] = len(body.encode("utf-8"))
    adapter, _calls = adapter_for(envelope(rehash(document)))

    with pytest.raises(PrdContentError, match="64 KiB byte bound"):
        adapter.fetch("release-beta")


def test_oversized_code_point_body_fails_the_contract_schema() -> None:
    document = example_content()
    document["content"]["body"] = "x" * 65537
    document["content"]["truncated"] = False
    document["content"]["total_bytes"] = 65537
    adapter, _calls = adapter_for(envelope(rehash(document)))

    with pytest.raises(PrdContentError, match="contract"):
        adapter.fetch("release-beta")


def test_digest_drift_fails_before_publication() -> None:
    document = example_content()
    document["content"]["body"] += "\ninjected line"  # deliberate drift, digest NOT recomputed
    adapter, _calls = adapter_for(envelope(document))

    with pytest.raises(PrdContentError, match="digest mismatch"):
        adapter.fetch("release-beta")


def test_truncation_incoherence_fails_closed() -> None:
    untruncated = example_content()
    untruncated["content"]["truncated"] = False  # total_bytes still 18211 != body bytes
    adapter, _calls = adapter_for(envelope(rehash(untruncated)))
    with pytest.raises(PrdContentError, match="total_bytes equal to the body byte length"):
        adapter.fetch("release-beta")

    truncated = example_content()
    truncated["content"]["total_bytes"] = len(
        truncated["content"]["body"].encode("utf-8")
    )  # truncated=true but no bytes were actually left out
    adapter, _calls = adapter_for(envelope(rehash(truncated)))
    with pytest.raises(PrdContentError, match="greater than the body byte length"):
        adapter.fetch("release-beta")


def test_invalid_utf8_body_fails_before_digest_checks() -> None:
    document = example_content()
    # A lone surrogate survives json.loads but cannot encode to UTF-8.
    document["content"]["body"] = json.loads('"\\ud800"')
    adapter, _calls = adapter_for(envelope(document))

    with pytest.raises(PrdContentError, match="not valid UTF-8"):
        adapter.fetch("release-beta")


def test_invalid_prd_id_request_is_refused_before_any_cli_call() -> None:
    adapter, calls = adapter_for(envelope(example_content()))

    for bad in ("", "-leading-dash", "Upper-Case", "spaces here", "a" * 65, "..//etc", None, 7):
        with pytest.raises(PrdContentError, match="not a valid scoped PRD identifier"):
            adapter.fetch(bad)  # type: ignore[arg-type]
    assert calls == []


def test_invalid_expected_revision_is_refused_before_any_cli_call() -> None:
    adapter, calls = adapter_for(envelope(example_content()))

    for bad in (0, -1, "5", 1.0, True):
        with pytest.raises(PrdContentError, match="expected_revision"):
            adapter.fetch("release-beta", expected_revision=bad)  # type: ignore[arg-type]
    assert calls == []


def test_undeclared_fields_cannot_smuggle_extra_content() -> None:
    top_level = example_content()
    top_level["extra_prds"] = [{"prd_id": "release-alpha", "body": "# Another PRD"}]
    adapter, _calls = adapter_for(envelope(rehash(top_level)))
    with pytest.raises(PrdContentError, match="contract"):
        adapter.fetch("release-beta")

    content_level = example_content()
    content_level["content"]["raw_project_dump"] = "everything"
    adapter, _calls = adapter_for(envelope(rehash(content_level)))
    with pytest.raises(PrdContentError, match="contract"):
        adapter.fetch("release-beta")


def test_spoofed_provider_fails_closed() -> None:
    document = example_content()
    document["provider"] = "rogue-state"
    adapter, _calls = adapter_for(envelope(rehash(document)))

    with pytest.raises(PrdContentError, match="contract"):
        adapter.fetch("release-beta")


def test_adapter_uses_only_the_pinned_descriptor() -> None:
    adapter, calls = adapter_for(envelope(example_content()))

    adapter.fetch("release-beta")
    adapter.fetch("release-beta")

    for operation, _args in calls:
        assert operation.operation_id == PRD_READ_CONTENT_OPERATION_ID
        assert operation.bridge_adapter == "state.cli.prd_read_content"
        assert operation.operation_digest == pinned_operations().prd_read_content.operation_digest
    # A caller-supplied operation id is impossible by construction: no public
    # method takes an operation parameter anywhere on the adapter surface.
    fetch_parameters = inspect.signature(PrdContentAdapter.fetch).parameters
    assert list(fetch_parameters) == ["self", "prd_id", "expected_revision"]
    init_parameters = inspect.signature(PrdContentAdapter.__init__).parameters
    assert "operation_id" not in init_parameters and "operation" not in init_parameters
    # The reference validator itself refuses a non-content descriptor.
    with pytest.raises(PrdContentError, match="only executes"):
        validate_prd_content_payload(
            example_content(), pinned_operations().project_snapshot, "release-beta"
        )


def test_malformed_command_output_and_envelope_fail_closed() -> None:
    adapter, _calls = adapter_for("this is not a document")
    with pytest.raises(PrdContentError, match="one JSON object"):
        adapter.fetch("release-beta")

    refused, _calls = adapter_for(json.dumps({"ok": False, "command": "prd-content", "data": {}}))
    with pytest.raises(PrdContentError, match="did not report ok"):
        refused.fetch("release-beta")

    dataless, _calls = adapter_for(json.dumps({"ok": True, "command": "prd-content", "data": "text"}))
    with pytest.raises(PrdContentError, match="no data object"):
        dataless.fetch("release-beta")

    with pytest.raises(PrdContentError, match="not configured"):
        PrdContentAdapter(pinned_operations(), "   ")


def test_default_runner_executes_the_configured_cli_argv(monkeypatch, tmp_path: Path) -> None:
    observed: list[tuple[list[str], Path | None]] = []

    def fake_run(args, **kwargs):
        observed.append((list(args), kwargs["cwd"]))
        return subprocess.CompletedProcess(args, 0, envelope(example_content()), "")

    monkeypatch.setattr(adapter_module.subprocess, "run", fake_run)
    adapter = PrdContentAdapter(
        pinned_operations(), "custom-state prd show --json", cwd=tmp_path,
    )

    assert adapter.fetch("release-beta").content_digest == example_content()["content_digest"]
    assert observed == [(["custom-state", "prd", "show", "--json", "release-beta"], tmp_path)]

    def failing_run(args, **_kwargs):
        return subprocess.CompletedProcess(args, 3, "", "state exploded")

    monkeypatch.setattr(adapter_module.subprocess, "run", failing_run)
    with pytest.raises(PrdContentError, match="state exploded"):
        PrdContentAdapter(
            pinned_operations(), "custom-state prd show --json", cwd=tmp_path,
        ).fetch("release-beta")


def test_publishable_content_is_immutable() -> None:
    adapter, _calls = adapter_for(envelope(example_content()))
    result = adapter.fetch("release-beta")

    with pytest.raises((AttributeError, TypeError)):
        result.content_digest = "sha256:" + "0" * 64  # type: ignore[misc]
    # The payload is exposed as a parsed copy; mutating it cannot alter the result.
    mutated = result.payload
    mutated["content"]["body"] = ""
    assert result.payload["content"]["body"], "payload accessor must return an untouched copy"


def test_generated_at_cannot_smuggle_markdown_scale_content() -> None:
    document = example_content()
    document["generated_at"] = "# Full PRD\n\n" + ("Thousands of words of requirements. " * 3000)
    adapter, _calls = adapter_for(envelope(document))

    with pytest.raises(PrdContentError, match="contract"):
        adapter.fetch("release-beta")


def test_missing_contract_schema_file_fails_closed(monkeypatch, tmp_path: Path) -> None:
    adapter_module._reset_prd_content_contract_validator_cache()
    monkeypatch.setattr(
        adapter_module, "_PRD_CONTENT_CONTRACT_SCHEMA", tmp_path / "absent.schema.json"
    )
    adapter, _calls = adapter_for(envelope(example_content()))
    with pytest.raises(PrdContentError, match="schema is unavailable"):
        adapter.fetch("release-beta")
    adapter_module._reset_prd_content_contract_validator_cache()


def test_drifted_open_or_unbounded_schema_fails_closed(monkeypatch, tmp_path: Path) -> None:
    base = json.loads(
        (ROOT / "docs" / "contracts" / "schemas" / "prd-content.v1.schema.json").read_text(encoding="utf-8")
    )
    opened = copy.deepcopy(base)
    del opened["properties"]["content"]["additionalProperties"]
    drifted = tmp_path / "drifted.schema.json"
    drifted.write_text(json.dumps(opened), encoding="utf-8")
    adapter_module._reset_prd_content_contract_validator_cache()
    monkeypatch.setattr(adapter_module, "_PRD_CONTENT_CONTRACT_SCHEMA", drifted)
    adapter, _calls = adapter_for(envelope(example_content()))
    with pytest.raises(PrdContentError, match="no longer closes its objects"):
        adapter.fetch("release-beta")

    unbounded = copy.deepcopy(base)
    del unbounded["properties"]["content"]["properties"]["body"]["maxLength"]
    drifted.write_text(json.dumps(unbounded), encoding="utf-8")
    adapter_module._reset_prd_content_contract_validator_cache()
    with pytest.raises(PrdContentError, match="no longer bounds its prose"):
        adapter.fetch("release-beta")
    adapter_module._reset_prd_content_contract_validator_cache()


def test_oversize_validation_error_is_truncated() -> None:
    document = example_content()
    document["prd"]["title"] = "SECRETISH " * 2000
    adapter, _calls = adapter_for(envelope(rehash(document)))
    with pytest.raises(PrdContentError) as excinfo:
        adapter.fetch("release-beta")
    assert len(str(excinfo.value)) < 800
