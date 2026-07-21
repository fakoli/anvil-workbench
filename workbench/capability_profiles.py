"""Validate a reviewed project capability profile against discovered catalogs.

T004.1 (:mod:`workbench.provider_catalogs`) publishes the frozen, validated
provider operation catalogs.  This module closes the profile half for T004.2:
a project capability profile -- the reviewed allowlist a workflow compiles
against -- is only trusted after it is

* digest-verified by local recompute and valid against the
  ``workbench-capability-profile/v1`` contract schema,
* resolvable operation-by-operation against the *discovered* catalogs at the
  exact pinned ``(provider, id, contract_version, operation_digest)``,
* a subset of the operator-configured model-profile, skill-digest, and
  approval-action allowlists, and
* free of duplicate or digest-conflicting entries.

Anything else fails closed with :class:`CapabilityProfileError` before a
workflow can be queued.  The ``limits`` budget block carries no separate
digest in v1; it is pinned by the profile digest itself and bounded by the
contract schema.

Authority model (acceptance criterion 3): every input to
:func:`validate_project_profile` is operator-configured local state -- the
reviewed profile document, the bridge's own :class:`PublishedCatalogSet`
(hub- or model-supplied mappings are refused by type), and the explicit
bridge-settings allowlists.  A browser- or model-authored capability addition
has no parameter to arrive through: a schema-valid profile naming an
undiscovered or unconfigured capability is refused, and the returned
:class:`PinnedCapabilityProfile` is frozen and built from scalar copies so
mutating any projection cannot alter a later validation.

Like the T004.1 registry, this validator is implemented and hermetically
tested but not wired into live workflow queueing yet.

Scope of profile v1: plugin descriptors and per-route digests are NOT part of
this contract revision — plugins are hard-disabled at the bridge
(features.plugins=false) and any plugin-shaped extension field is refused by
the closed schema; model profiles are pinned by reviewed name (their digests
live with Anvil Serving's declared surface). A future profile revision that
adds either must extend the contract schema first.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from jsonschema.exceptions import ValidationError

from .contracts import (
    ContractValidationError,
    profile_contract_validator,
    validate_profile,
)
from .provider_catalogs import ProviderCatalogError, PublishedCatalogSet
from .skills import SkillAdoptionStore, assert_skills_acknowledged


class CapabilityProfileError(RuntimeError):
    """A project capability profile cannot be trusted for workflow compilation."""


PROFILE_SCHEMA_VERSION = "workbench-capability-profile/v1"


@dataclass(frozen=True)
class PinnedOperationGrant:
    """One profile operation resolved at its exact discovered catalog pin."""

    provider: str
    id: str
    contract_version: str
    operation_digest: str

    def as_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "id": self.id,
            "contract_version": self.contract_version,
            "operation_digest": self.operation_digest,
        }


@dataclass(frozen=True)
class PinnedSkillGrant:
    """One profile skill resolved against the operator-configured digest."""

    id: str
    digest: str

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "digest": self.digest}


@dataclass(frozen=True)
class PinnedLimits:
    """The schema-bounded budget block, pinned by the profile digest."""

    max_parallel_runs: int
    max_agent_turns: int
    max_tool_calls: int

    def as_dict(self) -> dict[str, int]:
        return {
            "max_parallel_runs": self.max_parallel_runs,
            "max_agent_turns": self.max_agent_turns,
            "max_tool_calls": self.max_tool_calls,
        }


@dataclass(frozen=True)
class PinnedCapabilityProfile:
    """The frozen, fully resolved allowlist a workflow may compile against."""

    id: str
    revision: str
    digest: str
    operations: tuple[PinnedOperationGrant, ...]
    model_profiles: tuple[str, ...]
    skills: tuple[PinnedSkillGrant, ...]
    limits: PinnedLimits
    approval_actions: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "revision": self.revision,
            "digest": self.digest,
            "operations": [grant.as_dict() for grant in self.operations],
            "model_profiles": list(self.model_profiles),
            "skills": [grant.as_dict() for grant in self.skills],
            "limits": self.limits.as_dict(),
            "approval_actions": list(self.approval_actions),
        }


def _pin_operations(
    profile: Mapping[str, Any], published_catalogs: PublishedCatalogSet
) -> tuple[PinnedOperationGrant, ...]:
    grants: list[PinnedOperationGrant] = []
    seen_exact: set[tuple[str, str, str, str]] = set()
    pinned_digests: dict[tuple[str, str, str], str] = {}
    for entry in profile["operations"]:
        provider = str(entry["provider"])
        operation_id = str(entry["id"])
        contract_version = str(entry["contract_version"])
        operation_digest = str(entry["operation_digest"])
        exact = (provider, operation_id, contract_version, operation_digest)
        if exact in seen_exact:
            raise CapabilityProfileError(
                f"profile declares a duplicate operation grant: {provider} {operation_id} {contract_version}"
            )
        versioned = exact[:3]
        prior = pinned_digests.get(versioned)
        if prior is not None and prior != operation_digest:
            raise CapabilityProfileError(
                f"profile declares conflicting digests for one operation: {provider} {operation_id} {contract_version}"
            )
        seen_exact.add(exact)
        pinned_digests[versioned] = operation_digest
        try:
            catalog = published_catalogs.catalog(provider)
        except ProviderCatalogError as exc:
            raise CapabilityProfileError(
                f"profile operation {operation_id} names a provider with no discovered catalog: {provider}"
            ) from exc
        candidates = [
            operation for operation in catalog.operations
            if operation.id == operation_id and operation.contract_version == contract_version
        ]
        if not candidates:
            raise CapabilityProfileError(
                f"profile operation is not present in the discovered {provider} catalog: "
                f"{operation_id} {contract_version}"
            )
        if all(operation.operation_digest != operation_digest for operation in candidates):
            raise CapabilityProfileError(
                f"profile operation digest is stale against the discovered {provider} catalog: "
                f"{operation_id} {contract_version}"
            )
        grants.append(
            PinnedOperationGrant(
                provider=provider, id=operation_id,
                contract_version=contract_version, operation_digest=operation_digest,
            )
        )
    return tuple(grants)


def _pin_model_profiles(
    profile: Mapping[str, Any], allowed: frozenset[str]
) -> tuple[str, ...]:
    names: list[str] = []
    for name in profile["model_profiles"]:
        name = str(name)
        if name in names:
            raise CapabilityProfileError(f"profile declares a duplicate model profile: {name}")
        if name not in allowed:
            raise CapabilityProfileError(f"profile model profile is not operator-configured: {name}")
        names.append(name)
    return tuple(names)


def _pin_skills(
    profile: Mapping[str, Any], allowed: Mapping[str, str]
) -> tuple[PinnedSkillGrant, ...]:
    grants: list[PinnedSkillGrant] = []
    seen: set[str] = set()
    for entry in profile["skills"]:
        skill_id = str(entry["id"])
        digest = str(entry["digest"])
        if skill_id in seen:
            raise CapabilityProfileError(f"profile declares a duplicate skill: {skill_id}")
        seen.add(skill_id)
        configured = allowed.get(skill_id)
        if configured is None:
            raise CapabilityProfileError(f"profile skill is not operator-configured: {skill_id}")
        if configured != digest:
            raise CapabilityProfileError(
                f"profile skill digest is stale against the operator-configured digest: {skill_id}"
            )
        grants.append(PinnedSkillGrant(id=skill_id, digest=digest))
    return tuple(grants)


def _pin_approval_actions(
    profile: Mapping[str, Any], allowed: frozenset[str]
) -> tuple[str, ...]:
    actions: list[str] = []
    for action in profile.get("approval_actions", ()):
        action = str(action)
        if action in actions:
            raise CapabilityProfileError(f"profile declares a duplicate approval action: {action}")
        if action not in allowed:
            raise CapabilityProfileError(f"profile approval action is not operator-configured: {action}")
        actions.append(action)
    return tuple(actions)


def validate_project_profile(
    profile: Any,
    published_catalogs: PublishedCatalogSet,
    *,
    configured_model_profiles: Sequence[str],
    configured_skills: Mapping[str, str],
    approval_actions: Sequence[str] = (),
    skill_adoption_store: SkillAdoptionStore | None = None,
) -> PinnedCapabilityProfile:
    """Fail-closed pin one reviewed profile against discovered local authority.

    ``published_catalogs`` must be the T004.1 registry's own published set --
    the discovered-catalog source of truth -- never a caller-assembled
    mapping.  ``configured_model_profiles`` (the Serving route/profile names
    the operator declared), ``configured_skills`` (skill id to reviewed
    ``sha256:`` digest), and ``approval_actions`` are explicit bridge-settings
    allowlists, taken as parameters precisely so no ambient or
    browser-supplied channel can widen them.

    Digest verification runs before any semantic check, mirroring
    :func:`workbench.provider_catalogs.validate_provider_catalog`, so a
    drifted profile fails closed even when its entries would otherwise pass.

    When a ``skill_adoption_store`` is supplied (reviewed-tools-plugins T008),
    the profile's pinned skills are additionally gated on owner acknowledgment:
    a profile pinning a skill whose EXACT reviewed digest has not been
    acknowledged for adoption fails closed with the stable ``skill.unacknowledged``
    (or, on a since-changed body, ``skill.digest_changed``) typed refusal --
    acknowledging one digest never implicitly acknowledges a later change.  When
    it is ``None`` the adoption gate is not exercised (the legacy behaviour),
    so an operator that has not opted into the adoption ledger is unaffected.
    """
    if not isinstance(published_catalogs, PublishedCatalogSet):
        raise CapabilityProfileError(
            "discovered catalogs must be the registry's published set, not a caller-assembled mapping"
        )
    if not isinstance(profile, Mapping):
        raise CapabilityProfileError("capability profile is not a JSON object")
    try:
        validate_profile(profile)
    except ContractValidationError as exc:
        raise CapabilityProfileError(f"capability profile failed digest validation: {exc}") from exc
    try:
        validator = profile_contract_validator()
    except ContractValidationError as exc:
        raise CapabilityProfileError(str(exc)) from exc
    try:
        validator.validate(copy.deepcopy(dict(profile)))
    except ValidationError as exc:
        raise CapabilityProfileError(
            f"capability profile does not conform to the capability-profile contract: {exc.message}"
        ) from exc
    allowed_model_profiles = frozenset(str(name) for name in configured_model_profiles)
    allowed_skills = {str(key): str(value) for key, value in configured_skills.items()}
    allowed_actions = frozenset(str(action) for action in approval_actions)
    limits = profile["limits"]
    pinned_skills = _pin_skills(profile, allowed_skills)
    # T008 adoption gate: before the profile is trusted, every pinned skill's
    # EXACT digest must be owner-acknowledged for adoption.  This runs after the
    # digest/operator-configured checks (a stale or unconfigured skill is already
    # refused) and only widens the gate: an unacknowledged or since-changed skill
    # fails closed with a stable typed refusal instead of being silently trusted.
    if skill_adoption_store is not None:
        assert_skills_acknowledged(
            ((grant.id, grant.digest) for grant in pinned_skills), skill_adoption_store
        )
    return PinnedCapabilityProfile(
        id=str(profile["id"]),
        revision=str(profile["revision"]),
        digest=str(profile["digest"]),
        operations=_pin_operations(profile, published_catalogs),
        model_profiles=_pin_model_profiles(profile, allowed_model_profiles),
        skills=pinned_skills,
        limits=PinnedLimits(
            max_parallel_runs=int(limits["max_parallel_runs"]),
            max_agent_turns=int(limits["max_agent_turns"]),
            max_tool_calls=int(limits["max_tool_calls"]),
        ),
        approval_actions=_pin_approval_actions(profile, allowed_actions),
    )
