"""Generate and load a live delivery-projection seed for the Explorer.

The browser Explorer serves a PRD's real body through
``GET /api/projects/{project_id}/prds/{prd_id}/content``, which reads from a
:class:`~workbench.delivery_projection.DeliveryProjectionStore`.  In production
that store is ``None`` (503) because the display projection is deliberately not
wired into the live bridge poll loop.  This module bridges that gap without
granting the browser any new authority: it produces a bounded, fully validated
*seed* from the supported State surface, then loads it into a hub-durable
projection store so ``Select a PRD -> show its content`` works against real
State data.

Two halves, both fail-closed:

* :func:`generate_seed` shells the SUPPORTED ``anvil`` State CLI through the
  hermetically qualified read adapters -- :class:`StateManifestDiscovery`
  (pins the ``anvil-operation-catalog/v1`` read set), :class:`StateSnapshotAdapter`
  (``state.project.snapshot``), and :class:`PrdContentAdapter`
  (``state.prd.read_content``).  Every payload is validated by the exact same
  contract validators the projection store enforces before a single seed file
  is written; any nonconforming CLI output aborts the whole run and writes no
  partial seed.
* :func:`load_seed_dir` reads a seed directory and captures each record into a
  :class:`DeliveryProjectionStore` through its real ``capture_*`` methods (a
  second, independent contract gate).  It validates the entire seed into a
  throwaway store first, so an invalid or tampered file fails the whole load
  with no partial capture into the caller's store.

Authority boundary (AGENTS.md / CLAUDE.md): the ONLY State access here is the
supported ``anvil`` CLI's declared read output, routed through the pinned read
adapters.  This module never opens ``state.db`` and never issues a raw command
outside the adapter allowlist.  The seed carries only bounded, redacted display
data -- a PRD body, task titles/statuses -- never a path, credential, or command.

Live-qualification note (fakoli/anvil#178): the current ``anvil describe``
envelope advertises CLI/MCP command *names* but does not yet advertise the
``anvil-operation-catalog/v1`` operation catalog the pinned adapters require, so
:func:`generate_seed` fails closed at discovery against today's live CLI.  That
is the honest upstream gap, not a defect here; a fixture-manifest CLI (or the
upstream catalog once it lands) drives the generator end-to-end.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .contracts import (
    ContractValidationError,
    validate_prd_content,
    validate_task_reference,
)
from .delivery_projection import (
    DeliveryProjectionError,
    DeliveryProjectionStore,
    MemoryDeliveryProjectionStore,
)
from .prd_content_adapter import PrdContentAdapter
from .state_manifest import StateManifestDiscovery
from .state_snapshot_adapter import PublishableSnapshot, StateSnapshotAdapter

#: The seed manifest schema version. The loader refuses any other version.
SEED_SCHEMA_VERSION = "workbench-projection-seed/v1"
#: The manifest file the generator writes LAST and the loader keys off of, so a
#: crashed generator leaves no loadable seed.
SEED_MANIFEST_NAME = "seed-manifest.json"

#: The supported State-CLI read commands the generator shells.  Overridable so a
#: fixture-manifest CLI can drive the generator hermetically; the defaults name
#: the intended live ``anvil`` read surface (gated on fakoli/anvil#178).  The
#: content command has the scoped ``prd_id`` appended by the adapter as its
#: final argv token; none is ever passed through a shell.
DEFAULT_DESCRIBE_COMMAND = "anvil describe"
DEFAULT_SNAPSHOT_COMMAND = "anvil snapshot --json"
DEFAULT_CONTENT_COMMAND = "anvil prd read-content --json"

_PRD_CONTENT_KIND = "prd_content"
_TASK_REFERENCE_KIND = "task_reference"


class ProjectionSeedError(RuntimeError):
    """A seed cannot be generated or loaded and must fail closed."""


# --------------------------------------------------------------------------- #
# Generator
# --------------------------------------------------------------------------- #


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def derive_task_reference(
    snapshot: PublishableSnapshot, task: Mapping[str, Any], prd_index: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Project one snapshot task into a ``workbench-task-reference/v1`` payload.

    Reads only the already-validated snapshot payload; it invents no field the
    snapshot lacks.  The owning PRD's revision (and each dependency's owning-PRD
    revision) is resolved from the snapshot's own PRD set, so a reference is
    always pinned to a revision that exists in the same snapshot.  The result is
    contract-validated before it is returned.
    """
    source = snapshot.payload["source"]
    ref = task["ref"]
    owning_prd_id = str(ref["prd_id"])
    task_id = str(ref["task_id"])
    if owning_prd_id not in prd_index:
        raise ProjectionSeedError(
            f"snapshot task {task.get('scoped_id')!r} references a PRD absent from the snapshot: {owning_prd_id}"
        )
    prd_revision = int(prd_index[owning_prd_id]["revision"])
    scoped_id = str(task["scoped_id"])

    summary: dict[str, Any] = {
        "content_trust": str(task["content_trust"]),
        "title": str(task["title"]),
        "status": str(task["status"]),
    }
    if task.get("priority") is not None:
        summary["priority"] = str(task["priority"])
    depends_on = task.get("depends_on")
    if isinstance(depends_on, list) and depends_on:
        projected_deps: list[dict[str, Any]] = []
        for dependency in depends_on:
            dep_prd_id = str(dependency["prd_id"])
            if dep_prd_id not in prd_index:
                raise ProjectionSeedError(
                    f"task {scoped_id!r} depends on a task whose PRD is absent from the snapshot: {dep_prd_id}"
                )
            projected_deps.append({
                "prd_id": dep_prd_id,
                "task_id": str(dependency["task_id"]),
                "prd_revision": int(prd_index[dep_prd_id]["revision"]),
            })
        summary["depends_on"] = projected_deps

    hierarchy: dict[str, Any] = {
        "prd_id": owning_prd_id,
        "prd_title": str(prd_index[owning_prd_id]["title"]),
    }
    if task.get("feature_id") is not None:
        hierarchy["feature_id"] = str(task["feature_id"])

    reference = {
        "schema_version": "workbench-task-reference/v1",
        "ref": {"prd_id": owning_prd_id, "task_id": task_id, "prd_revision": prd_revision},
        "scoped_id": scoped_id,
        "run_label": f"{scoped_id}@r{prd_revision}",
        "source": {
            "provider": "anvil-state",
            "provider_contract_version": str(source["provider_contract_version"]),
            "read_operation_id": str(source["read_operation_id"]),
            "snapshot_digest": snapshot.snapshot_digest,
        },
        "hierarchy": hierarchy,
        "summary": summary,
    }
    try:
        validate_task_reference(reference)
    except ContractValidationError as exc:
        raise ProjectionSeedError(f"derived task reference for {scoped_id!r} is not valid: {exc}") from exc
    return reference


def _sanitize_component(value: str) -> str:
    """Render one id as a filesystem-safe, collision-free filename component."""
    return "".join(char if (char.isalnum() or char in "._-") else "_" for char in value)


def generate_seed(
    project_id: str,
    workspace_cwd: str | os.PathLike[str],
    out_dir: str | os.PathLike[str],
    *,
    describe_command: str = DEFAULT_DESCRIBE_COMMAND,
    snapshot_command: str = DEFAULT_SNAPSHOT_COMMAND,
    content_command: str = DEFAULT_CONTENT_COMMAND,
    describe_runner: Callable[[Sequence[str]], str] | None = None,
    snapshot_runner: Callable[[Any, Sequence[str]], str] | None = None,
    content_runner: Callable[[Any, Sequence[str]], str] | None = None,
) -> dict[str, Any]:
    """Shell the supported State CLI and write a validated projection seed.

    All CLI reads and contract validations complete IN FULL before any file is
    written, so a nonconforming payload aborts with no partial seed on disk.
    The per-PRD content payloads come from the pinned ``state.prd.read_content``
    adapter and the per-task references are projected from the pinned
    ``state.project.snapshot`` adapter; both are validated by the same
    validators the projection store enforces.

    Returns a summary ``{project_id, prds, tasks, prd_ids, snapshot_digest, out_dir}``.
    """
    workspace = Path(workspace_cwd)
    if not workspace.is_dir():
        raise ProjectionSeedError(f"workspace_cwd is not a directory: {workspace}")

    # 1. Pin the supported read set from the live provider manifest (fail-closed).
    discovery = StateManifestDiscovery(describe_command, runner=describe_runner, cwd=workspace)
    pinned = discovery.pinned()

    # 2. Read one bounded, digest-keyed project snapshot.
    snapshot = StateSnapshotAdapter(pinned, snapshot_command, runner=snapshot_runner, cwd=workspace).fetch()
    if snapshot.project_id != project_id:
        raise ProjectionSeedError(
            f"snapshot names project {snapshot.project_id!r}, not the requested {project_id!r}; "
            "refusing to seed a mislabeled projection"
        )
    payload = snapshot.payload
    prd_index: dict[str, Mapping[str, Any]] = {str(prd["prd_id"]): prd for prd in payload["prds"]}

    # 3. Project every task into a validated task reference.
    task_files: list[tuple[str, dict[str, Any], str]] = []  # (scoped_id, payload, relpath)
    for task in payload["tasks"]:
        reference = derive_task_reference(snapshot, task, prd_index)
        scoped_id = str(reference["scoped_id"])
        relpath = f"task-reference/{_sanitize_component(project_id)}/{_sanitize_component(scoped_id)}.json"
        task_files.append((scoped_id, reference, relpath))

    # 4. Read every PRD's bounded content through the pinned content adapter.
    prd_files: list[tuple[str, dict[str, Any], str]] = []  # (prd_id, payload, relpath)
    for prd_id, prd in prd_index.items():
        content_adapter = PrdContentAdapter(pinned, content_command, runner=content_runner, cwd=workspace)
        published = content_adapter.fetch(prd_id, expected_revision=int(prd["revision"]))
        document = published.payload
        # Independent re-validation, mirroring the store's own capture gate.
        try:
            validate_prd_content(document)
        except ContractValidationError as exc:
            raise ProjectionSeedError(f"PRD content for {prd_id!r} failed validation: {exc}") from exc
        relpath = f"prd-content/{_sanitize_component(project_id)}/{_sanitize_component(prd_id)}.json"
        prd_files.append((prd_id, document, relpath))

    # 5. Everything validated -> write the seed. Record files first, manifest last.
    entries: list[dict[str, Any]] = []
    files_to_write: list[tuple[str, str]] = []
    for prd_id, document, relpath in prd_files:
        files_to_write.append((relpath, json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False)))
        entries.append({"kind": _PRD_CONTENT_KIND, "project_id": project_id, "prd_id": prd_id, "path": relpath})
    for scoped_id, reference, relpath in task_files:
        files_to_write.append((relpath, json.dumps(reference, indent=2, sort_keys=True, ensure_ascii=False)))
        entries.append({"kind": _TASK_REFERENCE_KIND, "project_id": project_id, "scoped_id": scoped_id, "path": relpath})

    manifest = {
        "schema_version": SEED_SCHEMA_VERSION,
        "generated_at": _now_rfc3339(),
        "source": {
            "provider": pinned.provider,
            "catalog_version": pinned.catalog_version,
            "catalog_digest": pinned.catalog_digest,
            "snapshot_digest": snapshot.snapshot_digest,
        },
        "entries": entries,
    }

    _write_seed_tree(Path(out_dir), files_to_write, manifest)

    return {
        "project_id": project_id,
        "prds": len(prd_files),
        "tasks": len(task_files),
        "prd_ids": [prd_id for prd_id, _doc, _rel in prd_files],
        "snapshot_digest": snapshot.snapshot_digest,
        "out_dir": str(Path(out_dir)),
    }


def _write_seed_tree(out_dir: Path, files: Sequence[tuple[str, str]], manifest: Mapping[str, Any]) -> None:
    """Write record files then the manifest; clean up on any write failure.

    The manifest is written last, so a partially written tree (crash mid-write)
    is not loadable: :func:`load_seed_dir` requires the manifest.
    """
    written: list[Path] = []
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        for relpath, text in files:
            target = out_dir / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            written.append(target)
        manifest_path = out_dir / SEED_MANIFEST_NAME
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8",
        )
        written.append(manifest_path)
    except OSError as exc:
        for path in written:
            try:
                path.unlink()
            except OSError:
                pass
        raise ProjectionSeedError(f"failed to write seed to {out_dir}: {exc}") from exc


# --------------------------------------------------------------------------- #
# Loader
# --------------------------------------------------------------------------- #


def _read_seed_json(seed_dir: Path, relpath: str) -> Any:
    """Read one seed file, refusing any path that escapes the seed directory."""
    if not isinstance(relpath, str) or not relpath:
        raise ProjectionSeedError("seed manifest entry has no path")
    target = (seed_dir / relpath).resolve()
    root = seed_dir.resolve()
    if root != target and root not in target.parents:
        raise ProjectionSeedError(f"seed entry path escapes the seed directory: {relpath!r}")
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProjectionSeedError(f"seed file is missing: {relpath}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectionSeedError(f"seed file is not readable JSON: {relpath}: {exc}") from exc


def load_seed_dir(store: DeliveryProjectionStore, seed_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Load a seed directory into ``store``, fail-closed and all-or-nothing.

    The whole seed is validated into a throwaway projection store first; only if
    every record captures cleanly there does it get captured into the caller's
    ``store``.  An invalid, tampered, or missing file raises
    :class:`ProjectionSeedError` before the caller's store is touched, so a bad
    seed can never leave a partial projection behind.

    Returns a summary ``{prds, tasks, projects}``.
    """
    seed_path = Path(seed_dir)
    manifest_path = seed_path / SEED_MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProjectionSeedError(f"seed manifest is missing: {manifest_path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectionSeedError(f"seed manifest is not readable JSON: {exc}") from exc

    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != SEED_SCHEMA_VERSION:
        raise ProjectionSeedError(
            f"seed manifest is not a {SEED_SCHEMA_VERSION} document; refusing to load"
        )
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise ProjectionSeedError("seed manifest has no entries list")

    # Materialize every record up front so a bad file aborts before any capture.
    records: list[tuple[str, str, dict[str, Any]]] = []  # (kind, project_id, payload)
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ProjectionSeedError("seed manifest entry is not an object")
        kind = entry.get("kind")
        project_id = entry.get("project_id")
        if kind not in (_PRD_CONTENT_KIND, _TASK_REFERENCE_KIND):
            raise ProjectionSeedError(f"seed manifest entry has an unknown kind: {kind!r}")
        if not isinstance(project_id, str) or not project_id:
            raise ProjectionSeedError("seed manifest entry has no project_id")
        payload = _read_seed_json(seed_path, entry.get("path"))
        if not isinstance(payload, dict):
            raise ProjectionSeedError(f"seed record is not a JSON object: {entry.get('path')!r}")
        records.append((str(kind), project_id, payload))

    # Phase 1: validate the entire seed into a throwaway store (the real capture
    # gate) so nothing reaches the caller's store unless all of it is valid.
    def _capture_all(target: DeliveryProjectionStore) -> tuple[int, int, set[str]]:
        prds = tasks = 0
        projects: set[str] = set()
        for kind, project_id, payload in records:
            projects.add(project_id)
            try:
                if kind == _PRD_CONTENT_KIND:
                    target.capture_prd_content(project_id, payload)
                    prds += 1
                else:
                    target.capture_task_reference(project_id, payload)
                    tasks += 1
            except DeliveryProjectionError as exc:
                raise ProjectionSeedError(f"seed record failed the projection contract gate: {exc}") from exc
        return prds, tasks, projects

    _capture_all(MemoryDeliveryProjectionStore())

    # Phase 2: commit into the caller's store (validated a second time here).
    prds, tasks, projects = _capture_all(store)
    return {"prds": prds, "tasks": tasks, "projects": len(projects)}


# --------------------------------------------------------------------------- #
# CLI entry
# --------------------------------------------------------------------------- #


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m workbench.projection_seed",
        description="Generate a validated delivery-projection seed from the supported anvil State CLI.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    generate = sub.add_parser("generate", help="Generate a seed from a real State workspace.")
    generate.add_argument("--project-id", required=True, help="The project id the snapshot must name.")
    generate.add_argument("--cwd", required=True, help="The State workspace working directory.")
    generate.add_argument("--out", required=True, help="Output directory for the seed files.")
    generate.add_argument("--describe-command", default=DEFAULT_DESCRIBE_COMMAND)
    generate.add_argument("--snapshot-command", default=DEFAULT_SNAPSHOT_COMMAND)
    generate.add_argument("--content-command", default=DEFAULT_CONTENT_COMMAND)
    args = parser.parse_args(argv)

    if args.command == "generate":
        try:
            summary = generate_seed(
                args.project_id, args.cwd, args.out,
                describe_command=args.describe_command,
                snapshot_command=args.snapshot_command,
                content_command=args.content_command,
            )
        except (ProjectionSeedError, RuntimeError) as exc:
            print(f"seed generation failed (fail-closed): {exc}", file=sys.stderr)
            return 1
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess/CLI
    raise SystemExit(_main())
