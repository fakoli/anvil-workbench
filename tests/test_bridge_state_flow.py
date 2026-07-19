from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from workbench import bridge as bridge_module
from workbench.bridge import Bridge, BridgeError, BridgeSettings, CodexRunner, StateReader, VerificationRunner


def settings(tmp_path: Path, **overrides: object) -> BridgeSettings:
    values: dict[str, object] = {
        "hub": "https://workbench.tailnet.example",
        "bridge_id": "bridge_1",
        "token": "token",
        "project_root": tmp_path,
        "project_id": "project_1",
        "state_events": tmp_path / ".anvil" / "events.jsonl",
        "cursor_file": tmp_path / ".workbench" / "cursor",
        "state_status_command": "anvil status",
        "state_claim_command": "anvil claim {task_id} --actor {actor}",
        "state_work_packet_command": "anvil packet {task_id} --format json",
        "state_hook_command": "anvil hook capture-evidence",
        "state_submit_command": "anvil submit {task_id}",
        "state_apply_command": "",
        "codex_binary": "codex",
        "router_base_url": "http://100.87.34.66:8000/v1",
        "router_token_env": "ANVIL_ROUTER_TOKEN",
        "codex_config": (),
    }
    values.update(overrides)
    return BridgeSettings(**values)  # type: ignore[arg-type]


def test_state_packet_uses_the_supported_cli_and_parses_its_status_line(monkeypatch, tmp_path: Path):
    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, 'Wrote packet to fixture\\T001.json\n{"task_id":"T001"}\n', "")

    monkeypatch.setattr(bridge_module.subprocess, "run", fake_run)

    packet = StateReader(settings(tmp_path)).work_packet("T001")

    assert packet == {"task_id": "T001"}
    assert calls == [["anvil", "packet", "T001", "--format", "json"]]


def test_state_event_path_resolves_via_cli_without_opening_state_db(monkeypatch, tmp_path: Path):
    state_dir = tmp_path / "state-home" / ".anvil"
    state_dir.mkdir(parents=True)
    (state_dir / "events.jsonl").write_text(json.dumps({"id": "event_1"}) + "\n", encoding="utf-8")

    def fake_run(args, **_kwargs):
        assert args == ["anvil", "status"]
        return subprocess.CompletedProcess(args, 0, f"anvil for fixture\nPath: {state_dir}\n", "")

    monkeypatch.setattr(bridge_module.subprocess, "run", fake_run)
    reader = StateReader(settings(tmp_path, state_events=None))

    assert list(reader.new_events())[0][1]["id"] == "event_1"
    assert not (state_dir / "state.db").exists()


def test_verification_runner_records_the_observed_exit_code_with_state(monkeypatch, tmp_path: Path):
    captured: list[tuple[str, int, str, str, str]] = []
    reader = StateReader(settings(tmp_path))
    monkeypatch.setattr(
        reader,
        "capture_verification",
        lambda command, exit_code, stdout, stderr, actor: captured.append((command, exit_code, stdout, stderr, actor)),
    )
    command = f'"{sys.executable}" -c "print(42)"'
    packet = {"task": {"verification": {"commands": [command]}}}

    results = VerificationRunner(settings(tmp_path), lambda _role, _content: None).run(packet, reader, "bridge-actor")

    assert results[0].exit_code == 0
    assert len(results[0].output_sha256) == 64
    assert captured == [(command, 0, "42\n", "", "bridge-actor")]


def test_codex_runner_passes_the_workbench_selected_route_to_codex(monkeypatch, tmp_path: Path):
    commands: list[list[str]] = []

    class Process:
        def __init__(self, command, **_kwargs):
            commands.append(command)
            self.stdout = iter(())

        def wait(self):
            return 0

    monkeypatch.setenv("ANVIL_ROUTER_TOKEN", "local-only-token")
    monkeypatch.setattr(bridge_module.subprocess, "Popen", Process)

    assert CodexRunner(settings(tmp_path), lambda _role, _content: None).run("run_1", {"task_id": "T001"}, "heavy-local") == 0
    assert 'model="heavy-local"' in commands[0]
    assert 'model_providers.anvil.name="Anvil Serving"' in commands[0]
    assert 'web_search="disabled"' in commands[0]
    assert "--ignore-user-config" in commands[0]
    assert "--ignore-rules" in commands[0]
    assert "features.plugins=false" in commands[0]
    assert "features.apps=false" in commands[0]
    assert "features.multi_agent=false" in commands[0]
    assert commands[0][:4] == ["codex", "--ask-for-approval", "never", "exec"]


def test_codex_runner_rejects_an_unselected_model_route(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ANVIL_ROUTER_TOKEN", "local-only-token")

    with pytest.raises(BridgeError, match="selected Anvil model route"):
        CodexRunner(settings(tmp_path), lambda _role, _content: None).run("run_1", {}, "")


def test_bridge_run_claims_verifies_and_submits_before_needs_review(monkeypatch, tmp_path: Path):
    class Hub:
        def __init__(self) -> None:
            self.evidence_events: list[tuple[str, dict[str, object]]] = []
            self.statuses: list[tuple[str, str]] = []

        def next_command(self):
            return {"action_type": "run_codex", "payload": {"run_id": "run_1", "task_id": "T001"}}

        def evidence(self, source_kind, _source_id, _project_id, payload):
            self.evidence_events.append((source_kind, payload))

        def event(self, *_args):
            return None

        def run_status(self, run_id, status):
            self.statuses.append((run_id, status))

    class Verifier:
        def __init__(self, *_args) -> None:
            pass

        def run(self, *_args):
            return (bridge_module.VerificationResult("pytest tests/test_extract.py -v", 0, "a" * 64),)

        def changed_likely_files(self, _packet):
            return ("src/mdlinks/extract.py",)

    monkeypatch.setattr(bridge_module.CodexRunner, "run", lambda *_args: 0)
    monkeypatch.setattr(bridge_module, "VerificationRunner", Verifier)
    bridge = Bridge(settings(tmp_path))
    hub = Hub()
    bridge.hub = hub  # type: ignore[assignment]
    monkeypatch.setattr(bridge.state, "claim", lambda task_id, actor: {"task_id": task_id, "actor": actor})
    monkeypatch.setattr(
        bridge.state,
        "work_packet",
        lambda task_id: {"task_id": task_id, "task": {"likely_files": ["src/mdlinks/extract.py"]}},
    )
    submitted: list[tuple[str, tuple[str, ...], tuple[str, ...], str]] = []
    monkeypatch.setattr(
        bridge.state,
        "submit_evidence",
        lambda task_id, commands, files_changed, actor: submitted.append((task_id, tuple(commands), tuple(files_changed), actor)) or {"data": {"evidence_id": "EV1"}},
    )

    assert bridge.poll_once() is True
    assert submitted == [("T001", ("pytest tests/test_extract.py -v",), ("src/mdlinks/extract.py",), "workbench-bridge_1")]
    assert [kind for kind, _payload in hub.evidence_events] == ["state_event", "work_packet", "state_event"]
    assert hub.statuses == [("run_1", "running"), ("run_1", "evidenced")]


def test_bridge_marks_a_failed_verification_for_reconciliation(monkeypatch, tmp_path: Path):
    class Hub:
        def __init__(self) -> None:
            self.statuses: list[tuple[str, str]] = []
            self.evidence_events: list[tuple[str, dict[str, object]]] = []

        def next_command(self):
            return {"action_type": "run_codex", "payload": {"run_id": "run_1", "task_id": "T001", "model": "heavy-local"}}

        def evidence(self, source_kind, _source_id, _project_id, payload):
            self.evidence_events.append((source_kind, payload))

        def event(self, *_args):
            return None

        def run_status(self, run_id, status):
            self.statuses.append((run_id, status))

    class Verifier:
        def __init__(self, *_args) -> None:
            pass

        def run(self, *_args):
            return (bridge_module.VerificationResult("pytest", 1, "a" * 64),)

    monkeypatch.setattr(bridge_module.CodexRunner, "run", lambda *_args: 0)
    monkeypatch.setattr(bridge_module, "VerificationRunner", Verifier)
    bridge = Bridge(settings(tmp_path))
    hub = Hub()
    bridge.hub = hub  # type: ignore[assignment]
    monkeypatch.setattr(bridge.state, "claim", lambda *_args: {"task_id": "T001"})
    monkeypatch.setattr(bridge.state, "work_packet", lambda *_args: {"task_id": "T001", "task": {}})

    with pytest.raises(BridgeError, match="independent verification failed"):
        bridge.poll_once()
    assert hub.statuses == [("run_1", "running"), ("run_1", "reconciliation")]
    assert hub.evidence_events[-1][0] == "failure"
    assert hub.evidence_events[-1][1]["reconciliation_required"] is True


def test_bridge_rejects_standalone_state_apply_even_with_an_approval(tmp_path: Path):
    class Hub:
        def next_command(self):
            return {"action_type": "state_apply", "approval_id": "approval_1", "payload_hash": "hash", "payload": {"task_id": "T001"}}

        def consume(self, *_args):
            raise AssertionError("standalone State apply must not consume approval")

        def evidence(self, *_args):
            return None

    bridge = Bridge(settings(tmp_path))
    bridge.hub = Hub()  # type: ignore[assignment]

    with pytest.raises(BridgeError, match="not implemented"):
        bridge.poll_once()
