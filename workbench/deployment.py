"""Explicit live-deployment composition for the Workbench hub.

``workbench.api.create_app`` defaults every injectable supervision surface to
``None`` so an imported app is hermetic: without an explicit operator decision
each of those browser surfaces fails closed with 503.  That default is
deliberate and is NOT changed here.

This module is the ONE place where a live deployment opts those surfaces in, as
an explicit, env-driven operator decision.  A single master switch,
``WORKBENCH_LIVE_SURFACES`` (a comma-separated list of surface names), selects
which injectable surfaces are constructed for real and passed to
``create_app``.  An empty/unset switch reproduces today's behavior exactly --
every injectable surface stays ``None`` and keeps returning 503.

Fail-closed discipline (an adversarial reviewer will check each):

* An UNKNOWN surface name in ``WORKBENCH_LIVE_SURFACES`` raises at startup --
  never a silent skip that would leave the operator believing a surface is live.
* A requested surface whose reviewed dependency is missing or malformed (the
  settings catalog, the preference audit key, the reviewed plugin catalog, or
  chat persistence) raises at startup rather than serving a partial or unkeyed
  surface.
* Each surface is constructed through its OWN reviewed constructor with the
  reviewed dependency, so the wired HTTP path is the real contract -- not a
  hand-built stand-in.

Surfaces this module deliberately does NOT wire (they have separate live gates,
left unchanged): ``project_context_store``, ``run_context_store``,
``delivery_projection_store``, ``voice_relay_service``,
``chat_tool_dispatch_service``, and ``plugin_host_service`` beyond its existing
settings-driven path in ``create_app``.
"""
from __future__ import annotations

import json
import os
from typing import Any, Mapping

from fastapi import FastAPI

from .advanced_playground import (
    AdvancedPresetStore,
    AdvancedRatingStore,
    AdvancedTemplateStore,
)
from .api import _conversation_store, create_app
from .config import Settings
from .configuration_transfer import ConfigurationTransferService
from .contracts import ContractValidationError, validate_settings_descriptor
from .conversation_store import ConversationSearchService, ConversationStore
from .conversation_transfer import ConversationTransferService
from .models import MIN_PREF_AUDIT_KEY_BYTES
from .preference_gates import PolicyGateService
from .store import (
    MemoryPluginPreferenceService,
    MemoryPreferenceStore,
    MemorySkillAdoptionStore,
)

#: Operator-declared path to the reviewed, digest-pinned settings-descriptor
#: catalog.  Required by every catalog-backed surface (the preference store, the
#: policy-operation gate, and the configuration-transfer service).
SETTINGS_CATALOG_FILE_ENV = "WORKBENCH_SETTINGS_CATALOG_FILE"
#: Operator-held HMAC key (>=16 octets, UTF-8) for the redacted preference audit
#: fingerprint.  Required by every audit-keyed surface (configuration transfer,
#: conversation transfer, and the three advanced-playground stores).
PREF_AUDIT_KEY_ENV = "WORKBENCH_PREF_AUDIT_KEY"
#: The master switch: a comma-separated list of injectable surface names to wire.
LIVE_SURFACES_ENV = "WORKBENCH_LIVE_SURFACES"

#: The exact injectable-surface names ``WORKBENCH_LIVE_SURFACES`` may name.  A
#: name outside this set fails closed at startup.
LIVE_SURFACE_NAMES: frozenset[str] = frozenset({
    "preference_store",
    "policy_gate_service",
    "configuration_transfer_service",
    "conversation_transfer_service",
    "advanced_preset_store",
    "advanced_template_store",
    "advanced_rating_store",
    "plugin_preference_service",
    "conversation_search_service",
    "skill_adoption_store",
})


class DeploymentConfigError(RuntimeError):
    """A live-surface deployment cannot be trusted; fail closed at startup."""


def parse_live_surfaces(raw: str) -> tuple[str, ...]:
    """Parse ``WORKBENCH_LIVE_SURFACES`` into an ordered, de-duplicated tuple.

    An empty/unset value selects nothing (today's all-``None`` behavior).  An
    unknown surface name fails closed -- a typo can never silently leave a
    surface unwired while the operator believes it is live.
    """
    names: list[str] = []
    for item in raw.split(","):
        name = item.strip()
        if not name:
            continue
        if name not in LIVE_SURFACE_NAMES:
            allowed = ", ".join(sorted(LIVE_SURFACE_NAMES))
            raise DeploymentConfigError(
                f"WORKBENCH_LIVE_SURFACES names an unknown surface {name!r}; "
                f"allowed surfaces are: {allowed}"
            )
        if name not in names:
            names.append(name)
    return tuple(names)


def _load_settings_catalog(env: Mapping[str, str]) -> Mapping[str, Any]:
    """Load and fail-closed validate the reviewed settings-descriptor catalog.

    A catalog-backed surface must never run against an undeclared, unreadable,
    malformed, or digest-tampered catalog, so every one of those raises here
    rather than serving a surface over an untrusted catalog.
    """
    path = env.get(SETTINGS_CATALOG_FILE_ENV, "").strip()
    if not path:
        raise DeploymentConfigError(
            "a catalog-backed live surface (preference_store / policy_gate_service / "
            "configuration_transfer_service) requires a reviewed settings catalog; "
            f"set {SETTINGS_CATALOG_FILE_ENV} to its local path"
        )
    try:
        document = json.loads(_read_file(path))
    except FileNotFoundError as exc:
        raise DeploymentConfigError(
            f"{SETTINGS_CATALOG_FILE_ENV} points at a missing file: {path}"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise DeploymentConfigError(
            f"{SETTINGS_CATALOG_FILE_ENV} could not be read as a reviewed JSON catalog"
        ) from exc
    if not isinstance(document, Mapping):
        raise DeploymentConfigError(
            f"{SETTINGS_CATALOG_FILE_ENV} must contain a settings-descriptor object"
        )
    try:
        validate_settings_descriptor(document)
    except ContractValidationError as exc:
        raise DeploymentConfigError(
            f"the reviewed settings catalog fails contract validation: {exc}"
        ) from exc
    return document


def _load_plugin_catalog(env: Mapping[str, str]) -> Mapping[str, Any]:
    """Load the reviewed plugin catalog for the non-secret preference service.

    Reuses the SAME operator-declared trust-root path the plugin host loads
    (``WORKBENCH_PLUGIN_CATALOG_FILE``).  The service's own constructor
    fail-closed validates the catalog; a missing/unreadable/malformed file fails
    closed here first with a precise message.
    """
    path = env.get("WORKBENCH_PLUGIN_CATALOG_FILE", "").strip()
    if not path:
        raise DeploymentConfigError(
            "plugin_preference_service requires the reviewed plugin catalog; set "
            "WORKBENCH_PLUGIN_CATALOG_FILE to its local path"
        )
    try:
        document = json.loads(_read_file(path))
    except FileNotFoundError as exc:
        raise DeploymentConfigError(
            f"WORKBENCH_PLUGIN_CATALOG_FILE points at a missing file: {path}"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise DeploymentConfigError(
            "WORKBENCH_PLUGIN_CATALOG_FILE could not be read as a reviewed JSON catalog"
        ) from exc
    if not isinstance(document, Mapping):
        raise DeploymentConfigError(
            "WORKBENCH_PLUGIN_CATALOG_FILE must contain a plugin-catalog object"
        )
    return document


def _pref_audit_key(env: Mapping[str, str]) -> bytes:
    """Resolve the operator-held preference audit key, fail closed if unusable."""
    raw = env.get(PREF_AUDIT_KEY_ENV, "")
    key = raw.encode("utf-8")
    if len(key) < MIN_PREF_AUDIT_KEY_BYTES:
        raise DeploymentConfigError(
            "an audit-keyed live surface (configuration_transfer_service / "
            "conversation_transfer_service / advanced_*_store) requires a preference "
            f"audit key of at least {MIN_PREF_AUDIT_KEY_BYTES} octets; set {PREF_AUDIT_KEY_ENV}"
        )
    return key


def _read_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def build_live_overrides(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Construct the injectable ``create_app`` overrides from the env opt-ins.

    Returns ONLY the keyword arguments for the surfaces the operator explicitly
    opted into; every other injectable surface is left absent so ``create_app``
    keeps it ``None`` (503).  Shared dependencies (the settings catalog, the
    preference store, the conversation store, the audit key) are built once and
    reused so, e.g., ``conversation_search_service`` wraps the same conversation
    store the chat endpoints use and ``configuration_transfer_service`` operates
    over the same preference store the ``/api/preferences`` surface serves.
    """
    resolved_env = dict(os.environ if env is None else env)
    settings = Settings.from_env(resolved_env)
    requested = frozenset(parse_live_surfaces(resolved_env.get(LIVE_SURFACES_ENV, "")))

    overrides: dict[str, Any] = {}
    if not requested:
        return overrides

    # -- shared dependencies, built at most once ---------------------------
    _catalog_cache: dict[str, Mapping[str, Any]] = {}

    def settings_catalog() -> Mapping[str, Any]:
        if "settings" not in _catalog_cache:
            _catalog_cache["settings"] = _load_settings_catalog(resolved_env)
        return _catalog_cache["settings"]

    _key_cache: dict[str, bytes] = {}

    def audit_key() -> bytes:
        if "key" not in _key_cache:
            _key_cache["key"] = _pref_audit_key(resolved_env)
        return _key_cache["key"]

    # A single preference store is shared between the ``/api/preferences`` surface
    # and the configuration-transfer service so an export/import reflects exactly
    # what the preference surface serves.
    _pref_cache: dict[str, MemoryPreferenceStore] = {}

    def preference_store() -> MemoryPreferenceStore:
        if "store" not in _pref_cache:
            _pref_cache["store"] = MemoryPreferenceStore(settings_catalog())
        return _pref_cache["store"]

    # A single conversation store is shared with ``create_app`` so the search and
    # transfer surfaces wrap the SAME actor-scoped store the chat endpoints use.
    _conv_cache: dict[str, ConversationStore] = {}

    def conversation_store() -> ConversationStore:
        if "store" not in _conv_cache:
            store = _conversation_store(settings)
            if store is None:
                raise DeploymentConfigError(
                    "conversation_search_service / conversation_transfer_service require "
                    "chat persistence; set WORKBENCH_CHAT_HASH_KEY to enable the "
                    "conversation store"
                )
            _conv_cache["store"] = store
        return _conv_cache["store"]

    # -- per-surface construction ------------------------------------------
    if "preference_store" in requested:
        overrides["preference_store"] = preference_store()

    if "policy_gate_service" in requested:
        # The gate is constructed over the reviewed settings catalog only: its
        # operations are the hub-local, observational preference set/reset spine
        # (no external production declarations), so a browser can never route a
        # production effect through it.
        overrides["policy_gate_service"] = PolicyGateService(settings_catalog())

    if "configuration_transfer_service" in requested:
        overrides["configuration_transfer_service"] = ConfigurationTransferService(
            settings_catalog(), preference_store(), audit_key=audit_key(),
        )

    if "conversation_transfer_service" in requested:
        overrides["conversation_transfer_service"] = ConversationTransferService(
            conversation_store(), audit_key=audit_key(),
        )

    if "advanced_preset_store" in requested:
        overrides["advanced_preset_store"] = AdvancedPresetStore(audit_key=audit_key())
    if "advanced_template_store" in requested:
        overrides["advanced_template_store"] = AdvancedTemplateStore(audit_key=audit_key())
    if "advanced_rating_store" in requested:
        overrides["advanced_rating_store"] = AdvancedRatingStore(audit_key=audit_key())

    if "plugin_preference_service" in requested:
        overrides["plugin_preference_service"] = MemoryPluginPreferenceService(
            _load_plugin_catalog(resolved_env),
        )

    if "conversation_search_service" in requested:
        overrides["conversation_search_service"] = ConversationSearchService(
            conversation_store(),
        )

    if "skill_adoption_store" in requested:
        overrides["skill_adoption_store"] = MemorySkillAdoptionStore()

    # When a shared conversation store was built, hand the SAME instance to
    # create_app so the chat endpoints and the wrapping surfaces agree.
    if "store" in _conv_cache:
        overrides["conversation_store"] = _conv_cache["store"]

    return overrides


def create_live_app(env: Mapping[str, str] | None = None) -> FastAPI:
    """Build the FastAPI app with the operator-opted-in live surfaces wired.

    This is the deployment entrypoint's single composition seam.  With an
    empty/unset ``WORKBENCH_LIVE_SURFACES`` it is byte-for-byte the hermetic
    ``create_app()`` default (every injectable surface ``None`` -> 503).
    """
    resolved_env = dict(os.environ if env is None else env)
    settings = Settings.from_env(resolved_env)
    overrides = build_live_overrides(resolved_env)
    return create_app(settings=settings, **overrides)
