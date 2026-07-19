"""Narrow server-side Anvil Serving reads and sandbox requests.

The browser never receives the router token.  This module intentionally talks
only to an operator-configured Anvil Serving URL; it has no provider fallback.
"""
from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .redaction import redact_value


class RouterError(RuntimeError):
    """Anvil Serving could not complete a bounded Workbench request."""


def _request(base_url: str, token: str, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    if not base_url or not token:
        raise RouterError("Anvil Serving route access is not configured")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    body = None
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(base_url.rstrip("/") + path, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:  # nosec B310: operator-configured tailnet router
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RouterError(f"Anvil Serving rejected the request ({exc.code}): {detail}") from exc
    except URLError as exc:
        raise RouterError(f"Anvil Serving is unreachable: {exc.reason}") from exc
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise RouterError("Anvil Serving returned an invalid JSON response") from exc


def route_decisions(base_url: str, token: str, limit: int = 50) -> list[dict[str, Any]]:
    """Return only useful correlation metadata from the router decision log."""
    value = _request(base_url, token, "GET", f"/decisions?limit={max(1, min(limit, 100))}")
    rows = value.get("records", value.get("decisions", value)) if isinstance(value, dict) else value
    if not isinstance(rows, list):
        raise RouterError("Anvil Serving decisions response has an unexpected shape")
    allowed = {
        "request_id", "workbench_run_id", "task_id", "model", "served_model", "route",
        "tier", "profile", "reason", "created_at", "timestamp", "status", "fallback",
        "intent", "work_class", "served_tier", "fell_back",
    }
    return [{key: redact_value(row[key]) for key in allowed if key in row} for row in rows if isinstance(row, dict)]


def sandbox_response(base_url: str, token: str, model: str, text: str) -> dict[str, Any]:
    """Use the Responses contract through Serving with deliberately small limits."""
    response = _request(base_url, token, "POST", "/responses", {
        "model": model,
        "input": text,
        "max_output_tokens": 400,
        "stream": False,
    })
    if not isinstance(response, dict):
        raise RouterError("Anvil Serving Responses result has an unexpected shape")
    output_text = response.get("output_text")
    if not isinstance(output_text, str):
        output_text = ""
    if not output_text:
        fragments: list[str] = []
        output = response.get("output", [])
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content", [])
                if not isinstance(content, list):
                    continue
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                        fragments.append(part["text"])
        output_text = "\n".join(fragments)
    return {
        "id": str(response.get("id", ""))[:200],
        "model": str(response.get("model", model))[:240],
        "status": str(response.get("status", "completed"))[:80],
        "output_text": redact_value(output_text[:12_000]),
    }
