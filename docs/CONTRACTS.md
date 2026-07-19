# Integration contracts

This document defines the boundaries Workbench relies on. These are product contracts, not suggestions for alternate implementation paths.

## Anvil State contract

### Authority

Anvil State remains the only authority for PRDs, task dependencies, claims, submitted evidence, acceptance, and canonical task lifecycle. Workbench stores references to State task and event identifiers; it does not mirror or recreate task transition logic.

### Bridge inputs

The project-local bridge requires two explicitly configured, project-specific inputs:

| Input | Required behavior |
| --- | --- |
| State work-packet command | Runs in the project worktree and returns one JSON work packet for `{task_id}`. |
| Canonical event stream | An append-only JSONL event file that the bridge tails from a local cursor. |

The bridge may read the event stream and execute the work-packet command. It must not open, copy, remotely mount, or directly modify `state.db`.

### State writes

State evidence and acceptance remain State CLI operations executed locally by the bridge. An apply command is configured separately and can run only after an approved `merge_and_accept` action has merged successfully. A failed State apply after merge is a visible reconciliation failure.

## Anvil Serving contract

### Model policy

Every Workbench-managed model call goes through a configured Anvil Serving endpoint. Workbench never adds a raw OpenAI, Anthropic, or other provider fallback. The browser never receives the router token.

### Responses compatibility

The first harness is Codex. The bridge configures Codex with an Anvil model provider using `wire_api = "responses"` and the configured router base URL. The required supported path is the stateless `/v1/responses` subset: input, instructions, function tools, function-call output continuation, normal responses, and SSE streaming. Unsupported Responses features must fail explicitly at Anvil Serving rather than silently falling back.

### Correlation

For each Codex run the bridge includes these static request headers in the local provider configuration:

| Header | Meaning |
| --- | --- |
| `X-Anvil-Workbench-Run-Id` | Workbench run identifier. |
| `X-Anvil-Task-Id` | State task identifier, when the work packet carries one. |
| `X-Request-Id` | Per-run request correlation identifier. |

Anvil Serving records the correlation in route and evaluation evidence. The headers are correlation metadata, not credentials or a policy override.

### Retrieval

When configured, Workbench retrieval calls Anvil Serving’s existing embeddings and reranking purpose routes. Redaction occurs before retrieval. If local retrieval is unavailable, graph retrieval may use redacted keyword and lineage material only; no external provider fallback is allowed.

## Hub and browser contract

The hub is a private service behind an identity-aware tailnet proxy. The proxy must strip any user-provided copy of `WORKBENCH_IDENTITY_HEADER` and inject its authenticated identity. The API allowlists the configured owner and approvers.

`WORKBENCH_ALLOW_INSECURE_DEV_ACTOR=true` is a loopback-only local-development escape hatch. It lets the configured owner use the API without a proxy and must remain disabled in a deployed hub.

The browser receives redacted run activity and approval metadata only. It does not receive database passwords, bridge bootstrap tokens after their one-time registration response, router tokens, model provider keys, or GitHub credentials.

## Bridge and GitHub action contract

The bridge makes an outbound authenticated request to the hub and is the only process with access to the project worktree and local GitHub authentication. Its runner contract is intentionally small:

1. Poll for a queued `run_codex` or approved action.
2. Read the State work packet, run Codex locally, and stream redacted activity/evidence.
3. For `commit_pr`, recalculate the repository diff hash and consume the matching approval exactly once before committing, pushing, and creating a PR.
4. For `merge_and_accept`, verify required GitHub checks, merge first, then invoke the configured State acceptance command.

An approval is one-time, expires, is scoped to a bridge, and binds the canonical JSON payload hash. Any changed diff or replayed grant fails closed.

## Neo4j projection contract

Neo4j is a deterministic, idempotent projection keyed by source kind, source identifier, and source hash. It may contain State event metadata, work-packet metadata, Serving route/evaluation metadata, PR references, approvals, and redacted evidence artifacts.

It must not contain raw transcripts or message arrays, accept generic Cypher from agents, execute writes on behalf of agents, or return an approval decision. Graph output is evidence and recommendation context only.
