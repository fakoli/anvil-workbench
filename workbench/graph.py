"""Narrow, read-optimized Neo4j evidence projection.

The projector receives only redacted delivery metadata.  Neo4j is deliberately
not a workflow authority: it cannot claim tasks, issue approvals, write State,
or execute bridge actions.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

from .redaction import redact_value
from .retrieval import RetrievalClient, RetrievalError


class GraphError(RuntimeError):
    """A caller tried to place an unsafe artifact in the evidence graph."""


def projection_id(source_kind: str, source_id: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"source_kind": source_kind, "source_id": source_id, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class EvidenceGraph(Protocol):
    def project(self, source_kind: str, source_id: str, project_id: str, payload: dict[str, Any]) -> str: ...
    def evidence_search(self, project_id: str, query: str, limit: int = 8) -> list[dict[str, Any]]: ...
    def task_lineage(self, task_id: str) -> list[dict[str, Any]]: ...
    def related_failures(self, fingerprint: str, limit: int = 8) -> list[dict[str, Any]]: ...


def _safe_payload(source_kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    if source_kind in {"transcript", "raw_transcript"}:
        raise GraphError("raw transcripts are never indexed in Neo4j")
    clean = redact_value(payload)
    if "transcript" in clean or "messages" in clean:
        raise GraphError("graph projection accepts evidence metadata, not transcripts")
    return clean


@dataclass
class NullGraph:
    """A no-op graph used by unit tests and deployments with graph disabled."""

    def project(self, source_kind: str, source_id: str, project_id: str, payload: dict[str, Any]) -> str:
        return projection_id(source_kind, source_id, _safe_payload(source_kind, payload))

    def evidence_search(self, project_id: str, query: str, limit: int = 8) -> list[dict[str, Any]]:
        return []

    def task_lineage(self, task_id: str) -> list[dict[str, Any]]:
        return []

    def related_failures(self, fingerprint: str, limit: int = 8) -> list[dict[str, Any]]:
        return []


class Neo4jEvidenceGraph:
    """Deterministic, fixed-query graph access; it intentionally exposes no Cypher."""

    def __init__(self, uri: str, username: str, password: str, database: str = "neo4j", retrieval: RetrievalClient | None = None) -> None:
        try:
            from neo4j import GraphDatabase
        except ImportError as exc:  # pragma: no cover - dependency contract
            raise RuntimeError("neo4j is required when WORKBENCH_NEO4J_URI is configured") from exc
        self._driver = GraphDatabase.driver(uri, auth=(username, password))
        self._database = database
        self._retrieval = retrieval

    def close(self) -> None:
        self._driver.close()

    def project(self, source_kind: str, source_id: str, project_id: str, payload: dict[str, Any]) -> str:
        clean = _safe_payload(source_kind, payload)
        event_id = projection_id(source_kind, source_id, clean)
        compact = json.dumps(clean, sort_keys=True, ensure_ascii=True)
        embedding = None
        if self._retrieval is not None:
            try:
                embedding = self._retrieval.embed(compact)
            except RetrievalError:
                # Evidence remains queryable by graph/keyword path if the local
                # embedding serve is temporarily unavailable; no provider fallback.
                embedding = None
        if embedding is not None:
            try:
                with self._driver.session(database=self._database) as session:
                    session.run(
                        "CREATE VECTOR INDEX workbench_evidence_embedding IF NOT EXISTS FOR (event:EvidenceEvent) ON (event.embedding) OPTIONS {indexConfig: {'vector.dimensions': $dimensions, 'vector.similarity_function': 'cosine'}}",
                        dimensions=len(embedding),
                    ).consume()
            except Exception:
                # Older Neo4j editions may lack vector indexes; graph lineage and
                # keyword evidence search remain available without one.
                pass
        query = """
        MERGE (project:Project {id: $project_id})
        MERGE (event:EvidenceEvent {projection_id: $projection_id})
        ON CREATE SET event.source_kind = $source_kind, event.source_id = $source_id,
                      event.payload = $payload, event.search_text = $search_text,
                      event.embedding = $embedding
        MERGE (project)-[:HAS_EVIDENCE]->(event)
        WITH event
        FOREACH (task_id IN CASE WHEN $task_id IS NULL THEN [] ELSE [$task_id] END |
          MERGE (task:Task {id: task_id})
          MERGE (task)-[:HAS_EVIDENCE]->(event)
        )
        FOREACH (pr_url IN CASE WHEN $pr_url IS NULL THEN [] ELSE [$pr_url] END |
          MERGE (pr:PullRequest {url: pr_url})
          MERGE (event)-[:SUPPORTS]->(pr)
        )
        """
        with self._driver.session(database=self._database) as session:
            session.run(
                query,
                project_id=project_id,
                projection_id=event_id,
                source_kind=source_kind,
                source_id=source_id,
                payload=compact,
                search_text=compact[:12_000],
                embedding=embedding,
                task_id=clean.get("task_id"),
                pr_url=clean.get("pr_url"),
            ).consume()
        return event_id

    def evidence_search(self, project_id: str, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Fixed vector+graph retrieval with keyword fallback and optional rerank."""
        if self._retrieval is not None:
            try:
                embedding = self._retrieval.embed(query)
                statement = """
                CALL db.index.vector.queryNodes('workbench_evidence_embedding', $limit, $embedding)
                YIELD node AS event, score
                MATCH (:Project {id: $project_id})-[:HAS_EVIDENCE]->(event)
                RETURN event.source_kind AS source_kind, event.source_id AS source_id,
                       event.payload AS payload, event.projection_id AS citation, score
                """
                with self._driver.session(database=self._database) as session:
                    rows = [dict(record) for record in session.run(statement, project_id=project_id, embedding=embedding, limit=limit)]
                documents = [str(row.get("payload", "")) for row in rows]
                order = self._retrieval.rerank(query, documents)
                return [rows[index] for index in order if index < len(rows)]
            except Exception:
                pass
        statement = """
        MATCH (:Project {id: $project_id})-[:HAS_EVIDENCE]->(event:EvidenceEvent)
        WHERE toLower(event.search_text) CONTAINS toLower($query)
        RETURN event.source_kind AS source_kind, event.source_id AS source_id,
               event.payload AS payload, event.projection_id AS citation
        LIMIT $limit
        """
        with self._driver.session(database=self._database) as session:
            return [dict(record) for record in session.run(statement, project_id=project_id, query=query, limit=limit)]

    def task_lineage(self, task_id: str) -> list[dict[str, Any]]:
        statement = """
        MATCH (task:Task {id: $task_id})-[:HAS_EVIDENCE]->(event:EvidenceEvent)
        OPTIONAL MATCH (event)-[:SUPPORTS]->(pr:PullRequest)
        RETURN event.source_kind AS source_kind, event.source_id AS source_id,
               event.projection_id AS citation, pr.url AS pr_url
        ORDER BY event.source_kind, event.source_id
        """
        with self._driver.session(database=self._database) as session:
            return [dict(record) for record in session.run(statement, task_id=task_id)]

    def related_failures(self, fingerprint: str, limit: int = 8) -> list[dict[str, Any]]:
        statement = """
        MATCH (event:EvidenceEvent)
        WHERE event.source_kind = 'failure' AND toLower(event.search_text) CONTAINS toLower($fingerprint)
        RETURN event.source_id AS source_id, event.payload AS payload, event.projection_id AS citation
        LIMIT $limit
        """
        with self._driver.session(database=self._database) as session:
            return [dict(record) for record in session.run(statement, fingerprint=fingerprint, limit=limit)]
