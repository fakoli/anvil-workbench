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


def test_route_decisions_surfaces_servings_resolution_metadata(monkeypatch):
    # T010: the SAFE route-resolution fields Serving reports (requested vs served
    # route, selection provenance, episode id) are admitted; a prompt is not.
    monkeypatch.setattr(router, "_request", lambda *_args: {"records": [{
        "request_id": "req_1", "requested_route": "route.fast", "served_route": "route.heavy",
        "route_selection": "explicit", "episode_id": "ep_9", "fell_back": True,
        "divergence_reason": "capacity", "prompt": "must not leave Serving",
    }]})

    rows = router.route_decisions("http://127.0.0.1:8000/v1", "server-held")

    assert "prompt" not in rows[0]
    assert rows[0]["requested_route"] == "route.fast"
    assert rows[0]["served_route"] == "route.heavy"


def test_route_resolution_is_surface_only_and_never_substitutes_a_route():
    # T010 criterion 1 (NO FAILOVER / SURFACE-ONLY): the served route is EXACTLY
    # the one Serving reported. Workbench performs no retry-to-alternate — a
    # divergence surfaces Serving's own served route, never a Workbench-chosen one.
    decision = {
        "request_id": "req_7", "requested_route": "route.fast", "served_route": "route.heavy",
        "route_selection": "explicit", "divergence_reason": "route.fast at capacity",
    }
    resolution = router.route_resolution(decision)
    assert resolution["diverged"] is True
    # Pass-through: the served route is Serving's reported value, byte-for-byte.
    # (A regression that substituted a Workbench-chosen alternate route here would
    #  make this assertion fail — the no-failover revert-detection.)
    assert resolution["served_route"] == decision["served_route"]
    assert resolution["requested_route"] == decision["requested_route"]
    assert resolution["provenance"] == "explicit"
    assert resolution["episode_id"]  # a stable per-episode grouping id


def test_route_resolution_distinguishes_explicit_from_preference_default():
    # T010 criterion 2: explicit vs preference-derived is a real served field, not
    # a guess — an unreported provenance stays None rather than being invented.
    explicit = router.route_resolution({"route": "route.a", "served_route": "route.a", "route_selection": "explicit"})
    defaulted = router.route_resolution({"route": "route.a", "served_route": "route.a", "route_source": "preference_default"})
    unknown = router.route_resolution({"route": "route.a", "served_route": "route.a"})
    assert explicit["provenance"] == "explicit" and explicit["diverged"] is False
    assert defaulted["provenance"] == "preference_default"
    assert unknown["provenance"] is None


def test_route_resolution_shares_one_episode_id_across_a_divergence_episode():
    # T010 criterion 3 (once-per-episode): two turns of the SAME divergence episode
    # (same requested→served/reason) share an episode id, so the browser can show
    # the notice exactly once; a non-diverged turn carries no episode id.
    a = router.route_resolution({"requested_route": "route.fast", "served_route": "route.heavy", "divergence_reason": "capacity"})
    b = router.route_resolution({"requested_route": "route.fast", "served_route": "route.heavy", "divergence_reason": "capacity"})
    settled = router.route_resolution({"requested_route": "route.fast", "served_route": "route.fast"})
    assert a["episode_id"] == b["episode_id"]
    assert settled["diverged"] is False and settled["episode_id"] is None


def test_route_resolution_credential_scrubs_a_divergence_reason():
    # The surfaced reason is credential-scrubbed like every other Serving string.
    resolution = router.route_resolution({
        "requested_route": "route.a", "served_route": "route.b",
        "divergence_reason": "token=sk-ABCDEFGH12345678 exhausted",
    })
    assert "sk-ABCDEFGH12345678" not in resolution["divergence_reason"]
    assert "[REDACTED]" in resolution["divergence_reason"]


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
