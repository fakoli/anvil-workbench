"""Advanced-mode chat contract resources (advanced-model-playground T001).

These tests bind the four T001 acceptance criteria to concrete proofs over the
proposed ``advanced-branch``, ``advanced-trace``, ``advanced-preset``, and
``advanced-comparison`` contract resources:

1. A control cannot be submitted unless it is declared (type, bounds, default)
   by the pinned route capability, which itself carries the route/profile
   digest — ``test_criterion1_*``.
2. Trace/export schemas cannot carry credentials, raw headers, hidden
   reasoning, paths, or unredacted provider/tool payloads —
   ``test_criterion2_*``.
3. Presets pin exact route/tool digests and repair deterministically on drift —
   ``test_criterion3_*``.
4. An advanced branch references an existing conversation and turn lineage and
   the schema cannot mint a parallel transcript identity — ``test_criterion4_*``.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from workbench import contracts as contracts_module
from workbench.contracts import (
    ContractValidationError,
    contract_digest,
    validate_advanced_branch,
    validate_advanced_preset,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "docs" / "contracts" / "schemas"
EXAMPLES = ROOT / "docs" / "contracts" / "examples"

CONV_ID = re.compile(r"^conv_[a-zA-Z0-9_-]{8,128}$")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _validator(schema_name: str) -> Draft202012Validator:
    schema = _load(SCHEMAS / schema_name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _branch() -> dict:
    return _load(EXAMPLES / "advanced-branch.v1.json")


def _trace() -> dict:
    return _load(EXAMPLES / "advanced-trace.v1.json")


def _preset() -> dict:
    return _load(EXAMPLES / "advanced-preset.v1.json")


def _comparison() -> dict:
    return _load(EXAMPLES / "advanced-comparison.v1.json")


def _live_for(preset: dict) -> dict:
    """The undrifted live-digest view for a preset (every pinned digest matches)."""
    route = preset["route"]
    return {
        "route": {route["route_id"]: route["route_digest"]},
        "profile": {route["route_id"]: route["profile_digest"]},
        "tool": {tool["tool_id"]: tool["tool_digest"] for tool in preset["tools"]},
        "response_schema": {
            preset["response_format"]["schema_ref"]: preset["response_format"]["schema_digest"]
        },
    }


# --------------------------------------------------------------------------- #
# Criterion 1: a control cannot be submitted unless declared with type/bounds/
# default and a route/profile digest.
# --------------------------------------------------------------------------- #


def test_criterion1_route_capability_pins_route_and_profile_digests() -> None:
    branch = _branch()
    validate_advanced_branch(branch)
    capability = branch["route_capability"]
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", capability["route_digest"])
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", capability["profile_digest"])

    validator = _validator("advanced-branch.v1.schema.json")
    for required_digest in ("route_digest", "profile_digest"):
        without = copy.deepcopy(branch)
        del without["route_capability"][required_digest]
        with pytest.raises(ValidationError):
            validator.validate(without)  # no capability without its route/profile digest


def test_criterion1_every_declared_control_carries_type_bounds_and_default() -> None:
    validator = _validator("advanced-branch.v1.schema.json")
    # An int control with no bounds is not representable.
    unbounded = _branch()
    unbounded["route_capability"]["supported_controls"].append(
        {"name": "seed", "type": "int", "default": 7}
    )
    with pytest.raises(ValidationError):
        validator.validate(unbounded)
    # An enum control with no allowed_values is not representable.
    open_enum = _branch()
    open_enum["route_capability"]["supported_controls"].append(
        {"name": "truncation", "type": "enum", "default": "auto"}
    )
    with pytest.raises(ValidationError):
        validator.validate(open_enum)


def test_criterion1_undeclared_or_out_of_bounds_submitted_control_is_refused() -> None:
    # A crafted control the route does not declare (e.g. seed) is refused.
    crafted_seed = _branch()
    crafted_seed["submitted_controls"].append(
        {"name": "seed", "value": 42, "provenance": "declared"}
    )
    with pytest.raises(ContractValidationError, match="not declared by the route capability"):
        validate_advanced_branch(crafted_seed)

    # A declared control submitted outside its declared bounds is refused.
    out_of_bounds = _branch()
    out_of_bounds["submitted_controls"][0]["value"] = 9000  # temperature_milli max is 2000
    with pytest.raises(ContractValidationError, match="outside its declared bounds"):
        validate_advanced_branch(out_of_bounds)

    # A declared enum control submitted with a value outside its allowed set.
    bad_enum = _branch()
    bad_enum["submitted_controls"][1]["value"] = "extreme"
    with pytest.raises(ContractValidationError, match="allowed values"):
        validate_advanced_branch(bad_enum)


def test_criterion1_policy_owned_control_cannot_be_overridden() -> None:
    overridden = _branch()
    # response_streaming is policy_owned with default true; a crafted override.
    for submitted in overridden["submitted_controls"]:
        if submitted["name"] == "response_streaming":
            submitted["value"] = False
            submitted["provenance"] = "declared"
    with pytest.raises(ContractValidationError, match="read-only and cannot be overridden"):
        validate_advanced_branch(overridden)


# --------------------------------------------------------------------------- #
# Criterion 2: trace/export schemas prohibit credentials, raw headers, hidden
# reasoning, paths, and unredacted provider/tool payloads.
# --------------------------------------------------------------------------- #


def _declared_field_names(schema: object) -> set[str]:
    """Every field name the schema declares (object property + $defs keys).

    A closed contract can only carry a field it names, so scanning the declared
    field names — not the prose description, which legitimately spells out what
    it prohibits — is the sound structural check.
    """
    names: set[str] = set()
    if isinstance(schema, dict):
        block = schema.get("properties")
        if isinstance(block, dict):
            names.update(block.keys())
        for value in schema.values():
            names |= _declared_field_names(value)
    elif isinstance(schema, list):
        for value in schema:
            names |= _declared_field_names(value)
    return names


def test_criterion2_trace_schema_carries_no_forbidden_vocabulary() -> None:
    names = {name.lower() for name in _declared_field_names(_load(SCHEMAS / "advanced-trace.v1.schema.json"))}
    for forbidden in (
        "authorization",
        "api_key",
        "apikey",
        "headers",
        "raw_headers",
        "credential",
        "credentials",
        "password",
        "bearer",
        "token",
        "chain_of_thought",
        "reasoning",
        "hidden_reasoning",
        "unredacted",
        "audio_payload",
        "audio",
        "endpoint",
        "base_url",
        "url",
        "file_path",
        "path",
        "body",
        "output",
        "raw_output",
    ):
        assert forbidden not in names, forbidden


def test_criterion2_trace_example_is_declared_redacted_and_carries_no_raw_payload() -> None:
    trace = _trace()
    assert trace["request"]["redacted"] is True
    assert trace["redaction"]["status"] == "redacted"
    # Every tool_result names a digest and character count, never raw output.
    for event in trace["events"]:
        if event["kind"] == "tool_result":
            assert re.fullmatch(r"sha256:[a-f0-9]{64}", event["output_digest"])
            assert "output" not in event or event["output_digest"]
            assert event["tool_kind"] in {"mock", "read_only"}


def test_criterion2_trace_rejects_injected_credentials_headers_reasoning_and_paths() -> None:
    validator = _validator("advanced-trace.v1.schema.json")

    smuggled_root = _trace()
    smuggled_root["authorization"] = "Bearer abc"
    with pytest.raises(ValidationError):
        validator.validate(smuggled_root)

    smuggled_request = _trace()
    for leak_field, value in (
        ("raw_headers", {"Authorization": "Bearer abc"}),
        ("url", "https://serving.internal/v1/responses"),
        ("body", "raw prompt text"),
        ("file_path", "/home/op/prompt.txt"),
    ):
        leaked = copy.deepcopy(smuggled_request)
        leaked["request"][leak_field] = value
        with pytest.raises(ValidationError):
            validator.validate(leaked)

    hidden_reasoning = _trace()
    hidden_reasoning["events"][0]["reasoning"] = "internal chain of thought"
    with pytest.raises(ValidationError):
        validator.validate(hidden_reasoning)

    raw_tool_output = _trace()
    for event in raw_tool_output["events"]:
        if event["kind"] == "tool_result":
            event["output"] = "raw unredacted tool payload"
    with pytest.raises(ValidationError):
        validator.validate(raw_tool_output)

    endpoint_route = _trace()
    endpoint_route["route_decision"]["route_id"] = "https://serving.internal/v1"
    with pytest.raises(ValidationError):
        validator.validate(endpoint_route)


# --------------------------------------------------------------------------- #
# Criterion 3: presets reference exact route/tool digests and repair
# deterministically on drift.
# --------------------------------------------------------------------------- #


def test_criterion3_preset_pins_exact_route_and_tool_digests() -> None:
    preset = _preset()
    validate_advanced_preset(preset, _live_for(preset))
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", preset["route"]["route_digest"])
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", preset["route"]["profile_digest"])
    for tool in preset["tools"]:
        assert re.fullmatch(r"sha256:[a-f0-9]{64}", tool["tool_digest"])


def test_criterion3_preset_digest_is_deterministic_and_order_independent() -> None:
    preset = _preset()
    assert contract_digest("advanced-preset", preset) == preset["preset_digest"]

    reordered = copy.deepcopy(preset)
    reordered["tools"] = list(reversed(reordered["tools"]))
    reordered["control_values"] = list(reversed(reordered["control_values"]))
    # The volatile repair block is excluded from the digest.
    reordered["repair"] = {"status": "repair_required", "drifted_refs": [
        {"ref_kind": "tool", "id": "echo_fixture", "pinned_digest": preset["tools"][0]["tool_digest"]}
    ]}
    assert contract_digest("advanced-preset", reordered) == preset["preset_digest"]

    mutated = copy.deepcopy(preset)
    mutated["control_values"][0]["value"] = 301
    assert contract_digest("advanced-preset", mutated) != preset["preset_digest"]


def test_criterion3_preset_tamper_fails_closed() -> None:
    tampered = _preset()
    tampered["control_values"][0]["value"] = 301  # digest no longer recomputes
    with pytest.raises(ContractValidationError, match="digest mismatch"):
        validate_advanced_preset(tampered, _live_for(tampered))


def test_criterion3_drifted_preset_must_open_in_repair_mode() -> None:
    preset = _preset()
    live = _live_for(preset)
    # A tool digest drifts on disk/live: the same preset is now stale.
    live["tool"]["echo_fixture"] = "sha256:" + "9f" * 32

    # A preset still claiming ready under drift is refused (no silent substitution).
    with pytest.raises(ContractValidationError, match="must open in repair mode"):
        validate_advanced_preset(preset, live)

    # The deterministic repair state names exactly the drifted reference.
    repaired = copy.deepcopy(preset)
    repaired["repair"] = {
        "status": "repair_required",
        "drifted_refs": [
            {"ref_kind": "tool", "id": "echo_fixture", "pinned_digest": preset["tools"][0]["tool_digest"]}
        ],
    }
    validate_advanced_preset(repaired, live)

    # A repair block that lists the wrong drift set is refused.
    mislabelled = copy.deepcopy(repaired)
    mislabelled["repair"]["drifted_refs"][0]["ref_kind"] = "route"
    with pytest.raises(ContractValidationError, match="do not match the computed digest drift"):
        validate_advanced_preset(mislabelled, live)


def test_criterion3_route_digest_drift_is_detected() -> None:
    preset = _preset()
    live = _live_for(preset)
    live["route"]["route.chat-fast"] = "sha256:" + "ab" * 32  # route recipe changed
    with pytest.raises(ContractValidationError, match="must open in repair mode"):
        validate_advanced_preset(preset, live)


# --------------------------------------------------------------------------- #
# Criterion 4: an advanced branch references an existing conversation and turn
# lineage; the schema cannot mint a parallel transcript identity.
# --------------------------------------------------------------------------- #


def test_criterion4_branch_references_existing_conversation_and_lineage() -> None:
    branch = _branch()
    ref = branch["conversation_ref"]
    assert ref["binding"] == "existing_conversation"
    assert CONV_ID.fullmatch(ref["conversation_id"])
    assert ref["fork_point"]["parent_turn_id"].startswith("turn_")


def test_criterion4_branch_id_cannot_be_a_conversation_identity() -> None:
    branch = _branch()
    # The branch id is grammatically disjoint from the conv_ identity: a branch
    # can never be mistaken for (or minted as) a conversation.
    assert branch["branch_id"].startswith("advbranch_")
    assert not CONV_ID.fullmatch(branch["branch_id"])


def test_criterion4_schema_has_no_transcript_identity_field() -> None:
    schema = _load(SCHEMAS / "advanced-branch.v1.schema.json")
    top = schema["properties"]
    # No top-level conversation identity: the only conversation reference lives
    # inside conversation_ref and is required to name an EXISTING conversation.
    assert "conversation_id" not in top
    # No parallel transcript store: a branch owns no turns/messages/transcript.
    for forbidden in ("turns", "messages", "transcript", "history", "conversation"):
        assert forbidden not in top
    assert schema["required"].count("conversation_ref") == 1


def test_criterion4_branch_refuses_a_minted_conversation_or_embedded_transcript() -> None:
    validator = _validator("advanced-branch.v1.schema.json")

    minted = _branch()
    minted["conversation_id"] = "conv_forged_identity_0002"
    with pytest.raises(ValidationError):
        validator.validate(minted)  # root is closed; no second identity can ride in

    embedded = _branch()
    embedded["turns"] = [{"turn_id": "turn_forged_0001"}]
    with pytest.raises(ValidationError):
        validator.validate(embedded)

    rebound = _branch()
    rebound["conversation_ref"]["binding"] = "new_conversation"
    with pytest.raises(ValidationError):
        validator.validate(rebound)  # binding is a const: existing_conversation only


# --------------------------------------------------------------------------- #
# Supporting shape rules the work packet calls for.
# --------------------------------------------------------------------------- #


def test_branch_retention_marks_ephemeral_or_durable_explicitly() -> None:
    validator = _validator("advanced-branch.v1.schema.json")
    assert _branch()["retention"]["class"] == "durable"

    ephemeral = _branch()
    ephemeral["retention"] = {"class": "ephemeral", "expires_at": "2026-07-20T12:00:00Z"}
    validator.validate(ephemeral)

    # An ephemeral branch cannot also claim a durable save marker.
    contradictory = _branch()
    contradictory["retention"] = {
        "class": "ephemeral",
        "expires_at": "2026-07-20T12:00:00Z",
        "saved_at": "2026-07-20T10:05:00Z",
    }
    with pytest.raises(ValidationError):
        validator.validate(contradictory)

    # A durable branch must not carry an ephemeral expiry.
    durable_expiry = _branch()
    durable_expiry["retention"] = {
        "class": "durable",
        "saved_at": "2026-07-20T10:05:00Z",
        "expires_at": "2026-07-20T12:00:00Z",
    }
    with pytest.raises(ValidationError):
        validator.validate(durable_expiry)


def test_branch_tool_profile_admits_only_mock_and_read_only_tools() -> None:
    validator = _validator("advanced-branch.v1.schema.json")
    assert {tool["kind"] for tool in _branch()["tool_profile"]["tools"]} <= {"mock", "read_only"}
    for effectful in ("effectful", "plugin", "shell", "http"):
        crafted = _branch()
        crafted["tool_profile"]["tools"][0]["kind"] = effectful
        with pytest.raises(ValidationError):
            validator.validate(crafted)


def test_structured_output_json_schema_mode_requires_a_pinned_schema_digest() -> None:
    validator = _validator("advanced-branch.v1.schema.json")
    unpinned = _branch()
    unpinned["structured_output"] = {"mode": "json_schema", "schema_ref": "strict_json"}
    with pytest.raises(ValidationError):
        validator.validate(unpinned)
    text_mode = _branch()
    text_mode["structured_output"] = {"mode": "text"}
    validator.validate(text_mode)


def test_comparison_cannot_show_a_winner_without_a_declared_criterion() -> None:
    validator = _validator("advanced-comparison.v1.schema.json")
    comparison = _comparison()
    assert comparison["criterion"]["non_qualification"] is True
    assert 2 <= len(comparison["attempts"]) <= 4

    # A ranking (a winner) requires the declared criterion.
    without_criterion = copy.deepcopy(comparison)
    del without_criterion["criterion"]
    with pytest.raises(ValidationError):
        validator.validate(without_criterion)

    # No free-form winner field is representable at all.
    invented = copy.deepcopy(comparison)
    invented["winner"] = "turn_assistant_0003"
    with pytest.raises(ValidationError):
        validator.validate(invented)


def test_comparison_reuses_one_conversation_and_sibling_turn_lineage() -> None:
    comparison = _comparison()
    assert CONV_ID.fullmatch(comparison["conversation_id"])
    assert comparison["fork_point"]["parent_turn_id"].startswith("turn_")
    schema = _load(SCHEMAS / "advanced-comparison.v1.schema.json")
    # A comparison groups existing sibling turns; it has no transcript store.
    assert "turns" not in schema["properties"]
    assert "transcript" not in schema["properties"]


# --------------------------------------------------------------------------- #
# Digest-kind and validator-loader trust-root guards, mirroring siblings.
# --------------------------------------------------------------------------- #


def test_advanced_preset_digest_kind_is_registered() -> None:
    assert "advanced-preset" in contracts_module._PREFIXES
    assert contracts_module._PREFIXES["advanced-preset"] == b"anvil-workbench/advanced-preset/v1\0"


def test_advanced_branch_validator_trust_root_fails_closed(monkeypatch, tmp_path) -> None:
    contracts_module._reset_advanced_branch_contract_validator_cache()
    monkeypatch.setattr(
        contracts_module, "_ADVANCED_BRANCH_CONTRACT_SCHEMA_PATH", tmp_path / "absent.schema.json"
    )
    with pytest.raises(ContractValidationError, match="schema is unavailable"):
        contracts_module.advanced_branch_contract_validator()

    base = _load(SCHEMAS / "advanced-branch.v1.schema.json")
    del base["$defs"]["controlDescriptor"]["additionalProperties"]
    drifted = tmp_path / "drifted-branch.schema.json"
    drifted.write_text(json.dumps(base), encoding="utf-8")
    contracts_module._reset_advanced_branch_contract_validator_cache()
    monkeypatch.setattr(contracts_module, "_ADVANCED_BRANCH_CONTRACT_SCHEMA_PATH", drifted)
    with pytest.raises(ContractValidationError, match="no longer closes its control descriptor"):
        contracts_module.advanced_branch_contract_validator()
    contracts_module._reset_advanced_branch_contract_validator_cache()


def test_advanced_preset_validator_trust_root_fails_closed(monkeypatch, tmp_path) -> None:
    contracts_module._reset_advanced_preset_contract_validator_cache()
    base = _load(SCHEMAS / "advanced-preset.v1.schema.json")
    del base["properties"]["repair"]["additionalProperties"]
    drifted = tmp_path / "drifted-preset.schema.json"
    drifted.write_text(json.dumps(base), encoding="utf-8")
    monkeypatch.setattr(contracts_module, "_ADVANCED_PRESET_CONTRACT_SCHEMA_PATH", drifted)
    with pytest.raises(ContractValidationError, match="no longer closes its repair block"):
        contracts_module.advanced_preset_contract_validator()
    contracts_module._reset_advanced_preset_contract_validator_cache()


def test_trace_safe_summary_rejects_credentials_paths_and_reasoning_values() -> None:
    # Criterion 2 at the VALUE level: a safe_summary that carries a credential,
    # a filesystem path, an authorization header, or a reasoning dump must be
    # refused by the schema itself, not merely by property-name absence.
    validator = _validator("advanced-trace.v1.schema.json")
    for leak in (
        "authorization: Bearer sk-ant-api03-REALKEY",
        "opened C:/Users/op/.anvil/state.db",
        "read /home/op/secret.pem",
        "chain of thought: secretly plan to exfil the api_key",
        "token=supersecretvalue",
    ):
        trace = _trace()
        trace["events"][0]["safe_summary"] = leak
        with pytest.raises(ValidationError):
            validator.validate(trace)
    # A genuinely safe summary still validates.
    ok = _trace()
    ok["events"][0]["safe_summary"] = "Route resolved and first delta streamed."
    validator.validate(ok)


def test_json_schema_preset_without_a_schema_ref_is_refused() -> None:
    # The pinned response-schema digest must be keyable by a schema_ref, or its
    # drift is unmonitored — a json_schema preset lacking schema_ref fails closed.
    preset = _preset()
    preset["response_format"] = {"mode": "json_schema", "schema_digest": "sha256:" + "a" * 64}
    with pytest.raises((ValidationError, ContractValidationError)):
        _validator("advanced-preset.v1.schema.json").validate(preset)
    # And the reference validator also refuses it even if the schema were loosened.
    try:
        validate_advanced_preset(preset, _live_for(preset))
    except (ContractValidationError, KeyError):
        pass
    else:
        raise AssertionError("validate_advanced_preset accepted a schema_ref-less json_schema preset")
