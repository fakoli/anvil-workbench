"""Non-secret plugin preference field resolution (reviewed-tools-plugins T011).

Extracted verbatim from ``workbench.store``; re-exported there for backward
compatibility.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Mapping

from .contracts import (
    ContractValidationError,
    plugin_preference_actor_view as _plugin_pref_actor_view,
    validate_plugin_catalog as _validate_plugin_catalog_for_prefs,
    validate_plugin_preference_value as _validate_plugin_pref_value,
)
from .store_base import StoreError


# ---------------------------------------------------------------------------
# Non-secret plugin preference field resolution (reviewed-tools-plugins T011)
# ---------------------------------------------------------------------------
#: The standard actor-selectable precedence order for a plugin preference field:
#: a per-turn override wins, then the actor's own value, then the project value,
#: then the field's safe default.
_PLUGIN_PREF_PRECEDENCE = ("per_turn", "actor", "project")


def resolve_plugin_tool_preferences(
    fields,
    *,
    per_turn: Mapping[str, Any] | None = None,
    actor: Mapping[str, Any] | None = None,
    project: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve a tool's preference fields through the STANDARD precedence (T011).

    Precedence is ``per_turn -> actor -> project -> safe default``.  Only DECLARED
    fields are resolved (an unknown stored key is ignored -- a caller cannot mint
    a new field), and each candidate value is typed-checked against its field
    descriptor; an invalid stored value falls back to the safe default rather than
    reaching dispatch.  The returned mapping is the ONLY thing a dispatch uses --
    there is no other channel by which a browser-supplied value could ride in.

    NOTE (T011): the actual dispatch-side CONSUMPTION of these resolved
    preferences awaits the operation-layer integration (a bridge/operation that
    reads the resolved mapping).  Today this resolver is the ONLY channel that
    produces them, so its typed-check + safe-default fallback is the single place
    an actor value is validated before it could reach an effect; a secret-shaped
    value can never reach here because :func:`workbench.contracts.validate_plugin_catalog`
    already refuses a secret name/default AND a secret-shaped ``allowed_values``
    option at review time.
    """
    scopes = {
        "per_turn": dict(per_turn or {}),
        "actor": dict(actor or {}),
        "project": dict(project or {}),
    }
    resolved: dict[str, Any] = {}
    for field_descriptor in fields or []:
        if not isinstance(field_descriptor, Mapping):
            continue
        name = str(field_descriptor.get("name"))
        value = field_descriptor.get("default")
        for scope in _PLUGIN_PREF_PRECEDENCE:
            if name in scopes[scope]:
                candidate = scopes[scope][name]
                try:
                    _validate_plugin_pref_value(field_descriptor, candidate)
                except ContractValidationError:
                    # An invalid stored value never reaches dispatch: fall back to
                    # the safe default (keep scanning lower-precedence scopes).
                    continue
                value = candidate
                break
        resolved[name] = value
    return resolved


class PluginPreferenceStoreError(StoreError):
    """A plugin preference operation violated its scoping or descriptor contract."""


@dataclass
class PluginPreferenceRows:
    """Per-(scope, scope_key) stored plugin preference values.

    ``values`` maps ``(scope, scope_key) -> {(plugin_id, tool_id, field_name) -> value}``.
    The ``(scope, scope_key)`` namespace is the hard isolation boundary, mirroring
    :class:`PreferenceRows`: ``(actor, alice)`` and ``(project, proj_1)`` are
    disjoint namespaces, so a cross-actor read is structurally impossible.
    """

    values: dict[tuple[str, str], dict[tuple[str, str, str], Any]] = field(default_factory=dict)


class MemoryPluginPreferenceService:
    """Resolve a tool's actor-selectable preferences for the hub/browser (T011).

    Holds the reviewed plugin catalog (fail-closed validated at construction) and
    the per-scope stored values.  ``effective`` returns the actor-view field
    descriptors (never a secret one -- the catalog validator refused those) and
    the resolved effective values for one actor, resolved through the standard
    ``per_turn -> actor -> project -> default`` precedence.  A connector-host
    configuration value is never accepted from nor returned to a browser: only the
    declared NON-SECRET fields exist, and ``set_value`` refuses any value that is
    not a declared actor-selectable field.
    """

    def __init__(self, catalog: Mapping[str, Any], rows: PluginPreferenceRows | None = None) -> None:
        _validate_plugin_catalog_for_prefs(catalog)
        self._lock = threading.RLock()
        self._catalog = catalog
        self.rows = rows if rows is not None else PluginPreferenceRows()
        self._tools: dict[tuple[str, str], Mapping[str, Any]] = {}
        for plugin in catalog.get("plugins", []):
            for tool in plugin.get("tools", []) if isinstance(plugin, Mapping) else []:
                if isinstance(tool, Mapping):
                    self._tools[(str(plugin["id"]), str(tool["tool_id"]))] = tool

    def _tool(self, plugin_id: str, tool_id: str) -> Mapping[str, Any]:
        tool = self._tools.get((str(plugin_id), str(tool_id)))
        if tool is None:
            raise PluginPreferenceStoreError("unknown plugin tool")
        return tool

    def _field(self, tool: Mapping[str, Any], name: str) -> Mapping[str, Any]:
        for field_descriptor in tool.get("preference_fields", []) or []:
            if isinstance(field_descriptor, Mapping) and str(field_descriptor.get("name")) == str(name):
                return field_descriptor
        raise PluginPreferenceStoreError("unknown preference field")

    def set_value(
        self, scope: str, scope_key: str, plugin_id: str, tool_id: str, name: str, value: Any,
    ) -> None:
        """Store one actor/project preference value; refuse anything undeclared.

        A value is accepted only for a DECLARED actor-selectable field at the
        field's own scope (or a broader actor/project scope), and only when it
        typed-validates -- so a browser cannot smuggle a connector-host config
        value in under a made-up field name.
        """
        if scope not in ("actor", "project"):
            raise PluginPreferenceStoreError("a plugin preference value is stored only at actor or project scope")
        tool = self._tool(plugin_id, tool_id)
        descriptor = self._field(tool, name)
        try:
            _validate_plugin_pref_value(descriptor, value)
        except ContractValidationError as exc:
            raise PluginPreferenceStoreError(f"invalid plugin preference value: {exc}") from exc
        with self._lock:
            bucket = self.rows.values.setdefault((scope, str(scope_key)), {})
            bucket[(str(plugin_id), str(tool_id), str(name))] = value

    def _stored(self, scope: str, scope_key: str, plugin_id: str, tool_id: str) -> dict[str, Any]:
        bucket = self.rows.values.get((scope, str(scope_key)), {})
        return {
            fname: value
            for (pid, tid, fname), value in bucket.items()
            if pid == str(plugin_id) and tid == str(tool_id)
        }

    def effective(
        self,
        plugin_id: str,
        tool_id: str,
        *,
        actor: str,
        project_id: str | None = None,
        per_turn: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return ``{fields: actor-view, effective: {name: resolved value}}``.

        The actor's own namespace is keyed by ``actor``; the project namespace by
        ``project_id``.  ``per_turn`` is a runtime override supplied at resolution
        time (never a stored, browser-round-tripped connector-host config).
        """
        tool = self._tool(plugin_id, tool_id)
        with self._lock:
            actor_values = self._stored("actor", actor, plugin_id, tool_id)
            project_values = self._stored("project", project_id, plugin_id, tool_id) if project_id else {}
        resolved = resolve_plugin_tool_preferences(
            tool.get("preference_fields", []) or [],
            per_turn=per_turn, actor=actor_values, project=project_values,
        )
        return {"fields": _plugin_pref_actor_view(tool), "effective": resolved}
