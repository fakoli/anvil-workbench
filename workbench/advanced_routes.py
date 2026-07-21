"""Discover and validate Advanced-mode route capabilities and controls (AMP T002).

Advanced mode lets an operator tune a per-branch route: pick a reviewed Anvil
Serving route, adjust its declared-supported controls, and fork an experiment
turn.  Anvil Serving owns model policy and is the only managed model path; the
AGENTS.md boundary is explicit -- never add a raw-provider fallback and never
let the browser learn an endpoint, token, or credential.  This module is the
hub-side gate that makes the Advanced surface honor that boundary *before* any
Serving request exists, mirroring :mod:`workbench.chat_routes` for the richer
``advanced-branch.v1`` ``route_capability`` shape (a control declares its type,
bounds/allowed values, default, and a ``policy_owned`` flag).

The four T002 acceptance criteria this module binds:

* **Reject an unsupported, out-of-bounds, policy-owned, stale, or unknown
  control before a Serving request.**  :func:`validate_advanced_selection`
  refuses an unknown ``route_id``, a control the route does not declare, a value
  outside its declared type/bounds/allowed set, and a crafted override of a
  ``policy_owned`` control -- all with a typed :attr:`AdvancedRouteError.reason`
  so a refusal is asserted on its claimed cause, and all with no I/O so the
  refusal happens strictly before a Serving request could be issued.  It takes
  only the module's own frozen discovery snapshot (a caller-assembled mapping is
  refused by type), so no browser- or model-supplied structure can widen the
  allowlist.
* **Serving credentials and raw operational endpoints never reach the browser.**
  The closed config schema (:data:`_ALLOWED_CONFIG_KEYS`) has no endpoint, URL,
  token, credential, or policy-internal key -- an undeclared key refuses the
  whole discovery -- and the browser projection carries identifiers, digests,
  and declared control metadata only.  :func:`browser_projection` additionally
  runs the config-text last-hop scrub so even a mis-declared display string can
  never carry a secret, endpoint, or path out to the browser.
* **Catalog drift invalidates the request/preset rather than silently changing
  values.**  :func:`route_capability_repair` deterministically compares a
  branch/preset's pinned route/profile digests to the live discovered route and
  returns ``repair_required`` (never a substituted route), and
  :func:`validate_advanced_selection` fails closed with a ``*_digest_drift``
  reason when a caller pins a digest the live catalog no longer matches.
* **Browser metadata identifies the effective source and disabled reason without
  exposing hidden policy fields.**  :meth:`AdvancedControlDescriptor.control_view`
  reports whether a control is editable, its safe ``source`` token, and a safe
  ``disabled_reason`` for a policy-owned control -- never a hidden policy field.

Like the sibling discovery slices this is implemented and hermetically tested;
wiring a browser endpoint over the projection is a separate step and is not
wired into the live bridge loop here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .redaction import scrub_config_payload

SERVING_PROVIDER = "anvil-serving"

#: Identifier grammars, taken verbatim from ``advanced-branch.v1`` route_capability.
_ROUTE_ID = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")
_MODEL_PROFILE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_CONTRACT_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_CONTROL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

MAX_DISPLAY_NAME_CHARS = 120
MAX_SUPPORTED_CONTROLS = 32
MAX_ALLOWED_VALUES = 32
MAX_ALLOWED_VALUE_CHARS = 64

#: A display name is human-readable text only, guarded the same three ways as a
#: chat route display name: an allowlist charset that structurally forbids URL
#: and path punctuation (colon, slash, at-sign) so no endpoint can be expressed;
#: a semantic word denylist for secret vocabulary; and a per-token check refusing
#: secret-prefixed tokens, dotted host-like names, and high-entropy runs.
_DISPLAY_NAME_CHARSET = re.compile(r"^[\w .,()&+'\-]{1,120}$")
_FORBIDDEN_DISPLAY_WORDS = ("token", "secret", "bearer", "credential", "password", "api_key", "apikey")
_SECRET_PREFIXES = ("sk-", "sk_", "pk-", "pk_", "ghp_", "gho_", "xox", "aws_", "akia")
_CREDENTIAL_SHAPED_TOKEN = re.compile(r"^(?=.*[0-9])(?=.*[A-Za-z])[A-Za-z0-9_-]{20,}$")

_CONTROL_TYPES = frozenset({"int", "enum", "bool"})
_CONTROL_PROVENANCE = frozenset({"declared", "observed", "policy_override"})

#: The closed configuration schema for one route: exactly these keys, nothing
#: else.  An endpoint-, URL-, token-, or policy-internal-shaped key is undeclared
#: and refuses the whole discovery.
_REQUIRED_CONFIG_KEYS = frozenset({
    "route_id", "serving_contract_version", "route_digest", "profile_digest",
    "model_profile", "supported_controls",
})
_OPTIONAL_CONFIG_KEYS = frozenset({
    "provider", "display_name", "structured_output_supported", "tools_supported",
})
_ALLOWED_CONFIG_KEYS = _REQUIRED_CONFIG_KEYS | _OPTIONAL_CONFIG_KEYS

#: The closed per-control descriptor config schema.
_ALLOWED_CONTROL_KEYS = frozenset({
    "name", "type", "default", "bounds", "allowed_values", "policy_owned",
})

# --- Typed refusal reason codes (asserted on by callers/tests). -------------
REASON_ROUTE_UNKNOWN = "route_unknown"
REASON_CONTROL_UNSUPPORTED = "control_unsupported"
REASON_CONTROL_TYPE = "control_type"
REASON_CONTROL_OUT_OF_BOUNDS = "control_out_of_bounds"
REASON_CONTROL_NOT_ALLOWED = "control_not_allowed"
REASON_POLICY_OWNED_READONLY = "policy_owned_readonly"
REASON_ROUTE_DIGEST_DRIFT = "route_digest_drift"
REASON_PROFILE_DIGEST_DRIFT = "profile_digest_drift"
REASON_MALFORMED = "malformed_selection"


class AdvancedRouteError(RuntimeError):
    """An Advanced route configuration or control selection cannot be trusted.

    Carries a typed :attr:`reason` code so a refusal is asserted on its claimed
    cause, never merely on message text.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class AdvancedControlDescriptor:
    """One declared-supported tunable in its browser-safe projection.

    Mirrors ``advanced-branch.v1`` ``controlDescriptor``: a control declares its
    type, its bounds (int) or allowed values (enum), a default, and whether it is
    policy-owned (read-only).  There is no free-form field able to carry a hidden
    policy value or an endpoint.
    """

    name: str
    type: str
    default: Any
    bounds: tuple[int, int] | None = None
    allowed_values: tuple[str, ...] | None = None
    policy_owned: bool = False

    @property
    def editable(self) -> bool:
        """A policy-owned control is read-only; everything else is actor-editable."""
        return not self.policy_owned

    def check_value(self, value: Any) -> None:
        """Refuse a value that violates this control's declared type/bounds.

        Raises :class:`AdvancedRouteError` with the precise typed reason.  This
        runs before any Serving request, so an out-of-type/out-of-bounds value is
        never issued.  Mirrors ``contracts._check_advanced_control_value`` but
        attaches the typed reason the Advanced surface refuses on.
        """
        if self.type == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise AdvancedRouteError(
                    f"control {self.name} must be an integer: {value!r}", reason=REASON_CONTROL_TYPE
                )
            assert self.bounds is not None  # int always carries bounds by construction
            low, high = self.bounds
            if not low <= value <= high:
                raise AdvancedRouteError(
                    f"control {self.name} is outside its declared bounds [{low}, {high}]: {value!r}",
                    reason=REASON_CONTROL_OUT_OF_BOUNDS,
                )
        elif self.type == "enum":
            assert self.allowed_values is not None
            if not isinstance(value, str) or value not in self.allowed_values:
                raise AdvancedRouteError(
                    f"control {self.name} is not one of its declared allowed values: {value!r}",
                    reason=REASON_CONTROL_NOT_ALLOWED,
                )
        elif self.type == "bool":
            if not isinstance(value, bool):
                raise AdvancedRouteError(
                    f"control {self.name} must be a boolean: {value!r}", reason=REASON_CONTROL_TYPE
                )
        else:  # pragma: no cover - construction pins the type enum
            raise AdvancedRouteError(
                f"control {self.name} declares an unsupported type: {self.type!r}", reason=REASON_MALFORMED
            )

    def as_descriptor(self) -> dict[str, Any]:
        """The ``advanced-branch.v1`` ``controlDescriptor`` shape (for a branch)."""
        descriptor: dict[str, Any] = {"name": self.name, "type": self.type, "default": self.default}
        if self.bounds is not None:
            descriptor["bounds"] = {"min": self.bounds[0], "max": self.bounds[1]}
        if self.allowed_values is not None:
            descriptor["allowed_values"] = list(self.allowed_values)
        if self.policy_owned:
            descriptor["policy_owned"] = True
        return descriptor

    def control_view(self) -> dict[str, Any]:
        """Browser-safe control metadata: effective source + disabled reason.

        Carries the type, default, bounds/allowed values, and -- for a
        policy-owned control -- a truthful ``editable=false`` with a safe
        ``disabled_reason`` token.  Never a hidden policy field; the source token
        is a fixed label, not an internal policy value.
        """
        view: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "default": self.default,
            "editable": self.editable,
            "source": "policy_owned" if self.policy_owned else "route_default",
            "disabled_reason": "policy_owned" if self.policy_owned else None,
        }
        if self.bounds is not None:
            view["bounds"] = {"min": self.bounds[0], "max": self.bounds[1]}
        if self.allowed_values is not None:
            view["allowed_values"] = list(self.allowed_values)
        return view


@dataclass(frozen=True)
class AdvancedRouteCapability:
    """One reviewed Advanced route capability in its browser-safe projection.

    Identifiers, digests, and declared control metadata only -- exactly the
    ``advanced-branch.v1`` ``route_capability`` shape.  There is no endpoint,
    URL, token, credential, or policy-internal field; resolving ``route_id``
    against a live endpoint stays inside the configured Anvil Serving client.
    """

    route_id: str
    display_name: str
    route_digest: str
    profile_digest: str
    serving_contract_version: str
    model_profile: str
    supported_controls: tuple[AdvancedControlDescriptor, ...]
    structured_output_supported: bool = False
    tools_supported: bool = False

    @property
    def provider(self) -> str:
        return SERVING_PROVIDER

    def control(self, name: str) -> AdvancedControlDescriptor:
        for descriptor in self.supported_controls:
            if descriptor.name == name:
                return descriptor
        raise AdvancedRouteError(
            f"route {self.route_id} does not declare control {name!r}", reason=REASON_CONTROL_UNSUPPORTED
        )

    def as_route_capability(self) -> dict[str, Any]:
        """The ``advanced-branch.v1`` ``route_capability`` object (for a branch)."""
        return {
            "provider": self.provider,
            "route_id": self.route_id,
            "route_digest": self.route_digest,
            "profile_digest": self.profile_digest,
            "serving_contract_version": self.serving_contract_version,
            "model_profile": self.model_profile,
            "structured_output_supported": self.structured_output_supported,
            "tools_supported": self.tools_supported,
            "supported_controls": [descriptor.as_descriptor() for descriptor in self.supported_controls],
        }

    def browser_projection(self) -> dict[str, Any]:
        """The scrubbed, browser-safe route metadata: no endpoint, token, or path.

        Runs the config-text last-hop scrub over the whole projection so even a
        mis-declared display string can never emit a secret, endpoint, or path.
        Digests survive the scrub (delimiter-anchored patterns leave a
        ``sha256:...`` intact).
        """
        payload = {
            "provider": self.provider,
            "route_id": self.route_id,
            "display_name": self.display_name,
            "route_digest": self.route_digest,
            "profile_digest": self.profile_digest,
            "serving_contract_version": self.serving_contract_version,
            "model_profile": self.model_profile,
            "structured_output_supported": self.structured_output_supported,
            "tools_supported": self.tools_supported,
            "controls": [descriptor.control_view() for descriptor in self.supported_controls],
        }
        return scrub_config_payload(payload)


@dataclass(frozen=True)
class DiscoveredAdvancedRoutes:
    """The frozen snapshot of every reviewed Advanced route, in configured order."""

    routes: tuple[AdvancedRouteCapability, ...]

    @property
    def route_ids(self) -> tuple[str, ...]:
        return tuple(route.route_id for route in self.routes)

    def route(self, route_id: str) -> AdvancedRouteCapability:
        for route in self.routes:
            if route.route_id == route_id:
                return route
        raise AdvancedRouteError(
            f"advanced route is not in the reviewed allowlist: {route_id!r}", reason=REASON_ROUTE_UNKNOWN
        )

    def browser_projection(self) -> dict[str, Any]:
        return {"routes": [route.browser_projection() for route in self.routes]}


@dataclass(frozen=True)
class AdvancedRouteSelection:
    """One validated selection: a pinned route plus its accepted controls.

    ``controls`` carries ``(name, value, provenance)`` triples in sorted order so
    the selection is canonical; a policy-owned control is present only with its
    declared default and ``policy_override`` provenance.
    """

    route: AdvancedRouteCapability
    controls: tuple[tuple[str, Any, str], ...]

    def controls_dict(self) -> dict[str, Any]:
        return {name: value for name, value, _ in self.controls}

    def submitted_controls(self) -> list[dict[str, Any]]:
        """The ``advanced-branch.v1`` ``submitted_controls`` array for this selection."""
        return [
            {"name": name, "value": value, "provenance": provenance}
            for name, value, provenance in self.controls
        ]


# --- Discovery ---------------------------------------------------------------


def _identifier(entry: Mapping[str, Any], key: str, pattern: re.Pattern[str]) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise AdvancedRouteError(
            f"advanced route declares an invalid {key}: {value!r}", reason=REASON_MALFORMED
        )
    return value


def _token_is_credential_shaped(token: str) -> bool:
    lowered = token.lower()
    if any(lowered.startswith(prefix) for prefix in _SECRET_PREFIXES):
        return True
    segments = [part for part in token.split(".") if part]
    if len(segments) >= 3 and all(seg.isalnum() for seg in segments):
        return True
    return bool(_CREDENTIAL_SHAPED_TOKEN.match(token))


def _display_name(entry: Mapping[str, Any], route_id: str) -> str:
    value = entry.get("display_name", route_id)
    if not isinstance(value, str) or not value.strip() or not _DISPLAY_NAME_CHARSET.match(value):
        raise AdvancedRouteError(
            f"advanced route {route_id} declares an invalid display_name", reason=REASON_MALFORMED
        )
    lowered = value.lower()
    for word in _FORBIDDEN_DISPLAY_WORDS:
        if word in lowered:
            raise AdvancedRouteError(
                f"advanced route {route_id} display_name carries forbidden material ({word!r})",
                reason=REASON_MALFORMED,
            )
    for token in value.split():
        if _token_is_credential_shaped(token):
            raise AdvancedRouteError(
                f"advanced route {route_id} display_name carries credential- or host-shaped material",
                reason=REASON_MALFORMED,
            )
    return value.strip()


def _bool(entry: Mapping[str, Any], key: str) -> bool:
    value = entry.get(key, False)
    if not isinstance(value, bool):
        raise AdvancedRouteError(f"advanced route {key} must be a boolean", reason=REASON_MALFORMED)
    return value


def _control_descriptor(raw: Any, route_id: str) -> AdvancedControlDescriptor:
    if not isinstance(raw, Mapping):
        raise AdvancedRouteError(
            f"advanced route {route_id} control must be an object", reason=REASON_MALFORMED
        )
    undeclared = sorted(set(str(key) for key in raw) - _ALLOWED_CONTROL_KEYS)
    if undeclared:
        raise AdvancedRouteError(
            f"advanced route {route_id} control carries undeclared keys: {', '.join(undeclared)}",
            reason=REASON_MALFORMED,
        )
    name = raw.get("name")
    if not isinstance(name, str) or _CONTROL_NAME.fullmatch(name) is None:
        raise AdvancedRouteError(
            f"advanced route {route_id} control declares an invalid name: {name!r}", reason=REASON_MALFORMED
        )
    control_type = raw.get("type")
    if control_type not in _CONTROL_TYPES:
        raise AdvancedRouteError(
            f"advanced route {route_id} control {name} declares an unsupported type: {control_type!r}",
            reason=REASON_MALFORMED,
        )
    policy_owned = raw.get("policy_owned", False)
    if not isinstance(policy_owned, bool):
        raise AdvancedRouteError(
            f"advanced route {route_id} control {name} policy_owned must be a boolean", reason=REASON_MALFORMED
        )
    default = raw.get("default")
    bounds: tuple[int, int] | None = None
    allowed: tuple[str, ...] | None = None
    if control_type == "int":
        raw_bounds = raw.get("bounds")
        if not isinstance(raw_bounds, Mapping):
            raise AdvancedRouteError(
                f"advanced route {route_id} int control {name} requires bounds", reason=REASON_MALFORMED
            )
        low, high = raw_bounds.get("min"), raw_bounds.get("max")
        for edge in (low, high):
            if isinstance(edge, bool) or not isinstance(edge, int):
                raise AdvancedRouteError(
                    f"advanced route {route_id} int control {name} bounds must be integers",
                    reason=REASON_MALFORMED,
                )
        if low > high:
            raise AdvancedRouteError(
                f"advanced route {route_id} int control {name} has inverted bounds", reason=REASON_MALFORMED
            )
        bounds = (low, high)
        if isinstance(default, bool) or not isinstance(default, int) or not low <= default <= high:
            raise AdvancedRouteError(
                f"advanced route {route_id} int control {name} default is out of bounds", reason=REASON_MALFORMED
            )
    elif control_type == "enum":
        raw_allowed = raw.get("allowed_values")
        if (
            not isinstance(raw_allowed, (list, tuple))
            or not 1 <= len(raw_allowed) <= MAX_ALLOWED_VALUES
            or not all(
                isinstance(item, str) and 1 <= len(item) <= MAX_ALLOWED_VALUE_CHARS for item in raw_allowed
            )
            or len(set(raw_allowed)) != len(raw_allowed)
        ):
            raise AdvancedRouteError(
                f"advanced route {route_id} enum control {name} requires a unique allowed_values set",
                reason=REASON_MALFORMED,
            )
        allowed = tuple(raw_allowed)
        if not isinstance(default, str) or default not in allowed:
            raise AdvancedRouteError(
                f"advanced route {route_id} enum control {name} default is not an allowed value",
                reason=REASON_MALFORMED,
            )
    else:  # bool
        if not isinstance(default, bool):
            raise AdvancedRouteError(
                f"advanced route {route_id} bool control {name} default must be a boolean", reason=REASON_MALFORMED
            )
    return AdvancedControlDescriptor(
        name=name, type=control_type, default=default, bounds=bounds,
        allowed_values=allowed, policy_owned=policy_owned,
    )


def _supported_controls(entry: Mapping[str, Any], route_id: str) -> tuple[AdvancedControlDescriptor, ...]:
    declared = entry.get("supported_controls")
    if not isinstance(declared, Sequence) or isinstance(declared, (str, bytes)):
        raise AdvancedRouteError(
            f"advanced route {route_id} supported_controls must be an array", reason=REASON_MALFORMED
        )
    if not 1 <= len(declared) <= MAX_SUPPORTED_CONTROLS:
        raise AdvancedRouteError(
            f"advanced route {route_id} must declare between 1 and {MAX_SUPPORTED_CONTROLS} controls",
            reason=REASON_MALFORMED,
        )
    controls: list[AdvancedControlDescriptor] = []
    seen: set[str] = set()
    for raw in declared:
        descriptor = _control_descriptor(raw, route_id)
        if descriptor.name in seen:
            raise AdvancedRouteError(
                f"advanced route {route_id} declares a duplicate control: {descriptor.name}",
                reason=REASON_MALFORMED,
            )
        seen.add(descriptor.name)
        controls.append(descriptor)
    return tuple(controls)


def discover_advanced_routes(configured_routes: Sequence[Mapping[str, Any]]) -> DiscoveredAdvancedRoutes:
    """Fail-closed validate the operator-configured Advanced route capabilities.

    Returns exactly the configured set with stable identifiers, in configured
    order.  Any undeclared key, invalid identifier, duplicate route, foreign
    provider, malformed control, or unsafe display value refuses the whole
    discovery -- nothing partial is published, and there is no endpoint, URL,
    token, or credential key the config schema can even carry.
    """
    if isinstance(configured_routes, Mapping) or isinstance(configured_routes, (str, bytes)):
        raise AdvancedRouteError(
            "configured advanced routes must be a sequence of route objects", reason=REASON_MALFORMED
        )
    routes: list[AdvancedRouteCapability] = []
    seen: set[str] = set()
    for entry in configured_routes:
        if not isinstance(entry, Mapping):
            raise AdvancedRouteError("each configured advanced route must be an object", reason=REASON_MALFORMED)
        undeclared = sorted(set(str(key) for key in entry) - _ALLOWED_CONFIG_KEYS)
        if undeclared:
            raise AdvancedRouteError(
                f"advanced route configuration carries undeclared keys: {', '.join(undeclared)}",
                reason=REASON_MALFORMED,
            )
        missing = sorted(_REQUIRED_CONFIG_KEYS - set(entry))
        if missing:
            raise AdvancedRouteError(
                f"advanced route configuration is missing required keys: {', '.join(missing)}",
                reason=REASON_MALFORMED,
            )
        provider = entry.get("provider", SERVING_PROVIDER)
        if provider != SERVING_PROVIDER:
            raise AdvancedRouteError(
                f"advanced routes may only reference {SERVING_PROVIDER}, not {provider!r}", reason=REASON_MALFORMED
            )
        route_id = _identifier(entry, "route_id", _ROUTE_ID)
        if route_id in seen:
            raise AdvancedRouteError(
                f"advanced route configuration declares a duplicate route: {route_id}", reason=REASON_MALFORMED
            )
        seen.add(route_id)
        routes.append(
            AdvancedRouteCapability(
                route_id=route_id,
                display_name=_display_name(entry, route_id),
                route_digest=_identifier(entry, "route_digest", _DIGEST),
                profile_digest=_identifier(entry, "profile_digest", _DIGEST),
                serving_contract_version=_identifier(entry, "serving_contract_version", _CONTRACT_VERSION),
                model_profile=_identifier(entry, "model_profile", _MODEL_PROFILE),
                supported_controls=_supported_controls(entry, route_id),
                structured_output_supported=_bool(entry, "structured_output_supported"),
                tools_supported=_bool(entry, "tools_supported"),
            )
        )
    return DiscoveredAdvancedRoutes(routes=tuple(routes))


# --- Catalog-drift invalidation ---------------------------------------------


def route_capability_repair(
    pinned: Mapping[str, Any], discovered: DiscoveredAdvancedRoutes,
) -> dict[str, Any]:
    """Deterministically compute the drift/repair state for a pinned route.

    ``pinned`` names a ``route_id`` and the ``route_digest``/``profile_digest`` a
    branch or preset pinned.  The result is ``repair_required`` -- listing exactly
    the drifted references, never a substituted route (criterion 3) -- when the
    live discovered route is gone, when a pinned digest is missing or is not a
    valid digest string (an unverifiable pin cannot silently pass), or when a
    pinned digest no longer matches the live catalog.  It is ``ready`` only when
    every pinned digest is a valid string that matches the live route: drift, or an
    unverifiable pin, must invalidate and never fail open.
    """
    if not isinstance(discovered, DiscoveredAdvancedRoutes):
        raise AdvancedRouteError(
            "route capability repair requires the module's own frozen discovery snapshot",
            reason=REASON_MALFORMED,
        )
    route_id = str(pinned.get("route_id"))
    drifted: list[dict[str, str]] = []
    try:
        live = discovered.route(route_id)
    except AdvancedRouteError:
        live = None
    for ref_kind, pinned_key, live_value in (
        ("route", "route_digest", getattr(live, "route_digest", None)),
        ("profile", "profile_digest", getattr(live, "profile_digest", None)),
    ):
        pinned_digest = pinned.get(pinned_key)
        pinned_repr = pinned_digest if isinstance(pinned_digest, str) else ""
        if live is None:
            # The pinned route vanished from the live catalog: every ref is
            # unverifiable -> repair_required, never substituted.
            drifted.append({"ref_kind": ref_kind, "id": route_id, "pinned_digest": pinned_repr})
            continue
        if not isinstance(pinned_digest, str) or not _DIGEST.match(pinned_digest):
            # A missing or non-string/malformed pin cannot be verified against the
            # live catalog -- fail closed rather than silently pass.
            drifted.append({"ref_kind": ref_kind, "id": route_id, "pinned_digest": pinned_repr})
            continue
        if live_value != pinned_digest:
            drifted.append({"ref_kind": ref_kind, "id": route_id, "pinned_digest": pinned_digest})
    if drifted:
        return {"status": "repair_required", "drifted_refs": drifted}
    return {"status": "ready"}


def _refuse_drift(route: AdvancedRouteCapability, pinned: Mapping[str, Any]) -> None:
    pinned_route = pinned.get("route_digest")
    if isinstance(pinned_route, str) and pinned_route != route.route_digest:
        raise AdvancedRouteError(
            f"pinned route digest no longer matches the live catalog for {route.route_id}",
            reason=REASON_ROUTE_DIGEST_DRIFT,
        )
    pinned_profile = pinned.get("profile_digest")
    if isinstance(pinned_profile, str) and pinned_profile != route.profile_digest:
        raise AdvancedRouteError(
            f"pinned profile digest no longer matches the live catalog for {route.route_id}",
            reason=REASON_PROFILE_DIGEST_DRIFT,
        )


# --- Selection validation ----------------------------------------------------


def _normalize_submitted(submitted: Any) -> list[tuple[str, Any, str | None]]:
    """Accept either a ``{name: value}`` mapping or a submitted_controls array."""
    triples: list[tuple[str, Any, str | None]] = []
    if isinstance(submitted, Mapping):
        for key in submitted:
            triples.append((str(key), submitted[key], None))
        return triples
    if isinstance(submitted, Sequence) and not isinstance(submitted, (str, bytes)):
        for item in submitted:
            if not isinstance(item, Mapping):
                raise AdvancedRouteError("each submitted control must be an object", reason=REASON_MALFORMED)
            undeclared = sorted(set(str(k) for k in item) - {"name", "value", "provenance"})
            if undeclared:
                raise AdvancedRouteError(
                    f"submitted control carries undeclared keys: {', '.join(undeclared)}", reason=REASON_MALFORMED
                )
            provenance = item.get("provenance")
            if provenance is not None and provenance not in _CONTROL_PROVENANCE:
                raise AdvancedRouteError(
                    f"submitted control provenance is not allowlisted: {provenance!r}", reason=REASON_MALFORMED
                )
            triples.append((str(item.get("name")), item.get("value"), provenance))
        return triples
    raise AdvancedRouteError(
        "submitted controls must be a mapping or a submitted_controls array", reason=REASON_MALFORMED
    )


def validate_advanced_selection(
    route_id: Any,
    submitted_controls: Any,
    discovered: DiscoveredAdvancedRoutes,
    *,
    pinned: Mapping[str, Any] | None = None,
) -> AdvancedRouteSelection:
    """Fail-closed validate one Advanced control selection before any Serving request.

    ``discovered`` must be this module's own frozen discovery snapshot -- a
    caller-assembled mapping is refused by type, so no browser- or model-supplied
    structure can widen the allowlist.  Refuses, each with its typed reason and
    with no I/O:

    * an unknown ``route_id`` (:data:`REASON_ROUTE_UNKNOWN`);
    * a stale pin -- if ``pinned`` names a route/profile digest the live catalog
      no longer matches, the selection is invalidated with a ``*_digest_drift``
      reason rather than silently applied against a substituted route;
    * a control the route does not declare (:data:`REASON_CONTROL_UNSUPPORTED`);
    * a value outside the control's declared type/bounds/allowed set;
    * a crafted override of a ``policy_owned`` control -- a submitted value must
      carry ``policy_override`` provenance and equal the declared default
      (:data:`REASON_POLICY_OWNED_READONLY`).

    Returns a canonical :class:`AdvancedRouteSelection`; there is no fallback
    branch and no code path that reaches a raw provider.
    """
    if not isinstance(discovered, DiscoveredAdvancedRoutes):
        raise AdvancedRouteError(
            "discovered advanced routes must be the module's own frozen snapshot, "
            "not a caller-assembled mapping",
            reason=REASON_MALFORMED,
        )
    if not isinstance(route_id, str):
        raise AdvancedRouteError(f"advanced route id must be a string: {route_id!r}", reason=REASON_MALFORMED)
    route = discovered.route(route_id)  # raises REASON_ROUTE_UNKNOWN
    if pinned is not None:
        if not isinstance(pinned, Mapping):
            raise AdvancedRouteError("pinned digests must be a mapping", reason=REASON_MALFORMED)
        _refuse_drift(route, pinned)

    triples = _normalize_submitted(submitted_controls)
    seen: set[str] = set()
    accepted: list[tuple[str, Any, str]] = []
    for name, value, provenance in triples:
        if name in seen:
            raise AdvancedRouteError(f"control {name} is submitted more than once", reason=REASON_MALFORMED)
        seen.add(name)
        descriptor = route.control(name)  # raises REASON_CONTROL_UNSUPPORTED
        descriptor.check_value(value)  # raises the typed value reason
        if descriptor.policy_owned:
            # A policy-owned control is read-only: an actor may only echo its
            # declared default with policy_override provenance; any other value or
            # provenance is a crafted override.
            if provenance not in (None, "policy_override") or value != descriptor.default:
                raise AdvancedRouteError(
                    f"policy-owned control is read-only and cannot be overridden: {name}",
                    reason=REASON_POLICY_OWNED_READONLY,
                )
            accepted.append((name, descriptor.default, "policy_override"))
        else:
            accepted.append((name, value, provenance if provenance is not None else "declared"))
    accepted.sort(key=lambda item: item[0])
    return AdvancedRouteSelection(route=route, controls=tuple(accepted))
