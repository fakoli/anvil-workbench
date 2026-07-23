"""Wired-path live send/stream join (chat-first-voice T010 live join).

These tests drive the PRODUCTION endpoint ``POST /api/conversations/{id}/send``
through a FastAPI ``TestClient`` (the real router, actor dependency, conversation
store, response-lifecycle machinery, and chat relay), injecting only a scripted
Serving transport over the SAME ``ServingStreamTransport`` contract the hermetic
relay qualifies (``tests/test_chat_runtime_integration.py``).  No socket is
opened and no HTTP client is reached: a Serving failure is a scripted transport
exception, exactly as the relay's injected transport models it.

Every assertion is on VALUES, not shapes.  The frame shapes emitted match
``web/src/chat-api.js`` / ``web/src/Chat.integration.test.jsx`` field-for-field:
``{seq, kind:'delta', text}`` and ``{seq, kind:'terminal', outcome}``.  The
router SSE-parser tests (``stream_responses``) run at the bytes level over a
monkeypatched ``urlopen``.
"""
from __future__ import annotations

import contextlib
import io
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from workbench import router as router_module
from workbench.api import create_app
from workbench.chat_stream import ServingStreamUnavailable
from workbench.config import Settings
from workbench.conversation_api import HISTORY_MAX_TURNS, MAX_CONCURRENT_STREAMS_PER_ACTOR
from workbench.conversation_store import MemoryConversationStore
from workbench.graph import NullGraph
from workbench.response_lifecycle_store import MemoryResponseLifecycleStore
from workbench.router import RouterError, ServingResponsesTransport, stream_responses
from workbench.store import MemoryStore

KEY = "api-test-content-hash-key-32byte"
OWNER = "operator"
OTHER = "reviewer"

#: A router base host that MUST NEVER appear in any response byte (no leak).
ROUTER_HOST = "serving.example.ts.net"
ROUTER_BASE_URL = f"https://{ROUTER_HOST}"
ROUTER_TOKEN = "test-router-token-value"

CHAT_ROUTES = json.dumps([
    {
        "route_id": "chat.heavy",
        "display_name": "Heavy chat",
        "serving_contract_version": "1.2.0",
        "route_digest": "sha256:" + "b" * 64,
        "model_profile": "chat-heavy",
        "controls": ["temperature_milli", "max_output_tokens", "reasoning_effort"],
    },
])


def _delta(text: str) -> dict:
    return {"type": "response.output_text.delta", "delta": text}


_COMPLETED = {"type": "response.completed", "response": {"id": "resp_1"}}


class ScriptedTransport:
    """Injected Serving stream: scripted SSE events or a Serving failure.

    Records the request it was opened with (so a test can assert the production
    seam: model == the route's ``model_profile``, ``stream: True``, and the D1
    conversation history), whether ``open`` was called (to prove an invalid
    selection is refused BEFORE any Serving request), and honours the cancellation
    token / a scripted failure or a mid-stream cancel.  ``sink`` collects the
    request across a factory that builds a fresh transport per send.
    """

    def __init__(self, events, *, raise_at=None, error=None, cancel_after=None, sink=None):
        self._events = list(events)
        self._raise_at = raise_at
        self._error = error
        self._cancel_after = cancel_after
        self._sink = sink
        self.opened = False
        self.closed = False
        self.request = None

    def open(self, request, cancel):
        self.opened = True
        self.request = request
        if self._sink is not None:
            self._sink.append(request)

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
    this transport observes to stop -- so the durable turn settles cancelled with
    the partial text, never a fabricated completion.
    """

    def __init__(self, deltas):
        self._deltas = list(deltas)
        self.opened = False
        self.closed = False
        self.request = None

    def open(self, request, cancel):
        self.opened = True
        self.request = request

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


class HeldTransport:
    """Yield one delta then hold the stream open until a shared release Event fires
    (or the caller cancels).  Used to hold concurrent streams open so the per-actor
    concurrency ceiling can be exercised deterministically."""

    def __init__(self, release: threading.Event):
        self._release = release

    def open(self, request, cancel):
        release = self._release

        def _gen():
            yield _delta("held")
            while not release.is_set() and not cancel.cancelled:
                time.sleep(0.01)

        return _gen()


class HistoryFactory:
    """A transport factory that completes each send with a per-call reply and
    records every request the relay assembled (for the D1 history assertions)."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.requests: list[dict] = []
        self.calls = 0

    def __call__(self, _selection):
        reply = self._replies[min(self.calls, len(self._replies) - 1)]
        self.calls += 1
        return ScriptedTransport([_delta(reply), _COMPLETED], sink=self.requests)


def _settings(**overrides) -> Settings:
    values = dict(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner=OWNER, approvers=frozenset({OWNER, OTHER}), bridge_bootstrap_token="",
        anvil_router_base_url=ROUTER_BASE_URL, anvil_router_token=ROUTER_TOKEN,
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=False,
        chat_content_hash_key=KEY, chat_routes=CHAT_ROUTES,
    )
    values.update(overrides)
    return Settings(**values)


def _build_app(transport=None, factory=None, **overrides):
    """Build (app, conversation_store, lifecycle_store) with an injected transport."""
    conv_store = MemoryConversationStore(content_hash_key=KEY.encode("utf-8"))
    lifecycle = MemoryResponseLifecycleStore(recover_on_open=True)
    if factory is not None:
        tf = factory
    elif transport is not None:
        tf = lambda _selection: transport  # noqa: E731 - a one-liner injection
    else:
        tf = None
    app = create_app(
        settings=_settings(**overrides), store=MemoryStore(), graph=NullGraph(),
        conversation_store=conv_store, response_lifecycle_store=lifecycle,
        chat_stream_transport_factory=tf,
    )
    return app, conv_store, lifecycle


def _build(transport=None, factory=None, **overrides):
    """Build (TestClient, conversation_store, lifecycle_store)."""
    app, conv_store, lifecycle = _build_app(transport=transport, factory=factory, **overrides)
    return TestClient(app), conv_store, lifecycle


def _client(transport=None, **overrides) -> TestClient:
    client, _store, _lifecycle = _build(transport=transport, **overrides)
    return client


def _actor(name: str) -> dict[str, str]:
    return {"X-Workbench-Actor": name}


def _create_conversation(client: TestClient, actor: str = OWNER) -> str:
    response = client.post("/api/conversations", json={"title": "Send test"}, headers=_actor(actor))
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _frames(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _send(client: TestClient, conversation_id: str, prompt: str, actor: str = OWNER, controls=None):
    return client.post(
        f"/api/conversations/{conversation_id}/send",
        json={"route_id": "chat.heavy", "prompt": prompt, "controls": controls or {}},
        headers=_actor(actor),
    )


def _turns(client: TestClient, conversation_id: str, actor: str = OWNER) -> list[dict]:
    response = client.get(f"/api/conversations/{conversation_id}", headers=_actor(actor))
    assert response.status_code == 200, response.text
    return response.json()["turns"]


def _content_text(turn: dict) -> str:
    return "".join(block["text"] for block in turn["content"])


def _assistants(client, conversation_id, actor=OWNER):
    return [t for t in _turns(client, conversation_id, actor) if t["role"] == "assistant"]


def _free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class _ThreadedServer(uvicorn.Server):
    def install_signal_handlers(self) -> None:  # never install signal handlers off the main thread
        pass


@contextlib.contextmanager
def _running(app, port):
    """Run ``app`` on a REAL uvicorn server in a background thread.

    A real server (not the buffering TestClient / ASGITransport) is required to
    faithfully model a client disconnect: uvicorn delivers ``http.disconnect`` on
    the receive channel, which Starlette's ``StreamingResponse`` listens for and
    which cancels the streaming body -- the exact production teardown path.
    """
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


def _stream_socket_until_delta(port: int, conversation_id: str, prompt: str, timeout: float = 10.0):
    """Open a raw streaming POST /send socket and read until the first delta frame,
    leaving the connection open (holding its concurrency slot)."""
    body = json.dumps({"route_id": "chat.heavy", "prompt": prompt, "controls": {}}).encode()
    request = (
        f"POST /api/conversations/{conversation_id}/send HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"X-Workbench-Actor: {OWNER}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode() + body
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    sock.sendall(request)
    sock.settimeout(timeout)
    buffer = b""
    deadline = time.time() + timeout
    while b'"kind":"delta"' not in buffer and time.time() < deadline:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buffer += chunk
    assert b'"kind":"delta"' in buffer, buffer
    return sock


# --- happy path: streamed send, contiguous seqs, exact durable text, seam -----


def test_happy_path_streams_frames_persists_exact_text_and_pins_the_request():
    transport = ScriptedTransport([_delta("Hel"), _delta("lo"), _COMPLETED])
    with _client(transport) as client:
        conversation_id = _create_conversation(client)
        response = _send(client, conversation_id, "plan the demo", controls={"max_output_tokens": 256})
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("application/x-ndjson")

        frames = _frames(response.text)
        assert [f["kind"] for f in frames] == ["delta", "delta", "terminal"]
        assert frames[0]["text"] == "Hel" and frames[1]["text"] == "lo"
        assert frames[2]["outcome"] == "completed"
        assert [f["seq"] for f in frames] == [1, 2, 3]

        # G3: the request assembled at the production seam pins the route's Serving
        # model_profile and streaming, and the input is the message list (D1).
        assert transport.request["model"] == "chat-heavy"
        assert transport.request["stream"] is True
        assert transport.request["input"] == [{"role": "user", "content": "plan the demo"}]

        turns = _turns(client, conversation_id)
        user = next(t for t in turns if t["role"] == "user")
        assistant = next(t for t in turns if t["role"] == "assistant")
        assert _content_text(user) == "plan the demo"
        assert _content_text(assistant) == "Hello"  # EXACT concat of the streamed deltas
        assert assistant["status"] == "complete" and assistant["committed"] is True
        assert transport.closed is True


# --- D1: bounded conversation history reaches the Serving request -------------


def test_followup_send_carries_prior_user_and_assistant_turns_in_order():
    factory = HistoryFactory(["first answer", "second answer"])
    client, _store, _lifecycle = _build(factory=factory)
    with client:
        conversation_id = _create_conversation(client)
        assert _send(client, conversation_id, "first prompt").status_code == 200
        assert _send(client, conversation_id, "second prompt").status_code == 200

    # Turn 2's request carries turn 1's user AND assistant text, in order, then the
    # current prompt last -- the missing-history defect (D1) is closed.
    second_request = factory.requests[-1]
    assert second_request["input"] == [
        {"role": "user", "content": "first prompt"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second prompt"},
    ]


def test_history_excludes_failed_turns_from_the_next_request():
    """A failed assistant turn never re-enters the model's context (D1 exclusion)."""

    class FailSecondFactory:
        def __init__(self):
            self.requests: list[dict] = []
            self.calls = 0

        def __call__(self, _selection):
            self.calls += 1
            if self.calls == 2:
                return ScriptedTransport(
                    [_delta("bad partial")], raise_at=1,
                    error=ServingStreamUnavailable("503"), sink=self.requests,
                )
            reply = "first answer" if self.calls == 1 else "third answer"
            return ScriptedTransport([_delta(reply), _COMPLETED], sink=self.requests)

    factory = FailSecondFactory()
    client, _store, _lifecycle = _build(factory=factory)
    with client:
        conversation_id = _create_conversation(client)
        assert _send(client, conversation_id, "first prompt").status_code == 200
        assert _send(client, conversation_id, "second prompt").status_code == 200
        assert _send(client, conversation_id, "third prompt").status_code == 200
        turns = _turns(client, conversation_id)

    # The second exchange's assistant turn durably failed.
    assert [t["status"] for t in turns if t["role"] == "assistant"] == [
        "complete", "failed", "complete",
    ]
    third_input = factory.requests[-1]["input"]
    contents = [message["content"] for message in third_input]
    # The failed assistant text is excluded; its (complete) user turn and the
    # first full exchange remain, in order, with the current prompt last.
    assert "bad partial" not in contents
    assert contents == ["first prompt", "first answer", "second prompt", "third prompt"]


def test_history_is_bounded_and_drops_the_oldest_turns():
    # Six sends -> 5 prior complete pairs (10 turns) before the 6th; the bound keeps
    # only the last HISTORY_MAX_TURNS, dropping the oldest.
    assert HISTORY_MAX_TURNS == 8
    replies = [f"answer-{n}" for n in range(1, 7)]
    factory = HistoryFactory(replies)
    client, _store, _lifecycle = _build(factory=factory)
    with client:
        conversation_id = _create_conversation(client)
        for n in range(1, 7):
            assert _send(client, conversation_id, f"prompt-{n}").status_code == 200

    last_input = factory.requests[-1]["input"]
    contents = [message["content"] for message in last_input]
    # Exactly 8 history messages + the current prompt.
    assert len(last_input) == HISTORY_MAX_TURNS + 1
    # The two oldest turns were dropped; the recent ones survive, in order, current last.
    assert "prompt-1" not in contents and "answer-1" not in contents
    assert contents[0] == "prompt-2" and contents[1] == "answer-2"
    assert last_input[-1] == {"role": "user", "content": "prompt-6"}


# --- D2: each stream's emitted seq starts at 1 --------------------------------


def test_each_stream_restarts_seq_at_one():
    factory = HistoryFactory(["one", "two"])
    client, _store, _lifecycle = _build(factory=factory)
    with client:
        conversation_id = _create_conversation(client)
        first = _send(client, conversation_id, "first")
        second = _send(client, conversation_id, "second")
    # The FE reducer resets per send; the SECOND stream's first frame must be seq 1
    # (a cumulative seq would arrive as 3 and read as a dropped-frame gap -- D2).
    assert _frames(first.text)[0]["seq"] == 1
    assert _frames(second.text)[0]["seq"] == 1
    assert [f["seq"] for f in _frames(second.text)] == [1, 2]


# --- D3: linear turns serialize as plain (no BRANCH chip) ---------------------


def test_linear_turns_carry_no_branch_lineage():
    factory = HistoryFactory(["a", "b"])
    client, _store, _lifecycle = _build(factory=factory)
    with client:
        conversation_id = _create_conversation(client)
        _send(client, conversation_id, "one")
        _send(client, conversation_id, "two")
        turns = _turns(client, conversation_id)
    # Four linear turns; the FE renders a lineage chip only when kind != 'initial'.
    assert len(turns) == 4
    assert all(turn["lineage"]["kind"] == "initial" for turn in turns)
    assert not any(turn["lineage"]["kind"] in ("branch", "retry") for turn in turns)


# --- D4 / G1: a real client disconnect settles cancelled, never complete ------


def test_client_disconnect_before_terminal_settles_cancelled_not_complete():
    # A REAL mid-stream disconnect: a raw socket reads the first frame then closes,
    # against a REAL uvicorn server (TestClient / httpx.ASGITransport buffer streamed
    # bodies and never deliver a mid-stream disconnect).  The relay had NOT reached a
    # terminal (BlockingTransport blocks after the first delta), so the server MUST
    # settle the turn cancelled with the partial text -- never complete (D4/G1).
    partial = "the communal nature of baking became evident in temple complexes that housed"
    transport = BlockingTransport([partial])
    app, _store, lifecycle = _build_app(transport=transport)
    port = _free_port()
    with _running(app, port):
        base = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=base) as client:
            conversation_id = client.post(
                "/api/conversations", json={"title": "x"}, headers=_actor(OWNER),
            ).json()["id"]

        # Raw HTTP/1.1 POST; read until the first streamed delta frame, then CLOSE
        # the socket mid-stream (the browser Cancel button aborting the fetch).
        body = json.dumps({"route_id": "chat.heavy", "prompt": "history of bread", "controls": {}}).encode()
        request = (
            f"POST /api/conversations/{conversation_id}/send HTTP/1.1\r\n"
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
        sock.close()  # DISCONNECT before any terminal frame

        with httpx.Client(base_url=base) as client:
            assistant = None
            deadline = time.time() + 10
            while time.time() < deadline:
                turns = client.get(
                    f"/api/conversations/{conversation_id}", headers=_actor(OWNER),
                ).json()["turns"]
                found = [t for t in turns if t["role"] == "assistant"]
                if found:
                    assistant = found[0]
                    break
                time.sleep(0.05)

    assert assistant is not None, "the disconnect settle path never persisted a turn"
    # The partial text is preserved and settled CANCELLED -- never complete (D4).
    assert assistant["status"] == "cancelled"
    assert assistant["status"] != "complete" and assistant["committed"] is False
    assert _content_text(assistant) == partial
    # The transport observed the cancel and tore down (G1 teardown chain).
    assert transport.closed is True
    # The durable lifecycle terminal is cancelled, not completed.
    states = [row.state for row in lifecycle.rows.responses.values()]
    assert states and all(state == "cancelled" for state in states)


# --- fail-closed selection: typed refusal, no write, no transport call --------


def test_unknown_route_is_refused_typed_with_no_write_and_no_transport_call():
    transport = ScriptedTransport([_delta("x"), _COMPLETED])
    with _client(transport) as client:
        conversation_id = _create_conversation(client)
        response = client.post(
            f"/api/conversations/{conversation_id}/send",
            json={"route_id": "chat.unknown", "prompt": "hi", "controls": {}},
            headers=_actor(OWNER),
        )
        assert response.status_code == 422
        assert response.json()["detail"] == "chat route selection is not allowed"
        assert _turns(client, conversation_id) == []
        assert transport.opened is False


def test_undeclared_control_is_refused_typed_with_no_write_and_no_transport_call():
    transport = ScriptedTransport([_delta("x"), _COMPLETED])
    with _client(transport) as client:
        conversation_id = _create_conversation(client)
        response = client.post(
            f"/api/conversations/{conversation_id}/send",
            json={"route_id": "chat.heavy", "prompt": "hi", "controls": {"temperature_milli": 999_999}},
            headers=_actor(OWNER),
        )
        assert response.status_code == 422
        assert response.json()["detail"] == "chat route selection is not allowed"
        assert _turns(client, conversation_id) == []
        assert transport.opened is False


# --- mid-stream cancel (transport-observed) → cancelled, durably recorded -----


def test_mid_stream_cancel_settles_cancelled_and_persists_partial_text():
    transport = ScriptedTransport([_delta("par"), _delta("tial"), _COMPLETED], cancel_after=2)
    with _client(transport) as client:
        conversation_id = _create_conversation(client)
        response = _send(client, conversation_id, "summarize this")
        assert response.status_code == 200, response.text
        frames = _frames(response.text)
        assert frames[-1]["kind"] == "terminal" and frames[-1]["outcome"] == "cancelled"

        assistant = _assistants(client, conversation_id)[0]
        assert _content_text(assistant) == "partial"
        assert assistant["status"] == "cancelled" and assistant["committed"] is False
        assert transport.closed is True


# --- Serving failure → serving_unavailable terminal, no leak ------------------


def test_serving_failure_settles_serving_unavailable_and_leaks_no_url_or_token():
    transport = ScriptedTransport([_delta("part")], raise_at=1, error=ServingStreamUnavailable("503"))
    with _client(transport) as client:
        conversation_id = _create_conversation(client)
        response = _send(client, conversation_id, "hi")
        assert response.status_code == 200, response.text
        frames = _frames(response.text)
        assert frames[0] == {"seq": 1, "kind": "delta", "text": "part"}
        assert frames[-1]["kind"] == "terminal" and frames[-1]["outcome"] == "serving_unavailable"

        assistant_turns = _assistants(client, conversation_id)
        assert len(assistant_turns) == 1
        assistant = assistant_turns[0]
        assert _content_text(assistant) == "part"
        assert assistant["status"] == "failed"
        assert assistant["interrupted"] is False and assistant["committed"] is False

        for blob in (response.text, response.headers.get("x-error", "")):
            assert ROUTER_HOST not in blob and ROUTER_TOKEN not in blob
            assert "bearer" not in blob.lower()


# --- G6: a completion over one ContentBlock's bound persists intact -----------


def test_long_completion_over_the_block_bound_persists_one_intact_turn():
    # 25k chars > MAX_CONTENT_TEXT_CHARS (20k); streamed as five 5k deltas. It must
    # persist as ONE assistant turn whose joined content is the FULL text, and the
    # terminal frame must be delivered -- never a completed lifecycle with 0 turns.
    long_text = "x" * 25_000
    deltas = [_delta("x" * 5_000) for _ in range(5)]
    transport = ScriptedTransport(deltas + [_COMPLETED])
    with _client(transport) as client:
        conversation_id = _create_conversation(client)
        response = _send(client, conversation_id, "write a long answer")
        assert response.status_code == 200, response.text
        frames = _frames(response.text)
        assert frames[-1]["kind"] == "terminal" and frames[-1]["outcome"] == "completed"

        assistant_turns = _assistants(client, conversation_id)
        assert len(assistant_turns) == 1
        assistant = assistant_turns[0]
        assert _content_text(assistant) == long_text  # full text, chunked across blocks
        assert len(assistant["content"]) >= 2  # split into multiple ContentBlocks
        assert assistant["status"] == "complete"


# --- G7: per-actor concurrent-stream ceiling ---------------------------------


def test_concurrent_stream_ceiling_refuses_beyond_the_per_actor_bound():
    # Hold MAX_CONCURRENT_STREAMS_PER_ACTOR real streaming sockets open concurrently
    # (real uvicorn), then the next send for the same actor is refused 429 -- the
    # per-actor ceiling is enforced (G7).
    release = threading.Event()
    app, _store, _lifecycle = _build_app(factory=lambda _selection: HeldTransport(release))
    port = _free_port()
    with _running(app, port):
        base = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=base) as client:
            conversation_id = client.post(
                "/api/conversations", json={"title": "x"}, headers=_actor(OWNER),
            ).json()["id"]

        held = []
        try:
            for _ in range(MAX_CONCURRENT_STREAMS_PER_ACTOR):
                held.append(_stream_socket_until_delta(port, conversation_id, "hold"))

            with httpx.Client(base_url=base) as client:
                refused = client.post(
                    f"/api/conversations/{conversation_id}/send",
                    json={"route_id": "chat.heavy", "prompt": "one too many", "controls": {}},
                    headers=_actor(OWNER),
                )
            assert refused.status_code == 429
            assert refused.json()["detail"] == "too many concurrent chat streams are active for this actor"
            # The ceiling is acquired BEFORE the user-turn write: a refused send
            # leaves no orphan user turn (kills the acquire-after-write mutation).
            with httpx.Client(base_url=base) as client:
                snapshot = client.get(
                    f"/api/conversations/{conversation_id}", headers=_actor(OWNER),
                ).json()
            refused_texts = [
                block.get("text")
                for turn in snapshot.get("turns", [])
                for block in turn.get("content", [])
            ]
            assert "one too many" not in refused_texts
        finally:
            release.set()  # let the held streams complete so their slots free
            for sock in held:
                with contextlib.suppress(OSError):
                    sock.close()


# --- cross-actor send → the absent-record contract (no existence oracle) ------


def test_cross_actor_send_is_the_fixed_absent_record_body():
    transport = ScriptedTransport([_delta("x"), _COMPLETED])
    with _client(transport) as client:
        conversation_id = _create_conversation(client, actor=OWNER)
        foreign = client.post(
            f"/api/conversations/{conversation_id}/send",
            json={"route_id": "chat.heavy", "prompt": "hi", "controls": {}},
            headers=_actor(OTHER),
        )
        missing = client.post(
            "/api/conversations/conv_does_not_exist_1234/send",
            json={"route_id": "chat.heavy", "prompt": "hi", "controls": {}},
            headers=_actor(OTHER),
        )
        assert foreign.status_code == missing.status_code == 404
        assert foreign.json() == missing.json() == {"detail": "unknown conversation"}
        assert transport.opened is False
        assert _turns(client, conversation_id, actor=OWNER) == []


def test_send_refuses_503_when_chat_persistence_is_not_configured():
    app = create_app(
        settings=_settings(chat_content_hash_key=""), store=MemoryStore(), graph=NullGraph(),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/conversations/whatever/send",
            json={"route_id": "chat.heavy", "prompt": "hi", "controls": {}},
            headers=_actor(OWNER),
        )
        assert response.status_code == 503


# --- G2: router SSE parser (stream_responses / ServingResponsesTransport) -----


class _FakeCancel:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


def _patch_urlopen(monkeypatch, body: bytes):
    captured = {}

    def _fake(req, timeout=None):
        captured["timeout"] = timeout
        captured["url"] = req.full_url
        captured["body"] = req.data
        return io.BytesIO(body)

    monkeypatch.setattr(router_module, "urlopen", _fake)
    return captured


def _collect(body: bytes):
    return list(stream_responses(ROUTER_BASE_URL, ROUTER_TOKEN, {"model": "m", "stream": True}, _FakeCancel()))


def test_stream_responses_accumulates_multiline_data_and_terminates_on_done(monkeypatch):
    body = (
        b"event: response.output_text.delta\r\n"
        b"data: {\"type\":\"response.output_text.delta\",\r\n"
        b"data:  \"delta\":\"hi\"}\r\n"
        b"\r\n"
        b": keepalive\r\n"
        b"data: {\"type\":\"response.completed\"}\r\n"
        b"\r\n"
        b"data: [DONE]\r\n"
        b"\r\n"
    )
    _patch_urlopen(monkeypatch, body)
    events = _collect(body)
    # The split JSON is accumulated and parsed once (not dropped); keepalive and
    # event: lines are ignored; [DONE] terminates after the completed event.
    assert events == [
        {"type": "response.output_text.delta", "delta": "hi"},
        {"type": "response.completed"},
    ]


def test_stream_responses_flushes_a_trailing_event_without_a_final_blank_line(monkeypatch):
    body = b"data: {\"type\":\"response.completed\"}\n"
    _patch_urlopen(monkeypatch, body)
    assert _collect(body) == [{"type": "response.completed"}]


def test_stream_responses_ignores_non_json_and_non_object_data(monkeypatch):
    body = b"data: not-json\n\ndata: [1,2,3]\n\ndata: {\"type\":\"ok\"}\n\n"
    _patch_urlopen(monkeypatch, body)
    assert _collect(body) == [{"type": "ok"}]


def test_stream_responses_bounds_an_oversized_line(monkeypatch):
    monkeypatch.setattr(router_module, "_MAX_SSE_EVENT_BYTES", 40)
    body = b"data: " + b"z" * 200  # one long, newline-free line over the ceiling
    _patch_urlopen(monkeypatch, body)
    with pytest.raises(RouterError):
        _collect(body)


def test_stream_responses_maps_a_read_deadline_to_timeout(monkeypatch):
    class _TimeoutResponse:
        def readline(self, _limit=-1):
            raise TimeoutError("read timed out")

        def close(self):
            pass

    monkeypatch.setattr(router_module, "urlopen", lambda req, timeout=None: _TimeoutResponse())
    with pytest.raises(TimeoutError):
        _collect(b"")


def test_stream_responses_not_configured_raises_router_error():
    with pytest.raises(RouterError):
        list(stream_responses("", "", {}, _FakeCancel()))


def test_stream_responses_strips_internal_fields_serving_rejects(monkeypatch):
    # The hub's bounded request carries ``route_id`` (an internal correlation
    # field); Serving's /v1/responses fail-closes an unknown field with a 400
    # ``unsupported_feature``, rejecting the whole request.  The transport must
    # project the outbound body to the Serving-supported allowlist so every
    # supported field survives and no internal field ever reaches the wire.
    body = b"data: {\"type\":\"response.completed\"}\n\ndata: [DONE]\n\n"
    captured = _patch_urlopen(monkeypatch, body)
    request = {
        "model": "chat",
        "route_id": "route.chat",  # internal-only; Serving rejects it
        "input": "hi",
        "stream": True,
        "max_output_tokens": 16,
        "temperature": 0.4,
        "reasoning": {"effort": "low"},
    }
    events = list(stream_responses(ROUTER_BASE_URL, ROUTER_TOKEN, request, _FakeCancel()))
    assert events == [{"type": "response.completed"}]
    sent = json.loads(captured["body"].decode("utf-8"))
    assert "route_id" not in sent  # would 400 every turn if leaked
    assert sent == {
        "model": "chat",
        "input": "hi",
        "stream": True,
        "max_output_tokens": 16,
        "temperature": 0.4,
        "reasoning": {"effort": "low"},
    }


def test_serving_responses_transport_relays_parsed_events(monkeypatch):
    body = b"data: {\"type\":\"response.output_text.delta\",\"delta\":\"yo\"}\n\ndata: [DONE]\n\n"
    _patch_urlopen(monkeypatch, body)
    transport = ServingResponsesTransport(ROUTER_BASE_URL, ROUTER_TOKEN)
    events = list(transport.open({"model": "m", "stream": True}, _FakeCancel()))
    assert events == [{"type": "response.output_text.delta", "delta": "yo"}]
