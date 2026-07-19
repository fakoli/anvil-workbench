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
