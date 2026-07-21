"""Private tailnet API for the Workbench hub.

The browser only receives redacted data and never receives a model, GitHub, or
bridge credential.  An identity-aware tailnet proxy should set
``X-Workbench-Actor``; the development fallback is the configured owner.
"""
from __future__ import annotations

import hashlib
from typing import Any, Callable, Mapping

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Path, Request, WebSocket, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .config import Settings
from .contracts import settings_actor_view
from .conversation_api import (
    build_conversation_router,
    build_hub_retention_router,
    register_conversation_handlers,
)
from .conversation_store import ConversationStore, MemoryConversationStore
from .delivery_projection import (
    DeliveryProjectionStore,
    UnknownDeliveryRecordError,
)
from .directives import session_directive_view, submit_directive
from .idempotency_store import IdempotencyStore, MemoryIdempotencyStore
from .plugin_host import PluginHostService
from .project_context_store import ProjectContextStore, UnknownProjectionError
from .run_context_store import RunContextStore, UnknownRunContextError
from .graph import EvidenceGraph, Neo4jEvidenceGraph, NullGraph
from .models import (
    PreferenceValidationError, as_json, resolve_effective_settings, reviewed_catalog_valid_refs,
)
from .retrieval import AnvilPurposeRetrieval
from .router import RouterError, route_decisions, sandbox_response
from .redaction import scrub_config_payload
from .store import (
    MemoryPreferenceStore, PostgresStore, PreferenceStoreError, StalePreferenceWriteError,
    StoreError, UnknownPreferenceError, WorkbenchStore,
)
from .system_health import SystemHealthService, UnknownIntegrationError
from .voice import relay_realtime


def default_delivery_workflow(skills: list[str] | None = None) -> dict[str, Any]:
    """Small reviewed workflow used when a session does not supply a template."""
    return {
        "entry": "implement",
        "steps": [
            {"id": "implement", "kind": "agent", "model": "planning", "skills": skills or [], "next": ["review"]},
            {"id": "review", "kind": "approval_wait", "next": ["reconcile"]},
            {"id": "reconcile", "kind": "reconcile", "next": []},
        ],
    }


class ProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    state_root: str = Field(min_length=1, max_length=1024)


class BridgeInput(BaseModel):
    name: str = Field(min_length=1, max_length=160)


class RunInput(BaseModel):
    project_id: str
    task_id: str | None = None
    model: str = Field(min_length=1, max_length=240)


class SessionInput(BaseModel):
    project_id: str
    title: str = Field(min_length=1, max_length=160)
    worktree_id: str = Field(min_length=1, max_length=160)
    workflow_definition: dict[str, Any] | None = None
    skills: list[str] = Field(default_factory=list, max_length=16)


class WorkflowRevisionInput(BaseModel):
    expected_version: int = Field(ge=1)
    definition: dict[str, Any]


class WorkflowStartInput(BaseModel):
    task_id: str = Field(min_length=1, max_length=300)
    model: str = Field(default="planning", min_length=1, max_length=240)


class WorkflowStepInput(BaseModel):
    outcome: str = Field(pattern="^(succeeded|failed|cancelled)$")


class ApprovalInput(BaseModel):
    project_id: str
    action_type: str = Field(pattern="^(commit_pr|merge_and_accept|state_apply|deploy|model_policy)$")
    payload: dict[str, Any]
    ttl_seconds: int = Field(default=900, ge=30, le=86_400)
    bridge_id: str | None = None


class BridgeEvent(BaseModel):
    run_id: str
    role: str = Field(min_length=1, max_length=80)
    content: Any


class RunStatusInput(BaseModel):
    status: str = Field(pattern="^(running|reconciliation)$")


class RunFinalizationInput(BaseModel):
    status: str = Field(pattern="^(evidenced|reconciliation)$")
    command_id: str = Field(min_length=1, max_length=300)


class EvidenceInput(BaseModel):
    source_kind: str = Field(pattern="^(state_event|work_packet|route|evaluation|pull_request|approval|failure)$")
    source_id: str = Field(min_length=1, max_length=300)
    project_id: str
    payload: dict[str, Any]


class DirectiveInput(BaseModel):
    content: str = Field(min_length=1, max_length=8_000)


class BridgeSkillInput(BaseModel):
    skill_id: str = Field(pattern="^[a-zA-Z0-9][a-zA-Z0-9:_-]{0,119}$")
    description: str = Field(min_length=1, max_length=500)
    content_sha256: str = Field(pattern="^[a-f0-9]{64}$")


class BridgeSkillsInput(BaseModel):
    skills: list[BridgeSkillInput] = Field(default_factory=list, max_length=128)


class SandboxInput(BaseModel):
    model: str = Field(min_length=1, max_length=240)
    input: str = Field(min_length=1, max_length=8_000)


def _error(exc: StoreError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


def _store(settings: Settings) -> WorkbenchStore:
    store = PostgresStore(settings.database_url)
    store.initialize()
    return store


def _conversation_store(settings: Settings) -> ConversationStore | None:
    """Build chat persistence only when the hub holds the content-hash key.

    The key is hub configuration (``WORKBENCH_CHAT_HASH_KEY``); it is passed
    to the store constructor and held on the instance only, never persisted
    with the rows.  Without a key there is no store and the chat endpoints
    fail closed with 503.  A configured-but-invalid key raises loudly here
    instead of serving unkeyed fingerprints.
    """
    if not settings.chat_content_hash_key:
        return None
    return MemoryConversationStore(
        content_hash_key=settings.chat_content_hash_key.encode("utf-8"), recover_on_open=True,
    )


def _plugin_host_service(settings: Settings) -> "PluginHostService | None":
    """Build the read-only reviewed-plugin discovery surface from operator config.

    Mirrors how the provider catalog and system health derive from operator-
    declared config: when BOTH the reviewed catalog and the enable-only capability
    profile paths are declared, load and fail-closed validate them into a
    :class:`PluginHostService`.  This is operator trust-root config, not live-loop
    wiring — the service is a read-only discovery + stored-receipt surface, the
    router stays GET-only, and the install effect remains a service/bridge concern.
    When either path is unset the plugin host is not configured and stays ``None``
    so the browser surface fails closed (503).  A configured-but-invalid file
    raises loudly here rather than serving a drifted catalog.
    """
    if not settings.plugin_catalog_file or not settings.plugin_capability_file:
        return None
    return PluginHostService.from_files(
        settings.plugin_catalog_file, settings.plugin_capability_file,
    )


def _graph(settings: Settings) -> EvidenceGraph:
    if not settings.neo4j_password:
        return NullGraph()
    retrieval = None
    if settings.anvil_router_base_url and settings.anvil_router_token and settings.embedding_model:
        retrieval = AnvilPurposeRetrieval(
            settings.anvil_router_base_url, settings.anvil_router_token,
            settings.embedding_model, settings.rerank_model or None,
        )
    return Neo4jEvidenceGraph(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password, retrieval=retrieval)


#: The single non-leaking body every unknown-or-foreign project-context
#: lookup gets, so a cross-project probe cannot distinguish "missing" from
#: "belongs to another project".
_UNKNOWN_PROJECTION_DETAIL = "unknown project context"

#: The project-id / digest path grammars, mirrored from the projection and its
#: store so a malformed scope is rejected at the edge (422) before it can reach
#: the store as a distinguishable error.
_PROJECT_ID_PATTERN = r"^[a-zA-Z0-9._-]{1,128}$"
_SOURCE_DIGEST_PATTERN = r"^sha256:[a-f0-9]{64}$"

#: The single non-leaking body every unknown-or-foreign run-context lookup gets,
#: so a cross-project probe cannot distinguish "missing" from "belongs to
#: another project".
_UNKNOWN_RUN_CONTEXT_DETAIL = "unknown run context"

#: The run-id path grammar, mirrored from the run context's identity grammar so a
#: malformed run id is rejected at the edge (422) before it can reach the store
#: as a distinguishable error.
_RUN_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$"


def build_project_context_router(
    actor_dependency: Callable[..., str],
    project_context_store: ProjectContextStore | None,
) -> APIRouter:
    """Build the read-only, project-scoped context-projection browser surface.

    Every endpoint is authenticated by the hub's trusted ``actor`` dependency
    (tailnet identity + allowlist) and scoped by the ``project_id`` path
    segment.  Responses serialize only the explicitly non-canonical display
    projection (:meth:`ProjectContextProjection.as_dict`), whose closed field
    set structurally cannot carry a State storage path, a credential-bearing
    field, a token, or a raw executable provider payload — the projection is a
    display read-model, never canonical authority.

    Project scoping is a hard boundary: a digest that belongs to another
    project is not in this project's namespace, so a cross-project read is
    refused with the same indistinct not-found a genuinely missing record
    raises (``UnknownProjectionError`` -> the fixed 404 body).  One project can
    never learn whether another project's context exists.

    When ``project_context_store`` is ``None`` the derived projection is not
    configured (it is deliberately not wired into the live bridge poll loop)
    and every endpoint refuses with 503, mirroring the unconfigured chat store.
    """
    router = APIRouter(prefix="/api/projects")

    def context_store() -> ProjectContextStore:
        if project_context_store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="project-context projection is not configured",
            )
        return project_context_store

    @router.get("/{project_id}/context")
    def latest_project_context(
        project_id: str = Path(pattern=_PROJECT_ID_PATTERN),
        _actor: str = Depends(actor_dependency),
    ) -> dict[str, Any]:
        """The acting project's latest non-canonical display projection."""
        return {"context": context_store().get_latest(project_id).as_dict()}

    @router.get("/{project_id}/context/{source_digest}")
    def project_context_by_digest(
        project_id: str = Path(pattern=_PROJECT_ID_PATTERN),
        source_digest: str = Path(pattern=_SOURCE_DIGEST_PATTERN),
        _actor: str = Depends(actor_dependency),
    ) -> dict[str, Any]:
        """One of the acting project's projections addressed by its source digest.

        A digest owned by another project resolves to the indistinct 404, so
        this detail endpoint is not an existence oracle for foreign records.
        """
        return {"context": context_store().get(project_id, source_digest).as_dict()}

    return router


def build_run_context_router(
    actor_dependency: Callable[..., str],
    run_context_store: RunContextStore | None,
) -> APIRouter:
    """Build the read-only, project-scoped historical run-context surface.

    A single endpoint returns the immutable queue-time run context captured for
    one run (state-context-operations:T005.3).  It reads ONLY the persisted
    snapshot, so a later task/PRD rename or catalog/route/skill refresh cannot
    change the titles, revisions, or digests it returns.  The serialized
    :meth:`RunContext.as_dict` keeps trusted execution policy and untrusted
    PRD/task data in two separately labeled structures whose closed field set
    structurally cannot carry a secret, host path, raw command, or provider
    payload.

    Project scoping is a hard boundary: a run that belongs to another project is
    not in this project's namespace, so a cross-project read is refused with the
    same indistinct not-found a genuinely missing run raises
    (``UnknownRunContextError`` -> the fixed 404 body).  When
    ``run_context_store`` is ``None`` the surface is not configured and refuses
    with 503, mirroring the project-context and chat stores.
    """
    router = APIRouter(prefix="/api/projects")

    def context_store() -> RunContextStore:
        if run_context_store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="run-context history is not configured",
            )
        return run_context_store

    @router.get("/{project_id}/runs/{run_id}/context")
    def historical_run_context(
        project_id: str = Path(pattern=_PROJECT_ID_PATTERN),
        run_id: str = Path(pattern=_RUN_ID_PATTERN),
        _actor: str = Depends(actor_dependency),
    ) -> dict[str, Any]:
        """The project's captured queue-time run context, read as stored.

        A run owned by another project resolves to the indistinct 404, so this
        endpoint is not an existence oracle for foreign runs.

        Last-hop redaction (security lens, mirroring
        :func:`build_system_health_router`): the ``untrusted`` PRD/task channel
        carries whatever prose a PRD title/criterion/scope/evidence happened to
        contain, so it is re-scrubbed here with
        :func:`~workbench.redaction.scrub_config_payload`.  Construction-time
        scrubbing already ran (so the store never held the secret), but this
        closes a rogue or duck-typed record whose ``as_dict()`` bypassed it. The
        ``trusted`` policy structure is left untouched -- it is a closed set of
        typed identifiers/digests/enums with no free-form host/secret field, and
        the config scrubber's path/URL patterns would otherwise mangle safe
        ``sha256:`` digests and ``.../v1`` versions.
        """
        context = context_store().get(project_id, run_id).as_dict()
        context["untrusted"] = scrub_config_payload(context["untrusted"])
        return {"context": context}

    return router


#: The single non-leaking body every unknown-or-foreign delivery-projection
#: lookup gets, so a cross-project probe cannot distinguish "missing" from
#: "belongs to another project".
_UNKNOWN_DELIVERY_DETAIL = "unknown delivery record"

#: The PRD / task / delivery-run / approval path grammars, mirrored from the
#: contract patterns so a malformed scope is rejected at the edge (422) before it
#: can reach the store as a distinguishable error.
_PRD_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"
_TASK_ID_PATTERN = r"^T[0-9]{3}(\.[0-9]{1,3})?$"
_DELIVERY_RUN_ID_PATTERN = r"^run_[a-zA-Z0-9_-]{8,128}$"
_APPROVAL_ID_PATTERN = r"^approval_[a-zA-Z0-9_-]{4,128}$"


def build_delivery_projection_router(
    actor_dependency: Callable[..., str],
    delivery_projection_store: DeliveryProjectionStore | None,
) -> APIRouter:
    """Build the read-only, project-scoped delivery display surface (T002 / T004).

    Serves four display read-models, all authenticated by the hub's trusted
    ``actor`` dependency and scoped by the ``project_id`` path segment: bounded
    redacted PRD content, scoped task references, delivery-eligibility verdicts,
    and the pinned operational run rows / approval bindings.  Task references and
    eligibility are keyed by ``(prd_id, task_id)`` so a ``T001`` in two PRDs can
    never collapse into one row (T002 criterion 1).  Eligibility reflects the
    current snapshot: when the task reference's source advanced, the served
    verdict is derived as ``stale.snapshot_superseded`` rather than replaying a
    superseded ``eligible`` verdict (criterion 2).

    Every response is scrubbed on this last hop with
    :func:`~workbench.redaction.scrub_config_payload`, so the untrusted PRD body,
    task title, or attempt label can never ferry a secret, endpoint, or path to
    the UI (criterion 3 / recurring redaction gate).  Project scoping is a hard
    boundary: a record owned by another project raises the same
    ``UnknownDeliveryRecordError`` a genuinely missing record raises, so this
    surface is never a cross-project existence oracle.  The surface is GET-only
    and fail-closed (503) until a projection store is configured — it is
    deliberately NOT wired into the live bridge poll loop.
    """
    router = APIRouter(prefix="/api/projects")

    def projection() -> DeliveryProjectionStore:
        if delivery_projection_store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="delivery projection is not configured",
            )
        return delivery_projection_store

    @router.get("/{project_id}/prds/{prd_id}/content")
    def prd_content(
        project_id: str = Path(pattern=_PROJECT_ID_PATTERN),
        prd_id: str = Path(pattern=_PRD_ID_PATTERN),
        _actor: str = Depends(actor_dependency),
    ) -> dict[str, Any]:
        """One PRD's bounded, untrusted content, redacted for safe rendering."""
        return scrub_config_payload({"content": projection().get_prd_content(project_id, prd_id)})

    @router.get("/{project_id}/prds/{prd_id}/tasks")
    def task_references(
        project_id: str = Path(pattern=_PROJECT_ID_PATTERN),
        prd_id: str = Path(pattern=_PRD_ID_PATTERN),
        _actor: str = Depends(actor_dependency),
    ) -> dict[str, Any]:
        """Every scoped task reference in the PRD's plan/feature hierarchy."""
        return scrub_config_payload({"tasks": projection().list_task_references(project_id, prd_id)})

    @router.get("/{project_id}/prds/{prd_id}/tasks/{task_id}")
    def task_reference(
        project_id: str = Path(pattern=_PROJECT_ID_PATTERN),
        prd_id: str = Path(pattern=_PRD_ID_PATTERN),
        task_id: str = Path(pattern=_TASK_ID_PATTERN),
        _actor: str = Depends(actor_dependency),
    ) -> dict[str, Any]:
        """One scoped task reference; a foreign/missing scope is the indistinct 404."""
        return scrub_config_payload({"task": projection().get_task_reference(project_id, prd_id, task_id)})

    @router.get("/{project_id}/prds/{prd_id}/tasks/{task_id}/eligibility")
    def task_eligibility(
        project_id: str = Path(pattern=_PROJECT_ID_PATTERN),
        prd_id: str = Path(pattern=_PRD_ID_PATTERN),
        task_id: str = Path(pattern=_TASK_ID_PATTERN),
        _actor: str = Depends(actor_dependency),
    ) -> dict[str, Any]:
        """The task's delivery-eligibility verdict, stale-checked against the source."""
        return scrub_config_payload(
            {"eligibility": projection().get_eligibility(project_id, prd_id, task_id)}
        )

    @router.get("/{project_id}/delivery/runs")
    def delivery_runs(
        project_id: str = Path(pattern=_PROJECT_ID_PATTERN),
        prd_id: str | None = None,
        task_id: str | None = None,
        run_status: str | None = None,
        route_digest: str | None = None,
        capability_profile_digest: str | None = None,
        since: str | None = None,
        until: str | None = None,
        _actor: str = Depends(actor_dependency),
    ) -> dict[str, Any]:
        """The project's pinned run rows, grouped/filtered by the query facets.

        Every row's headline is the pinned task title (never a bare id when a
        title exists), and repeated attempts for one task are distinguished by
        their human attempt label and start time (T004 criteria 1 and 3).
        """
        rows = projection().list_run_rows(
            project_id,
            prd_id=prd_id,
            task_id=task_id,
            status=run_status,
            route_digest=route_digest,
            capability_profile_digest=capability_profile_digest,
            since=since,
            until=until,
        )
        return scrub_config_payload({"runs": [row.as_dict() for row in rows]})

    @router.get("/{project_id}/delivery/runs/{run_id}")
    def delivery_run(
        project_id: str = Path(pattern=_PROJECT_ID_PATTERN),
        run_id: str = Path(pattern=_DELIVERY_RUN_ID_PATTERN),
        _actor: str = Depends(actor_dependency),
    ) -> dict[str, Any]:
        """One pinned run row; a foreign/missing run is the indistinct 404."""
        return scrub_config_payload({"run": projection().get_run_row(project_id, run_id).as_dict()})

    @router.get("/{project_id}/delivery/approvals/{approval_id}")
    def delivery_approval(
        project_id: str = Path(pattern=_PROJECT_ID_PATTERN),
        approval_id: str = Path(pattern=_APPROVAL_ID_PATTERN),
        _actor: str = Depends(actor_dependency),
    ) -> dict[str, Any]:
        """One approval binding, exposing every exact safe authorization binding."""
        return scrub_config_payload(
            {"approval": projection().get_approval_binding(project_id, approval_id).as_dict()}
        )

    return router


#: The integration-id path grammar, mirrored from
#: :data:`workbench.system_health.INTEGRATION_IDS`, so a malformed id is rejected
#: at the edge (422) before it reaches the service as a distinguishable lookup.
_INTEGRATION_ID_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"

#: The single fixed body an unknown-integration lookup returns.  The set of
#: declared integrations is a public, deployment-invariant catalog (never a
#: per-tenant secret), so this 404 is a plain not-found, not an existence oracle.
_UNKNOWN_INTEGRATION_DETAIL = "unknown integration"


def build_system_health_router(
    actor_dependency: Callable[..., str],
    health_service: SystemHealthService,
) -> APIRouter:
    """Build the read-only system-health and observational-posture browser surface.

    Every endpoint is authenticated by the hub's trusted ``actor`` dependency
    (tailnet identity + allowlist) and serializes only the closed
    :class:`~workbench.system_health.IntegrationDescriptor` /
    :class:`~workbench.system_health.PostureReport` display shapes, whose field
    sets structurally cannot carry a credential, a raw endpoint URL, a local
    path, an approval, or an execution surface.

    Redaction guarantee (T003.1 criterion 2, security lens): the browser-facing
    scrub is enforced here, at the serialized API boundary, by
    :func:`~workbench.redaction.scrub_config_payload` over every response body --
    not only field-by-field at descriptor construction.  Construction-time
    scrubbing still runs (so the digest commits to safe content and the CLI is
    protected), but a rogue or duck-typed ``health_service`` whose ``as_dict()``
    bypassed it cannot make this router emit a secret/endpoint/path: the last hop
    scrubs whatever it returns.  Delimiter-anchored patterns leave the content
    digest, schema version, and timestamp intact.

    This router is deliberately read-only: it registers only ``GET`` routes and
    exposes no mutation, execution, or approval path (T003.2 criterion 3 /
    T008).  Unlike the project-context projection it has no unconfigured-503
    state -- reporting an *unconfigured integration as a truthful disabled
    descriptor* is precisely its job, so an all-unset deployment still answers
    200 with disabled descriptors rather than failing closed.
    """
    router = APIRouter(prefix="/api/system")

    @router.get("/health")
    def system_health(_actor: str = Depends(actor_dependency)) -> dict[str, Any]:
        """Every declared integration's observational descriptor."""
        return scrub_config_payload(
            {"integrations": [descriptor.as_dict() for descriptor in health_service.descriptors()]}
        )

    @router.get("/health/{integration_id}")
    def system_health_integration(
        integration_id: str = Path(pattern=_INTEGRATION_ID_PATTERN),
        _actor: str = Depends(actor_dependency),
    ) -> dict[str, Any]:
        """One declared integration's descriptor; an unknown id is a plain 404."""
        return scrub_config_payload({"integration": health_service.get(integration_id).as_dict()})

    @router.get("/posture")
    def system_posture(_actor: str = Depends(actor_dependency)) -> dict[str, Any]:
        """The deterministic observational posture audit (same runner as the CLI)."""
        return scrub_config_payload(health_service.posture().as_dict())

    return router


#: The plugin-id path grammar, mirrored from the reviewed plugin catalog's
#: ``pluginId`` so a malformed id is rejected at the edge (422) before it reaches
#: the discovery projection as a distinguishable lookup.
_PLUGIN_ID_PATTERN = r"^[a-z][a-z0-9-]{1,62}$"
_REQUEST_DIGEST_PATTERN = r"^sha256:[a-f0-9]{64}$"

#: The single non-leaking body every unknown-or-not-enabled plugin lookup gets,
#: so the discovery surface is never an existence oracle for a reviewed-but-not-
#: enabled or an unknown plugin.
_UNKNOWN_PLUGIN_DETAIL = "unknown plugin"
_UNKNOWN_PLUGIN_RECEIPT_DETAIL = "unknown plugin receipt"


def build_plugin_router(
    actor_dependency: Callable[..., str],
    plugin_host_service: "PluginHostService | None",
) -> APIRouter:
    """Build the read-only reviewed-plugin discovery and receipt browser surface.

    Every endpoint is authenticated by the hub's trusted ``actor`` dependency and
    serves only the redacted discovery projection (approved AND capability-enabled
    plugins) or a stored, redacted install receipt.  The browser never receives a
    credential value: the discovery projection reports credential handling by
    opaque reference only, and a receipt reports credential use by reference only
    (reviewed-tools-plugins T002 criterion 1 / T003 criterion 1).  Every response
    body is scrubbed at this last hop with
    :func:`~workbench.redaction.scrub_config_payload`, so even untrusted plugin
    prose (a title, summary, description, or receipt line) cannot ferry a secret,
    endpoint, or path to the UI.

    The surface is deliberately read-only: it registers only ``GET`` routes and
    exposes no install/mutation path (an install is a bridge/host effect, never a
    browser mutation).  When ``plugin_host_service`` is ``None`` the lane is not
    configured (it is deliberately NOT wired into the live bridge poll loop) and
    every endpoint refuses with 503, mirroring the unconfigured context stores.
    """
    router = APIRouter(prefix="/api/plugins")

    def service() -> "PluginHostService":
        if plugin_host_service is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="plugin host is not configured",
            )
        return plugin_host_service

    @router.get("")
    def list_plugins(_actor: str = Depends(actor_dependency)) -> dict[str, Any]:
        """Every approved, capability-enabled plugin's redacted projection."""
        return scrub_config_payload({"plugins": service().list_plugins()})

    @router.get("/{plugin_id}")
    def get_plugin(
        plugin_id: str = Path(pattern=_PLUGIN_ID_PATTERN),
        _actor: str = Depends(actor_dependency),
    ) -> dict[str, Any]:
        """One approved, enabled plugin; an unknown/not-enabled id is a plain 404.

        A reviewed-but-not-enabled or an unknown plugin returns the byte-identical
        not-found body, so this endpoint is not an existence oracle.
        """
        plugin = service().get_plugin(plugin_id)
        if plugin is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_UNKNOWN_PLUGIN_DETAIL)
        return scrub_config_payload({"plugin": plugin})

    @router.get("/receipts/{request_digest}")
    def get_receipt(
        request_digest: str = Path(pattern=_REQUEST_DIGEST_PATTERN),
        _actor: str = Depends(actor_dependency),
    ) -> dict[str, Any]:
        """One stored install receipt, redacted; a missing digest is a plain 404."""
        receipt = service().get_receipt(request_digest)
        if receipt is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_UNKNOWN_PLUGIN_RECEIPT_DETAIL)
        return scrub_config_payload({"receipt": receipt})

    return router


#: The setting-id path grammar, mirrored from the settings-descriptor
#: ``settingId`` definition, so a malformed id is rejected at the edge (422)
#: before it reaches the store as a distinguishable lookup.
_SETTING_ID_PATTERN = r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$"

#: The single fixed body an unknown/cross-scope preference lookup returns. A
#: missing preference and another actor's/project's preference render the same
#: 404 so the read surface is never a cross-scope existence oracle.
_UNKNOWN_PREFERENCE_DETAIL = "unknown preference"


class PreferenceWriteInput(BaseModel):
    # Reject any unknown body field: a client can never smuggle an undeclared
    # key (e.g. a spoofed actor/scope_key) past the typed edge.
    model_config = ConfigDict(extra="forbid")

    scope: str = Field(pattern=r"^(personal|project)$")
    value: Any
    expected_version: int = Field(ge=0)
    project_id: str | None = Field(default=None, max_length=128)


class PreferenceResetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str = Field(pattern=r"^(personal|project)$")
    expected_version: int = Field(ge=0)
    project_id: str | None = Field(default=None, max_length=128)


def build_preferences_router(
    actor_dependency: Callable[..., str],
    preference_store: MemoryPreferenceStore | None,
    live_valid_refs_provider: Callable[[], Mapping[str, Any]] | None = None,
) -> APIRouter:
    """Build the actor-scoped preference read/write browser surface (T002.3).

    Every endpoint is authenticated by the hub's trusted ``actor`` dependency.
    The personal namespace is ALWAYS keyed by the authenticated actor — a client
    can never name another actor — so a cross-actor read is structurally
    impossible; a project namespace is keyed by the path/body ``project_id``. A
    read for a setting not set in the actor's namespace, or for a foreign
    namespace, returns the same indistinct 404, so the surface is never a
    cross-scope existence oracle.

    Only the settings actor-view and actor-scope effective values are serialized
    (never an authority-owned, secret, or path-like descriptor or value), and
    every response is scrubbed on the last hop with
    :func:`~workbench.redaction.scrub_config_payload`. A stale write returns a
    reload-required 409 distinct from the 422 a malformed value raises. When
    ``preference_store`` is ``None`` the surface is not configured and refuses
    with 503, mirroring the other injectable read-models.

    ``live_valid_refs_provider`` is the reference-validity source the shared
    resolver uses to detect an invalidated capability/route reference. When
    supplied it is called per request and must return
    ``{ref_kind -> valid ref values}`` (e.g. the chat-first-voice route
    discovery / the profile-scoped route allowlist); a stored reference outside
    that live set falls back to the safe default with a repair notice. When it
    is ``None`` the endpoint falls back to the reviewed-catalog baseline
    (:func:`~workbench.models.reviewed_catalog_valid_refs` — the reviewed default
    refs), so a stale/since-removed reference is STILL repaired out of the box
    rather than served verbatim.
    """
    router = APIRouter(prefix="/api/preferences")

    def pref_store() -> MemoryPreferenceStore:
        if preference_store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="preference store is not configured",
            )
        return preference_store

    def _live_valid_refs(store: MemoryPreferenceStore) -> Mapping[str, Any]:
        # The one ref-validity source both the effective read and the reset
        # share, so they resolve identical effective values. An injected live
        # provider (the operator's current route allowlist) wins; otherwise the
        # reviewed-catalog default baseline still repairs a stale reference.
        if live_valid_refs_provider is not None:
            return live_valid_refs_provider()
        return reviewed_catalog_valid_refs(store.catalog)

    def _scope_key(scope: str, actor: str, project_id: str | None) -> str:
        # The personal namespace is bound to the authenticated actor and can
        # never be addressed for another actor. A project namespace needs an
        # explicit project id.
        if scope == "personal":
            return actor
        if not project_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="a project-scope preference requires a project_id",
            )
        return project_id

    @router.get("")
    def effective_preferences(
        project_id: str | None = None,
        actor: str = Depends(actor_dependency),
    ) -> dict[str, Any]:
        """The actor's resolved effective values plus the settings actor-view.

        Merges the actor's own personal namespace, the named project namespace,
        and the public authority (deployment/policy) values needed for ceilings,
        resolves them through the single shared resolver, and serializes only
        actor-scope effective values and the actor-view descriptor projection.
        """
        store = pref_store()
        catalog = store.catalog
        # Ownership-filtered merge: each namespace contributes only the setting
        # ids IT owns, so a corrupt/injected row bearing a foreign-scope id (e.g.
        # a personal row carrying a ``policy.*`` id) cannot override a
        # higher-authority value against the declared scope_precedence. Merge is
        # authority-first, actor-last; because ownership filtering makes the
        # namespaces disjoint, no lower-authority row can shadow an authority one.
        stored: dict[str, Any] = {}
        stored.update(store.owned_values("deployment", "deployment"))
        stored.update(store.owned_values("policy", "policy"))
        if project_id:
            stored.update(store.owned_values("project", project_id))
        stored.update(store.owned_values("personal", actor))
        resolved = resolve_effective_settings(catalog, stored, live_valid_refs=_live_valid_refs(store))
        actor_view = settings_actor_view(catalog)
        actor_setting_ids = {setting["id"] for setting in actor_view["settings"]}
        effective = [
            value.as_dict()
            for setting_id, value in sorted(resolved.items())
            if setting_id in actor_setting_ids
        ]
        return scrub_config_payload({"catalog": actor_view, "effective": effective})

    @router.get("/{setting_id}")
    def read_preference(
        setting_id: str = Path(pattern=_SETTING_ID_PATTERN),
        scope: str = "personal",
        project_id: str | None = None,
        actor: str = Depends(actor_dependency),
    ) -> dict[str, Any]:
        """One stored preference record in the actor's own namespace."""
        store = pref_store()
        if scope not in ("personal", "project"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="scope must be personal or project"
            )
        scope_key = _scope_key(scope, actor, project_id)
        try:
            record = store.get(scope, scope_key, setting_id)
        except UnknownPreferenceError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=_UNKNOWN_PREFERENCE_DETAIL
            ) from exc
        return scrub_config_payload({"preference": record.as_dict()})

    @router.put("/{setting_id}")
    def write_preference(
        payload: PreferenceWriteInput,
        setting_id: str = Path(pattern=_SETTING_ID_PATTERN),
        actor: str = Depends(actor_dependency),
    ) -> dict[str, Any]:
        """Commit one scoped preference write under optimistic concurrency."""
        store = pref_store()
        scope_key = _scope_key(payload.scope, actor, payload.project_id)
        try:
            record = store.set_preference(
                payload.scope, scope_key, setting_id, payload.value, payload.expected_version, actor,
            )
        except PreferenceValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            ) from exc
        except StalePreferenceWriteError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "detail": "reload required before writing",
                    "reload_required": True,
                    "current_version": exc.current_version,
                },
            ) from exc
        except UnknownPreferenceError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=_UNKNOWN_PREFERENCE_DETAIL
            ) from exc
        except PreferenceStoreError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return scrub_config_payload({"preference": record.as_dict()})

    @router.post("/{setting_id}/reset")
    def reset_preference(
        payload: PreferenceResetInput,
        setting_id: str = Path(pattern=_SETTING_ID_PATTERN),
        actor: str = Depends(actor_dependency),
    ) -> dict[str, Any]:
        """Reset one preference to its declared inherited/default state."""
        store = pref_store()
        scope_key = _scope_key(payload.scope, actor, payload.project_id)
        try:
            effective = store.reset_preference(
                payload.scope, scope_key, setting_id, payload.expected_version, actor,
                live_valid_refs=_live_valid_refs(store),
            )
        except StalePreferenceWriteError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "detail": "reload required before writing",
                    "reload_required": True,
                    "current_version": exc.current_version,
                },
            ) from exc
        except UnknownPreferenceError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=_UNKNOWN_PREFERENCE_DETAIL
            ) from exc
        except PreferenceStoreError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return scrub_config_payload({"effective": effective.as_dict()})

    return router


def create_app(
    settings: Settings | None = None,
    store: WorkbenchStore | None = None,
    graph: EvidenceGraph | None = None,
    conversation_store: ConversationStore | None = None,
    idempotency_store: IdempotencyStore | None = None,
    project_context_store: ProjectContextStore | None = None,
    run_context_store: RunContextStore | None = None,
    delivery_projection_store: DeliveryProjectionStore | None = None,
    system_health: SystemHealthService | None = None,
    plugin_host_service: "PluginHostService | None" = None,
    preference_store: MemoryPreferenceStore | None = None,
    live_valid_refs_provider: Callable[[], Mapping[str, Any]] | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    store = store or _store(settings)
    graph = graph or _graph(settings)
    conversation_store = conversation_store or _conversation_store(settings)
    idempotency_store = idempotency_store or MemoryIdempotencyStore()
    # System health is always available: it is a read-only projection of the
    # already-parsed settings, so it defaults to a live service rather than
    # failing closed. An injected service lets tests exercise mock bridge health
    # or seeded prose without env plumbing.
    system_health = system_health or SystemHealthService(settings)
    # The reviewed-plugin discovery surface derives from operator-declared config
    # (both the reviewed catalog and the capability profile paths), mirroring the
    # provider-catalog / system-health precedent. An injected service overrides for
    # tests; otherwise it is built from Settings when both files are declared, and
    # stays None (503) when they are not.
    plugin_host_service = plugin_host_service or _plugin_host_service(settings)
    app = FastAPI(title="Anvil Workbench", version="0.1.0", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.store = store
    app.state.graph = graph
    app.state.conversation_store = conversation_store
    app.state.idempotency_store = idempotency_store
    # The derived project-context projection is a display read-model that is
    # deliberately NOT wired into the live bridge poll loop; it stays ``None``
    # unless an instance is injected, so the browser surface fails closed (503).
    app.state.project_context_store = project_context_store
    # The historical run-context store is likewise a hub-side supervision
    # read-model that is deliberately NOT wired into the live bridge poll loop;
    # it stays ``None`` unless injected, so the browser surface fails closed (503).
    app.state.run_context_store = run_context_store
    # The delivery display projection (PRD/plan/task/eligibility + pinned run
    # rows/approval bindings) is a hub-side supervision read-model that is
    # deliberately NOT wired into the live bridge poll loop; it stays ``None``
    # unless injected, so the browser surface fails closed (503).
    app.state.delivery_projection_store = delivery_projection_store
    app.state.system_health = system_health
    # The reviewed-plugin discovery/receipt surface is a hub-side read-model that
    # is deliberately NOT wired into the live bridge poll loop; it stays ``None``
    # unless a service is injected, so the browser surface fails closed (503).
    app.state.plugin_host_service = plugin_host_service
    # The scoped preference store is a hub-side supervision read/write model that
    # is deliberately NOT wired into the live bridge poll loop; it stays ``None``
    # unless injected, so the browser surface fails closed (503).
    app.state.preference_store = preference_store

    def actor(request: Request) -> str:
        name = (request.headers.get(settings.identity_header) or "").strip()
        if not name and settings.allow_insecure_dev_actor:
            name = settings.owner
        if not name:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="trusted tailnet identity is required")
        if name not in settings.approvers:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="actor is not allowlisted")
        return name

    def owner(current_actor: str = Depends(actor)) -> str:
        if current_actor != settings.owner:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="owner permission required")
        return current_actor

    def bridge_identity(
        request: Request,
        x_workbench_bridge: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> str:
        bridge_id = x_workbench_bridge or request.path_params.get("bridge_id")
        if not bridge_id or not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bridge authentication required")
        try:
            store.authenticate_bridge(bridge_id, authorization.removeprefix("Bearer "))
        except StoreError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
        return bridge_id

    @app.exception_handler(StoreError)
    async def store_error_handler(_: Request, exc: StoreError):
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})

    # A missing projection and another project's projection raise the same
    # ``UnknownProjectionError``; both render the identical fixed 404 body so the
    # project-context read surface is never an existence oracle. This handler is
    # more specific than ``StoreError`` above, so it wins for that subclass.
    @app.exception_handler(UnknownProjectionError)
    async def unknown_projection_handler(_: Request, __: UnknownProjectionError):
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND, content={"detail": _UNKNOWN_PROJECTION_DETAIL}
        )

    # A missing run context and another project's run context raise the same
    # ``UnknownRunContextError``; both render the identical fixed 404 body so the
    # historical run-context surface is never an existence oracle. More specific
    # than the ``StoreError`` handler, so it wins for that subclass.
    @app.exception_handler(UnknownRunContextError)
    async def unknown_run_context_handler(_: Request, __: UnknownRunContextError):
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND, content={"detail": _UNKNOWN_RUN_CONTEXT_DETAIL}
        )

    # An unknown system-health integration id renders a plain fixed 404. The
    # declared-integration set is a public, deployment-invariant catalog, so this
    # is a genuine not-found, not a cross-tenant existence oracle.
    @app.exception_handler(UnknownIntegrationError)
    async def unknown_integration_handler(_: Request, __: UnknownIntegrationError):
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND, content={"detail": _UNKNOWN_INTEGRATION_DETAIL}
        )

    # A missing delivery record and another project's delivery record raise the
    # same ``UnknownDeliveryRecordError``; both render the identical fixed 404
    # body so the delivery display surface is never a cross-project existence
    # oracle. More specific than the ``StoreError`` handler, so it wins.
    @app.exception_handler(UnknownDeliveryRecordError)
    async def unknown_delivery_record_handler(_: Request, __: UnknownDeliveryRecordError):
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND, content={"detail": _UNKNOWN_DELIVERY_DETAIL}
        )

    # Actor-scoped chat surface (chat-first-voice T002.4): identity comes from
    # the same trusted ``actor`` dependency; the store enforces ownership.
    register_conversation_handlers(app)
    app.include_router(build_conversation_router(actor, conversation_store, idempotency_store))
    # Off-read-path retention (chat-first-voice T009): the preview + explicit
    # batched enforce pass are operator-only and mounted OFF the actor surface;
    # a single actor can neither preview across nor delete another actor's records.
    app.include_router(build_hub_retention_router(owner, conversation_store))
    # Read-only, project-scoped browser projection of the State-derived context
    # (state-context-operations T003.3): authenticated by the same trusted
    # ``actor`` dependency, scoped by the path ``project_id``, and fail-closed
    # (503) until a projection store is configured. Serves only the explicitly
    # non-canonical display read-model; never canonical State.
    app.include_router(build_project_context_router(actor, project_context_store))
    # Read-only, project-scoped historical run-context surface
    # (state-context-operations T005.3): authenticated by the same trusted
    # ``actor`` dependency, scoped by the path ``project_id``/``run_id``, and
    # fail-closed (503) until a run-context store is configured. Serves only the
    # immutable queue-time snapshot with trusted policy and untrusted PRD/task
    # data separately labeled; never a secret, path, command, or provider payload.
    app.include_router(build_run_context_router(actor, run_context_store))
    # Read-only, project-scoped delivery display surface (plan-task-delivery
    # T002/T004): authenticated by the same trusted ``actor`` dependency, scoped
    # by the path ``project_id``, GET-only, and fail-closed (503) until a
    # projection store is configured. Serves bounded redacted PRD content, scoped
    # task references, stale-checked eligibility verdicts, and the pinned run
    # rows / approval bindings; never canonical State, a secret, path, or command.
    app.include_router(build_delivery_projection_router(actor, delivery_projection_store))
    # Read-only system-health + observational posture surface (preferences-
    # configuration T003.2 / T008): authenticated by the same trusted ``actor``
    # dependency, GET-only, and serving only the closed descriptor/posture display
    # shapes. It never fails closed on an unconfigured integration -- a disabled
    # descriptor with safe remediation is the truthful answer.
    app.include_router(build_system_health_router(actor, system_health))
    # Read-only reviewed-plugin discovery + install-receipt surface (reviewed-
    # tools-plugins T002/T003): authenticated by the same trusted ``actor``
    # dependency, GET-only, and fail-closed (503) until a plugin host service is
    # configured. Serves only the redacted discovery projection and stored
    # receipts; a credential value is never accepted from nor returned to the
    # browser (credentials are reported by opaque reference only).
    app.include_router(build_plugin_router(actor, plugin_host_service))
    # Actor-scoped preference read/write surface (preferences-configuration
    # T002.3): authenticated by the same trusted ``actor`` dependency, personal
    # namespace bound to the authenticated actor, and fail-closed (503) until a
    # preference store is configured. Serves only the settings actor-view and
    # actor-scope effective values; a stale write is a reload-required 409
    # distinct from the 422 a malformed value raises.
    app.include_router(build_preferences_router(actor, preference_store, live_valid_refs_provider))

    @app.get("/healthz")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "anvil-workbench", "graph": type(graph).__name__}

    @app.get("/api/bootstrap")
    def bootstrap(current_actor: str = Depends(actor)) -> dict[str, Any]:
        projects = [as_json(project) for project in store.list_projects()]
        sessions = store.list_sessions()
        directives = [
            as_json(event) for session in sessions for event in store.list_workflow_events(session.id)
            if event.kind == "operator.directive"
        ]
        return {
            "actor": current_actor,
            "projects": projects,
            "runs": [as_json(run) for run in store.list_runs()],
            "sessions": [as_json(session) for session in sessions],
            "workflows": [as_json(workflow) for workflow in store.list_workflows()],
            "approvals": [as_json(approval) for approval in store.list_approvals()],
            "skills": [as_json(skill) for skill in store.list_bridge_skills()],
            "directives": directives[-100:],
            "audit": [as_json(event) for event in store.list_audit()],
            "router_configured": bool(settings.anvil_router_base_url and settings.anvil_router_token),
            "sandbox": {"available": bool(settings.anvil_router_base_url and settings.anvil_router_token and settings.sandbox_models), "models": sorted(settings.sandbox_models)},
            "voice": {
                "available": bool(settings.anvil_voice_realtime_url),
                "transport": "workbench-realtime-relay" if settings.anvil_voice_realtime_url else "not_configured",
                "retains_transcripts": settings.voice_retain_transcripts,
            },
        }

    @app.post("/api/projects", status_code=status.HTTP_201_CREATED)
    def create_project(payload: ProjectInput, _: str = Depends(owner)) -> dict[str, Any]:
        return as_json(store.create_project(payload.name, payload.state_root))

    @app.post("/api/projects/{project_id}/bridges", status_code=status.HTTP_201_CREATED)
    def register_bridge(project_id: str, payload: BridgeInput, _: str = Depends(owner)) -> dict[str, Any]:
        bridge, token = store.register_bridge(project_id, payload.name)
        # Deliberately the single opportunity to retrieve the bridge secret.
        return {"bridge": as_json(bridge), "bootstrap_token": token}

    @app.post("/api/sessions", status_code=status.HTTP_201_CREATED)
    def create_session(payload: SessionInput, current_actor: str = Depends(actor)) -> dict[str, Any]:
        session, workflow = store.create_session(
            payload.project_id, payload.title, payload.worktree_id,
            payload.workflow_definition or default_delivery_workflow(payload.skills),
        )
        store.append_audit("session.requested", current_actor, payload.project_id, {
            "session_id": session.id, "workflow_id": workflow.id,
        })
        return {"session": as_json(session), "workflow": as_json(workflow)}

    @app.post("/api/sessions/{session_id}/directives", status_code=status.HTTP_202_ACCEPTED)
    def add_directive(session_id: str, payload: DirectiveInput, current_actor: str = Depends(actor)) -> dict[str, Any]:
        # Typed delivery semantics (plan-task-delivery T008): the directive is a
        # scrubbed append-only session event queued for the next work packet. It
        # cannot signal, interrupt, or retarget a running Codex process; the only
        # effect is recording the event. The outcome is one stable typed code.
        #
        # Not-wired seam (reviewer-flagged, not a bug): the
        # ``directive.rejected_unknown_session`` typed outcome is unreachable on
        # THIS wired path — ``store.get_session`` below raises a 404 for an unknown
        # session first, so the API never returns that typed code. It is reachable
        # only by a direct ``submit_directive`` store-layer caller (covered by
        # ``tests/test_harness_kernel.py::test_ptd_t008_directive_outcomes_are_typed_and_append_only``).
        session = store.get_session(session_id)
        workflow = next(iter(store.list_workflows(session_id)), None)
        result = submit_directive(
            store, session.id, payload.content, current_actor, workflow.id if workflow else None,
        )
        event = result.get("event")
        if event is not None:
            store.append_audit(
                "session.directive_added", current_actor, session.project_id,
                {"session_id": session.id, "event_id": event.id, "outcome": result["outcome"]},
            )
        response = {"outcome": result["outcome"], "recorded": result["recorded"]}
        if event is not None:
            response["event"] = as_json(event)
        return response

    @app.get("/api/sessions/{session_id}/directives")
    def list_directives(session_id: str, _: str = Depends(actor)) -> dict[str, Any]:
        """The session's directives split into pending vs. packet-included (T008)."""
        store.get_session(session_id)
        return scrub_config_payload(session_directive_view(store, session_id))

    @app.get("/api/sessions/{session_id}/events")
    def session_events(session_id: str, after_sequence: int = 0, _: str = Depends(actor)) -> dict[str, Any]:
        return {"events": [as_json(event) for event in store.list_workflow_events(session_id, max(after_sequence, 0))]}

    @app.post("/api/workflows/{workflow_id}/revise")
    def revise_workflow(workflow_id: str, payload: WorkflowRevisionInput, current_actor: str = Depends(actor)) -> dict[str, Any]:
        return as_json(store.revise_workflow(workflow_id, payload.expected_version, payload.definition, current_actor))

    @app.post("/api/workflows/{workflow_id}/start", status_code=status.HTTP_201_CREATED)
    def start_workflow(workflow_id: str, payload: WorkflowStartInput, current_actor: str = Depends(actor)) -> dict[str, Any]:
        started, run = store.start_workflow_run(
            workflow_id, payload.task_id, payload.model, current_actor,
        )
        return {"workflow": as_json(started), "run": as_json(run)}

    @app.post("/api/runs", status_code=status.HTTP_201_CREATED)
    def create_run(payload: RunInput, current_actor: str = Depends(actor)) -> dict[str, Any]:
        project = next((project for project in store.list_projects() if project.id == payload.project_id), None)
        if project is None or project.bridge_id is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="a project bridge is required before a run can start")
        run = store.create_run(payload.project_id, payload.task_id, payload.model)
        store.enqueue_run(project.bridge_id, run)
        store.append_audit("run.requested", current_actor, payload.project_id, {"run_id": run.id})
        return as_json(run)

    @app.post("/api/approvals", status_code=status.HTTP_201_CREATED)
    def request_approval(payload: ApprovalInput, current_actor: str = Depends(actor)) -> dict[str, Any]:
        approval = store.create_approval(
            payload.project_id, payload.action_type, payload.payload, current_actor,
            payload.ttl_seconds, payload.bridge_id,
        )
        return as_json(approval)

    @app.post("/api/approvals/{approval_id}/approve")
    def approve(approval_id: str, current_actor: str = Depends(actor)) -> dict[str, Any]:
        approval = store.approve(approval_id, current_actor, settings.approvers)
        if approval.bridge_id:
            store.enqueue_command(approval.bridge_id, approval)
        return as_json(approval)

    @app.post("/api/projects/{project_id}/skills/probe", status_code=status.HTTP_202_ACCEPTED)
    def probe_skills(project_id: str, current_actor: str = Depends(actor)) -> dict[str, Any]:
        project = next((item for item in store.list_projects() if item.id == project_id), None)
        if project is None or project.bridge_id is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="a project bridge is required before skills can be checked")
        store.enqueue_skill_probe(project.bridge_id)
        store.append_audit("bridge.skills_probe_requested", current_actor, project.id, {"bridge_id": project.bridge_id})
        return {"accepted": True, "bridge_id": project.bridge_id}

    @app.get("/api/bridge/{bridge_id}/commands/next")
    def next_command(bridge_id: str, authenticated_bridge: str = Depends(bridge_identity)) -> dict[str, Any] | None:
        if bridge_id != authenticated_bridge:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge id mismatch")
        return store.next_command(bridge_id)

    @app.post("/api/bridge/{bridge_id}/skills", status_code=status.HTTP_202_ACCEPTED)
    def publish_bridge_skills(
        bridge_id: str, payload: BridgeSkillsInput, authenticated_bridge: str = Depends(bridge_identity),
    ) -> dict[str, Any]:
        if bridge_id != authenticated_bridge:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge id mismatch")
        skills = store.replace_bridge_skills(bridge_id, [item.model_dump() for item in payload.skills])
        return {"skills": [as_json(skill) for skill in skills]}

    @app.post("/api/bridge/{bridge_id}/approvals/{approval_id}/consume")
    def consume(
        bridge_id: str,
        approval_id: str,
        payload: dict[str, str],
        authenticated_bridge: str = Depends(bridge_identity),
    ) -> dict[str, Any]:
        if bridge_id != authenticated_bridge:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge id mismatch")
        approval = store.get_approval(approval_id)
        if approval.bridge_id != bridge_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="approval belongs to another bridge")
        return as_json(store.consume(approval_id, payload.get("payload_hash", "")))

    @app.post("/api/bridge/{bridge_id}/approvals/{approval_id}/consume-for-run")
    def consume_for_run(
        bridge_id: str,
        approval_id: str,
        payload: dict[str, str],
        authenticated_bridge: str = Depends(bridge_identity),
    ) -> dict[str, Any]:
        if bridge_id != authenticated_bridge:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge id mismatch")
        approval = store.get_approval(approval_id)
        if approval.bridge_id != bridge_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="approval belongs to another bridge")
        return as_json(store.consume_approval_for_run(approval_id, payload.get("payload_hash", ""), bridge_id))

    @app.post("/api/bridge/{bridge_id}/approvals/{approval_id}/complete-merge")
    def complete_merge(
        bridge_id: str,
        approval_id: str,
        payload: dict[str, str],
        authenticated_bridge: str = Depends(bridge_identity),
    ) -> dict[str, Any]:
        if bridge_id != authenticated_bridge:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge id mismatch")
        command_id = payload.get("command_id", "")
        if not command_id:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="command id is required for merge completion")
        return as_json(store.complete_approved_merge(
            approval_id, payload.get("payload_hash", ""), bridge_id, command_id,
        ))

    @app.post("/api/bridge/{bridge_id}/events", status_code=status.HTTP_202_ACCEPTED)
    def bridge_event(bridge_id: str, event: BridgeEvent, authenticated_bridge: str = Depends(bridge_identity)) -> dict[str, bool]:
        if bridge_id != authenticated_bridge:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge id mismatch")
        store.add_transcript(event.run_id, event.role, event.content, bridge_id)
        return {"accepted": True}

    @app.post("/api/bridge/{bridge_id}/commands/{command_id}/ack", status_code=status.HTTP_202_ACCEPTED)
    def acknowledge_bridge_command(
        bridge_id: str, command_id: str, authenticated_bridge: str = Depends(bridge_identity),
    ) -> dict[str, bool]:
        if bridge_id != authenticated_bridge:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge id mismatch")
        store.acknowledge_command(bridge_id, command_id)
        return {"acknowledged": True}

    @app.post("/api/bridge/{bridge_id}/runs/{run_id}/status")
    def bridge_run_status(
        bridge_id: str, run_id: str, payload: RunStatusInput,
        authenticated_bridge: str = Depends(bridge_identity),
    ) -> dict[str, Any]:
        if bridge_id != authenticated_bridge:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge id mismatch")
        return as_json(store.update_run_status(run_id, payload.status, bridge_id))

    @app.post("/api/bridge/{bridge_id}/runs/{run_id}/finalize")
    def finalize_bridge_run(
        bridge_id: str, run_id: str, payload: RunFinalizationInput,
        authenticated_bridge: str = Depends(bridge_identity),
    ) -> dict[str, Any]:
        if bridge_id != authenticated_bridge:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge id mismatch")
        return as_json(store.finalize_run_command(
            run_id, payload.status, bridge_id, payload.command_id,
        ))

    @app.get("/api/bridge/{bridge_id}/runs/{run_id}/lease")
    def bridge_run_lease(
        bridge_id: str, run_id: str,
        authenticated_bridge: str = Depends(bridge_identity),
    ) -> dict[str, Any]:
        if bridge_id != authenticated_bridge:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge id mismatch")
        run = store.validate_run_lease(run_id, bridge_id)
        if run.session_id is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="approved delivery actions require a session-bound run")
        session = store.get_session(run.session_id)
        return {
            "run_id": run.id,
            "session_id": session.id,
            "worktree_id": session.worktree_id,
            "lease_epoch": run.lease_epoch,
        }

    @app.post("/api/bridge/{bridge_id}/runs/{run_id}/lease/renew")
    def renew_bridge_run_lease(
        bridge_id: str, run_id: str,
        authenticated_bridge: str = Depends(bridge_identity),
    ) -> dict[str, Any]:
        if bridge_id != authenticated_bridge:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge id mismatch")
        return as_json(store.renew_run_lease(run_id, bridge_id))

    @app.post("/api/bridge/{bridge_id}/runs/{run_id}/lease/release")
    def release_bridge_run_lease(
        bridge_id: str, run_id: str,
        authenticated_bridge: str = Depends(bridge_identity),
    ) -> dict[str, Any]:
        if bridge_id != authenticated_bridge:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge id mismatch")
        return as_json(store.release_run_lease(run_id, bridge_id))

    @app.post("/api/bridge/{bridge_id}/evidence", status_code=status.HTTP_202_ACCEPTED)
    def project_evidence(bridge_id: str, evidence: EvidenceInput, authenticated_bridge: str = Depends(bridge_identity)) -> dict[str, Any]:
        if bridge_id != authenticated_bridge:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge id mismatch")
        project = next((item for item in store.list_projects() if item.id == evidence.project_id), None)
        if project is None or project.bridge_id != bridge_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge does not own this project")
        citation = graph.project(evidence.source_kind, evidence.source_id, evidence.project_id, evidence.payload)
        store.append_audit("evidence.projected", "bridge:" + bridge_id, evidence.project_id, {"citation": citation, "source_kind": evidence.source_kind})
        return {"accepted": True, "citation": citation}

    @app.get("/api/routes")
    def routes(limit: int = 50, _: str = Depends(actor)) -> dict[str, Any]:
        try:
            rows = route_decisions(settings.anvil_router_base_url, settings.anvil_router_token, limit)
        except RouterError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        known_runs = {run.id for run in store.list_runs()}
        return {"routes": [row for row in rows if row.get("workbench_run_id") in known_runs]}

    @app.post("/api/sandbox")
    def sandbox(payload: SandboxInput, current_actor: str = Depends(actor)) -> dict[str, Any]:
        if payload.model not in settings.sandbox_models:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="the requested sandbox model is not allowed")
        try:
            response = sandbox_response(settings.anvil_router_base_url, settings.anvil_router_token, payload.model, payload.input)
        except RouterError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        store.append_audit("sandbox.completed", current_actor, None, {
            "model": payload.model,
            "input_sha256": hashlib.sha256(payload.input.encode("utf-8")).hexdigest(),
            "output_characters": len(str(response.get("output_text", ""))),
        })
        return response

    @app.post("/api/bridge/{bridge_id}/workflows/{workflow_id}/steps/{step_id}")
    def bridge_workflow_step(
        bridge_id: str, workflow_id: str, step_id: str, payload: WorkflowStepInput,
        authenticated_bridge: str = Depends(bridge_identity),
    ) -> dict[str, Any]:
        if bridge_id != authenticated_bridge:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge id mismatch")
        workflow = store.get_workflow(workflow_id)
        project = next((item for item in store.list_projects() if item.id == workflow.project_id), None)
        if project is None or project.bridge_id != bridge_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge does not own this workflow")
        return as_json(store.complete_workflow_step(workflow_id, step_id, payload.outcome, "bridge:" + bridge_id))

    @app.get("/api/evidence/search")
    def evidence_search(project_id: str, query: str, _: str = Depends(actor)) -> dict[str, Any]:
        return {"results": graph.evidence_search(project_id, query)}

    @app.get("/api/tasks/{task_id}/lineage")
    def task_lineage(task_id: str, _: str = Depends(actor)) -> dict[str, Any]:
        return {"task_id": task_id, "lineage": graph.task_lineage(task_id)}

    @app.get("/api/failures/related")
    def related_failures(fingerprint: str, _: str = Depends(actor)) -> dict[str, Any]:
        return {"results": graph.related_failures(fingerprint)}

    @app.websocket("/api/sessions/{session_id}/voice/realtime")
    async def session_voice(session_id: str, websocket: WebSocket) -> None:
        current_actor = (websocket.headers.get(settings.identity_header) or "").strip()
        if not current_actor and settings.allow_insecure_dev_actor:
            current_actor = settings.owner
        if not current_actor or current_actor not in settings.approvers:
            await websocket.close(code=1008)
            return
        if not settings.anvil_voice_realtime_url:
            await websocket.close(code=1013)
            return
        try:
            session = store.get_session(session_id)
        except StoreError:
            await websocket.close(code=1008)
            return
        workflow = next(iter(store.list_workflows(session_id)), None)

        async def record(kind: str, data: dict[str, Any]) -> None:
            store.record_session_event(session.id, workflow.id if workflow else None, kind, data)

        await relay_realtime(
            websocket, settings.anvil_voice_realtime_url, settings.anvil_voice_realtime_token,
            record, settings.voice_retain_transcripts,
        )

    return app
