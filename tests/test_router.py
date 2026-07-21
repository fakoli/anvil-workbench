from workbench import router


def test_route_decisions_accepts_the_serving_safe_records_summary(monkeypatch):
    monkeypatch.setattr(router, "_request", lambda *_args: {
        "records": [{
            "intent": "planning", "served_tier": "heavy-local", "workbench_run_id": "run_1",
            "task_id": "TASK-1", "request_id": "request_1", "prompt": "must not leave Serving",
        }],
    })

    rows = router.route_decisions("http://127.0.0.1:8000/v1", "server-held")

    assert rows == [{
        "intent": "planning", "served_tier": "heavy-local", "workbench_run_id": "run_1",
        "task_id": "TASK-1", "request_id": "request_1",
    }]


def test_sandbox_response_extracts_standard_responses_output_text(monkeypatch):
    monkeypatch.setattr(router, "_request", lambda *_args: {
        "id": "resp_1", "model": "chat-fast", "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "SANDBOX_OK"}]}],
    })

    response = router.sandbox_response("http://127.0.0.1:8000/v1", "server-held", "chat-fast", "hello")

    assert response["output_text"] == "SANDBOX_OK"


# =========================================================================== #
# reviewed-tools-plugins T010: the first-party conversation-search tool, proven
# through the REAL wired router create_app builds. Scoped to the actor by
# construction; delimited untrusted results + a typed receipt; fail-closed 503.
# =========================================================================== #

import json as _t010r_json


def _t010r_app(with_service: bool = True):
    from workbench.api import create_app
    from workbench.config import Settings
    from workbench.conversation_models import ConversationActor, RetentionPolicy
    from workbench.conversation_store import ConversationSearchService, MemoryConversationStore
    from workbench.graph import NullGraph
    from workbench.store import MemoryStore

    settings = Settings(
        database_url="unused", neo4j_uri="unused", neo4j_user="neo4j", neo4j_password="",
        owner="operator", approvers=frozenset({"operator"}), bridge_bootstrap_token="",
        anvil_router_base_url="", anvil_router_token="",
        identity_header="X-Workbench-Actor", allow_insecure_dev_actor=True,
    )
    service = None
    if with_service:
        store = MemoryConversationStore(content_hash_key=b"r" * 32)
        store.create_conversation(
            ConversationActor(actor_id="operator"),
            RetentionPolicy(policy_id="p", transcript_text="retained_redacted",
                            voice_transcript_text="retained_redacted", delete_after=None),
            title="release checklist",
        )
        service = ConversationSearchService(store)
    return create_app(settings=settings, store=MemoryStore(), graph=NullGraph(),
                      conversation_search_service=service)


def test_t010_wired_conversation_search_returns_delimited_results_and_receipt():
    from fastapi.testclient import TestClient

    with TestClient(_t010r_app()) as client:
        r = client.get("/api/conversation-search?query=release", headers={"X-Workbench-Actor": "operator"})
    assert r.status_code == 200
    body = r.json()
    assert body["content_trust"] == "untrusted_task_data"
    assert body["delimited"] is True
    assert isinstance(body["payload_json"], str)
    assert body["result_count"] == 1
    assert "release checklist" in body["payload_json"]
    assert body["receipt"]["status"] == "succeeded"
    assert body["receipt"]["operation"]["provider"] == "workbench-conversation-search"


def test_t010_wired_conversation_search_fails_closed_without_service():
    from fastapi.testclient import TestClient

    with TestClient(_t010r_app(with_service=False)) as client:
        r = client.get("/api/conversation-search?query=x", headers={"X-Workbench-Actor": "operator"})
    assert r.status_code == 503
