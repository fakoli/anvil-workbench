"""Approved plugin discovery and an isolated plugin-host install lifecycle.

reviewed-tools-plugins T002 (discovery + lifecycle) and T003 (credential-reference
and plugin-health boundaries) build on the RTP:T001 plugin contracts
(:mod:`workbench.contracts`).  The T001 resources pin the *shapes* — a reviewed,
digest-pinned plugin catalog, an enable-only capability profile, a typed idempotent
request, a redacted receipt.  This module is the *behaviour* those shapes were
authored for, mirroring the operator-declared, fail-closed
:class:`workbench.provider_catalogs.ProviderCatalogRegistry` precedent:

* **Discovery is limited to exact approved entries** (T002 criterion 1).  A
  :class:`PluginDiscovery` loads the operator-reviewed local catalog and an
  enable-only capability profile, fail-closed validates both, and answers a
  request ONLY when the exact ``(plugin_id, plugin_digest)`` — and, for a tool,
  the exact ``tool_id`` — is present in the reviewed catalog AND enabled in the
  profile.  The lookup input is ids and digests only; an arbitrary source, path,
  or URL is never accepted (only the operator-configured local file is loaded),
  and an unknown plugin, a drifted digest, or a not-enabled entry each fails
  closed on its own typed reason.

* **Installation cannot reach unrelated credentials** (T002 criterion 2 / T003).
  A :class:`CredentialBroker` resolves ONLY the installing plugin's own declared
  ``credential.credential_refs`` under its own declared ``owner_host`` into
  opaque host-owned :class:`CredentialHandle` tokens.  A handle carries a
  reference and an opaque token — there is deliberately no value/secret/key
  field anywhere, so a credential value is not representable and can never cross
  the browser/model boundary.  A plugin's own entry names only its own refs, so
  another plugin's refs, a Workbench/bridge secret, or a provider credential are
  structurally unreachable; a declared ref the host does not own fails closed
  *before* any host effect (T003 criterion 2).

* **Replay, digest drift, host failure, and unknown outcome fail closed with
  reconciliation** (T002 criterion 3).  The :class:`MemoryPluginHostStore`
  installs a typed :mod:`plugin-request` idempotently keyed by its
  ``request_digest`` under a per-instance lock: a replay returns the prior
  receipt without re-running the host; a request whose pinned digest no longer
  matches the reviewed catalog fails closed as ``digest_drift``; a host failure
  becomes a ``denied`` receipt and persists nothing (retriable); an unknown
  in-flight outcome becomes a persisted ``reconcile`` receipt (R012) so a replay
  reconciles rather than blindly re-attempting the effect.

Every human-readable field that reaches a persisted or served receipt is scrubbed
through :func:`workbench.redaction.redact_config_text` and held to the receipt
contract's ``safeText`` backstop, so a plugin error detail can never ferry a
secret, endpoint, or path into the audit record or the browser (T003 criterion 3).

Like :mod:`workbench.provider_catalogs`, this lane is implemented and hermetically
tested but deliberately NOT wired into the live bridge poll loop; the browser
surface (:func:`workbench.api.build_plugin_router`) stays fail-closed (503) until a
service is injected, and live qualification stays gated on a real plugin host.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .contracts import (
    ContractValidationError,
    contract_digest,
    validate_plugin_capability,
    validate_plugin_catalog,
    validate_plugin_receipt,
    validate_plugin_request,
)
from .models import new_id, now_utc
from .redaction import redact_config_text

# --------------------------------------------------------------------------- #
# Errors — every refusal carries a stable typed ``code`` so a caller (and a
# test) asserts the CLAIMED reason, never an incidental message match.
# --------------------------------------------------------------------------- #


class PluginHostError(RuntimeError):
    """A plugin discovery, credential, or lifecycle operation failed closed.

    ``code`` is a stable machine-checkable reason (``unknown_plugin``,
    ``digest_drift``, ``not_enabled``, ``credential_unavailable``, ...).  The
    message is a bounded, already-safe summary; it never carries a raw endpoint,
    path, or credential (callers build it from typed fields, not host prose).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PluginHostFailure(PluginHostError):
    """The isolated host itself failed while performing an install effect.

    Raised by a host runner to signal a fail-closed host error (as opposed to a
    preflight refusal).  ``detail`` is untrusted host prose: it is scrubbed
    before it can enter a receipt's ``safe_summary``.
    """

    def __init__(self, detail: str = "", code: str = "host_failure") -> None:
        super().__init__(code, "the isolated plugin host failed to complete the install")
        self.detail = detail


# --------------------------------------------------------------------------- #
# The receipt ``safeText`` backstop, loaded from the reviewed contract so this
# module stays in lockstep with the schema; fail closed if the schema moved.
# --------------------------------------------------------------------------- #

_RECEIPT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "contracts" / "schemas" / "plugin-receipt.v1.schema.json"
)


def _load_safe_text_pattern() -> re.Pattern[str]:
    try:
        schema = json.loads(_RECEIPT_SCHEMA_PATH.read_text(encoding="utf-8"))
    except OSError as exc:  # pragma: no cover - defensive
        raise PluginHostError("contract_unavailable", "plugin-receipt contract schema is unavailable") from exc
    pattern = schema.get("$defs", {}).get("safeText", {}).get("pattern")
    if not isinstance(pattern, str):
        raise PluginHostError(
            "contract_unavailable", "plugin-receipt contract no longer bounds its safe text; refusing to emit receipts"
        )
    return re.compile(pattern)


_SAFE_TEXT = _load_safe_text_pattern()

#: A fixed, known-safe fallback used when a scrubbed summary would still trip the
#: receipt ``safeText`` backstop.  Guarantees a receipt is always emittable
#: without ever letting an unscrubbed shape ride through.
_SAFE_FALLBACK = "The plugin host reported an error; details are withheld for safety."


# NOTE (shared-redaction follow-up): the scheme-less single-label ``host:port``
# shape (``serving:8443``, ``neo4j:7687`` — a tailnet compose service name) is
# caught HERE by the receipt ``safeText`` backstop (see the lowercase-anchored
# ``[a-z][a-z0-9-]*:\d{2,5}`` alternative in plugin-receipt.v1.schema.json), which
# downgrades such a line to the fixed safe fallback.  The SHARED
# :func:`workbench.redaction.redact_config_text` still only removes a *dotted*
# ``host:port`` (its host rule requires >=1 dot), so a dotless label:port survives
# that scrub.  Extending the shared scrubber with the same coverage is a deliberate
# careful follow-up (NOT done here): it is used by five lanes and must first gain a
# timestamp/digest exclusion (``sha256:...``, an ISO ``T09:15``) so it does not
# over-redact legitimate audit fields.  This lane fixes only its own backstop.
def _safe_receipt_summary(raw: str, fallback: str = _SAFE_FALLBACK) -> str:
    """Return a bounded, scrubbed, ``safeText``-valid summary for a receipt.

    Runs the configuration/health scrub first (which neutralizes the credential,
    endpoint, and path corpus), bounds the result, then enforces the receipt
    contract's structural backstop.  If anything survives that the backstop still
    forbids (e.g. a residual ``key=value`` shape, or a scheme-less ``host:port``
    the dotted-host scrub leaves), the fixed safe fallback is used — a receipt is
    always safe to persist and serve, never a channel for a leak.
    """
    redacted = redact_config_text(raw).strip()[:400]
    if not redacted:
        return fallback
    if _SAFE_TEXT.fullmatch(redacted) is None:
        return fallback
    return redacted


# --------------------------------------------------------------------------- #
# Approved-catalog + capability discovery (T002 criterion 1).
# --------------------------------------------------------------------------- #

#: Only an operator-reviewed local file is a trusted plugin catalog origin.  A
#: browser- or model-supplied URL is never a source; ``local_json`` is the sole
#: implemented transport, mirroring ``ProviderCatalogRegistry``.
_IMPLEMENTED_TRANSPORTS = frozenset({"local_json"})


@dataclass(frozen=True)
class PluginCatalogSource:
    """One operator-reviewed local origin for the reviewed plugin catalog.

    ``location`` is a catalog file path.  It is bridge/operator configuration,
    never a browser- or model-supplied value, and it is never part of any
    published projection.
    """

    transport: str
    location: str

    def __post_init__(self) -> None:
        if self.transport not in _IMPLEMENTED_TRANSPORTS:
            raise PluginHostError(
                "unsupported_transport", f"plugin catalog source transport is not implemented: {self.transport!r}"
            )
        if not self.location.strip():
            raise PluginHostError("empty_source", "plugin catalog source has no location")


def _load_local_json(source: PluginCatalogSource, cwd: Path | None) -> Any:
    path = Path(source.location)
    if not path.is_absolute() and cwd is not None:
        path = cwd / path
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        # Never echo the path (topology leak); name the file basename only.
        raise PluginHostError("unreadable_source", f"plugin catalog file is unreadable: {path.name}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PluginHostError("malformed_source", "plugin catalog file is not valid JSON") from exc


@dataclass(frozen=True)
class DiscoveredPlugin:
    """The exact reviewed catalog plugin (and optional tool) a request resolved to.

    Holds deep copies of the reviewed catalog entries so a caller cannot mutate
    the registry's cached catalog through a returned reference.
    """

    plugin: Mapping[str, Any]
    tool: Mapping[str, Any] | None = None

    @property
    def plugin_id(self) -> str:
        return str(self.plugin["id"])

    @property
    def plugin_digest(self) -> str:
        return str(self.plugin["plugin_digest"])


class PluginDiscovery:
    """Discover exact approved plugin/tool entries; refuse everything else.

    Composes the reviewed catalog (what the operator installed and signed) with
    the enable-only capability profile (what an actor may use).  A resolution
    succeeds only when the exact id+digest is present in the catalog AND enabled
    in the profile; every other case fails closed on its own typed reason so a
    caller can distinguish an unknown plugin from a drifted digest from a
    not-enabled one.
    """

    def __init__(self, catalog: Mapping[str, Any], capability: Mapping[str, Any]) -> None:
        # Fail closed on either document up front: a drifted or unsafe catalog or
        # profile is refused before any lookup is answered.
        validate_plugin_catalog(catalog)
        validate_plugin_capability(capability)
        self._catalog = copy.deepcopy(dict(catalog))
        self._capability = copy.deepcopy(dict(capability))
        self._by_id: dict[str, Mapping[str, Any]] = {
            str(plugin["id"]): plugin for plugin in self._catalog["plugins"]
        }
        self._enabled: dict[tuple[str, str], frozenset[str]] = {
            (str(entry["plugin_id"]), str(entry["plugin_digest"])): frozenset(entry["enabled_tools"])
            for entry in self._capability["plugins"]
        }

    @classmethod
    def from_sources(
        cls,
        catalog_source: PluginCatalogSource,
        capability_source: PluginCatalogSource,
        cwd: Path | None = None,
    ) -> "PluginDiscovery":
        """Build discovery from two operator-reviewed local JSON files."""
        catalog = _load_local_json(catalog_source, cwd)
        capability = _load_local_json(capability_source, cwd)
        return cls(catalog, capability)

    def resolve(self, plugin_id: str, plugin_digest: str, tool_id: str | None = None) -> DiscoveredPlugin:
        """Return the exact approved entry, or fail closed on a typed reason.

        ``plugin_id``/``plugin_digest`` (and ``tool_id`` when given) are the only
        inputs — never a source, path, or URL — so a caller cannot request an
        arbitrary origin, only an exact reviewed entry (criterion 1).
        """
        plugin = self._by_id.get(str(plugin_id))
        if plugin is None:
            raise PluginHostError("unknown_plugin", "no reviewed plugin matches the requested id")
        if str(plugin["plugin_digest"]) != str(plugin_digest):
            # The id exists but the pinned digest drifted from the reviewed
            # catalog: refuse on the digest reason, never silently serve the
            # catalog's current digest.
            raise PluginHostError("digest_drift", "the pinned plugin digest does not match the reviewed catalog")
        enabled_tools = self._enabled.get((str(plugin_id), str(plugin_digest)))
        if enabled_tools is None:
            raise PluginHostError("not_enabled", "the plugin is reviewed but not enabled by the capability profile")
        tool: Mapping[str, Any] | None = None
        if tool_id is not None:
            tool = next((item for item in plugin["tools"] if str(item["tool_id"]) == str(tool_id)), None)
            if tool is None:
                raise PluginHostError("unknown_tool", "no reviewed tool matches the requested id in this plugin")
            if str(tool_id) not in enabled_tools:
                raise PluginHostError("tool_not_enabled", "the tool is reviewed but not enabled by the capability profile")
        return DiscoveredPlugin(plugin=copy.deepcopy(dict(plugin)), tool=copy.deepcopy(dict(tool)) if tool else None)

    def published(self) -> list[dict[str, Any]]:
        """The redacted browser projection of every approved, enabled plugin.

        Exposes identifiers, digests, versions, declared effect classes, gate
        sets, typed I/O schemas, and declared data/host/credential *references*
        — never a credential value (the closed catalog schema makes one
        unrepresentable), and only entries the profile actually enables.  Prose
        fields are untrusted display data; the API last hop scrubs them again.
        """
        published: list[dict[str, Any]] = []
        for (plugin_id, plugin_digest), enabled_tools in sorted(self._enabled.items()):
            plugin = self._by_id.get(plugin_id)
            if plugin is None or str(plugin["plugin_digest"]) != plugin_digest:
                # A profile entry pinning a digest the catalog no longer carries
                # is not published (fail closed by omission), never surfaced with
                # the wrong entry.
                continue
            published.append(_published_plugin(plugin, enabled_tools))
        return published


def _published_plugin(plugin: Mapping[str, Any], enabled_tools: frozenset[str]) -> dict[str, Any]:
    """Safe metadata projection for one plugin, limited to its enabled tools."""
    credential = plugin.get("credential", {})
    tools = [
        {
            "tool_id": tool["tool_id"],
            "title": tool["title"],
            "summary": tool["summary"],
            "effect": tool["effect"],
            "gates": copy.deepcopy(dict(tool["gates"])),
            "data_access": list(tool.get("data_access", [])),
            "input_schema": copy.deepcopy(dict(tool["input_schema"])),
            "output_schema": copy.deepcopy(dict(tool["output_schema"])),
        }
        for tool in plugin["tools"]
        if str(tool["tool_id"]) in enabled_tools
    ]
    return {
        "plugin_id": plugin["id"],
        "title": plugin["title"],
        "version": plugin["version"],
        "plugin_digest": plugin["plugin_digest"],
        "publisher": copy.deepcopy(dict(plugin["publisher"])),
        "description": plugin["description"],
        "support_status": plugin["support_status"],
        # Credential handling is reported by REFERENCE only (requirement, owning
        # host, opaque reference ids); the catalog schema makes a value
        # unrepresentable, so no secret is in this projection.
        "credential": {
            "requirement": credential.get("requirement", "none"),
            **({"owner_host": credential["owner_host"]} if "owner_host" in credential else {}),
            **({"credential_refs": list(credential["credential_refs"])} if "credential_refs" in credential else {}),
        },
        "tools": tools,
    }


# --------------------------------------------------------------------------- #
# Credential isolation (T002 criterion 2 / T003 criterion 1-2).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CredentialHandle:
    """An opaque host-owned reference to a credential — never the value.

    A handle names the credential by its declared reference id and owning host
    and carries a bounded opaque token minted by the host for this install only.
    There is deliberately no value/secret/key/token-material field: a credential
    value is not representable, so it can never be serialized or cross the
    browser/model boundary (T003 criterion 1).
    """

    owner_host: str
    ref: str
    handle: str


class CredentialBroker:
    """Resolve a plugin's OWN declared credential references — nothing else.

    Seeded with the reference ids each host actually owns
    (``{owner_host: {ref, ...}}``).  A plugin's catalog entry names only its own
    ``owner_host`` and ``credential_refs``; the broker resolves exactly those and
    refuses any reference the named host does not own, so a Workbench/bridge
    secret, a provider credential, or another plugin's reference is structurally
    unreachable (they are never among this plugin's declared refs).  The broker
    only ever handles reference ids and mints opaque tokens — a raw credential
    value never enters it.
    """

    def __init__(self, owned: Mapping[str, Sequence[str]]) -> None:
        self._owned: dict[str, frozenset[str]] = {
            str(host): frozenset(str(ref) for ref in refs) for host, refs in owned.items()
        }

    def resolve(self, plugin: Mapping[str, Any]) -> tuple[CredentialHandle, ...]:
        """Mint opaque handles for the plugin's declared refs; fail closed otherwise."""
        credential = plugin.get("credential", {})
        if credential.get("requirement") != "host_owned":
            return ()
        owner_host = str(credential["owner_host"])
        owned = self._owned.get(owner_host)
        if owned is None:
            raise PluginHostError("unknown_host", "the plugin's declared credential host is not configured")
        handles: list[CredentialHandle] = []
        for ref in credential.get("credential_refs", []):
            ref = str(ref)
            if ref not in owned:
                # Scope mismatch: a declared reference the host does not own is
                # refused BEFORE any dispatch (T003 criterion 2).
                raise PluginHostError(
                    "credential_unavailable", "a declared credential reference is not owned by the plugin's host"
                )
            handles.append(CredentialHandle(owner_host=owner_host, ref=ref, handle=secrets.token_hex(16)))
        return tuple(handles)


# --------------------------------------------------------------------------- #
# The isolated install lifecycle (T002 criterion 3).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HostInstallOutcome:
    """The typed result an isolated host runner returns for one install.

    ``status`` is ``installed`` (the effect completed) or ``unknown`` (an
    in-flight effect whose outcome the host cannot confirm — routed to
    reconciliation, never reported as a false success).  ``output`` is an opaque
    host payload reduced to a digest; it never enters the receipt as prose.
    """

    status: str = "installed"
    output: Mapping[str, Any] = field(default_factory=dict)
    summary: str = ""
    reconcile_code: str = "install_outcome_unknown"


#: A host runner takes the discovered plugin and its resolved credential handles
#: and returns a :class:`HostInstallOutcome`, or raises :class:`PluginHostFailure`.
HostRunner = Callable[[DiscoveredPlugin, "tuple[CredentialHandle, ...]"], HostInstallOutcome]


def _lifecycle_effect(kind: str) -> str:
    return "plugin_lifecycle"


def _credential_use(plugin: Mapping[str, Any]) -> dict[str, Any]:
    """Report the plugin's credential use by reference only (R004)."""
    credential = plugin.get("credential", {})
    if credential.get("requirement") != "host_owned":
        return {"requirement": "none"}
    return {
        "requirement": "host_owned",
        "owner_host": credential["owner_host"],
        "credential_refs": list(credential["credential_refs"]),
    }


def _output_digest(output: Mapping[str, Any]) -> str:
    payload = json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(b"anvil-workbench/plugin-output/v1\0" + payload).hexdigest()


@dataclass
class PluginHostRows:
    """The persisted receipt container, keyed by request digest (idempotency)."""

    receipts: dict[str, dict[str, Any]] = field(default_factory=dict)


class MemoryPluginHostStore:
    """Hermetic, isolated install lifecycle with fail-closed replay/drift/failure.

    A single install path (:meth:`install`) runs the whole preflight-run-persist
    sequence under a per-instance reentrant lock, so two concurrent identical
    requests resolve to exactly one host effect and one persisted receipt: the
    first commits, the second observes the committed receipt and replays it as a
    ``duplicate`` (mirroring :class:`workbench.idempotency_store.MemoryIdempotencyStore`).
    A production host follows the same discipline with a durable unique
    constraint on the request digest; it is not implemented here.
    """

    def __init__(self, rows: PluginHostRows | None = None) -> None:
        self._lock = threading.RLock()
        self.rows = rows if rows is not None else PluginHostRows()

    def install(
        self,
        request: Mapping[str, Any],
        discovery: PluginDiscovery,
        broker: CredentialBroker,
        host_runner: HostRunner,
    ) -> dict[str, Any]:
        """Install one reviewed plugin idempotently and fail closed on drift/failure.

        Returns a redacted, schema-valid ``plugin-receipt``.  The sequence:

        1. Fail closed unless the request is a schema-valid, digest-consistent
           ``install`` (``validate_plugin_request``).
        2. Replay: an identical ``request_digest`` returns the prior receipt as a
           ``duplicate`` without re-running the host.
        3. Resolve the exact approved catalog entry; an unknown/drifted/not-enabled
           plugin is a ``denied`` receipt on its typed reason.
        4. Resolve ONLY the plugin's own credential references into opaque
           handles; a scope-mismatched or unavailable reference is a ``denied``
           receipt before any host effect.
        5. Run the isolated host: success -> ``accepted``; a host failure ->
           ``denied`` (persist nothing, retriable); an unknown outcome ->
           persisted ``reconcile`` (R012).
        """
        with self._lock:
            return self._install_locked(request, discovery, broker, host_runner)

    def _install_locked(
        self,
        request: Mapping[str, Any],
        discovery: PluginDiscovery,
        broker: CredentialBroker,
        host_runner: HostRunner,
    ) -> dict[str, Any]:
        try:
            validate_plugin_request(request)
        except ContractValidationError as exc:
            raise PluginHostError("invalid_request", f"plugin request failed validation: {exc}") from exc
        if request.get("kind") != "install":
            raise PluginHostError("unsupported_kind", "this lifecycle path installs only")

        request_digest = str(request["request_digest"])
        prior = self.rows.receipts.get(request_digest)
        if prior is not None:
            # Idempotent replay: never re-run the host.  A prior accepted install
            # replays as a duplicate; a prior reconcile replays as itself (the
            # in-flight outcome is still unknown until reconciliation resolves it).
            if prior["status"] == "accepted":
                return self._duplicate_of(prior, request)
            return copy.deepcopy(prior)

        plugin_ref = request["plugin"]
        try:
            discovered = discovery.resolve(str(plugin_ref["plugin_id"]), str(plugin_ref["plugin_digest"]))
        except PluginHostError as exc:
            return self._denied(request, exc.code, str(exc), retryable=exc.code == "not_enabled")

        try:
            handles = broker.resolve(discovered.plugin)
        except PluginHostError as exc:
            return self._denied(request, exc.code, str(exc), retryable=True)

        try:
            outcome = host_runner(discovered, handles)
        except PluginHostFailure as exc:
            # A host failure persists nothing so the request stays retriable, and
            # the untrusted host detail is scrubbed before it enters the receipt.
            return self._denied(request, exc.code, _safe_receipt_summary(exc.detail), retryable=True)

        if outcome.status == "unknown":
            receipt = self._reconcile(request, outcome)
            self.rows.receipts[request_digest] = copy.deepcopy(receipt)
            return receipt

        receipt = self._accepted(request, discovered.plugin, outcome)
        self.rows.receipts[request_digest] = copy.deepcopy(receipt)
        return receipt

    def receipt(self, request_digest: str) -> dict[str, Any] | None:
        """Return a stored receipt by request digest, or ``None`` if absent."""
        with self._lock:
            stored = self.rows.receipts.get(str(request_digest))
            return copy.deepcopy(stored) if stored is not None else None

    # --- receipt builders -------------------------------------------------- #

    def _finalize(self, receipt: dict[str, Any], request: Mapping[str, Any]) -> dict[str, Any]:
        validate_plugin_receipt(receipt, request)
        return receipt

    def _base(self, request: Mapping[str, Any], status: str) -> dict[str, Any]:
        plugin_ref = request["plugin"]
        return {
            "schema_version": "workbench-plugin-receipt/v1",
            "receipt_id": new_id("plugrcpt"),
            "request_digest": request["request_digest"],
            "kind": request["kind"],
            "plugin": {
                "plugin_id": plugin_ref["plugin_id"],
                "plugin_digest": plugin_ref["plugin_digest"],
            },
            "status": status,
            "effect": _lifecycle_effect(str(request["kind"])),
            "redaction": {"status": "redacted"},
            "completed_at": now_utc().isoformat(),
        }

    def _accepted(
        self, request: Mapping[str, Any], plugin: Mapping[str, Any], outcome: HostInstallOutcome
    ) -> dict[str, Any]:
        receipt = self._base(request, "accepted")
        receipt["credential_use"] = _credential_use(plugin)
        result: dict[str, Any] = {"output_digest": _output_digest(outcome.output)}
        summary = _safe_receipt_summary(outcome.summary, fallback="") if outcome.summary else ""
        if summary:
            result["output_summary"] = summary
        receipt["result"] = result
        return self._finalize(receipt, request)

    def _duplicate_of(self, prior: Mapping[str, Any], request: Mapping[str, Any]) -> dict[str, Any]:
        receipt = self._base(request, "duplicate")
        if "credential_use" in prior:
            receipt["credential_use"] = copy.deepcopy(prior["credential_use"])
        receipt["result"] = copy.deepcopy(prior["result"])
        return self._finalize(receipt, request)

    def _denied(self, request: Mapping[str, Any], code: str, summary: str, retryable: bool) -> dict[str, Any]:
        receipt = self._base(request, "denied")
        receipt["error"] = {
            "code": code,
            "safe_summary": _safe_receipt_summary(summary),
            "retryable": retryable,
        }
        return self._finalize(receipt, request)

    def _reconcile(self, request: Mapping[str, Any], outcome: HostInstallOutcome) -> dict[str, Any]:
        receipt = self._base(request, "reconcile")
        receipt["reconciliation"] = {
            "code": outcome.reconcile_code,
            "safe_summary": _safe_receipt_summary(
                outcome.summary or "the install outcome is unknown and awaits reconciliation"
            ),
        }
        return self._finalize(receipt, request)


# --------------------------------------------------------------------------- #
# The service facade the (not-wired) browser router calls.
# --------------------------------------------------------------------------- #


class PluginHostService:
    """Bind discovery + the isolated lifecycle store for the hub API surface.

    A read-only discovery projection and a stored-receipt lookup are what the
    browser may see; the install effect is a bridge/host concern exercised at
    this service layer, never a browser mutation path.  A credential value is
    never accepted from nor returned to the browser: the discovery projection and
    the receipt both report credentials by opaque reference only.
    """

    def __init__(self, discovery: PluginDiscovery, store: MemoryPluginHostStore | None = None) -> None:
        self._discovery = discovery
        self._store = store if store is not None else MemoryPluginHostStore()

    @classmethod
    def from_files(
        cls, catalog_path: str, capability_path: str, cwd: Path | None = None
    ) -> "PluginHostService":
        """Build the service from two operator-reviewed local JSON files.

        Mirrors :meth:`workbench.provider_catalogs.ProviderCatalogRegistry.from_settings`:
        the operator declares the reviewed catalog and capability files; the
        service loads and fail-closed validates them at construction.
        """
        discovery = PluginDiscovery.from_sources(
            PluginCatalogSource("local_json", catalog_path),
            PluginCatalogSource("local_json", capability_path),
            cwd=cwd,
        )
        return cls(discovery)

    @property
    def store(self) -> MemoryPluginHostStore:
        return self._store

    def list_plugins(self) -> list[dict[str, Any]]:
        return self._discovery.published()

    def get_plugin(self, plugin_id: str) -> dict[str, Any] | None:
        for plugin in self._discovery.published():
            if plugin["plugin_id"] == plugin_id:
                return plugin
        return None

    def get_receipt(self, request_digest: str) -> dict[str, Any] | None:
        return self._store.receipt(request_digest)
