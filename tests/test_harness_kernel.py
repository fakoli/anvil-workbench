from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import sys
import types
from pathlib import Path

import pytest

from workbench.capability_profiles import validate_project_profile
from workbench.models import (
    RunCapability,
    RunConstraints,
    RunContext,
    RunContextError,
    RunCursor,
    RunIdentity,
    RunReceipt,
    RunSkill,
    RunWorkflowPin,
    UntrustedEvidence,
    UntrustedTask,
    UntrustedTaskRef,
    run_capabilities_from_snapshot,
    run_skills_from_snapshot,
)
from workbench.provider_catalogs import (
    DEFAULT_PROVIDER_ALLOWLIST,
    PublishedCatalogSet,
    validate_provider_catalog,
)
from workbench.store import MemoryStore, StoreError
from workbench.voice import VoiceRelayError, relay_realtime, sanitize_client_event, summarize_server_event
from workbench.workflow_snapshot import compile_workflow_snapshot
from workbench.workflows import WorkflowError, validate_definition

_EXAMPLES = Path(__file__).resolve().parents[1] / "docs" / "contracts" / "examples"


def _load_example(name: str) -> dict:
    return json.loads((_EXAMPLES / name).read_text(encoding="utf-8"))


def compiled_delivery_snapshot():
    """Compile the reviewed delivery snapshot from the checked-in examples."""
    published = PublishedCatalogSet(
        catalogs=tuple(
            validate_provider_catalog(provider, _load_example(f"{provider}.catalog.v1.json"))
            for provider in sorted(DEFAULT_PROVIDER_ALLOWLIST)
        )
    )
    profile = validate_project_profile(
        _load_example("project-capability-profile.v1.json"),
        published,
        configured_model_profiles=("coding-local", "planning-local"),
        configured_skills={"anvil:execute": "sha256:" + "7" * 64},
        approval_actions=("commit_pr", "merge_and_accept"),
    )
    workflow = _load_example("delivery.workflow.v2.json")
    selected_ops: list[dict] = []
    seen: set[tuple] = set()
    for step in workflow["steps"]:
        if step["kind"] != "operation":
            continue
        key = tuple(sorted(step["operation"].items()))
        if key not in seen:
            seen.add(key)
            selected_ops.append(copy.deepcopy(step["operation"]))
    return compile_workflow_snapshot(
        workflow, profile, published,
        selected_operations=selected_ops,
        selected_skills=[{"id": "anvil:execute", "digest": "sha256:" + "7" * 64}],
        route="coding-local",
    )


def valid_run_context(**overrides) -> RunContext:
    """Build a complete, valid run context; overrides replace capture kwargs."""
    snapshot = overrides.pop("snapshot", None) or compiled_delivery_snapshot()
    kwargs = dict(
        context_id="ctx_run_example_0001",
        identity=RunIdentity(
            run_id="run_1", session_id="sess_1", bridge_id="bridge_1",
            worktree_name="checkout-a", task_id="release-beta:T001", request_id="req_1",
        ),
        workflow=RunWorkflowPin.from_snapshot(snapshot),
        capabilities=run_capabilities_from_snapshot(snapshot),
        skills=run_skills_from_snapshot(snapshot, {"anvil:execute": "State-backed implementation guidance."}),
        constraints=RunConstraints(
            turn_limit=12, tool_limit=24,
            stop_conditions=("Do not submit evidence before verification passes.",),
        ),
        cursor=RunCursor(
            step_id="implement", attempt=1,
            completed_receipts=(RunReceipt(receipt_id="rcpt_claim", summary="claim succeeded"),),
        ),
        task=UntrustedTask(
            ref=UntrustedTaskRef(prd_id="release-beta", task_id="T001", prd_revision=5),
            title="Add a documented operation contract",
            acceptance_criteria=("Add a versioned resource", "Validate its JSON shape"),
            work_packet_digest="sha256:" + "8" * 64,
            scope=("docs/contracts",),
            verification_plan=("Run the allowlisted verification command.",),
        ),
        evidence=(UntrustedEvidence(citation="state-event:claim", summary="Task claim is active."),),
    )
    kwargs.update(overrides)
    return RunContext.capture(**kwargs)


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
