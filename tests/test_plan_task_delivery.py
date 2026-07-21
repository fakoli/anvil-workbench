"""Task reference, hierarchy, eligibility, and Deliver intent contracts (plan-task-delivery T001).

These tests bind the three T001 acceptance criteria to concrete proofs over the
proposed ``task-reference``, ``delivery-eligibility``, ``deliver-intent``, and
``deliver-start-receipt`` contract resources:

1. A task reference cannot validate without its owning PRD and its source
   digest/revision, and a ``T001`` from two PRDs never collapses into one row or
   run — ``test_criterion1_*`` (R004).
2. A Deliver intent carries ids and approved selections only; it cannot carry a
   path, command, token, or executable workflow — ``test_criterion2_*``.
3. Blocked and stale states carry stable codes plus human-safe explanations —
   ``test_criterion3_*``.

The resources are *proposed* operation-layer resources: no live endpoint reads
them yet. These tests pin the authority rules a later Deliver implementation
must inherit so the shape cannot quietly drift into granting a privilege.
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
    validate_delivery_eligibility,
    validate_deliver_intent,
    validate_deliver_start_receipt,
    validate_task_reference,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "docs" / "contracts" / "schemas"
EXAMPLES = ROOT / "docs" / "contracts" / "examples"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _validator(schema_name: str) -> Draft202012Validator:
    schema = _load(SCHEMAS / schema_name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _reference() -> dict:
    return _load(EXAMPLES / "task-reference.v1.json")


def _eligibility() -> dict:
    return _load(EXAMPLES / "delivery-eligibility.v1.json")


def _intent() -> dict:
    return _load(EXAMPLES / "deliver-intent.v1.json")


def _start_receipt() -> dict:
    return _load(EXAMPLES / "deliver-start-receipt.v1.json")


def _refusal_receipt() -> dict:
    return _load(EXAMPLES / "deliver-start-receipt.refusal.v1.json")


# --------------------------------------------------------------------------- #
# Criterion 1: a task reference cannot validate without its owning PRD and its
# source digest/revision; scoped ids stay unambiguous across PRDs (R004).
# --------------------------------------------------------------------------- #


def test_criterion1_reference_requires_owning_prd_revision_and_source_digest() -> None:
    reference = _reference()
    validate_task_reference(reference)
    assert reference["ref"]["prd_id"] and reference["ref"]["prd_revision"] >= 1
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", reference["source"]["snapshot_digest"])

    validator = _validator("task-reference.v1.schema.json")
    for missing in ("prd_id", "prd_revision"):
        without = copy.deepcopy(reference)
        del without["ref"][missing]
        with pytest.raises(ValidationError):
            validator.validate(without)  # a reference is not representable without its owning PRD/revision

    no_source = copy.deepcopy(reference)
    del no_source["source"]
    with pytest.raises(ValidationError):
        validator.validate(no_source)  # nor without its pinned source
    no_snapshot = copy.deepcopy(reference)
    del no_snapshot["source"]["snapshot_digest"]
    with pytest.raises(ValidationError):
        validator.validate(no_snapshot)  # nor without the source digest


def test_criterion1_bare_task_id_string_is_not_a_reference() -> None:
    validator = _validator("task-reference.v1.schema.json")
    bare = _reference()
    bare["ref"] = "T001"
    with pytest.raises(ValidationError):
        validator.validate(bare)  # a bare task id string is never a reference


def test_criterion1_two_prds_t001_do_not_collapse_into_one_scoped_id() -> None:
    alpha = _reference()
    beta = copy.deepcopy(alpha)
    beta["ref"]["prd_id"] = "release-beta"
    beta["scoped_id"] = "release-beta:T001"
    beta["run_label"] = "release-beta:T001@r4"
    beta["hierarchy"]["prd_id"] = "release-beta"
    validate_task_reference(alpha)
    validate_task_reference(beta)
    # Same task id, different owning PRD -> two distinct scoped ids and run labels.
    assert alpha["scoped_id"] != beta["scoped_id"]
    assert alpha["run_label"] != beta["run_label"]


def test_criterion1_scoped_id_and_run_label_must_derive_from_the_reference() -> None:
    mismatched = _reference()
    mismatched["scoped_id"] = "release-beta:T001"
    with pytest.raises(ContractValidationError, match="scoped_id does not match"):
        validate_task_reference(mismatched)

    drifted_label = _reference()
    drifted_label["run_label"] = "release-alpha:T001@r9"
    with pytest.raises(ContractValidationError, match="immutable"):
        validate_task_reference(drifted_label)  # the label is derived from the pinned revision

    wrong_owner = _reference()
    wrong_owner["hierarchy"]["prd_id"] = "release-beta"
    with pytest.raises(ContractValidationError, match="different owning PRD"):
        validate_task_reference(wrong_owner)


# --------------------------------------------------------------------------- #
# Criterion 2: a Deliver intent carries ids/approved selections only; no path,
# command, token, or executable workflow is representable.
# --------------------------------------------------------------------------- #


def test_criterion2_intent_selections_are_ids_and_digests_only() -> None:
    intent = _intent()
    validate_deliver_intent(intent)
    selections = intent["selections"]
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", selections["capability_profile_digest"])
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", selections["workflow"]["digest"])
    # The workflow is selected by id/revision/digest, never an inline body.
    assert set(selections["workflow"]) == {"id", "revision", "digest"}


def test_criterion2_intent_rejects_path_command_token_or_executable_workflow() -> None:
    validator = _validator("deliver-intent.v1.schema.json")

    # A path anywhere in the closed record is unrepresentable.
    with_path = _intent()
    with_path["selections"]["worktree_path"] = "C:/projects/anvil/worktree"
    with pytest.raises(ValidationError):
        validator.validate(with_path)

    # A raw shell command is unrepresentable.
    with_command = _intent()
    with_command["selections"]["command"] = "git push --force"
    with pytest.raises(ValidationError):
        validator.validate(with_command)

    # A credential/token is unrepresentable.
    with_token = _intent()
    with_token["selections"]["github_token"] = "ghp_example"
    with pytest.raises(ValidationError):
        validator.validate(with_token)

    # An inline, executable workflow body is unrepresentable: the workflow is a
    # reference, so a steps array cannot ride in.
    with_steps = _intent()
    with_steps["selections"]["workflow"]["steps"] = [{"id": "commit", "kind": "operation"}]
    with pytest.raises(ValidationError):
        validator.validate(with_steps)

    # And a top-level smuggled command field is refused by the closed root.
    with_top_command = _intent()
    with_top_command["raw_command"] = "rm -rf ."
    with pytest.raises(ValidationError):
        validator.validate(with_top_command)


def test_criterion2_intent_digest_is_the_idempotency_key_and_tamper_evident() -> None:
    intent = _intent()
    assert contract_digest("deliver-intent", intent) == intent["intent_digest"]

    # Reordering the selection lists does not change the idempotency key.
    reordered = copy.deepcopy(intent)
    reordered["selections"]["skills"] = list(reversed(reordered["selections"]["skills"]))
    reordered["selections"]["catalogs"] = list(reversed(reordered["selections"]["catalogs"]))
    assert contract_digest("deliver-intent", reordered) == intent["intent_digest"]

    # A changed selection changes the key (a different run, not the same one).
    mutated = copy.deepcopy(intent)
    mutated["selections"]["workflow"]["revision"] = "2"
    assert contract_digest("deliver-intent", mutated) != intent["intent_digest"]

    tampered = _intent()
    tampered["selections"]["workflow"]["revision"] = "2"  # digest no longer recomputes
    with pytest.raises(ContractValidationError, match="digest mismatch"):
        validate_deliver_intent(tampered)


def test_criterion2_intent_binds_one_unambiguous_task() -> None:
    intent = _intent()
    assert intent["task_ref"]["scoped_id"] == "release-alpha:T001"

    validator = _validator("deliver-intent.v1.schema.json")
    no_source = copy.deepcopy(intent)
    del no_source["task_ref"]["snapshot_digest"]
    with pytest.raises(ValidationError):
        validator.validate(no_source)  # the intent binds the pinned source snapshot

    mismatched = _intent()
    mismatched["task_ref"]["scoped_id"] = "release-beta:T001"
    mismatched["intent_digest"] = contract_digest("deliver-intent", mismatched)
    with pytest.raises(ContractValidationError, match="scoped_id does not match"):
        validate_deliver_intent(mismatched)


# --------------------------------------------------------------------------- #
# Criterion 3: blocked and stale states carry stable codes plus human-safe
# explanations.
# --------------------------------------------------------------------------- #


def test_criterion3_blocked_and_stale_reasons_carry_stable_codes_and_explanations() -> None:
    verdict = _eligibility()
    validate_delivery_eligibility(verdict)
    by_class = {reason["class"]: reason for reason in verdict["reasons"]}
    assert {"blocked", "stale"} <= set(by_class)
    for reason in verdict["reasons"]:
        assert re.fullmatch(r"(blocked|stale|info)\.[a-z0-9_.]+", reason["code"])
        assert reason["explanation"].strip()  # a human-safe explanation is present


def test_criterion3_explanation_cannot_carry_a_path_endpoint_or_credential() -> None:
    validator = _validator("delivery-eligibility.v1.schema.json")
    for leak in (
        "read C:/Users/op/.anvil/state.db",
        "blocked at /home/op/secret.pem",
        "call https://serving.internal/v1/responses",
        "authorization: Bearer sk-ant-api03-REALKEY",
        "token=supersecretvalue",
    ):
        verdict = _eligibility()
        verdict["reasons"][0]["explanation"] = leak
        with pytest.raises(ValidationError):
            validator.validate(verdict)
    # A genuinely safe explanation still validates.
    ok = _eligibility()
    ok["reasons"][0]["explanation"] = "A dependency task has not merged yet."
    validator.validate(ok)


def test_criterion3_eligibility_state_is_derived_not_free() -> None:
    # blocked dominates stale.
    wrong_state = _eligibility()
    wrong_state["state"] = "stale"
    with pytest.raises(ContractValidationError, match="must be the derived 'blocked'"):
        validate_delivery_eligibility(wrong_state)

    # An eligible flag cannot contradict a blocked/stale verdict.
    lying = _eligibility()
    lying["eligible"] = True
    with pytest.raises(ContractValidationError, match="disagrees with the derived state"):
        validate_delivery_eligibility(lying)

    # A stale-only verdict derives the stale state.
    stale_only = _eligibility()
    stale_only["reasons"] = [reason for reason in stale_only["reasons"] if reason["class"] == "stale"]
    stale_only["state"] = "stale"
    validate_delivery_eligibility(stale_only)

    # An eligible verdict has no blocked/stale reasons.
    eligible = {
        "schema_version": "workbench-delivery-eligibility/v1",
        "ref": {"prd_id": "release-alpha", "task_id": "T001", "prd_revision": 4},
        "scoped_id": "release-alpha:T001",
        "eligible": True,
        "state": "eligible",
        "reasons": [
            {"class": "info", "code": "info.ready", "content_trust": "untrusted_task_data",
             "explanation": "All dependencies are merged and the source is current."}
        ],
    }
    validate_delivery_eligibility(eligible)


def test_criterion3_a_stale_code_cannot_be_filed_under_a_blocked_reason() -> None:
    misfiled = _eligibility()
    misfiled["reasons"][0]["code"] = "stale.snapshot_superseded"  # class is still "blocked"
    with pytest.raises(ContractValidationError, match="code does not match its class"):
        validate_delivery_eligibility(misfiled)


def test_unknown_reason_code_is_refused_by_the_schema() -> None:
    validator = _validator("delivery-eligibility.v1.schema.json")
    invented = _eligibility()
    invented["reasons"][0]["code"] = "blocked.made_up_code"
    with pytest.raises(ValidationError):
        validator.validate(invented)


# --------------------------------------------------------------------------- #
# Start receipt: the idempotent acknowledgment binds to its intent.
# --------------------------------------------------------------------------- #


def test_start_receipt_echoes_the_intent_idempotency_key() -> None:
    intent = _intent()
    receipt = _start_receipt()
    assert receipt["status"] == "accepted"
    validate_deliver_start_receipt(receipt, intent)
    assert receipt["intent_digest"] == intent["intent_digest"]
    assert receipt["run"]["run_id"].startswith("run_")

    wrong = _start_receipt()
    wrong["intent_digest"] = "sha256:" + "0" * 64
    with pytest.raises(ContractValidationError, match="echo the intent idempotency key"):
        validate_deliver_start_receipt(wrong, intent)


def test_accepted_start_receipt_must_carry_a_bounded_run_not_an_error() -> None:
    validator = _validator("deliver-start-receipt.v1.schema.json")
    contradictory = _start_receipt()
    contradictory["error"] = {"code": "x.y", "safe_summary": "no", "retryable": False}
    with pytest.raises(ValidationError):
        validator.validate(contradictory)  # an accepted start cannot also be an error

    no_run = _start_receipt()
    del no_run["run"]
    with pytest.raises(ValidationError):
        validator.validate(no_run)  # an accepted start must carry its run block


def test_denied_start_receipt_is_a_typed_refusal_with_a_stable_code() -> None:
    receipt = _refusal_receipt()
    validate_deliver_start_receipt(receipt)
    assert receipt["status"] == "denied"
    assert receipt["error"]["retryable"] is False
    assert re.fullmatch(r"[a-z][a-z0-9_.]*", receipt["error"]["code"])
    assert receipt["redaction"]["status"] == "metadata_only"

    validator = _validator("deliver-start-receipt.v1.schema.json")
    with_run = _refusal_receipt()
    with_run["run"] = _start_receipt()["run"]
    with pytest.raises(ValidationError):
        validator.validate(with_run)  # a denied start carries no run


# --------------------------------------------------------------------------- #
# Digest-kind and validator-loader trust-root guards, mirroring siblings.
# --------------------------------------------------------------------------- #


def test_deliver_intent_digest_kind_is_registered() -> None:
    assert "deliver-intent" in contracts_module._PREFIXES
    assert contracts_module._PREFIXES["deliver-intent"] == b"anvil-workbench/deliver-intent/v1\0"


def test_deliver_intent_digest_domain_rejects_floats() -> None:
    with pytest.raises(ContractValidationError, match="floating-point"):
        contract_digest("deliver-intent", {"selections": {"weight": 0.5}})


def test_task_reference_validator_trust_root_fails_closed(monkeypatch, tmp_path) -> None:
    contracts_module._reset_task_reference_contract_validator_cache()
    monkeypatch.setattr(
        contracts_module, "_TASK_REFERENCE_CONTRACT_SCHEMA_PATH", tmp_path / "absent.schema.json"
    )
    with pytest.raises(ContractValidationError, match="schema is unavailable"):
        contracts_module.task_reference_contract_validator()

    base = _load(SCHEMAS / "task-reference.v1.schema.json")
    del base["$defs"]["source"]["additionalProperties"]
    drifted = tmp_path / "drifted-reference.schema.json"
    drifted.write_text(json.dumps(base), encoding="utf-8")
    contracts_module._reset_task_reference_contract_validator_cache()
    monkeypatch.setattr(contracts_module, "_TASK_REFERENCE_CONTRACT_SCHEMA_PATH", drifted)
    with pytest.raises(ContractValidationError, match="no longer closes its source object"):
        contracts_module.task_reference_contract_validator()
    contracts_module._reset_task_reference_contract_validator_cache()


def test_deliver_intent_validator_trust_root_fails_closed(monkeypatch, tmp_path) -> None:
    contracts_module._reset_deliver_intent_contract_validator_cache()
    base = _load(SCHEMAS / "deliver-intent.v1.schema.json")
    del base["properties"]["selections"]["additionalProperties"]
    drifted = tmp_path / "drifted-intent.schema.json"
    drifted.write_text(json.dumps(base), encoding="utf-8")
    monkeypatch.setattr(contracts_module, "_DELIVER_INTENT_CONTRACT_SCHEMA_PATH", drifted)
    with pytest.raises(ContractValidationError, match="no longer closes its selections object"):
        contracts_module.deliver_intent_contract_validator()
    contracts_module._reset_deliver_intent_contract_validator_cache()


def test_delivery_eligibility_validator_trust_root_fails_closed(monkeypatch, tmp_path) -> None:
    contracts_module._reset_delivery_eligibility_contract_validator_cache()
    base = _load(SCHEMAS / "delivery-eligibility.v1.schema.json")
    del base["properties"]["reasons"]["items"]["additionalProperties"]
    drifted = tmp_path / "drifted-eligibility.schema.json"
    drifted.write_text(json.dumps(base), encoding="utf-8")
    monkeypatch.setattr(contracts_module, "_DELIVERY_ELIGIBILITY_CONTRACT_SCHEMA_PATH", drifted)
    with pytest.raises(ContractValidationError, match="no longer closes its reason object"):
        contracts_module.delivery_eligibility_contract_validator()
    contracts_module._reset_delivery_eligibility_contract_validator_cache()
