"""Prove the settings-descriptor contract enforces T001's three criteria.

The catalog is a *proposed* operation-layer resource: no live endpoint reads it
yet. These tests pin the authority rules a later Settings implementation must
inherit — one owning scope with deterministic precedence, a hard secret/path
serialization boundary, and typed id/digest references for every capability
default — so the shape cannot quietly drift into granting a privilege.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from workbench.contracts import (
    ContractValidationError,
    contract_digest,
    settings_actor_view,
    validate_settings_descriptor,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "docs" / "contracts" / "schemas" / "settings-descriptor.v1.schema.json"
EXAMPLE_PATH = ROOT / "docs" / "contracts" / "examples" / "settings-descriptor.v1.json"

#: Markers that must never appear in an actor/project serialization or export.
_FORBIDDEN_ACTOR_MARKERS = (
    "secret", "token", "api_key", "password", "authorization",
    "state.db", "endpoint", "://", "c:\\", "c:/", "/home/", "/users/",
)


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _schema_validator() -> Draft202012Validator:
    schema = _load(SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _rehash(catalog: dict[str, object]) -> dict[str, object]:
    catalog["catalog_digest"] = contract_digest("settings-descriptor", catalog)
    return catalog


def test_example_catalog_validates_and_its_digest_is_stable_order_independent_content_sensitive() -> None:
    catalog = _load(EXAMPLE_PATH)
    _schema_validator().validate(catalog)
    validate_settings_descriptor(catalog)

    advertised = catalog["catalog_digest"]
    assert contract_digest("settings-descriptor", catalog) == advertised

    reordered = copy.deepcopy(catalog)
    reordered["settings"] = list(reversed(reordered["settings"]))
    assert contract_digest("settings-descriptor", reordered) == advertised

    changed = copy.deepcopy(catalog)
    changed["settings"][0]["title"] += "!"
    assert contract_digest("settings-descriptor", changed) != advertised


# --- Criterion 1: exactly one owning scope and deterministic precedence -------

def test_criterion1_every_setting_owns_exactly_one_scope_within_a_total_precedence_order() -> None:
    catalog = _load(EXAMPLE_PATH)
    scopes = set(catalog["scope_precedence"])
    assert scopes == {"personal", "project", "deployment", "policy"}
    assert len(catalog["scope_precedence"]) == 4, "precedence must be a total order, no duplicates"

    for setting in catalog["settings"]:
        # `scope` is a single enum value, so exactly one scope owns each setting.
        assert isinstance(setting["scope"], str)
        assert setting["scope"] in scopes


def test_criterion1_precedence_must_cover_every_scope_exactly_once() -> None:
    catalog = _load(EXAMPLE_PATH)

    missing = copy.deepcopy(catalog)
    missing["scope_precedence"] = ["policy", "deployment", "project"]
    with pytest.raises(ValidationError):
        _schema_validator().validate(missing)  # schema: minItems 4

    duplicated = copy.deepcopy(catalog)
    duplicated["scope_precedence"] = ["policy", "policy", "project", "personal"]
    with pytest.raises(ValidationError):
        _schema_validator().validate(duplicated)  # schema: uniqueItems


def test_criterion1_policy_ceiling_must_be_owned_by_a_strictly_higher_scope() -> None:
    # A personal value can never exceed a route policy / capability profile /
    # retention ceiling (PRD non-goal), so a ceiling must outrank its setting.
    catalog = _load(EXAMPLE_PATH)
    inverted = copy.deepcopy(catalog)
    # Point the operator ceiling *down* at a personal setting: rank must reject it.
    ceiling = next(s for s in inverted["settings"] if s["id"] == "policy.transcript_retention_max_days")
    ceiling["policy_ceiling"] = {"ceiling_setting": "personal.chat_transcript_retention_days"}
    with pytest.raises(ContractValidationError, match="strictly higher-authority scope"):
        validate_settings_descriptor(_rehash(inverted))

    dangling = copy.deepcopy(catalog)
    personal = next(s for s in dangling["settings"] if s["id"] == "personal.default_chat_route")
    personal["policy_ceiling"] = {"ceiling_setting": "policy.does_not_exist"}
    with pytest.raises(ContractValidationError, match="policy_ceiling names an unknown setting"):
        validate_settings_descriptor(_rehash(dangling))


def test_digest_recompute_fails_closed_on_tamper() -> None:
    catalog = _load(EXAMPLE_PATH)
    drifted = copy.deepcopy(catalog)
    drifted["settings"][0]["default"] = "delivery"
    with pytest.raises(ContractValidationError, match="digest mismatch"):
        validate_settings_descriptor(drifted)


def test_duplicate_setting_ids_are_refused() -> None:
    catalog = _load(EXAMPLE_PATH)
    dup = copy.deepcopy(catalog)
    dup["settings"].append(copy.deepcopy(dup["settings"][0]))
    with pytest.raises(ContractValidationError, match="duplicate setting id"):
        validate_settings_descriptor(_rehash(dup))


# --- Criterion 2: secret / path-like fields cannot be serialized --------------

def test_criterion2_actor_view_drops_authority_secret_and_path_like_descriptors() -> None:
    catalog = _load(EXAMPLE_PATH)
    view = settings_actor_view(catalog)

    kept = {setting["id"] for setting in view["settings"]}
    # Only personal/project scopes survive; nothing deployment/policy owned.
    assert kept and all(setting_id.startswith(("personal.", "project.")) for setting_id in kept)
    assert "deployment.state_read_location" not in kept
    assert "deployment.identity_header_name" not in kept

    serialized = json.dumps(view).lower()
    for marker in _FORBIDDEN_ACTOR_MARKERS:
        assert marker not in serialized, marker


def test_criterion2_secret_value_and_default_never_reach_the_actor_view() -> None:
    # Defence-in-depth: even a malformed catalog that smuggles a secret value
    # into a personal-scoped descriptor is stripped by the projection, not just
    # by the schema. The forbidden token must not survive serialization.
    catalog = _load(EXAMPLE_PATH)
    rogue = copy.deepcopy(catalog)
    rogue["settings"].append({
        "id": "personal.smuggled_token",
        "title": "Rogue",
        "type": "string",
        "scope": "personal",
        "sensitivity": "secret",
        "mutability": "mutable",
        "application_timing": "immediate",
        "default": "token-abc123-should-never-serialize",
    })
    view = settings_actor_view(rogue)
    assert "personal.smuggled_token" not in {s["id"] for s in view["settings"]}
    assert "token-abc123-should-never-serialize" not in json.dumps(view)


def test_criterion2_schema_refuses_a_secret_or_path_like_field_at_actor_scope() -> None:
    validator = _schema_validator()
    catalog = _load(EXAMPLE_PATH)

    secret_personal = copy.deepcopy(catalog)
    secret_personal["settings"].append({
        "id": "personal.leaky_secret",
        "title": "Leaky",
        "type": "string",
        "scope": "personal",
        "sensitivity": "secret",
        "mutability": "env_only",
        "application_timing": "immediate",
    })
    with pytest.raises(ValidationError):
        validator.validate(secret_personal)  # secret must be deployment/policy owned

    path_at_project = copy.deepcopy(catalog)
    path_at_project["settings"].append({
        "id": "project.leaky_path",
        "title": "Leaky path",
        "type": "string",
        "scope": "project",
        "sensitivity": "redacted",
        "path_like": True,
        "mutability": "env_only",
        "application_timing": "immediate",
    })
    with pytest.raises(ValidationError):
        validator.validate(path_at_project)  # path-like must be authority owned

    secret_with_default = copy.deepcopy(catalog)
    secret_with_default["settings"].append({
        "id": "deployment.leaky_default",
        "title": "Leaky default",
        "type": "string",
        "scope": "deployment",
        "sensitivity": "secret",
        "mutability": "env_only",
        "application_timing": "immediate",
        "default": "opaque",
    })
    with pytest.raises(ValidationError):
        validator.validate(secret_with_default)  # a secret carries no serializable default


# --- Criterion 3: capability defaults reference typed ids/digests -------------

def test_criterion3_reference_defaults_are_typed_ids_or_digests_for_every_kind() -> None:
    catalog = _load(EXAMPLE_PATH)
    by_kind = {
        setting["ref_kind"]: setting
        for setting in catalog["settings"]
        if "ref_kind" in setting
    }
    assert set(by_kind) == {"route", "worktree", "workflow", "skill", "plugin", "capability"}
    for setting in by_kind.values():
        assert setting["type"] in ("id_ref", "digest_ref")
        assert isinstance(setting["default"], str)


def test_criterion3_a_capability_default_declared_as_free_text_string_is_refused() -> None:
    validator = _schema_validator()
    catalog = _load(EXAMPLE_PATH)

    free_text_route = copy.deepcopy(catalog)
    free_text_route["settings"].append({
        "id": "project.free_text_route",
        "title": "Free text route",
        "type": "string",
        "scope": "project",
        "sensitivity": "public",
        "mutability": "mutable",
        "application_timing": "next_run",
        "ref_kind": "route",
        "default": "just some free text",
    })
    with pytest.raises(ValidationError):
        validator.validate(free_text_route)  # a ref_kind default may not be a free-text string

    unpatterned_id = copy.deepcopy(catalog)
    target = next(s for s in unpatterned_id["settings"] if s["id"] == "project.preferred_worktree")
    target["default"] = "Not A Valid Id With Spaces"
    with pytest.raises(ValidationError):
        validator.validate(unpatterned_id)  # id_ref default must match the id pattern

    digest_free_text = copy.deepcopy(catalog)
    profile = next(s for s in digest_free_text["settings"] if s["id"] == "project.default_capability_profile")
    profile["default"] = "latest"
    with pytest.raises(ValidationError):
        validator.validate(digest_free_text)  # digest_ref default must be a sha256 digest


def test_criterion3_validator_rejects_a_reference_kind_on_a_non_reference_type() -> None:
    catalog = _load(EXAMPLE_PATH)
    mistyped = copy.deepcopy(catalog)
    mistyped["settings"].append({
        "id": "project.mistyped_reference",
        "title": "Mistyped",
        "type": "enum",
        "scope": "project",
        "sensitivity": "public",
        "mutability": "mutable",
        "application_timing": "next_run",
        "allowed_values": ["a", "b"],
        "ref_kind": "skill",
        "default": "a",
    })
    # Schema rejects it (ref_kind => id_ref/digest_ref); the semantic validator
    # would too, but schema is the first gate.
    with pytest.raises(ValidationError):
        _schema_validator().validate(mistyped)


# --- Contract-idiom guards (mirror the profile/workflow siblings) -------------

def test_missing_reference_kind_completeness_fails_closed() -> None:
    catalog = _load(EXAMPLE_PATH)
    stripped = copy.deepcopy(catalog)
    stripped["settings"] = [s for s in stripped["settings"] if s.get("ref_kind") != "plugin"]
    with pytest.raises(ContractValidationError, match="omits reference-kind defaults"):
        validate_settings_descriptor(_rehash(stripped))


def test_settings_descriptor_contract_schema_trust_root_fails_closed(monkeypatch, tmp_path) -> None:
    from workbench import contracts as contracts_module

    contracts_module._reset_settings_descriptor_contract_validator_cache()
    monkeypatch.setattr(
        contracts_module, "_SETTINGS_DESCRIPTOR_CONTRACT_SCHEMA_PATH", tmp_path / "absent.schema.json"
    )
    with pytest.raises(ContractValidationError, match="schema is unavailable"):
        contracts_module.settings_descriptor_contract_validator()

    base = _load(SCHEMA_PATH)
    del base["$defs"]["descriptor"]["additionalProperties"]
    drifted = tmp_path / "drifted.schema.json"
    drifted.write_text(json.dumps(base), encoding="utf-8")
    contracts_module._reset_settings_descriptor_contract_validator_cache()
    monkeypatch.setattr(contracts_module, "_SETTINGS_DESCRIPTOR_CONTRACT_SCHEMA_PATH", drifted)
    with pytest.raises(ContractValidationError, match="no longer closes its descriptor object"):
        contracts_module.settings_descriptor_contract_validator()
    contracts_module._reset_settings_descriptor_contract_validator_cache()
