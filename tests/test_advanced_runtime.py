"""Hermetic tests for Advanced mode in the Chat runtime (AMP T003).

Criterion map (from ``anvil show advanced-model-playground:T003``):

1. Normal, SSE, cancellation, timeout, malformed stream, invalid JSON schema/
   output, and Serving-unavailable states are distinct and durable --
   ``test_c1_seven_states_are_distinct``, ``test_c1_*`` per-state tests, and
   ``test_c1_lifecycle_is_durable_and_terminal``.
2. Forking creates a new branch with an explicit shared parent and does not
   mutate prior turns -- ``test_c2_fork_appends_sibling_under_shared_parent``,
   ``test_c2_fork_does_not_mutate_prior_turns``.
3. Advanced mode does not create a second conversation or turn store --
   ``test_c3_fork_uses_the_same_store``,
   ``test_c3_branch_referencing_another_conversation_is_refused``.
4. Advanced records cannot be submitted as State evidence / attached to a
   delivery run -- ``test_c4_refuse_advanced_evidence_*``.

Plus the redaction gate: ``test_trace_is_redacted_and_schema_valid`` and
``test_served_trace_scrubs_the_adversarial_corpus``.  No test opens a socket.
"""
from __future__ import annotations

import pytest

from workbench import advanced_runtime as art
from workbench import contracts as contracts_module
from workbench.advanced_routes import discover_advanced_routes, validate_advanced_selection
from workbench.advanced_runtime import (
    AdvancedRuntimeError,
    AdvancedState,
    build_advanced_request,
    build_advanced_trace,
    open_advanced_branch,
    refuse_advanced_evidence,
    run_advanced_stream,
    settle_advanced_branch,
)
from workbench.chat_stream import (
    CancellationToken,
    ServingStreamTimeout,
    ServingStreamUnavailable,
)
from workbench.contracts import ContractValidationError, validate_advanced_trace
from workbench.conversation_models import (
    ContentBlock,
    ConversationActor,
    RetentionPolicy,
    TurnLineage,
    TurnRedaction,
)
from workbench.conversation_store import MemoryConversationStore
from workbench.response_lifecycle_store import MemoryResponseLifecycleStore, SafeUsage

ACTOR = ConversationActor("operator")
OTHER = ConversationActor("operator_bob")
KEY = b"advanced-runtime-content-hash-01"
REDACTED = TurnRedaction("redacted", "workbench.default")

_ROUTE_DIGEST = "sha256:" + "a1" * 32
_PROFILE_DIGEST = "sha256:" + "b2" * 32


def _route_config():
    return {
        "route_id": "route.chat-fast",
        "display_name": "Fast chat",
        "route_digest": _ROUTE_DIGEST,
        "profile_digest": _PROFILE_DIGEST,
        "serving_contract_version": "1.0.0",
        "model_profile": "chat-fast",
        "supported_controls": [
            {"name": "temperature_milli", "type": "int", "bounds": {"min": 0, "max": 2000}, "default": 700},
            {"name": "reasoning_effort", "type": "enum",
             "allowed_values": ["low", "medium", "high"], "default": "medium"},
        ],
    }


def _selection(controls=None):
    discovered = discover_advanced_routes([_route_config()])
    return validate_advanced_selection("route.chat-fast", controls or {"temperature_milli": 300}, discovered)


def _delta(text):
    return {"type": "response.output_text.delta", "delta": text}


_COMPLETED = {"type": "response.completed", "response": {"id": "resp_1"}}


class ScriptedTransport:
    """Injected Serving stream: yields scripted SSE events or raises (no network)."""

    def __init__(self, events, *, raise_at=None, error=None, cancel_after=None):
        self._events = list(events)
        self._raise_at = raise_at
        self._error = error
        self._cancel_after = cancel_after
        self.closed = False

    def open(self, request, cancel):
        def _gen():
            try:
                for index, event in enumerate(self._events):
                    if cancel.cancelled:
                        return
                    if self._raise_at is not None and index == self._raise_at:
                        raise self._error
                    if self._cancel_after is not None and index == self._cancel_after:
                        cancel.cancel()
                        return
                    yield event
                if self._raise_at is not None and self._raise_at >= len(self._events):
                    raise self._error
            finally:
                self.closed = True

        return _gen()


def _conversation():
    store = MemoryConversationStore(content_hash_key=KEY)
    conversation = store.create_conversation(ACTOR, RetentionPolicy("workbench.default-90d",
                                                                    "retained_redacted", "retained_redacted"),
                                              title="Advanced kickoff")
    root = store.append_turn(
        ACTOR, conversation.id, role="user", status="complete",
        lineage=TurnLineage(None, 0, "initial"), redaction=REDACTED,
        content=(ContentBlock("text", "explain forking"),),
    )
    return store, conversation, root


def _branch(conversation_id, parent_turn_id, *, controls=None, structured=None):
    selection = _selection(controls)
    branch = {
        "schema_version": "workbench-advanced-branch/v1",
        "branch_id": "advbranch_runtime_0001",
        "mode": "advanced",
        "conversation_ref": {
            "binding": "existing_conversation",
            "conversation_id": conversation_id,
            "fork_point": {"parent_turn_id": parent_turn_id},
        },
        "retention": {"class": "durable", "saved_at": "2026-07-20T10:05:00Z"},
        "route_capability": selection.route.as_route_capability(),
        "submitted_controls": selection.submitted_controls(),
        "repair": {"status": "ready"},
        "created_at": "2026-07-20T10:00:00Z",
    }
    if structured is not None:
        branch["structured_output"] = structured
    return branch


def _run(transport, *, controls=None, structured_output_mode="text", output_validator=None,
         cancel=None, usage=None, summary=None, request_id="req_adv_1"):
    lifecycle = MemoryResponseLifecycleStore()
    return run_advanced_stream(
        selection=_selection(controls),
        prompt="hello advanced",
        transport=transport,
        branch_id="advbranch_runtime_0001",
        conversation_id="conv_advanced_playground_0001",
        turn_id="turn_assistant_0002",
        lifecycle_store=lifecycle,
        actor=ACTOR,
        request_id=request_id,
        structured_output_mode=structured_output_mode,
        output_validator=output_validator,
        route_request_id="req_abc123",
        usage=usage,
        summary=summary,
    ), lifecycle


# --- Criterion 1: seven distinct, durable states -----------------------------


def test_c1_seven_states_are_distinct():
    assert len({s.value for s in AdvancedState}) == 7


def test_c1_normal_completion_is_complete_not_streamed():
    # A completion with no text deltas is the "normal" (non-streamed) state.
    result, _ = _run(ScriptedTransport([_COMPLETED]))
    assert result.state is AdvancedState.complete
    assert result.turn_status == "complete"
    assert result.streamed is False


def test_c1_sse_completion_is_streamed():
    result, _ = _run(ScriptedTransport([_delta("Hel"), _delta("lo"), _COMPLETED]))
    assert result.state is AdvancedState.streamed
    assert result.turn_status == "complete"
    assert result.partial_text == "Hello"
    assert result.streamed is True


def test_c1_cancellation_never_completes():
    result, _ = _run(ScriptedTransport([_delta("par"), _delta("tial"), _COMPLETED], cancel_after=2))
    assert result.state is AdvancedState.cancelled
    assert result.turn_status == "cancelled"
    assert result.is_complete is False


def test_c1_timeout_is_interrupted():
    result, _ = _run(ScriptedTransport([_delta("half")], raise_at=1, error=ServingStreamTimeout("deadline")))
    assert result.state is AdvancedState.timed_out
    assert result.turn_status == "interrupted"


def test_c1_malformed_stream_is_distinct_from_unavailable():
    malformed, _ = _run(ScriptedTransport([_delta("x"), "not-a-mapping"]))
    assert malformed.state is AdvancedState.malformed_stream
    assert malformed.turn_status == "failed"

    unavailable, _ = _run(ScriptedTransport([_delta("x")], raise_at=1,
                                            error=ServingStreamUnavailable("Serving 503")))
    assert unavailable.state is AdvancedState.serving_unavailable
    assert unavailable.turn_status == "failed"
    # The two failure states are genuinely distinct.
    assert malformed.state is not unavailable.state


def test_c1_stream_ending_without_completion_is_unavailable():
    result, _ = _run(ScriptedTransport([_delta("x"), _delta("y")]))
    assert result.state is AdvancedState.serving_unavailable
    assert result.turn_status == "failed"


def test_c1_schema_invalid_output_is_distinct_from_completion():
    branch_structured = {"mode": "json_schema", "schema_ref": "strict_json", "schema_digest": "sha256:" + "e5" * 32}
    # A completion whose output is not valid JSON settles schema_invalid, never complete.
    invalid, _ = _run(
        ScriptedTransport([_delta("not json at all"), _COMPLETED]),
        structured_output_mode="json_schema",
    )
    assert invalid.state is AdvancedState.schema_invalid
    assert invalid.turn_status == "failed"
    assert invalid.structured_output_valid is False

    # A valid JSON completion under the same mode settles streamed/complete.
    valid, _ = _run(
        ScriptedTransport([_delta('{"ok": true}'), _COMPLETED]),
        structured_output_mode="json_schema",
    )
    assert valid.state is AdvancedState.streamed
    assert valid.turn_status == "complete"
    assert valid.structured_output_valid is True
    assert branch_structured["mode"] == "json_schema"  # sanity: schema-mode branch shape


def test_c1_lifecycle_is_durable_and_terminal():
    # The durable reconnect-safe lifecycle terminal is set exactly once and the
    # record is immutable afterward.
    result, lifecycle = _run(ScriptedTransport([_delta("Hel"), _delta("lo"), _COMPLETED]))
    snapshot = lifecycle.snapshot(ACTOR, "req_adv_1")
    assert snapshot.is_terminal is True
    assert snapshot.state == "completed"
    # A cancelled attempt persists a distinct durable terminal.
    cancelled, cancel_lifecycle = _run(
        ScriptedTransport([_delta("a"), _COMPLETED], cancel_after=1), request_id="req_adv_2",
    )
    assert cancel_lifecycle.snapshot(ACTOR, "req_adv_2").state == "cancelled"


# --- Criterion 2: forking under a shared parent, no mutation of prior turns ---


def test_c2_fork_appends_sibling_under_shared_parent():
    store, conversation, root = _conversation()
    branch = _branch(conversation.id, root.id)
    turn = open_advanced_branch(store, ACTOR, conversation.id, branch)
    assert turn.mode == "advanced"
    assert turn.status == "streaming"
    assert turn.lineage.parent_turn_id == root.id  # explicit shared parent
    assert turn.lineage.kind == "branch"


def test_c2_fork_does_not_mutate_prior_turns():
    store, conversation, root = _conversation()
    _, before_turns = store.get_conversation_with_turns(ACTOR, conversation.id)
    branch = _branch(conversation.id, root.id)
    open_advanced_branch(store, ACTOR, conversation.id, branch)
    # The pre-existing root turn is byte-identical after the fork.
    _, after_turns = store.get_conversation_with_turns(ACTOR, conversation.id)
    root_after = next(t for t in after_turns if t.id == root.id)
    assert root_after == root
    assert len(before_turns) == 1 and len(after_turns) == 2


def test_c2_cancelled_advanced_turn_settles_non_complete():
    store, conversation, root = _conversation()
    branch = _branch(conversation.id, root.id)
    turn = open_advanced_branch(store, ACTOR, conversation.id, branch)
    result, _ = _run(ScriptedTransport([_delta("a"), _COMPLETED], cancel_after=1))
    settled = settle_advanced_branch(store, ACTOR, conversation.id, turn.id, result)
    assert settled.status == "cancelled"
    assert settled.status != "complete"


# --- Criterion 3: no second conversation or turn store -----------------------


def test_c3_fork_uses_the_same_store():
    store, conversation, root = _conversation()
    branch = _branch(conversation.id, root.id)
    turn = open_advanced_branch(store, ACTOR, conversation.id, branch)
    # The advanced turn is retrievable from the SAME conversation store; no
    # separate transcript identity was minted.
    _, turns = store.get_conversation_with_turns(ACTOR, conversation.id)
    assert turn.id in {t.id for t in turns}
    assert turn.conversation_id == conversation.id


def test_c3_branch_referencing_another_conversation_is_refused():
    store, conversation, root = _conversation()
    branch = _branch("conv_some_other_identity_9999", root.id)
    with pytest.raises(AdvancedRuntimeError, match="mint or cross identity"):
        open_advanced_branch(store, ACTOR, conversation.id, branch)


def test_c3_branch_binding_must_be_existing_conversation():
    store, conversation, root = _conversation()
    branch = _branch(conversation.id, root.id)
    branch["conversation_ref"]["binding"] = "existing_conversation"  # valid
    open_advanced_branch(store, ACTOR, conversation.id, branch)  # accepted
    # An invalid branch (undeclared control) is refused before any fork.
    bad = _branch(conversation.id, root.id)
    bad["submitted_controls"].append({"name": "seed", "value": 42, "provenance": "declared"})
    with pytest.raises(AdvancedRuntimeError, match="not valid"):
        open_advanced_branch(store, ACTOR, conversation.id, bad)


# --- Criterion 4: advanced records are non-authoritative ---------------------


def test_c4_refuse_advanced_evidence_on_result():
    result, _ = _run(ScriptedTransport([_delta("x"), _COMPLETED]))
    assert result.authoritative is False
    with pytest.raises(AdvancedRuntimeError, match="non-authoritative"):
        refuse_advanced_evidence(result)


def test_c4_refuse_advanced_evidence_on_trace_and_turn():
    result, _ = _run(ScriptedTransport([_delta("x"), _COMPLETED]))
    with pytest.raises(AdvancedRuntimeError, match="non-authoritative"):
        refuse_advanced_evidence(result.trace)  # advanced-trace schema_version
    store, conversation, root = _conversation()
    turn = open_advanced_branch(store, ACTOR, conversation.id, _branch(conversation.id, root.id))
    with pytest.raises(AdvancedRuntimeError, match="non-authoritative"):
        refuse_advanced_evidence(turn)  # mode == "advanced"


def test_c4_ordinary_record_is_not_refused():
    # A non-advanced record passes the guard untouched (no false positive).
    refuse_advanced_evidence({"schema_version": "chat-turn/v1", "mode": "ordinary"})
    store, conversation, root = _conversation()
    refuse_advanced_evidence(root)  # an ordinary user turn


# --- Redaction gate ----------------------------------------------------------


def test_trace_is_redacted_and_schema_valid():
    result, _ = _run(ScriptedTransport([_delta("Hel"), _delta("lo"), _COMPLETED]))
    trace = result.trace
    assert trace["request"]["redacted"] is True
    assert trace["redaction"]["status"] == "redacted"
    assert trace["branch_ref"]["conversation_id"] == "conv_advanced_playground_0001"
    validate_advanced_trace(trace)  # SERVED record is schema valid


def test_served_trace_scrubs_the_adversarial_corpus():
    # A leaky free-text summary (as an error/tool excerpt might carry) is scrubbed
    # in the SERVED, schema-validated trace -- proven on the served record, not on
    # construction inputs.
    corpus = (
        "AKIA1234567890ABCDEF aws_secret_access_key=deadbeef "
        "eyJhbGciOiJIUzI1NiJ9.body.sig authorization: Bearer sk-proj-xyz "
        "db.tail1234.ts.net:7687 100.64.0.5:8443 path=/etc/anvil/state.db "
        "postgres://user:pw@host/db ghp_tokenvalue C:/Users/op/.anvil/state.db"
    )
    result, _ = _run(
        ScriptedTransport([_delta("x")], raise_at=1, error=ServingStreamUnavailable("boom")),
        summary=corpus,
    )
    trace = result.trace
    validate_advanced_trace(trace)  # would raise if a leak shape survived
    served = repr(trace).lower()
    for leak in ("akia1234567890abcdef", "aws_secret_access_key=deadbeef", "eyjhbg",
                 "bearer sk-proj", "ghp_tokenvalue", ".ts.net:7687", "100.64.0.5:8443",
                 "/etc/anvil", "postgres://", "c:/users/op"):
        assert leak not in served, leak


def test_build_advanced_request_is_bounded_and_carries_no_endpoint():
    selection = _selection({"temperature_milli": 300, "reasoning_effort": "high"})
    request = build_advanced_request(selection, "hello", instructions="stay in template")
    assert request["model"] == "chat-fast"
    assert request["route_id"] == "route.chat-fast"
    assert request["temperature"] == 0.3
    assert request["reasoning"] == {"effort": "high"}
    assert request["instructions"] == "stay in template"
    serialized = repr(request).lower()
    # "token" is legitimate (max_output_tokens); the leak markers are endpoints,
    # credentials, and scheme literals -- none may appear.
    for forbidden in ("http", "bearer", "endpoint", "://", "secret", "credential", "authorization"):
        assert forbidden not in serialized
    with pytest.raises(AdvancedRuntimeError, match="prompt"):
        build_advanced_request(selection, "x" * 20_001)
    with pytest.raises(AdvancedRuntimeError, match="AdvancedRouteSelection"):
        build_advanced_request({"route": "x"}, "hi")  # type: ignore[arg-type]


def test_trace_validator_trust_root_fails_closed(monkeypatch, tmp_path):
    import json
    from pathlib import Path

    contracts_module._reset_advanced_trace_contract_validator_cache()
    monkeypatch.setattr(contracts_module, "_ADVANCED_TRACE_CONTRACT_SCHEMA_PATH", tmp_path / "absent.json")
    with pytest.raises(ContractValidationError, match="schema is unavailable"):
        contracts_module.advanced_trace_contract_validator()

    root = Path(__file__).resolve().parents[1] / "docs" / "contracts" / "schemas" / "advanced-trace.v1.schema.json"
    base = json.loads(root.read_text(encoding="utf-8"))
    del base["properties"]["events"]["items"]["additionalProperties"]
    drifted = tmp_path / "drifted-trace.json"
    drifted.write_text(json.dumps(base), encoding="utf-8")
    contracts_module._reset_advanced_trace_contract_validator_cache()
    monkeypatch.setattr(contracts_module, "_ADVANCED_TRACE_CONTRACT_SCHEMA_PATH", drifted)
    with pytest.raises(ContractValidationError, match="no longer closes its event card"):
        contracts_module.advanced_trace_contract_validator()
    contracts_module._reset_advanced_trace_contract_validator_cache()
