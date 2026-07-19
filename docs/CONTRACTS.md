# Integration contracts

This document defines the boundaries Workbench relies on. These are product contracts, not suggestions for alternate implementation paths.

## Anvil State contract

### Authority

Anvil State remains the only authority for PRDs, task dependencies, claims, submitted evidence, acceptance, and canonical task lifecycle. Workbench stores references to State task and event identifiers; it does not mirror or recreate task transition logic.

### Bridge inputs

The project-local bridge requires explicitly configured, project-specific inputs. Its v1 defaults are shown so a new project can validate the exact CLI surface before overriding it:

| Input | Required behavior |
| --- | --- |
| State work-packet command | Runs in the project worktree and returns one JSON work packet for `{task_id}`. |
| Canonical event stream | An append-only JSONL event file that the bridge tails from a local cursor. |
| State status command | `anvil status`; resolves the canonical event path when no explicit event file is supplied. |
| Claim command | `anvil claim {task_id} --actor {actor}`. |
| Work-packet command | `anvil packet {task_id} --format json`; the bridge tolerates its leading `Wrote packet ...` status line. |
| Verification capture | `anvil hook capture-evidence`; receives the bridge-observed exit code and captured stdout/stderr. |
| Evidence submit command | `anvil submit {task_id}` with verification commands and packet-declared changed files. |

The bridge may read the event stream and execute the work-packet command. It must not open, copy, remotely mount, or directly modify `state.db`.

### State writes

The bridge performs `claim -> work packet -> Codex -> independent verification capture -> State evidence submit`. State evidence is not submitted when a verification command fails or no packet-declared file changed. An apply command is configured separately and can run only after an approved `merge_and_accept` action has merged successfully; a standalone `state_apply` command is rejected. A failed State apply after merge is a visible reconciliation failure.

## Anvil Serving contract

### Model policy

Every Workbench-managed model call goes through a configured Anvil Serving endpoint. Workbench never adds a raw OpenAI, Anthropic, or other provider fallback. The browser never receives the router token.

### Responses compatibility

The first harness is Codex. The bridge configures Codex with an Anvil model provider using `wire_api = "responses"` and the configured router base URL. The required supported path is the stateless `/v1/responses` subset: input, instructions, function tools, function-call output continuation, normal responses, and SSE streaming. `store: false`, `include: ["reasoning.encrypted_content"]`, bounded `reasoning`, `parallel_tool_calls: false`, and `truncation: "disabled"` are recognized for Codex compatibility; they never override the Serving profile. Provider-hosted tools, arbitrary namespaces, stateful response chaining, background work, and provider-side storage fail explicitly rather than silently falling back. The bridge suppresses the internal Codex `collaboration` namespace because Workbench does not permit model-delegated agents without a separately reviewed bridge contract.

### Correlation

For each Codex run the bridge includes these static request headers in the local provider configuration:

| Header | Meaning |
| --- | --- |
| `X-Anvil-Workbench-Run-Id` | Workbench run identifier. |
| `X-Anvil-Task-Id` | State task identifier, when the work packet carries one. |
| `X-Request-Id` | Per-run request correlation identifier. |

Anvil Serving records the correlation in route and evaluation evidence. The headers are correlation metadata, not credentials or a policy override.

### Operator sandbox

The optional browser sandbox is a hub-owned, bounded `POST /v1/responses` call to the configured Anvil Serving endpoint. Its model must be present in `WORKBENCH_SANDBOX_MODELS`; the hub holds the router token, limits prompt and output sizes, redacts the returned text, and appends only request/output metadata to the immutable audit. It is not a delivery runner and cannot access bridge tools, State, worktrees, GitHub, approvals, or provider credentials. An unset allowlist makes the control unavailable, with no fallback.

### Retrieval

When configured, Workbench retrieval calls Anvil Serving’s existing embeddings and reranking purpose routes. Redaction occurs before retrieval. If local retrieval is unavailable, graph retrieval may use redacted keyword and lineage material only; no external provider fallback is allowed.

## Hub and browser contract

The hub is a private service behind an identity-aware tailnet proxy. The proxy must strip any user-provided copy of `WORKBENCH_IDENTITY_HEADER` and inject its authenticated identity. The API allowlists the configured owner and approvers.

`WORKBENCH_ALLOW_INSECURE_DEV_ACTOR=true` is a loopback-only local-development escape hatch. It lets the configured owner use the API without a proxy and must remain disabled in a deployed hub.

The browser receives redacted run activity and approval metadata only. It does not receive database passwords, bridge bootstrap tokens after their one-time registration response, router tokens, model provider keys, or GitHub credentials.

The bridge alone may transition a run from `queued` to `running`, then to `evidenced` after State evidence submission, or to `reconciliation` after Codex, verification, State submission, or an approved action fails. Terminal states include a completion timestamp and immutable audit event; a reconciliation is never represented as completed delivery.

## Harness session and workflow contract

A Workbench **session** is a resumable, Workbench-owned supervision context. It links a project, a browser-visible title, a named bridge worktree, one version-pinned workflow, redacted event sequence, and the run/route trace. It is not an Anvil State task and cannot replace State claim or acceptance authority.

- A session permits one `queued` or `running` run at a time.
- The hub takes a fenced, expiring lease on `worktree:{name}` before it queues a run. An unexpired lease held by a different session fails closed. The bridge receives the lease epoch for traceability and resolves the name only from its local `--worktree ID=PATH` configuration.
- Before a session run can transition to `running`, the bridge calls the hub's authenticated lease preflight. The hub rejects an expired, reassigned, or epoch-mismatched lease, so a stale command cannot touch a newly leased worktree. Terminal run states release their matching lease epoch.
- While a session run is active, the bridge renews the same lease epoch every minute. A renewal failure stops the delivery from submitting evidence and moves it to reconciliation; it never grants a new lease or silently changes worktrees.
- A workflow is validated before it is persisted, cannot contain unknown step kinds or cycles, and cannot be revised after it starts. It records an append-only, per-session event sequence for browser catch-up.
- V1 accepts only `agent`, `tool`, `condition`, `fan_out`, `join`, `approval_wait`, `evidence_submit`, `reconcile`, and `cancel` nodes. Models may propose a reviewed definition only; they do not execute arbitrary graph code or alter policy.
- An `agent` step may name up to sixteen bridge-published skills. Workflow start rejects missing skill metadata before it creates a run or transitions the workflow. A skill name and digest are copied into the immutable queued command; the bridge resolves the body only from explicit local `--skills-root` directories and fails closed when its digest no longer matches. The hub and browser never receive a local skill path or body.
- An operator direction is an append-only `operator.directive` session event. It is included in the **next** queued work packet for that session; it never interrupts or retargets an already-running Codex process.
- Effects checkpoint before execution through a bridge command and remain idempotent at the State/approval boundary. A failed bridge operation or invalid lease path becomes reconciliation, never a silent retry of an external effect.

The default delivery template is deliberately narrow: `agent -> approval_wait -> reconcile`. Its agent step owns local edit/test/evidence submission; it then pauses for a human review gate. PR creation, merge, State acceptance, deployment, and model-policy changes remain independent hash-bound approvals.

## Voice transport contract

Voice is a transport into a specific Workbench session, not a browser-side model route. The browser opens a same-origin WebSocket to `/api/sessions/{session_id}/voice/realtime`; the hub authenticates the configured private `ANVIL_VOICE_REALTIME_URL` with its own environment-held token when required.

- The relay accepts only `session.update`, input-audio append/commit/clear, `response.create`, and `response.cancel`. It strips model, tool, tool-choice, instructions, and arbitrary prompt controls.
- Input/output audio frames are forwarded in memory only. The hub stores redacted lifecycle summaries (`voice.started`, utterance/response state, interruption, error) but not audio payloads.
- Transcript text is omitted from stored events by default. `WORKBENCH_VOICE_RETAIN_TRANSCRIPTS=true` is an explicit retention choice and remains subject to existing redaction before storage.
- Voice can start, cancel, or add a turn to a session, but cannot create a PR, merge, apply State, deploy, or change model policy. Those actions retain their normal approval path.

## Bridge and GitHub action contract

The bridge makes an outbound authenticated request to the hub and is the only process with access to the project worktree and local GitHub authentication. Its runner contract is intentionally small:

1. Poll for a queued `run_codex` or approved action.
2. Read the State work packet, run Codex locally, and stream redacted activity/evidence.
3. For `commit_pr`, recalculate the repository diff hash and consume the matching approval exactly once before committing, pushing, and creating a PR.
4. For `merge_and_accept`, verify required GitHub checks, merge first, then invoke the configured State acceptance command.

Bridge commands are leased for delivery rather than deleted when fetched. A terminal run acknowledges its command only after its `evidenced` or `reconciliation` state is recorded; an interrupted fetch becomes eligible for recovery after its delivery lease expires. The hub checks that every bridge event and evidence projection belongs to the authenticated bridge's project/run.

An approval is one-time, expires, is scoped to a bridge, and binds the canonical JSON payload hash. Any changed diff or replayed grant fails closed.

`skill_probe` is the sole non-mutating bridge command outside a run or approved action. It resolves the hub-selected names locally, verifies their digests, projects redacted evaluation evidence, then acknowledges. A missing or changed digest is acknowledged only after a redacted reconciliation evidence artifact is projected; it does not retry forever. It never invokes Codex or executes skill content.

## Neo4j projection contract

Neo4j is a deterministic, idempotent projection keyed by source kind, source identifier, and source hash. It may contain State event metadata, work-packet metadata, Serving route/evaluation metadata, PR references, approvals, and redacted evidence artifacts.

It must not contain raw transcripts or message arrays, accept generic Cypher from agents, execute writes on behalf of agents, or return an approval decision. Graph output is evidence and recommendation context only.
