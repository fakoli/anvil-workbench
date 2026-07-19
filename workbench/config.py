"""Environment-only Workbench configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _csv(name: str, env: dict[str, str]) -> frozenset[str]:
    return frozenset(item.strip() for item in env.get(name, "").split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    database_url: str
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    owner: str
    approvers: frozenset[str]
    bridge_bootstrap_token: str
    anvil_router_base_url: str
    anvil_router_token: str
    anvil_voice_realtime_url: str = ""
    anvil_voice_realtime_token: str = ""
    voice_retain_transcripts: bool = False
    embedding_model: str = ""
    rerank_model: str = ""
    identity_header: str = "Tailscale-User-Login"
    allow_insecure_dev_actor: bool = False

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        values = dict(os.environ if env is None else env)
        owner = values.get("WORKBENCH_OWNER", "operator").strip() or "operator"
        approvers = _csv("WORKBENCH_APPROVERS", values) | frozenset({owner})
        return cls(
            database_url=values.get("WORKBENCH_DATABASE_URL", "postgresql://workbench:workbench@db:5432/workbench"),
            neo4j_uri=values.get("WORKBENCH_NEO4J_URI", "bolt://neo4j:7687"),
            neo4j_user=values.get("WORKBENCH_NEO4J_USER", "neo4j"),
            neo4j_password=values.get("WORKBENCH_NEO4J_PASSWORD", ""),
            owner=owner,
            approvers=approvers,
            bridge_bootstrap_token=values.get("WORKBENCH_BRIDGE_BOOTSTRAP_TOKEN", ""),
            anvil_router_base_url=values.get("ANVIL_ROUTER_BASE_URL", ""),
            anvil_router_token=values.get("ANVIL_ROUTER_TOKEN", ""),
            anvil_voice_realtime_url=values.get("ANVIL_VOICE_REALTIME_URL", "").strip(),
            anvil_voice_realtime_token=values.get("ANVIL_VOICE_REALTIME_TOKEN", ""),
            voice_retain_transcripts=values.get("WORKBENCH_VOICE_RETAIN_TRANSCRIPTS", "").lower() in {"1", "true", "yes"},
            embedding_model=values.get("WORKBENCH_EMBEDDING_MODEL", ""),
            rerank_model=values.get("WORKBENCH_RERANK_MODEL", ""),
            identity_header=values.get("WORKBENCH_IDENTITY_HEADER", "Tailscale-User-Login").strip() or "Tailscale-User-Login",
            allow_insecure_dev_actor=values.get("WORKBENCH_ALLOW_INSECURE_DEV_ACTOR", "").lower() in {"1", "true", "yes"},
        )
