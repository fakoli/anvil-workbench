from __future__ import annotations

import asyncio
import sys
import types

import pytest

from workbench.store import MemoryStore, StoreError
from workbench.voice import VoiceRelayError, relay_realtime, sanitize_client_event, summarize_server_event
from workbench.workflows import WorkflowError, validate_definition


def delivery_workflow() -> dict[str, object]:
    return {
        "entry": "implement",
        "steps": [
            {"id": "implement", "kind": "agent", "next": ["review"]},
            {"id": "review", "kind": "approval_wait", "next": []},
        ],
    }


def test_concurrent_sessions_have_isolated_runs_and_worktree_leases():
    store = MemoryStore()
    project = store.create_project("demo", ".anvil")
    first, first_workflow = store.create_session(project.id, "first", "one", delivery_workflow())
    second, second_workflow = store.create_session(project.id, "second", "two", delivery_workflow())

    first_run = store.create_run(project.id, "TASK-1", "planning", session_id=first.id, workflow_id=first_workflow.id, workflow_step_id="implement")
    second_run = store.create_run(project.id, "TASK-2", "planning", session_id=second.id, workflow_id=second_workflow.id, workflow_step_id="implement")

    assert first_run.lease_epoch == 1
    assert second_run.lease_epoch == 1
    with pytest.raises(StoreError, match="only one active run"):
        store.create_run(project.id, "TASK-3", "planning", session_id=first.id)

    third, third_workflow = store.create_session(project.id, "third", "one", delivery_workflow())
    with pytest.raises(StoreError, match="leased"):
        store.create_run(project.id, "TASK-4", "planning", session_id=third.id, workflow_id=third_workflow.id)


def test_fenced_run_cannot_start_after_its_worktree_lease_changes():
    store = MemoryStore()
    project = store.create_project("demo", ".anvil")
    bridge, _ = store.register_bridge(project.id, "bridge")
    first, workflow = store.create_session(project.id, "first", "one", delivery_workflow())
    run = store.create_run(project.id, "TASK-1", "planning", session_id=first.id, workflow_id=workflow.id)
    assert store.validate_run_lease(run.id, bridge.id).id == run.id

    replacement, _ = store.create_session(project.id, "replacement", "one", delivery_workflow())
    # This simulates lease expiry followed by a new session acquiring the worktree.
    current = store.leases["worktree:one"]
    store.leases["worktree:one"] = type(current)(current.resource_key, current.session_id, current.epoch, current.created_at)
    store.acquire_lease("worktree:one", replacement.id, 300)
    with pytest.raises(StoreError, match="stale"):
        store.validate_run_lease(run.id, bridge.id)


def test_run_command_is_leased_until_atomic_terminal_finalization():
    store = MemoryStore()
    project = store.create_project("demo", ".anvil")
    bridge, _ = store.register_bridge(project.id, "bridge")
    run = store.create_run(project.id, "TASK-1", "planning")
    store.enqueue_run(bridge.id, run)

    command = store.next_command(bridge.id)
    assert command is not None
    assert command["delivery_attempts"] == 1
    assert store.next_command(bridge.id) is None
    assert len(store.commands[bridge.id]) == 1

    with pytest.raises(StoreError, match="terminal finalization"):
        store.acknowledge_command(bridge.id, command["id"])
    store.finalize_run_command(run.id, "reconciliation", bridge.id, command["id"])
    assert store.next_command(bridge.id) is None


def test_workflows_are_version_pinned_and_wait_at_approval_boundary():
    store = MemoryStore()
    project = store.create_project("demo", ".anvil")
    session, workflow = store.create_session(project.id, "review", "default", delivery_workflow())

    started = store.start_workflow(workflow.id, "operator")
    waiting = store.complete_workflow_step(workflow.id, "implement", "succeeded", "bridge")

    assert started.cursor == ("implement",)
    assert waiting.status == "waiting_approval"
    assert waiting.cursor == ("review",)
    with pytest.raises(StoreError, match="version-pinned"):
        store.revise_workflow(workflow.id, 1, delivery_workflow(), "operator")
    events = store.list_workflow_events(session.id)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert events[-1].kind == "workflow.step.finished"


def test_workflow_start_is_one_retry_safe_run_lease_and_command_operation():
    store = MemoryStore()
    project = store.create_project("demo", ".anvil")
    bridge, _ = store.register_bridge(project.id, "bridge")
    session, workflow = store.create_session(project.id, "atomic", "checkout-a", delivery_workflow())

    started, run = store.start_workflow_run(workflow.id, "TASK-1", "planning", "operator")
    command = store.next_command(bridge.id)
    assert started.status == "running"
    assert run.lease_epoch == 1
    assert command is not None and command["payload"]["run_id"] == run.id

    with pytest.raises(StoreError, match="not a draft"):
        store.start_workflow_run(workflow.id, "TASK-1", "planning", "operator")
    assert len(store.list_runs(project.id)) == 1
    assert store.leases["worktree:" + session.worktree_id].epoch == 1
    assert len(store.commands[bridge.id]) == 1


def test_fan_out_completion_preserves_siblings_and_waits_for_join_barrier():
    store = MemoryStore()
    project = store.create_project("demo", ".anvil")
    definition = {
        "entry": "fork",
        "steps": [
            {"id": "fork", "kind": "fan_out", "next": ["left", "right"]},
            {"id": "left", "kind": "agent", "next": ["join"]},
            {"id": "right", "kind": "agent", "next": ["join"]},
            {"id": "join", "kind": "join", "next": []},
        ],
    }
    _session, workflow = store.create_session(project.id, "fan out", "default", definition)
    store.start_workflow(workflow.id, "operator")
    forked = store.complete_workflow_step(workflow.id, "fork", "succeeded", "bridge")
    after_left = store.complete_workflow_step(workflow.id, "left", "succeeded", "bridge")
    after_right = store.complete_workflow_step(workflow.id, "right", "succeeded", "bridge")

    assert forked.cursor == ("left", "right")
    assert after_left.cursor == ("right",)
    assert after_right.cursor == ("join",)


def test_run_finalizer_advances_workflow_and_acknowledges_exact_command_atomically():
    store = MemoryStore()
    project = store.create_project("demo", ".anvil")
    bridge, _ = store.register_bridge(project.id, "bridge")
    _session, workflow = store.create_session(project.id, "finalize", "checkout-a", delivery_workflow())
    _started, run = store.start_workflow_run(workflow.id, "TASK-1", "planning", "operator")
    command = store.next_command(bridge.id)
    assert command is not None
    store.update_run_status(run.id, "running", bridge.id)

    with pytest.raises(StoreError, match="does not match"):
        store.finalize_run_command(run.id, "evidenced", bridge.id, "command_wrong")
    assert store.runs[run.id].status == "running"
    assert len(store.commands[bridge.id]) == 1

    evidenced = store.finalize_run_command(run.id, "evidenced", bridge.id, command["id"])
    assert evidenced.status == "evidenced"
    assert store.get_workflow(workflow.id).status == "waiting_approval"
    assert store.next_command(bridge.id) is None


def test_delivery_actions_hold_the_leased_worktree_until_merge_and_reconcile_failures():
    store = MemoryStore()
    project = store.create_project("demo", ".anvil")
    bridge, _ = store.register_bridge(project.id, "bridge")
    session, workflow = store.create_session(project.id, "delivery", "checkout-a", delivery_workflow())
    run = store.create_run(
        project.id, "TASK-1", "heavy-local", session_id=session.id,
        workflow_id=workflow.id, workflow_step_id="implement",
    )
    store.start_workflow(workflow.id, "operator")
    store.update_run_status(run.id, "running", bridge.id)
    evidenced = store.update_run_status(run.id, "evidenced", bridge.id)
    binding = {
        "run_id": run.id, "session_id": session.id, "worktree_id": "checkout-a",
        "lease_epoch": evidenced.lease_epoch,
    }
    commit = store.create_approval(
        project.id, "commit_pr", {"diff_hash": "a" * 64, "branch": "codex/demo", **binding},
        "operator", 60, bridge.id,
    )
    approved_commit = store.approve(commit.id, "operator", frozenset({"operator"}))
    store.consume_approval_for_run(approved_commit.id, approved_commit.payload_hash, bridge.id)
    # PR creation does not release the checkout: a later merge approval must
    # bind the same evidenced State task and fenced worktree.
    with pytest.raises(StoreError, match="task id"):
        store.create_approval(
            project.id, "merge_and_accept", {"pr": "1", "task_id": "TASK-OTHER", "expected_head_sha": "a" * 40, **binding},
            "operator", 60, bridge.id,
        )
    merge = store.create_approval(
        project.id, "merge_and_accept", {"pr": "1", "task_id": "TASK-1", "expected_head_sha": "a" * 40, **binding},
        "operator", 60, bridge.id,
    )
    assert merge.status == "pending"

    reconciled = store.update_run_status(run.id, "reconciliation", bridge.id)
    assert reconciled.status == "reconciliation"
    assert store.get_workflow(workflow.id).status == "reconciliation"
    with pytest.raises(StoreError, match="stale"):
        store.create_approval(
            project.id, "merge_and_accept", {"pr": "1", "task_id": "TASK-1", "expected_head_sha": "a" * 40, **binding},
            "operator", 60, bridge.id,
        )


def test_delivery_completes_only_after_the_bridge_reports_merge_and_state_success():
    store = MemoryStore()
    project = store.create_project("demo", ".anvil")
    bridge, _ = store.register_bridge(project.id, "bridge")
    session, workflow = store.create_session(project.id, "delivery", "checkout-a", delivery_workflow())
    run = store.create_run(
        project.id, "TASK-1", "heavy-local", session_id=session.id,
        workflow_id=workflow.id, workflow_step_id="implement",
    )
    store.start_workflow(workflow.id, "operator")
    store.update_run_status(run.id, "running", bridge.id)
    evidenced = store.update_run_status(run.id, "evidenced", bridge.id)
    approval = store.create_approval(
        project.id, "merge_and_accept", {
            "pr": "1", "task_id": "TASK-1", "expected_head_sha": "a" * 40, "run_id": run.id,
            "session_id": session.id, "worktree_id": "checkout-a", "lease_epoch": evidenced.lease_epoch,
        }, "operator", 60, bridge.id,
    )
    approved = store.approve(approval.id, "operator", frozenset({"operator"}))
    store.enqueue_command(bridge.id, approved)
    command = store.next_command(bridge.id)
    assert command is not None
    consumed = store.consume_approval_for_run(approved.id, approved.payload_hash, bridge.id)

    completed = store.complete_approved_merge(consumed.id, consumed.payload_hash, bridge.id, command["id"])
    assert completed.status == "completed"
    assert store.get_workflow(workflow.id).status == "completed"
    assert store.next_command(bridge.id) is None


def test_workflow_definition_rejects_unbounded_or_unallowlisted_control_flow():
    with pytest.raises(WorkflowError, match="allowlisted"):
        validate_definition({"steps": [{"id": "deploy", "kind": "shell", "next": []}]})
    with pytest.raises(WorkflowError, match="cycles"):
        validate_definition({
            "entry": "a",
            "steps": [
                {"id": "a", "kind": "agent", "next": ["b"]},
                {"id": "b", "kind": "condition", "next": ["a"]},
            ],
        })


def test_voice_relay_filters_model_and_tool_controls_and_never_summarizes_audio():
    cleaned = sanitize_client_event('{"type":"session.update","session":{"model":"other","tools":[{}],"voice":"alloy"}}')
    assert cleaned == {"type": "session.update", "session": {"voice": "alloy"}}
    assert sanitize_client_event('{"type":"response.create","response":{"tools":[{"type":"function"}],"instructions":"bypass"}}') == {
        "type": "response.create", "response": {"modalities": ["audio", "text"]},
    }
    with pytest.raises(VoiceRelayError):
        sanitize_client_event('{"type":"conversation.item.create"}')
    assert summarize_server_event('{"type":"response.output_audio.delta","delta":"base64-audio"}') == ("voice.tts.chunk", {"bytes": 12})
    assert summarize_server_event('{"type":"conversation.item.input_audio_transcription.completed","transcript":"private note"}') == ("voice.utterance.final", {"characters": 12})


def test_voice_relay_returns_an_explicit_error_for_a_rejected_browser_event(monkeypatch):
    class Browser:
        sent: list[dict[str, object]] = []
        closed: list[int] = []

        async def accept(self):
            return None

        async def receive_text(self):
            return '{"type":"conversation.item.create"}'

        async def send_json(self, value):
            self.sent.append(value)

        async def close(self, code):
            self.closed.append(code)

    class Upstream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def send(self, _value):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(60)
            raise StopAsyncIteration

    browser = Browser()
    events: list[str] = []
    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=lambda *_args, **_kwargs: Upstream()))

    async def record(kind, _data):
        events.append(kind)

    asyncio.run(relay_realtime(browser, "ws://voice.test/v1/realtime", "", record))
    assert browser.sent[0]["error"]["code"] == "invalid_voice_event"
    assert browser.closed == [1008]
    assert events == ["voice.connected", "voice.disconnected"]
