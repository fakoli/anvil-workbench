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
# T003.4): the rendered read-model response can never carry a State storage
# path, a credential/token, or a raw executable provider payload, even when the
# underlying State prose is adversarially seeded with secrets. ----------------

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

    # No serialized VALUE carries a concrete storage path, workspace, or URL,
    # and no identifier/title field carries a path separator.
    for value in strings:
        lowered = value.lower()
        for marker in ("state.db", ".anvil", "-wal", "-shm", "://"):
            assert marker not in lowered, f"response value {value!r} leaked {marker!r}"
