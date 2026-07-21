"""Discover and validate the allowed chat routes (chat-first-voice T003.1).

Anvil Serving is the only managed model path and it owns model policy; the
AGENTS.md boundary is explicit: never add a raw-provider fallback.  This
module is the hub-side gate that makes the chat surface honor that boundary
*before* any Serving request exists:

* :func:`discover_chat_routes` fail-closed validates the operator-configured
  chat route declarations (``WORKBENCH_CHAT_ROUTES``, a reviewed JSON array)
  into a frozen :class:`DiscoveredChatRoutes` snapshot.  The published
  projection is exactly the ``chat-turn.v1`` route-reference shape plus the
  declared-supported Advanced controls: provider const ``anvil-serving``,
  ``route_id``, ``serving_contract_version``, ``route_digest``,
  ``model_profile``, bounded display metadata, and control names.  No
  endpoint, URL, token, credential, or policy field exists in the config
  schema (an undeclared key refuses the whole discovery) or in the
  projection, so there is nothing secret to redact and nothing for a browser
  to replay.
* :func:`validate_chat_route_selection` refuses an unknown ``route_id`` or a
  requested control the selected route does not declare, and bounds every
  control value to the ``chat-turn.v1`` ``advanced_controls`` limits.  It
  performs no I/O and takes only the module's own frozen discovery snapshot
  (a caller-assembled mapping is refused by type), so a refusal happens
  strictly before a Serving request could be issued and there is no code
  path that falls back to a raw provider -- this module deliberately imports
  no HTTP client at all.

Like the sibling T004.x discovery slices this is implemented and
hermetically tested; wiring a browser endpoint over the projection is a
separate step.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


class ChatRouteError(RuntimeError):
    """A chat route configuration or selection cannot be trusted."""


SERVING_PROVIDER = "anvil-serving"

#: Identifier grammars, taken verbatim from the ``chat-turn.v1`` route block.
_ROUTE_ID = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")
_MODEL_PROFILE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_CONTRACT_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_ROUTE_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")

MAX_DISPLAY_NAME_CHARS = 120

#: A display name is human-readable text only, guarded three ways: an
#: allowlist charset that structurally forbids URL and path punctuation
#: (colon, slash, at-sign) so no endpoint can be expressed; a semantic word
#: denylist for secret vocabulary; and a per-token check refusing
#: secret-prefixed tokens, dotted host-like names, and high-entropy runs the
#: charset alone would admit.
_DISPLAY_NAME_CHARSET = re.compile(r"^[\w .,()&+'\-]{1,120}$")
_FORBIDDEN_DISPLAY_WORDS = ("token", "secret", "bearer", "credential", "password", "api_key", "apikey")
_SECRET_PREFIXES = ("sk-", "sk_", "pk-", "pk_", "ghp_", "gho_", "xox", "aws_", "akia")
_CREDENTIAL_SHAPED_TOKEN = re.compile(r"^(?=.*[0-9])(?=.*[A-Za-z])[A-Za-z0-9_-]{20,}$")

#: The complete declared-supported Advanced control surface, mirroring the
#: ``chat-turn.v1`` ``advanced_controls`` block.  A control name outside this
#: mapping is not representable, in configuration or in a selection.
_INT_CONTROL_BOUNDS: dict[str, tuple[int, int]] = {
    "temperature_milli": (0, 2000),
    "max_output_tokens": (1, 1_000_000),
}
_ENUM_CONTROL_VALUES: dict[str, frozenset[str]] = {
    "reasoning_effort": frozenset({"low", "medium", "high"}),
}
DECLARED_CHAT_CONTROLS = frozenset(_INT_CONTROL_BOUNDS) | frozenset(_ENUM_CONTROL_VALUES)

#: The closed configuration schema: exactly these keys, nothing else.  An
#: endpoint-, URL-, token-, or policy-shaped key is undeclared and refuses.
_REQUIRED_CONFIG_KEYS = frozenset({
    "route_id", "serving_contract_version", "route_digest", "model_profile",
})
_OPTIONAL_CONFIG_KEYS = frozenset({"provider", "display_name", "controls"})
_ALLOWED_CONFIG_KEYS = _REQUIRED_CONFIG_KEYS | _OPTIONAL_CONFIG_KEYS


@dataclass(frozen=True)
class ChatRouteDescriptor:
    """One reviewed chat route in its browser-safe projection.

    Identifiers, digests, and declared control names only -- the shape a
    ``chat-turn.v1`` route reference pins.  There is no endpoint, URL, token,
    credential, or policy field; resolution of ``route_id`` against a live
    endpoint stays inside the configured Anvil Serving client.
    """

    route_id: str
    display_name: str
    serving_contract_version: str
    route_digest: str
    model_profile: str
    controls: tuple[str, ...]

    @property
    def provider(self) -> str:
        return SERVING_PROVIDER

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "route_id": self.route_id,
            "display_name": self.display_name,
            "serving_contract_version": self.serving_contract_version,
            "route_digest": self.route_digest,
            "model_profile": self.model_profile,
            "controls": list(self.controls),
        }


@dataclass(frozen=True)
class DiscoveredChatRoutes:
    """The frozen snapshot of every reviewed chat route, in configured order."""

    routes: tuple[ChatRouteDescriptor, ...]

    @property
    def route_ids(self) -> tuple[str, ...]:
        return tuple(route.route_id for route in self.routes)

    def route(self, route_id: str) -> ChatRouteDescriptor:
        for route in self.routes:
            if route.route_id == route_id:
                return route
        raise ChatRouteError(f"chat route is not in the reviewed allowlist: {route_id!r}")

    def as_dict(self) -> dict[str, Any]:
        return {"routes": [route.as_dict() for route in self.routes]}


@dataclass(frozen=True)
class ChatRouteSelection:
    """One validated selection: a pinned route plus its accepted controls."""

    route: ChatRouteDescriptor
    controls: tuple[tuple[str, Any], ...]

    def controls_dict(self) -> dict[str, Any]:
        return dict(self.controls)


def _identifier(entry: Mapping[str, Any], key: str, pattern: re.Pattern[str]) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ChatRouteError(f"chat route declares an invalid {key}: {value!r}")
    return value


def _display_name(entry: Mapping[str, Any], route_id: str) -> str:
    value = entry.get("display_name", route_id)
    if not isinstance(value, str) or not value.strip() or not _DISPLAY_NAME_CHARSET.match(value):
        raise ChatRouteError(f"chat route {route_id} declares an invalid display_name")
    lowered = value.lower()
    for word in _FORBIDDEN_DISPLAY_WORDS:
        if word in lowered:
            raise ChatRouteError(
                f"chat route {route_id} display_name carries forbidden material ({word!r})"
            )
    for token in value.split():
        if _token_is_credential_shaped(token):
            raise ChatRouteError(
                f"chat route {route_id} display_name carries credential- or host-shaped material"
            )
    return value.strip()


def _token_is_credential_shaped(token: str) -> bool:
    """Refuse a display-name token that looks like a secret or a provider host."""
    lowered = token.lower()
    if any(lowered.startswith(prefix) for prefix in _SECRET_PREFIXES):
        return True
    segments = [part for part in token.split(".") if part]
    if len(segments) >= 3 and all(seg.isalnum() for seg in segments):
        return True  # a dotted host-like token (three or more alnum segments)
    return bool(_CREDENTIAL_SHAPED_TOKEN.match(token))


def _controls(entry: Mapping[str, Any], route_id: str) -> tuple[str, ...]:
    declared = entry.get("controls", [])
    if not isinstance(declared, Sequence) or isinstance(declared, (str, bytes)):
        raise ChatRouteError(f"chat route {route_id} controls must be an array of control names")
    names: list[str] = []
    for name in declared:
        if not isinstance(name, str) or name not in DECLARED_CHAT_CONTROLS:
            raise ChatRouteError(
                f"chat route {route_id} declares a control outside the chat-turn.v1 surface: {name!r}"
            )
        if name in names:
            raise ChatRouteError(f"chat route {route_id} declares a duplicate control: {name}")
        names.append(name)
    return tuple(sorted(names))


def parse_chat_routes_config(raw: str) -> tuple[Mapping[str, Any], ...]:
    """Parse the raw ``WORKBENCH_CHAT_ROUTES`` JSON document, fail closed.

    An empty setting means chat routes are not configured: it parses to an
    empty tuple, and the resulting discovery refuses every selection.
    """
    if not raw.strip():
        return ()
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ChatRouteError("configured chat routes are not valid JSON") from exc
    if not isinstance(document, list):
        raise ChatRouteError("configured chat routes must be a JSON array")
    entries: list[Mapping[str, Any]] = []
    for entry in document:
        if not isinstance(entry, Mapping):
            raise ChatRouteError("each configured chat route must be a JSON object")
        entries.append(entry)
    return tuple(entries)


def discover_chat_routes(configured_routes: Sequence[Mapping[str, Any]]) -> DiscoveredChatRoutes:
    """Fail-closed validate the operator-configured chat routes.

    Returns exactly the configured set with stable identifiers, in configured
    order.  Any undeclared key, invalid identifier, duplicate route, foreign
    provider, undeclared control, or unsafe display value refuses the whole
    discovery -- nothing partial is published.
    """
    if isinstance(configured_routes, Mapping) or isinstance(configured_routes, (str, bytes)):
        raise ChatRouteError("configured chat routes must be a sequence of route objects")
    routes: list[ChatRouteDescriptor] = []
    seen: set[str] = set()
    for entry in configured_routes:
        if not isinstance(entry, Mapping):
            raise ChatRouteError("each configured chat route must be a JSON object")
        undeclared = sorted(set(str(key) for key in entry) - _ALLOWED_CONFIG_KEYS)
        if undeclared:
            raise ChatRouteError(
                f"chat route configuration carries undeclared keys: {', '.join(undeclared)}"
            )
        missing = sorted(_REQUIRED_CONFIG_KEYS - set(entry))
        if missing:
            raise ChatRouteError(
                f"chat route configuration is missing required keys: {', '.join(missing)}"
            )
        provider = entry.get("provider", SERVING_PROVIDER)
        if provider != SERVING_PROVIDER:
            raise ChatRouteError(
                f"chat routes may only reference {SERVING_PROVIDER}, not {provider!r}"
            )
        route_id = _identifier(entry, "route_id", _ROUTE_ID)
        if route_id in seen:
            raise ChatRouteError(f"chat route configuration declares a duplicate route: {route_id}")
        seen.add(route_id)
        routes.append(
            ChatRouteDescriptor(
                route_id=route_id,
                display_name=_display_name(entry, route_id),
                serving_contract_version=_identifier(entry, "serving_contract_version", _CONTRACT_VERSION),
                route_digest=_identifier(entry, "route_digest", _ROUTE_DIGEST),
                model_profile=_identifier(entry, "model_profile", _MODEL_PROFILE),
                controls=_controls(entry, route_id),
            )
        )
    return DiscoveredChatRoutes(routes=tuple(routes))


def _validated_control_value(name: str, value: Any) -> Any:
    if name in _INT_CONTROL_BOUNDS:
        low, high = _INT_CONTROL_BOUNDS[name]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ChatRouteError(
                f"chat control {name} must be an integer in [{low}, {high}]: {value!r}"
            )
        return value
    allowed = _ENUM_CONTROL_VALUES[name]
    if not isinstance(value, str) or value not in allowed:
        raise ChatRouteError(
            f"chat control {name} must be one of {sorted(allowed)}: {value!r}"
        )
    return value


def validate_chat_route_selection(
    route_id: Any,
    requested_controls: Mapping[str, Any] | None,
    discovered: DiscoveredChatRoutes,
) -> ChatRouteSelection:
    """Fail-closed validate one chat selection before any Serving request.

    ``discovered`` must be this module's own frozen discovery snapshot -- a
    caller-assembled mapping is refused by type, so no browser- or
    model-supplied structure can widen the allowlist.  An unknown ``route_id``
    or a control the selected route does not declare refuses here, without
    any I/O; there is no fallback branch.
    """
    if not isinstance(discovered, DiscoveredChatRoutes):
        raise ChatRouteError(
            "discovered chat routes must be the module's own frozen snapshot, "
            "not a caller-assembled mapping"
        )
    if not isinstance(route_id, str):
        raise ChatRouteError(f"chat route id must be a string: {route_id!r}")
    route = discovered.route(route_id)
    controls = requested_controls if requested_controls is not None else {}
    if not isinstance(controls, Mapping):
        raise ChatRouteError("requested chat controls must be a mapping of control name to value")
    accepted: list[tuple[str, Any]] = []
    for name in sorted(str(key) for key in controls):
        if name not in DECLARED_CHAT_CONTROLS:
            raise ChatRouteError(
                f"requested chat control is outside the chat-turn.v1 surface: {name!r}"
            )
        if name not in route.controls:
            raise ChatRouteError(
                f"chat route {route.route_id} does not declare control {name!r}"
            )
        accepted.append((name, _validated_control_value(name, controls[name])))
    return ChatRouteSelection(route=route, controls=tuple(accepted))
