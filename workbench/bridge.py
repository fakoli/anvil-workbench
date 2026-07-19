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
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .redaction import redact_value
from .skills import LocalSkill, SkillError, SkillRegistry


class BridgeError(RuntimeError):
    """A bridge operation could not complete safely."""


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


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
    worktrees: Mapping[str, Path] = field(default_factory=dict)
    skill_roots: tuple[Path, ...] = ()


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

    def consume(self, approval_id: str, approved_hash: str) -> None:
        self.request(
            "POST", f"/api/bridge/{self.bridge_id}/approvals/{approval_id}/consume",
            {"payload_hash": approved_hash},
        )

    def event(self, run_id: str, role: str, content: Any) -> None:
        self.request("POST", f"/api/bridge/{self.bridge_id}/events", {"run_id": run_id, "role": role, "content": redact_value(content)})

    def run_status(self, run_id: str, status: str) -> None:
        self.request("POST", f"/api/bridge/{self.bridge_id}/runs/{run_id}/status", {"status": status})

    def validate_run_lease(self, run_id: str) -> None:
        self.request("GET", f"/api/bridge/{self.bridge_id}/runs/{run_id}/lease")

    def renew_run_lease(self, run_id: str) -> None:
        self.request("POST", f"/api/bridge/{self.bridge_id}/runs/{run_id}/lease/renew", {})

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
        rendered = command.format(**values)
        args = shlex.split(rendered, posix=os.name != "nt")
        if not args:
            raise BridgeError("State command is not configured")
        return args

    def _run(self, args: list[str], action: str) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            args, cwd=self.settings.project_root, capture_output=True, text=True, check=False,
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

    def claim(self, task_id: str, actor: str) -> dict[str, Any]:
        if not task_id:
            raise BridgeError("State claim requires a task id")
        if not self.settings.state_claim_command:
            raise BridgeError("State claim command is not configured for this bridge")
        completed = self._run(
            self._command_args(self.settings.state_claim_command, task_id=task_id, actor=actor),
            "claim",
        )
        try:
            return self._json_document(completed.stdout)
        except BridgeError:
            return {"stdout": completed.stdout[-1000:]}

    def work_packet(self, task_id: str) -> dict[str, Any]:
        if not task_id:
            raise BridgeError("Codex runs require a State task id")
        completed = self._run(
            self._command_args(self.settings.state_work_packet_command, task_id=task_id, actor=""),
            "work-packet",
        )
        return self._json_document(completed.stdout)

    def capture_verification(
        self, command: str, exit_code: int, stdout: str, stderr: str, actor: str,
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
            self._run(args, "verification capture")
        finally:
            for path in (stdout_path, stderr_path):
                if path is not None:
                    try:
                        path.unlink()
                    except OSError:
                        pass

    def submit_evidence(
        self, task_id: str, commands: Iterable[str], files_changed: Iterable[str], actor: str,
    ) -> dict[str, Any]:
        if not self.settings.state_submit_command:
            raise BridgeError("State submit command is not configured for this bridge")
        args = self._command_args(self.settings.state_submit_command, task_id=task_id, actor=actor)
        for command in commands:
            args.extend(["--commands", command])
        for file_name in files_changed:
            args.extend(["--files-changed", file_name])
        args.extend(["--actor", actor, "--json"])
        completed = self._run(args, "evidence submit")
        return self._json_document(completed.stdout)

    def apply_acceptance(self, task_id: str) -> dict[str, Any]:
        if not task_id:
            raise BridgeError("State acceptance requires a task id")
        if not self.settings.state_apply_command:
            raise BridgeError("State apply command is not configured for this bridge")
        completed = self._run(
            self._command_args(self.settings.state_apply_command, task_id=task_id, actor=""),
            "acceptance",
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

    def __init__(self, settings: BridgeSettings, emit: Callable[[str, Any], None], worktree_root: Path | None = None) -> None:
        self.settings = settings
        self.emit = emit
        self.worktree_root = worktree_root or settings.project_root

    def run(self, run_id: str, work_packet: dict[str, Any], model: str, skills: Iterable[LocalSkill] = ()) -> int:
        token = os.environ.get(self.settings.router_token_env, "")
        if not self.settings.router_base_url or not token:
            raise BridgeError("Anvil router base URL and local router token environment variable are required")
        if not model.strip():
            raise BridgeError("Codex runs require a Workbench-selected Anvil model route")
        selected_skills = tuple(skills)
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
        default_config = (
            f"model={json.dumps(model)}",
            'model_provider="anvil"',
            'model_providers.anvil.name="Anvil Serving"',
            f'model_providers.anvil.base_url="{self.settings.router_base_url.rstrip("/")}"',
            'model_providers.anvil.env_key="ANVIL_ROUTER_TOKEN"',
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
        command = [
            self.settings.codex_binary, "--ask-for-approval", "never", "exec", "--json", "-C", str(self.worktree_root),
            # The bridge's own sandbox/approval contract is authoritative.
            # Project rules are unreviewed input to this supervisor and must
            # not silently add external tool surfaces to a managed run.
            "--sandbox", "workspace-write", "--ignore-user-config", "--ignore-rules",
        ]
        for entry in (*default_config, *self.settings.codex_config):
            command.extend(["-c", entry])
        command.append(prompt)
        environment = dict(os.environ)
        environment["ANVIL_ROUTER_TOKEN"] = token
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


class VerificationRunner:
    """Run packet-declared checks outside Codex and attest their actual output to State."""

    def __init__(self, settings: BridgeSettings, emit: Callable[[str, Any], None], worktree_root: Path | None = None) -> None:
        self.settings = settings
        self.emit = emit
        self.worktree_root = worktree_root or settings.project_root

    @staticmethod
    def _commands(work_packet: dict[str, Any]) -> tuple[str, ...]:
        task = work_packet.get("task")
        verification = task.get("verification") if isinstance(task, dict) else None
        commands = verification.get("commands") if isinstance(verification, dict) else None
        if not isinstance(commands, list) or not commands or not all(isinstance(item, str) and item.strip() for item in commands):
            raise BridgeError("State work packet has no runnable verification commands")
        return tuple(item.strip() for item in commands)

    def run(self, work_packet: dict[str, Any], state: StateReader, actor: str) -> tuple[VerificationResult, ...]:
        results: list[VerificationResult] = []
        for command in self._commands(work_packet):
            completed = subprocess.run(
                command, cwd=self.worktree_root, shell=True, capture_output=True,
                text=True, encoding="utf-8", errors="replace", check=False,
            )
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            state.capture_verification(command, completed.returncode, stdout, stderr, actor)
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
        completed = subprocess.run(
            ["git", "diff", "--name-only", "--relative", "HEAD"], cwd=self.worktree_root,
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        if completed.returncode != 0:
            raise BridgeError("bridge requires a committed project baseline before it can submit evidence")
        changed = {line.strip().replace("\\", "/") for line in completed.stdout.splitlines() if line.strip()}
        return tuple(str(item) for item in likely_files if isinstance(item, str) and item.replace("\\", "/") in changed)


class ApprovedActionRunner:
    """Hash-check and execute the two GitHub mutations the browser cannot perform."""

    def __init__(self, settings: BridgeSettings) -> None:
        self.settings = settings

    def _run(self, *args: str) -> str:
        completed = subprocess.run(args, cwd=self.settings.project_root, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise BridgeError(f"command failed ({args[0]}): {completed.stderr.strip()[:500]}")
        return completed.stdout.strip()

    def diff_hash(self) -> str:
        diff = self._run("git", "diff", "--binary", "--no-ext-diff", "HEAD")
        return hashlib.sha256(diff.encode("utf-8")).hexdigest()

    def commit_pr(self, payload: dict[str, Any]) -> dict[str, Any]:
        expected = str(payload.get("diff_hash", ""))
        actual = self.diff_hash()
        if not expected or expected != actual:
            raise BridgeError("current diff differs from the hash that was approved")
        title = str(payload.get("title", "Anvil Workbench delivery"))
        branch = str(payload.get("branch", ""))
        if not branch:
            raise BridgeError("approved PR action is missing its target branch")
        base = str(payload.get("base", "main"))
        self._run("git", "add", "-A")
        self._run("git", "commit", "-m", title)
        self._run("git", "push", "-u", "origin", branch)
        pr_url = self._run("gh", "pr", "create", "--base", base, "--head", branch, "--title", title, "--fill")
        return {"pr_url": pr_url, "diff_hash": actual}

    def merge_and_accept(self, payload: dict[str, Any], state: StateReader) -> dict[str, Any]:
        pr = str(payload.get("pr", ""))
        task_id = str(payload.get("task_id", ""))
        if not pr or not task_id:
            raise BridgeError("merge action requires both PR reference and State task id")
        self._run("gh", "pr", "checks", pr, "--required")
        self._run("gh", "pr", "merge", pr, "--merge", "--delete-branch")
        acceptance = state.apply_acceptance(task_id)
        return {"pr": pr, "task_id": task_id, "state_acceptance": acceptance}


class Bridge:
    def __init__(self, settings: BridgeSettings) -> None:
        self.settings = settings
        self.hub = HubTransport(settings.hub, settings.bridge_id, settings.token)
        self.state = StateReader(settings)
        self.skills = SkillRegistry(settings.skill_roots)
        self._published_skill_hash = ""

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
                claim = self.state.claim(task_id, actor)
                self.hub.evidence("state_event", f"{run_id}:claim", self.settings.project_id, {"task_id": task_id, "claim": claim})
                packet = self.state.work_packet(task_id)
                packet.setdefault("task_id", payload.get("task_id"))
                directives = payload.get("directives", [])
                if isinstance(directives, list):
                    packet["workbench_directives"] = [str(item) for item in directives if isinstance(item, str)][-32:]
                self.hub.evidence("work_packet", run_id, self.settings.project_id, {"task_id": payload.get("task_id"), "packet": packet})
                model = str(payload.get("model") or "")
                selected_skills = self._selected_skills(payload, available_skills)
                exit_code = CodexRunner(
                    self.settings, lambda role, content: self._emit(run_id, role, content), worktree_root,
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
                )
                require_live_lease()
                self.hub.evidence("state_event", f"{run_id}:evidence", self.settings.project_id, {
                    "task_id": task_id,
                    "files_changed": list(files_changed),
                    "commands": [result.command for result in verification],
                    "evidence_id": submission.get("data", {}).get("evidence_id") if isinstance(submission.get("data"), dict) else None,
                })
                workflow_id = payload.get("workflow_id")
                workflow_step_id = payload.get("workflow_step_id")
                if isinstance(workflow_id, str) and isinstance(workflow_step_id, str):
                    self.hub.workflow_step(workflow_id, workflow_step_id, "succeeded")
            except BridgeError as exc:
                self.hub.evidence("failure", f"{run_id}:reconciliation", self.settings.project_id, {
                    "task_id": task_id, "fingerprint": "bridge-reconciliation", "reconciliation_required": True, "error": str(exc),
                })
                workflow_id = payload.get("workflow_id")
                workflow_step_id = payload.get("workflow_step_id")
                if isinstance(workflow_id, str) and isinstance(workflow_step_id, str):
                    try:
                        self.hub.workflow_step(workflow_id, workflow_step_id, "failed")
                    except BridgeError:
                        # Run reconciliation remains the durable source if the
                        # workflow transition itself cannot be recorded.
                        pass
                self.hub.run_status(run_id, "reconciliation")
                self.hub.acknowledge_command(str(command["id"]))
                raise
            else:
                self.hub.run_status(run_id, "evidenced")
                self.hub.acknowledge_command(str(command["id"]))
                return True
            finally:
                lease_stop.set()
                if lease_thread is not None:
                    lease_thread.join(timeout=2)
        approval_id = command.get("approval_id")
        if not approval_id:
            raise BridgeError(f"unapproved bridge action rejected: {action}")
        runner = ApprovedActionRunner(self.settings)
        try:
            if action == "commit_pr":
                if str(payload.get("diff_hash") or "") != runner.diff_hash():
                    raise BridgeError("current diff differs from the hash that was approved")
                # Consumption is atomic and proves the exact approved payload can execute once.
                self.hub.consume(approval_id, command["payload_hash"])
                result = runner.commit_pr(payload)
                self.hub.evidence("pull_request", approval_id, self.settings.project_id, result)
            elif action == "merge_and_accept":
                self.hub.consume(approval_id, command["payload_hash"])
                result = runner.merge_and_accept(payload, self.state)
                self.hub.evidence("pull_request", approval_id, self.settings.project_id, result)
            else:
                raise BridgeError(f"approval action is not implemented by the v1 bridge: {action}")
        except BridgeError as exc:
            self.hub.evidence("failure", approval_id, self.settings.project_id, {"action": action, "reconciliation_required": True, "error": str(exc)})
            raise
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
    parser.add_argument("--state-claim-command", default="anvil claim {task_id} --actor {actor}")
    parser.add_argument("--state-work-packet-command", default="anvil packet {task_id} --format json")
    parser.add_argument("--state-hook-command", default="anvil hook capture-evidence")
    parser.add_argument("--state-submit-command", default="anvil submit {task_id}")
    parser.add_argument("--state-apply-command", default="")
    parser.add_argument("--codex-binary", default="codex")
    parser.add_argument("--router-base-url", required=True)
    parser.add_argument("--router-token-env", default="ANVIL_ROUTER_TOKEN")
    parser.add_argument("--codex-config", action="append", default=[])
    parser.add_argument("--worktree", action="append", default=[], metavar="ID=PATH", help="allow a named local worktree for concurrent sessions")
    parser.add_argument("--skills-root", action="append", default=[], type=Path, help="allow explicit local SKILL.md roots for this bridge")
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
        worktrees[worktree_id.strip()] = Path(raw_path.strip()).resolve()
    settings = BridgeSettings(
        hub=args.hub, bridge_id=args.bridge_id, token=token, project_root=root, project_id=args.project_id,
        state_events=args.state_events,
        cursor_file=args.cursor_file or root / ".workbench" / "state-events.cursor",
        state_status_command=args.state_status_command,
        state_claim_command=args.state_claim_command,
        state_work_packet_command=args.state_work_packet_command,
        state_hook_command=args.state_hook_command,
        state_submit_command=args.state_submit_command,
        state_apply_command=args.state_apply_command,
        codex_binary=args.codex_binary, router_base_url=args.router_base_url,
        router_token_env=args.router_token_env, codex_config=tuple(args.codex_config),
        worktrees=worktrees, skill_roots=tuple(path.resolve() for path in args.skills_root),
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
