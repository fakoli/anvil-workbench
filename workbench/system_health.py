"""Observational integration descriptors and the posture audit.

This module builds the *read-only* system-health view of the hub's declared
integrations (Anvil Serving, the Neo4j evidence projection, purpose retrieval,
the voice relay, chat persistence, and the project bridge).  It answers one
question per integration -- "is this configured, and if not, how do I fix it?"
-- and nothing else.

Authority boundary (AGENTS.md + preferences-configuration T003): every value
here is *observational*.  A descriptor's frozen field set is a closed record
(the dataclass is the ``additionalProperties: false`` closure): no field is
named for, or shaped to hold, a credential, a raw endpoint URL, a local
filesystem path, an approval, a command, or any execution surface.  Reading a
descriptor grants no claim, lease, approval, or effect, and construction opens
no integration and mutates nothing -- it reads only the already-parsed
:class:`~workbench.config.Settings` and an optional injected bridge-health
observation.

Redaction (T003.1 criterion 2): every readable prose field (title, owner,
remediation, detail) is scrubbed on construction through
:func:`workbench.redaction.redact_config_text`, which removes all four declared
classes -- secrets/credentials, sensitive raw URLs/endpoints, and local paths
(see that function for the exact shapes).  Identifier, version, and timestamp
fields are strict-pattern validated instead of scrubbed, so a secret cannot ride
in through them either.  Construction-time scrubbing keeps the digest a
commitment to safe content and protects the CLI render path; the browser-facing
guarantee is additionally enforced at the API boundary, where
:func:`workbench.redaction.scrub_config_payload` scrubs the serialized response
so even a rogue service that bypassed construction cannot leak through the hub.

Determinism (T008 criterion 1): a descriptor's ``digest`` is computed over its
stable content *excluding* the volatile ``last_checked_at`` observation time, so
identical configuration yields an identical digest and identical posture
findings regardless of when the check ran.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .config import Settings
from .contracts import canonical_json_bytes
from .redaction import redact_config_text

SYSTEM_HEALTH_SCHEMA_VERSION = "workbench-system-health/v1"

#: The three observational states a descriptor may report.  ``ready`` means the
#: integration is configured (and, for the bridge, observed healthy);
#: ``degraded`` means configured but observed unhealthy; ``disabled`` means not
#: configured.  ``degraded`` and ``disabled`` are both "unavailable" states with
#: truthful remediation.
STATES = frozenset({"ready", "degraded", "disabled"})

#: The fixed, public catalog of declared integrations.  This set is not secret
#: (it is the same for every deployment), so an unknown-integration lookup is a
#: plain 404, never an existence oracle.
INTEGRATION_IDS = (
    "anvil_serving",
    "graph_projection",
    "purpose_retrieval",
    "voice_relay",
    "chat_persistence",
    "project_bridge",
)

#: Observed bridge-health signals the hub may pass through.  ``None`` means the
#: hub has no live observation, which is reported truthfully as ``disabled``.
BRIDGE_HEALTH_SIGNALS = frozenset({"healthy", "degraded", "unreachable"})

_INTEGRATION_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,63}$")
_OWNER = re.compile(r"^[a-z][a-z0-9\- ]{0,63}$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
#: RFC 3339 / ISO-8601 UTC-or-offset instant, length-bounded so the timestamp
#: field can never smuggle unbounded content.
_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$"
)
MAX_TIMESTAMP_CHARS = 40
MAX_TITLE_CHARS = 120
MAX_REMEDIATION_CHARS = 400
MAX_DETAIL_CHARS = 200
MAX_DEPENDENCIES = 8

_DESCRIPTOR_DIGEST_PREFIX = b"anvil-workbench/system-health-descriptor/v1\0"


class SystemHealthError(ValueError):
    """An integration descriptor or posture check violates its display contract."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemHealthError(message)


def rfc3339(moment: datetime) -> str:
    """Serialize a timezone-aware instant to a bounded RFC 3339 UTC string."""
    _require(isinstance(moment, datetime) and moment.tzinfo is not None, "checked_at must be timezone-aware")
    text = moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _require(bool(_RFC3339.match(text)) and len(text) <= MAX_TIMESTAMP_CHARS, "checked_at is not RFC 3339")
    return text


def _prose(value: Any, limit: int, label: str) -> str:
    _require(isinstance(value, str) and 0 < len(value) <= limit, f"{label} must be bounded readable text")
    # Last-hop scrub: strip any credential, raw URL, or filesystem path a caller
    # might have spliced into descriptor prose before it reaches the browser.
    return redact_config_text(value)


@dataclass(frozen=True)
class IntegrationDescriptor:
    """One integration's observational configuration/health descriptor.

    The closed field set exposes exactly the six things T003.1 requires --
    configured *state*, *version*/digest, *last check*, safe *owner*,
    *dependencies*, and *remediation* -- and structurally nothing else: no
    approval, command, credential, endpoint, or path field is representable.
    """

    integration_id: str
    title: str
    state: str
    configured: bool
    owner: str
    remediation: str
    dependencies: tuple[str, ...] = ()
    version: str | None = None
    detail: str | None = None
    last_checked_at: str | None = None
    schema_version: str = SYSTEM_HEALTH_SCHEMA_VERSION
    non_canonical: bool = True

    def __post_init__(self) -> None:
        _require(bool(_INTEGRATION_ID.match(str(self.integration_id))), "integration_id is invalid")
        _require(str(self.integration_id) in INTEGRATION_IDS, f"unknown integration_id: {self.integration_id}")
        object.__setattr__(self, "title", _prose(self.title, MAX_TITLE_CHARS, "title"))
        _require(self.state in STATES, f"state must be one of {sorted(STATES)}")
        _require(isinstance(self.configured, bool), "configured must be a boolean")
        # A disabled integration is never "configured"; a ready/degraded one always is.
        _require(self.configured == (self.state != "disabled"), "configured must agree with state")
        _require(bool(_OWNER.match(str(self.owner))), "owner must be a safe lowercase label")
        object.__setattr__(self, "owner", _prose(self.owner, MAX_TITLE_CHARS, "owner"))
        object.__setattr__(self, "remediation", _prose(self.remediation, MAX_REMEDIATION_CHARS, "remediation"))

        deps = tuple(self.dependencies)
        _require(len(deps) <= MAX_DEPENDENCIES, "too many dependencies")
        for dep in deps:
            _require(dep in INTEGRATION_IDS, f"dependency names an unknown integration: {dep}")
            _require(dep != self.integration_id, "an integration cannot depend on itself")
        _require(len(set(deps)) == len(deps), "dependencies must be unique")
        object.__setattr__(self, "dependencies", deps)

        if self.version is not None:
            _require(bool(_VERSION.match(str(self.version))), "version is invalid")
        if self.detail is not None:
            object.__setattr__(self, "detail", _prose(self.detail, MAX_DETAIL_CHARS, "detail"))
        if self.last_checked_at is not None:
            _require(
                bool(_RFC3339.match(str(self.last_checked_at))) and len(str(self.last_checked_at)) <= MAX_TIMESTAMP_CHARS,
                "last_checked_at is not RFC 3339",
            )
        _require(self.schema_version == SYSTEM_HEALTH_SCHEMA_VERSION, "schema_version is unexpected")
        _require(self.non_canonical is True, "a health descriptor is always non-canonical")

    @property
    def digest(self) -> str:
        """A deterministic content digest over the stable fields.

        Excludes ``last_checked_at`` (the only volatile input) so identical
        configuration hashes identically -- the invariant T008 determinism
        depends on.  Computed over the already-scrubbed prose, so the digest is
        a commitment to the safe, redacted content, never the raw input.
        """
        stable = {
            "schema_version": self.schema_version,
            "integration_id": self.integration_id,
            "title": self.title,
            "state": self.state,
            "configured": self.configured,
            "owner": self.owner,
            "remediation": self.remediation,
            "dependencies": list(self.dependencies),
            "version": self.version,
            "detail": self.detail,
        }
        return "sha256:" + hashlib.sha256(_DESCRIPTOR_DIGEST_PREFIX + canonical_json_bytes(stable)).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        """Deterministic display serialization with a closed field set."""
        data: dict[str, Any] = {
            "configured": self.configured,
            "dependencies": list(self.dependencies),
            "digest": self.digest,
            "integration_id": self.integration_id,
            "non_canonical": self.non_canonical,
            "owner": self.owner,
            "remediation": self.remediation,
            "schema_version": self.schema_version,
            "state": self.state,
            "title": self.title,
        }
        if self.version is not None:
            data["version"] = self.version
        if self.detail is not None:
            data["detail"] = self.detail
        if self.last_checked_at is not None:
            data["last_checked_at"] = self.last_checked_at
        return data


@dataclass(frozen=True)
class _IntegrationSpec:
    """The static, reviewed definition of one declared integration."""

    integration_id: str
    title: str
    owner: str
    remediation: str
    dependencies: tuple[str, ...]
    configured: Callable[[Settings], bool]
    version: str | None = None


#: The reviewed integration catalog.  Each ``configured`` predicate reads only
#: already-parsed Settings booleans -- never the secret value itself -- so
#: evaluating it cannot leak a credential.  Each ``remediation`` names the
#: environment variable(s) to set (a safe public name), never a value or path.
_SPECS: tuple[_IntegrationSpec, ...] = (
    _IntegrationSpec(
        integration_id="anvil_serving",
        title="Anvil Serving model plane",
        owner="anvil-serving",
        remediation=(
            "Set ANVIL_ROUTER_BASE_URL and ANVIL_ROUTER_TOKEN to the tailnet "
            "Serving endpoint credentials; the hub has no provider fallback."
        ),
        dependencies=(),
        configured=lambda s: bool(s.anvil_router_base_url and s.anvil_router_token),
    ),
    _IntegrationSpec(
        integration_id="graph_projection",
        title="Neo4j evidence projection",
        owner="neo4j-projection",
        remediation=(
            "Set WORKBENCH_NEO4J_PASSWORD to enable the retrieval/lineage "
            "projection; it stays disabled and never becomes canonical until then."
        ),
        dependencies=(),
        configured=lambda s: bool(s.neo4j_password),
    ),
    _IntegrationSpec(
        integration_id="purpose_retrieval",
        title="Purpose retrieval",
        owner="anvil-serving",
        remediation=(
            "Set WORKBENCH_EMBEDDING_MODEL (and optionally WORKBENCH_RERANK_MODEL) "
            "with Anvil Serving and the Neo4j projection both configured."
        ),
        dependencies=("anvil_serving", "graph_projection"),
        configured=lambda s: bool(
            s.embedding_model and s.anvil_router_base_url and s.anvil_router_token and s.neo4j_password
        ),
    ),
    _IntegrationSpec(
        integration_id="voice_relay",
        title="Voice realtime relay",
        owner="anvil-serving",
        remediation=(
            "Set ANVIL_VOICE_REALTIME_URL and ANVIL_VOICE_REALTIME_TOKEN to the "
            "private Realtime relay; voice stays disabled without it."
        ),
        dependencies=("anvil_serving",),
        configured=lambda s: bool(s.anvil_voice_realtime_url and s.anvil_voice_realtime_token),
    ),
    _IntegrationSpec(
        integration_id="chat_persistence",
        title="Chat persistence",
        owner="workbench-hub",
        remediation=(
            "Set WORKBENCH_CHAT_HASH_KEY to the hub-held content-fingerprint key; "
            "without it chat endpoints fail closed and persist nothing."
        ),
        dependencies=(),
        configured=lambda s: bool(s.chat_content_hash_key),
    ),
)

#: Bridge health is a runtime observation, not an env-configured integration, so
#: it is handled separately from the predicate-driven specs above.
_BRIDGE_ID = "project_bridge"
_BRIDGE_TITLE = "Project bridge"
_BRIDGE_OWNER = "project-bridge"
_BRIDGE_DEPS = ("anvil_serving",)
_BRIDGE_REMEDIATION_DISABLED = (
    "Register a project bridge and let it poll the hub; local execution stays "
    "unavailable until a bridge is connected."
)
_BRIDGE_REMEDIATION_DEGRADED = (
    "The project bridge is connected but reporting an unhealthy poll; check the "
    "bridge process and its lease renewal, then let it re-register."
)


def _bridge_descriptor(signal: str | None, checked_at: str | None) -> IntegrationDescriptor:
    """Build the bridge descriptor from an optional observed health signal.

    ``None`` (no live observation) is reported truthfully as ``disabled``.  A
    ``healthy`` signal is ``ready``; ``degraded``/``unreachable`` map to
    ``degraded`` so an unhealthy-but-connected bridge is never reported ready.
    """
    _require(signal is None or signal in BRIDGE_HEALTH_SIGNALS, f"unknown bridge health signal: {signal}")
    if signal == "healthy":
        state, remediation, detail = "ready", "The project bridge is connected and polling normally.", None
    elif signal in ("degraded", "unreachable"):
        state, remediation, detail = "degraded", _BRIDGE_REMEDIATION_DEGRADED, f"observed bridge signal: {signal}"
    else:
        state, remediation, detail = "disabled", _BRIDGE_REMEDIATION_DISABLED, None
    return IntegrationDescriptor(
        integration_id=_BRIDGE_ID,
        title=_BRIDGE_TITLE,
        state=state,
        configured=state != "disabled",
        owner=_BRIDGE_OWNER,
        remediation=remediation,
        dependencies=_BRIDGE_DEPS,
        detail=detail,
        last_checked_at=checked_at,
    )


def build_integration_descriptors(
    settings: Settings,
    *,
    checked_at: str | None = None,
    bridge_health: str | None = None,
) -> tuple[IntegrationDescriptor, ...]:
    """Build one descriptor for every declared integration, in catalog order.

    Reads only already-parsed Settings booleans and the optional injected
    ``bridge_health`` observation.  Config-derived integrations report ``ready``
    or ``disabled`` (the hub runs no live probe, so it never claims ``degraded``
    it did not observe); the bridge additionally reports ``degraded`` from a
    passed-through signal.  Unconfigured integrations get a truthful ``disabled``
    state with safe remediation (T003.1 criterion 4).
    """
    descriptors: list[IntegrationDescriptor] = []
    for spec in _SPECS:
        configured = bool(spec.configured(settings))
        state = "ready" if configured else "disabled"
        remediation = (
            f"{spec.title} is configured and observational-only."
            if configured
            else spec.remediation
        )
        descriptors.append(
            IntegrationDescriptor(
                integration_id=spec.integration_id,
                title=spec.title,
                state=state,
                configured=configured,
                owner=spec.owner,
                remediation=remediation,
                dependencies=spec.dependencies,
                version=spec.version,
                last_checked_at=checked_at,
            )
        )
    descriptors.append(_bridge_descriptor(bridge_health, checked_at))
    return tuple(descriptors)


# ---------------------------------------------------------------------------
# Posture audit (T008): stable-ID, non-mutating checks over the same
# descriptors, rendered identically by the CLI and the System Health API.
# ---------------------------------------------------------------------------

POSTURE_SCHEMA_VERSION = "workbench-posture/v1"

#: Posture check statuses.  ``ok`` = healthy; ``attention`` = a posture concern
#: to act on; ``disabled`` = an unconfigured integration reported truthfully.
POSTURE_STATUSES = frozenset({"ok", "attention", "disabled"})
POSTURE_SEVERITIES = frozenset({"info", "warn"})

#: A check id is a stable ``posture.<segment>[.<segment>…]`` dotted label, never
#: a command name.  Requiring the ``posture.`` prefix and at least one dotted
#: segment means a bare command-shaped token (``run_codex``) can never pass, so a
#: finding cannot smuggle an executable/approval name through its identifier.
_CHECK_ID = re.compile(r"^posture(?:\.[a-z][a-z0-9_]{0,31}){1,5}$")


@dataclass(frozen=True)
class PostureCheck:
    """One observational posture finding with a stable, surface-independent ID."""

    check_id: str
    title: str
    status: str
    severity: str
    remediation: str

    def __post_init__(self) -> None:
        _require(bool(_CHECK_ID.match(str(self.check_id))), "check_id is invalid")
        object.__setattr__(self, "title", _prose(self.title, MAX_TITLE_CHARS, "title"))
        _require(self.status in POSTURE_STATUSES, f"status must be one of {sorted(POSTURE_STATUSES)}")
        _require(self.severity in POSTURE_SEVERITIES, f"severity must be one of {sorted(POSTURE_SEVERITIES)}")
        object.__setattr__(self, "remediation", _prose(self.remediation, MAX_REMEDIATION_CHARS, "remediation"))

    def as_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "remediation": self.remediation,
            "severity": self.severity,
            "status": self.status,
            "title": self.title,
        }


@dataclass(frozen=True)
class PostureReport:
    """A deterministic, non-mutating snapshot of every posture check."""

    checks: tuple[PostureCheck, ...]
    checked_at: str | None = None
    schema_version: str = POSTURE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        checks = tuple(self.checks)
        ids = [check.check_id for check in checks]
        _require(len(set(ids)) == len(ids), "posture check IDs must be unique")
        # Deterministic ordering by stable ID so the same configuration renders
        # the same sequence on every surface and every run.
        object.__setattr__(self, "checks", tuple(sorted(checks, key=lambda c: c.check_id)))
        if self.checked_at is not None:
            _require(
                bool(_RFC3339.match(str(self.checked_at))) and len(str(self.checked_at)) <= MAX_TIMESTAMP_CHARS,
                "checked_at is not RFC 3339",
            )
        _require(self.schema_version == POSTURE_SCHEMA_VERSION, "schema_version is unexpected")

    def findings(self) -> list[dict[str, Any]]:
        """The surface-independent findings (no timestamp), for byte-equal comparison."""
        return [check.as_dict() for check in self.checks]

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "checks": self.findings(),
        }
        if self.checked_at is not None:
            data["checked_at"] = self.checked_at
        return data


_STATE_TO_STATUS = {"ready": "ok", "degraded": "attention", "disabled": "disabled"}


def run_posture_audit(
    settings: Settings,
    *,
    checked_at: str | None = None,
    bridge_health: str | None = None,
) -> PostureReport:
    """Run the deterministic, non-mutating posture audit.

    This is the single check runner both the CLI and the System Health API call,
    so the two surfaces can never drift.  It derives one ``posture.integration.*``
    check per declared integration from the same descriptors, then adds
    hub-security checks that are not integration-bound (currently the insecure
    dev-actor override).  Every finding is deterministic for identical settings
    and carries remediation without secrets or paths (the descriptor prose is
    already scrubbed).
    """
    checks: list[PostureCheck] = []
    for descriptor in build_integration_descriptors(settings, checked_at=checked_at, bridge_health=bridge_health):
        status = _STATE_TO_STATUS[descriptor.state]
        checks.append(
            PostureCheck(
                check_id=f"posture.integration.{descriptor.integration_id}",
                title=descriptor.title,
                status=status,
                severity="info" if status in ("ok", "disabled") else "warn",
                remediation=descriptor.remediation,
            )
        )

    # Hub-security posture: the development-only actor override must never be on
    # outside a loopback dev stack, so surface it as an actionable concern.
    insecure = bool(settings.allow_insecure_dev_actor)
    checks.append(
        PostureCheck(
            check_id="posture.security.insecure_dev_actor",
            title="Insecure development actor override",
            status="attention" if insecure else "ok",
            severity="warn" if insecure else "info",
            remediation=(
                "Unset WORKBENCH_ALLOW_INSECURE_DEV_ACTOR; the hub is trusting an "
                "unauthenticated loopback identity, which is safe only in local development."
                if insecure
                else "The hub requires a trusted tailnet identity for every actor."
            ),
        )
    )
    return PostureReport(checks=tuple(checks), checked_at=checked_at)


def render_posture_rows(report: PostureReport) -> list[str]:
    """Render a report as stable, tab-separated CLI rows (no timestamp).

    Deliberately excludes ``checked_at`` so the rows are a pure function of the
    findings -- the CLI and the API therefore render the *same* content for the
    same configuration.
    """
    return [
        f"{check.check_id}\t{check.status}\t{check.severity}\t{check.remediation}"
        for check in report.checks
    ]


class UnknownIntegrationError(LookupError):
    """No declared integration has the requested id."""


class SystemHealthService:
    """Builds descriptors and posture reports from settings and observed bridge health.

    A single injected clock keeps ``last_checked_at`` deterministic in tests.
    The service holds no credential and performs no I/O; it is a thin, read-only
    projection of already-parsed settings plus an optional bridge observation.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        clock: Callable[[], datetime] | None = None,
        bridge_health: str | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        _require(
            bridge_health is None or bridge_health in BRIDGE_HEALTH_SIGNALS,
            f"unknown bridge health signal: {bridge_health}",
        )
        self._bridge_health = bridge_health

    def _now(self) -> str:
        return rfc3339(self._clock())

    def descriptors(self) -> tuple[IntegrationDescriptor, ...]:
        return build_integration_descriptors(
            self._settings, checked_at=self._now(), bridge_health=self._bridge_health
        )

    def get(self, integration_id: str) -> IntegrationDescriptor:
        for descriptor in self.descriptors():
            if descriptor.integration_id == integration_id:
                return descriptor
        raise UnknownIntegrationError(integration_id)

    def posture(self) -> PostureReport:
        return run_posture_audit(
            self._settings, checked_at=self._now(), bridge_health=self._bridge_health
        )
