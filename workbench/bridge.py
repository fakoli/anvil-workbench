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
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .redaction import redact_value


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
    state_events: Path
    cursor_file: Path
    state_work_packet_command: str
    state_apply_command: str
    codex_binary: str
    router_base_url: str
    router_token_env: str
    codex_config: tuple[str, ...]


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

    def consume(self, approval_id: str, approved_hash: str) -> None:
        self.request(
            "POST", f"/api/bridge/{self.bridge_id}/approvals/{approval_id}/consume",
            {"payload_hash": approved_hash},
        )

    def event(self, run_id: str, role: str, content: Any) -> None:
        self.request("POST", f"/api/bridge/{self.bridge_id}/events", {"run_id": run_id, "role": role, "content": redact_value(content)})

    def evidence(self, source_kind: str, source_id: str, project_id: str, payload: dict[str, Any]) -> None:
        self.request(
            "POST", f"/api/bridge/{self.bridge_id}/evidence",
            {"source_kind": source_kind, "source_id": source_id, "project_id": project_id, "payload": redact_value(payload)},
        )


class StateReader:
    """CLI/event reader with a deliberately one-way State boundary."""

    def __init__(self, settings: BridgeSettings) -> None:
        self.settings = settings

    def work_packet(self, task_id: str) -> dict[str, Any]:
        if not task_id:
            raise BridgeError("Codex runs require a State task id")
        rendered = self.settings.state_work_packet_command.format(task_id=task_id)
        args = shlex.split(rendered, posix=os.name != "nt")
        completed = subprocess.run(
            args, cwd=self.settings.project_root, capture_output=True, text=True, check=False,
        )
        if completed.returncode != 0:
            raise BridgeError(f"State work-packet command failed: {completed.stderr.strip()[:500]}")
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise BridgeError("State work-packet command must return JSON") from exc

    def apply_acceptance(self, task_id: str) -> dict[str, Any]:
        if not task_id:
            raise BridgeError("State acceptance requires a task id")
        if not self.settings.state_apply_command:
            raise BridgeError("State apply command is not configured for this bridge")
        rendered = self.settings.state_apply_command.format(task_id=task_id)
        completed = subprocess.run(
            shlex.split(rendered, posix=os.name != "nt"), cwd=self.settings.project_root,
            capture_output=True, text=True, check=False,
        )
        if completed.returncode != 0:
            raise BridgeError(f"State acceptance failed after merge: {completed.stderr.strip()[:500]}")
        return {"stdout": completed.stdout[-4000:]}

    def new_events(self) -> Iterable[tuple[int, dict[str, Any]]]:
        """Tail the canonical State event log only; no database file is opened."""
        offset = 0
        if self.settings.cursor_file.exists():
            try:
                offset = int(self.settings.cursor_file.read_text(encoding="utf-8").strip() or "0")
            except ValueError:
                offset = 0
        if not self.settings.state_events.exists():
            return []
        events: list[tuple[int, dict[str, Any]]] = []
        with self.settings.state_events.open("r", encoding="utf-8") as stream:
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

    def __init__(self, settings: BridgeSettings, emit: Callable[[str, Any], None]) -> None:
        self.settings = settings
        self.emit = emit

    def run(self, run_id: str, work_packet: dict[str, Any]) -> int:
        token = os.environ.get(self.settings.router_token_env, "")
        if not self.settings.router_base_url or not token:
            raise BridgeError("Anvil router base URL and local router token environment variable are required")
        prompt = (
            "You are executing an Anvil State work packet. Work only in the current project. "
            "Run the relevant tests, collect evidence, and do not create a GitHub PR or merge.\n\n"
            + json.dumps(work_packet, indent=2, ensure_ascii=True)
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
            'model_provider="anvil"',
            f'model_providers.anvil.base_url="{self.settings.router_base_url.rstrip("/")}"',
            'model_providers.anvil.env_key="ANVIL_ROUTER_TOKEN"',
            'model_providers.anvil.wire_api="responses"',
            f"model_providers.anvil.http_headers={{ {header_toml} }}",
        )
        command = [self.settings.codex_binary, "exec", "--json", "-C", str(self.settings.project_root), "--sandbox", "workspace-write", "--ask-for-approval", "never"]
        for entry in (*default_config, *self.settings.codex_config):
            command.extend(["-c", entry])
        command.append(prompt)
        environment = dict(os.environ)
        environment["ANVIL_ROUTER_TOKEN"] = token
        self.emit("bridge.codex.started", {"command": [self.settings.codex_binary, "exec", "--json"], "router": self.settings.router_base_url})
        process = subprocess.Popen(
            command, cwd=self.settings.project_root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
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

    def _emit(self, run_id: str, role: str, content: Any) -> None:
        self.hub.event(run_id, role, content)

    def project_state_events(self) -> int:
        count = 0
        for offset, event in self.state.new_events():
            event_id = str(event.get("id") or event.get("event_id") or f"offset-{offset}")
            self.hub.evidence("state_event", event_id, self.settings.project_id, {"event": event, "task_id": event.get("task_id")})
            self.state.commit_cursor(offset)
            count += 1
        return count

    def poll_once(self) -> bool:
        self.project_state_events()
        command = self.hub.next_command()
        if command is None:
            return False
        action = command["action_type"]
        payload = command["payload"]
        if action == "run_codex":
            run_id = payload["run_id"]
            packet = self.state.work_packet(str(payload.get("task_id") or ""))
            packet.setdefault("task_id", payload.get("task_id"))
            self.hub.evidence("work_packet", run_id, self.settings.project_id, {"task_id": payload.get("task_id"), "packet": packet})
            exit_code = CodexRunner(self.settings, lambda role, content: self._emit(run_id, role, content)).run(run_id, packet)
            if exit_code:
                self.hub.evidence("failure", run_id, self.settings.project_id, {"task_id": payload.get("task_id"), "fingerprint": "codex-exit", "exit_code": exit_code})
                raise BridgeError(f"Codex exited with {exit_code}")
            return True
        approval_id = command.get("approval_id")
        if not approval_id:
            raise BridgeError(f"unapproved bridge action rejected: {action}")
        # Consumption is atomic and proves the exact approved payload can execute once.
        self.hub.consume(approval_id, command["payload_hash"])
        runner = ApprovedActionRunner(self.settings)
        try:
            if action == "commit_pr":
                result = runner.commit_pr(payload)
                self.hub.evidence("pull_request", approval_id, self.settings.project_id, result)
            elif action == "merge_and_accept":
                result = runner.merge_and_accept(payload, self.state)
                self.hub.evidence("pull_request", approval_id, self.settings.project_id, result)
            elif action == "state_apply":
                result = self.state.apply_acceptance(str(payload.get("task_id") or ""))
                self.hub.evidence("state_event", approval_id, self.settings.project_id, {"task_id": payload.get("task_id"), "acceptance": result})
            else:
                raise BridgeError(f"approval action is not implemented by the v1 bridge: {action}")
        except BridgeError as exc:
            self.hub.evidence("failure", approval_id, self.settings.project_id, {"action": action, "reconciliation_required": True, "error": str(exc)})
            raise
        return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a project-local Anvil Workbench bridge")
    parser.add_argument("--hub", required=True, help="private tailnet Workbench hub URL")
    parser.add_argument("--bridge-id", required=True)
    parser.add_argument("--token-env", default="WORKBENCH_BRIDGE_TOKEN")
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--state-events", type=Path)
    parser.add_argument("--cursor-file", type=Path)
    parser.add_argument("--state-work-packet-command", default="anvil task show {task_id} --json")
    parser.add_argument("--state-apply-command", default="")
    parser.add_argument("--codex-binary", default="codex")
    parser.add_argument("--router-base-url", required=True)
    parser.add_argument("--router-token-env", default="ANVIL_ROUTER_TOKEN")
    parser.add_argument("--codex-config", action="append", default=[])
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--once", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    token = os.environ.get(args.token_env, "")
    if not token:
        raise SystemExit(f"{args.token_env} is required in the local bridge environment")
    root = args.project_root.resolve()
    settings = BridgeSettings(
        hub=args.hub, bridge_id=args.bridge_id, token=token, project_root=root, project_id=args.project_id,
        state_events=args.state_events or root / ".anvil" / "events.jsonl",
        cursor_file=args.cursor_file or root / ".workbench" / "state-events.cursor",
        state_work_packet_command=args.state_work_packet_command,
        state_apply_command=args.state_apply_command,
        codex_binary=args.codex_binary, router_base_url=args.router_base_url,
        router_token_env=args.router_token_env, codex_config=tuple(args.codex_config),
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
