from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import types

import pytest

from workbench.models import (
    RunContext,
    RunContextError,
    RunIdentity,
    RunWorkflowPin,
    UntrustedTask,
    UntrustedTaskRef,
    run_capabilities_from_snapshot,
    run_skills_from_snapshot,
)
from workbench.store import MemoryStore, StoreError
from workbench.voice import VoiceRelayError, relay_realtime, sanitize_client_event, summarize_server_event
from workbench.workflows import WorkflowError, validate_definition

from _support import build_run_context, compile_delivery_snapshot

# The hermetic discovery -> profile -> snapshot -> capture pipeline is defined
# once in ``tests/_support`` and shared with conftest and the run-context tests
# (no per-module re-implementation).  ``compiled_delivery_snapshot`` is a thin
# alias; ``valid_run_context`` delegates to the shared factory, supplying only
# the harness-kernel-specific identity, context id, and skill purpose.
compiled_delivery_snapshot = compile_delivery_snapshot


def valid_run_context(**overrides) -> RunContext:
    """Build a complete, valid run context; overrides replace capture kwargs."""
    snapshot = overrides.pop("snapshot", None) or compile_delivery_snapshot()
    defaults: dict[str, object] = dict(
        context_id="ctx_run_example_0001",
        identity=RunIdentity(
            run_id="run_1", session_id="sess_1", bridge_id="bridge_1",
            worktree_name="checkout-a", task_id="release-beta:T001", request_id="req_1",
        ),
        skills=run_skills_from_snapshot(snapshot, {"anvil:execute": "State-backed implementation guidance."}),
    )
    defaults.update(overrides)
    return build_run_context(snapshot=snapshot, **defaults)


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


# --- Immutable run-context model (state-context-operations:T005.1) ----------


def test_run_context_captures_every_required_authority_and_readable_field():
    """Criterion 1: a captured run context carries the exact authority pins and
    the human-readable fields, sourced coherently from the compiled snapshot."""
    snapshot = compiled_delivery_snapshot()
    run_context = valid_run_context(snapshot=snapshot)
    data = run_context.as_dict()

    # Exact authority fields: the workflow/catalog/profile digests are the
    # snapshot's own immutable pins, byte-for-byte.
    workflow = data["trusted"]["workflow"]
    assert workflow["digest"] == snapshot.workflow_digest
    assert workflow["capability_profile_digest"] == snapshot.capability_profile_digest
    assert {c["provider"]: c["digest"] for c in workflow["catalogs"]} == {
        c.provider: c.catalog_digest for c in snapshot.catalogs
    }
    # Every pinned capability carries the snapshot operation's exact digest and
    # effect, plus a resolved gate.
    captured = {
        (c["provider"], c["operation_id"]): (c["operation_digest"], c["effect"], c["gate"])
        for c in data["trusted"]["capabilities"]
    }
    for operation in snapshot.operations:
        digest, effect, gate = captured[(operation.provider, operation.id)]
        assert digest == operation.operation_digest
        assert effect == operation.effect
        assert gate in {"none", "preview", "approval"}
    # The external GitHub effects are approval-gated by the conservative default.
    commit = next(
        c for c in data["trusted"]["capabilities"] if c["operation_id"] == "bridge.github.commit_pr"
    )
    assert commit["effect"] == "external_effect" and commit["gate"] == "approval"

    # Human-readable fields are present in the untrusted structure.
    task = data["untrusted"]["task"]
    assert task["title"] == "Add a documented operation contract"
    assert task["acceptance_criteria"] == ["Add a versioned resource", "Validate its JSON shape"]
    assert data["trusted"]["skills"][0]["purpose"] == "State-backed implementation guidance."


def test_gate_override_may_strengthen_but_never_weaken_the_default():
    """Security floor: a per-operation gate override may only raise the
    conservative default up the ``none < preview < approval`` order.  A downgrade
    of an ``external_effect`` from ``approval`` to a laxer gate fails closed, so a
    caller can never quietly widen authority; a strengthening (or equal) override
    is accepted."""
    snapshot = compiled_delivery_snapshot()
    external = next(o for o in snapshot.operations if o.effect == "external_effect")
    ungated = next(
        o for o in snapshot.operations
        if o.effect in {"read", "bounded_execution", "state_mutation"}
    )

    # Weakening an approval-defaulted external effect is refused for every laxer gate.
    for weaker in ("none", "preview"):
        with pytest.raises(RunContextError):
            run_capabilities_from_snapshot(snapshot, gates={(external.provider, external.id): weaker})

    # Strengthening an ungated (default none) operation up to approval is accepted.
    strengthened = {
        (c.provider, c.operation_id): c
        for c in run_capabilities_from_snapshot(
            snapshot, gates={(ungated.provider, ungated.id): "approval"}
        )
    }
    assert strengthened[(ungated.provider, ungated.id)].gate == "approval"
    # The external effect keeps its conservative approval default (unweakened).
    assert strengthened[(external.provider, external.id)].gate == "approval"

    # An equal-strictness override (approval on an already-approval effect) is fine.
    same = {
        (c.provider, c.operation_id): c
        for c in run_capabilities_from_snapshot(
            snapshot, gates={(external.provider, external.id): "approval"}
        )
    }
    assert same[(external.provider, external.id)].gate == "approval"


def test_run_context_fails_closed_when_a_required_field_is_unresolved():
    """Criterion 1: a missing/malformed required field prevents construction, so
    a dispatch that depends on it can never proceed (T005.2 fail-closed root)."""
    snapshot = compiled_delivery_snapshot()

    # A missing skill purpose (required human-readable field) fails closed.
    with pytest.raises(RunContextError, match="missing a required human-readable purpose"):
        run_skills_from_snapshot(snapshot, {})

    # A malformed exact-authority field (a non-sha256 workflow digest) fails.
    with pytest.raises(RunContextError, match="workflow digest must be a sha256"):
        RunWorkflowPin(
            workflow_id="delivery", workflow_revision="1.0.0", workflow_digest="not-a-digest",
            catalogs=RunWorkflowPin.from_snapshot(snapshot).catalogs,
            capability_profile_digest=snapshot.capability_profile_digest,
        )

    # An empty acceptance-criteria list (required readable field) fails closed.
    with pytest.raises(RunContextError, match="at least one acceptance criterion"):
        UntrustedTask(
            ref=UntrustedTaskRef(prd_id="release-beta", task_id="T001", prd_revision=5),
            title="x", acceptance_criteria=(), work_packet_digest="sha256:" + "8" * 64,
        )

    # A context id that is not the bounded ``ctx_`` grammar fails closed.
    with pytest.raises(RunContextError, match="context_id is invalid"):
        valid_run_context(context_id="run-not-ctx")


def test_run_context_fields_cannot_be_mutated_at_any_level():
    """Criterion 2: the run context is frozen at every level and its collections
    are tuples, so no in-place mutation of authority or prose is possible."""
    run_context = valid_run_context()

    with pytest.raises(dataclasses.FrozenInstanceError):
        run_context.context_id = "ctx_other_00000000"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        run_context.trusted.workflow.workflow_digest = "sha256:" + "0" * 64  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        run_context.trusted.capabilities[0].gate = "none"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        run_context.untrusted.task.title = "rewritten"  # type: ignore[misc]
    # Collections are tuples: no append/assignment reaches the authority set.
    assert isinstance(run_context.trusted.capabilities, tuple)
    assert isinstance(run_context.trusted.workflow.catalogs, tuple)
    assert isinstance(run_context.untrusted.task.acceptance_criteria, tuple)

    # Mutating the dict projection cannot reach back into the frozen record.
    expected = run_context.as_dict()
    view = run_context.as_dict()
    view["trusted"]["capabilities"].append({"operation_id": "smuggled"})
    view["untrusted"]["task"]["title"] = "tampered"
    view["trusted"]["workflow"]["digest"] = "sha256:" + "1" * 64
    assert run_context.as_dict() == expected


def test_run_context_separates_and_labels_trusted_policy_from_untrusted_data():
    """Criterion 3: trusted policy and untrusted PRD/task data serialize into two
    separately labeled top-level structures, and closed field sets reject
    leak-by-addition on either side."""
    run_context = valid_run_context()
    data = run_context.as_dict()

    # Two separate, explicitly labeled structures.
    assert set(data) == {"schema_version", "context_id", "trusted", "untrusted"}
    assert data["trusted"]["trust"] == "trusted_execution_policy"
    assert data["untrusted"]["content_trust"] == "untrusted_task_data"
    # The exact authority pins live ONLY under trusted; the PRD/task prose lives
    # ONLY under untrusted. Neither the workflow digest nor the task title
    # crosses the boundary.
    trusted_blob = json.dumps(data["trusted"])
    untrusted_blob = json.dumps(data["untrusted"])
    assert run_context.trusted.workflow.workflow_digest in trusted_blob
    assert run_context.trusted.workflow.workflow_digest not in untrusted_blob
    assert "Add a documented operation contract" in untrusted_blob
    assert "Add a documented operation contract" not in trusted_blob
    # Every untrusted prose item is labeled untrusted_task_data.
    assert data["untrusted"]["task"]["content_trust"] == "untrusted_task_data"
    for evidence in data["untrusted"]["evidence"]:
        assert evidence["content_trust"] == "untrusted_task_data"

    # Round-trips exactly.
    assert RunContext.from_dict(data).as_dict() == data

    # Leak-by-addition fails on BOTH structures: an undeclared field anywhere is
    # rejected rather than silently carried.
    poisoned_trusted = run_context.as_dict()
    poisoned_trusted["trusted"]["capabilities"][0]["command"] = "rm -rf /"
    with pytest.raises(RunContextError, match="capability carries undeclared fields"):
        RunContext.from_dict(poisoned_trusted)

    poisoned_untrusted = run_context.as_dict()
    poisoned_untrusted["untrusted"]["task"]["state_db_path"] = "/var/anvil/state.db"
    with pytest.raises(RunContextError, match="task carries undeclared fields"):
        RunContext.from_dict(poisoned_untrusted)

    mislabeled = run_context.as_dict()
    mislabeled["trusted"]["trust"] = "untrusted_task_data"
    with pytest.raises(RunContextError, match="trusted policy label is wrong"):
        RunContext.from_dict(mislabeled)


def test_run_context_prose_is_credential_scrubbed_on_capture():
    """Criterion 3 (safety): untrusted prose is credential-scrubbed before it is
    ever persisted or rendered, even though it is otherwise served as-is."""
    run_context = valid_run_context(
        task=UntrustedTask(
            ref=UntrustedTaskRef(prd_id="release-beta", task_id="T001", prd_revision=5),
            title="Fix token=supersecretvalue leak",
            acceptance_criteria=("Rotate Bearer sk-live-abc123DEADBEEF now",),
            work_packet_digest="sha256:" + "8" * 64,
        ),
    )
    blob = json.dumps(run_context.as_dict())
    for leaked in ("supersecretvalue", "sk-live-abc123DEADBEEF"):
        assert leaked not in blob
    assert "[REDACTED]" in blob


def test_run_context_workflow_pin_is_exactly_the_snapshot_pins():
    """Criterion 1: the trusted workflow pin is derived only from the immutable
    snapshot pins — no execution block, command, or path can arrive through."""
    snapshot = compiled_delivery_snapshot()
    pin = RunWorkflowPin.from_snapshot(snapshot)
    assert pin.as_dict() == {
        "id": snapshot.workflow_id,
        "revision": snapshot.workflow_revision,
        "digest": snapshot.workflow_digest,
        "catalogs": [
            {"provider": c.provider, "digest": c.catalog_digest} for c in snapshot.catalogs
        ],
        "capability_profile_digest": snapshot.capability_profile_digest,
    }
    # A non-snapshot input is refused by type.
    with pytest.raises(RunContextError, match="requires a WorkflowSnapshot"):
        RunWorkflowPin.from_snapshot({"digest": "sha256:" + "0" * 64})  # type: ignore[arg-type]


# --- Capture and persist before bridge dispatch (T005.2) --------------------


def test_run_context_is_persisted_before_the_bridge_dispatch_begins():
    """Criterion 1: the run context is durably stored BEFORE dispatch runs."""
    from workbench.run_context_store import MemoryRunContextStore, dispatch_with_run_context

    store = MemoryRunContextStore()
    dispatched: list[str] = []

    def dispatch(run_context: RunContext) -> None:
        # By the time the bridge dispatch is invoked, the context is already
        # readable from the store — persistence strictly precedes dispatch.
        assert store.get("project_a", run_context.run_id).as_dict() == run_context.as_dict()
        dispatched.append(run_context.run_id)

    persisted = dispatch_with_run_context(
        store, "project_a", valid_run_context, dispatch,
    )
    assert dispatched == [persisted.run_id]
    assert store.get("project_a", persisted.run_id) is persisted


def test_unresolved_or_unpersisted_context_prevents_dispatch():
    """Criterion 2: a resolve failure OR a persist failure blocks dispatch."""
    from workbench.run_context_store import (
        MemoryRunContextStore,
        RunContextStoreError,
        dispatch_with_run_context,
    )

    dispatched: list[str] = []

    def dispatch(run_context: RunContext) -> None:
        dispatched.append(run_context.run_id)

    # Resolve failure: a build that raises RunContextError never reaches dispatch.
    store = MemoryRunContextStore()

    def build_incomplete() -> RunContext:
        return valid_run_context(context_id="not-a-ctx-id")

    with pytest.raises(RunContextError):
        dispatch_with_run_context(store, "project_a", build_incomplete, dispatch)
    assert dispatched == []
    assert store.rows.contexts == {}

    # Persist failure: a store that fails to persist blocks dispatch too.
    class FailingStore(MemoryRunContextStore):
        def capture(self, acting_project_id, run_context):  # type: ignore[override]
            raise RunContextStoreError("persist failed")

    with pytest.raises(RunContextStoreError, match="persist failed"):
        dispatch_with_run_context(FailingStore(), "project_a", valid_run_context, dispatch)
    assert dispatched == []


def test_later_source_changes_do_not_rewrite_the_stored_snapshot():
    """Criterion 3: a re-capture with different content fails closed; the stored
    queue-time snapshot is never rewritten by a later change."""
    from workbench.run_context_store import (
        MemoryRunContextStore,
        RunContextImmutableError,
    )

    store = MemoryRunContextStore()
    original = valid_run_context()
    store.capture("project_a", original)
    baseline = original.as_dict()

    # An identical re-capture is an idempotent retry: same stored record.
    assert store.capture("project_a", original) is store.get("project_a", original.run_id)

    # A later task/PRD change yields a DIFFERENT context for the same run; the
    # store refuses to rewrite the immutable queue-time snapshot.
    renamed = valid_run_context(
        task=UntrustedTask(
            ref=UntrustedTaskRef(prd_id="release-beta", task_id="T001", prd_revision=6),
            title="Renamed after queue time",
            acceptance_criteria=("A different criterion",),
            work_packet_digest="sha256:" + "9" * 64,
        ),
    )
    assert renamed.run_id == original.run_id
    with pytest.raises(RunContextImmutableError, match="cannot be rewritten"):
        store.capture("project_a", renamed)
    # The stored snapshot is byte-identical to the queue-time capture.
    assert store.get("project_a", original.run_id).as_dict() == baseline


def test_run_context_store_scopes_reads_and_captures_to_the_owning_project():
    """Cross-project capture and read fail closed with the indistinct not-found."""
    from workbench.run_context_store import MemoryRunContextStore, UnknownRunContextError

    store = MemoryRunContextStore()
    context = valid_run_context()
    store.capture("project_b", context)

    # A read under another project scope is indistinguishable from missing.
    with pytest.raises(UnknownRunContextError):
        store.get("project_a", context.run_id)
    # A genuinely missing run raises the identical error.
    with pytest.raises(UnknownRunContextError):
        store.get("project_b", "run_absent")
    # project_a's namespace is never created by a foreign read.
    assert "project_a" not in store.rows.contexts
    # An invalid scope is rejected at the edge.
    with pytest.raises(StoreError, match="valid acting project scope"):
        store.get("", context.run_id)


def test_concurrent_captures_for_one_run_admit_exactly_one_snapshot():
    """A contended check-then-act on one (project, run) key must let exactly one
    DIFFERENT-content capture win; the loser fails closed, never last-wins.

    The check->act gap is made real and contestable by a namespace dict whose
    ``get`` yields between the existence check and the write, so two writers can
    only both-succeed (last-wins) if the store lock is absent. Detection power
    was confirmed locally by unwrapping ``MemoryRunContextStore.capture`` (the
    lock): the unsynchronized store lets both writers pass the None check and
    stores no immutable refusal, failing the ``len(errors) == 1`` assertion.
    """
    import sys
    import threading
    import time

    from workbench.run_context_store import (
        MemoryRunContextStore,
        RunContextImmutableError,
        RunContextRows,
    )

    class YieldingDict(dict):
        """A namespace whose ``get`` widens the capture check->act window."""

        def get(self, key, default=None):  # noqa: D401
            value = super().get(key, default)
            time.sleep(0.002)  # force a preemption point inside the critical section
            return value

    original = valid_run_context()
    variant = valid_run_context(
        task=UntrustedTask(
            ref=UntrustedTaskRef(prd_id="release-beta", task_id="T001", prd_revision=7),
            title="Concurrent variant",
            acceptance_criteria=("Variant criterion",),
            work_packet_digest="sha256:" + "a" * 64,
        ),
    )
    assert original.run_id == variant.run_id
    assert original.as_dict() != variant.as_dict()

    old_interval = sys.getswitchinterval()
    try:
        sys.setswitchinterval(1e-6)
        for _ in range(15):
            # Pre-seed the acting project's namespace with the yielding dict so
            # the store's setdefault returns it and the get->set gap is real.
            store = MemoryRunContextStore(RunContextRows(contexts={"project_a": YieldingDict()}))
            start = threading.Barrier(2)
            errors: list[Exception] = []
            errors_lock = threading.Lock()

            def worker(ctx: RunContext) -> None:
                start.wait()
                try:
                    store.capture("project_a", ctx)
                except RunContextImmutableError as exc:
                    with errors_lock:
                        errors.append(exc)

            threads = [
                threading.Thread(target=worker, args=(original,)),
                threading.Thread(target=worker, args=(variant,)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            # Exactly one distinct-content capture wins; the other is refused as
            # a rewrite. Never both silently stored (last-wins).
            stored = store.get("project_a", original.run_id).as_dict()
            assert stored in (original.as_dict(), variant.as_dict())
            assert len(errors) == 1, "exactly one contender must be refused as immutable"
    finally:
        sys.setswitchinterval(old_interval)


# ---------------------------------------------------------------------------
# Immediate bridge authority preflight (state-context-operations:T006.2)
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone

from workbench.bridge import (
    OperationLeaseState,
    PreflightedOperation,
    preflight_operation_command,
)
from workbench.contracts import approval_payload_digest
from workbench.models import OperationRef, OperationRefusal, TypedOperationError
from workbench.store import (
    MemoryOperationApprovalStore,
    MemoryOperationReceiptStore,
    OperationApprovalGrant,
    OperationOutcome,
    OperationReceiptRows,
    OperationReceiptStoreError,
    UnknownOutcomeError,
)

from _support import (
    capability_profile_document,
    invoke_operation_command,
    local_catalogs,
    operation_ref_for,
    published_catalog_set,
)

_PREFLIGHT_NOW = datetime(2026, 7, 19, 0, 0, 0, tzinfo=timezone.utc)
_SUBMIT_INPUTS = {"task_ref": "release-beta:T001", "verification_receipt_ids": ["rcpt_v"]}
_COMMIT_INPUTS = {"diff_hash": "a" * 64, "branch": "codex/x", "title": "Anvil Workbench delivery", "base": "main"}


def _lease_authority(epoch: int = 3, minutes: int = 5):
    def authority(worktree_name: str) -> OperationLeaseState:
        return OperationLeaseState(worktree_name, epoch, _PREFLIGHT_NOW + timedelta(minutes=minutes))

    return authority


def _non_gated_command(snapshot):
    return invoke_operation_command(
        snapshot, operation_id="state.evidence.submit", inputs=_SUBMIT_INPUTS,
    )


def _gated_command(snapshot, *, grant_id: str = "approval_typedop_00000001", inputs=None):
    inputs = inputs if inputs is not None else _COMMIT_INPUTS
    return invoke_operation_command(
        snapshot, operation_id="bridge.github.commit_pr", inputs=inputs,
        grant_id=grant_id, action="commit_pr", payload_hash=approval_payload_digest(inputs),
    )


def test_preflight_passes_for_a_valid_non_gated_operation():
    snapshot = compile_delivery_snapshot()
    result = preflight_operation_command(
        _non_gated_command(snapshot), catalogs=local_catalogs(), profile=capability_profile_document(),
        lease_authority=_lease_authority(), now=_PREFLIGHT_NOW,
    )
    assert isinstance(result, PreflightedOperation)
    assert result.bridge_adapter == "state.cli.submit_evidence"
    assert result.effect == "state_mutation"
    assert result.lease_epoch == 3
    assert result.approval_grant_id is None


def test_preflight_refuses_an_expired_command():
    snapshot = compile_delivery_snapshot()
    with pytest.raises(TypedOperationError) as excinfo:
        preflight_operation_command(
            _non_gated_command(snapshot), catalogs=local_catalogs(), profile=capability_profile_document(),
            lease_authority=_lease_authority(), now=_PREFLIGHT_NOW + timedelta(hours=1),
        )
    assert excinfo.value.code == "command.expired"


def test_preflight_refuses_a_missing_lease():
    snapshot = compile_delivery_snapshot()
    with pytest.raises(TypedOperationError) as excinfo:
        preflight_operation_command(
            _non_gated_command(snapshot), catalogs=local_catalogs(), profile=capability_profile_document(),
            lease_authority=lambda name: None, now=_PREFLIGHT_NOW,
        )
    assert excinfo.value.code == "lease.missing"


def test_preflight_rechecks_the_lease_expiry_immediately_before_the_effect():
    # The lease was valid when the command issued but the live authority now
    # reports it expired -- the immediate recheck stops the effect.
    snapshot = compile_delivery_snapshot()
    expired = lambda name: OperationLeaseState(name, 3, _PREFLIGHT_NOW - timedelta(seconds=1))
    with pytest.raises(TypedOperationError) as excinfo:
        preflight_operation_command(
            _non_gated_command(snapshot), catalogs=local_catalogs(), profile=capability_profile_document(),
            lease_authority=expired, now=_PREFLIGHT_NOW,
        )
    assert excinfo.value.code == "lease.expired"


def test_preflight_refuses_a_fenced_out_lease_epoch():
    snapshot = compile_delivery_snapshot()
    with pytest.raises(TypedOperationError) as excinfo:
        preflight_operation_command(
            _non_gated_command(snapshot), catalogs=local_catalogs(), profile=capability_profile_document(),
            lease_authority=_lease_authority(epoch=9), now=_PREFLIGHT_NOW,
        )
    assert excinfo.value.code == "lease.epoch_mismatch"


def test_preflight_refuses_a_drifted_local_catalog_digest():
    snapshot = compile_delivery_snapshot()
    command = _non_gated_command(snapshot)
    for entry in command["workflow_snapshot"]["catalogs"]:
        if entry["provider"] == "anvil-state":
            entry["digest"] = "sha256:" + "0" * 64
    with pytest.raises(TypedOperationError) as excinfo:
        preflight_operation_command(
            command, catalogs=local_catalogs(), profile=capability_profile_document(),
            lease_authority=_lease_authority(), now=_PREFLIGHT_NOW,
        )
    assert excinfo.value.code == "operation.digest_drift"


def test_preflight_refuses_a_changed_work_packet():
    snapshot = compile_delivery_snapshot()
    with pytest.raises(TypedOperationError) as excinfo:
        preflight_operation_command(
            _non_gated_command(snapshot), catalogs=local_catalogs(), profile=capability_profile_document(),
            lease_authority=_lease_authority(), now=_PREFLIGHT_NOW,
            pinned_work_packet_digest="sha256:" + "8" * 64,
            current_work_packet_digest="sha256:" + "9" * 64,
        )
    assert excinfo.value.code == "work_packet.digest_changed"


def test_preflight_refuses_an_undeclared_input_field():
    snapshot = compile_delivery_snapshot()
    command = _non_gated_command(snapshot)
    command["payload"]["inputs"]["command"] = "rm -rf /"
    with pytest.raises(TypedOperationError) as excinfo:
        preflight_operation_command(
            command, catalogs=local_catalogs(), profile=capability_profile_document(),
            lease_authority=_lease_authority(), now=_PREFLIGHT_NOW,
        )
    assert excinfo.value.code == "operation.input_invalid"


def test_preflight_gated_operation_consumes_a_one_time_hash_bound_approval():
    snapshot = compile_delivery_snapshot()
    command = _gated_command(snapshot)
    approvals = MemoryOperationApprovalStore()
    approvals.grant(
        "approval_typedop_00000001", "commit_pr", approval_payload_digest(_COMMIT_INPUTS),
        "bridge_example", "project_example",
    )
    result = preflight_operation_command(
        command, catalogs=local_catalogs(), profile=capability_profile_document(),
        lease_authority=_lease_authority(), approval_consumer=approvals, now=_PREFLIGHT_NOW,
    )
    assert result.bridge_adapter == "bridge.github.commit_pr"
    assert result.approval_grant_id == "approval_typedop_00000001"
    # The grant is one-time: a replay of the identical command fails closed.
    with pytest.raises(TypedOperationError) as excinfo:
        preflight_operation_command(
            command, catalogs=local_catalogs(), profile=capability_profile_document(),
            lease_authority=_lease_authority(), approval_consumer=approvals, now=_PREFLIGHT_NOW,
        )
    assert excinfo.value.code == "approval.invalid"


def test_preflight_gated_operation_refuses_a_missing_grant():
    snapshot = compile_delivery_snapshot()
    command = _gated_command(snapshot)
    del command["payload"]["approval"]
    del command["approval_grant_id"]
    with pytest.raises(TypedOperationError) as excinfo:
        preflight_operation_command(
            command, catalogs=local_catalogs(), profile=capability_profile_document(),
            lease_authority=_lease_authority(), approval_consumer=MemoryOperationApprovalStore(),
            now=_PREFLIGHT_NOW,
        )
    assert excinfo.value.code == "approval.missing"


def test_preflight_gated_operation_refuses_a_hash_that_does_not_bind_the_inputs():
    snapshot = compile_delivery_snapshot()
    command = _gated_command(snapshot)
    # Tamper the inputs after the approval hash was computed over the originals.
    command["payload"]["inputs"]["branch"] = "codex/evil"
    approvals = MemoryOperationApprovalStore()
    approvals.grant(
        "approval_typedop_00000001", "commit_pr", approval_payload_digest(_COMMIT_INPUTS),
        "bridge_example", "project_example",
    )
    with pytest.raises(TypedOperationError) as excinfo:
        preflight_operation_command(
            command, catalogs=local_catalogs(), profile=capability_profile_document(),
            lease_authority=_lease_authority(), approval_consumer=approvals, now=_PREFLIGHT_NOW,
        )
    assert excinfo.value.code == "approval.hash_mismatch"


def test_preflight_gated_operation_refuses_an_approval_action_that_differs_from_the_gate():
    # bridge.py preflight step 6 binds the descriptor's gates.approval_action: an
    # approval carrying a DIFFERENT action (e.g. a merge_and_accept grant replayed
    # onto a commit_pr operation) is refused with the stable
    # approval.action_mismatch code, BEFORE the hash bind or the one-time consume.
    snapshot = compile_delivery_snapshot()
    command = invoke_operation_command(
        snapshot, operation_id="bridge.github.commit_pr", inputs=_COMMIT_INPUTS,
        grant_id="approval_typedop_00000001", action="merge_and_accept",
        payload_hash=approval_payload_digest(_COMMIT_INPUTS),
    )
    with pytest.raises(TypedOperationError) as excinfo:
        preflight_operation_command(
            command, catalogs=local_catalogs(), profile=capability_profile_document(),
            lease_authority=_lease_authority(), approval_consumer=MemoryOperationApprovalStore(),
            now=_PREFLIGHT_NOW,
        )
    assert excinfo.value.code == "approval.action_mismatch"


def test_preflight_gated_operation_refuses_a_cross_bridge_grant():
    snapshot = compile_delivery_snapshot()
    command = _gated_command(snapshot)
    approvals = MemoryOperationApprovalStore()
    # Grant bound to a DIFFERENT bridge/project than the command carries.
    approvals.grant(
        "approval_typedop_00000001", "commit_pr", approval_payload_digest(_COMMIT_INPUTS),
        "other_bridge", "other_project",
    )
    with pytest.raises(TypedOperationError) as excinfo:
        preflight_operation_command(
            command, catalogs=local_catalogs(), profile=capability_profile_document(),
            lease_authority=_lease_authority(), approval_consumer=approvals, now=_PREFLIGHT_NOW,
        )
    assert excinfo.value.code == "approval.invalid"


# ---------------------------------------------------------------------------
# Idempotent typed receipts + reconciliation records (state-context-operations:T006.3)
# ---------------------------------------------------------------------------


def _submit_operation() -> OperationRef:
    return OperationRef(**operation_ref_for("state.evidence.submit"))


def test_unknown_outcome_files_exactly_one_reconciliation_and_is_never_retried():
    store = MemoryOperationReceiptStore()
    operation = _submit_operation()
    executions = {"n": 0}

    def interrupted() -> OperationOutcome:
        executions["n"] += 1
        raise UnknownOutcomeError(
            "the external merge outcome is unknown", external_ref={"pr": "gh:1"}, reason="interrupted",
        )

    receipt, replayed = store.record_attempt(
        run_id="run_1", command_id="cmd_1", operation=operation,
        idempotency_key="run:run_1:merge:1", executor=interrupted,
    )
    assert receipt["status"] == "reconciliation_required"
    assert replayed is False
    items = store.list_reconciliations()
    assert len(items) == 1
    assert items[0]["reason"] == "interrupted"

    # A replay must NOT re-run the unknown external effect; it returns the stored
    # reconciliation receipt and files no second reconciliation item.
    receipt2, replayed2 = store.record_attempt(
        run_id="run_1", command_id="cmd_1", operation=operation,
        idempotency_key="run:run_1:merge:1", executor=interrupted,
    )
    assert executions["n"] == 1
    assert replayed2 is True
    assert receipt2["receipt_id"] == receipt["receipt_id"]
    assert len(store.list_reconciliations()) == 1


def test_every_attempt_reaches_a_typed_terminal_receipt_or_reconciliation():
    store = MemoryOperationReceiptStore()
    operation = _submit_operation()

    ok, _ = store.record_attempt(
        run_id="r", command_id="c", operation=operation, idempotency_key="k-ok",
        executor=lambda: OperationOutcome("succeeded", evidence_refs=("state_event_x",)),
    )
    denied, _ = store.record_attempt(
        run_id="r", command_id="c", operation=operation, idempotency_key="k-denied",
        executor=lambda: OperationOutcome("denied", error=OperationRefusal("operation.digest_drift", "stale")),
    )
    unknown, _ = store.record_attempt(
        run_id="r", command_id="c", operation=operation, idempotency_key="k-unknown",
        executor=lambda: OperationOutcome("unknown", external_ref={"pr": "gh:2"}),
    )
    assert ok["status"] == "succeeded"
    assert denied["status"] == "denied"
    assert denied["redaction"]["status"] == "metadata_only"
    assert unknown["status"] == "reconciliation_required"


def test_concurrent_same_key_attempts_execute_the_effect_exactly_once():
    """A contended check-then-act on one idempotency key must execute the effect
    exactly ONCE; the loser replays the committed receipt, never re-runs it.

    The check->act gap is made real (not just asserted) by a ``receipts`` dict
    whose ``get`` sleeps between the existence check and the write, and by driving
    two barrier-synchronized workers at a 1e-6 thread switch interval across 15
    rounds.  Without the store's synchronization both workers would pass the
    ``None`` existence check and run the executor, so the committed
    ``executions == 1`` assertion is what proves the effect is serialized; the
    paired assertions prove both attempts resolve to the one receipt with exactly
    one first-runner and one replayer.
    """
    import sys
    import threading
    import time

    class YieldingDict(dict):
        def get(self, key, default=None):  # noqa: D401
            value = super().get(key, default)
            time.sleep(0.002)  # force a preemption point inside the critical section
            return value

    operation = _submit_operation()
    old_interval = sys.getswitchinterval()
    try:
        sys.setswitchinterval(1e-6)
        for _ in range(15):
            store = MemoryOperationReceiptStore(OperationReceiptRows(receipts=YieldingDict()))
            executions = {"n": 0}
            exec_lock = threading.Lock()
            start = threading.Barrier(2)
            outcomes: list[tuple[str, bool]] = []
            outcomes_lock = threading.Lock()

            def executor() -> OperationOutcome:
                with exec_lock:
                    executions["n"] += 1
                time.sleep(0.001)
                return OperationOutcome("succeeded", external_ref={"state_event_id": "evt_1"})

            def worker() -> None:
                start.wait()
                receipt, replayed = store.record_attempt(
                    run_id="run_1", command_id="cmd_1", operation=operation,
                    idempotency_key="k-race", executor=executor,
                )
                with outcomes_lock:
                    outcomes.append((receipt["receipt_id"], replayed))

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            assert executions["n"] == 1, "the effect must execute exactly once under contention"
            assert len({rid for rid, _ in outcomes}) == 1, "both attempts resolve to one receipt"
            assert sorted(replayed for _, replayed in outcomes) == [False, True]
    finally:
        sys.setswitchinterval(old_interval)


def test_operation_approval_grant_is_one_time_and_hash_bound():
    approvals = MemoryOperationApprovalStore()
    payload_hash = approval_payload_digest(_COMMIT_INPUTS)
    approvals.grant("approval_g1", "commit_pr", payload_hash, "bridge_1", "project_1")

    approvals.consume("approval_g1", "commit_pr", payload_hash, "bridge_1", "project_1")
    # Replay of the consumed grant fails closed.
    with pytest.raises(OperationReceiptStoreError, match="already consumed"):
        approvals.consume("approval_g1", "commit_pr", payload_hash, "bridge_1", "project_1")

    # A different-payload consume on a fresh grant fails closed (hash binding).
    approvals.grant("approval_g2", "commit_pr", payload_hash, "bridge_1", "project_1")
    with pytest.raises(OperationReceiptStoreError, match="payload hash"):
        approvals.consume("approval_g2", "commit_pr", "sha256:" + "b" * 64, "bridge_1", "project_1")


# ---------------------------------------------------------------------------
# Preference configuration slice (preferences-configuration:
# T004.1 / T002.1 / T002.2 / T002.3 / T002)
# ---------------------------------------------------------------------------

from workbench.contracts import preference_operation_digest
from workbench.models import (
    PREFERENCE_OPERATION_KINDS,
    PREFERENCE_RECORD_SCHEMA_VERSION,
    EffectiveValue,
    PolicyOperation,
    PolicyOperationError,
    PolicyOperationPreview,
    PreferenceMigrationError,
    PreferenceRecord,
    PreferenceValidationError,
    build_policy_operation,
    migrate_preference_record,
    resolve_effective_settings,
    validate_setting_value,
)
from workbench.store import (
    MemoryPreferenceStore,
    PreferenceRows,
    PreferenceStoreError,
    StalePreferenceWriteError,
    UnknownPreferenceError,
)

from _support import load_example


def _settings_catalog() -> dict:
    return load_example("settings-descriptor.v1.json")


def _descriptor(catalog: dict, setting_id: str) -> dict:
    return next(s for s in catalog["settings"] if s["id"] == setting_id)


#: A server-held key standing in for the hub's audit-fingerprint key (>= 16 octets).
_PREF_AUDIT_KEY = b"pref-audit-key-0123456789"


def _pref_api_client(store, live_valid_refs_provider=None):
    """A real create_app TestClient over the injected preference store.

    Used so the effective/repair/reset assertions run through the ACTUAL wired
    GET/POST /api/preferences path (not a hand-passed-refs vacuum), and thus fail
    if the endpoint stops threading the ceiling / ref-validity into the shared
    resolver.
    """
    from fastapi.testclient import TestClient

    from workbench.api import create_app
    from workbench.config import Settings
    from workbench.graph import NullGraph
    from workbench.store import MemoryStore

    settings = Settings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="alice", approvers=frozenset({"alice"}), bridge_bootstrap_token="",
        anvil_router_base_url="http://serving", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=True,
    )
    return TestClient(create_app(
        settings=settings, store=MemoryStore(), graph=NullGraph(),
        preference_store=store, live_valid_refs_provider=live_valid_refs_provider,
    ))


_PREF_API_ACTOR = {"X-Workbench-Actor": "alice"}


# --- T004.1: typed policy operations + canonical payload hashing -------------


def test_every_mutable_policy_maps_to_one_typed_versioned_operation():
    # Criterion 1: each mutable, non-secret, non-deployment-only setting maps to
    # one typed, versioned operation from the CLOSED kind set -- never a generic
    # command name.
    catalog = _settings_catalog()
    built = 0
    for descriptor in catalog["settings"]:
        if descriptor.get("sensitivity") == "secret" or descriptor.get("path_like"):
            continue
        if descriptor.get("scope") == "deployment" or descriptor.get("mutability") == "env_only":
            continue
        op = build_policy_operation(descriptor, operation="preference.reset", op_version=1)
        assert isinstance(op, PolicyOperation)
        assert op.operation in PREFERENCE_OPERATION_KINDS
        assert op.setting_id == descriptor["id"]
        assert op.scope == descriptor["scope"]
        assert op.op_version == 1
        built += 1
    assert built >= 1


def test_policy_operation_hashes_equivalent_payloads_identically_and_detects_changes():
    # Criterion 2: equivalent payloads hash identically; any material scope,
    # value, version, or expiry change produces a different digest.
    catalog = _settings_catalog()
    descriptor = _descriptor(catalog, "personal.chat_transcript_retention_days")
    a = build_policy_operation(descriptor, operation="preference.set", op_version=1, value=30)
    b = build_policy_operation(descriptor, operation="preference.set", op_version=1, value=30)
    assert a.digest == b.digest == preference_operation_digest(a.payload())

    # A changed value, version, or scope each changes the digest (no field escapes).
    assert a.digest != build_policy_operation(descriptor, operation="preference.set", op_version=1, value=31).digest
    assert a.digest != build_policy_operation(descriptor, operation="preference.set", op_version=2, value=30).digest
    when = datetime(2026, 7, 21, tzinfo=timezone.utc)
    assert a.digest != PolicyOperation("preference.set", descriptor["id"], "personal", 1, 30, when).digest
    assert a.digest != PolicyOperation("preference.set", descriptor["id"], "project", 1, 30).digest


def test_policy_operation_refuses_secret_and_deployment_only_values():
    # Criterion 4: a secret/path-like or deployment-only value can never enter an
    # operation payload -- refused before it is hashed or applied.
    catalog = _settings_catalog()
    for setting_id in ("deployment.identity_header_name", "deployment.state_read_location"):
        with pytest.raises(PolicyOperationError):
            build_policy_operation(
                _descriptor(catalog, setting_id), operation="preference.set", op_version=1, value="x",
            )
    # A public, approval-gated policy setting is NOT deployment-only, so it builds.
    policy_op = build_policy_operation(
        _descriptor(catalog, "policy.transcript_retention_max_days"),
        operation="preference.set", op_version=1, value=120,
    )
    assert policy_op.scope == "policy"


def test_policy_operation_dataclass_refuses_a_deployment_scope_on_construction():
    # Finding 9: the frozen dataclass itself fails closed on a deployment scope,
    # not only the builder -- so no direct construction/deserialization can mint a
    # deployment-owned operation that bypasses build_policy_operation.
    with pytest.raises(PolicyOperationError):
        PolicyOperation("preference.set", "deployment.state_read_location", "deployment", 1, "x")
    # Actor/policy scopes still construct (regression guard for the digest tests).
    assert PolicyOperation("preference.reset", "personal.time_format", "personal", 1).scope == "personal"
    assert PolicyOperation("preference.set", "policy.route_allowlist_profile", "policy", 1,
                           "sha256:" + "b" * 64).scope == "policy"


def test_policy_operation_preview_shares_digest_and_cannot_mutate_a_store():
    # Criterion 3: creating/storing a preview is pure -- it shares the applied
    # operation's digest (so an approval binds the exact effect) and exposes no
    # write path, so a store's committed value is untouched by building a preview.
    catalog = _settings_catalog()
    store = MemoryPreferenceStore(catalog)
    store.set_preference("personal", "alice", "personal.chat_transcript_retention_days", 30, 0, "alice")
    op = build_policy_operation(
        _descriptor(catalog, "personal.chat_transcript_retention_days"),
        operation="preference.set", op_version=2, value=45,
    )
    preview = PolicyOperationPreview(op, "raise retention to 45 days")
    assert preview.digest == op.digest
    assert not hasattr(preview, "commit") and not hasattr(preview, "apply")
    # The store's committed value is unchanged by constructing/serializing a preview.
    preview.as_dict()
    assert store.get("personal", "alice", "personal.chat_transcript_retention_days").value == 30


# --- T002.1: preference record, versioning, migration, typed validation ------


def test_preference_record_carries_monotonic_write_and_schema_versions():
    record = PreferenceRecord(
        setting_id="personal.time_format", scope="personal", scope_key="alice",
        value="format_12h", write_version=1, updated_by="alice",
    )
    assert record.write_version == 1
    assert record.schema_version == PREFERENCE_RECORD_SCHEMA_VERSION
    # Frozen: a stored value is replaced, never mutated in place.
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.value = "format_24h"  # type: ignore[misc]


def test_preference_migration_upgrades_every_supported_prior_version():
    # v1 shape (actor/version) upgrades to the current (scope_key/write_version).
    v1 = {
        "setting_id": "personal.time_format", "scope": "personal", "actor": "alice",
        "value": "format_12h", "version": 3, "updated_at": "2026-07-20T00:00:00Z",
    }
    upgraded = migrate_preference_record(v1)
    assert upgraded.schema_version == PREFERENCE_RECORD_SCHEMA_VERSION
    assert upgraded.write_version == 3
    assert upgraded.scope_key == "alice" and upgraded.updated_by == "alice"

    # v2 is already current and round-trips unchanged.
    v2 = {
        "setting_id": "personal.time_format", "scope": "personal", "scope_key": "bob",
        "value": "format_24h", "write_version": 5, "updated_by": "bob",
        "schema_version": 2, "updated_at": "2026-07-20T00:00:00Z",
    }
    assert migrate_preference_record(v2).write_version == 5

    # An unknown/malformed version fails closed rather than loading as current.
    with pytest.raises(PreferenceMigrationError):
        migrate_preference_record({"schema_version": 99, "setting_id": "x.y"})


def test_preference_audit_metadata_excludes_secret_and_pii():
    record = PreferenceRecord(
        setting_id="personal.default_chat_route", scope="personal",
        scope_key="alice@example.com", value="route.private-abc",
        write_version=2, updated_by="alice@example.com",
    )
    meta = record.audit_metadata(key=_PREF_AUDIT_KEY)
    assert meta["setting_id"] == "personal.default_chat_route"
    assert meta["write_version"] == 2 and meta["schema_version"] == PREFERENCE_RECORD_SCHEMA_VERSION
    # No value and no identifying fields (raw scope key / updater) leak.
    for forbidden in ("value", "scope_key", "updated_by"):
        assert forbidden not in meta
    assert "alice@example.com" not in json.dumps(meta)
    assert "route.private-abc" not in json.dumps(meta)
    assert meta["scope_key_fingerprint"] != "alice@example.com"
    # The fingerprint is KEYED: the same scope key under a different server key
    # yields a different tag, so a holder of the tag without the key cannot run a
    # dictionary of candidate actors against it. An unsalted sha256 could not do
    # this. A too-short key fails closed.
    other = record.audit_metadata(key=b"a-different-key-0123456789")
    assert other["scope_key_fingerprint"] != meta["scope_key_fingerprint"]
    with pytest.raises(PreferenceValidationError):
        record.audit_metadata(key=b"too-short")


def test_malformed_or_out_of_range_value_raises_typed_error_before_persistence():
    catalog = _settings_catalog()
    store = MemoryPreferenceStore(catalog)
    # Out of bounds (max is 90), wrong type, and non-member enum each raise the
    # typed validation error and leave nothing persisted.
    with pytest.raises(PreferenceValidationError):
        store.set_preference("personal", "alice", "personal.chat_transcript_retention_days", 500, 0, "alice")
    with pytest.raises(PreferenceValidationError):
        store.set_preference("personal", "alice", "personal.chat_transcript_retention_days", "lots", 0, "alice")
    with pytest.raises(PreferenceValidationError):
        store.set_preference("personal", "alice", "personal.landing_surface", "spaceship", 0, "alice")
    with pytest.raises(UnknownPreferenceError):
        store.get("personal", "alice", "personal.chat_transcript_retention_days")


# --- T002.2: scoped durable storage + stale-write rejection ------------------


def test_valid_preference_write_commits_atomically_and_increments_version():
    catalog = _settings_catalog()
    store = MemoryPreferenceStore(catalog)
    first = store.set_preference("personal", "alice", "personal.time_format", "format_12h", 0, "alice")
    assert first.write_version == 1
    second = store.set_preference("personal", "alice", "personal.time_format", "format_24h", 1, "alice")
    assert second.write_version == 2
    assert store.get("personal", "alice", "personal.time_format").value == "format_24h"


def test_stale_preference_write_is_reload_required_and_leaves_value_unchanged():
    catalog = _settings_catalog()
    store = MemoryPreferenceStore(catalog)
    store.set_preference("personal", "alice", "personal.time_format", "format_12h", 0, "alice")
    with pytest.raises(StalePreferenceWriteError) as excinfo:
        # Expected version 0 is stale: the stored version is already 1.
        store.set_preference("personal", "alice", "personal.time_format", "format_24h", 0, "alice")
    assert excinfo.value.reload_required is True
    assert excinfo.value.current_version == 1
    # The stored value is exactly as it was; the stale write did not overwrite it.
    assert store.get("personal", "alice", "personal.time_format").value == "format_12h"
    # A stale write is NOT a validation failure -- distinct typed exceptions.
    assert not isinstance(excinfo.value, PreferenceValidationError)


def test_preference_store_isolates_cross_actor_and_cross_project_scopes():
    catalog = _settings_catalog()
    store = MemoryPreferenceStore(catalog)
    store.set_preference("personal", "alice", "personal.time_format", "format_12h", 0, "alice")
    store.set_preference("project", "proj_1", "project.delivery_route", "route.delivery-heavy", 0, "alice")

    # A cross-actor and a cross-project read raise the SAME indistinct not-found a
    # genuinely missing record raises -- byte-identical, so neither is an oracle.
    def _err_bytes(fn) -> bytes:
        try:
            fn()
        except UnknownPreferenceError as exc:
            return str(exc).encode("utf-8")
        raise AssertionError("expected UnknownPreferenceError")

    foreign_actor = _err_bytes(lambda: store.get("personal", "bob", "personal.time_format"))
    foreign_project = _err_bytes(lambda: store.get("project", "proj_2", "project.delivery_route"))
    genuinely_missing = _err_bytes(lambda: store.get("personal", "carol", "personal.landing_surface"))
    assert foreign_actor == foreign_project == genuinely_missing

    # A personal actor cannot write a project/policy-owned setting from personal scope.
    with pytest.raises(PreferenceStoreError):
        store.set_preference("personal", "alice", "project.delivery_route", "route.delivery-heavy", 0, "alice")
    # No cross-scope value crossed over: bob has no personal.time_format.
    with pytest.raises(UnknownPreferenceError):
        store.get("personal", "bob", "personal.time_format")


def test_preference_reset_returns_the_inherited_default_state():
    catalog = _settings_catalog()
    store = MemoryPreferenceStore(catalog)
    store.set_preference("personal", "alice", "personal.landing_surface", "delivery", 0, "alice")
    effective = store.reset_preference("personal", "alice", "personal.landing_surface", 1, "alice")
    # Reset falls back to the reviewed descriptor default ("chat").
    assert isinstance(effective, EffectiveValue)
    assert effective.value == "chat" and effective.source == "default"
    with pytest.raises(UnknownPreferenceError):
        store.get("personal", "alice", "personal.landing_surface")


def test_concurrent_same_version_preference_writes_commit_exactly_one():
    """A contended optimistic write on one setting must commit EXACTLY once; the
    loser is rejected as a stale, reload-required write, never a second commit.

    The check->act gap (read current version, then write) is made real by an
    inner namespace whose ``get`` sleeps, and by two barrier-synchronized workers
    at a 1e-6 switch interval.  Disabling the store lock locally is what proves
    the assertion is not a tautology: without serialization both workers pass the
    version-0 check and double-commit; the store's lock is what forces the loser
    to observe the winner's version and fail stale.  The lock is restored in a
    ``finally`` and never committed disabled.
    """
    import sys
    import threading
    import time

    class YieldingDict(dict):
        def get(self, key, default=None):
            value = super().get(key, default)
            time.sleep(0.002)  # force a preemption point inside the critical section
            return value

    class _NullLock:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    catalog = _settings_catalog()

    def run_round(disable_lock: bool) -> tuple[int, int]:
        rows = PreferenceRows(records={("personal", "alice"): YieldingDict()})
        store = MemoryPreferenceStore(catalog, rows)
        if disable_lock:
            store._lock = _NullLock()
        start = threading.Barrier(2)
        results: list[str] = []
        results_lock = threading.Lock()

        def worker(value: str) -> None:
            start.wait()
            try:
                store.set_preference("personal", "alice", "personal.time_format", value, 0, "alice")
                outcome = "ok"
            except StalePreferenceWriteError:
                outcome = "stale"
            with results_lock:
                results.append(outcome)

        threads = [
            threading.Thread(target=worker, args=("format_12h",)),
            threading.Thread(target=worker, args=("format_24h",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return results.count("ok"), results.count("stale")

    old_interval = sys.getswitchinterval()
    try:
        sys.setswitchinterval(1e-6)
        # With the store lock, every contended round commits exactly one write and
        # rejects the other as stale.
        for _ in range(15):
            oks, stales = run_round(disable_lock=False)
            assert (oks, stales) == (1, 1), "the write must be serialized: one commit, one stale"
        # Detection check: with the lock disabled the invariant BREAKS (both pass
        # the version-0 check and double-commit), proving the locked assertion is
        # load-bearing rather than a tautology.
        broke = any(run_round(disable_lock=True)[0] == 2 for _ in range(15))
        assert broke, "disabling the lock must expose the check->act race"
    finally:
        sys.setswitchinterval(old_interval)


# --- T002.3: shared effective-value resolver ---------------------------------


def test_shared_resolver_gives_real_consumer_surfaces_identical_effective_values():
    # Criterion 3: the REAL consumer surfaces resolve identical effective values
    # for identical state. This diffs two genuine call sites -- the API GET
    # effective value and the store reset effective value -- rather than calling
    # one pure function four times (a tautology). It FAILS if the two surfaces
    # diverge, mirroring the CLI/System-Health identical-posture precedent.
    catalog = _settings_catalog()
    store = MemoryPreferenceStore(catalog)
    # A tightened operator ceiling (7) BELOW even the reviewed default (30): so
    # the clamp is observable post-reset. Before reset the personal 60 clamps to
    # 7; after reset the override is gone but the default 30 STILL clamps to 7.
    store.seed_authority_value("policy", "policy.transcript_retention_max_days", 7)
    store.set_preference("personal", "alice", "personal.chat_transcript_retention_days", 60, 0, "alice")

    setting = "personal.chat_transcript_retention_days"
    with _pref_api_client(store) as client:
        api_before = {item["setting_id"]: item for item in
                      client.get("/api/preferences", headers=_PREF_API_ACTOR).json()["effective"]}[setting]
        # The store reset surface (via POST): removes the override and reports
        # the effective value it resolves.
        reset_effective = client.post(
            f"/api/preferences/{setting}/reset", headers=_PREF_API_ACTOR,
            json={"scope": "personal", "expected_version": 1},
        ).json()["effective"]
        api_after = {item["setting_id"]: item for item in
                     client.get("/api/preferences", headers=_PREF_API_ACTOR).json()["effective"]}[setting]

    # Reset (store surface) and GET (api surface) agree on the SAME effective
    # value for the SAME post-reset state -- both the clamped ceiling 7, not a
    # bare default 30. If reset ignored the ceiling (the old bug: value=30
    # source=default) these two real surfaces diverge and the test fails.
    assert reset_effective == api_after
    assert reset_effective["value"] == 7 and reset_effective["source"] == "clamped"
    assert api_before["value"] == 7 and api_before["source"] == "clamped"


def test_policy_ceiling_clamps_a_personal_value_that_exceeds_the_bound():
    # Criterion 1 / PRD non-goal: a personal value can never exceed a policy bound.
    catalog = _settings_catalog()
    stored = {
        "personal.chat_transcript_retention_days": 200,  # exceeds the 90 ceiling
        "policy.transcript_retention_max_days": 90,
    }
    resolved = resolve_effective_settings(catalog, stored)
    clamped = resolved["personal.chat_transcript_retention_days"]
    assert clamped.value == 90 and clamped.source == "clamped"
    assert clamped.repair is not None


def test_route_ceiling_is_enforced_via_ref_validity_not_a_silent_numeric_noop():
    # Finding 5: personal.default_chat_route carries a policy_ceiling that names
    # the route_allowlist_profile capability digest. That ref/non-scalar ceiling
    # is NOT a numeric clamp (which would be a silent no-op for a route string);
    # enforcement is the ref-validity path -- the live route set is scoped to the
    # approved profile, so an out-of-profile route falls back to the safe default.
    catalog = _settings_catalog()
    # The profile admits route.chat-fast only. An out-of-profile stored route
    # repairs to the reviewed default (which is in-profile), NOT served verbatim.
    out_of_profile = resolve_effective_settings(
        catalog, {"personal.default_chat_route": "route.premium-unapproved"},
        live_valid_refs={"route": {"route.chat-fast"}},
    )["personal.default_chat_route"]
    assert out_of_profile.source == "repaired" and out_of_profile.value == "route.chat-fast"
    assert out_of_profile.repair is not None
    # An in-profile route is served unchanged -- no false clamp/repair.
    in_profile = resolve_effective_settings(
        catalog, {"personal.default_chat_route": "route.chat-fast"},
        live_valid_refs={"route": {"route.chat-fast", "route.premium-unapproved"}},
    )["personal.default_chat_route"]
    assert in_profile.source == "stored" and in_profile.value == "route.chat-fast"


def test_invalidated_capability_resolves_to_safe_state_with_repair_notice():
    # Criterion 4 (T002 crit 3 / T002.3 crit 4): an invalidated capability/route
    # reference served by the WIRED GET /api/preferences endpoint falls back to a
    # safe state with a repair notice -- never the stale value. This goes through
    # the real API path so it FAILS if the endpoint stops threading ref-validity
    # into the shared resolver (the fallback-never-fires regression).
    catalog = _settings_catalog()
    store = MemoryPreferenceStore(catalog)
    # A stored route that the live valid set no longer admits (deleted long ago).
    store.set_preference("personal", "alice", "personal.default_chat_route", "route.deleted-long-ago", 0, "alice")

    # The live valid set contains the reviewed default but NOT the stale route.
    with _pref_api_client(store, live_valid_refs_provider=lambda: {"route": {"route.chat-fast"}}) as client:
        effective = {item["setting_id"]: item for item in
                     client.get("/api/preferences", headers=_PREF_API_ACTOR).json()["effective"]}
    repaired = effective["personal.default_chat_route"]
    # The stale value is NOT served; it is repaired to the reviewed default with a
    # notice (the default IS in the live valid set).
    assert repaired["source"] == "repaired" and repaired["value"] == "route.chat-fast"
    assert repaired.get("repair") is not None
    assert repaired["value"] != "route.deleted-long-ago"

    # With the reviewed-catalog baseline (NO injected provider), a stale stored
    # route is STILL repaired out of the box rather than served verbatim -- the
    # default source fires in the wired endpoint, not only when refs are injected.
    with _pref_api_client(store) as client:
        effective2 = {item["setting_id"]: item for item in
                      client.get("/api/preferences", headers=_PREF_API_ACTOR).json()["effective"]}
    repaired2 = effective2["personal.default_chat_route"]
    assert repaired2["source"] == "repaired" and repaired2["value"] == "route.chat-fast"

    # When the live set DOES admit the stored route, it is served unchanged (no
    # false repair): proves the fallback keys off validity, not blanket repair.
    with _pref_api_client(
        store, live_valid_refs_provider=lambda: {"route": {"route.deleted-long-ago", "route.chat-fast"}},
    ) as client:
        effective3 = {item["setting_id"]: item for item in
                      client.get("/api/preferences", headers=_PREF_API_ACTOR).json()["effective"]}
    served = effective3["personal.default_chat_route"]
    assert served["source"] == "stored" and served["value"] == "route.deleted-long-ago"


# --- T002: whole-slice integration -------------------------------------------


def test_preferences_slice_integration_scope_stale_migration_and_fallback():
    catalog = _settings_catalog()
    rows = PreferenceRows()
    store = MemoryPreferenceStore(catalog, rows)

    # 1) Scope resolution through the one shared resolver, with a policy ceiling
    #    clamping a personal value. The policy ceiling is seeded via the authority
    #    path (an approval-gated policy write is refused for an actor).
    store.set_preference("personal", "alice", "personal.chat_transcript_retention_days", 90, 0, "alice")
    store.seed_authority_value("policy", "policy.transcript_retention_max_days", 60)
    stored = {}
    stored.update(store.owned_values("policy", "policy"))
    stored.update(store.owned_values("personal", "alice"))
    resolved = resolve_effective_settings(catalog, stored)
    assert resolved["personal.chat_transcript_retention_days"].value == 60  # clamped to the tightened ceiling

    # 2) Stale write is reload-required and leaves the value intact.
    with pytest.raises(StalePreferenceWriteError):
        store.set_preference("personal", "alice", "personal.chat_transcript_retention_days", 10, 0, "alice")
    assert store.get("personal", "alice", "personal.chat_transcript_retention_days").value == 90

    # 3) Migration: a persisted v1 row loads as the current shape.
    migrated = migrate_preference_record({
        "setting_id": "personal.time_format", "scope": "personal", "actor": "alice",
        "value": "format_12h", "version": 4, "updated_at": "2026-07-20T00:00:00Z",
    })
    assert migrated.schema_version == PREFERENCE_RECORD_SCHEMA_VERSION and migrated.write_version == 4

    # 4) Capability invalidation through the WIRED endpoint falls back to a safe
    #    state, not a hard failure and not the stale value. Exercised via the real
    #    GET /api/preferences so a regression that drops ref-validity is caught.
    store.set_preference("personal", "alice", "personal.default_chat_route", "route.gone", 0, "alice")
    with _pref_api_client(store, live_valid_refs_provider=lambda: {"route": set()}) as client:
        effective = {item["setting_id"]: item for item in
                     client.get("/api/preferences", headers=_PREF_API_ACTOR).json()["effective"]}
    fallback = effective["personal.default_chat_route"]
    assert fallback["source"] == "repaired" and fallback["value"] != "route.gone"


# --------------------------------------------------------------------------- #
# plan-task-delivery T002/T004/T005/T008 — delivery projection, atomic Deliver,
# typed directive semantics.  These bind the store/logic layer; the router,
# redaction, and release-ordering proofs live in test_api / test_security_contract
# / test_release_workflow.
# --------------------------------------------------------------------------- #

import sys as _sys
import threading as _threading

from _support import load_example as _load_example
from workbench.deliver import (
    DeliverError,
    DeliverPreconditions,
    MemoryDeliverStartStore,
)
from workbench.delivery_projection import (
    ApprovalBinding,
    DeliveryImmutableError,
    MemoryDeliveryProjectionStore,
    NotEligibleError,
    RunDisplayRow,
    StaleEligibilityError,
    UnknownDeliveryRecordError,
)
from workbench.directives import (
    DIRECTIVE_OUTCOMES,
    record_packet_inclusion,
    session_directive_view,
    submit_directive,
)


def _reference(prd_id: str = "release-alpha", snapshot_digest: str | None = None) -> dict:
    ref = _load_example("task-reference.v1.json")
    if prd_id != "release-alpha":
        ref["ref"]["prd_id"] = prd_id
        ref["scoped_id"] = f"{prd_id}:T001"
        ref["run_label"] = f"{prd_id}:T001@r4"
        ref["hierarchy"]["prd_id"] = prd_id
        ref["summary"]["title"] = f"{prd_id} task"
    if snapshot_digest is not None:
        ref["source"]["snapshot_digest"] = snapshot_digest
    return ref


def _eligible_verdict(prd_id: str = "release-alpha") -> dict:
    return {
        "schema_version": "workbench-delivery-eligibility/v1",
        "ref": {"prd_id": prd_id, "task_id": "T001", "prd_revision": 4},
        "scoped_id": f"{prd_id}:T001",
        "eligible": True,
        "state": "eligible",
        "reasons": [
            {"class": "info", "code": "info.ready", "content_trust": "untrusted_task_data",
             "explanation": "All dependencies are merged and the source is current."}
        ],
    }


def _blocked_verdict(prd_id: str = "release-alpha") -> dict:
    verdict = _load_example("delivery-eligibility.v1.json")
    verdict["ref"]["prd_id"] = prd_id
    verdict["scoped_id"] = f"{prd_id}:T001"
    return verdict


_DIGEST_A = "sha256:5ddaacfaf8405e6e3f0d0a920e0f1f2b20afadded4f8d98748fb42868da0ad2e"
_DIGEST_B = "sha256:" + "b" * 64


def test_ptd_t002_two_prds_same_task_id_never_collapse():
    store = MemoryDeliveryProjectionStore()
    store.capture_task_reference("proj", _reference("release-alpha"))
    store.capture_task_reference("proj", _reference("release-beta"))
    alpha = store.get_task_reference("proj", "release-alpha", "T001")
    beta = store.get_task_reference("proj", "release-beta", "T001")
    assert alpha["scoped_id"] == "release-alpha:T001"
    assert beta["scoped_id"] == "release-beta:T001"
    assert alpha["summary"]["title"] != beta["summary"]["title"]
    assert [r["scoped_id"] for r in store.list_task_references("proj", "release-alpha")] == ["release-alpha:T001"]
    assert [r["scoped_id"] for r in store.list_task_references("proj", "release-beta")] == ["release-beta:T001"]


def test_ptd_t002_cross_project_lookup_is_indistinct_not_found():
    store = MemoryDeliveryProjectionStore()
    store.capture_task_reference("owner", _reference("release-alpha"))
    with pytest.raises(UnknownDeliveryRecordError):
        store.get_task_reference("intruder", "release-alpha", "T001")
    with pytest.raises(UnknownDeliveryRecordError):
        store.get_task_reference("owner", "release-alpha", "T099")


def test_ptd_t002_eligibility_goes_stale_when_source_snapshot_advances():
    store = MemoryDeliveryProjectionStore()
    store.capture_task_reference("proj", _reference("release-alpha", _DIGEST_A))
    store.capture_eligibility("proj", _eligible_verdict("release-alpha"))
    fresh = store.get_eligibility("proj", "release-alpha", "T001")
    assert fresh["state"] == "eligible" and fresh["eligible"] is True
    assert store.eligibility_for_start("proj", "release-alpha", "T001", _DIGEST_A)["state"] == "eligible"

    store.capture_task_reference("proj", _reference("release-alpha", _DIGEST_B))
    stale = store.get_eligibility("proj", "release-alpha", "T001")
    assert stale["state"] == "stale" and stale["eligible"] is False
    assert stale["reasons"][0]["code"] == "stale.snapshot_superseded"
    with pytest.raises(StaleEligibilityError):
        store.eligibility_for_start("proj", "release-alpha", "T001", _DIGEST_B)

    store.capture_task_reference("proj", _reference("release-alpha", _DIGEST_A))
    store.capture_eligibility("proj", _eligible_verdict("release-alpha"))
    with pytest.raises(StaleEligibilityError):
        store.eligibility_for_start("proj", "release-alpha", "T001", _DIGEST_B)


def test_ptd_t002_eligibility_for_start_fails_closed_when_not_eligible():
    store = MemoryDeliveryProjectionStore()
    store.capture_task_reference("proj", _reference("release-alpha", _DIGEST_A))
    store.capture_eligibility("proj", _blocked_verdict("release-alpha"))
    with pytest.raises(NotEligibleError):
        store.eligibility_for_start("proj", "release-alpha", "T001", _DIGEST_A)


def test_ptd_t002_capture_rejects_a_verdict_pinned_to_a_superseded_revision():
    # SHOULD-FIX 2 (capture-time TOCTOU): binding only the CURRENT snapshot digest
    # let a verdict computed against a superseded source (revision 4) be captured
    # against an already-advanced reference (revision 5), then served fresh and
    # reused for start. Capture must fail closed when the verdict's own pinned
    # prd_revision / scoped_id no longer matches the current reference.
    store = MemoryDeliveryProjectionStore()
    advanced = _reference("release-alpha", _DIGEST_B)
    advanced["ref"]["prd_revision"] = 5
    advanced["run_label"] = "release-alpha:T001@r5"
    store.capture_task_reference("proj", advanced)
    stale_verdict = _eligible_verdict("release-alpha")  # still pins prd_revision 4
    with pytest.raises(StaleEligibilityError):
        store.capture_eligibility("proj", stale_verdict)


def test_ptd_immutable_records_are_isolated_from_caller_mutation():
    # NOTE 5: capture_prd_content / capture_task_reference must deep-copy so a
    # caller retaining the nested content dict cannot mutate a stored "immutable"
    # record (through either the input alias or a returned read).
    store = MemoryDeliveryProjectionStore()
    ref = _reference("release-alpha")
    store.capture_task_reference("proj", ref)
    ref["summary"]["title"] = "MUTATED-VIA-INPUT"
    ref["ref"]["prd_revision"] = 999
    stored = store.get_task_reference("proj", "release-alpha", "T001")
    assert stored["summary"]["title"] != "MUTATED-VIA-INPUT"
    assert stored["ref"]["prd_revision"] == 4
    stored["summary"]["title"] = "MUTATED-VIA-READ"
    again = store.get_task_reference("proj", "release-alpha", "T001")
    assert again["summary"]["title"] != "MUTATED-VIA-READ"

    prd = _load_example("anvil-state.prd-content.v1.json")
    store.capture_prd_content("proj", prd)
    prd["content"]["body"] = "MUTATED-BODY"
    stored_prd = store.get_prd_content("proj", prd["prd"]["prd_id"])
    assert stored_prd["content"]["body"] != "MUTATED-BODY"


def _run_row(run_id: str = "run_alpha_t001_0001", **overrides) -> RunDisplayRow:
    defaults = dict(
        run_id=run_id, run_label="release-alpha:T001@r4", scoped_id="release-alpha:T001",
        prd_id="release-alpha", task_id="T001", prd_revision=4,
        task_title="Add routed chat", prd_title="Chat-first Workbench",
        status="running", attempt_label="attempt 1", started_at="2026-07-20T12:00:01Z",
        workflow_digest="sha256:" + "0" * 64, capability_profile_digest="sha256:" + "4" * 64,
    )
    defaults.update(overrides)
    return RunDisplayRow(**defaults)


def test_ptd_t004_run_row_headline_is_pinned_title_and_immutable():
    store = MemoryDeliveryProjectionStore()
    row = store.capture_run_row("proj", _run_row())
    assert row.headline == "Add routed chat"
    assert row.as_dict()["headline"] == "Add routed chat"
    store.capture_run_row("proj", _run_row())
    with pytest.raises(DeliveryImmutableError):
        store.capture_run_row("proj", _run_row(task_title="Renamed later"))
    assert store.get_run_row("proj", "run_alpha_t001_0001").task_title == "Add routed chat"


def test_ptd_t004_run_list_groups_filters_and_orders_attempts():
    store = MemoryDeliveryProjectionStore()
    store.capture_run_row("proj", _run_row("run_alpha_t001_0001", attempt_label="attempt 1",
                                           started_at="2026-07-20T12:00:01Z", status="evidenced"))
    store.capture_run_row("proj", _run_row("run_alpha_t001_0002", attempt_label="attempt 2",
                                           started_at="2026-07-20T13:00:01Z", status="running"))
    store.capture_run_row("proj", _run_row("run_beta_t001_0001", run_label="release-beta:T001@r4",
                                           scoped_id="release-beta:T001", prd_id="release-beta",
                                           task_title="beta task", status="queued",
                                           started_at="2026-07-20T14:00:01Z"))
    alpha = store.list_run_rows("proj", prd_id="release-alpha")
    assert [r.run_id for r in alpha] == ["run_alpha_t001_0001", "run_alpha_t001_0002"]
    assert [r.attempt_label for r in alpha] == ["attempt 1", "attempt 2"]
    assert alpha[0].started_at != alpha[1].started_at
    assert [r.run_id for r in store.list_run_rows("proj", status="running")] == ["run_alpha_t001_0002"]
    assert len(store.list_run_rows("proj", capability_profile_digest="sha256:" + "4" * 64)) == 3
    windowed = store.list_run_rows("proj", since="2026-07-20T12:30:00Z", until="2026-07-20T13:30:00Z")
    assert [r.run_id for r in windowed] == ["run_alpha_t001_0002"]


def test_ptd_t004_approval_binding_exposes_every_safe_binding():
    store = MemoryDeliveryProjectionStore()
    binding = ApprovalBinding(
        approval_id="approval_alpha_0001", scoped_id="release-alpha:T001",
        run_label="release-alpha:T001@r4", action="commit_pr", payload_hash="a" * 64,
        bridge_id="bridge-1", expires_at="2026-07-20T13:00:01Z",
        workflow_digest="sha256:" + "0" * 64, capability_profile_digest="sha256:" + "4" * 64,
    )
    store.capture_approval_binding("proj", binding)
    served = store.get_approval_binding("proj", "approval_alpha_0001").as_dict()
    assert set(served) == {
        "approval_id", "scoped_id", "run_label", "action", "payload_hash",
        "bridge_id", "expires_at", "workflow_digest", "capability_profile_digest",
    }
    with pytest.raises(DeliveryImmutableError):
        store.capture_approval_binding("proj", dataclasses.replace(binding, action="force_push"))


def _intent() -> dict:
    return _load_example("deliver-intent.v1.json")


def test_ptd_t005_start_is_idempotent_and_replays_without_relaunch():
    store = MemoryDeliverStartStore()
    launches: list[str] = []

    def launch():
        launches.append("x")
        return store.default_run_block("run_release_alpha_t001_0001")

    intent = _intent()
    receipt, replayed = store.start(intent, launch=launch, preconditions=DeliverPreconditions())
    assert receipt["status"] == "accepted" and replayed is False
    run_id = receipt["run"]["run_id"]
    again, replayed2 = store.start(intent, launch=launch, preconditions=DeliverPreconditions())
    assert replayed2 is True and again["status"] == "duplicate"
    assert again["run"]["run_id"] == run_id
    assert len(launches) == 1
    assert receipt["run"]["workflow_digest"] == intent["selections"]["workflow"]["digest"]
    assert receipt["run"]["capability_profile_digest"] == intent["selections"]["capability_profile_digest"]


def test_ptd_t005_precondition_failure_leaves_no_effect_and_is_ordered():
    cases = [
        (DeliverPreconditions(stale_snapshot=True), "deliver.stale_snapshot"),
        (DeliverPreconditions(dependency_changed=True), "deliver.dependency_changed"),
        (DeliverPreconditions(active_run=True), "deliver.active_run"),
        (DeliverPreconditions(invalid_worktree=True), "deliver.invalid_worktree"),
        (DeliverPreconditions(lease_lost=True), "deliver.lease_unavailable"),
        (DeliverPreconditions(capability_missing=True), "deliver.capability_missing"),
        (DeliverPreconditions(prd_unapproved=True), "deliver.prd_unapproved"),
    ]
    for preconditions, code in cases:
        store = MemoryDeliverStartStore()
        launched = []
        receipt, replayed = store.start(
            _intent(), launch=lambda: launched.append("x") or store.default_run_block("run_x_00001"),
            preconditions=preconditions,
        )
        assert receipt["status"] == "denied" and replayed is False
        assert receipt["error"]["code"] == code
        assert launched == []
        assert store.get_receipt(_intent()["intent_digest"]) is None

    store = MemoryDeliverStartStore()
    receipt, _ = store.start(
        _intent(), launch=lambda: store.default_run_block("run_x_00001"),
        preconditions=DeliverPreconditions(stale_snapshot=True, active_run=True, prd_unapproved=True),
    )
    assert receipt["error"]["code"] == "deliver.stale_snapshot"


def test_ptd_t005_launch_failure_stays_retriable_no_fabricated_success():
    store = MemoryDeliverStartStore()

    def boom():
        raise RuntimeError("codex launch failed")

    with pytest.raises(RuntimeError):
        store.start(_intent(), launch=boom, preconditions=DeliverPreconditions())
    assert store.get_receipt(_intent()["intent_digest"]) is None

    ok, replayed = store.start(
        _intent(), launch=lambda: store.default_run_block("run_retry_0001"),
        preconditions=DeliverPreconditions(),
    )
    assert ok["status"] == "accepted" and replayed is False


def test_ptd_t005_concurrent_starts_launch_exactly_once():
    store = MemoryDeliverStartStore()
    launches: list[str] = []
    barrier = _threading.Barrier(8)
    results: list[tuple[dict, bool]] = []
    lock = _threading.Lock()

    def launch():
        with lock:
            launches.append("x")
        return store.default_run_block("run_race_0001")

    def worker():
        barrier.wait()
        outcome = store.start(_intent(), launch=launch, preconditions=DeliverPreconditions())
        with lock:
            results.append(outcome)

    old = _sys.getswitchinterval()
    _sys.setswitchinterval(1e-6)
    try:
        threads = [_threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        _sys.setswitchinterval(old)

    assert len(launches) == 1
    accepted = [r for r, replayed in results if not replayed]
    duplicates = [r for r, replayed in results if replayed]
    assert len(accepted) == 1 and len(duplicates) == 7
    run_ids = {r["run"]["run_id"] for r, _ in results}
    assert run_ids == {"run_race_0001"}


def test_ptd_t005_tampered_intent_fails_closed_before_any_effect():
    store = MemoryDeliverStartStore()
    tampered = _intent()
    tampered["selections"]["workflow"]["revision"] = "2"
    with pytest.raises(DeliverError):
        store.start(tampered, launch=lambda: store.default_run_block("run_x_00001"),
                    preconditions=DeliverPreconditions())


def _session_store():
    store = MemoryStore()
    project = store.create_project("demo", ".anvil")
    session, workflow = store.create_session(project.id, "s", "checkout-a", delivery_workflow())
    return store, session, workflow


def test_ptd_t008_directive_outcomes_are_typed_and_append_only():
    store, session, _ = _session_store()
    ok = submit_directive(store, session.id, "Run the independent evidence check.", "operator")
    assert ok["outcome"] == "directive.queued_pending" and ok["recorded"] is True
    assert ok["event"].kind == "operator.directive"
    empty = submit_directive(store, session.id, "   ", "operator")
    assert empty["outcome"] == "directive.rejected_empty" and empty["recorded"] is False
    toolong = submit_directive(store, session.id, "x" * 8001, "operator")
    assert toolong["outcome"] == "directive.rejected_too_long" and toolong["recorded"] is False
    unknown = submit_directive(store, "sess_missing", "hi", "operator")
    assert unknown["outcome"] == "directive.rejected_unknown_session"
    for result in (ok, empty, toolong, unknown):
        assert result["outcome"] in DIRECTIVE_OUTCOMES
    events = [e for e in store.list_workflow_events(session.id) if e.kind == "operator.directive"]
    assert len(events) == 1


def test_ptd_t008_pending_vs_packet_included_distinction():
    store, session, _ = _session_store()
    first = submit_directive(store, session.id, "first steer", "operator")["event"]
    submit_directive(store, session.id, "second steer", "operator")
    view = session_directive_view(store, session.id)
    assert [d["content"] for d in view["pending"]] == ["first steer", "second steer"]
    assert view["included"] == []
    record_packet_inclusion(store, session.id, first.sequence)
    view2 = session_directive_view(store, session.id)
    assert [d["content"] for d in view2["included"]] == ["first steer"]
    assert [d["content"] for d in view2["pending"]] == ["second steer"]


def test_ptd_t008_directive_never_signals_a_bridge_effect():
    store, session, _ = _session_store()
    submit_directive(store, session.id, "please steer", "operator")
    assert store.commands == {} or all(len(q) == 0 for q in store.commands.values())
    assert store.list_runs() == []
