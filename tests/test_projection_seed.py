"""Hermetic tests for the live delivery-projection seed (generate + load).

Two halves are exercised:

* The LOADER: a valid seed dir is captured into a real projection store and the
  content is then served through the ACTUAL wired API entrypoint
  (``create_app`` + ``TestClient`` GET .../content).  An invalid or tampered
  seed file fails the whole load closed with NO partial capture, and
  cross-project scoping is preserved.
* The GENERATOR: a FAKE ``anvil`` CLI (a stub script driven through real
  ``subprocess`` plus injected recording runners) produces conforming and
  non-conforming State outputs.  The generator's exact command allowlist is
  asserted (no shell), it fails closed on nonconforming output writing no
  partial seed, and the written seeds round-trip through the loader with the
  served PRD body equal to the body the CLI emitted.

No live ``anvil`` is ever invoked here.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workbench.api import create_app
from workbench.config import Settings
from workbench.contracts import contract_digest
from workbench.delivery_projection import MemoryDeliveryProjectionStore, UnknownDeliveryRecordError
from workbench.graph import NullGraph
from workbench.state_manifest import (
    PRD_READ_CONTENT_OPERATION_ID,
    PROJECT_SNAPSHOT_OPERATION_ID,
)
from workbench.store import MemoryStore
from workbench import projection_seed
from workbench.projection_seed import (
    DEFAULT_CONTENT_COMMAND,
    DEFAULT_DESCRIBE_COMMAND,
    DEFAULT_SNAPSHOT_COMMAND,
    SEED_MANIFEST_NAME,
    SEED_SCHEMA_VERSION,
    ProjectionSeedError,
    generate_seed,
    load_seed_dir,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "docs" / "contracts" / "examples"
_ACTOR = {"X-Workbench-Actor": "operator"}


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def _example(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def _snapshot_payload(project_id: str = "project_example") -> dict:
    snap = _example("anvil-state.project-snapshot.v1.json")
    snap["project"]["project_id"] = project_id
    snap["snapshot_digest"] = contract_digest("state-snapshot", snap)
    return snap


def _prd_content_payload(prd_id: str, revision: int, body: str) -> dict:
    doc = {
        "schema_version": "workbench-prd-content/v1",
        "content_digest": "sha256:" + "0" * 64,
        "provider": "anvil-state",
        "generated_at": "2026-07-21T00:00:00Z",
        "prd": {"prd_id": prd_id, "title": f"Title for {prd_id}", "status": "approved", "revision": revision},
        "content_trust": "untrusted_task_data",
        "content": {
            "format": "markdown",
            "body": body,
            "truncated": False,
            "total_bytes": len(body.encode("utf-8")),
        },
        "redaction": {"status": "redacted", "ruleset": "hub.default"},
    }
    doc["content_digest"] = contract_digest("prd-content", doc)
    return doc


def _client(store):
    settings = Settings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator"}), bridge_bootstrap_token="",
        anvil_router_base_url="http://100.87.34.66:8000/v1", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=True,
    )
    return TestClient(create_app(
        settings=settings, store=MemoryStore(), graph=NullGraph(),
        delivery_projection_store=store,
    ))


def _write_manifest_seed(seed_dir: Path, entries):
    """Write a hand-built seed dir; each entry is (kind, project_id, filename, payload)."""
    manifest_entries = []
    for kind, project_id, relpath, payload in entries:
        target = seed_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        manifest_entries.append({"kind": kind, "project_id": project_id, "path": relpath})
    manifest = {"schema_version": SEED_SCHEMA_VERSION, "generated_at": "2026-07-21T00:00:00Z", "entries": manifest_entries}
    (seed_dir / SEED_MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Loader
# --------------------------------------------------------------------------- #


def test_loader_serves_prd_content_through_the_wired_api(tmp_path):
    body = "# Release Alpha\n\nThe real PRD body the Explorer should show.\n"
    seed = tmp_path / "seed"
    _write_manifest_seed(seed, [
        (projection_seed._PRD_CONTENT_KIND, "proj", "prd-content/proj/release-alpha.json",
         _prd_content_payload("release-alpha", 4, body)),
    ])
    store = MemoryDeliveryProjectionStore()
    summary = load_seed_dir(store, seed)
    assert summary == {"prds": 1, "tasks": 0, "projects": 1}
    with _client(store) as client:
        served = client.get("/api/projects/proj/prds/release-alpha/content", headers=_ACTOR)
        assert served.status_code == 200
        assert served.json()["content"]["content"]["body"] == body


def test_loader_captures_task_references_and_content(tmp_path):
    seed = tmp_path / "seed"
    reference = {
        "schema_version": "workbench-task-reference/v1",
        "ref": {"prd_id": "release-alpha", "task_id": "T001", "prd_revision": 4},
        "scoped_id": "release-alpha:T001",
        "run_label": "release-alpha:T001@r4",
        "source": {
            "provider": "anvil-state", "provider_contract_version": "1.0.0",
            "read_operation_id": "state.project.snapshot",
            "snapshot_digest": "sha256:" + "a" * 64,
        },
        "hierarchy": {"prd_id": "release-alpha", "prd_title": "Chat-first Workbench"},
        "summary": {"content_trust": "untrusted_task_data", "title": "Add routed chat", "status": "ready"},
    }
    _write_manifest_seed(seed, [
        (projection_seed._PRD_CONTENT_KIND, "proj", "prd-content/proj/release-alpha.json",
         _prd_content_payload("release-alpha", 4, "# body\n")),
        (projection_seed._TASK_REFERENCE_KIND, "proj", "task-reference/proj/release-alpha_T001.json", reference),
    ])
    store = MemoryDeliveryProjectionStore()
    assert load_seed_dir(store, seed) == {"prds": 1, "tasks": 1, "projects": 1}
    with _client(store) as client:
        tasks = client.get("/api/projects/proj/prds/release-alpha/tasks", headers=_ACTOR).json()["tasks"]
        assert [t["scoped_id"] for t in tasks] == ["release-alpha:T001"]


def test_loader_fails_closed_on_tampered_file_with_no_partial_capture(tmp_path):
    seed = tmp_path / "seed"
    tampered = _prd_content_payload("release-alpha", 4, "# ok\n")
    tampered["content_digest"] = "sha256:" + "f" * 64  # digest no longer recomputes
    _write_manifest_seed(seed, [
        (projection_seed._PRD_CONTENT_KIND, "proj", "prd-content/proj/good.json",
         _prd_content_payload("release-beta", 5, "# good\n")),
        (projection_seed._PRD_CONTENT_KIND, "proj", "prd-content/proj/bad.json", tampered),
    ])
    store = MemoryDeliveryProjectionStore()
    with pytest.raises(ProjectionSeedError):
        load_seed_dir(store, seed)
    # The valid record that preceded the bad one must NOT have been captured.
    with pytest.raises(UnknownDeliveryRecordError):
        store.get_prd_content("proj", "release-beta")


def test_loader_rejects_unknown_manifest_kind(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "x.json").write_text("{}", encoding="utf-8")
    manifest = {
        "schema_version": SEED_SCHEMA_VERSION, "generated_at": "2026-07-21T00:00:00Z",
        "entries": [{"kind": "eligibility", "project_id": "proj", "path": "x.json"}],
    }
    (seed / SEED_MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    store = MemoryDeliveryProjectionStore()
    with pytest.raises(ProjectionSeedError):
        load_seed_dir(store, seed)


def test_loader_rejects_wrong_schema_version_and_missing_manifest(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ProjectionSeedError):
        load_seed_dir(MemoryDeliveryProjectionStore(), empty)

    wrong = tmp_path / "wrong"
    wrong.mkdir()
    (wrong / SEED_MANIFEST_NAME).write_text(json.dumps({"schema_version": "nope", "entries": []}), encoding="utf-8")
    with pytest.raises(ProjectionSeedError):
        load_seed_dir(MemoryDeliveryProjectionStore(), wrong)


def test_loader_rejects_path_traversal(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    manifest = {
        "schema_version": SEED_SCHEMA_VERSION, "generated_at": "2026-07-21T00:00:00Z",
        "entries": [{"kind": projection_seed._PRD_CONTENT_KIND, "project_id": "proj", "path": "../escape.json"}],
    }
    (seed / SEED_MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "escape.json").write_text(json.dumps(_prd_content_payload("release-alpha", 4, "x")), encoding="utf-8")
    with pytest.raises(ProjectionSeedError):
        load_seed_dir(MemoryDeliveryProjectionStore(), seed)


def test_loader_preserves_cross_project_scoping(tmp_path):
    seed = tmp_path / "seed"
    _write_manifest_seed(seed, [
        (projection_seed._PRD_CONTENT_KIND, "proj-a", "prd-content/proj-a/p.json",
         _prd_content_payload("shared-prd", 4, "# A body\n")),
        (projection_seed._PRD_CONTENT_KIND, "proj-b", "prd-content/proj-b/p.json",
         _prd_content_payload("shared-prd", 4, "# B body\n")),
    ])
    store = MemoryDeliveryProjectionStore()
    assert load_seed_dir(store, seed) == {"prds": 2, "tasks": 0, "projects": 2}
    with _client(store) as client:
        a = client.get("/api/projects/proj-a/prds/shared-prd/content", headers=_ACTOR).json()
        assert a["content"]["content"]["body"] == "# A body\n"
        # proj-a can never see proj-b's record collapse in; each stays scoped.
        b = client.get("/api/projects/proj-b/prds/shared-prd/content", headers=_ACTOR).json()
        assert b["content"]["content"]["body"] == "# B body\n"
        # A project that owns no such record gets the indistinct 404.
        assert client.get("/api/projects/intruder/prds/shared-prd/content", headers=_ACTOR).status_code == 404


# --------------------------------------------------------------------------- #
# Generator (fake anvil CLI)
# --------------------------------------------------------------------------- #

_FAKE_CLI = r'''
import json, os, sys

log = os.environ.get("FAKE_ANVIL_ARGV_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(sys.argv[1:]) + "\n")

responses = os.environ["FAKE_ANVIL_RESPONSES"]
argv = sys.argv[1:]

if argv[:1] == ["describe"]:
    name = "describe.json"
elif argv[:1] == ["snapshot"]:
    name = "snapshot.json"
elif argv[:2] == ["prd", "read-content"]:
    prd_id = argv[-1]
    name = "content-" + prd_id + ".json"
else:
    sys.stderr.write("unknown command: %r\n" % (argv,))
    sys.exit(2)

with open(os.path.join(responses, name), "r", encoding="utf-8") as handle:
    sys.stdout.write(handle.read())
'''


def _envelope(command: str, data: dict) -> str:
    return json.dumps({"ok": True, "command": command, "data": data})


def _catalog() -> dict:
    return _example("anvil-state.catalog.v1.json")


def _setup_fake_cli(tmp_path, *, responses: dict, argv_log: Path | None = None):
    """Write the stub CLI and its canned responses; return the three command strings."""
    stub = tmp_path / "fake_anvil.py"
    stub.write_text(_FAKE_CLI, encoding="utf-8")
    resp_dir = tmp_path / "responses"
    resp_dir.mkdir()
    for name, text in responses.items():
        (resp_dir / name).write_text(text, encoding="utf-8")

    # The adapters call subprocess.run without a custom env, so the stub reads
    # its responses/argv-log locations from the inherited process environment.
    os.environ["FAKE_ANVIL_RESPONSES"] = str(resp_dir)
    if argv_log is not None:
        os.environ["FAKE_ANVIL_ARGV_LOG"] = str(argv_log)
    else:
        os.environ.pop("FAKE_ANVIL_ARGV_LOG", None)

    py = sys.executable
    return {
        "describe_command": f"{py} {stub} describe",
        "snapshot_command": f"{py} {stub} snapshot --json",
        "content_command": f"{py} {stub} prd read-content --json",
    }


@pytest.fixture(autouse=True)
def _clean_fake_env():
    saved = {k: os.environ.get(k) for k in ("FAKE_ANVIL_RESPONSES", "FAKE_ANVIL_ARGV_LOG")}
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _skip_if_spaced_interpreter():
    if " " in sys.executable:
        pytest.skip("interpreter path contains a space; shlex(posix=False) round-trip is unsafe on Windows")


def test_generator_real_subprocess_roundtrips_through_loader(tmp_path):
    _skip_if_spaced_interpreter()
    project_id = "project_example"
    alpha_body = "# Chat-first Workbench\n\nThe alpha PRD body from the CLI.\n"
    beta_body = "# State context\n\nThe beta PRD body from the CLI.\n"
    responses = {
        "describe.json": json.dumps(_catalog()),
        "snapshot.json": _envelope("snapshot", _snapshot_payload(project_id)),
        "content-release-alpha.json": _envelope("prd read-content", _prd_content_payload("release-alpha", 4, alpha_body)),
        "content-release-beta.json": _envelope("prd read-content", _prd_content_payload("release-beta", 5, beta_body)),
    }
    argv_log = tmp_path / "argv.log"
    commands = _setup_fake_cli(tmp_path, responses=responses, argv_log=argv_log)
    out_dir = tmp_path / "seed"

    summary = generate_seed(project_id, tmp_path, out_dir, **commands)
    assert summary["project_id"] == project_id
    assert summary["prds"] == 2
    assert summary["tasks"] == 3  # release-alpha:T001, release-beta:T001, release-beta:T002.2
    assert sorted(summary["prd_ids"]) == ["release-alpha", "release-beta"]

    # No shell: the recorded argv are exactly the pinned read commands, tokenized.
    recorded = [json.loads(line) for line in argv_log.read_text(encoding="utf-8").splitlines()]
    assert recorded[0] == ["describe"]
    assert recorded[1] == ["snapshot", "--json"]
    content_calls = [a for a in recorded if a[:2] == ["prd", "read-content"]]
    assert {a[-1] for a in content_calls} == {"release-alpha", "release-beta"}
    for call in content_calls:
        assert call == ["prd", "read-content", "--json", call[-1]]

    # The written seed round-trips through the loader and serves the CLI body.
    store = MemoryDeliveryProjectionStore()
    load_summary = load_seed_dir(store, out_dir)
    assert load_summary == {"prds": 2, "tasks": 3, "projects": 1}
    with _client(store) as client:
        served = client.get(f"/api/projects/{project_id}/prds/release-alpha/content", headers=_ACTOR).json()
        assert served["content"]["content"]["body"] == alpha_body
        beta = client.get(f"/api/projects/{project_id}/prds/release-beta/content", headers=_ACTOR).json()
        assert beta["content"]["content"]["body"] == beta_body


def test_generator_exact_command_allowlist_via_injected_runners(tmp_path):
    """Prove the exact per-operation argv the generator issues (no shell, no extras)."""
    project_id = "project_example"
    snap = _snapshot_payload(project_id)
    describe_calls: list[list[str]] = []
    snapshot_calls: list[list[str]] = []
    content_calls: list[tuple[str, list[str]]] = []

    def describe_runner(args):
        describe_calls.append(list(args))
        return json.dumps(_catalog())

    def snapshot_runner(operation, args):
        assert operation.operation_id == PROJECT_SNAPSHOT_OPERATION_ID
        snapshot_calls.append(list(args))
        return _envelope("snapshot", snap)

    def content_runner(operation, args):
        assert operation.operation_id == PRD_READ_CONTENT_OPERATION_ID
        content_calls.append((operation.operation_id, list(args)))
        prd_id = args[-1]
        revision = 4 if prd_id == "release-alpha" else 5
        return _envelope("prd read-content", _prd_content_payload(prd_id, revision, f"# {prd_id}\n"))

    out_dir = tmp_path / "seed"
    summary = generate_seed(
        project_id, tmp_path, out_dir,
        describe_command=DEFAULT_DESCRIBE_COMMAND,
        snapshot_command=DEFAULT_SNAPSHOT_COMMAND,
        content_command=DEFAULT_CONTENT_COMMAND,
        describe_runner=describe_runner, snapshot_runner=snapshot_runner, content_runner=content_runner,
    )
    assert summary["prds"] == 2 and summary["tasks"] == 3

    assert describe_calls == [["anvil", "describe"]]
    assert snapshot_calls == [["anvil", "snapshot", "--json"]]
    for _op, args in content_calls:
        assert args[:4] == ["anvil", "prd", "read-content", "--json"]
        assert len(args) == 5  # exactly the scoped prd_id appended, nothing else
    assert {args[-1] for _op, args in content_calls} == {"release-alpha", "release-beta"}


def test_generator_fails_closed_on_nonconforming_content_no_partial_seed(tmp_path):
    project_id = "project_example"
    snap = _snapshot_payload(project_id)

    def describe_runner(args):
        return json.dumps(_catalog())

    def snapshot_runner(operation, args):
        return _envelope("snapshot", snap)

    def content_runner(operation, args):
        prd_id = args[-1]
        doc = _prd_content_payload(prd_id, 4 if prd_id == "release-alpha" else 5, f"# {prd_id}\n")
        if prd_id == "release-beta":
            doc["content_digest"] = "sha256:" + "9" * 64  # digest no longer recomputes -> nonconforming
        return _envelope("prd read-content", doc)

    out_dir = tmp_path / "seed"
    with pytest.raises((ProjectionSeedError, RuntimeError)):
        generate_seed(
            project_id, tmp_path, out_dir,
            describe_runner=describe_runner, snapshot_runner=snapshot_runner, content_runner=content_runner,
        )
    # Fail-closed: no partial seed on disk (manifest never written; ideally nothing).
    assert not (out_dir / SEED_MANIFEST_NAME).exists()


def test_generator_fails_closed_on_mislabeled_project(tmp_path):
    snap = _snapshot_payload("project_example")

    def describe_runner(args):
        return json.dumps(_catalog())

    def snapshot_runner(operation, args):
        return _envelope("snapshot", snap)

    def content_runner(operation, args):
        return _envelope("prd read-content", _prd_content_payload(args[-1], 4, "# x\n"))

    out_dir = tmp_path / "seed"
    with pytest.raises(ProjectionSeedError):
        generate_seed(
            "a-different-project", tmp_path, out_dir,
            describe_runner=describe_runner, snapshot_runner=snapshot_runner, content_runner=content_runner,
        )
    assert not out_dir.exists() or not any(out_dir.iterdir())


def test_generator_fails_closed_when_describe_lacks_operation_catalog(tmp_path):
    """Mirror the live fakoli/anvil#178 gap: describe advertises no catalog."""
    def describe_runner(args):
        return json.dumps({"ok": True, "command": "describe", "data": {
            "api_version": "4", "cli": {"commands": [], "count": 0}, "mcp": {"tools": [], "count": 0},
        }})

    out_dir = tmp_path / "seed"
    with pytest.raises(RuntimeError):  # StateManifestError (a RuntimeError) fails discovery closed
        generate_seed(
            "project_example", tmp_path, out_dir, describe_runner=describe_runner,
        )
    assert not out_dir.exists() or not any(out_dir.iterdir())
