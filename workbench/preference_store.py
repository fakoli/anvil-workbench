"""Scoped durable preference storage + stale-write rejection
(preferences-configuration: T002.2).

Extracted verbatim from ``workbench.store``; re-exported there for backward
compatibility.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Mapping

from .models import (
    EffectiveValue, PreferenceRecord, resolve_effective_settings,
    reviewed_catalog_valid_refs, validate_setting_value,
)
from .store_base import StoreError


# ---------------------------------------------------------------------------
# Scoped durable preference storage + stale-write rejection
# (preferences-configuration: T002.2)
# ---------------------------------------------------------------------------


class PreferenceStoreError(StoreError):
    """A preference store operation violates its scoping or concurrency contract."""


class UnknownPreferenceError(PreferenceStoreError):
    """No such stored preference for this scope.

    Raised identically for a genuinely missing preference and for another
    actor's or project's preference, so a cross-scope probe can never learn
    whether the record exists — the indistinct not-found mirrors the
    run-context and project-context stores.
    """


class StalePreferenceWriteError(PreferenceStoreError):
    """An optimistic write lost a version race; the caller must reload.

    Deliberately distinct from :class:`~workbench.models.PreferenceValidationError`
    (a malformed value): a stale write is a reload-required conflict, not a bad
    request, and the stored value is left unchanged.  Carries the current stored
    ``write_version`` so the caller can reload and retry against it.
    """

    def __init__(self, current_version: int) -> None:
        self.current_version = current_version
        self.reload_required = True
        super().__init__("a newer version exists; reload required before writing")


@dataclass
class PreferenceRows:
    """The persisted row container shared by preference-store instances.

    ``records`` maps ``(scope, scope_key) -> {setting_id -> PreferenceRecord}``.
    The ``(scope, scope_key)`` namespace is the hard cross-scope boundary:
    ``(personal, alice)`` and ``(personal, bob)`` and ``(project, proj_1)`` are
    three disjoint namespaces.  Handing the same rows to a fresh
    :class:`MemoryPreferenceStore` simulates a hub restart over the same records.
    """

    records: dict[tuple[str, str], dict[str, PreferenceRecord]] = field(default_factory=dict)


class MemoryPreferenceStore:
    """Hermetic, lock-serialized scoped preference store with stale-write rejection.

    Every mutation runs the read-current-version check and the write under one
    reentrant lock, so two concurrent same-version writers cannot both commit:
    the first increments the version, the second observes it and is rejected as
    stale.  A setting is writable only at the scope its descriptor owns, so a
    personal actor can never write a project/deployment/policy value, and each
    ``(scope, scope_key)`` namespace is isolated so a cross-actor or cross-project
    read returns the indistinct not-found.
    """

    def __init__(self, catalog: Mapping[str, Any], rows: PreferenceRows | None = None) -> None:
        self._lock = threading.RLock()
        self.catalog = catalog
        self._by_id: dict[str, Mapping[str, Any]] = {
            str(setting.get("id")): setting
            for setting in catalog.get("settings", [])
            if isinstance(setting, Mapping)
        }
        self.rows = rows if rows is not None else PreferenceRows()

    @staticmethod
    def _require_scope(scope: str, scope_key: str) -> tuple[str, str]:
        if scope not in ("personal", "project", "deployment", "policy"):
            raise PreferenceStoreError(f"unknown preference scope: {scope!r}")
        if not isinstance(scope_key, str) or not scope_key:
            raise PreferenceStoreError("a preference operation requires a scope key")
        return scope, scope_key

    def _descriptor(self, setting_id: str) -> Mapping[str, Any]:
        descriptor = self._by_id.get(setting_id)
        if descriptor is None:
            raise UnknownPreferenceError("unknown preference")
        return descriptor

    def _writable_descriptor(self, scope: str, setting_id: str) -> Mapping[str, Any]:
        """Return the descriptor only if this scope may write it; else fail closed."""
        descriptor = self._descriptor(setting_id)
        if descriptor.get("scope") != scope:
            # A setting is owned by exactly one scope. Writing it from another
            # scope is a cross-scope write attempt and must be INDISTINGUISHABLE
            # from an unknown id: raising a distinct "not owned by this scope"
            # error made the write surface an existence oracle (a probe could
            # tell that an authority setting id exists — the very ids the read
            # surface hides). Raise the SAME indistinct not-found so a
            # cross-scope write leaks neither the id's existence nor its value.
            raise UnknownPreferenceError("unknown preference")
        if descriptor.get("mutability") == "env_only":
            raise PreferenceStoreError("setting is environment-managed and not writable through the store")
        if descriptor.get("mutability") == "approval_gated":
            # An approval-gated setting (a policy) requires a bound, consumed
            # approval before it commits. That approval layer is not wired into
            # this store, so a direct actor write must FAIL CLOSED rather than
            # commit unapproved. Authority values are seeded via
            # :meth:`seed_authority_value`, which represents the already-approved
            # / environment-derived write, never an actor-proposed one.
            raise PreferenceStoreError("setting is approval-gated and cannot be written without an approval")
        return descriptor

    def get(self, scope: str, scope_key: str, setting_id: str) -> PreferenceRecord:
        """Return one stored preference record, or the indistinct not-found.

        A record in another actor's or project's namespace is not in this
        namespace, so a cross-scope read raises the same
        :class:`UnknownPreferenceError` a genuinely missing record raises.
        """
        scope, scope_key = self._require_scope(scope, scope_key)
        namespace = self.rows.records.get((scope, scope_key))
        if namespace is None or setting_id not in namespace:
            raise UnknownPreferenceError("unknown preference")
        return namespace[setting_id]

    def current_version(self, scope: str, scope_key: str, setting_id: str) -> int:
        """The stored write version for a setting in this namespace, or 0 if unset."""
        scope, scope_key = self._require_scope(scope, scope_key)
        namespace = self.rows.records.get((scope, scope_key))
        record = namespace.get(setting_id) if namespace is not None else None
        return record.write_version if record is not None else 0

    def stored_values(self, scope: str, scope_key: str) -> dict[str, Any]:
        """The ``{setting_id: value}`` map for one namespace, for the resolver.

        Returns only this ``(scope, scope_key)`` namespace's own values; the
        caller merges the scopes it is authorized to read.
        """
        scope, scope_key = self._require_scope(scope, scope_key)
        namespace = self.rows.records.get((scope, scope_key), {})
        return {setting_id: record.value for setting_id, record in namespace.items()}

    def owned_values(self, scope: str, scope_key: str) -> dict[str, Any]:
        """Namespace values RESTRICTED to setting ids this scope actually owns.

        A durable row is keyed by ``(scope, scope_key)`` but its ``setting_id``
        is not otherwise pinned to that scope, so a corrupt or injected row could
        carry a foreign-scope id (e.g. a ``policy.*`` id sitting in a personal
        namespace). Merging such a row would let a lower-authority scope override
        a higher-authority value against the declared ``scope_precedence``. This
        filters to only the ids whose descriptor is owned by ``scope``, so a
        mis-scoped row is dropped at the merge boundary and cannot escalate.
        """
        return {
            setting_id: value
            for setting_id, value in self.stored_values(scope, scope_key).items()
            if self._by_id.get(setting_id, {}).get("scope") == scope
        }

    def seed_authority_value(
        self, scope: str, setting_id: str, value: Any, *,
        updated_by: str = "authority", expected_version: int | None = None,
    ) -> PreferenceRecord:
        """Seed a deployment/policy authority value, bypassing the actor gate.

        Actor writes (:meth:`set_preference`) fail closed for authority scopes:
        an ``env_only`` deployment value comes from the environment and an
        ``approval_gated`` policy value requires a consumed approval. This method
        represents that already-authorized authority write (the output of the
        environment/approval layer) so the hub — and tests standing in for it —
        can establish a ceiling/allowlist without minting an unapproved actor
        write. It refuses any actor scope, and still typed-validates the value.

        When ``expected_version`` is supplied the write is optimistic-concurrency
        guarded exactly like :meth:`set_preference`: the read-current-version
        check and the write run under one lock, so a stale authority commit
        (e.g. a policy operation whose bound version has been overtaken) raises
        :class:`StalePreferenceWriteError` and leaves the stored value UNCHANGED
        rather than double-applying. ``None`` keeps the unguarded seed behaviour
        for the environment/ceiling-seeding callers that do not race.
        """
        if scope not in ("deployment", "policy"):
            raise PreferenceStoreError("seed_authority_value is only for deployment/policy scopes")
        descriptor = self._descriptor(setting_id)
        if descriptor.get("scope") != scope:
            raise PreferenceStoreError("authority seed setting is not owned by this scope")
        validate_setting_value(descriptor, value)
        if expected_version is not None and (
            not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 0
        ):
            raise PreferenceStoreError("expected_version must be a non-negative integer")
        with self._lock:
            namespace = self.rows.records.setdefault((scope, scope), {})
            existing = namespace.get(setting_id)
            current = existing.write_version if existing is not None else 0
            if expected_version is not None and expected_version != current:
                # Reload-required: the stored authority value is left untouched.
                raise StalePreferenceWriteError(current)
            record = PreferenceRecord(
                setting_id=setting_id,
                scope=scope,
                scope_key=scope,
                value=value,
                write_version=current + 1,
                updated_by=updated_by,
            )
            namespace[setting_id] = record
            return record

    def clear_authority_value(
        self, scope: str, setting_id: str, *, expected_version: int, updated_by: str = "authority",
    ) -> None:
        """Remove a deployment/policy authority override under an optimistic guard.

        The authority-scope counterpart to :meth:`reset_preference`: it drops the
        stored override so the setting falls back to its descriptor default. Used
        by the policy-operation gate for an approved ``preference.reset`` of a
        policy setting. A stale ``expected_version`` raises
        :class:`StalePreferenceWriteError` and leaves the stored value unchanged.
        """
        if scope not in ("deployment", "policy"):
            raise PreferenceStoreError("clear_authority_value is only for deployment/policy scopes")
        descriptor = self._descriptor(setting_id)
        if descriptor.get("scope") != scope:
            raise PreferenceStoreError("authority setting is not owned by this scope")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 0:
            raise PreferenceStoreError("expected_version must be a non-negative integer")
        with self._lock:
            namespace = self.rows.records.get((scope, scope))
            existing = namespace.get(setting_id) if namespace is not None else None
            current = existing.write_version if existing is not None else 0
            if expected_version != current:
                raise StalePreferenceWriteError(current)
            if namespace is not None and setting_id in namespace:
                del namespace[setting_id]

    def _resolved_effective(
        self, scope: str, scope_key: str, setting_id: str, *, live_valid_refs: Mapping[str, Any] | None,
    ) -> EffectiveValue:
        """Resolve one setting's effective value through the SHARED resolver.

        Builds the same merged view the GET endpoint resolves for this setting —
        the authority namespaces (deployment/policy, the source of any ceiling)
        plus the setting's own ``(scope, scope_key)`` namespace — ownership
        filtered so a mis-scoped row cannot cross over, then runs the one shared
        :func:`resolve_effective_settings` with the same ceiling + ref-validity
        inputs. Because each setting is single-scope and every ceiling is
        authority-owned, this agrees byte-for-byte with the API GET effective
        value for the same setting (T002.3 criterion 3).
        """
        refs = live_valid_refs if live_valid_refs is not None else reviewed_catalog_valid_refs(self.catalog)
        merged: dict[str, Any] = {}
        merged.update(self.owned_values("deployment", "deployment"))
        merged.update(self.owned_values("policy", "policy"))
        merged.update(self.owned_values(scope, scope_key))
        resolved = resolve_effective_settings(self.catalog, merged, live_valid_refs=refs)
        return resolved[setting_id]

    def set_preference(
        self,
        scope: str,
        scope_key: str,
        setting_id: str,
        value: Any,
        expected_version: int,
        actor: str,
    ) -> PreferenceRecord:
        """Commit one scoped preference write under optimistic concurrency.

        The value is typed-validated against its descriptor BEFORE any version
        check, so a malformed value raises :class:`PreferenceValidationError`
        (a 422) rather than a stale-write conflict.  A stale ``expected_version``
        raises :class:`StalePreferenceWriteError` (a reload-required 409) and
        leaves the stored value unchanged.  A valid write commits atomically and
        increments the version by exactly one.
        """
        scope, scope_key = self._require_scope(scope, scope_key)
        descriptor = self._writable_descriptor(scope, setting_id)
        # Typed value validation is first and is NOT a concurrency conflict.
        validate_setting_value(descriptor, value)
        if not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 0:
            raise PreferenceStoreError("expected_version must be a non-negative integer")
        with self._lock:
            namespace = self.rows.records.get((scope, scope_key))
            existing = namespace.get(setting_id) if namespace is not None else None
            current = existing.write_version if existing is not None else 0
            if expected_version != current:
                # Reload-required: the stored value is left exactly as it was.
                raise StalePreferenceWriteError(current)
            record = PreferenceRecord(
                setting_id=setting_id,
                scope=scope,
                scope_key=scope_key,
                value=value,
                write_version=current + 1,
                updated_by=actor,
            )
            self.rows.records.setdefault((scope, scope_key), {})[setting_id] = record
            return record

    def reset_preference(
        self,
        scope: str,
        scope_key: str,
        setting_id: str,
        expected_version: int,
        actor: str,
        *,
        live_valid_refs: Mapping[str, Any] | None = None,
    ) -> EffectiveValue:
        """Reset one preference to its declared inherited/default state.

        Subject to the same optimistic check as a write (a stale reset is
        reload-required and leaves the stored value untouched).  On success the
        stored override is removed, so the setting falls back to its descriptor
        default (or unset).

        The returned effective value is resolved through the SAME shared resolver
        the GET endpoint uses — applying the policy ceiling and the ref-validity
        set — so ``reset`` and ``GET /api/preferences`` report the identical
        effective value for the identical state (T002.3 criterion 3). Reporting a
        bare descriptor default here (ignoring the ceiling/refs) made the two
        surfaces disagree — e.g. reset saying 30/default while GET said the
        clamped value. The API passes the same ``live_valid_refs`` it resolves
        GET with; ``None`` falls back to the reviewed-catalog baseline.
        """
        scope, scope_key = self._require_scope(scope, scope_key)
        # Ownership/gate is still enforced (a cross-scope reset is the indistinct
        # not-found, an authority reset fails closed) exactly like a write.
        self._writable_descriptor(scope, setting_id)
        if not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 0:
            raise PreferenceStoreError("expected_version must be a non-negative integer")
        with self._lock:
            namespace = self.rows.records.get((scope, scope_key))
            existing = namespace.get(setting_id) if namespace is not None else None
            current = existing.write_version if existing is not None else 0
            if expected_version != current:
                raise StalePreferenceWriteError(current)
            if namespace is not None and setting_id in namespace:
                del namespace[setting_id]
            return self._resolved_effective(
                scope, scope_key, setting_id, live_valid_refs=live_valid_refs
            )

    def apply_batch(
        self, operations: "list[Mapping[str, Any]]", actor: str,
    ) -> list[dict[str, Any]]:
        """Atomically apply a set of scoped set/reset operations, all-or-nothing.

        Each operation is ``{scope, scope_key, setting_id, op, value?, expected_version}``
        where ``op`` is ``"set"`` or ``"reset"``.  This is the batch counterpart of
        :meth:`set_preference` / :meth:`reset_preference` and reuses the SAME typed
        validation and optimistic-concurrency rules: under one lock it first
        validates every writable descriptor + set value AND checks every
        ``expected_version``, and only then commits every operation.  If ANY check
        fails — a malformed value (:class:`~workbench.models.PreferenceValidationError`),
        a cross-scope/unknown id (:class:`UnknownPreferenceError`), or a stale version
        (:class:`StalePreferenceWriteError`) — nothing is committed, so a batch import
        or a scoped reset lands entirely or not at all (T006.2 / T006.3 atomicity).

        Every operation is bound to exactly its own ``(scope, scope_key)`` namespace,
        so a batch touches ONLY the named scopes and can never mutate another actor's
        namespace, a project it did not name, or a deployment/policy authority value
        (scope isolation).  Returns one result per applied op with its committed
        record (a ``set``) or ``None`` (a ``reset``) for the caller's audit trail.
        """
        prepared: list[tuple[str, str, str, str, Any, int]] = []
        seen_keys: set[tuple[str, str, str]] = set()
        with self._lock:
            # Phase 1 -- validate everything and check every version. NO mutation.
            for op in operations:
                scope, scope_key = self._require_scope(op["scope"], op["scope_key"])
                setting_id = op["setting_id"]
                # A duplicate (scope, scope_key, setting_id) in one batch would pass
                # the SAME phase-1 version check twice and then double-commit; reject
                # it up front so the batch stays a single, unambiguous atomic apply.
                batch_key = (scope, scope_key, setting_id)
                if batch_key in seen_keys:
                    raise PreferenceStoreError(
                        f"duplicate operation for {setting_id!r} in scope {scope!r} within one batch"
                    )
                seen_keys.add(batch_key)
                # Ownership/writability gate (cross-scope -> indistinct not-found,
                # authority/env_only -> fail closed) exactly like a single write.
                descriptor = self._writable_descriptor(scope, setting_id)
                kind = op["op"]
                if kind == "set":
                    validate_setting_value(descriptor, op.get("value"))
                elif kind != "reset":
                    raise PreferenceStoreError(f"unknown batch operation: {kind!r}")
                expected = op["expected_version"]
                if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
                    raise PreferenceStoreError("expected_version must be a non-negative integer")
                namespace = self.rows.records.get((scope, scope_key))
                existing = namespace.get(setting_id) if namespace is not None else None
                current = existing.write_version if existing is not None else 0
                if expected != current:
                    # Reload-required: NOTHING in the batch is committed.
                    raise StalePreferenceWriteError(current)
                prepared.append((scope, scope_key, setting_id, kind, op.get("value"), current))
            # Phase 2 -- commit every op; all validation + version checks passed.
            results: list[dict[str, Any]] = []
            for scope, scope_key, setting_id, kind, value, current in prepared:
                if kind == "set":
                    record = PreferenceRecord(
                        setting_id=setting_id, scope=scope, scope_key=scope_key,
                        value=value, write_version=current + 1, updated_by=actor,
                    )
                    self.rows.records.setdefault((scope, scope_key), {})[setting_id] = record
                    results.append({
                        "setting_id": setting_id, "scope": scope, "scope_key": scope_key,
                        "op": "set", "record": record,
                    })
                else:  # reset
                    namespace = self.rows.records.get((scope, scope_key))
                    if namespace is not None and setting_id in namespace:
                        del namespace[setting_id]
                    results.append({
                        "setting_id": setting_id, "scope": scope, "scope_key": scope_key,
                        "op": "reset", "record": None,
                    })
            return results
