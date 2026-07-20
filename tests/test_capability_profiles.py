"""Hermetic tests for project capability-profile validation against discovery.

Fixtures are the checked-in contract examples under
``docs/contracts/examples/``: the project capability profile plus the three
T001 provider catalogs it pins.  No live CLI, network, or filesystem source is
touched; the discovered set is built directly with
:func:`validate_provider_catalog`.

Acceptance mapping (state-context-operations:T004.2):

* Criterion 1 (the profile pins exact operation, route/profile, skill,
  approval-action, and budget descriptors):
  ``test_happy_path_pins_every_capability``,
  ``test_limits_outside_schema_bounds_fail_closed``.
* Criterion 2 (unknown, unprofiled, stale, or multiply-conflicting
  capabilities fail before workflow compilation):
  ``test_schema_valid_but_undiscovered_operation_fails_closed``,
  ``test_stale_operation_digest_fails_closed``,
  ``test_operation_removed_from_discovery_fails_closed``,
  ``test_duplicate_and_conflicting_operation_grants_fail_closed``,
  ``test_unconfigured_or_duplicate_model_profiles_fail_closed``,
  ``test_unknown_stale_or_duplicate_skills_fail_closed``,
  ``test_unconfigured_or_duplicate_approval_actions_fail_closed``,
  plus the digest-drift and contract-schema fail-closed tests.
* Criterion 3 (browser/model-authored additions cannot extend the reviewed
  profile; the pinned result is isolated):
  ``test_validation_accepts_only_operator_configured_authority``,
  ``test_schema_valid_but_undiscovered_operation_fails_closed``,
  ``test_pinned_profile_is_frozen_and_deep_copy_isolated``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from workbench.capability_profiles import (
    CapabilityProfileError,
    PinnedCapabilityProfile,
    validate_project_profile,
)
from workbench.contracts import contract_digest
from workbench.provider_catalogs import (
    DEFAULT_PROVIDER_ALLOWLIST,
    PublishedCatalogSet,
    validate_provider_catalog,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "docs" / "contracts" / "examples"

CONFIGURED_MODEL_PROFILES = ("coding-local", "planning-local")
CONFIGURED_SKILLS = {"anvil:execute": "sha256:" + "7" * 64}
CONFIGURED_APPROVAL_ACTIONS = ("commit_pr", "merge_and_accept")


def catalog_example(provider: str) -> dict:
    return json.loads((EXAMPLES / f"{provider}.catalog.v1.json").read_text(encoding="utf-8"))


def profile_example() -> dict:
    return json.loads(
        (EXAMPLES / "project-capability-profile.v1.json").read_text(encoding="utf-8")
    )


def rehash(profile: dict) -> dict:
    """Recompute the profile digest after a deliberate fixture mutation.

    This isolates the semantic check under test from the digest-drift check,
    which has its own dedicated test below.
    """
    profile["digest"] = contract_digest("profile", profile)
    return profile


def rehash_catalog(catalog: dict) -> dict:
    for operation in catalog["operations"]:
        operation["operation_digest"] = contract_digest("operation", operation)
    catalog["catalog_digest"] = contract_digest("catalog", catalog)
    return catalog


def discovered(overrides: dict[str, dict] | None = None) -> PublishedCatalogSet:
    catalogs = []
    for provider in sorted(DEFAULT_PROVIDER_ALLOWLIST):
        document = (overrides or {}).get(provider, catalog_example(provider))
        catalogs.append(validate_provider_catalog(provider, document))
    return PublishedCatalogSet(catalogs=tuple(catalogs))


def pin(profile: dict | None = None, catalogs: PublishedCatalogSet | None = None, **overrides):
    kwargs: dict = {
        "configured_model_profiles": CONFIGURED_MODEL_PROFILES,
        "configured_skills": dict(CONFIGURED_SKILLS),
        "approval_actions": CONFIGURED_APPROVAL_ACTIONS,
    }
    kwargs.update(overrides)
    return validate_project_profile(
        profile if profile is not None else profile_example(),
        catalogs if catalogs is not None else discovered(),
        **kwargs,
    )


def test_happy_path_pins_every_capability() -> None:
    fixture = profile_example()

    pinned = pin()

    assert isinstance(pinned, PinnedCapabilityProfile)
    assert pinned.id == fixture["id"]
    assert pinned.revision == fixture["revision"]
    assert pinned.digest == fixture["digest"]
    # Criterion 1: every grant is the exact reviewed descriptor pin.
    assert [grant.as_dict() for grant in pinned.operations] == fixture["operations"]
    assert list(pinned.model_profiles) == fixture["model_profiles"]
    assert [grant.as_dict() for grant in pinned.skills] == fixture["skills"]
    assert pinned.limits.as_dict() == fixture["limits"]
    assert list(pinned.approval_actions) == fixture["approval_actions"]
    assert pinned.as_dict() == {
        "id": fixture["id"],
        "revision": fixture["revision"],
        "digest": fixture["digest"],
        "operations": fixture["operations"],
        "model_profiles": fixture["model_profiles"],
        "skills": fixture["skills"],
        "limits": fixture["limits"],
        "approval_actions": fixture["approval_actions"],
    }


def test_untyped_inputs_fail_closed() -> None:
    with pytest.raises(CapabilityProfileError, match="not a JSON object"):
        pin(profile=["not", "an", "object"])  # type: ignore[arg-type]
    # A caller-assembled mapping is not the registry's published set; a hub or
    # model payload must have no parameter to arrive through (criterion 3).
    with pytest.raises(CapabilityProfileError, match="registry's published set"):
        validate_project_profile(
            profile_example(),
            {"anvil-state": catalog_example("anvil-state")},  # type: ignore[arg-type]
            configured_model_profiles=CONFIGURED_MODEL_PROFILES,
            configured_skills=CONFIGURED_SKILLS,
            approval_actions=CONFIGURED_APPROVAL_ACTIONS,
        )


def test_digest_drift_fails_closed_before_any_semantic_check() -> None:
    drifted = profile_example()
    drifted["revision"] = "9.9.9"  # no rehash: the advertised digest is now wrong
    with pytest.raises(CapabilityProfileError, match="digest validation"):
        pin(profile=drifted)


def test_contract_schema_violations_fail_closed() -> None:
    # A digest-valid document with an unreviewed extension field is refused:
    # the profile object is closed, so extra keys are not a channel.
    extended = profile_example()
    extended["extra_capabilities"] = ["bridge.shell.exec"]
    with pytest.raises(CapabilityProfileError, match="capability-profile contract"):
        pin(profile=rehash(extended))

    wrong_version = profile_example()
    wrong_version["schema_version"] = "workbench-capability-profile/v2"
    with pytest.raises(CapabilityProfileError, match="capability-profile contract"):
        pin(profile=rehash(wrong_version))

    no_operations = profile_example()
    no_operations["operations"] = []
    with pytest.raises(CapabilityProfileError, match="capability-profile contract"):
        pin(profile=rehash(no_operations))


def test_limits_outside_schema_bounds_fail_closed() -> None:
    for field, value in (
        ("max_parallel_runs", 0),
        ("max_parallel_runs", 17),
        ("max_agent_turns", 101),
        ("max_tool_calls", -1),
    ):
        out_of_bounds = profile_example()
        out_of_bounds["limits"][field] = value
        with pytest.raises(CapabilityProfileError, match="capability-profile contract"):
            pin(profile=rehash(out_of_bounds))


def test_schema_valid_but_undiscovered_operation_fails_closed() -> None:
    # Criteria 2 and 3: a schema-valid, digest-valid profile granting an
    # operation the catalogs never published must refuse before compilation.
    unknown = profile_example()
    unknown["operations"].append(
        {
            "provider": "anvil-state",
            "id": "state.task.delete",
            "contract_version": "1.0.0",
            "operation_digest": "sha256:" + "a" * 64,
        }
    )
    with pytest.raises(
        CapabilityProfileError, match="not present in the discovered anvil-state catalog"
    ):
        pin(profile=rehash(unknown))

    # Same id, undiscovered contract version: also unprofiled.
    wrong_version = profile_example()
    wrong_version["operations"][0]["contract_version"] = "1.4.0"
    with pytest.raises(CapabilityProfileError, match="not present in the discovered"):
        pin(profile=rehash(wrong_version))

    # A provider outside the discovered set has no catalog to resolve against.
    foreign = profile_example()
    foreign["operations"].append(
        {
            "provider": "mystery-provider",
            "id": "mystery.do",
            "contract_version": "1.0.0",
            "operation_digest": "sha256:" + "b" * 64,
        }
    )
    with pytest.raises(CapabilityProfileError, match="no discovered catalog: mystery-provider"):
        pin(profile=rehash(foreign))


def test_stale_operation_digest_fails_closed() -> None:
    stale = profile_example()
    stale["operations"][0]["operation_digest"] = "sha256:" + "c" * 64
    with pytest.raises(
        CapabilityProfileError, match="stale against the discovered anvil-state catalog"
    ):
        pin(profile=rehash(stale))


def test_operation_removed_from_discovery_fails_closed() -> None:
    # The profile was valid yesterday; today's discovery no longer publishes
    # state.task.claim, so the grant is stale and must refuse.
    narrowed = catalog_example("anvil-state")
    narrowed["operations"] = [
        operation for operation in narrowed["operations"] if operation["id"] != "state.task.claim"
    ]
    catalogs = discovered({"anvil-state": rehash_catalog(narrowed)})
    with pytest.raises(CapabilityProfileError, match="state.task.claim"):
        pin(catalogs=catalogs)


def test_duplicate_and_conflicting_operation_grants_fail_closed() -> None:
    duplicated = profile_example()
    duplicated["operations"].append(copy.deepcopy(duplicated["operations"][0]))
    with pytest.raises(CapabilityProfileError, match="duplicate operation grant"):
        pin(profile=rehash(duplicated))

    conflicting = profile_example()
    clashing = copy.deepcopy(conflicting["operations"][0])
    clashing["operation_digest"] = "sha256:" + "d" * 64
    conflicting["operations"].append(clashing)
    with pytest.raises(CapabilityProfileError, match="conflicting digests for one operation"):
        pin(profile=rehash(conflicting))


def test_unconfigured_or_duplicate_model_profiles_fail_closed() -> None:
    widened = profile_example()
    widened["model_profiles"].append("research-remote")
    with pytest.raises(
        CapabilityProfileError, match="model profile is not operator-configured: research-remote"
    ):
        pin(profile=rehash(widened))

    duplicated = profile_example()
    duplicated["model_profiles"].append("coding-local")
    with pytest.raises(CapabilityProfileError, match="duplicate model profile"):
        pin(profile=rehash(duplicated))


def test_unknown_stale_or_duplicate_skills_fail_closed() -> None:
    unknown = profile_example()
    unknown["skills"].append({"id": "anvil:finish", "digest": "sha256:" + "e" * 64})
    with pytest.raises(
        CapabilityProfileError, match="skill is not operator-configured: anvil:finish"
    ):
        pin(profile=rehash(unknown))

    stale = profile_example()
    stale["skills"][0]["digest"] = "sha256:" + "f" * 64
    with pytest.raises(CapabilityProfileError, match="skill digest is stale"):
        pin(profile=rehash(stale))

    duplicated = profile_example()
    duplicated["skills"].append(copy.deepcopy(duplicated["skills"][0]))
    with pytest.raises(CapabilityProfileError, match="duplicate skill"):
        pin(profile=rehash(duplicated))


def test_unconfigured_or_duplicate_approval_actions_fail_closed() -> None:
    widened = profile_example()
    widened["approval_actions"].append("deployment")
    with pytest.raises(
        CapabilityProfileError, match="approval action is not operator-configured: deployment"
    ):
        pin(profile=rehash(widened))

    duplicated = profile_example()
    duplicated["approval_actions"].append("commit_pr")
    with pytest.raises(CapabilityProfileError, match="duplicate approval action"):
        pin(profile=rehash(duplicated))

    # The schema keeps approval_actions optional; its absence grants nothing.
    absent = profile_example()
    del absent["approval_actions"]
    assert pin(profile=rehash(absent)).approval_actions == ()


def test_validation_accepts_only_operator_configured_authority() -> None:
    # Criterion 3, allowlist half: narrowing the operator configuration
    # refuses the same reviewed profile -- the profile cannot self-authorize.
    with pytest.raises(CapabilityProfileError, match="not operator-configured"):
        pin(configured_model_profiles=("coding-local",))
    with pytest.raises(CapabilityProfileError, match="not operator-configured"):
        pin(configured_skills={})
    with pytest.raises(CapabilityProfileError, match="not operator-configured"):
        pin(approval_actions=("commit_pr",))
    # The default for approval_actions is the empty allowlist: deny.
    denied = profile_example()
    with pytest.raises(CapabilityProfileError, match="not operator-configured"):
        validate_project_profile(
            denied,
            discovered(),
            configured_model_profiles=CONFIGURED_MODEL_PROFILES,
            configured_skills=CONFIGURED_SKILLS,
        )


def test_pinned_profile_is_frozen_and_deep_copy_isolated() -> None:
    profile = profile_example()
    configured_skills = dict(CONFIGURED_SKILLS)
    pinned = validate_project_profile(
        profile,
        discovered(),
        configured_model_profiles=CONFIGURED_MODEL_PROFILES,
        configured_skills=configured_skills,
        approval_actions=CONFIGURED_APPROVAL_ACTIONS,
    )
    expected = pinned.as_dict()

    with pytest.raises(AttributeError):
        pinned.operations = ()  # type: ignore[misc]
    with pytest.raises(AttributeError):
        pinned.operations[0].operation_digest = "sha256:" + "0" * 64  # type: ignore[misc]
    with pytest.raises(AttributeError):
        pinned.limits.max_tool_calls = 100  # type: ignore[misc]

    # Mutating the dict projection cannot reach back into the pinned profile
    # or into any later validation (criterion 3, isolation half).
    view = pinned.as_dict()
    view["operations"].append(
        {
            "provider": "project-bridge",
            "id": "bridge.shell.exec",
            "contract_version": "1.0.0",
            "operation_digest": "sha256:" + "1" * 64,
        }
    )
    view["skills"].clear()
    view["limits"]["max_parallel_runs"] = 16
    assert pinned.as_dict() == expected

    # Mutating the source inputs after validation cannot alter the pin either.
    profile["operations"].clear()
    configured_skills.clear()
    assert pinned.as_dict() == expected

    # A fresh validation from pristine inputs still produces the same pin.
    assert pin().as_dict() == expected
