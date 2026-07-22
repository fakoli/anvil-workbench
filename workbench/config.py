"""Environment-only Workbench configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _csv(name: str, env: dict[str, str]) -> frozenset[str]:
    return frozenset(item.strip() for item in env.get(name, "").split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    database_url: str
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    owner: str
    approvers: frozenset[str]
    bridge_bootstrap_token: str
    anvil_router_base_url: str
    anvil_router_token: str
    sandbox_models: frozenset[str] = frozenset()
    anvil_voice_realtime_url: str = ""
    anvil_voice_realtime_token: str = ""
    #: Operator-declared http(s) URL of the TTS serve's voice-catalog endpoint
    #: (e.g. ``http://<kokoro-host>/v1/audio/voices``).  When set, the hub exposes
    #: an actor-gated ``GET /api/chat/voice/voices`` that enumerates the selectable
    #: TTS voices for the Voice tab; the browser never hits the serve directly.
    #: Unset means voice selection is not configured and that endpoint fails closed.
    anvil_voice_voices_url: str = ""
    voice_retain_transcripts: bool = False
    embedding_model: str = ""
    rerank_model: str = ""
    identity_header: str = "Tailscale-User-Login"
    allow_insecure_dev_actor: bool = False
    #: Hub-held key for the keyed chat content fingerprint (PRD R008).  Unset
    #: means chat persistence is not configured and chat endpoints refuse.
    chat_content_hash_key: str = ""
    #: Operator-reviewed JSON array of allowed Anvil Serving chat routes
    #: (chat-first-voice T003.1).  Parsed and fail-closed validated by
    #: :mod:`workbench.chat_routes`; unset means no chat route is allowed.
    chat_routes: str = ""
    #: Operator-reviewed local paths to the signed reviewed plugin catalog and the
    #: enable-only capability profile (reviewed-tools-plugins T002).  They are the
    #: operator-declared trust root for :class:`workbench.plugin_host.PluginHostService`,
    #: mirroring the provider-catalog local-JSON precedent.  When BOTH are declared,
    #: ``create_app`` builds the read-only discovery surface from them (an injected
    #: service overrides for tests); if EITHER is unset the plugin host is not
    #: configured and its browser surface fails closed (503).  This wires only the
    #: operator-declared read-only discovery projection -- the install effect stays a
    #: service/bridge concern and is deliberately not on the live poll loop.
    plugin_catalog_file: str = ""
    plugin_capability_file: str = ""
    #: Operator-declared LOCAL path to a reviewed, digest-pinned OpenAPI document
    #: (reviewed-tools-plugins T009 / R016).  This is a REVIEW-TIME-ONLY input:
    #: it is consumed by the OpenAPI -> descriptor compiler
    #: (:func:`workbench.contracts.compile_openapi_read_connector_plugin`) during
    #: operator catalog review to produce the reviewed ``plugin_catalog_file``.
    #: ``create_app`` NEVER reads it -- there is deliberately no runtime or browser
    #: path that ingests an OpenAPI URL or document; only the already-compiled,
    #: digest-pinned catalog is loaded at runtime.  A URL / remote value is refused
    #: by :func:`reviewed_openapi_source_location` (a document is a local reviewed
    #: file, never fetched live).
    plugin_openapi_document_file: str = ""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        values = dict(os.environ if env is None else env)
        owner = values.get("WORKBENCH_OWNER", "operator").strip() or "operator"
        approvers = _csv("WORKBENCH_APPROVERS", values) | frozenset({owner})
        return cls(
            database_url=values.get("WORKBENCH_DATABASE_URL", "postgresql://workbench:workbench@db:5432/workbench"),
            neo4j_uri=values.get("WORKBENCH_NEO4J_URI", "bolt://neo4j:7687"),
            neo4j_user=values.get("WORKBENCH_NEO4J_USER", "neo4j"),
            neo4j_password=values.get("WORKBENCH_NEO4J_PASSWORD", ""),
            owner=owner,
            approvers=approvers,
            bridge_bootstrap_token=values.get("WORKBENCH_BRIDGE_BOOTSTRAP_TOKEN", ""),
            anvil_router_base_url=values.get("ANVIL_ROUTER_BASE_URL", ""),
            anvil_router_token=values.get("ANVIL_ROUTER_TOKEN", ""),
            sandbox_models=_csv("WORKBENCH_SANDBOX_MODELS", values),
            anvil_voice_realtime_url=values.get("ANVIL_VOICE_REALTIME_URL", "").strip(),
            anvil_voice_realtime_token=values.get("ANVIL_VOICE_REALTIME_TOKEN", ""),
            anvil_voice_voices_url=values.get("ANVIL_VOICE_VOICES_URL", "").strip(),
            voice_retain_transcripts=values.get("WORKBENCH_VOICE_RETAIN_TRANSCRIPTS", "").lower() in {"1", "true", "yes"},
            embedding_model=values.get("WORKBENCH_EMBEDDING_MODEL", ""),
            rerank_model=values.get("WORKBENCH_RERANK_MODEL", ""),
            identity_header=values.get("WORKBENCH_IDENTITY_HEADER", "Tailscale-User-Login").strip() or "Tailscale-User-Login",
            allow_insecure_dev_actor=values.get("WORKBENCH_ALLOW_INSECURE_DEV_ACTOR", "").lower() in {"1", "true", "yes"},
            chat_content_hash_key=values.get("WORKBENCH_CHAT_HASH_KEY", ""),
            chat_routes=values.get("WORKBENCH_CHAT_ROUTES", "").strip(),
            plugin_catalog_file=values.get("WORKBENCH_PLUGIN_CATALOG_FILE", "").strip(),
            plugin_capability_file=values.get("WORKBENCH_PLUGIN_CAPABILITY_FILE", "").strip(),
            plugin_openapi_document_file=values.get("WORKBENCH_PLUGIN_OPENAPI_DOCUMENT_FILE", "").strip(),
        )


def reviewed_openapi_source_location(location: str) -> str:
    """Resolve the operator-declared LOCAL reviewed OpenAPI document location.

    Review-time only: the returned path is consumed by the OpenAPI -> descriptor
    compiler during operator catalog review, NEVER by ``create_app`` at runtime.
    A URL or remote transport is refused -- a reviewed OpenAPI document is a
    local, digest-pinned, operator-reviewed file, never fetched live from a
    browser- or hub-supplied URL (R016).
    """
    text = str(location).strip()
    if not text:
        raise ValueError("no reviewed OpenAPI document location is configured")
    lowered = text.lower()
    if "://" in lowered or lowered.startswith("//"):
        raise ValueError("a reviewed OpenAPI document must be a local path, never a URL")
    return text
