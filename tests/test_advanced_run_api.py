"""Wired-path live Advanced "Run branch" join (advanced-model-playground).

These tests drive the PRODUCTION endpoint ``POST
/api/conversations/{id}/advanced/run`` through a FastAPI ``TestClient`` (the real
router, actor dependency, conversation store, response-lifecycle machinery, and
chat relay), injecting only a scripted Serving transport over the SAME
``ServingStreamTransport`` contract the hermetic relay qualifies.  No socket is
opened and no HTTP client is reached: a Serving failure is a scripted transport
exception, exactly as the relay's injected transport models it.

The emitted frame shapes match ``web/src/chat-api.js`` / ``web/src/api.js``
``runAdvancedBranch`` field-for-field: ``{seq, kind:'delta', text}`` and a
terminal ``{seq, kind:'terminal', outcome, turn_id, branch_id, trace}``.  The
disconnect test additionally runs a REAL uvicorn server so a mid-stream socket
close delivers ``http.disconnect`` and settles the durable turn cancelled.
"""
from __future__ import annotations

import contextlib
import json
import re
import socket
import threading
import time

import httpx
import uvicorn
from fastapi.testclient import TestClient

from workbench.advanced_routes import discover_advanced_routes, validate_advanced_selection
from workbench.advanced_runtime import (
    AdvancedState,
    run_advanced_stream,
    stream_advanced_attempt,
)
from workbench.api import create_app
from workbench.chat_stream import RelayEvent, ServingStreamUnavailable
from workbench.config import Settings
from workbench.conversation_models import ConversationActor
from workbench.conversation_store import MemoryConversationStore
from workbench.graph import NullGraph
from workbench.response_lifecycle_store import MemoryResponseLifecycleStore
from workbench.store import MemoryStore

KEY = "adv-run-api-test-content-hash-32b"
OWNER = "operator"
OTHER = "reviewer"

#: A router base host/token that MUST NEVER appear in any response byte (no leak).
ROUTER_HOST = "serving.example.ts.net"
ROUTER_BASE_URL = f"https://{ROUTER_HOST}"
ROUTER_TOKEN = "test-router-token-value"

_ROUTE_DIGEST = "sha256:" + "a1" * 32
_PROFILE_DIGEST = "sha256:" + "b2" * 32

_ROUTE_CONFIG = {
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
ADVANCED_ROUTES = json.dumps([_ROUTE_CONFIG])


def _delta(text: str) -> dict:
    return {"type": "response.output_text.delta", "delta": text}


_COMPLETED = {"type": "response.completed", "response": {"id": "resp_1"}}


class ScriptedTransport:
    """Injected Serving stream: scripted SSE events or a Serving failure (no network)."""

    def __init__(self, events, *, raise_at=None, error=None, cancel_after=None):
        self._events = list(events)
        self._raise_at = raise_at
        self._error = error
        self._cancel_after = cancel_after
        self.opened = False
        self.closed = False
        self.request = None

    def open(self, request, cancel):
        self.opened = True
        self.request = request

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


class BlockingTransport:
    """Yield the scripted deltas, then BLOCK until the caller trips cancel.

    Models a real Serving stream that has NOT completed when the browser aborts:
    the client disconnect must trip cancel (via the endpoint's teardown), which
    this transport observes to stop -- so the durable turn settles cancelled.
    """

    def __init__(self, deltas):
        self._deltas = list(deltas)
        self.opened = False
        self.closed = False

    def open(self, request, cancel):
        self.opened = True

        def _gen():
            try:
                for text in self._deltas:
                    if cancel.cancelled:
                        return
                    yield _delta(text)
                for _ in range(2000):  # bounded ~20s; cancel ends it far sooner
                    if cancel.cancelled:
                        return
                    time.sleep(0.01)
            finally:
                self.closed = True

        return _gen()


def _settings(**overrides) -> Settings:
    values = dict(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner=OWNER, approvers=frozenset({OWNER, OTHER}), bridge_bootstrap_token="",
        anvil_router_base_url=ROUTER_BASE_URL, anvil_router_token=ROUTER_TOKEN,
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=False,
        chat_content_hash_key=KEY, advanced_routes=ADVANCED_ROUTES,
    )
    values.update(overrides)
    return Settings(**values)


def _build_app(transport=None, **overrides):
    conv_store = MemoryConversationStore(content_hash_key=KEY.encode("utf-8"))
    lifecycle = MemoryResponseLifecycleStore(recover_on_open=True)
    tf = (lambda _selection: transport) if transport is not None else None
    app = create_app(
        settings=_settings(**overrides), store=MemoryStore(), graph=NullGraph(),
        conversation_store=conv_store, response_lifecycle_store=lifecycle,
        chat_stream_transport_factory=tf,
    )
    return app, conv_store, lifecycle


def _build(transport=None, **overrides):
    app, conv_store, lifecycle = _build_app(transport=transport, **overrides)
    return TestClient(app), conv_store, lifecycle


def _client(transport=None, **overrides) -> TestClient:
    client, _store, _lifecycle = _build(transport=transport, **overrides)
    return client


def _actor(name: str) -> dict[str, str]:
    return {"X-Workbench-Actor": name}


def _create_conversation(client: TestClient, actor: str = OWNER) -> str:
    response = client.post("/api/conversations", json={"title": "Advanced run"}, headers=_actor(actor))
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _create_parent_turn(client: TestClient, conversation_id: str, actor: str = OWNER) -> str:
    """Append a root user turn to fork the advanced attempt under."""
    response = client.post(
        f"/api/conversations/{conversation_id}/turns",
        json={
            "role": "user", "status": "complete",
            "lineage": {"parent_turn_id": None, "sibling_index": 0, "kind": "initial"},
            "content": [{"kind": "text", "text": "explain forking"}],
        },
        headers=_actor(actor),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _frames(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _run_body(parent_turn_id: str, *, controls=None, **extra) -> dict:
    body = {
        "parent_turn_id": parent_turn_id,
        "branch_id": "advbranch_local_0001",
        "route_id": "route.chat-fast",
        "controls": controls if controls is not None else [{"name": "temperature_milli", "value": 300, "provenance": "declared"}],
        "prompt": "hello advanced",
    }
    body.update(extra)
    return body


def _run(client: TestClient, conversation_id: str, parent_turn_id: str, actor: str = OWNER, **kw):
    return client.post(
        f"/api/conversations/{conversation_id}/advanced/run",
        json=_run_body(parent_turn_id, **kw), headers=_actor(actor),
    )


def _turns(client: TestClient, conversation_id: str, actor: str = OWNER) -> list[dict]:
    response = client.get(f"/api/conversations/{conversation_id}", headers=_actor(actor))
    assert response.status_code == 200, response.text
    return response.json()["turns"]


def _assistants(client, conversation_id, actor=OWNER):
    return [t for t in _turns(client, conversation_id, actor) if t["role"] == "assistant"]


# --- happy path: streamed deltas, terminal carries ids + redacted trace -------


def test_happy_path_streams_deltas_then_terminal_with_ids_and_trace():
    transport = ScriptedTransport([_delta("tun"), _delta("ed"), _COMPLETED])
    with _client(transport) as client:
        conversation_id = _create_conversation(client)
        parent = _create_parent_turn(client, conversation_id)
        response = _run(client, conversation_id, parent)
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("application/x-ndjson")

        frames = _frames(response.text)
        assert [f["kind"] for f in frames] == ["delta", "delta", "terminal"]
        assert frames[0]["text"] == "tun" and frames[1]["text"] == "ed"
        assert [f["seq"] for f in frames] == [1, 2, 3]

        terminal = frames[-1]
        assert terminal["outcome"] == "completed"
        # The branch_id is minted SERVER-side (advbranch_ grammar the durable trace
        # requires), NOT the client's advisory local value -- so a client that sends
        # a non-conforming branch_id can never fail the trace schema.  The browser
        # adopts this authoritative id from the terminal frame.
        assert re.fullmatch(r"advbranch_[a-zA-Z0-9_-]{8,128}", terminal["branch_id"])
        assert terminal["branch_id"] != "advbranch_local_0001"  # not echoed
        assert terminal["trace"]["branch_ref"]["branch_id"] == terminal["branch_id"]
        assert isinstance(terminal["turn_id"], str) and terminal["turn_id"]
        assert terminal["trace"]["schema_version"] == "workbench-advanced-trace/v1"
        assert terminal["trace"]["status"] == "complete"

        # The production seam pins the route's Serving model_profile + streaming.
        assert transport.request["model"] == "chat-fast"
        assert transport.request["stream"] is True

        # The durable advanced sibling settled complete and is a mode="advanced" turn.
        assistants = _assistants(client, conversation_id)
        assert len(assistants) == 1
        assistant = assistants[0]
        assert assistant["id"] == terminal["turn_id"]
        assert assistant["mode"] == "advanced"
        assert assistant["status"] == "complete"
        assert transport.closed is True


def test_non_conforming_client_branch_id_still_completes():
    # The live browser sends a NON-conforming advisory branch_id ("advbranch-1":
    # hyphen, too short) that does NOT match the advanced-trace.v1 grammar.  Because
    # the server mints the authoritative branch_id itself, the attempt must still
    # complete cleanly (the earlier bug: echoing the client's value failed the trace
    # schema on every completed attempt, settling it "failed").
    transport = ScriptedTransport([_delta("ok"), _COMPLETED])
    with _client(transport) as client:
        conversation_id = _create_conversation(client)
        parent = _create_parent_turn(client, conversation_id)
        response = _run(client, conversation_id, parent, branch_id="advbranch-1")
        assert response.status_code == 200, response.text
        terminal = _frames(response.text)[-1]
        assert terminal["outcome"] == "completed"  # not "failed"
        assert re.fullmatch(r"advbranch_[a-zA-Z0-9_-]{8,128}", terminal["branch_id"])
        assert terminal["branch_id"] != "advbranch-1"
        assert terminal["trace"]["status"] == "complete"


def test_trace_carries_reported_token_usage_and_latency():
    # Serving reports token usage on the completion event; the trace (and thus the
    # comparison) must carry those real counts + a wall-clock latency, not zeros.
    completed_with_usage = {
        "type": "response.completed",
        "response": {"id": "resp_1", "usage": {"input_tokens": 12, "output_tokens": 7, "total_tokens": 19}},
    }
    transport = ScriptedTransport([_delta("hello there"), completed_with_usage])
    with _client(transport) as client:
        conversation_id = _create_conversation(client)
        parent = _create_parent_turn(client, conversation_id)
        response = _run(client, conversation_id, parent)
        assert response.status_code == 200, response.text
        terminal = _frames(response.text)[-1]
        assert terminal["outcome"] == "completed"
        usage = terminal["trace"]["usage"]
        assert usage["input_tokens"] == 12 and usage["output_tokens"] == 7
        # Wall-clock latency is stamped (a non-negative int); no longer a fixed zero.
        assert isinstance(usage["latency_ms"], int) and usage["latency_ms"] >= 0


def test_no_url_or_token_appears_in_any_streamed_byte():
    transport = ScriptedTransport([_delta("secretless"), _COMPLETED])
    with _client(transport) as client:
        conversation_id = _create_conversation(client)
        parent = _create_parent_turn(client, conversation_id)
        response = _run(client, conversation_id, parent)
        assert response.status_code == 200, response.text
        blob = response.text
        assert ROUTER_HOST not in blob and ROUTER_TOKEN not in blob
        assert "://" not in blob
        assert "bearer" not in blob.lower()


# --- Serving failure -> serving_unavailable terminal, durable failed ----------


def test_serving_failure_settles_serving_unavailable_and_durable_failed():
    transport = ScriptedTransport([_delta("part")], raise_at=1, error=ServingStreamUnavailable("503"))
    with _client(transport) as client:
        conversation_id = _create_conversation(client)
        parent = _create_parent_turn(client, conversation_id)
        response = _run(client, conversation_id, parent)
        assert response.status_code == 200, response.text
        frames = _frames(response.text)
        assert frames[0] == {"seq": 1, "kind": "delta", "text": "part"}
        terminal = frames[-1]
        assert terminal["kind"] == "terminal" and terminal["outcome"] == "serving_unavailable"
        assert terminal["trace"]["status"] == "serving_unavailable"

        assistant = _assistants(client, conversation_id)[0]
        assert assistant["status"] == "failed"
        assert assistant["committed"] is False


# --- mid-stream client disconnect -> cancelled, never a later completion -------


def _free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class _ThreadedServer(uvicorn.Server):
    def install_signal_handlers(self) -> None:  # never off the main thread
        pass


@contextlib.contextmanager
def _running(app, port):
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = _ThreadedServer(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(500):
            if server.started:
                break
            time.sleep(0.02)
        assert server.started, "uvicorn did not start"
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_client_disconnect_before_terminal_settles_cancelled_not_complete():
    # A REAL mid-stream disconnect against a REAL uvicorn server (TestClient buffers
    # the streamed body and never delivers a mid-stream disconnect).  The relay had
    # NOT reached a terminal (BlockingTransport blocks after the first delta), so the
    # server MUST settle the advanced sibling cancelled -- never complete.
    transport = BlockingTransport(["the communal nature of baking"])
    app, _store, lifecycle = _build_app(transport=transport)
    port = _free_port()
    with _running(app, port):
        base = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=base) as client:
            conversation_id = client.post(
                "/api/conversations", json={"title": "x"}, headers=_actor(OWNER),
            ).json()["id"]
            parent = client.post(
                f"/api/conversations/{conversation_id}/turns",
                json={
                    "role": "user", "status": "complete",
                    "lineage": {"parent_turn_id": None, "sibling_index": 0, "kind": "initial"},
                    "content": [{"kind": "text", "text": "explain forking"}],
                },
                headers=_actor(OWNER),
            ).json()["id"]

        body = json.dumps(_run_body(parent)).encode()
        request = (
            f"POST /api/conversations/{conversation_id}/advanced/run HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"X-Workbench-Actor: {OWNER}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode() + body
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        sock.sendall(request)
        buffer = b""
        deadline = time.time() + 10
        while b'"kind":"delta"' not in buffer and time.time() < deadline:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buffer += chunk
        assert b'"kind":"delta"' in buffer, buffer
        # No terminal was ever emitted on the wire before the disconnect.
        assert b'"kind":"terminal"' not in buffer
        sock.close()  # DISCONNECT before any terminal frame

        with httpx.Client(base_url=base) as client:
            assistant = None
            deadline = time.time() + 10
            while time.time() < deadline:
                turns = client.get(
                    f"/api/conversations/{conversation_id}", headers=_actor(OWNER),
                ).json()["turns"]
                found = [t for t in turns if t["role"] == "assistant"]
                if found and found[0]["status"] != "streaming":
                    assistant = found[0]
                    break
                time.sleep(0.05)

    assert assistant is not None, "the disconnect settle path never settled the turn"
    assert assistant["status"] == "cancelled"
    assert assistant["status"] != "complete" and assistant["committed"] is False
    assert transport.closed is True  # the cancel tore the transport down
    # Invariant: after a disconnect-settled run, NO lifecycle record is left
    # in_progress -- a system sweep for interrupted records finds nothing. Two
    # paths jointly guarantee this: the handler's disconnect ``finally`` settles
    # the lifecycle explicitly, and the abandoned worker thread (unblocked by the
    # cancel) runs the generator's own tail settle; this pins that at least one
    # fired, so a reconnecting client can never resync to a stale in_progress.
    assert lifecycle.recover_interrupted() == ()


# --- fail-closed: unknown conversation, invalid control, unset config, actor --


def test_unknown_or_foreign_conversation_is_the_fixed_404():
    transport = ScriptedTransport([_delta("x"), _COMPLETED])
    with _client(transport) as client:
        conversation_id = _create_conversation(client, actor=OWNER)
        parent = _create_parent_turn(client, conversation_id, actor=OWNER)
        foreign = _run(client, conversation_id, parent, actor=OTHER)
        missing = _run(client, "conv_does_not_exist_1234", "turn_missing_0001", actor=OTHER)
        assert foreign.status_code == missing.status_code == 404
        assert foreign.json() == missing.json() == {"detail": "unknown conversation"}
        # No Serving request and no durable advanced sibling written.
        assert transport.opened is False
        assert _assistants(client, conversation_id, actor=OWNER) == []


def test_out_of_bounds_control_is_refused_422_with_no_transport_call():
    transport = ScriptedTransport([_delta("x"), _COMPLETED])
    with _client(transport) as client:
        conversation_id = _create_conversation(client)
        parent = _create_parent_turn(client, conversation_id)
        response = _run(
            client, conversation_id, parent,
            controls=[{"name": "temperature_milli", "value": 999_999, "provenance": "declared"}],
        )
        assert response.status_code == 422
        assert response.json()["detail"] == "advanced route selection is not allowed"
        assert transport.opened is False
        # Fail-closed before any durable write: no advanced sibling appended.
        assert _assistants(client, conversation_id) == []


def test_unknown_route_is_refused_422():
    transport = ScriptedTransport([_delta("x"), _COMPLETED])
    with _client(transport) as client:
        conversation_id = _create_conversation(client)
        parent = _create_parent_turn(client, conversation_id)
        response = _run(client, conversation_id, parent, route_id="route.not-declared")
        assert response.status_code == 422
        assert transport.opened is False


def test_unset_advanced_routes_config_fails_closed():
    # An unset WORKBENCH_ADVANCED_ROUTES is the honest empty allowlist, so ANY route
    # id is unknown and the selection is refused typed (422) -- never a Serving call.
    transport = ScriptedTransport([_delta("x"), _COMPLETED])
    with _client(transport, advanced_routes="") as client:
        conversation_id = _create_conversation(client)
        parent = _create_parent_turn(client, conversation_id)
        response = _run(client, conversation_id, parent)
        assert response.status_code == 422
        assert transport.opened is False


def test_persistence_unconfigured_refuses_503():
    app = create_app(
        settings=_settings(chat_content_hash_key=""), store=MemoryStore(), graph=NullGraph(),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/conversations/whatever/advanced/run",
            json=_run_body("turn_x_0001"), headers=_actor(OWNER),
        )
        assert response.status_code == 503


def test_actor_gating_requires_trusted_identity_401():
    transport = ScriptedTransport([_delta("x"), _COMPLETED])
    with _client(transport) as client:
        response = client.post(
            "/api/conversations/whatever/advanced/run", json=_run_body("turn_x_0001"),
        )  # no identity header
        assert response.status_code == 401


# --- focused runtime test: stream_advanced_attempt parity with run_advanced ---


def test_stream_advanced_attempt_yields_deltas_and_returns_settled_result():
    discovered = discover_advanced_routes([_ROUTE_CONFIG])
    selection = validate_advanced_selection("route.chat-fast", {"temperature_milli": 300}, discovered)
    actor = ConversationActor("operator")
    events = [_delta("Hel"), _delta("lo"), _COMPLETED]

    # Drive the STREAMING variant: collect each yielded delta, capture the returned
    # AdvancedTurnResult from StopIteration.value.
    stream_lifecycle = MemoryResponseLifecycleStore()
    gen = stream_advanced_attempt(
        selection=selection, prompt="hello advanced", transport=ScriptedTransport(list(events)),
        branch_id="advbranch_runtime_0001", conversation_id="conv_advanced_playground_0001", turn_id="turn_assistant_0002",
        lifecycle_store=stream_lifecycle, actor=actor, request_id="req_stream_1",
    )
    yielded: list[RelayEvent] = []
    try:
        while True:
            yielded.append(next(gen))
    except StopIteration as stop:
        streamed_result = stop.value

    assert [e.kind for e in yielded] == ["delta", "delta"]  # only deltas are yielded live
    assert [e.text for e in yielded] == ["Hel", "lo"]
    assert streamed_result.state is AdvancedState.streamed
    assert streamed_result.partial_text == "Hello"

    # The BLOCKING reference settles the SAME state/trace for the same scripted stream.
    blocking_result = run_advanced_stream(
        selection=selection, prompt="hello advanced", transport=ScriptedTransport(list(events)),
        branch_id="advbranch_runtime_0001", conversation_id="conv_advanced_playground_0001", turn_id="turn_assistant_0002",
        lifecycle_store=MemoryResponseLifecycleStore(), actor=actor, request_id="req_block_1",
    )
    assert streamed_result.state is blocking_result.state
    assert streamed_result.turn_status == blocking_result.turn_status
    assert streamed_result.streamed == blocking_result.streamed
    assert streamed_result.partial_text == blocking_result.partial_text
    assert streamed_result.trace["status"] == blocking_result.trace["status"]

    # The streaming variant also drove the SAME durable lifecycle heartbeat/terminal
    # as the blocking one: two in_progress deltas then a completed terminal.
    states = [row.state for row in stream_lifecycle.rows.responses.values()]
    assert states == ["completed"]


def test_stream_advanced_attempt_serving_failure_returns_failed_state():
    discovered = discover_advanced_routes([_ROUTE_CONFIG])
    selection = validate_advanced_selection("route.chat-fast", {"temperature_milli": 300}, discovered)
    actor = ConversationActor("operator")
    gen = stream_advanced_attempt(
        selection=selection, prompt="hello advanced",
        transport=ScriptedTransport([_delta("part")], raise_at=1, error=ServingStreamUnavailable("503")),
        branch_id="advbranch_runtime_0001", conversation_id="conv_advanced_playground_0001", turn_id="turn_assistant_0002",
        lifecycle_store=MemoryResponseLifecycleStore(), actor=actor, request_id="req_fail_1",
    )
    yielded = []
    try:
        while True:
            yielded.append(next(gen))
    except StopIteration as stop:
        result = stop.value
    assert [e.text for e in yielded] == ["part"]
    assert result.state is AdvancedState.serving_unavailable
    assert result.turn_status == "failed"
