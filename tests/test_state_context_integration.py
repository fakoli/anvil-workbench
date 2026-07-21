"""End-to-end integration of the supported State context read-adapter trio.

This is the T002 integrate-and-qualify fixture (state-context-operations:T002).
It wires the three already-implemented, individually-tested halves together
through ONE pinned descriptor set:

    manifest discovery  ->  project-snapshot adapter
    (workbench.state_manifest)   (workbench.state_snapshot_adapter)
                         \\->  bounded PRD-content adapter
                              (workbench.prd_content_adapter)

Discovery runs once and pins the immutable read set; BOTH adapters take that
same set by constructor, so the integration proves the pinned descriptor and
source digests flow coherently from `anvil describe` through to a
`PublishableSnapshot` and a `PublishablePrdContent`.

The whole pipeline is hermetic: every transport is an injected runner and no
live State CLI is executed.  This is integration-of-existing-parts only; the
trio is still deliberately NOT wired into the live bridge poll loop (live
qualification stays gated on the upstream State CLI advertising the operation
catalog from `anvil describe`, fakoli/anvil#178).

Acceptance-criterion map:

* Criterion 1 (discovery + snapshot validation + bounded content share one
  pinned descriptor set and source digests):
  ``test_pipeline_shares_one_pinned_descriptor_set_and_source_digests``.
* Criterion 2 (same-numbered tasks stay distinct, full PRD Markdown stays out
  of the snapshot, bounded content retains exact revision/digest/encoding/limit
  metadata):
  ``test_same_numbered_tasks_distinct_and_markdown_stays_out_of_the_snapshot``,
  ``test_bounded_content_retains_exact_revision_digest_encoding_and_limits``.
* Criterion 3 (missing/incompatible operations, malformed ownership, oversize
  content, invalid encoding, and digest drift fail before publication):
  ``test_missing_or_incompatible_operation_fails_discovery_before_any_adapter``,
  ``test_malformed_ownership_fails_the_snapshot_before_publication``,
  ``test_oversize_and_invalid_encoding_content_fail_before_publication``,
  ``test_digest_drift_on_either_read_fails_before_publication``.
* Criterion 4 (source and runtime probes prove nothing touches ``state.db``):
  ``test_source_probe_no_integration_module_references_state_storage``,
  ``test_runtime_probe_pipeline_never_shells_out_or_touches_state_db``.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from workbench import prd_content_adapter as content_module
from workbench import state_manifest as manifest_module
from workbench import state_snapshot_adapter as snapshot_module
from workbench.contracts import contract_digest
from workbench.prd_content_adapter import (
    PrdContentAdapter,
    PrdContentError,
    PublishablePrdContent,
)
from workbench.state_manifest import (
    PRD_READ_CONTENT_OPERATION_ID,
    PROJECT_SNAPSHOT_OPERATION_ID,
    PinnedStateReadOperations,
    StateManifestDiscovery,
    StateManifestError,
)
from workbench.state_snapshot_adapter import (
    PublishableSnapshot,
    StateSnapshotAdapter,
    StateSnapshotError,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CATALOG = ROOT / "docs" / "contracts" / "examples" / "anvil-state.catalog.v1.json"
EXAMPLE_SNAPSHOT = ROOT / "docs" / "contracts" / "examples" / "anvil-state.project-snapshot.v1.json"
EXAMPLE_CONTENT = ROOT / "docs" / "contracts" / "examples" / "anvil-state.prd-content.v1.json"

DESCRIBE_COMMAND = "anvil describe --json"
SNAPSHOT_COMMAND = "anvil snapshot --json"
CONTENT_COMMAND = "anvil prd show --json"


# --- fixture documents ------------------------------------------------------


def example_catalog() -> dict:
    return json.loads(EXAMPLE_CATALOG.read_text(encoding="utf-8"))


def example_snapshot() -> dict:
    return json.loads(EXAMPLE_SNAPSHOT.read_text(encoding="utf-8"))


def example_content() -> dict:
    return json.loads(EXAMPLE_CONTENT.read_text(encoding="utf-8"))


def rehash_catalog(catalog: dict) -> dict:
    for operation in catalog["operations"]:
        operation["operation_digest"] = contract_digest("operation", operation)
    catalog["catalog_digest"] = contract_digest("catalog", catalog)
    return catalog


def rehash_snapshot(snapshot: dict) -> dict:
    snapshot["snapshot_digest"] = contract_digest("state-snapshot", snapshot)
    return snapshot


def rehash_content(document: dict) -> dict:
    document["content_digest"] = contract_digest("prd-content", document)
    return document


def describe_envelope(catalog: dict, prefix: str = "") -> str:
    return prefix + json.dumps({"ok": True, "command": "describe", "data": catalog})


def snapshot_envelope(payload: dict, prefix: str = "") -> str:
    return prefix + json.dumps({"ok": True, "command": "snapshot", "data": payload})


def content_envelope(payload: dict, prefix: str = "") -> str:
    return prefix + json.dumps({"ok": True, "command": "prd-content", "data": payload})


def task_named(snapshot: dict, scoped_id: str) -> dict:
    return next(task for task in snapshot["tasks"] if task["scoped_id"] == scoped_id)


# --- the integrated pipeline (one pinned set feeds both adapters) -----------


class RecordingPipeline:
    """Discovery + both adapters wired over injected runners, recording argv.

    A single :class:`StateManifestDiscovery` pins the read set once; both the
    snapshot and content adapters are constructed from that SAME immutable set,
    mirroring exactly how a live bridge would resolve one descriptor set and
    reuse it.  Every runner records the pinned operation it executed and the
    argv it was handed so the integration can attest the transport surface.
    """

    def __init__(self, catalog_doc: str, snapshot_doc: str, content_doc: str) -> None:
        self.calls: list[tuple[str, list[str]]] = []

        def describe_runner(args) -> str:
            self.calls.append(("describe", list(args)))
            return catalog_doc

        self.discovery = StateManifestDiscovery(DESCRIBE_COMMAND, runner=describe_runner)
        self.pinned: PinnedStateReadOperations = self.discovery.pinned()

        def snapshot_runner(operation, args) -> str:
            self.calls.append((operation.operation_id, list(args)))
            return snapshot_doc

        def content_runner(operation, args) -> str:
            self.calls.append((operation.operation_id, list(args)))
            return content_doc

        self.snapshot_adapter = StateSnapshotAdapter(
            self.pinned, SNAPSHOT_COMMAND, runner=snapshot_runner,
        )
        self.content_adapter = PrdContentAdapter(
            self.pinned, CONTENT_COMMAND, runner=content_runner,
        )


def happy_pipeline() -> RecordingPipeline:
    return RecordingPipeline(
        describe_envelope(example_catalog(), prefix="anvil workbench fixture\n"),
        snapshot_envelope(example_snapshot()),
        content_envelope(example_content()),
    )


# --- Criterion 1 ------------------------------------------------------------


def test_pipeline_shares_one_pinned_descriptor_set_and_source_digests() -> None:
    """Criterion 1: one discovery pins the read set both adapters bind to."""
    pipeline = happy_pipeline()

    snapshot = pipeline.snapshot_adapter.fetch()
    content = pipeline.content_adapter.fetch("release-beta")

    assert isinstance(snapshot, PublishableSnapshot)
    assert isinstance(content, PublishablePrdContent)

    # Both results are attributed to the SAME pinned descriptor set discovery
    # produced (identical operation digests), not to a re-discovered one.
    assert snapshot.operation_id == PROJECT_SNAPSHOT_OPERATION_ID
    assert snapshot.operation_digest == pipeline.pinned.project_snapshot.operation_digest
    assert content.operation_id == PRD_READ_CONTENT_OPERATION_ID
    assert content.operation_digest == pipeline.pinned.prd_read_content.operation_digest

    # The provider and per-read source digests survive end-to-end.
    assert snapshot.provider == content.provider == "anvil-state"
    assert snapshot.project_id == "project_example"
    assert snapshot.snapshot_digest == example_snapshot()["snapshot_digest"]
    assert content.content_digest == example_content()["content_digest"]

    # Discovery ran exactly once; each adapter executed only its own pinned
    # descriptor against the configured argv (never a hardcoded path).
    assert pipeline.calls == [
        ("describe", ["anvil", "describe", "--json"]),
        (PROJECT_SNAPSHOT_OPERATION_ID, ["anvil", "snapshot", "--json"]),
        (PRD_READ_CONTENT_OPERATION_ID, ["anvil", "prd", "show", "--json", "release-beta"]),
    ]


def test_repeated_reads_reuse_the_pinned_set_without_rediscovery() -> None:
    """The pinned set is resolved once and reused; nothing rediscovers per call."""
    pipeline = happy_pipeline()
    assert pipeline.discovery.pinned() is pipeline.pinned

    pipeline.snapshot_adapter.fetch()
    pipeline.snapshot_adapter.fetch()
    pipeline.content_adapter.fetch("release-beta")

    describe_calls = [call for call in pipeline.calls if call[0] == "describe"]
    assert describe_calls == [("describe", ["anvil", "describe", "--json"])]


# --- Criterion 2 ------------------------------------------------------------


def test_same_numbered_tasks_distinct_and_markdown_stays_out_of_the_snapshot() -> None:
    """Criterion 2: T001 in two PRDs stays distinct; no Markdown in the snapshot."""
    pipeline = happy_pipeline()
    snapshot = pipeline.snapshot_adapter.fetch()

    assert "release-alpha:T001" in snapshot.scoped_task_ids
    assert "release-beta:T001" in snapshot.scoped_task_ids
    assert len(set(snapshot.scoped_task_ids)) == len(snapshot.scoped_task_ids)
    refs = {(t["ref"]["prd_id"], t["ref"]["task_id"]) for t in snapshot.payload["tasks"]}
    assert ("release-alpha", "T001") in refs and ("release-beta", "T001") in refs

    # Bounded summaries only: no snapshot entry carries a body/content/markdown
    # field, and every prose title is within the contract's code-point bound.
    for entry in snapshot.payload["prds"] + snapshot.payload["tasks"]:
        assert not {"body", "content", "markdown", "description"} & set(entry)
        assert len(entry["title"]) <= 500

    # The full PRD Markdown is only ever reachable through the SEPARATE bounded
    # content read, never smuggled into the whole-project snapshot.
    content = pipeline.content_adapter.fetch("release-beta")
    assert content.body and content.content_format == "markdown"
    snapshot_json = snapshot.payload_json
    assert content.body not in snapshot_json


def test_bounded_content_retains_exact_revision_digest_encoding_and_limits() -> None:
    """Criterion 2: the bounded content read keeps exact typed bound metadata."""
    document = example_content()
    pipeline = RecordingPipeline(
        describe_envelope(example_catalog()),
        snapshot_envelope(example_snapshot()),
        content_envelope(document),
    )
    content = pipeline.content_adapter.fetch("release-beta")

    # Exact source revision + content digest.
    assert content.prd_id == "release-beta"
    assert content.prd_revision == 5
    assert content.prd_status == "approved"
    assert content.content_digest == document["content_digest"]
    # Exact encoding size (UTF-8 bytes) and the 64 KiB byte bound.
    assert content.body_bytes == len(document["content"]["body"].encode("utf-8"))
    assert content.body_bytes <= 65536
    # Exact limit metadata: truncated prefix of a larger source document.
    assert content.truncated is True
    assert content.total_bytes == 18211
    assert content.total_bytes > content.body_bytes

    # An expected-revision that matches the snapshot's release-beta revision (5)
    # is accepted; a stale one is refused — the two reads agree on freshness.
    snapshot = pipeline.snapshot_adapter.fetch()
    beta = next(p for p in snapshot.payload["prds"] if p["prd_id"] == "release-beta")
    assert beta["revision"] == 5
    assert pipeline.content_adapter.fetch("release-beta", expected_revision=5).prd_revision == 5
    with pytest.raises(PrdContentError, match="stale or unexpected"):
        pipeline.content_adapter.fetch("release-beta", expected_revision=4)


# --- Criterion 3 ------------------------------------------------------------


def test_missing_or_incompatible_operation_fails_discovery_before_any_adapter() -> None:
    """Criterion 3: a broken catalog fails discovery, so no adapter is built."""
    missing = example_catalog()
    missing["operations"] = [
        op for op in missing["operations"] if op["id"] != PRD_READ_CONTENT_OPERATION_ID
    ]
    with pytest.raises(StateManifestError, match="missing required read operation"):
        RecordingPipeline(
            describe_envelope(rehash_catalog(missing)),
            snapshot_envelope(example_snapshot()),
            content_envelope(example_content()),
        )

    incompatible = example_catalog()
    next(op for op in incompatible["operations"] if op["id"] == PROJECT_SNAPSHOT_OPERATION_ID)[
        "contract_version"
    ] = "2.0.0"
    with pytest.raises(StateManifestError, match="incompatible contract major"):
        RecordingPipeline(
            describe_envelope(rehash_catalog(incompatible)),
            snapshot_envelope(example_snapshot()),
            content_envelope(example_content()),
        )


def test_malformed_ownership_fails_the_snapshot_before_publication() -> None:
    """Criterion 3: a task pointing at an absent PRD yields no PublishableSnapshot."""
    snapshot = example_snapshot()
    task = task_named(snapshot, "release-alpha:T001")
    task["ref"]["prd_id"] = "release-ghost"
    task["scoped_id"] = "release-ghost:T001"
    pipeline = RecordingPipeline(
        describe_envelope(example_catalog()),
        snapshot_envelope(rehash_snapshot(snapshot)),
        content_envelope(example_content()),
    )
    with pytest.raises(StateSnapshotError, match="unknown PRD"):
        pipeline.snapshot_adapter.fetch()


def test_oversize_and_invalid_encoding_content_fail_before_publication() -> None:
    """Criterion 3: oversize and non-UTF-8 content are refused before a result."""
    oversize = example_content()
    body = "é" * 40000  # within the code-point ceiling, beyond the 64 KiB byte bound
    oversize["content"]["body"] = body
    oversize["content"]["truncated"] = False
    oversize["content"]["total_bytes"] = len(body.encode("utf-8"))
    over_pipeline = RecordingPipeline(
        describe_envelope(example_catalog()),
        snapshot_envelope(example_snapshot()),
        content_envelope(rehash_content(oversize)),
    )
    with pytest.raises(PrdContentError, match="64 KiB byte bound"):
        over_pipeline.content_adapter.fetch("release-beta")

    invalid = example_content()
    invalid["content"]["body"] = json.loads('"\\ud800"')  # lone surrogate: not UTF-8 encodable
    bad_pipeline = RecordingPipeline(
        describe_envelope(example_catalog()),
        snapshot_envelope(example_snapshot()),
        content_envelope(invalid),
    )
    with pytest.raises(PrdContentError, match="not valid UTF-8"):
        bad_pipeline.content_adapter.fetch("release-beta")


def test_digest_drift_on_either_read_fails_before_publication() -> None:
    """Criterion 3: an unrecomputed digest on either read fails closed."""
    drifted_snapshot = example_snapshot()
    drifted_snapshot["prds"][0]["title"] += "!"  # digest deliberately NOT recomputed
    snap_pipeline = RecordingPipeline(
        describe_envelope(example_catalog()),
        snapshot_envelope(drifted_snapshot),
        content_envelope(example_content()),
    )
    with pytest.raises(StateSnapshotError, match="digest mismatch"):
        snap_pipeline.snapshot_adapter.fetch()

    drifted_content = example_content()
    drifted_content["content"]["body"] += "\ninjected"  # digest deliberately NOT recomputed
    content_pipeline = RecordingPipeline(
        describe_envelope(example_catalog()),
        snapshot_envelope(example_snapshot()),
        content_envelope(drifted_content),
    )
    with pytest.raises(PrdContentError, match="digest mismatch"):
        content_pipeline.content_adapter.fetch("release-beta")


# --- Criterion 4: source and runtime probes (never touch state.db) ----------

#: State's SQLite storage (``state.db`` + its ``-journal``/``-wal``/``-shm``
#: siblings) or any ``.anvil`` state workspace path, and the SQLite driver.
_STATE_STORAGE = re.compile(r"state\.db|\.anvil\W{0,6}state|sqlite|\bapsw\b", re.IGNORECASE)
_PROHIBITION_DOC = re.compile(
    r"(never|not|no)\b.{0,80}(touch|open|copy|mount|mutat|storage)", re.IGNORECASE
)


def test_source_probe_no_integration_module_references_state_storage() -> None:
    """Criterion 4 (source probe): no adapter module names State storage.

    The only supported transport is the injected/bridge-configured CLI argv.
    None of the three integrated modules may reference ``state.db``, a
    ``.anvil`` state workspace path, or a SQLite driver except in a docstring
    that states the prohibition itself.
    """
    # Positive control: the scanner is live and would catch a real reference.
    assert _STATE_STORAGE.search("open('/var/anvil/state.db')")
    assert _STATE_STORAGE.search("import sqlite3")

    modules = [manifest_module, snapshot_module, content_module]
    violations: list[str] = []
    for module in modules:
        source = Path(module.__file__)
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
            if not _STATE_STORAGE.search(line):
                continue
            stripped = line.strip()
            is_doc = (
                stripped.startswith("#")
                or stripped.startswith("*")
                or ("=" not in line and "(" not in line)
            )
            # A documentation line that states the prohibition is allowed; any
            # executable reference to State storage is a violation.
            if not (is_doc and _PROHIBITION_DOC.search(line)):
                violations.append(f"{source.name}:{number}: {stripped}")
    # The strongest outcome: the integrated adapters never name State storage
    # at all (the pinned CLI argv is the only transport).
    assert violations == []


def test_runtime_probe_pipeline_never_shells_out_or_touches_state_db(
    monkeypatch, tmp_path: Path,
) -> None:
    """Criterion 4 (runtime probe): the pipeline opens no subprocess or state.db.

    With injected runners the integrated pipeline must never invoke
    ``subprocess.run`` at all, and no ``state.db`` (or ``.anvil`` workspace)
    may materialize in the working tree during a full discovery + both reads.
    """
    for module in (manifest_module, snapshot_module, content_module):
        def _forbidden_run(*_args, **_kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("the integrated pipeline must not shell out to State")

        monkeypatch.setattr(module.subprocess, "run", _forbidden_run)

    monkeypatch.chdir(tmp_path)
    pipeline = happy_pipeline()
    snapshot = pipeline.snapshot_adapter.fetch()
    content = pipeline.content_adapter.fetch("release-beta")

    assert snapshot.snapshot_digest and content.content_digest
    # No argv the pipeline handled its runners named State storage, and nothing
    # created a state database or a .anvil workspace on disk.
    for _operation, argv in pipeline.calls:
        assert not any(_STATE_STORAGE.search(token) for token in argv), argv
    assert not (tmp_path / "state.db").exists()
    assert not (tmp_path / ".anvil").exists()
    assert list(tmp_path.iterdir()) == []


def test_default_runner_probe_executes_only_the_configured_argv(
    monkeypatch, tmp_path: Path,
) -> None:
    """Criterion 4: the default (non-injected) runners shell out to the argv only.

    Exercises the real default subprocess path once for each surface to prove
    the only transport is the configured CLI command — never a hardcoded State
    path — and that a state database is never created as a side effect.
    """
    observed: list[list[str]] = []

    def fake_run(args, **kwargs):
        observed.append(list(args))
        assert kwargs["cwd"] == tmp_path
        if args[:1] == ["state-describe"]:
            return subprocess.CompletedProcess(args, 0, describe_envelope(example_catalog()), "")
        if args[:1] == ["state-snapshot"]:
            return subprocess.CompletedProcess(args, 0, snapshot_envelope(example_snapshot()), "")
        return subprocess.CompletedProcess(args, 0, content_envelope(example_content()), "")

    monkeypatch.setattr(manifest_module.subprocess, "run", fake_run)
    monkeypatch.setattr(snapshot_module.subprocess, "run", fake_run)
    monkeypatch.setattr(content_module.subprocess, "run", fake_run)

    pinned = StateManifestDiscovery("state-describe --json", cwd=tmp_path).pinned()
    snapshot = StateSnapshotAdapter(pinned, "state-snapshot --json", cwd=tmp_path).fetch()
    content = PrdContentAdapter(pinned, "state-content --json", cwd=tmp_path).fetch("release-beta")

    assert snapshot.project_id == "project_example"
    assert content.prd_id == "release-beta"
    assert observed == [
        ["state-describe", "--json"],
        ["state-snapshot", "--json"],
        ["state-content", "--json", "release-beta"],
    ]
    assert not (tmp_path / "state.db").exists()
