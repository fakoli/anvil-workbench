"""Wired-path live send/stream join (chat-first-voice T010 live join).

These tests drive the PRODUCTION endpoint ``POST /api/conversations/{id}/send``
through a FastAPI ``TestClient`` (the real router, actor dependency, conversation
store, response-lifecycle machinery, and chat relay), injecting only a scripted
Serving transport over the SAME ``ServingStreamTransport`` contract the hermetic
relay qualifies (``tests/test_chat_runtime_integration.py``).  No socket is
opened and no HTTP client is reached: a Serving failure is a scripted transport
exception, exactly as the relay's injected transport models it.

Every assertion is on VALUES, not shapes: streamed frames parse as RelayEvent
dicts with contiguous seqs, and the durable turns match the streamed text
EXACTLY.  The frame shapes emitted match ``web/src/chat-api.js`` /
``web/src/Chat.integration.test.jsx`` field-for-field: ``{seq, kind:'delta',
text}`` and ``{seq, kind:'terminal', outcome}``.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from workbench.api import create_app
from workbench.chat_stream import ServingStreamUnavailable
from workbench.config import Settings
from workbench.conversation_store import MemoryConversationStore
from workbench.graph import NullGraph
from workbench.store import MemoryStore

KEY = "api-test-content-hash-key-32byte"
OWNER = "operator"
OTHER = "reviewer"

#: A router base host that MUST NEVER appear in any response byte (no leak).
ROUTER_HOST = "serving.example.ts.net"
ROUTER_BASE_URL = f"https://{ROUTER_HOST}"
ROUTER_TOKEN = "test-router-token-value"

#: The operator-reviewed chat route allowlist (chat_routes.discover_chat_routes).
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

    Mirrors the hermetic qualification transport: records whether ``open`` was
    called (to prove an invalid selection is refused BEFORE any Serving request)
    and honours the cancellation token / a scripted failure or mid-stream cancel.
    """

    def __init__(self, events, *, raise_at=None, error=None, cancel_after=None):
        self._events = list(events)
        self._raise_at = raise_at
        self._error = error
        self._cancel_after = cancel_after
        self.opened = False
        self.closed = False

    def open(self, request, cancel):
        self.opened = True

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


def _client(transport: ScriptedTransport | None = None, **overrides) -> TestClient:
    factory = (lambda _selection: transport) if transport is not None else None
    app = create_app(
        settings=_settings(**overrides), store=MemoryStore(), graph=NullGraph(),
        conversation_store=MemoryConversationStore(content_hash_key=KEY.encode("utf-8")),
        chat_stream_transport_factory=factory,
    )
    return TestClient(app)


def _actor(name: str) -> dict[str, str]:
    return {"X-Workbench-Actor": name}


def _create_conversation(client: TestClient, actor: str = OWNER) -> str:
    response = client.post("/api/conversations", json={"title": "Send test"}, headers=_actor(actor))
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _frames(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _turns(client: TestClient, conversation_id: str, actor: str = OWNER) -> list[dict]:
    response = client.get(f"/api/conversations/{conversation_id}", headers=_actor(actor))
    assert response.status_code == 200, response.text
    return response.json()["turns"]


def _content_text(turn: dict) -> str:
    return "".join(block["text"] for block in turn["content"])


# --- happy path: streamed send, contiguous seqs, exact durable text ----------


def test_happy_path_streams_frames_and_persists_exact_text():
    transport = ScriptedTransport([_delta("Hel"), _delta("lo"), _COMPLETED])
    with _client(transport) as client:
        conversation_id = _create_conversation(client)
        response = client.post(
            f"/api/conversations/{conversation_id}/send",
            json={"route_id": "chat.heavy", "route_selection": "explicit",
                  "prompt": "plan the demo", "controls": {"max_output_tokens": 256}},
            headers=_actor(OWNER),
        )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("application/x-ndjson")

        frames = _frames(response.text)
        # Two deltas then exactly one terminal; every frame parses as a RelayEvent dict.
        assert [f["kind"] for f in frames] == ["delta", "delta", "terminal"]
        assert frames[0]["text"] == "Hel" and frames[1]["text"] == "lo"
        assert frames[2]["outcome"] == "completed"
        # Seqs are strictly monotonic and contiguous across the whole stream.
        assert [f["seq"] for f in frames] == [1, 2, 3]

        # Durable turns: the user prompt and the assistant text EXACTLY as streamed.
        turns = _turns(client, conversation_id)
        user = next(t for t in turns if t["role"] == "user")
        assistant = next(t for t in turns if t["role"] == "assistant")
        assert _content_text(user) == "plan the demo"
        assert user["status"] == "complete"
        assert _content_text(assistant) == "Hello"  # EXACT concat of the streamed deltas
        assert assistant["status"] == "complete"
        assert assistant["committed"] is True
        assert transport.closed is True  # the upstream request was torn down


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
        # No durable turn was written and the Serving transport was never opened.
        assert _turns(client, conversation_id) == []
        assert transport.opened is False


def test_undeclared_control_is_refused_typed_with_no_write_and_no_transport_call():
    transport = ScriptedTransport([_delta("x"), _COMPLETED])
    with _client(transport) as client:
        conversation_id = _create_conversation(client)
        response = client.post(
            f"/api/conversations/{conversation_id}/send",
            # temperature_milli is declared by the route but 999_999 is out of range.
            json={"route_id": "chat.heavy", "prompt": "hi", "controls": {"temperature_milli": 999_999}},
            headers=_actor(OWNER),
        )
        assert response.status_code == 422
        assert response.json()["detail"] == "chat route selection is not allowed"
        assert _turns(client, conversation_id) == []
        assert transport.opened is False


# --- mid-stream client cancel → cancelled outcome, durably recorded ----------


def test_mid_stream_cancel_settles_cancelled_and_persists_partial_text():
    # cancel_after=2 trips the cancel on the queued completion, exactly as a browser
    # cancel mid-stream would: two deltas relay, then the stream settles cancelled
    # and the completion is never honoured.
    transport = ScriptedTransport([_delta("par"), _delta("tial"), _COMPLETED], cancel_after=2)
    with _client(transport) as client:
        conversation_id = _create_conversation(client)
        response = client.post(
            f"/api/conversations/{conversation_id}/send",
            json={"route_id": "chat.heavy", "prompt": "summarize this", "controls": {}},
            headers=_actor(OWNER),
        )
        assert response.status_code == 200, response.text
        frames = _frames(response.text)
        assert frames[-1]["kind"] == "terminal"
        assert frames[-1]["outcome"] == "cancelled"  # never "completed"

        assistant = next(t for t in _turns(client, conversation_id) if t["role"] == "assistant")
        # The partial text actually relayed is preserved, and the turn is cancelled.
        assert _content_text(assistant) == "partial"
        assert assistant["status"] == "cancelled"
        assert assistant["committed"] is False
        assert transport.closed is True


# --- Serving failure → serving_unavailable terminal, no leak ------------------


def test_serving_failure_settles_serving_unavailable_and_leaks_no_url_or_token():
    transport = ScriptedTransport([_delta("part")], raise_at=1, error=ServingStreamUnavailable("503"))
    with _client(transport) as client:
        conversation_id = _create_conversation(client)
        response = client.post(
            f"/api/conversations/{conversation_id}/send",
            json={"route_id": "chat.heavy", "prompt": "hi", "controls": {}},
            headers=_actor(OWNER),
        )
        assert response.status_code == 200, response.text
        frames = _frames(response.text)
        assert frames[0] == {"seq": 1, "kind": "delta", "text": "part"}
        assert frames[-1]["kind"] == "terminal"
        assert frames[-1]["outcome"] == "serving_unavailable"  # typed, no provider fallback

        # No partial assistant-turn corruption: exactly one assistant turn, failed.
        assistant_turns = [t for t in _turns(client, conversation_id) if t["role"] == "assistant"]
        assert len(assistant_turns) == 1
        assistant = assistant_turns[0]
        assert _content_text(assistant) == "part"
        assert assistant["status"] == "failed"
        assert assistant["interrupted"] is False and assistant["committed"] is False

        # The router base host and token never ride out on ANY response byte.
        for blob in (response.text, response.headers.get("x-error", "")):
            assert ROUTER_HOST not in blob
            assert ROUTER_TOKEN not in blob
            assert "bearer" not in blob.lower()


# --- cross-actor send → the absent-record contract (no existence oracle) ------


def test_cross_actor_send_is_the_fixed_absent_record_body():
    transport = ScriptedTransport([_delta("x"), _COMPLETED])
    with _client(transport) as client:
        conversation_id = _create_conversation(client, actor=OWNER)
        # A foreign actor's send is byte-identical to a genuinely missing id.
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
        # The refusal is strictly upstream of any Serving request or write: the
        # transport was never opened and the owner's conversation gained no turn.
        assert transport.opened is False
        assert _turns(client, conversation_id, actor=OWNER) == []


# --- 503 when chat persistence / routes are not configured --------------------


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
