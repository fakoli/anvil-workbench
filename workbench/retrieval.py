"""Evidence retrieval through Anvil Serving purpose-model routes only."""
from __future__ import annotations

import json
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class RetrievalError(RuntimeError):
    pass


class RetrievalClient(Protocol):
    def embed(self, text: str) -> list[float]: ...
    def rerank(self, query: str, documents: list[str]) -> list[int]: ...


class AnvilPurposeRetrieval:
    """Small purpose-model client that never calls a provider directly."""

    def __init__(self, base_url: str, token: str, embedding_model: str, rerank_model: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.embedding_model = embedding_model
        self.rerank_model = rerank_model

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"}
        request = Request(self.base_url + path, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            with urlopen(request, timeout=20) as response:  # nosec B310: configured private router only
                value = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, ValueError) as exc:
            raise RetrievalError("Anvil Serving purpose-model request failed") from exc
        if not isinstance(value, dict):
            raise RetrievalError("Anvil Serving returned an invalid purpose-model response")
        return value

    def embed(self, text: str) -> list[float]:
        result = self._post("/embeddings", {"model": self.embedding_model, "input": text})
        try:
            vector = result["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RetrievalError("embedding response did not include data[0].embedding") from exc
        if not isinstance(vector, list) or not all(isinstance(value, (int, float)) for value in vector):
            raise RetrievalError("embedding response was not a numeric vector")
        return [float(value) for value in vector]

    def rerank(self, query: str, documents: list[str]) -> list[int]:
        if not self.rerank_model or not documents:
            return list(range(len(documents)))
        result = self._post("/rerank", {"model": self.rerank_model, "query": query, "documents": documents})
        rows = result.get("results", [])
        if not isinstance(rows, list):
            return list(range(len(documents)))
        indexes = [row.get("index") for row in rows if isinstance(row, dict) and isinstance(row.get("index"), int)]
        return [index for index in indexes if 0 <= index < len(documents)] or list(range(len(documents)))
