"""Wired-path LIVE parallel multi-route dispatch join (advanced-model-playground).

These tests drive the PRODUCTION endpoint ``POST
/api/conversations/{id}/advanced/dispatch`` through a FastAPI ``TestClient`` (the
real router, actor dependency, conversation store, response-lifecycle machinery,
and chat relay), injecting only a scripted Serving transport over the SAME
``ServingStreamTransport`` contract the hermetic relay qualifies.  No socket is
opened by the happy/fail-closed tests; the disconnect test runs a REAL uvicorn
server so a mid-stream socket close settles every branch cancelled.

The dispatch endpoint fans ONE prompt out across N reviewed routes as concurrent
``mode="advanced"`` siblings and MULTIPLEXES their progress onto one NDJSON
stream, each frame tagged by ``branch_id``: an initial ``{kind:'dispatch',
branches:[...]}`` frame, then interleaved per-branch ``{branch_id, seq,
kind:'delta', text}`` frames (each branch's seq starting at 1), then N per-branch
``{branch_id, seq, kind:'terminal', outcome, turn_id, trace}`` frames.  The
existing "Build comparison" then works over the N siblings unchanged.
"""
from __future__ import annotations

import contextlib
import json
import socket
import threading
import time

import httpx
import uvicorn
from fastapi.testclient import TestClient

from workbench.api import create_app
from workbench.chat_stream import ServingStreamUnavailable
from workbench.config import Settings
from workbench.conversation_store import MemoryConversationStore
from workbench.graph import NullGraph
from workbench.response_lifecycle_store import MemoryResponseLifecycleStore
from workbench.store import MemoryStore

KEY = "adv-dispatch-api-test-content-hash"
OWNER = "operator"
OTHER = "reviewer"

#: A router base host/token that MUST NEVER appear in any response byte (no leak).
ROUTER_HOST = "serving.example.ts.net"
ROUTER_BASE_URL = f"https://{ROUTER_HOST}"
ROUTER_TOKEN = "test-router-token-value"

_ROUTE_DIGEST = "sha256:" + "a1" * 32
_PROFILE_DIGEST = "sha256:" + "b2" * 32


def _route_config(route_id: str, model_profile: str) -> dict:
    return {
        "route_id": route_id,
        "display_name": model_profile,
        "route_digest": _ROUTE_DIGEST,
        "profile_digest": _PROFILE_DIGEST,
        "serving_contract_version": "1.0.0",
        "model_profile": model_profile,
        "supported_controls": [
            {"name": "temperature_milli", "type": "int", "bounds": {"min": 0, "max": 2000}, "default": 700},
            {"name": "reasoning_effort", "type": "enum",
             "allowed_values": ["low", "medium", "high"], "default": "medium"},
        ],
    }


_ROUTE_CONFIGS = [
    _route_config("route.chat-fast", "chat-fast"),
    _route_config("route.chat-heavy", "chat-heavy"),
    _route_config("route.chat-mini", "chat-mini"),
]
ADVANCED_ROUTES = json.dumps(_ROUTE_CONFIGS)

_ALL_ROUTE_IDS = ["route.chat-fast", "route.chat-heavy", "route.chat-mini"]


def _delta(text: str) -> dict:
    return {"type": "response.output_text.delta", "delta": text}


def _completed(rid: str = "resp_1") -> dict:
    return {"type": "response.completed", "response": {"id": rid}}


class ScriptedTransport:
    """Injected Serving stream: scripted SSE events or a Serving failure (no network)."""

    def __init__(self, events, *, raise_at=None, error=None):
        self._events = list(events)
        self._raise_at = raise_at
        self._error = error
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
                    yield event
                if self._raise_at is not None and self._raise_at >= len(self._events):
                    raise self._error
            finally:
                self.closed = True

        return _gen()


class BlockingTransport:
    """Yield the scripted deltas, then BLOCK until the caller trips cancel."""

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


class _RouteTransportFactory:
    """Hand each branch a FRESH, independent transport keyed by its route model.

    The production ``chat_stream_transport_factory`` builds a new
    ``ServingResponsesTransport`` per selection; the parallel dispatch relies on
    that independence, so the test factory mints/returns one transport per route
    (never a shared mutable one across branches).
    """

    def __init__(self, by_model: dict[str, object]):
        self._by_model = by_model
        self.calls: list[str] = []

    def __call__(self, selection):
        model = selection.route.model_profile
        self.calls.append(model)
        return self._by_model[model]


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


def _build_app(transport_factory=None, **overrides):
    conv_store = MemoryConversationStore(content_hash_key=KEY.encode("utf-8"))
    lifecycle = MemoryResponseLifecycleStore(recover_on_open=True)
    app = create_app(
        settings=_settings(**overrides), store=MemoryStore(), graph=NullGraph(),
        conversation_store=conv_store, response_lifecycle_store=lifecycle,
        chat_stream_transport_factory=transport_factory,
    )
    return app, conv_store, lifecycle


def _build(transport_factory=None, **overrides):
    app, conv_store, lifecycle = _build_app(transport_factory=transport_factory, **overrides)
    return TestClient(app), conv_store, lifecycle


def _client(transport_factory=None, **overrides) -> TestClient:
    client, _store, _lifecycle = _build(transport_factory=transport_factory, **overrides)
    return client


def _actor(name: str) -> dict[str, str]:
    return {"X-Workbench-Actor": name}


def _create_conversation(client: TestClient, actor: str = OWNER) -> str:
    response = client.post("/api/conversations", json={"title": "Advanced dispatch"}, headers=_actor(actor))
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _create_parent_turn(client: TestClient, conversation_id: str, actor: str = OWNER) -> str:
    response = client.post(
        f"/api/conversations/{conversation_id}/turns",
        json={
            "role": "user", "status": "complete",
            "lineage": {"parent_turn_id": None, "sibling_index": 0, "kind": "initial"},
            "content": [{"kind": "text", "text": "compare these routes"}],
        },
        headers=_actor(actor),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _frames(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _dispatch_body(parent_turn_id: str, route_ids, *, prompt="hello routes", **extra) -> dict:
    body = {
        "parent_turn_id": parent_turn_id,
        "prompt": prompt,
        "routes": [{"route_id": rid, "controls": {}} for rid in route_ids],
    }
    body.update(extra)
    return body


def _dispatch(client, conversation_id, parent_turn_id, route_ids, actor=OWNER, **kw):
    return client.post(
        f"/api/conversations/{conversation_id}/advanced/dispatch",
        json=_dispatch_body(parent_turn_id, route_ids, **kw), headers=_actor(actor),
    )


def _turns(client, conversation_id, actor=OWNER) -> list[dict]:
    response = client.get(f"/api/conversations/{conversation_id}", headers=_actor(actor))
    assert response.status_code == 200, response.text
    return response.json()["turns"]


def _assistants(client, conversation_id, actor=OWNER):
    return [t for t in _turns(client, conversation_id, actor) if t["role"] == "assistant"]


def _scripted_factory(route_ids):
    """A fresh completing transport per route model."""
    by_model = {}
    for rid in route_ids:
        model = rid.split(".", 1)[1]
        by_model[model] = ScriptedTransport([_delta(model[:3]), _delta("!"), _completed()])
    return _RouteTransportFactory(by_model)


# --- happy path: dispatch frame, interleaved per-branch deltas, N terminals ----


def test_happy_three_route_dispatch_streams_dispatch_then_branch_deltas_and_terminals():
    factory = _scripted_factory(_ALL_ROUTE_IDS)
    with _client(factory) as client:
        conversation_id = _create_conversation(client)
        parent = _create_parent_turn(client, conversation_id)
        response = _dispatch(client, conversation_id, parent, _ALL_ROUTE_IDS)
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("application/x-ndjson")

        frames = _frames(response.text)
        # First frame announces the three branches so the FE renders N columns.
        assert frames[0]["kind"] == "dispatch"
        announced = frames[0]["branches"]
        assert len(announced) == 3
        assert {b["route_id"] for b in announced} == set(_ALL_ROUTE_IDS)
        branch_ids = {b["branch_id"] for b in announced}
        assert len(branch_ids) == 3  # distinct branch ids
        for b in announced:
            assert b["branch_id"].startswith("advbranch_")
            assert isinstance(b["turn_id"], str) and b["turn_id"]

        rest = frames[1:]
        # Every non-dispatch frame carries a branch_id in the announced set.
        assert all(f["branch_id"] in branch_ids for f in rest)

        # Per-branch: seq starts at 1 and increases by 1 (deltas then one terminal).
        terminals = {}
        for bid in branch_ids:
            branch_frames = [f for f in rest if f["branch_id"] == bid]
            seqs = [f["seq"] for f in branch_frames]
            assert seqs == list(range(1, len(seqs) + 1)), (bid, seqs)
            assert branch_frames[-1]["kind"] == "terminal"
            assert all(f["kind"] == "delta" for f in branch_frames[:-1])
            terminals[bid] = branch_frames[-1]

        # N terminals, each completed, carrying turn_id + a redacted trace.
        assert len(terminals) == 3
        for terminal in terminals.values():
            assert terminal["outcome"] == "completed"
            assert isinstance(terminal["turn_id"], str) and terminal["turn_id"]
            assert terminal["trace"]["schema_version"] == "workbench-advanced-trace/v1"
            assert terminal["trace"]["status"] == "complete"
            assert terminal["trace"]["branch_ref"]["branch_id"] == terminal["branch_id"]

        # N durable mode="advanced" siblings all settled complete under the parent.
        assistants = _assistants(client, conversation_id)
        assert len(assistants) == 3
        assert all(a["mode"] == "advanced" for a in assistants)
        assert all(a["status"] == "complete" for a in assistants)
        assert all(a["lineage"]["parent_turn_id"] == parent for a in assistants)
        # Each announced turn_id is a real durable sibling.
        durable_ids = {a["id"] for a in assistants}
        assert {b["turn_id"] for b in announced} == durable_ids
        # A fresh independent transport was minted per branch (three model seams).
        assert set(factory.calls) == {"chat-fast", "chat-heavy", "chat-mini"}


def test_two_route_dispatch_is_the_minimum_batch():
    ids = ["route.chat-fast", "route.chat-heavy"]
    factory = _scripted_factory(ids)
    with _client(factory) as client:
        conversation_id = _create_conversation(client)
        parent = _create_parent_turn(client, conversation_id)
        response = _dispatch(client, conversation_id, parent, ids)
        assert response.status_code == 200, response.text
        frames = _frames(response.text)
        assert frames[0]["kind"] == "dispatch" and len(frames[0]["branches"]) == 2
        assert len(_assistants(client, conversation_id)) == 2


def test_no_url_or_token_appears_in_any_streamed_byte():
    factory = _scripted_factory(_ALL_ROUTE_IDS)
    with _client(factory) as client:
        conversation_id = _create_conversation(client)
        parent = _create_parent_turn(client, conversation_id)
        response = _dispatch(client, conversation_id, parent, _ALL_ROUTE_IDS)
        assert response.status_code == 200, response.text
        blob = response.text
        assert ROUTER_HOST not in blob and ROUTER_TOKEN not in blob
        assert "://" not in blob
        assert "bearer" not in blob.lower()


# --- isolation: one failing route settles serving_unavailable, siblings complete


def test_one_failing_route_is_isolated_from_completing_siblings():
    ids = _ALL_ROUTE_IDS
    by_model = {
        "chat-fast": ScriptedTransport([_delta("ok"), _completed()]),
        # The heavy route's Serving stream fails after one delta.
        "chat-heavy": ScriptedTransport([_delta("part")], raise_at=1, error=ServingStreamUnavailable("503")),
        "chat-mini": ScriptedTransport([_delta("ok"), _completed()]),
    }
    factory = _RouteTransportFactory(by_model)
    with _client(factory) as client:
        conversation_id = _create_conversation(client)
        parent = _create_parent_turn(client, conversation_id)
        response = _dispatch(client, conversation_id, parent, ids)
        assert response.status_code == 200, response.text
        frames = _frames(response.text)
        announced = {b["route_id"]: b["branch_id"] for b in frames[0]["branches"]}

        def _terminal(route_id):
            bid = announced[route_id]
            return [f for f in frames if f.get("branch_id") == bid and f["kind"] == "terminal"][0]

        assert _terminal("route.chat-heavy")["outcome"] == "serving_unavailable"
        assert _terminal("route.chat-fast")["outcome"] == "completed"
        assert _terminal("route.chat-mini")["outcome"] == "completed"

        # Durable siblings: the failing branch is failed, the other two complete.
        by_status = sorted(a["status"] for a in _assistants(client, conversation_id))
        assert by_status == ["complete", "complete", "failed"]


# --- fail-closed preflight: nothing forked on refusal --------------------------


def test_invalid_control_refuses_422_with_no_sibling_forked():
    factory = _scripted_factory(_ALL_ROUTE_IDS)
    with _client(factory) as client:
        conversation_id = _create_conversation(client)
        parent = _create_parent_turn(client, conversation_id)
        response = client.post(
            f"/api/conversations/{conversation_id}/advanced/dispatch",
            json={
                "parent_turn_id": parent, "prompt": "x",
                "routes": [
                    {"route_id": "route.chat-fast", "controls": {}},
                    {"route_id": "route.chat-heavy",
                     "controls": [{"name": "temperature_milli", "value": 999_999}]},
                ],
            },
            headers=_actor(OWNER),
        )
        assert response.status_code == 422
        assert response.json()["detail"] == "advanced route selection is not allowed"
        # Preflight fails closed BEFORE any fork: no sibling and no transport opened.
        assert _assistants(client, conversation_id) == []
        assert all(t.opened is False for t in factory._by_model.values())


def test_unknown_route_in_batch_refuses_422_with_no_sibling_forked():
    factory = _scripted_factory(_ALL_ROUTE_IDS)
    with _client(factory) as client:
        conversation_id = _create_conversation(client)
        parent = _create_parent_turn(client, conversation_id)
        response = _dispatch(
            client, conversation_id, parent, ["route.chat-fast", "route.not-declared"],
        )
        assert response.status_code == 422
        assert _assistants(client, conversation_id) == []


def test_duplicate_route_ids_refuses_422():
    factory = _scripted_factory(_ALL_ROUTE_IDS)
    with _client(factory) as client:
        conversation_id = _create_conversation(client)
        parent = _create_parent_turn(client, conversation_id)
        response = _dispatch(
            client, conversation_id, parent, ["route.chat-fast", "route.chat-fast"],
        )
        assert response.status_code == 422
        assert response.json()["detail"] == "advanced dispatch batch declares a duplicate route"
        assert _assistants(client, conversation_id) == []


def test_batch_below_two_routes_is_422():
    factory = _scripted_factory(_ALL_ROUTE_IDS)
    with _client(factory) as client:
        conversation_id = _create_conversation(client)
        parent = _create_parent_turn(client, conversation_id)
        response = _dispatch(client, conversation_id, parent, ["route.chat-fast"])
        assert response.status_code == 422  # pydantic min_length=2
        assert _assistants(client, conversation_id) == []


def test_batch_above_four_routes_is_422():
    factory = _scripted_factory(_ALL_ROUTE_IDS)
    with _client(factory) as client:
        conversation_id = _create_conversation(client)
        parent = _create_parent_turn(client, conversation_id)
        five = _ALL_ROUTE_IDS + ["route.chat-fast", "route.chat-heavy"]
        response = _dispatch(client, conversation_id, parent, five)
        assert response.status_code == 422  # pydantic max_length=4
        assert _assistants(client, conversation_id) == []


def test_unknown_or_foreign_conversation_is_the_fixed_404():
    factory = _scripted_factory(_ALL_ROUTE_IDS)
    with _client(factory) as client:
        conversation_id = _create_conversation(client, actor=OWNER)
        parent = _create_parent_turn(client, conversation_id, actor=OWNER)
        foreign = _dispatch(client, conversation_id, parent, _ALL_ROUTE_IDS, actor=OTHER)
        missing = _dispatch(client, "conv_missing_1234", "turn_missing_0001", _ALL_ROUTE_IDS, actor=OTHER)
        assert foreign.status_code == missing.status_code == 404
        assert foreign.json() == missing.json() == {"detail": "unknown conversation"}
        assert all(t.opened is False for t in factory._by_model.values())
        assert _assistants(client, conversation_id, actor=OWNER) == []


def test_unset_advanced_routes_config_fails_closed_422():
    # An unset WORKBENCH_ADVANCED_ROUTES is the honest empty allowlist, so every
    # route id is unknown and the batch is refused typed (422) -- never a fork.
    factory = _scripted_factory(_ALL_ROUTE_IDS)
    with _client(factory, advanced_routes="") as client:
        conversation_id = _create_conversation(client)
        parent = _create_parent_turn(client, conversation_id)
        response = _dispatch(client, conversation_id, parent, _ALL_ROUTE_IDS)
        assert response.status_code == 422
        assert _assistants(client, conversation_id) == []


def test_persistence_unconfigured_refuses_503():
    app = create_app(
        settings=_settings(chat_content_hash_key=""), store=MemoryStore(), graph=NullGraph(),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/conversations/whatever/advanced/dispatch",
            json=_dispatch_body("turn_x_0001", ["route.chat-fast", "route.chat-heavy"]),
            headers=_actor(OWNER),
        )
        assert response.status_code == 503


def test_actor_gating_requires_trusted_identity_401():
    factory = _scripted_factory(_ALL_ROUTE_IDS)
    with _client(factory) as client:
        response = client.post(
            "/api/conversations/whatever/advanced/dispatch",
            json=_dispatch_body("turn_x_0001", ["route.chat-fast", "route.chat-heavy"]),
        )  # no identity header
        assert response.status_code == 401


# --- one batch holds ONE actor slot (does not consume N) -----------------------


def test_batch_holds_one_slot_and_a_second_batch_is_refused():
    # A dispatch batch fans out N branches internally but consumes exactly ONE
    # per-actor DISPATCH slot; and dispatch concurrency is bounded to
    # MAX_CONCURRENT_DISPATCH_BATCHES_PER_ACTOR (1) so one actor cannot stack
    # batches (each of which already fans to up to MAX_DISPATCH_ROUTES Serving
    # streams). Proof: while a 2-route batch is held open on a raw socket, a SECOND
    # concurrent dispatch batch from the same actor is refused 429. If the first
    # batch wrongly consumed N slots, or the ceiling weren't per-batch, the second
    # would be admitted -- so this pins BOTH the single-slot claim and the bound.
    import workbench.conversation_api as capi

    assert capi.MAX_CONCURRENT_DISPATCH_BATCHES_PER_ACTOR == 1
    dispatch_ids = ["route.chat-fast", "route.chat-heavy"]
    transports = {
        "chat-fast": BlockingTransport(["a"]),
        "chat-heavy": BlockingTransport(["b"]),
    }
    factory = _RouteTransportFactory(transports)

    app, _store, _lifecycle = _build_app(transport_factory=factory)
    port = _free_port()
    with _running(app, port):
        base = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=base, timeout=15) as client:
            conversation_id = client.post(
                "/api/conversations", json={"title": "slots"}, headers=_actor(OWNER),
            ).json()["id"]
            parent = client.post(
                f"/api/conversations/{conversation_id}/turns",
                json={
                    "role": "user", "status": "complete",
                    "lineage": {"parent_turn_id": None, "sibling_index": 0, "kind": "initial"},
                    "content": [{"kind": "text", "text": "seed"}],
                },
                headers=_actor(OWNER),
            ).json()["id"]

        # Open the blocking 2-route dispatch on a raw socket; it holds the one
        # dispatch slot open (its branches block on their transports) until close.
        got = {}

        def _open_dispatch():
            body = json.dumps(_dispatch_body(parent, dispatch_ids)).encode()
            request = (
                f"POST /api/conversations/{conversation_id}/advanced/dispatch HTTP/1.1\r\n"
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
            got["sock"] = sock
            got["live"] = b'"kind":"delta"' in buffer

        thread = threading.Thread(target=_open_dispatch, daemon=True)
        thread.start()
        thread.join(timeout=10)
        assert got.get("live"), "dispatch stream never opened / streamed a branch delta"

        try:
            # A SECOND concurrent dispatch batch from the same actor is refused 429.
            # It reuses the SAME declared routes so it passes preflight (which runs
            # BEFORE the slot check) -- the 429 can therefore only be the slot bound,
            # and its transports are never opened (the batch is refused before fork).
            with httpx.Client(base_url=base, timeout=15) as client:
                second = client.post(
                    f"/api/conversations/{conversation_id}/advanced/dispatch",
                    json=_dispatch_body(parent, dispatch_ids),
                    headers=_actor(OWNER),
                )
                assert second.status_code == 429, second.text
        finally:
            with contextlib.suppress(Exception):
                got["sock"].close()


# --- mid-stream client disconnect -> ALL N cancelled, no later completion ------


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


def test_client_disconnect_settles_all_branches_cancelled_not_complete():
    ids = _ALL_ROUTE_IDS
    transports = {
        "chat-fast": BlockingTransport(["fast delta"]),
        "chat-heavy": BlockingTransport(["heavy delta"]),
        "chat-mini": BlockingTransport(["mini delta"]),
    }
    factory = _RouteTransportFactory(transports)
    app, _store, lifecycle = _build_app(transport_factory=factory)
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
                    "content": [{"kind": "text", "text": "compare"}],
                },
                headers=_actor(OWNER),
            ).json()["id"]

        body = json.dumps(_dispatch_body(parent, ids)).encode()
        request = (
            f"POST /api/conversations/{conversation_id}/advanced/dispatch HTTP/1.1\r\n"
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
        # Wait until at least one branch delta has streamed (branches are live).
        while b'"kind":"delta"' not in buffer and time.time() < deadline:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buffer += chunk
        assert b'"kind":"delta"' in buffer, buffer
        # No branch terminal was emitted on the wire before the disconnect.
        assert b'"kind":"terminal"' not in buffer
        sock.close()  # DISCONNECT mid-stream

        with httpx.Client(base_url=base) as client:
            settled = []
            deadline = time.time() + 10
            while time.time() < deadline:
                turns = client.get(
                    f"/api/conversations/{conversation_id}", headers=_actor(OWNER),
                ).json()["turns"]
                assistants = [t for t in turns if t["role"] == "assistant"]
                if len(assistants) == 3 and all(a["status"] != "streaming" for a in assistants):
                    settled = assistants
                    break
                time.sleep(0.05)

    assert len(settled) == 3, "the disconnect settle path never settled all three siblings"
    assert all(a["status"] == "cancelled" for a in settled)
    assert all(a["committed"] is False for a in settled)
    assert all(t.closed is True for t in transports.values())  # every transport torn down
    # Invariant: after the disconnect-settled batch, NO lifecycle record is left
    # in_progress -- a system sweep for interrupted records finds nothing, so a
    # reconnecting client can never resync to a stale in_progress for any branch.
    assert lifecycle.recover_interrupted() == ()
