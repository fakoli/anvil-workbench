from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from workbench import bridge as bridge_module
from workbench.bridge import (
    ApprovedActionRunner, Bridge, BridgeError, BridgeSettings, CodexRunner, StateReader,
    VerificationRunner,
)
from workbench.skills import SkillError, SkillRegistry


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
        "state_claim_command": "anvil claim {task_id} --actor {actor} --json",
        "state_work_packet_command": "anvil packet {task_id} --format json",
        "state_hook_command": "anvil hook capture-evidence",
        "state_submit_command": "anvil submit {task_id}",
        "state_apply_command": "",
        "codex_binary": "codex",
        "router_base_url": "http://100.87.34.66:8000/v1",
        "router_token_env": "ANVIL_ROUTER_TOKEN",
        "codex_config": (),
        "verification_commands": (),
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


def test_state_commands_follow_the_bridge_selected_worktree(monkeypatch, tmp_path: Path):
    checkout = tmp_path / "checkout-a"
    checkout.mkdir()
    observed: list[Path] = []

    def fake_run(args, **kwargs):
        observed.append(kwargs["cwd"])
        return subprocess.CompletedProcess(args, 0, '{"task_id":"T001"}\n', "")

    monkeypatch.setattr(bridge_module.subprocess, "run", fake_run)
    reader = StateReader(settings(tmp_path))
    assert reader.work_packet("T001", checkout) == {"task_id": "T001"}
    assert observed == [checkout]


def test_state_claim_requires_the_returned_branch_to_match_the_leased_worktree(monkeypatch, tmp_path: Path):
    checkout = tmp_path / "checkout-a"
    checkout.mkdir()
    calls: list[tuple[list[str], Path]] = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs["cwd"]))
        if args[0] == "anvil":
            return subprocess.CompletedProcess(
                args, 0,
                '{"ok":true,"command":"claim","data":{"branch":"agent/t001-example"}}\n', "",
            )
        return subprocess.CompletedProcess(args, 0, "agent/t001-example\n", "")

    monkeypatch.setattr(bridge_module.subprocess, "run", fake_run)
    reader = StateReader(settings(tmp_path))
    assert reader.claim("T001", "bridge", checkout)["data"]["branch"] == "agent/t001-example"
    assert calls == [
        (["anvil", "claim", "T001", "--actor", "bridge", "--json"], checkout),
        (["git", "branch", "--show-current"], checkout),
    ]


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
        lambda command, exit_code, stdout, stderr, actor, _worktree: captured.append((command, exit_code, stdout, stderr, actor)),
    )
    command = f'"{sys.executable}" -c "print(42)"'
    packet = {"task": {"verification": {"commands": [command]}}}

    results = VerificationRunner(
        settings(tmp_path, verification_commands=(command,)), lambda _role, _content: None,
    ).run(packet, reader, "bridge-actor")

    assert results[0].exit_code == 0
    assert len(results[0].output_sha256) == 64
    assert captured == [(command, 0, "42\n", "", "bridge-actor")]


def test_verification_rejects_packet_shell_text_not_in_the_local_allowlist(monkeypatch, tmp_path: Path):
    reader = StateReader(settings(tmp_path))
    monkeypatch.setattr(bridge_module.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("must not execute"))
    packet = {"task": {"verification": {"commands": ["python -m pytest -q; git push --force"]}}}
    with pytest.raises(BridgeError, match="not in the bridge allowlist"):
        VerificationRunner(
            settings(tmp_path, verification_commands=("python -m pytest -q",)),
            lambda _role, _content: None,
        ).run(packet, reader, "bridge-actor")


def test_state_reader_rejects_task_id_argument_injection(tmp_path: Path):
    with pytest.raises(BridgeError, match="unsupported characters"):
        StateReader(settings(tmp_path)).work_packet("T001 --force")


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
            return {"id": "command_1", "action_type": "run_codex", "payload": {"run_id": "run_1", "task_id": "T001"}}

        def acknowledge_command(self, _command_id):
            return None

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
    monkeypatch.setattr(bridge.state, "claim", lambda task_id, actor, _worktree: {"task_id": task_id, "actor": actor})
    monkeypatch.setattr(
        bridge.state,
        "work_packet",
        lambda task_id, _worktree: {"task_id": task_id, "task": {"likely_files": ["src/mdlinks/extract.py"]}},
    )
    submitted: list[tuple[str, tuple[str, ...], tuple[str, ...], str]] = []
    monkeypatch.setattr(
        bridge.state,
        "submit_evidence",
        lambda task_id, commands, files_changed, actor, _worktree: submitted.append((task_id, tuple(commands), tuple(files_changed), actor)) or {"data": {"evidence_id": "EV1"}},
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
            return {"id": "command_1", "action_type": "run_codex", "payload": {"run_id": "run_1", "task_id": "T001", "model": "heavy-local"}}

        def acknowledge_command(self, _command_id):
            return None

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
            return {"id": "command_1", "action_type": "state_apply", "approval_id": "approval_1", "payload_hash": "hash", "payload": {"task_id": "T001"}}

        def consume(self, *_args):
            raise AssertionError("standalone State apply must not consume approval")

        def evidence(self, *_args):
            return None

    bridge = Bridge(settings(tmp_path))
    bridge.hub = Hub()  # type: ignore[assignment]

    with pytest.raises(BridgeError, match="not implemented"):
        bridge.poll_once()


def test_bridge_resolves_only_named_operator_configured_worktrees(tmp_path: Path):
    checkout = tmp_path / "checkout-b"
    checkout.mkdir()
    bridge = Bridge(settings(tmp_path, worktrees={"checkout-b": checkout}))

    assert bridge._worktree_root({"worktree_id": "checkout-b"}) == checkout.resolve()
    assert bridge._worktree_root({}) == tmp_path.resolve()
    with pytest.raises(BridgeError, match="not configured"):
        bridge._worktree_root({"worktree_id": "../../untrusted"})


def test_approved_github_action_runner_uses_only_the_bound_worktree(monkeypatch, tmp_path: Path):
    default = tmp_path / "default"
    checkout = tmp_path / "checkout-b"
    default.mkdir()
    checkout.mkdir()
    observed: list[Path] = []

    def fake_run(_args, **kwargs):
        observed.append(kwargs["cwd"])
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(bridge_module.subprocess, "run", fake_run)
    runner = ApprovedActionRunner(settings(default), checkout)
    runner._run("git", "status", "--short")
    assert observed == [checkout]


def test_merge_rejects_a_pull_request_head_that_changed_after_approval(monkeypatch, tmp_path: Path):
    commands: list[tuple[str, ...]] = []

    def fake_run(*args: str) -> str:
        commands.append(args)
        if args[:4] == ("gh", "pr", "view", "1"):
            return "b" * 40
        raise AssertionError(f"unexpected command: {args}")

    runner = ApprovedActionRunner(settings(tmp_path))
    monkeypatch.setattr(runner, "_run", fake_run)
    state = object()
    with pytest.raises(BridgeError, match="head differs"):
        runner.merge_and_accept(
            {"pr": "1", "task_id": "TASK-1", "expected_head_sha": "a" * 40}, state,  # type: ignore[arg-type]
        )
    assert commands == [("gh", "pr", "view", "1", "--json", "headRefOid", "--jq", ".headRefOid")]


def test_approved_action_requires_the_matching_live_session_lease_and_configured_checkout(tmp_path: Path):
    default = tmp_path / "default"
    checkout = tmp_path / "checkout-b"
    default.mkdir()
    checkout.mkdir()

    class Hub:
        def validate_run_lease(self, run_id):
            assert run_id == "run_delivery"
            return {"session_id": "session_delivery", "worktree_id": "checkout-b", "lease_epoch": 7}

    bridge = Bridge(settings(default, worktrees={"checkout-b": checkout}))
    bridge.hub = Hub()  # type: ignore[assignment]
    assert bridge._approved_action_worktree({
        "run_id": "run_delivery", "session_id": "session_delivery", "worktree_id": "checkout-b", "lease_epoch": 7,
    }) == ("run_delivery", checkout.resolve())

    with pytest.raises(BridgeError, match="differs from the active run worktree lease"):
        bridge._approved_action_worktree({
            "run_id": "run_delivery", "session_id": "session_delivery", "worktree_id": "default", "lease_epoch": 7,
        })


def test_failed_approved_action_reconciles_and_acknowledges_without_releasing_a_retry(monkeypatch, tmp_path: Path):
    class Hub:
        def __init__(self) -> None:
            self.statuses: list[tuple[str, str]] = []
            self.acknowledged: list[str] = []
            self.evidence_events: list[tuple[str, dict[str, object]]] = []

        def next_command(self):
            return {
                "id": "command_pr", "approval_id": "approval_pr", "payload_hash": "approved-hash",
                "action_type": "commit_pr",
                "payload": {
                    "run_id": "run_delivery", "session_id": "session_delivery", "worktree_id": "default",
                    "lease_epoch": 2, "diff_hash": "a" * 64, "branch": "codex/demo",
                },
            }

        def validate_run_lease(self, _run_id):
            return {"session_id": "session_delivery", "worktree_id": "default", "lease_epoch": 2}

        def consume_approval_for_run(self, _approval_id, _payload_hash):
            return None

        def renew_run_lease(self, _run_id):
            return None

        def evidence(self, kind, _source_id, _project_id, payload):
            self.evidence_events.append((kind, payload))

        def run_status(self, run_id, status):
            self.statuses.append((run_id, status))

        def acknowledge_command(self, command_id):
            self.acknowledged.append(command_id)

    class FailingRunner:
        def __init__(self, *_args) -> None:
            pass

        def diff_hash(self):
            return "a" * 64

        def commit_pr(self, _payload):
            raise BridgeError("git push failed after approval consumption")

    monkeypatch.setattr(bridge_module, "ApprovedActionRunner", FailingRunner)
    bridge = Bridge(settings(tmp_path))
    hub = Hub()
    bridge.hub = hub  # type: ignore[assignment]

    with pytest.raises(BridgeError, match="git push failed"):
        bridge.poll_once()
    assert hub.statuses == [("run_delivery", "reconciliation")]
    assert hub.acknowledged == ["command_pr"]
    assert hub.evidence_events[-1][0] == "failure"
    assert hub.evidence_events[-1][1]["reconciliation_required"] is True


def test_successful_merge_and_state_acceptance_marks_delivery_completed_and_releases_lease(monkeypatch, tmp_path: Path):
    class Hub:
        def __init__(self) -> None:
            self.statuses: list[tuple[str, str]] = []
            self.acknowledged: list[str] = []
            self.released: list[str] = []
            self.evidence_events: list[tuple[str, dict[str, object]]] = []

        def next_command(self):
            return {
                "id": "command_merge", "approval_id": "approval_merge", "payload_hash": "approved-hash",
                "action_type": "merge_and_accept",
                "payload": {
                    "run_id": "run_delivery", "session_id": "session_delivery", "worktree_id": "default",
                    "lease_epoch": 2, "pr": "1", "task_id": "TASK-1", "expected_head_sha": "a" * 40,
                },
            }

        def validate_run_lease(self, _run_id):
            return {"session_id": "session_delivery", "worktree_id": "default", "lease_epoch": 2}

        def consume_approval_for_run(self, _approval_id, _payload_hash):
            return None

        def renew_run_lease(self, _run_id):
            return None

        def evidence(self, kind, _source_id, _project_id, payload):
            self.evidence_events.append((kind, payload))

        def complete_approved_merge(self, approval_id, payload_hash, command_id):
            self.statuses.append((approval_id, payload_hash))
            self.acknowledged.append(command_id)

        def acknowledge_command(self, command_id):
            self.acknowledged.append(command_id)

    class SuccessfulRunner:
        def __init__(self, *_args) -> None:
            pass

        def merge_and_accept(self, payload, _state):
            assert payload["pr"] == "1"
            return {"pr": "1", "task_id": "TASK-1", "state_acceptance": {"ok": True}}

    monkeypatch.setattr(bridge_module, "ApprovedActionRunner", SuccessfulRunner)
    bridge = Bridge(settings(tmp_path))
    hub = Hub()
    bridge.hub = hub  # type: ignore[assignment]

    assert bridge.poll_once() is True
    assert hub.statuses == [("approval_merge", "approved-hash")]
    assert hub.released == []
    assert hub.acknowledged == ["command_merge"]
    assert hub.evidence_events == [("pull_request", {"pr": "1", "task_id": "TASK-1", "state_acceptance": {"ok": True}})]


def test_bridge_skill_registry_never_reports_local_paths_and_rejects_duplicates(tmp_path: Path):
    root = tmp_path / "skills"
    first = root / "review"
    first.mkdir(parents=True)
    (first / "SKILL.md").write_text(
        "---\nname: anvil:review\ndescription: Review redacted evidence.\n---\nUse cited evidence only.\n",
        encoding="utf-8",
    )
    discovered = SkillRegistry([root]).discover()
    assert discovered["anvil:review"].metadata() == {
        "skill_id": "anvil:review", "description": "Review redacted evidence.",
        "content_sha256": discovered["anvil:review"].content_sha256,
    }
    second = root / "duplicate"
    second.mkdir()
    (second / "SKILL.md").write_text(
        "---\nname: anvil:review\ndescription: Duplicate.\n---\nDo not load me.\n", encoding="utf-8",
    )
    with pytest.raises(SkillError, match="duplicate"):
        SkillRegistry([root]).discover()


def test_bridge_skill_probe_attests_matching_local_digest_without_running_codex(tmp_path: Path):
    root = tmp_path / "skills"
    directory = root / "review"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: anvil:review\ndescription: Review.\n---\nUse the evidence packet.\n", encoding="utf-8",
    )
    skill = SkillRegistry([root]).discover()["anvil:review"]

    class Hub:
        def __init__(self):
            self.evidence_events = []
            self.acknowledged = []
        def publish_skills(self, _metadata): return None
        def next_command(self): return {"id": "command_1", "action_type": "skill_probe", "payload": {"skills": [{"skill_id": skill.skill_id, "content_sha256": skill.content_sha256}]}}
        def evidence(self, kind, _id, _project, payload): self.evidence_events.append((kind, payload))
        def acknowledge_command(self, command_id): self.acknowledged.append(command_id)

    bridge = Bridge(settings(tmp_path, skill_roots=(root,)))
    bridge.hub = Hub()  # type: ignore[assignment]
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(bridge, "project_state_events", lambda: 0)
        assert bridge.poll_once() is True
    finally:
        monkeypatch.undo()
    assert bridge.hub.evidence_events[0][0] == "evaluation"
    assert bridge.hub.acknowledged == ["command_1"]


def test_bridge_skill_probe_records_reconciliation_when_a_local_skill_digest_changes(tmp_path: Path):
    root = tmp_path / "skills"
    directory = root / "review"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: anvil:review\ndescription: Review.\n---\nUse the evidence packet.\n", encoding="utf-8",
    )

    class Hub:
        def __init__(self):
            self.evidence_events = []
            self.acknowledged = []
        def publish_skills(self, _metadata): return None
        def next_command(self): return {"id": "command_1", "action_type": "skill_probe", "payload": {"skills": [{"skill_id": "anvil:review", "content_sha256": "f" * 64}]}}
        def evidence(self, kind, _id, _project, payload): self.evidence_events.append((kind, payload))
        def acknowledge_command(self, command_id): self.acknowledged.append(command_id)

    bridge = Bridge(settings(tmp_path, skill_roots=(root,)))
    bridge.hub = Hub()  # type: ignore[assignment]
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(bridge, "project_state_events", lambda: 0)
        with pytest.raises(BridgeError, match="changed since it was selected"):
            bridge.poll_once()
    finally:
        monkeypatch.undo()
    assert bridge.hub.evidence_events[0][0] == "failure"
    assert bridge.hub.evidence_events[0][1]["reconciliation_required"] is True
    assert bridge.hub.acknowledged == ["command_1"]
