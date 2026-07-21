"""Project-local Workbench bridge.

The bridge is intentionally the only component allowed to touch a project
worktree or its GitHub credential.  It reads Anvil State through its documented
CLI and canonical event file; it never opens or mutates ``state.db``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from datetime import datetime, timezone

from .models import OperationRef, OperationRefusal, TypedOperationError, now_utc
from .redaction import redact_value
from .skills import (
    LocalSkill,
    SkillAdoptionStore,
    SkillError,
    SkillRegistry,
    assert_skills_acknowledged,
    skill_adoption_digest,
)


class BridgeError(RuntimeError):
    """A bridge operation could not complete safely."""


_SAFE_STATE_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

_CODEX_RUNTIME_ENV = frozenset({
    "COMSPEC", "LANG", "LC_ALL", "PATH", "PATHEXT", "PYTHONIOENCODING",
    "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "WINDIR",
})


def _allowlisted_environment(source: Mapping[str, str]) -> dict[str, str]:
    """Return only non-credential process variables needed to launch Codex."""
    allowed = {name.casefold() for name in _CODEX_RUNTIME_ENV}
    return {name: value for name, value in source.items() if name.casefold() in allowed}


def _toml_inline_table(values: Mapping[str, str]) -> str:
    return "{ " + ", ".join(
        f"{json.dumps(name)} = {json.dumps(value)}" for name, value in sorted(values.items())
    ) + " }"


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


class _CodexTokenHandler(BaseHTTPRequestHandler):
    server_version = "WorkbenchCodexToken/1"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        server = self.server
        if (
            self.client_address[0] != "127.0.0.1"
            or self.path != f"/token/{server.nonce}"
        ):
            self.send_error(404)
            return
        body = server.token.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        # A credential endpoint must never log its nonce or request path.
        return


class CodexTokenBroker:
    """Expose one run-scoped router token only to Codex provider auth."""

    def __init__(self, token: str) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _CodexTokenHandler)
        self._server.daemon_threads = True
        self._server.token = token
        self._server.nonce = secrets.token_urlsafe(32)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="workbench-codex-token", daemon=True,
        )

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}/token/{self._server.nonce}"

    def __enter__(self) -> CodexTokenBroker:
        self._thread.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


@dataclass(frozen=True)
class BridgeSettings:
    hub: str
    bridge_id: str
    token: str
    project_root: Path
    project_id: str
    state_events: Path | None
    cursor_file: Path
    state_status_command: str
    state_claim_command: str
    state_work_packet_command: str
    state_hook_command: str
    state_submit_command: str
    state_apply_command: str
    codex_binary: str
    router_base_url: str
    router_token_env: str
    codex_config: tuple[str, ...]
    state_describe_command: str = "anvil describe"
    worktrees: Mapping[str, Path] = field(default_factory=dict)
    provider_catalog_files: Mapping[str, Path] = field(default_factory=dict)
    skill_roots: tuple[Path, ...] = ()
    verification_commands: tuple[str, ...] = ()
    #: T008 (reviewed-tools-plugins): an OPTIONAL operator-declared LOCAL path to a
    #: reviewed skill-adoption ledger (a JSON array of ``{skill_id, digest,
    #: content_sha256?}`` acknowledgments).  When set, :class:`Bridge` builds the
    #: workflow-start adoption gate from it and a run REFUSES to start on an
    #: unacknowledged/since-changed skill digest (fail-closed, matching the browser
    #: surfaces).  When UNSET (``None``) the bridge is legacy-ungated -- the shipped
    #: poll loop runs selected skills exactly as before.  A live store may also be
    #: INJECTED into ``Bridge`` directly (tests/embedders); an injected store
    #: overrides this path, mirroring the ``create_app`` inject-or-default pattern.
    skill_adoption_ledger_file: Path | None = None


class HubTransport:
    def __init__(self, hub: str, bridge_id: str, token: str) -> None:
        self.hub = hub.rstrip("/")
        self.bridge_id = bridge_id
        self.token = token

    def request(self, method: str, path: str, payload: Any | None = None) -> Any:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Workbench-Bridge": self.bridge_id,
            "Accept": "application/json",
        }
        data = None
        if payload is not None:
            data = _json_bytes(payload)
            headers["Content-Type"] = "application/json"
        request = Request(self.hub + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=30) as response:  # nosec B310: hub is operator-configured tailnet origin
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise BridgeError(f"hub rejected {method} {path}: {exc.code} {body[:500]}") from exc
        except URLError as exc:
            raise BridgeError(f"cannot reach Workbench hub: {exc.reason}") from exc
        return json.loads(body) if body else None

    def next_command(self) -> dict[str, Any] | None:
        return self.request("GET", f"/api/bridge/{self.bridge_id}/commands/next")

    def acknowledge_command(self, command_id: str) -> None:
        self.request("POST", f"/api/bridge/{self.bridge_id}/commands/{command_id}/ack", {})

    def consume_approval_for_run(self, approval_id: str, approved_hash: str) -> None:
        self.request(
            "POST", f"/api/bridge/{self.bridge_id}/approvals/{approval_id}/consume-for-run",
            {"payload_hash": approved_hash},
        )

    def complete_approved_merge(self, approval_id: str, approved_hash: str, command_id: str) -> None:
        self.request(
            "POST", f"/api/bridge/{self.bridge_id}/approvals/{approval_id}/complete-merge",
            {"payload_hash": approved_hash, "command_id": command_id},
        )

    def event(self, run_id: str, role: str, content: Any) -> None:
        self.request("POST", f"/api/bridge/{self.bridge_id}/events", {"run_id": run_id, "role": role, "content": redact_value(content)})

    def run_status(self, run_id: str, status: str) -> None:
        self.request("POST", f"/api/bridge/{self.bridge_id}/runs/{run_id}/status", {"status": status})

    def finalize_run(self, run_id: str, status: str, command_id: str) -> None:
        self.request(
            "POST", f"/api/bridge/{self.bridge_id}/runs/{run_id}/finalize",
            {"status": status, "command_id": command_id},
        )

    def validate_run_lease(self, run_id: str) -> dict[str, Any]:
        result = self.request("GET", f"/api/bridge/{self.bridge_id}/runs/{run_id}/lease")
        if not isinstance(result, dict):
            raise BridgeError("hub returned an invalid run lease context")
        return result

    def renew_run_lease(self, run_id: str) -> None:
        self.request("POST", f"/api/bridge/{self.bridge_id}/runs/{run_id}/lease/renew", {})

    def release_run_lease(self, run_id: str) -> None:
        self.request("POST", f"/api/bridge/{self.bridge_id}/runs/{run_id}/lease/release", {})

    def evidence(self, source_kind: str, source_id: str, project_id: str, payload: dict[str, Any]) -> None:
        self.request(
            "POST", f"/api/bridge/{self.bridge_id}/evidence",
            {"source_kind": source_kind, "source_id": source_id, "project_id": project_id, "payload": redact_value(payload)},
        )

    def workflow_step(self, workflow_id: str, step_id: str, outcome: str) -> None:
        self.request(
            "POST", f"/api/bridge/{self.bridge_id}/workflows/{workflow_id}/steps/{step_id}",
            {"outcome": outcome},
        )

    def publish_skills(self, skills: list[dict[str, str]]) -> None:
        self.request("POST", f"/api/bridge/{self.bridge_id}/skills", {"skills": skills})


class StateReader:
    """CLI/event reader with a deliberately one-way State boundary."""

    def __init__(self, settings: BridgeSettings) -> None:
        self.settings = settings

    @staticmethod
    def _json_document(raw: str) -> dict[str, Any]:
        """Parse a JSON document even when a State CLI emits a status line first."""
        decoder = json.JSONDecoder()
        for position, character in enumerate(raw):
            if character not in "[{":
                continue
            try:
                value, end = decoder.raw_decode(raw[position:])
            except json.JSONDecodeError:
                continue
            if raw[position + end :].strip():
                continue
            if isinstance(value, dict):
                return value
        raise BridgeError("State command must return one JSON object")

    def _command_args(self, command: str, **values: str) -> list[str]:
        for name, value in values.items():
            if value and not _SAFE_STATE_VALUE.fullmatch(value):
                raise BridgeError(f"State {name} contains unsupported characters")
        rendered = command.format(**values)
        args = shlex.split(rendered, posix=os.name != "nt")
        if not args:
            raise BridgeError("State command is not configured")
        return args

    def _run(
        self, args: list[str], action: str, worktree_root: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            args, cwd=worktree_root or self.settings.project_root,
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[:500]
            raise BridgeError(f"State {action} command failed: {detail}")
        return completed

    def _event_path(self) -> Path:
        if self.settings.state_events is not None:
            return self.settings.state_events
        legacy = self.settings.project_root / ".anvil" / "events.jsonl"
        if legacy.exists():
            return legacy
        status = self._run(
            self._command_args(self.settings.state_status_command), "status",
        )
        match = re.search(r"(?m)^Path:\s*(.+?)\s*$", status.stdout)
        if match is None:
            raise BridgeError(
                "State event path could not be resolved; pass --state-events explicitly"
            )
        return Path(match.group(1)) / "events.jsonl"

    @staticmethod
    def _current_branch(worktree_root: Path) -> str:
        completed = subprocess.run(
            ["git", "branch", "--show-current"], cwd=worktree_root,
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        branch = completed.stdout.strip() if completed.returncode == 0 else ""
        if not branch:
            raise BridgeError("State claim left the leased worktree without a checked-out branch")
        return branch

    def claim(self, task_id: str, actor: str, worktree_root: Path | None = None) -> dict[str, Any]:
        if not task_id:
            raise BridgeError("State claim requires a task id")
        if not self.settings.state_claim_command:
            raise BridgeError("State claim command is not configured for this bridge")
        completed = self._run(
            self._command_args(self.settings.state_claim_command, task_id=task_id, actor=actor),
            "claim", worktree_root,
        )
        try:
            result = self._json_document(completed.stdout)
        except BridgeError:
            raise BridgeError("State claim must use its machine-readable --json envelope") from None
        claim_data = result.get("data")
        expected_branch = claim_data.get("branch") if isinstance(claim_data, dict) else None
        root = worktree_root or self.settings.project_root
        actual_branch = self._current_branch(root)
        if not isinstance(expected_branch, str) or expected_branch != actual_branch:
            raise BridgeError("State claim branch does not match the leased worktree branch")
        return result

    def work_packet(self, task_id: str, worktree_root: Path | None = None) -> dict[str, Any]:
        if not task_id:
            raise BridgeError("Codex runs require a State task id")
        completed = self._run(
            self._command_args(self.settings.state_work_packet_command, task_id=task_id, actor=""),
            "work-packet", worktree_root,
        )
        return self._json_document(completed.stdout)

    def capture_verification(
        self, command: str, exit_code: int, stdout: str, stderr: str, actor: str,
        worktree_root: Path | None = None,
    ) -> None:
        """Record the bridge's independently observed verification result in State."""
        stdout_path: Path | None = None
        stderr_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as stream:
                stream.write(stdout)
                stdout_path = Path(stream.name)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as stream:
                stream.write(stderr)
                stderr_path = Path(stream.name)
            args = self._command_args(self.settings.state_hook_command, task_id="", actor=actor)
            args.extend([
                "--command", command,
                "--exit-code", str(exit_code),
                "--stdout-file", str(stdout_path),
                "--stderr-file", str(stderr_path),
                "--actor", actor,
            ])
            self._run(args, "verification capture", worktree_root)
        finally:
            for path in (stdout_path, stderr_path):
                if path is not None:
                    try:
                        path.unlink()
                    except OSError:
                        pass

    def submit_evidence(
        self, task_id: str, commands: Iterable[str], files_changed: Iterable[str], actor: str,
        worktree_root: Path | None = None,
    ) -> dict[str, Any]:
        if not self.settings.state_submit_command:
            raise BridgeError("State submit command is not configured for this bridge")
        args = self._command_args(self.settings.state_submit_command, task_id=task_id, actor=actor)
        for command in commands:
            args.extend(["--commands", command])
        for file_name in files_changed:
            args.extend(["--files-changed", file_name])
        args.extend(["--actor", actor, "--json"])
        completed = self._run(args, "evidence submit", worktree_root)
        return self._json_document(completed.stdout)

    def apply_acceptance(self, task_id: str, worktree_root: Path | None = None) -> dict[str, Any]:
        if not task_id:
            raise BridgeError("State acceptance requires a task id")
        if not self.settings.state_apply_command:
            raise BridgeError("State apply command is not configured for this bridge")
        completed = self._run(
            self._command_args(self.settings.state_apply_command, task_id=task_id, actor=""),
            "acceptance", worktree_root,
        )
        return {"stdout": completed.stdout[-4000:]}

    def new_events(self) -> Iterable[tuple[int, dict[str, Any]]]:
        """Tail the canonical State event log only; no database file is opened."""
        offset = 0
        if self.settings.cursor_file.exists():
            try:
                offset = int(self.settings.cursor_file.read_text(encoding="utf-8").strip() or "0")
            except ValueError:
                offset = 0
        events_path = self._event_path()
        if not events_path.exists():
            return []
        events: list[tuple[int, dict[str, Any]]] = []
        with events_path.open("r", encoding="utf-8") as stream:
            stream.seek(offset)
            while line := stream.readline():
                next_offset = stream.tell()
                if not line.strip():
                    continue
                try:
                    events.append((next_offset, json.loads(line)))
                except json.JSONDecodeError:
                    continue
        return events

    def commit_cursor(self, offset: int) -> None:
        self.settings.cursor_file.parent.mkdir(parents=True, exist_ok=True)
        self.settings.cursor_file.write_text(str(offset), encoding="utf-8")


class CodexRunner:
    """Launch Codex only through an Anvil Serving Responses-compatible route."""

    def __init__(
        self,
        settings: BridgeSettings,
        emit: Callable[[str, Any], None],
        worktree_root: Path | None = None,
        *,
        skill_adoption_store: SkillAdoptionStore | None = None,
    ) -> None:
        self.settings = settings
        self.emit = emit
        self.worktree_root = worktree_root or settings.project_root
        # T008: when an owner adoption ledger is configured, a run refuses to
        # START if any selected bridge skill's exact reviewed digest has not been
        # acknowledged for adoption.  When None the gate is not exercised (the
        # legacy behaviour), so an operator that has not opted in is unaffected.
        self.skill_adoption_store = skill_adoption_store

    def run(self, run_id: str, work_packet: dict[str, Any], model: str, skills: Iterable[LocalSkill] = ()) -> int:
        selected_skills = tuple(skills)
        # T008 workflow-start adoption gate, BEFORE any router/model check and
        # BEFORE a skill body is ever assembled into the run prompt: an
        # unacknowledged (or since-changed) skill fails closed with a stable
        # typed refusal, so a new or changed skill cannot silently enter a run.
        if self.skill_adoption_store is not None and selected_skills:
            assert_skills_acknowledged(
                ((skill.skill_id, skill_adoption_digest(skill)) for skill in selected_skills),
                self.skill_adoption_store,
            )
        token = os.environ.get(self.settings.router_token_env, "")
        if not self.settings.router_base_url or not token:
            raise BridgeError("Anvil router base URL and local router token environment variable are required")
        if not model.strip():
            raise BridgeError("Codex runs require a Workbench-selected Anvil model route")
        skills_prompt = ""
        if selected_skills:
            skills_prompt = "\n\nUse only these operator-approved bridge skills when applicable:\n" + "\n\n".join(
                f"### Skill: {skill.skill_id}\n{skill.instructions}" for skill in selected_skills
            )
        prompt = (
            "You are executing an Anvil State work packet. Work only in the current project. "
            "Run the relevant tests, collect evidence, and do not create a GitHub PR or merge.\n\n"
            + json.dumps(work_packet, indent=2, ensure_ascii=True)
            + skills_prompt
        )
        correlation_headers = {
            "X-Anvil-Workbench-Run-Id": run_id,
            "X-Request-Id": f"codex-{run_id}",
        }
        task_id = work_packet.get("task_id")
        if isinstance(task_id, str) and task_id:
            correlation_headers["X-Anvil-Task-Id"] = task_id
        header_toml = ", ".join(
            f"{json.dumps(name)} = {json.dumps(value)}" for name, value in correlation_headers.items()
        )
        environment = _allowlisted_environment(os.environ)
        tool_environment = dict(environment)
        default_config = (
            f"model={json.dumps(model)}",
            'model_provider="anvil"',
            'model_providers.anvil.name="Anvil Serving"',
            f'model_providers.anvil.base_url="{self.settings.router_base_url.rstrip("/")}"',
            'model_providers.anvil.wire_api="responses"',
            # Hosted web search is represented as a non-function Responses
            # tool and would bypass the project-local tool boundary. The
            # bridge deliberately gives Codex local shell tools only; any
            # future retrieval integration must be a reviewed bridge tool.
            'web_search="disabled"',
            # Do not inherit or inject user-oriented tools into a project
            # bridge. Those integrations can carry their own credentials and
            # tool namespaces; Workbench has a separate reviewed bridge
            # contract for any future integration.
            "features.plugins=false",
            "features.apps=false",
            "features.multi_agent=false",
            "features.browser_use=false",
            "features.computer_use=false",
            "features.image_generation=false",
            f"model_providers.anvil.http_headers={{ {header_toml} }}",
        )
        security_config = (
            # The router bearer token exists only in the Codex supervisor
            # process. Managed shell tools inherit no ambient environment and
            # receive only this non-credential runtime allowlist.
            'shell_environment_policy.inherit="none"',
            f"shell_environment_policy.set={_toml_inline_table(tool_environment)}",
            "shell_environment_policy.ignore_default_excludes=false",
            "sandbox_workspace_write.network_access=false",
        )
        with CodexTokenBroker(token) as broker:
            auth_config = (
                f"model_providers.anvil.auth.command={json.dumps(sys.executable)}",
                "model_providers.anvil.auth.args=["
                + ", ".join(
                    json.dumps(value) for value in ("-I", "-m", "workbench.codex_auth", broker.endpoint)
                )
                + "]",
                "model_providers.anvil.auth.timeout_ms=5000",
                "model_providers.anvil.auth.refresh_interval_ms=0",
            )
            command = [
                self.settings.codex_binary, "--ask-for-approval", "never", "exec", "--json", "-C", str(self.worktree_root),
                # The bridge's own sandbox/approval contract is authoritative.
                # Project rules are unreviewed input to this supervisor and must
                # not silently add external tool surfaces to a managed run.
                "--sandbox", "workspace-write", "--ignore-user-config", "--ignore-rules",
            ]
            for entry in (*default_config, *auth_config, *self.settings.codex_config, *security_config):
                command.extend(["-c", entry])
            command.append(prompt)
            self.emit("bridge.codex.started", {
                "command": [self.settings.codex_binary, "--ask-for-approval", "never", "exec", "--json"],
                "router": self.settings.router_base_url,
                "model": model,
            })
            process = subprocess.Popen(
                command, cwd=self.worktree_root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=environment,
            )
            assert process.stdout is not None
            for line in process.stdout:
                line = line.rstrip("\n")
                try:
                    event: Any = json.loads(line)
                except json.JSONDecodeError:
                    event = {"type": "codex.output", "text": line}
                self.emit("codex.event", event)
            code = process.wait()
        self.emit("bridge.codex.finished", {"exit_code": code})
        return code


@dataclass(frozen=True)
class VerificationResult:
    command: str
    exit_code: int
    output_sha256: str


@dataclass(frozen=True)
class GitSnapshot:
    """Exact working-tree state represented by an isolated Git index."""

    diff_sha256: str
    tree_sha: str
    changed_files: tuple[str, ...]


def _git_snapshot(worktree_root: Path) -> GitSnapshot:
    """Stage the complete working tree in a temporary index without mutating it."""
    index_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False) as stream:
            index_path = Path(stream.name)
        index_path.unlink()
        environment = dict(os.environ)
        environment["GIT_INDEX_FILE"] = str(index_path)

        def git(*args: str) -> bytes:
            completed = subprocess.run(
                ("git", *args), cwd=worktree_root, env=environment,
                capture_output=True, check=False,
            )
            if completed.returncode != 0:
                detail = completed.stderr.decode("utf-8", errors="replace").strip()[:500]
                raise BridgeError(f"Git snapshot failed ({args[0]}): {detail}")
            return completed.stdout

        git("read-tree", "HEAD")
        git("add", "-A")
        diff = git("diff", "--binary", "--no-ext-diff", "--cached", "HEAD")
        names = git("diff", "--name-only", "-z", "--relative", "--cached", "HEAD")
        tree_sha = git("write-tree").decode("ascii").strip()
        changed = tuple(
            item.decode("utf-8", errors="surrogateescape").replace("\\", "/")
            for item in names.split(b"\0") if item
        )
        return GitSnapshot(hashlib.sha256(diff).hexdigest(), tree_sha, changed)
    finally:
        if index_path is not None:
            try:
                index_path.unlink()
            except OSError:
                pass


class VerificationRunner:
    """Run packet-declared checks outside Codex and attest their actual output to State."""

    def __init__(self, settings: BridgeSettings, emit: Callable[[str, Any], None], worktree_root: Path | None = None) -> None:
        self.settings = settings
        self.emit = emit
        self.worktree_root = worktree_root or settings.project_root

    def _commands(self, work_packet: dict[str, Any]) -> tuple[tuple[str, tuple[str, ...]], ...]:
        task = work_packet.get("task")
        verification = task.get("verification") if isinstance(task, dict) else None
        commands = verification.get("commands") if isinstance(verification, dict) else None
        if not isinstance(commands, list) or not commands or not all(isinstance(item, str) and item.strip() for item in commands):
            raise BridgeError("State work packet has no runnable verification commands")
        configured: dict[str, tuple[str, ...]] = {}
        for command in self.settings.verification_commands:
            rendered = command.strip()
            if not rendered:
                continue
            # State packet commands are matched byte-for-byte to locally
            # configured text, then parsed as a portable argv form.  In
            # particular, this preserves a quoted Windows executable path
            # without reintroducing a command shell.
            argv = tuple(shlex.split(rendered, posix=True))
            if not argv:
                raise BridgeError("bridge verification command allowlist is invalid")
            configured[rendered] = argv
        if not configured:
            raise BridgeError("bridge has no operator-configured verification command allowlist")
        selected: list[tuple[str, tuple[str, ...]]] = []
        for command in commands:
            rendered = command.strip()
            argv = configured.get(rendered)
            if argv is None:
                raise BridgeError("State verification command is not in the bridge allowlist")
            selected.append((rendered, argv))
        return tuple(selected)

    def run(
        self, work_packet: dict[str, Any], state: StateReader, actor: str,
    ) -> tuple[VerificationResult, ...]:
        results: list[VerificationResult] = []
        for command, argv in self._commands(work_packet):
            completed = subprocess.run(
                argv, cwd=self.worktree_root, shell=False, capture_output=True,
                text=True, encoding="utf-8", errors="replace", check=False,
                env=_allowlisted_environment(os.environ),
            )
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            state.capture_verification(
                command, completed.returncode, stdout, stderr, actor, self.worktree_root,
            )
            digest = hashlib.sha256((stdout + stderr).encode("utf-8")).hexdigest()
            result = VerificationResult(command, completed.returncode, digest)
            results.append(result)
            self.emit("bridge.verification.finished", {
                "command": command, "exit_code": completed.returncode, "output_sha256": digest,
            })
            if completed.returncode != 0:
                break
        return tuple(results)

    def changed_likely_files(self, work_packet: dict[str, Any]) -> tuple[str, ...]:
        task = work_packet.get("task")
        likely_files = task.get("likely_files") if isinstance(task, dict) else None
        if not isinstance(likely_files, list):
            return ()
        changed = set(_git_snapshot(self.worktree_root).changed_files)
        return tuple(str(item) for item in likely_files if isinstance(item, str) and item.replace("\\", "/") in changed)


class ApprovedActionRunner:
    """Hash-check and execute the two GitHub mutations the browser cannot perform."""

    def __init__(self, settings: BridgeSettings, worktree_root: Path | None = None) -> None:
        self.settings = settings
        self.worktree_root = worktree_root or settings.project_root

    def _run(self, *args: str, environment: Mapping[str, str] | None = None) -> str:
        completed = subprocess.run(
            args, cwd=self.worktree_root, capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False, env=environment,
        )
        if completed.returncode != 0:
            raise BridgeError(f"command failed ({args[0]}): {completed.stderr.strip()[:500]}")
        return completed.stdout.strip()

    def diff_hash(self) -> str:
        return _git_snapshot(self.worktree_root).diff_sha256

    def commit_pr(self, payload: dict[str, Any]) -> dict[str, Any]:
        expected = str(payload.get("diff_hash", ""))
        snapshot = _git_snapshot(self.worktree_root)
        actual = snapshot.diff_sha256
        if not expected or expected != actual:
            raise BridgeError("current diff differs from the hash that was approved")
        title = str(payload.get("title", "Anvil Workbench delivery"))
        branch = str(payload.get("branch", ""))
        if not branch:
            raise BridgeError("approved PR action is missing its target branch")
        base = str(payload.get("base", "main"))
        # Populate the real index with the exact tree that was just verified.
        # A file-system change after this point cannot broaden the approved
        # commit, because no second `git add` reads from the working tree.
        self._run("git", "read-tree", snapshot.tree_sha)
        self._run("git", "commit", "-m", title)
        self._run("git", "push", "-u", "origin", branch)
        pr_url = self._run("gh", "pr", "create", "--base", base, "--head", branch, "--title", title, "--fill")
        head_sha = self._run("git", "rev-parse", "HEAD")
        return {"pr_url": pr_url, "diff_hash": actual, "head_sha": head_sha}

    def merge_and_accept(self, payload: dict[str, Any], state: StateReader) -> dict[str, Any]:
        pr = str(payload.get("pr", ""))
        task_id = str(payload.get("task_id", ""))
        expected_head_sha = str(payload.get("expected_head_sha", ""))
        if not pr or not task_id or not re.fullmatch(r"[0-9a-f]{40,64}", expected_head_sha):
            raise BridgeError("merge action requires PR reference, State task id, and expected head SHA")
        observed_head_sha = self._run("gh", "pr", "view", pr, "--json", "headRefOid", "--jq", ".headRefOid")
        if observed_head_sha != expected_head_sha:
            raise BridgeError("pull request head differs from the hash that was approved")
        self._run("gh", "pr", "checks", pr, "--required")
        self._run("gh", "pr", "merge", pr, "--merge", "--delete-branch", "--match-head-commit", expected_head_sha)
        acceptance = state.apply_acceptance(task_id, self.worktree_root)
        return {"pr": pr, "task_id": task_id, "head_sha": expected_head_sha, "state_acceptance": acceptance}


# ---------------------------------------------------------------------------
# Immediate bridge authority preflight for a typed operation
# (state-context-operations:T006.2)
# ---------------------------------------------------------------------------
#
# Before ANY adapter touches an effect, the bridge re-derives every authority
# fact from its OWN locally configured catalogs/profile and the live lease --
# it never trusts the hub's validation.  This preflight rechecks, in order and
# fail-closed with a stable typed :class:`OperationRefusal` code:
#
# 1. the command envelope and its expiry (a stale command never runs);
# 2. the worktree lease, re-read IMMEDIATELY before the effect (fenced+expiring:
#    a missing/expired/epoch-changed lease stops the run);
# 3. the pinned work-packet digest (a changed packet stops and reconciles);
# 4. the descriptor pin: the local catalog digest is recomputed, matched to the
#    run snapshot, and the operation resolved at its exact pinned digest, then
#    checked against the local profile allowlist;
# 5. the typed input against the pinned local input schema;
# 6. for an approval-gated effect, a matching, unexpired, hash-bound, one-time
#    approval consumed atomically -- a replayed or mismatched grant fails closed.
#
# Every refusal names the failed fact via its code and a redacted summary; no
# credential, raw payload, adapter, or path is exposed.  This is the bridge-side
# counterpart to the hub's :func:`workbench.workflows.resolve_operation` and
# :func:`workbench.contracts.validate_bridge_command_snapshot`.  It is a
# hermetic, in-memory validator: it is deliberately NOT wired into
# :meth:`Bridge.poll_once`, so the live poll loop still dispatches only the
# v1 ``run_codex``/``skill_probe``/``commit_pr``/``merge_and_accept`` commands
# until an ``invoke_operation`` adapter path is separately reviewed and enabled.


def _op_refuse(code: str, summary: str) -> TypedOperationError:
    return TypedOperationError(OperationRefusal(code, summary))


@dataclass(frozen=True)
class OperationLeaseState:
    """The live fenced worktree lease the bridge re-reads before an effect."""

    worktree_name: str
    epoch: int
    expires_at: datetime


@dataclass(frozen=True)
class PreflightedOperation:
    """The bridge-private result of a passing preflight, ready for dispatch.

    ``bridge_adapter`` is the local adapter the bridge would invoke; it is
    resolved from the bridge's OWN configured catalog and never leaves the host.
    """

    operation: OperationRef
    effect: str
    bridge_adapter: str
    inputs: Mapping[str, Any]
    lease_epoch: int
    approval_grant_id: str | None


def _parse_rfc3339(value: Any, code: str, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise _op_refuse(code, f"{label} timestamp is missing")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise _op_refuse(code, f"{label} timestamp is not a valid RFC 3339 value") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _local_operation(catalog: Mapping[str, Any], ref: OperationRef) -> Mapping[str, Any]:
    """Resolve one operation in the bridge's local catalog at its exact digest."""
    candidates = [
        operation for operation in catalog.get("operations", [])
        if isinstance(operation, Mapping)
        and operation.get("id") == ref.id
        and operation.get("contract_version") == ref.contract_version
    ]
    if not candidates:
        raise _op_refuse(
            "operation.unknown",
            f"the operation is not present in the local {ref.provider} catalog: {ref.id} {ref.contract_version}",
        )
    for operation in candidates:
        if operation.get("operation_digest") == ref.operation_digest:
            return operation
    raise _op_refuse(
        "operation.digest_drift",
        f"the pinned operation digest no longer matches the local {ref.provider} catalog: {ref.id}",
    )


def preflight_operation_command(
    command: Mapping[str, Any],
    *,
    catalogs: Mapping[str, Mapping[str, Any]],
    profile: Mapping[str, Any],
    lease_authority: Callable[[str], OperationLeaseState | None],
    approval_consumer: Any | None = None,
    pinned_work_packet_digest: str | None = None,
    current_work_packet_digest: str | None = None,
    now: datetime | None = None,
) -> PreflightedOperation:
    """Fail-closed re-derive every authority fact before a typed operation effect.

    ``catalogs`` is the bridge's OWN ``{provider: catalog_dict}`` local
    configuration (full descriptors with execution/gate blocks), ``profile`` its
    local pinned profile dict, and ``lease_authority`` a callback that returns
    the LIVE lease for a worktree name (re-read at call time) or ``None``.  On
    success returns a bridge-private :class:`PreflightedOperation`; on any failed
    fact raises :class:`TypedOperationError` with a stable code and a redacted
    summary.  Reuses the reviewed digest/schema/approval primitives in
    :mod:`workbench.contracts`.
    """
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import ValidationError

    from .contracts import (
        ContractValidationError,
        approval_payload_digest,
        check_operation_input_schema,
        validate_catalog,
    )

    now = now or now_utc()
    if not isinstance(command, Mapping):
        raise _op_refuse("command.malformed", "bridge command is not an object")
    if command.get("kind") != "invoke_operation":
        raise _op_refuse("command.malformed", "bridge command is not an invoke_operation command")
    payload = command.get("payload")
    snapshot = command.get("workflow_snapshot")
    lease_block = command.get("lease")
    if not isinstance(payload, Mapping) or not isinstance(snapshot, Mapping):
        raise _op_refuse("command.malformed", "invoke_operation command has no payload or workflow snapshot")
    if not isinstance(lease_block, Mapping):
        raise _op_refuse("command.malformed", "invoke_operation command has no lease block")

    # 1. Command expiry -- a stale command never reaches an effect.
    expires_at = _parse_rfc3339(command.get("expires_at"), "command.expired", "command expiry")
    if now >= expires_at:
        raise _op_refuse("command.expired", "the bridge command expired before preflight completed")

    # 2. Lease recheck, IMMEDIATELY before the effect (fenced + expiring).
    worktree_name = lease_block.get("worktree_name")
    epoch = lease_block.get("epoch")
    if not isinstance(worktree_name, str) or not worktree_name or not isinstance(epoch, int) or isinstance(epoch, bool):
        raise _op_refuse("command.malformed", "invoke_operation lease block is invalid")
    live = lease_authority(worktree_name)
    if live is None:
        raise _op_refuse("lease.missing", "the worktree lease is no longer held by this run")
    if live.expires_at <= now:
        raise _op_refuse("lease.expired", "the worktree lease expired before the effect")
    if live.epoch != epoch:
        raise _op_refuse("lease.epoch_mismatch", "the worktree lease epoch changed; the run was fenced out")

    # 3. Work-packet digest -- a changed packet stops and reconciles.
    if pinned_work_packet_digest is not None and current_work_packet_digest != pinned_work_packet_digest:
        raise _op_refuse("work_packet.digest_changed", "the work packet changed since the run snapshot was pinned")

    # 4. Descriptor pin against the LOCAL catalog (recomputed digest).
    ref = OperationRef.from_mapping(payload.get("operation"), "command operation")
    catalog = catalogs.get(ref.provider)
    if not isinstance(catalog, Mapping):
        raise _op_refuse("operation.provider_unknown", f"the operation provider is not locally configured: {ref.provider}")
    try:
        validate_catalog(catalog)
    except ContractValidationError as exc:
        raise _op_refuse("operation.digest_drift", f"the local {ref.provider} catalog failed its digest recompute: {exc}") from exc
    snapshot_catalogs = snapshot.get("catalogs")
    if not isinstance(snapshot_catalogs, list):
        raise _op_refuse("command.malformed", "workflow snapshot catalogs are invalid")
    pinned = next(
        (entry.get("digest") for entry in snapshot_catalogs
         if isinstance(entry, Mapping) and entry.get("provider") == ref.provider),
        None,
    )
    if pinned != catalog.get("catalog_digest"):
        raise _op_refuse("operation.digest_drift", f"the local {ref.provider} catalog digest differs from the pinned run snapshot")
    if snapshot.get("capability_profile_digest") != profile.get("digest"):
        raise _op_refuse("operation.unprofiled", "the run snapshot capability-profile digest differs from the local profile")
    operation = _local_operation(catalog, ref)

    # 5. Profile allowlist + typed input against the pinned LOCAL input schema.
    profile_keys = {
        (str(item.get("provider")), str(item.get("id")), str(item.get("contract_version")), str(item.get("operation_digest")))
        for item in profile.get("operations", []) if isinstance(item, Mapping)
    }
    if ref.key not in profile_keys:
        raise _op_refuse("operation.unprofiled", f"the operation is not allowlisted by the local profile: {ref.provider} {ref.id}")
    inputs = payload.get("inputs")
    if not isinstance(inputs, Mapping):
        raise _op_refuse("operation.input_not_object", "the operation input must be an object")
    input_schema = operation.get("input_schema")
    try:
        check_operation_input_schema(input_schema)
    except ContractValidationError as exc:
        raise _op_refuse("operation.schema_unresolvable", f"the local operation input schema {exc}") from exc
    try:
        Draft202012Validator(dict(input_schema)).validate(dict(inputs))
    except ValidationError as exc:
        raise _op_refuse("operation.input_invalid", f"the operation input does not match the pinned schema: {exc.message}") from exc
    except Exception as exc:
        raise _op_refuse("operation.schema_unresolvable", f"the local operation input schema cannot be evaluated: {exc}") from exc

    # 6. Approval binding + atomic one-time consumption for a gated effect.
    gates = operation.get("gates")
    grant_id: str | None = None
    if isinstance(gates, Mapping) and gates.get("human_approval") == "required":
        approval = payload.get("approval")
        if not isinstance(approval, Mapping):
            raise _op_refuse("approval.missing", "the approval-gated operation requires a typed approval grant")
        grant_id = approval.get("grant_id")
        if not grant_id or command.get("approval_grant_id") != grant_id:
            raise _op_refuse("approval.missing", "the approval-gated operation has no matching approval grant id")
        if approval.get("action") != gates.get("approval_action"):
            raise _op_refuse("approval.action_mismatch", "the approval action does not match the operation gate")
        if approval.get("payload_hash") != approval_payload_digest(dict(inputs)):
            raise _op_refuse("approval.hash_mismatch", "the approval hash does not bind the exact operation inputs")
        if approval_consumer is None:
            raise _op_refuse("approval.missing", "the approval-gated operation requires an atomic approval consumer")
        try:
            approval_consumer.consume(
                str(approval["grant_id"]), str(approval["action"]), str(approval["payload_hash"]),
                str(command.get("bridge_id", "")), str(command.get("project_id", "")),
            )
        except TypedOperationError:
            raise
        except Exception as exc:
            raise _op_refuse(
                "approval.invalid",
                "the approval grant is missing, expired, replayed, or not bound to this bridge and project",
            ) from exc

    execution = operation.get("execution")
    bridge_adapter = str(execution.get("bridge_adapter")) if isinstance(execution, Mapping) else ""
    return PreflightedOperation(
        operation=ref,
        effect=str(operation.get("effect")),
        bridge_adapter=bridge_adapter,
        inputs=dict(inputs),
        lease_epoch=epoch,
        approval_grant_id=grant_id,
    )


def load_skill_adoption_store(path: Path) -> SkillAdoptionStore:
    """Build the T008 adoption gate from an operator-reviewed LOCAL ledger file.

    The ledger is a JSON array of acknowledgment records -- ``{skill_id, digest,
    content_sha256?, description?, acknowledged_by?}``.  Each is replayed through
    the store's validated ``acknowledge`` path, so a malformed ``sha256:`` digest
    is refused AT LOAD TIME (the gate is never seeded from a bad record) and every
    description is scrubbed before it can enter the ledger.  This is the operator's
    opt-in: when this file is declared the workflow-start gate is ENFORCED; when it
    is unset the bridge stays legacy-ungated.
    """
    from .store import MemorySkillAdoptionStore, SkillAdoptionStoreError

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BridgeError(f"skill adoption ledger is unreadable: {exc}") from exc
    if not isinstance(raw, list):
        raise BridgeError("skill adoption ledger must be a JSON array of acknowledgments")
    store = MemorySkillAdoptionStore()
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise BridgeError("skill adoption ledger entry must be an object")
        try:
            store.acknowledge(
                str(entry.get("skill_id", "")),
                str(entry.get("digest", "")),
                description=str(entry.get("description", "")),
                content_sha256=str(entry.get("content_sha256", "")),
                acknowledged_by=str(entry.get("acknowledged_by", "operator")),
            )
        except SkillAdoptionStoreError as exc:
            raise BridgeError(f"skill adoption ledger has an invalid acknowledgment: {exc}") from exc
    return store


class Bridge:
    def __init__(
        self,
        settings: BridgeSettings,
        *,
        skill_adoption_store: SkillAdoptionStore | None = None,
    ) -> None:
        self.settings = settings
        self.hub = HubTransport(settings.hub, settings.bridge_id, settings.token)
        self.state = StateReader(settings)
        self.skills = SkillRegistry(settings.skill_roots)
        self._published_skill_hash = ""
        # T008 workflow-start adoption gate wiring.  UNCONFIGURED (no injected
        # store and no declared ledger) is the legacy-UNGATED default: the shipped
        # poll loop runs selected skills exactly as before, matching the
        # already-tested opt-out.  CONFIGURED (an injected store, or a reviewed
        # ledger declared on the settings) ENFORCES the gate -- CodexRunner then
        # refuses an unacknowledged/since-changed skill digest at workflow start
        # (fail-closed), like the browser surfaces.  An injected store overrides
        # the ledger path, mirroring the create_app inject-or-default pattern.
        if skill_adoption_store is None and settings.skill_adoption_ledger_file is not None:
            skill_adoption_store = load_skill_adoption_store(settings.skill_adoption_ledger_file)
        self.skill_adoption_store = skill_adoption_store

    def _emit(self, run_id: str, role: str, content: Any) -> None:
        self.hub.event(run_id, role, content)

    def _worktree_root(self, payload: dict[str, Any]) -> Path:
        """Resolve a bridge-configured worktree id, never a browser-supplied path."""
        worktree_id = str(payload.get("worktree_id") or "default")
        roots = {"default": self.settings.project_root, **self.settings.worktrees}
        root = roots.get(worktree_id)
        if root is None:
            raise BridgeError(f"worktree is not configured on this bridge: {worktree_id}")
        root = root.resolve()
        if not root.is_dir():
            raise BridgeError(f"configured worktree is unavailable: {worktree_id}")
        return root

    def _approved_action_worktree(self, payload: dict[str, Any]) -> tuple[str, Path]:
        """Bind an approved GitHub effect to the same live session worktree."""
        run_id = payload.get("run_id")
        session_id = payload.get("session_id")
        worktree_id = payload.get("worktree_id")
        lease_epoch = payload.get("lease_epoch")
        if not all(isinstance(value, str) and value for value in (run_id, session_id, worktree_id)) or not isinstance(lease_epoch, int):
            raise BridgeError("approved action must bind run, session, worktree, and lease epoch")
        lease = self.hub.validate_run_lease(run_id)
        if (
            lease.get("session_id") != session_id
            or lease.get("worktree_id") != worktree_id
            or lease.get("lease_epoch") != lease_epoch
        ):
            raise BridgeError("approved action binding differs from the active run worktree lease")
        return run_id, self._worktree_root({"worktree_id": worktree_id})

    def project_state_events(self) -> int:
        count = 0
        for offset, event in self.state.new_events():
            event_id = str(event.get("id") or event.get("event_id") or f"offset-{offset}")
            self.hub.evidence("state_event", event_id, self.settings.project_id, {"event": event, "task_id": event.get("task_id")})
            self.state.commit_cursor(offset)
            count += 1
        return count

    def _publish_skills(self) -> dict[str, LocalSkill]:
        """Publish only reviewed metadata; skill bodies and paths stay local."""
        if not self.settings.skill_roots:
            return {}
        try:
            discovered = self.skills.discover()
        except SkillError as exc:
            raise BridgeError(str(exc)) from exc
        metadata = [skill.metadata() for _, skill in sorted(discovered.items())]
        fingerprint = hashlib.sha256(_json_bytes(metadata)).hexdigest()
        if fingerprint != self._published_skill_hash:
            self.hub.publish_skills(metadata)
            self._published_skill_hash = fingerprint
        return discovered

    def _selected_skills(self, payload: dict[str, Any], available: Mapping[str, LocalSkill]) -> tuple[LocalSkill, ...]:
        requested = payload.get("skills", [])
        if not isinstance(requested, list):
            raise BridgeError("bridge skill payload is invalid")
        selected: list[LocalSkill] = []
        for expected in requested:
            if not isinstance(expected, dict):
                raise BridgeError("bridge skill payload is invalid")
            skill_id = str(expected.get("skill_id", ""))
            skill = available.get(skill_id)
            if skill is None:
                raise BridgeError(f"requested skill is not configured on this bridge: {skill_id}")
            if str(expected.get("content_sha256", "")) != skill.content_sha256:
                raise BridgeError(f"requested skill changed since it was selected: {skill_id}")
            selected.append(skill)
        return tuple(selected)

    def poll_once(self) -> bool:
        available_skills = self._publish_skills()
        self.project_state_events()
        command = self.hub.next_command()
        if command is None:
            return False
        action = command["action_type"]
        payload = command["payload"]
        if action == "skill_probe":
            try:
                selected = self._selected_skills(payload, available_skills)
                if not selected:
                    raise BridgeError("skill probe has no selected bridge skills")
            except BridgeError as exc:
                self.hub.evidence("failure", f"skills:{command['id']}:reconciliation", self.settings.project_id, {
                    "kind": "bridge_skill_probe",
                    "reconciliation_required": True,
                    "error": str(exc),
                })
                self.hub.acknowledge_command(str(command["id"]))
                raise
            self.hub.evidence("evaluation", f"skills:{command['id']}", self.settings.project_id, {
                "kind": "bridge_skill_probe",
                "skills": [skill.metadata() for skill in selected],
                "result": "all selected skills resolved with matching digests",
            })
            self.hub.acknowledge_command(str(command["id"]))
            return True
        if action == "run_codex":
            run_id = payload["run_id"]
            task_id = str(payload.get("task_id") or "")
            actor = f"workbench-{self.settings.bridge_id}"
            lease_stop = threading.Event()
            lease_errors: list[str] = []
            lease_thread: threading.Thread | None = None

            def keep_lease_alive() -> None:
                while not lease_stop.wait(60):
                    try:
                        self.hub.renew_run_lease(run_id)
                    except BridgeError as exc:
                        lease_errors.append(str(exc))
                        return

            def require_live_lease() -> None:
                if lease_errors:
                    raise BridgeError("run worktree lease renewal failed; reconciliation is required")

            try:
                worktree_root = self._worktree_root(payload)
                if payload.get("session_id"):
                    self.hub.validate_run_lease(run_id)
                self.hub.run_status(run_id, "running")
                if payload.get("session_id"):
                    lease_thread = threading.Thread(target=keep_lease_alive, name=f"workbench-lease-{run_id}", daemon=True)
                    lease_thread.start()
                claim = self.state.claim(task_id, actor, worktree_root)
                self.hub.evidence("state_event", f"{run_id}:claim", self.settings.project_id, {"task_id": task_id, "claim": claim})
                packet = self.state.work_packet(task_id, worktree_root)
                packet.setdefault("task_id", payload.get("task_id"))
                directives = payload.get("directives", [])
                if isinstance(directives, list):
                    packet["workbench_directives"] = [str(item) for item in directives if isinstance(item, str)][-32:]
                self.hub.evidence("work_packet", run_id, self.settings.project_id, {"task_id": payload.get("task_id"), "packet": packet})
                model = str(payload.get("model") or "")
                selected_skills = self._selected_skills(payload, available_skills)
                exit_code = CodexRunner(
                    self.settings, lambda role, content: self._emit(run_id, role, content), worktree_root,
                    # T008: when the operator configured an adoption ledger (or
                    # injected a store) the runner refuses to START on an
                    # unacknowledged/changed skill digest; when None the gate is
                    # not exercised (legacy-ungated).
                    skill_adoption_store=self.skill_adoption_store,
                ).run(run_id, packet, model, selected_skills)
                require_live_lease()
                if exit_code:
                    raise BridgeError(f"Codex exited with {exit_code}")
                verifier = VerificationRunner(
                    self.settings, lambda role, content: self._emit(run_id, role, content), worktree_root,
                )
                verification = verifier.run(packet, self.state, actor)
                require_live_lease()
                failed = [result.command for result in verification if result.exit_code != 0]
                if failed:
                    raise BridgeError(f"independent verification failed: {failed[0]}")
                files_changed = verifier.changed_likely_files(packet)
                if not files_changed:
                    raise BridgeError("no packet-declared changed files found; State evidence was not submitted")
                submission = self.state.submit_evidence(
                    task_id, (result.command for result in verification), files_changed, actor,
                    worktree_root,
                )
                require_live_lease()
                self.hub.evidence("state_event", f"{run_id}:evidence", self.settings.project_id, {
                    "task_id": task_id,
                    "files_changed": list(files_changed),
                    "commands": [result.command for result in verification],
                    "evidence_id": submission.get("data", {}).get("evidence_id") if isinstance(submission.get("data"), dict) else None,
                })
            except BridgeError as exc:
                self.hub.evidence("failure", f"{run_id}:reconciliation", self.settings.project_id, {
                    "task_id": task_id, "fingerprint": "bridge-reconciliation", "reconciliation_required": True, "error": str(exc),
                })
                self.hub.finalize_run(run_id, "reconciliation", str(command["id"]))
                raise
            else:
                self.hub.finalize_run(run_id, "evidenced", str(command["id"]))
                return True
            finally:
                lease_stop.set()
                if lease_thread is not None:
                    lease_thread.join(timeout=2)
        approval_id = command.get("approval_id")
        if not approval_id:
            raise BridgeError(f"unapproved bridge action rejected: {action}")
        if action not in {"commit_pr", "merge_and_accept"}:
            raise BridgeError(f"approval action is not implemented by the v1 bridge: {action}")
        run_id, worktree_root = self._approved_action_worktree(payload)
        runner = ApprovedActionRunner(self.settings, worktree_root)
        lease_stop = threading.Event()
        lease_errors: list[str] = []
        lease_thread: threading.Thread | None = None
        completion_acknowledged = False

        def keep_action_lease_alive() -> None:
            while not lease_stop.wait(60):
                try:
                    self.hub.renew_run_lease(run_id)
                except BridgeError as exc:
                    lease_errors.append(str(exc))
                    return

        def require_action_lease() -> None:
            if lease_errors:
                raise BridgeError("approved action worktree lease renewal failed; reconciliation is required")

        try:
            if action == "commit_pr":
                if str(payload.get("diff_hash") or "") != runner.diff_hash():
                    raise BridgeError("current diff differs from the hash that was approved")
                # The hub revalidates the approval binding and renews the exact
                # lease in one transaction before the external GitHub effect.
                self.hub.consume_approval_for_run(approval_id, command["payload_hash"])
                lease_thread = threading.Thread(
                    target=keep_action_lease_alive, name=f"workbench-action-lease-{run_id}", daemon=True,
                )
                lease_thread.start()
                result = runner.commit_pr(payload)
                require_action_lease()
                self.hub.evidence("pull_request", approval_id, self.settings.project_id, result)
            elif action == "merge_and_accept":
                self.hub.consume_approval_for_run(approval_id, command["payload_hash"])
                lease_thread = threading.Thread(
                    target=keep_action_lease_alive, name=f"workbench-action-lease-{run_id}", daemon=True,
                )
                lease_thread.start()
                result = runner.merge_and_accept(payload, self.state)
                require_action_lease()
                self.hub.evidence("pull_request", approval_id, self.settings.project_id, result)
                self.hub.complete_approved_merge(approval_id, command["payload_hash"], str(command["id"]))
                completion_acknowledged = True
        except BridgeError as exc:
            self.hub.evidence("failure", approval_id, self.settings.project_id, {"action": action, "reconciliation_required": True, "error": str(exc)})
            self.hub.run_status(run_id, "reconciliation")
            self.hub.acknowledge_command(str(command["id"]))
            raise
        finally:
            lease_stop.set()
            if lease_thread is not None:
                lease_thread.join(timeout=2)
        # A successful PR retains the lease until merge/accept (or expiry), so
        # its later hash-bound merge approval can still bind the same checkout.
        # ``completed`` releases the matching lease atomically with the status
        # transition, so there is no stale second release after merge/accept.
        if not completion_acknowledged:
            self.hub.acknowledge_command(str(command["id"]))
        return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a project-local Anvil Workbench bridge")
    parser.add_argument("--hub", required=True, help="private tailnet Workbench hub URL")
    parser.add_argument("--bridge-id", required=True)
    parser.add_argument("--token-env", default="WORKBENCH_BRIDGE_TOKEN")
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--state-events", type=Path, help="canonical State events.jsonl; resolves through State CLI when omitted")
    parser.add_argument("--cursor-file", type=Path)
    parser.add_argument("--state-status-command", default="anvil status")
    parser.add_argument(
        "--state-describe-command", default="anvil describe",
        help="State CLI manifest command used to discover and pin read-operation descriptors",
    )
    parser.add_argument("--state-claim-command", default="anvil claim {task_id} --actor {actor} --json")
    parser.add_argument("--state-work-packet-command", default="anvil packet {task_id} --format json")
    parser.add_argument("--state-hook-command", default="anvil hook capture-evidence")
    parser.add_argument("--state-submit-command", default="anvil submit {task_id}")
    parser.add_argument("--state-apply-command", default="")
    parser.add_argument("--codex-binary", default="codex")
    parser.add_argument("--router-base-url", required=True)
    parser.add_argument("--router-token-env", default="ANVIL_ROUTER_TOKEN")
    parser.add_argument("--codex-config", action="append", default=[])
    parser.add_argument("--worktree", action="append", default=[], metavar="ID=PATH", help="allow a named local worktree for concurrent sessions")
    parser.add_argument(
        "--provider-catalog", action="append", default=[], metavar="PROVIDER=PATH",
        help="allow one reviewed local operation-catalog JSON file for a named provider",
    )
    parser.add_argument("--skills-root", action="append", default=[], type=Path, help="allow explicit local SKILL.md roots for this bridge")
    parser.add_argument("--skill-adoption-ledger", type=Path, default=None, help="opt in to the T008 skill-adoption gate from a reviewed local JSON ledger; unset = legacy-ungated")
    parser.add_argument("--verification-command", action="append", default=[], help="allow one exact State verification command; it runs without a shell")
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--once", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    token = os.environ.get(args.token_env, "")
    if not token:
        raise SystemExit(f"{args.token_env} is required in the local bridge environment")
    root = args.project_root.resolve()
    worktrees: dict[str, Path] = {}
    for item in args.worktree:
        worktree_id, separator, raw_path = item.partition("=")
        if not separator or not worktree_id.strip() or not raw_path.strip():
            raise SystemExit("--worktree must use ID=PATH")
        if worktree_id.strip() == "default":
            raise SystemExit("default is reserved for --project-root")
        if worktree_id.strip() in worktrees:
            raise SystemExit(f"duplicate --worktree id: {worktree_id.strip()}")
        worktrees[worktree_id.strip()] = Path(raw_path.strip()).resolve()
    provider_catalog_files: dict[str, Path] = {}
    for item in args.provider_catalog:
        provider, separator, raw_path = item.partition("=")
        if not separator or not provider.strip() or not raw_path.strip():
            raise SystemExit("--provider-catalog must use PROVIDER=PATH")
        provider = provider.strip()
        if provider in provider_catalog_files:
            raise SystemExit(f"duplicate --provider-catalog provider: {provider}")
        provider_catalog_files[provider] = Path(raw_path.strip()).resolve()
    settings = BridgeSettings(
        hub=args.hub, bridge_id=args.bridge_id, token=token, project_root=root, project_id=args.project_id,
        state_events=args.state_events,
        cursor_file=args.cursor_file or root / ".workbench" / "state-events.cursor",
        state_status_command=args.state_status_command,
        state_describe_command=args.state_describe_command,
        state_claim_command=args.state_claim_command,
        state_work_packet_command=args.state_work_packet_command,
        state_hook_command=args.state_hook_command,
        state_submit_command=args.state_submit_command,
        state_apply_command=args.state_apply_command,
        codex_binary=args.codex_binary, router_base_url=args.router_base_url,
        router_token_env=args.router_token_env, codex_config=tuple(args.codex_config),
        worktrees=worktrees, provider_catalog_files=provider_catalog_files,
        skill_roots=tuple(path.resolve() for path in args.skills_root),
        verification_commands=tuple(args.verification_command),
        skill_adoption_ledger_file=args.skill_adoption_ledger.resolve() if args.skill_adoption_ledger else None,
    )
    bridge = Bridge(settings)
    while True:
        try:
            bridge.poll_once()
        except BridgeError as exc:
            print(f"workbench bridge: {exc}", file=sys.stderr)
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(max(args.interval, 0.25))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
