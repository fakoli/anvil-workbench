"""Compile and preflight the immutable per-workflow snapshot of pinned digests.

T004.1 (:mod:`workbench.provider_catalogs`) publishes the discovered provider
catalogs and T004.2 (:mod:`workbench.capability_profiles`) pins the reviewed
project capability profile against them.  This module closes T004.3: when a
workflow is compiled, every selected descriptor and digest -- provider
catalogs, operations, the Serving route/model profiles, skills, approval
actions, the budget limits, the capability-profile digest, and the workflow
digest itself -- is captured once into a frozen, source-attributed
:class:`WorkflowSnapshot`.  A later catalog or profile refresh can never
reinterpret an already-compiled snapshot: the snapshot holds only deep-copied
scalars and frozen tuples, and it carries its own domain-separated
``snapshot_digest`` so tampering is detectable.

Authority model:

* :func:`compile_workflow_snapshot` accepts only the registry's own
  :class:`PublishedCatalogSet` and the validator's own
  :class:`PinnedCapabilityProfile`; a caller-assembled mapping is refused by
  type, so a hub- or model-supplied catalog/profile has no parameter to
  arrive through.
* Every selected operation must resolve at its exact
  ``(provider, id, contract_version, operation_digest)`` in both the
  discovered catalogs and the pinned profile allowlist; every selected skill,
  route, model profile, and approval action must be profile-pinned.  The
  selection must exactly cover what the workflow document references --
  a workflow step cannot smuggle an unselected descriptor and a compile
  cannot carry an unused grant.
* :func:`preflight_snapshot` re-derives every pinned digest against the
  CURRENT discovered catalogs and profile before any effect.  A matching
  snapshot passes; any missing or changed pin refuses with stable typed
  :class:`SnapshotDrift` metadata (:class:`WorkflowSnapshotDriftError`),
  mirroring the ``validate_bridge_command_snapshot`` semantics in
  :mod:`workbench.contracts`.

Like the T004.1/T004.2 halves, this compiler is implemented and hermetically
tested but not wired into live workflow queueing yet.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from jsonschema.exceptions import ValidationError

from .capability_profiles import PinnedCapabilityProfile, PinnedLimits
from .contracts import (
    ContractValidationError,
    contract_digest,
    workflow_contract_validator,
)
from .provider_catalogs import (
    ProviderCatalogError,
    PublishedCatalog,
    PublishedCatalogSet,
    PublishedOperation,
)


class WorkflowSnapshotError(RuntimeError):
    """A workflow snapshot cannot be compiled or trusted for execution."""


SNAPSHOT_SCHEMA_VERSION = "workbench-workflow-snapshot/v1"

#: The closed set of drift kinds preflight may report.  These strings are
#: stable receipt metadata; extend the tuple, never rename a member.
DRIFT_KINDS = (
    "snapshot_digest_mismatch",
    "profile_digest_changed",
    "catalog_missing",
    "catalog_digest_changed",
    "operation_removed",
    "operation_digest_changed",
    "skill_removed",
    "skill_digest_changed",
    "model_profile_removed",
    "route_removed",
    "approval_action_removed",
)


@dataclass(frozen=True)
class SnapshotDrift:
    """One stable, typed preflight refusal: which pin no longer holds.

    ``pinned``/``current`` are digests where the descriptor carries one and
    ``None`` where the descriptor is absent on that side.  ``provider`` is
    ``None`` for profile-sourced pins (skills, routes, approval actions).
    """

    kind: str
    descriptor: str
    provider: str | None
    pinned: str | None
    current: str | None

    def __post_init__(self) -> None:
        if self.kind not in DRIFT_KINDS:
            raise WorkflowSnapshotError(f"drift kind is not declared: {self.kind!r}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "descriptor": self.descriptor,
            "provider": self.provider,
            "pinned": self.pinned,
            "current": self.current,
        }


class WorkflowSnapshotDriftError(WorkflowSnapshotError):
    """Preflight refusal carrying every detected typed drift record."""

    def __init__(self, drifts: Sequence[SnapshotDrift]) -> None:
        self.drifts: tuple[SnapshotDrift, ...] = tuple(drifts)
        summary = "; ".join(
            f"{drift.kind}:{drift.descriptor}" for drift in self.drifts
        )
        super().__init__(f"workflow snapshot drift detected before effect: {summary}")


@dataclass(frozen=True)
class PinnedCatalogSnapshot:
    """One provider catalog at the exact discovered revision and digest."""

    provider: str
    catalog_version: str
    catalog_digest: str

    def as_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "catalog_version": self.catalog_version,
            "catalog_digest": self.catalog_digest,
        }


@dataclass(frozen=True)
class PinnedOperationSnapshot:
    """One selected operation descriptor, attributed to its source catalog."""

    provider: str
    id: str
    contract_version: str
    operation_digest: str
    effect: str
    source_catalog_version: str
    source_catalog_digest: str

    def as_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "id": self.id,
            "contract_version": self.contract_version,
            "operation_digest": self.operation_digest,
            "effect": self.effect,
            "source_catalog_version": self.source_catalog_version,
            "source_catalog_digest": self.source_catalog_digest,
        }


@dataclass(frozen=True)
class PinnedSkillSnapshot:
    """One selected skill digest, attributed to its source capability profile."""

    id: str
    digest: str
    source_profile_digest: str

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "digest": self.digest,
            "source_profile_digest": self.source_profile_digest,
        }


@dataclass(frozen=True)
class PinnedRouteSnapshot:
    """The selected Serving route/model profile, attributed to the profile."""

    model_profile: str
    source_profile_digest: str

    def as_dict(self) -> dict[str, str]:
        return {
            "model_profile": self.model_profile,
            "source_profile_digest": self.source_profile_digest,
        }


@dataclass(frozen=True)
class WorkflowSnapshot:
    """The immutable, source-attributed run context for one compiled workflow."""

    workflow_id: str
    workflow_revision: str
    workflow_digest: str
    capability_profile_id: str
    capability_profile_revision: str
    capability_profile_digest: str
    catalogs: tuple[PinnedCatalogSnapshot, ...]
    operations: tuple[PinnedOperationSnapshot, ...]
    skills: tuple[PinnedSkillSnapshot, ...]
    model_profiles: tuple[str, ...]
    route: PinnedRouteSnapshot | None
    approval_actions: tuple[str, ...]
    limits: PinnedLimits
    snapshot_digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "workflow": {
                "id": self.workflow_id,
                "revision": self.workflow_revision,
                "digest": self.workflow_digest,
            },
            "capability_profile": {
                "id": self.capability_profile_id,
                "revision": self.capability_profile_revision,
                "digest": self.capability_profile_digest,
            },
            "catalogs": [catalog.as_dict() for catalog in self.catalogs],
            "operations": [operation.as_dict() for operation in self.operations],
            "skills": [skill.as_dict() for skill in self.skills],
            "model_profiles": list(self.model_profiles),
            "route": self.route.as_dict() if self.route is not None else None,
            "approval_actions": list(self.approval_actions),
            "limits": self.limits.as_dict(),
            "snapshot_digest": self.snapshot_digest,
        }

    def bridge_snapshot(self) -> dict[str, Any]:
        """The exact ``workflow_snapshot`` block a v1 bridge command carries.

        Shape-compatible with ``bridge-command.v1.schema.json`` and consumed
        by :func:`workbench.contracts.validate_bridge_command_snapshot`.
        """
        return {
            "workflow_digest": self.workflow_digest,
            "catalogs": [
                {"provider": catalog.provider, "digest": catalog.catalog_digest}
                for catalog in self.catalogs
            ],
            "capability_profile_digest": self.capability_profile_digest,
        }


def _operation_key(value: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(value.get("provider", "")),
        str(value.get("id", "")),
        str(value.get("contract_version", "")),
        str(value.get("operation_digest", "")),
    )


def _validated_workflow(workflow: Any) -> Mapping[str, Any]:
    if not isinstance(workflow, Mapping):
        raise WorkflowSnapshotError("workflow is not a JSON object")
    try:
        validator = workflow_contract_validator()
    except ContractValidationError as exc:
        raise WorkflowSnapshotError(str(exc)) from exc
    try:
        validator.validate(dict(workflow))
    except ValidationError as exc:
        raise WorkflowSnapshotError(
            f"workflow does not conform to the workflow contract: {exc.message}"
        ) from exc
    return workflow


def _resolve_catalog_operation(
    published_catalogs: PublishedCatalogSet, key: tuple[str, str, str, str]
) -> tuple[PublishedCatalog, PublishedOperation]:
    provider, operation_id, contract_version, operation_digest = key
    try:
        catalog = published_catalogs.catalog(provider)
    except ProviderCatalogError as exc:
        raise WorkflowSnapshotError(
            f"selected operation {operation_id} names a provider with no discovered catalog: {provider}"
        ) from exc
    for operation in catalog.operations:
        if (
            operation.id == operation_id
            and operation.contract_version == contract_version
            and operation.operation_digest == operation_digest
        ):
            return catalog, operation
    raise WorkflowSnapshotError(
        f"selected operation is not present at the pinned digest in the discovered "
        f"{provider} catalog: {operation_id} {contract_version}"
    )


def _pin_selected_operations(
    workflow: Mapping[str, Any],
    pinned_profile: PinnedCapabilityProfile,
    published_catalogs: PublishedCatalogSet,
    selected_operations: Sequence[Mapping[str, Any]],
) -> tuple[PinnedOperationSnapshot, ...]:
    allowlisted = {
        (grant.provider, grant.id, grant.contract_version, grant.operation_digest)
        for grant in pinned_profile.operations
    }
    selected_keys: list[tuple[str, str, str, str]] = []
    pinned: list[PinnedOperationSnapshot] = []
    for reference in selected_operations:
        if not isinstance(reference, Mapping):
            raise WorkflowSnapshotError("selected operation reference is not an object")
        key = _operation_key(reference)
        if key in selected_keys:
            raise WorkflowSnapshotError(
                f"duplicate selected operation: {key[0]} {key[1]} {key[2]}"
            )
        if key not in allowlisted:
            raise WorkflowSnapshotError(
                f"selected operation is not allowlisted by the pinned capability profile: "
                f"{key[0]} {key[1]} {key[2]}"
            )
        catalog, operation = _resolve_catalog_operation(published_catalogs, key)
        selected_keys.append(key)
        pinned.append(
            PinnedOperationSnapshot(
                provider=key[0],
                id=key[1],
                contract_version=key[2],
                operation_digest=key[3],
                effect=operation.effect,
                source_catalog_version=catalog.catalog_version,
                source_catalog_digest=catalog.catalog_digest,
            )
        )
    workflow_keys = {
        _operation_key(step["operation"])
        for step in workflow["steps"]
        if step.get("kind") == "operation"
    }
    missing = workflow_keys - set(selected_keys)
    if missing:
        provider, operation_id, contract_version, _ = sorted(missing)[0]
        raise WorkflowSnapshotError(
            f"workflow references an unselected operation: {provider} {operation_id} {contract_version}"
        )
    unused = set(selected_keys) - workflow_keys
    if unused:
        provider, operation_id, contract_version, _ = sorted(unused)[0]
        raise WorkflowSnapshotError(
            f"selected operation is not referenced by the workflow: {provider} {operation_id} {contract_version}"
        )
    return tuple(
        sorted(
            pinned,
            key=lambda item: (item.provider, item.id, item.contract_version, item.operation_digest),
        )
    )


def _pin_selected_skills(
    workflow: Mapping[str, Any],
    pinned_profile: PinnedCapabilityProfile,
    selected_skills: Sequence[Mapping[str, Any]],
) -> tuple[PinnedSkillSnapshot, ...]:
    profile_skills = {grant.id: grant.digest for grant in pinned_profile.skills}
    selected: dict[str, str] = {}
    for reference in selected_skills:
        if not isinstance(reference, Mapping):
            raise WorkflowSnapshotError("selected skill reference is not an object")
        skill_id = str(reference.get("id", ""))
        digest = str(reference.get("digest", ""))
        if skill_id in selected:
            raise WorkflowSnapshotError(f"duplicate selected skill: {skill_id}")
        profile_digest = profile_skills.get(skill_id)
        if profile_digest is None:
            raise WorkflowSnapshotError(
                f"selected skill is not pinned by the capability profile: {skill_id}"
            )
        if profile_digest != digest:
            raise WorkflowSnapshotError(
                f"selected skill digest differs from the profile pin: {skill_id}"
            )
        selected[skill_id] = digest
    # Collect every (id, digest) the workflow references — keyed by the FULL
    # pair like operations, so a step that lists one skill id under two
    # digests cannot silently drop the un-selected one (order-independent).
    workflow_skill_keys: set[tuple[str, str]] = set()
    for step in workflow["steps"]:
        if step.get("kind") != "agent":
            continue
        for skill in step.get("skills", ()):
            workflow_skill_keys.add((str(skill["id"]), str(skill["digest"])))
    selected_keys = {(skill_id, digest) for skill_id, digest in selected.items()}
    unpinned = workflow_skill_keys - selected_keys
    if unpinned:
        skill_id, _ = sorted(unpinned)[0]
        if skill_id not in selected:
            raise WorkflowSnapshotError(f"workflow references an unselected skill: {skill_id}")
        raise WorkflowSnapshotError(
            f"workflow skill digest differs from the selected pin: {skill_id}"
        )
    unused = selected_keys - workflow_skill_keys
    if unused:
        skill_id, _ = sorted(unused)[0]
        raise WorkflowSnapshotError(
            f"selected skill is not referenced by the workflow: {skill_id}"
        )
    return tuple(
        PinnedSkillSnapshot(
            id=skill_id, digest=selected[skill_id], source_profile_digest=pinned_profile.digest,
        )
        for skill_id in sorted(selected)
    )


def _pin_model_profiles_and_route(
    workflow: Mapping[str, Any],
    pinned_profile: PinnedCapabilityProfile,
    route: str | None,
) -> tuple[tuple[str, ...], PinnedRouteSnapshot | None]:
    allowed = set(pinned_profile.model_profiles)
    model_profiles: list[str] = []
    for step in workflow["steps"]:
        if step.get("kind") != "agent":
            continue
        name = str(step["model_profile"])
        if name not in allowed:
            raise WorkflowSnapshotError(
                f"workflow model profile is not pinned by the capability profile: {name}"
            )
        if name not in model_profiles:
            model_profiles.append(name)
    pinned_route: PinnedRouteSnapshot | None = None
    if route is not None:
        name = str(route)
        if name not in allowed:
            raise WorkflowSnapshotError(
                f"selected route is not pinned by the capability profile: {name}"
            )
        pinned_route = PinnedRouteSnapshot(
            model_profile=name, source_profile_digest=pinned_profile.digest,
        )
    return tuple(sorted(model_profiles)), pinned_route


def _pin_approval_actions(
    workflow: Mapping[str, Any], pinned_profile: PinnedCapabilityProfile,
) -> tuple[str, ...]:
    allowed = set(pinned_profile.approval_actions)
    actions: list[str] = []
    for step in workflow["steps"]:
        if step.get("kind") != "approval_wait":
            continue
        action = str(step["approval_action"])
        if action not in allowed:
            raise WorkflowSnapshotError(
                f"workflow approval action is not pinned by the capability profile: {action}"
            )
        if action not in actions:
            actions.append(action)
    return tuple(sorted(actions))


def compile_workflow_snapshot(
    workflow: Any,
    pinned_profile: PinnedCapabilityProfile,
    published_catalogs: PublishedCatalogSet,
    *,
    selected_operations: Sequence[Mapping[str, Any]],
    selected_skills: Sequence[Mapping[str, Any]] = (),
    route: str | None = None,
) -> WorkflowSnapshot:
    """Compile one immutable, source-attributed snapshot of every selected pin.

    ``pinned_profile`` must be the T004.2 validator's own
    :class:`PinnedCapabilityProfile` and ``published_catalogs`` the T004.1
    registry's own :class:`PublishedCatalogSet`; a caller-assembled mapping is
    refused by type.  Every field is deep-copied into frozen scalar values at
    compile time, so a later refresh of the source registry or profile cannot
    reinterpret the returned snapshot (acceptance criterion 2), and the
    snapshot carries its own recomputable ``snapshot_digest``.
    """
    if not isinstance(pinned_profile, PinnedCapabilityProfile):
        raise WorkflowSnapshotError(
            "capability profile must be the validator's pinned profile, not a caller-assembled mapping"
        )
    if not isinstance(published_catalogs, PublishedCatalogSet):
        raise WorkflowSnapshotError(
            "discovered catalogs must be the registry's published set, not a caller-assembled mapping"
        )
    workflow = _validated_workflow(workflow)
    try:
        workflow_digest = contract_digest("workflow", workflow)
    except ContractValidationError as exc:
        raise WorkflowSnapshotError(f"workflow cannot be digested: {exc}") from exc
    catalogs = tuple(
        PinnedCatalogSnapshot(
            provider=catalog.provider,
            catalog_version=catalog.catalog_version,
            catalog_digest=catalog.catalog_digest,
        )
        for catalog in published_catalogs.catalogs
    )
    model_profiles, pinned_route = _pin_model_profiles_and_route(workflow, pinned_profile, route)
    snapshot = WorkflowSnapshot(
        workflow_id=str(workflow["id"]),
        workflow_revision=str(workflow["revision"]),
        workflow_digest=workflow_digest,
        capability_profile_id=pinned_profile.id,
        capability_profile_revision=pinned_profile.revision,
        capability_profile_digest=pinned_profile.digest,
        catalogs=catalogs,
        operations=_pin_selected_operations(
            workflow, pinned_profile, published_catalogs, selected_operations
        ),
        skills=_pin_selected_skills(workflow, pinned_profile, selected_skills),
        model_profiles=model_profiles,
        route=pinned_route,
        approval_actions=_pin_approval_actions(workflow, pinned_profile),
        limits=PinnedLimits(
            max_parallel_runs=pinned_profile.limits.max_parallel_runs,
            max_agent_turns=pinned_profile.limits.max_agent_turns,
            max_tool_calls=pinned_profile.limits.max_tool_calls,
        ),
        snapshot_digest="",
    )
    payload = snapshot.as_dict()
    del payload["snapshot_digest"]
    try:
        digest = contract_digest("workflow-snapshot", payload)
    except ContractValidationError as exc:
        raise WorkflowSnapshotError(f"workflow snapshot cannot be digested: {exc}") from exc
    object.__setattr__(snapshot, "snapshot_digest", digest)
    return snapshot


def _preflight_operations(
    snapshot: WorkflowSnapshot, current_catalogs: PublishedCatalogSet,
) -> list[SnapshotDrift]:
    drifts: list[SnapshotDrift] = []
    current_by_provider = {catalog.provider: catalog for catalog in current_catalogs.catalogs}
    for pinned in snapshot.catalogs:
        current = current_by_provider.get(pinned.provider)
        if current is None:
            drifts.append(
                SnapshotDrift(
                    kind="catalog_missing", descriptor=pinned.provider,
                    provider=pinned.provider, pinned=pinned.catalog_digest, current=None,
                )
            )
        elif current.catalog_digest != pinned.catalog_digest:
            drifts.append(
                SnapshotDrift(
                    kind="catalog_digest_changed", descriptor=pinned.provider,
                    provider=pinned.provider, pinned=pinned.catalog_digest,
                    current=current.catalog_digest,
                )
            )
    for operation in snapshot.operations:
        catalog = current_by_provider.get(operation.provider)
        candidates = [
            item for item in (catalog.operations if catalog is not None else ())
            if item.id == operation.id and item.contract_version == operation.contract_version
        ]
        if not candidates:
            drifts.append(
                SnapshotDrift(
                    kind="operation_removed", descriptor=operation.id,
                    provider=operation.provider, pinned=operation.operation_digest, current=None,
                )
            )
        elif all(item.operation_digest != operation.operation_digest for item in candidates):
            drifts.append(
                SnapshotDrift(
                    kind="operation_digest_changed", descriptor=operation.id,
                    provider=operation.provider, pinned=operation.operation_digest,
                    current=candidates[0].operation_digest,
                )
            )
    return drifts


def _preflight_profile(
    snapshot: WorkflowSnapshot, current_profile: PinnedCapabilityProfile,
) -> list[SnapshotDrift]:
    drifts: list[SnapshotDrift] = []
    if current_profile.digest != snapshot.capability_profile_digest:
        drifts.append(
            SnapshotDrift(
                kind="profile_digest_changed", descriptor=snapshot.capability_profile_id,
                provider=None, pinned=snapshot.capability_profile_digest,
                current=current_profile.digest,
            )
        )
    current_skills = {grant.id: grant.digest for grant in current_profile.skills}
    for skill in snapshot.skills:
        current_digest = current_skills.get(skill.id)
        if current_digest is None:
            drifts.append(
                SnapshotDrift(
                    kind="skill_removed", descriptor=skill.id,
                    provider=None, pinned=skill.digest, current=None,
                )
            )
        elif current_digest != skill.digest:
            drifts.append(
                SnapshotDrift(
                    kind="skill_digest_changed", descriptor=skill.id,
                    provider=None, pinned=skill.digest, current=current_digest,
                )
            )
    allowed_profiles = set(current_profile.model_profiles)
    for name in snapshot.model_profiles:
        if name not in allowed_profiles:
            drifts.append(
                SnapshotDrift(
                    kind="model_profile_removed", descriptor=name,
                    provider=None, pinned=None, current=None,
                )
            )
    if snapshot.route is not None and snapshot.route.model_profile not in allowed_profiles:
        drifts.append(
            SnapshotDrift(
                kind="route_removed", descriptor=snapshot.route.model_profile,
                provider=None, pinned=None, current=None,
            )
        )
    allowed_actions = set(current_profile.approval_actions)
    for action in snapshot.approval_actions:
        if action not in allowed_actions:
            drifts.append(
                SnapshotDrift(
                    kind="approval_action_removed", descriptor=action,
                    provider=None, pinned=None, current=None,
                )
            )
    return drifts


def preflight_snapshot(
    snapshot: WorkflowSnapshot,
    current_catalogs: PublishedCatalogSet,
    current_profile: PinnedCapabilityProfile,
) -> None:
    """Fail closed before any effect unless every pinned digest still holds.

    The snapshot's own digest is recomputed first, so a tampered snapshot is
    refused before any comparison.  Every drift is collected -- not just the
    first -- and raised as one :class:`WorkflowSnapshotDriftError` whose
    ``drifts`` are stable typed records suitable for a redacted refusal
    receipt (``catalog.digest_drift``-style).  A matching snapshot returns
    ``None`` and grants nothing by itself: the bridge still runs its own
    ``validate_bridge_command_snapshot`` immediately before the adapter.
    """
    if not isinstance(snapshot, WorkflowSnapshot):
        raise WorkflowSnapshotError(
            "preflight requires the compiler's own WorkflowSnapshot, not a caller-assembled mapping"
        )
    if not isinstance(current_catalogs, PublishedCatalogSet):
        raise WorkflowSnapshotError(
            "discovered catalogs must be the registry's published set, not a caller-assembled mapping"
        )
    if not isinstance(current_profile, PinnedCapabilityProfile):
        raise WorkflowSnapshotError(
            "capability profile must be the validator's pinned profile, not a caller-assembled mapping"
        )
    payload = snapshot.as_dict()
    del payload["snapshot_digest"]
    try:
        recomputed = contract_digest("workflow-snapshot", payload)
    except ContractValidationError as exc:
        raise WorkflowSnapshotError(f"workflow snapshot cannot be digested: {exc}") from exc
    if recomputed != snapshot.snapshot_digest:
        raise WorkflowSnapshotDriftError(
            (
                SnapshotDrift(
                    kind="snapshot_digest_mismatch", descriptor=snapshot.workflow_id,
                    provider=None, pinned=snapshot.snapshot_digest, current=recomputed,
                ),
            )
        )
    drifts = _preflight_operations(snapshot, current_catalogs)
    drifts.extend(_preflight_profile(snapshot, current_profile))
    if drifts:
        raise WorkflowSnapshotDriftError(tuple(drifts))
    return None
