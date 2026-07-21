"""Configuration export / import / scoped reset (preferences-configuration:T006).

A vertical slice over the reviewed preference spine.  It never becomes a new
privilege: it reads and writes ONLY the portable actor/project settings the
:func:`~workbench.contracts.settings_actor_view` projection admits, through the
SAME :class:`~workbench.store.MemoryPreferenceStore` (optimistic concurrency,
typed validation, scope isolation) the rest of the surface uses.  Three
operations compose those primitives:

* **export** (T006.1) — a CLOSED, versioned, redacted serialization of only the
  portable settings the actor actually set, plus the schema version, the source
  scope, and a SAFE OPAQUE actor reference (never the raw actor identity).  A
  secret, credential, token, local path, raw URL, chat history entry, or raw
  prompt is structurally absent: the export ranges only over the actor-view ids,
  which drop every ``secret`` / path-like / authority-owned descriptor, and the
  router scrubs the serialized body at the last hop.

* **import** (T006.2) — validate / preview / apply.  An unknown or unsupported
  extension envelope is REJECTED (closed schema, not interpreted loosely).  A
  preview distinguishes the typed categories creates / changes / resets /
  skipped-read-only / unavailable-references and identifies EVERY repairable
  field; an invalid import applies NOTHING.  A valid apply is atomic (one
  :meth:`~workbench.store.MemoryPreferenceStore.apply_batch`), scope-bound,
  optimistic-version checked, and AUDITED.

* **reset** (T006.3) — a scoped reset previews the exact values + scope that will
  change, then applies atomically, version-checked, and audited, touching ONLY
  the selected actor/project namespace: another actor's values, project/system
  policy, and deployment configuration are left byte-identical.

Like the other supervision models this service is not wired into the live bridge
poll loop; the hub app leaves it ``None`` (fail-closed 503) until injected.
"""
from __future__ import annotations

from typing import Any, Mapping

from .contracts import settings_actor_view
from .models import (
    now_utc,
    opaque_scope_ref,
    preference_scope_key_fingerprint,
    require_pref_audit_key,
    reviewed_catalog_valid_refs,
    validate_setting_value,
    PreferenceValidationError,
)
from .store import MemoryPreferenceStore, StalePreferenceWriteError

#: The pinned export/envelope schema version.  An envelope declaring any other
#: version is an unknown/unsupported envelope and is REJECTED, not coerced.
CONFIGURATION_EXPORT_SCHEMA_VERSION = "workbench-configuration-export/v1"

#: The CLOSED key set of each envelope level.  A key outside these sets is an
#: unknown extension the import refuses rather than interpreting loosely
#: (T006.1 criterion 2, additionalProperties:false recursively).
_ENVELOPE_KEYS = frozenset({"schema_version", "source", "settings"})
_SOURCE_KEYS = frozenset({"scope", "actor_ref", "project_ref", "catalog_id", "catalog_revision"})
_ENTRY_KEYS = frozenset({"setting_id", "scope", "value"})

_ACTOR_SCOPES = ("personal", "project")


class ConfigurationTransferError(ValueError):
    """A configuration envelope is malformed/unsupported, or an import is invalid.

    Raised BEFORE any effect, so a rejected envelope or an invalid import mutates
    nothing.  The API maps it to a typed 422; it is deliberately distinct from the
    reload-required :class:`~workbench.store.StalePreferenceWriteError` (a 409).
    """


def validate_configuration_envelope(envelope: Any) -> dict[str, Any]:
    """Return the envelope only if it is a CLOSED, supported configuration export.

    Fail closed on anything unexpected: a non-object, an unknown top-level key
    (an extension envelope), a wrong/absent ``schema_version``, a non-list
    ``settings``, a malformed entry, an unknown entry key, or an unknown ``source``
    key.  An unsupported extension envelope is REJECTED, never interpreted loosely.
    """
    if not isinstance(envelope, Mapping):
        raise ConfigurationTransferError("a configuration envelope must be an object")
    extra = set(envelope) - _ENVELOPE_KEYS
    if extra:
        raise ConfigurationTransferError(
            f"configuration envelope has unsupported extension keys: {sorted(extra)}"
        )
    if envelope.get("schema_version") != CONFIGURATION_EXPORT_SCHEMA_VERSION:
        raise ConfigurationTransferError(
            "configuration envelope declares an unknown or unsupported schema_version"
        )
    source = envelope.get("source")
    if source is not None:
        if not isinstance(source, Mapping):
            raise ConfigurationTransferError("configuration envelope source must be an object")
        source_extra = set(source) - _SOURCE_KEYS
        if source_extra:
            raise ConfigurationTransferError(
                f"configuration envelope source has unsupported keys: {sorted(source_extra)}"
            )
    settings = envelope.get("settings")
    if not isinstance(settings, list):
        raise ConfigurationTransferError("configuration envelope settings must be a list")
    for entry in settings:
        if not isinstance(entry, Mapping):
            raise ConfigurationTransferError("a configuration setting entry must be an object")
        entry_extra = set(entry) - _ENTRY_KEYS
        if entry_extra:
            raise ConfigurationTransferError(
                f"a configuration setting entry has unsupported keys: {sorted(entry_extra)}"
            )
        if not isinstance(entry.get("setting_id"), str) or not entry.get("setting_id"):
            raise ConfigurationTransferError("a configuration setting entry requires a setting_id")
        if "value" not in entry:
            raise ConfigurationTransferError("a configuration setting entry requires a value")
    return dict(envelope)


class ConfigurationTransferService:
    """The wired export/import/reset entrypoint over the reviewed preference spine.

    Composes the reviewed primitives rather than re-implementing them: the shared
    :func:`~workbench.contracts.settings_actor_view` portability filter, the shared
    :func:`~workbench.models.resolve_effective_settings` ref-validity baseline, and
    the injected :class:`~workbench.store.MemoryPreferenceStore` (optimistic
    concurrency + typed validation + scope isolation).  The audit trail is an
    in-memory list of non-identifying records keyed by the SAME keyed
    scope-key fingerprint the single-write audit metadata uses.
    """

    def __init__(
        self,
        catalog: Mapping[str, Any],
        preference_store: MemoryPreferenceStore,
        *,
        audit_key: bytes,
        live_valid_refs: Mapping[str, Any] | None = None,
    ) -> None:
        self.catalog = catalog
        self.preferences = preference_store
        self._audit_key = require_pref_audit_key(audit_key)
        self._live_valid_refs = live_valid_refs
        self._by_id: dict[str, Mapping[str, Any]] = {
            str(s.get("id")): s for s in catalog.get("settings", []) if isinstance(s, Mapping)
        }
        # The portable id set is EXACTLY the actor-view projection: personal- and
        # project-owned, non-secret, non-path descriptors. Every export/import
        # decision ranges over this set, so an authority/secret/path id is never
        # exported and is skipped-read-only on import.
        actor_view = settings_actor_view(catalog)
        self._portable_ids: set[str] = {str(s.get("id")) for s in actor_view["settings"]}
        self._catalog_id = actor_view.get("catalog_id")
        self._catalog_revision = actor_view.get("revision")
        self._audit_records: list[dict[str, Any]] = []

    # -- shared helpers -----------------------------------------------------

    def _refs(self) -> Mapping[str, Any]:
        if self._live_valid_refs is not None:
            return self._live_valid_refs
        return reviewed_catalog_valid_refs(self.catalog)

    def _scope_key(self, scope: str, actor: str, project_id: str | None) -> str | None:
        if scope == "personal":
            return actor
        if scope == "project":
            return project_id or None
        return None

    def _current(self, scope: str, scope_key: str, setting_id: str) -> tuple[Any, int]:
        """The stored ``(value, write_version)`` for a setting, or ``(MISSING, 0)``."""
        version = self.preferences.current_version(scope, scope_key, setting_id)
        if version == 0:
            return (_MISSING, 0)
        return (self.preferences.stored_values(scope, scope_key).get(setting_id, _MISSING), version)

    # -- export (pure read; cannot mutate) ----------------------------------

    def export(self, *, actor: str, project_id: str | None = None) -> dict[str, Any]:
        """A CLOSED, versioned, redacted export of the actor's portable settings.

        Ranges ONLY over the portable actor-view ids and includes a setting only
        when the actor actually set it (a stored override) in the personal
        namespace (and the project namespace when a project is named).  A secret,
        credential, token, path, raw URL, chat history, or raw prompt is
        structurally absent — there is no such portable id — and the router scrubs
        the serialized body at the last hop.  Records the schema version, the
        source scope, and a SAFE OPAQUE actor/project reference; never the raw id.
        """
        scopes = ["personal"] + (["project"] if project_id else [])
        entries: list[dict[str, Any]] = []
        for scope in scopes:
            scope_key = self._scope_key(scope, actor, project_id)
            if not scope_key:
                continue
            owned = self.preferences.owned_values(scope, scope_key)
            for setting_id in sorted(owned):
                if setting_id not in self._portable_ids:
                    continue
                entries.append({"setting_id": setting_id, "scope": scope, "value": owned[setting_id]})
        source: dict[str, Any] = {
            "scope": "+".join(scopes),
            "actor_ref": opaque_scope_ref("actor", actor, key=self._audit_key),
            "catalog_id": self._catalog_id,
            "catalog_revision": self._catalog_revision,
        }
        if project_id:
            source["project_ref"] = opaque_scope_ref("project", project_id, key=self._audit_key)
        return {
            "schema_version": CONFIGURATION_EXPORT_SCHEMA_VERSION,
            "source": source,
            "settings": entries,
        }

    # -- import (validate / preview / apply) --------------------------------

    def _classify(
        self, envelope: Mapping[str, Any], *, actor: str, project_id: str | None,
    ) -> dict[str, Any]:
        """The typed preview of an import; the single source both preview and apply use."""
        validated = validate_configuration_envelope(envelope)
        refs = self._refs()
        creates: list[dict[str, Any]] = []
        changes: list[dict[str, Any]] = []
        resets: list[dict[str, Any]] = []
        skipped_read_only: list[dict[str, Any]] = []
        unavailable_references: list[dict[str, Any]] = []
        no_ops: list[dict[str, Any]] = []
        repairable: list[dict[str, Any]] = []
        base_versions: dict[str, int] = {}

        seen: set[str] = set()
        for entry in validated["settings"]:
            setting_id = str(entry["setting_id"])
            value = entry["value"]
            if setting_id in seen:
                repairable.append({"setting_id": setting_id, "reason": "the entry is duplicated in the envelope"})
                continue
            seen.add(setting_id)

            descriptor = self._by_id.get(setting_id)
            if descriptor is None:
                repairable.append({"setting_id": setting_id, "reason": "unknown setting id"})
                continue
            scope = str(descriptor.get("scope"))
            if setting_id not in self._portable_ids:
                # A known but owner-managed (authority/secret/path) setting: it can
                # never be imported. Skipped, but it does NOT invalidate the batch.
                skipped_read_only.append({"setting_id": setting_id, "reason": "owner-managed; not importable"})
                continue
            declared_scope = entry.get("scope")
            if declared_scope is not None and declared_scope != scope:
                repairable.append({"setting_id": setting_id, "reason": "entry scope does not match the setting"})
                continue
            scope_key = self._scope_key(scope, actor, project_id)
            if not scope_key:
                # A project-scoped setting with no project selected cannot be bound
                # to a namespace; skip it rather than mutate the wrong scope.
                skipped_read_only.append({
                    "setting_id": setting_id,
                    "reason": "select a project to import project-scoped settings",
                })
                continue

            try:
                validate_setting_value(descriptor, value)
            except PreferenceValidationError as exc:
                repairable.append({"setting_id": setting_id, "reason": str(exc)})
                continue

            ref_kind = descriptor.get("ref_kind")
            if descriptor.get("type") in ("id_ref", "digest_ref") and ref_kind:
                valid = refs.get(str(ref_kind))
                if valid is not None and value not in valid:
                    # A valid-typed reference that names a route/skill/etc. that is
                    # not currently available: skipped, surfaced distinctly.
                    unavailable_references.append(
                        {"setting_id": setting_id, "ref_kind": str(ref_kind), "value": value}
                    )
                    continue

            current, version = self._current(scope, scope_key, setting_id)
            default = descriptor.get("default", _MISSING)
            has_override = version > 0
            base_versions[setting_id] = version
            if has_override and current == value:
                no_ops.append({"setting_id": setting_id, "scope": scope})
            elif value == default and has_override:
                resets.append({
                    "setting_id": setting_id, "scope": scope,
                    "from": current, "to_default": None if default is _MISSING else default,
                    "expected_version": version,
                })
            elif value == default and not has_override:
                # Already inheriting the default; storing it explicitly changes no
                # effective value, so it is a no-op rather than a create.
                no_ops.append({"setting_id": setting_id, "scope": scope})
            elif not has_override:
                creates.append({
                    "setting_id": setting_id, "scope": scope, "value": value, "expected_version": 0,
                })
            else:
                changes.append({
                    "setting_id": setting_id, "scope": scope,
                    "from": current, "to": value, "expected_version": version,
                })

        return {
            "valid": not repairable,
            "creates": creates,
            "changes": changes,
            "resets": resets,
            "skipped_read_only": skipped_read_only,
            "unavailable_references": unavailable_references,
            "no_ops": no_ops,
            "repairable": repairable,
            "base_versions": {sid: base_versions[sid] for sid in base_versions},
        }

    def import_preview(
        self, *, actor: str, envelope: Mapping[str, Any], project_id: str | None = None,
    ) -> dict[str, Any]:
        """Preview an import as typed categories; mutate NOTHING (T006.2 #2)."""
        return self._classify(envelope, actor=actor, project_id=project_id)

    def import_apply(
        self,
        *,
        actor: str,
        envelope: Mapping[str, Any],
        project_id: str | None = None,
        base_versions: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        """Apply a valid import ATOMICALLY, scope-bound, version-checked, audited.

        Recomputes the typed preview and refuses the WHOLE import if any field is
        repairable (nothing is applied, T006.2 #1).  The creates/changes/resets are
        committed in one :meth:`~workbench.store.MemoryPreferenceStore.apply_batch`,
        so a stale version (against the caller's ``base_versions`` from the preview)
        fails closed and leaves every value untouched.  Skipped/unavailable/no-op
        entries are never applied.  Records one audit entry per committed op.
        """
        plan = self._classify(envelope, actor=actor, project_id=project_id)
        if not plan["valid"]:
            raise ConfigurationTransferError(
                "the import is invalid; repair every flagged field before applying — nothing was applied"
            )
        operations = self._plan_operations(
            plan["creates"] + plan["changes"] + plan["resets"],
            actor=actor, project_id=project_id, base_versions=base_versions,
        )
        results = self.preferences.apply_batch(operations, actor)
        self._record_audit("configuration.import", results)
        applied = self._summarize(results)
        return {
            "applied": applied,
            # The affected scope(s), carried from the applied ops so the result can
            # report scope, result, and remediation (T006.4 #3), as a reset does.
            "scopes": sorted({str(entry["scope"]) for entry in applied}),
            "creates": len(plan["creates"]),
            "changes": len(plan["changes"]),
            "resets": len(plan["resets"]),
            "skipped_read_only": plan["skipped_read_only"],
            "unavailable_references": plan["unavailable_references"],
            "no_ops": plan["no_ops"],
        }

    # -- scoped reset (preview / apply) -------------------------------------

    def reset_preview(
        self, *, actor: str, scope: str, project_id: str | None = None,
    ) -> dict[str, Any]:
        """The exact values + scope a scoped reset will change; mutate NOTHING.

        Lists every portable setting in the selected actor/project namespace that
        currently carries a stored override, with its current value and the
        inherited/default target it will fall back to (T006.3 #1).
        """
        if scope not in _ACTOR_SCOPES:
            raise ConfigurationTransferError("a scoped reset targets the personal or project scope")
        scope_key = self._scope_key(scope, actor, project_id)
        if not scope_key:
            raise ConfigurationTransferError("a project-scope reset requires a project id")
        owned = self.preferences.owned_values(scope, scope_key)
        changes: list[dict[str, Any]] = []
        base_versions: dict[str, int] = {}
        for setting_id in sorted(owned):
            if setting_id not in self._portable_ids:
                continue
            descriptor = self._by_id.get(setting_id, {})
            version = self.preferences.current_version(scope, scope_key, setting_id)
            default = descriptor.get("default", _MISSING)
            changes.append({
                "setting_id": setting_id, "scope": scope,
                "from": owned[setting_id],
                "to_default": None if default is _MISSING else default,
                "expected_version": version,
            })
            base_versions[setting_id] = version
        return {"scope": scope, "changes": changes, "base_versions": base_versions}

    def reset_apply(
        self,
        *,
        actor: str,
        scope: str,
        project_id: str | None = None,
        base_versions: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        """Apply a scoped reset ATOMICALLY, version-checked, audited, isolated.

        Removes every portable stored override in the selected namespace in one
        :meth:`~workbench.store.MemoryPreferenceStore.apply_batch`, so a stale
        version fails closed and nothing is removed.  Because every op is bound to
        exactly ``(scope, scope_key)``, another actor's namespace, project/system
        policy, and deployment configuration are untouched (T006.3 #3).
        """
        plan = self.reset_preview(actor=actor, scope=scope, project_id=project_id)
        operations = self._plan_operations(
            [{**change, "op": "reset"} for change in plan["changes"]],
            actor=actor, project_id=project_id, base_versions=base_versions,
            default_scope=scope,
        )
        results = self.preferences.apply_batch(operations, actor)
        self._record_audit("configuration.reset", results)
        return {"scope": scope, "applied": self._summarize(results)}

    # -- internal op building + audit ---------------------------------------

    def _plan_operations(
        self,
        planned: list[Mapping[str, Any]],
        *,
        actor: str,
        project_id: str | None,
        base_versions: Mapping[str, int] | None,
        default_scope: str | None = None,
    ) -> list[dict[str, Any]]:
        operations: list[dict[str, Any]] = []
        for item in planned:
            setting_id = str(item["setting_id"])
            scope = str(item.get("scope") or default_scope)
            scope_key = self._scope_key(scope, actor, project_id)
            # Prefer the caller's echoed base version (the preview snapshot) so a
            # store that moved since preview fails closed; else the freshly-observed
            # version the plan carries.
            if base_versions is not None and setting_id in base_versions:
                expected = base_versions[setting_id]
            else:
                expected = item.get("expected_version", 0)
            op_kind = "reset" if ("op" in item and item["op"] == "reset") or "to_default" in item else "set"
            operation = {
                "scope": scope, "scope_key": scope_key, "setting_id": setting_id,
                "op": op_kind, "expected_version": expected,
            }
            if op_kind == "set":
                operation["value"] = item.get("value") if "value" in item else item.get("to")
            operations.append(operation)
        return operations

    def _record_audit(self, action: str, results: list[Mapping[str, Any]]) -> None:
        recorded_at = now_utc().isoformat()
        for result in results:
            self._audit_records.append({
                "action": action,
                "setting_id": result["setting_id"],
                "scope": result["scope"],
                "op": result["op"],
                "scope_key_fingerprint": preference_scope_key_fingerprint(
                    result["scope"], result["scope_key"], key=self._audit_key,
                ),
                "recorded_at": recorded_at,
            })

    @staticmethod
    def _summarize(results: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [
            {"setting_id": r["setting_id"], "scope": r["scope"], "op": r["op"]}
            for r in results
        ]

    def audit_records(self) -> list[dict[str, Any]]:
        """The non-identifying audit trail of applied imports/resets (browser-safe)."""
        return [dict(record) for record in self._audit_records]


class _Missing:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "<MISSING>"


#: A sentinel distinguishing "no stored value" from a stored ``None``.
_MISSING = _Missing()
