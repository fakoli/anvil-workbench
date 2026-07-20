# Integration contracts

This document defines the boundaries Workbench relies on. These are product contracts, not suggestions for alternate implementation paths.

The versioned resources in [contracts/README.md](contracts/README.md) are the
proposed implementation aid for the next operation layer. They validate shape
and provide fixtures; this document remains the authority for the currently
implemented v1 boundary until production code adopts a newer contract.

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
| Claim command | `anvil claim {task_id} --actor {actor} --json`; the bridge compares its returned branch with the checked-out branch of the leased worktree. |
| Work-packet command | `anvil packet {task_id} --format json`; the bridge tolerates its leading `Wrote packet ...` status line. |
| Verification capture | `anvil hook capture-evidence`; receives the bridge-observed exit code and captured stdout/stderr. |
| Evidence submit command | `anvil submit {task_id}` with verification commands and packet-declared changed files. |

The bridge may read the event stream and execute the work-packet command. It must not open, copy, remotely mount, or directly modify `state.db`. This is enforced, not just stated: a repository-scan contract test (`tests/test_security_contract.py::test_no_workbench_source_opens_copies_mounts_or_mutates_state_storage`) fails if any Workbench hub, bridge, or browser source references State's SQLite storage (`state.db`, its journal/WAL/shm siblings, or a `.anvil` state workspace path) or uses a SQLite driver at all; the only allowlisted form is a documentation string that states this prohibition.

### State read-adapter contract (implemented, not wired live)

Beyond the event stream and the work-packet command, Workbench has exactly one additional supported State read surface: the pinned read-adapter trio below. All three are implemented and hermetically tested, but **none is wired into the live bridge poll loop**. Live qualification is gated on the upstream State CLI actually advertising an `anvil-operation-catalog/v1` catalog from `anvil describe` (tracked as fakoli/anvil#178); until that lands, the trio is validated against the fixture manifests under [contracts/examples/](contracts/examples/). Do not present these adapters as live endpoints.

| Surface | Module | Contract |
| --- | --- | --- |
| Manifest discovery | `workbench/state_manifest.py` | Runs the bridge-configured `--state-describe-command` once and pins `state.project.snapshot` + `state.prd.read_content` as an immutable descriptor set. Fail-closed: the catalog digest must recompute, each operation must declare the `read` effect class, a compatible contract major, valid draft 2020-12 object schemas, and the `state_cli` transport through a named bridge adapter; an operation declaring any active preview/confirmation/approval gate is refused rather than pinned as an ungated read. Downstream adapters take the pinned set by constructor and can resolve only pinned descriptors — a browser- or model-selected operation id outside the read set is rejected, and nothing re-runs discovery per call. |
| Project snapshot | `workbench/state_snapshot_adapter.py` | Executes only the pinned `state.project.snapshot` descriptor and validates the payload, in order, against the closed-object, bounded-prose `workbench-state-snapshot/v1` contract schema (so no full PRD Markdown and no smuggled fields), the pinned output schema, source provenance (pinned operation id, provider, contract version), and the reference validator (digest recompute, owning-PRD references, scoped-id equality, uniqueness). The result is an immutable `PublishableSnapshot` whose `snapshot_digest` is the hub's idempotent publication key; no partial result exists on any failure. |
| Bounded PRD content | `workbench/prd_content_adapter.py` | Executes only the pinned `state.prd.read_content` descriptor for exactly one caller-named `prd_id`, validated against the pinned input schema before any CLI transport is invoked; the optional `expected_revision` is a post-read freshness check against `prd.revision`, never an extra CLI input. The payload passes the closed-object `workbench-prd-content/v1` contract schema, the pinned output schema, provider identity, requested-PRD equality (a response naming another PRD is refused — one scoped PRD, never a whole-project dump), UTF-8 encodability, and the reference validator (digest recompute, the 64 KiB UTF-8 byte bound, truncation/`total_bytes` coherence). The result is an immutable `PublishablePrdContent` whose `content_digest` is the publication key; the body is untrusted display data and grants no capability. |

All three surfaces share one transport rule: they execute the bridge-configured command argv only (shlex-split, no shell, never a hardcoded path), decode stdout as UTF-8, and accept exactly one JSON document, tolerating the State CLI's leading status line. The runner is injectable so every contract above is testable hermetically (`tests/test_state_manifest_discovery.py`, `tests/test_state_snapshot_adapter.py`, `tests/test_prd_content_adapter.py`); the default runner is the only subprocess path, and it never touches State storage — the State CLI remains the transport and the only authority.

### State writes

The bridge performs `claim -> work packet -> Codex -> independent verification capture -> State evidence submit` in the same bridge-resolved, leased worktree. It never receives a browser path; the worktree name must resolve from its local `--worktree ID=PATH` configuration. State packet verification prose is untrusted task data: a packet command must exactly match a local operator-configured `--verification-command` entry and is executed as an argv list with no shell. An unknown command fails closed. Changed-file evidence comes from the same isolated full-tree Git snapshot used by approval hashing, so newly created files are included without mutating the operator's index. State evidence is not submitted when a verification command fails or no packet-declared file changed. An apply command is configured separately and can run only after an approved `merge_and_accept` action has merged successfully; a standalone `state_apply` command is rejected. A failed State apply after merge is a visible reconciliation failure.

## Anvil Serving contract

### Model policy

Every Workbench-managed model call goes through a configured Anvil Serving endpoint. Workbench never adds a raw OpenAI, Anthropic, or other provider fallback. The browser never receives the router token.

### Responses compatibility

The first harness is Codex. The bridge configures Codex with an Anvil model provider using `wire_api = "responses"` and the configured router base URL. Provider authentication is command-backed: a run-scoped helper retrieves the router token from a nonce-bearing IPv4-loopback broker owned by the bridge. The Codex process receives no router, bridge, GitHub, or provider credential in its environment, and managed shell tools have network access disabled so they cannot invoke that broker. The required supported path is the stateless `/v1/responses` subset: input, instructions, function tools, function-call output continuation, normal responses, and SSE streaming. `store: false`, `include: ["reasoning.encrypted_content"]`, bounded `reasoning`, `parallel_tool_calls: false`, and `truncation: "disabled"` are recognized for Codex compatibility; they never override the Serving profile. Provider-hosted tools, arbitrary namespaces, stateful response chaining, background work, and provider-side storage fail explicitly rather than silently falling back. The bridge suppresses the internal Codex `collaboration` namespace because Workbench does not permit model-delegated agents without a separately reviewed bridge contract.

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

The browser receives redacted run activity and approval metadata only. It does not receive database passwords, bridge bootstrap tokens after their one-time registration response, router tokens, model provider keys, or GitHub credentials. It never selects a pending approval by list order: the operator must choose an approval ID, inspect its complete safe payload plus run/worktree binding, and only then can the matching authorization control be enabled.

The bridge alone may transition a run from `queued` to `running`, then to `evidenced` after State evidence submission. Its authenticated finalizer records `evidenced` or `reconciliation`, advances the bound workflow, releases a reconciliation lease when applicable, and acknowledges the exact `run_codex` command in one store transaction. `evidenced` is a delivery checkpoint, not completion: it may move to `completed` only after merge and State acceptance both succeed, or to `reconciliation` after Codex, verification, State submission, or an approved action fails. Terminal states include a completion timestamp and immutable audit event; reconciliation is never represented as completed delivery.

## Harness session and workflow contract

A Workbench **session** is a resumable, Workbench-owned supervision context. It links a project, a browser-visible title, a named bridge worktree, one version-pinned workflow, redacted event sequence, and the run/route trace. It is not an Anvil State task and cannot replace State claim or acceptance authority.

- A session permits one `queued` or `running` run at a time.
- The hub takes a fenced, expiring lease on `worktree:{name}` before it queues a run. An unexpired lease held by a different session fails closed. The bridge receives the lease epoch for traceability and resolves the name only from its local `--worktree ID=PATH` configuration.
- Before a session run can transition to `running`, the bridge calls the hub's authenticated lease preflight. The hub rejects an expired, reassigned, or epoch-mismatched lease, so a stale command cannot touch a newly leased worktree. A reconciliation releases its matching lease epoch. An `evidenced` delivery run retains it until its bound PR/merge action completes, the bridge explicitly releases it, or it expires; this prevents a later action from being redirected to a reused checkout.
- While a session run is active, the bridge renews the same lease epoch every minute. A renewal failure stops the delivery from submitting evidence and moves it to reconciliation; it never grants a new lease or silently changes worktrees.
- A workflow is validated before it is persisted, cannot contain unknown step kinds or cycles, and cannot be revised after it starts. Workflow start validates its draft, bridge, skills, session, and lease before atomically transitioning the workflow, creating the run/lease, recording the command, events, and audit. A retry cannot leave an orphan run or a running workflow without delivery. It records an append-only, per-session event sequence for browser catch-up.
- V1 accepts only `agent`, `tool`, `condition`, `fan_out`, `join`, `approval_wait`, `evidence_submit`, `reconcile`, and `cancel` nodes. Completing one cursor node removes only that node and merges its successors with unfinished siblings under a locked compare-and-swap transition; a join waits while another active branch can still reach it. Models may propose a reviewed definition only; they do not execute arbitrary graph code or alter policy.
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
3. For `commit_pr`, require an evidenced session run plus its active `run_id`, `session_id`, `worktree_id`, and lease epoch; atomically revalidate that binding, consume the approval, and renew the exact lease immediately before recalculating the repository snapshot hash and performing the effect. The bridge builds a temporary Git index from `HEAD` plus `git add -A`, which includes untracked files, hashes its binary cached diff, and writes its tree. After hash verification, the real index is populated from that exact tree rather than rereading the working tree. Commit, push, and PR creation run only in that bridge-configured worktree. Its receipt records the committed head SHA. A successful PR keeps that lease for the later merge approval.
4. For `merge_and_accept`, require that receipt's exact head SHA and the run's exact State task ID. Atomically revalidate and renew the same bound worktree lease, compare the current PR head, verify required GitHub checks, and use GitHub's head compare-and-swap merge. Only after that succeeds may the bridge invoke the configured State acceptance command in that same worktree. The dedicated consumed-merge finalizer releases the lease, marks delivery complete, and acknowledges that exact bridge command in one durable operation, so an interrupted response cannot redeliver the completed merge.

Bridge commands are leased for delivery rather than deleted when fetched. A terminal run's status, workflow transition, applicable lease release, and exact command deletion are one transaction; there is no evidenced-before-acknowledgment crash window. An interrupted fetch before that transaction becomes eligible for recovery after its delivery lease expires. The hub checks that every bridge event and evidence projection belongs to the authenticated bridge's project/run.

An approval is one-time, expires, is scoped to a bridge, and binds the canonical JSON payload hash. GitHub delivery approvals additionally bind an evidenced session run, its worktree name, lease epoch, and, for merge, the run's task ID and approved PR head SHA. Their consumption is a hub-side transaction that validates the approval/run/lease together and renews the fence before the bridge begins the external effect. Any changed diff/head, stale worktree binding, mismatched task, expired/replayed grant, or action failure fails closed into reconciliation.

`skill_probe` is the sole non-mutating bridge command outside a run or approved action. It resolves the hub-selected names locally, verifies their digests, projects redacted evaluation evidence, then acknowledges. A missing or changed digest is acknowledged only after a redacted reconciliation evidence artifact is projected; it does not retry forever. It never invokes Codex or executes skill content.

## Neo4j projection contract

Neo4j is a deterministic, idempotent projection keyed by source kind, source identifier, and source hash. It may contain State event metadata, work-packet metadata, Serving route/evaluation metadata, PR references, approvals, and redacted evidence artifacts.

It must not contain raw transcripts or message arrays, accept generic Cypher from agents, execute writes on behalf of agents, or return an approval decision. Graph output is evidence and recommendation context only.

## Settings and preferences descriptor (proposed)

This describes a **proposed** contract resource, not an implemented endpoint. No live API reads or writes it yet; it is the shape-and-authority companion to the [settings descriptor schema](contracts/schemas/settings-descriptor.v1.schema.json) and [example catalog](contracts/examples/settings-descriptor.v1.json) that a later Settings implementation (milestone-4 `preferences-configuration` F001) must adopt.

A settings descriptor catalog is a reviewed, version-pinned, digest-bearing list of every initial setting. Each descriptor declares its type, allowed values or bounds, default, exactly one owning scope, sensitivity, mutability, dependencies, optional migration, an optional authority-owned policy ceiling, and application timing. The catalog also pins `scope_precedence`, a total order over every scope. Three boundaries are load-bearing:

- **One owning scope, deterministic precedence.** Each setting owns exactly one scope (`personal`, `project`, `deployment`, or `policy`). `scope_precedence` is a permutation of all four scopes, so effective-value resolution is deterministic. A `policy_ceiling` must be owned by a strictly higher-authority scope, so a personal value can never exceed a project/deployment/policy bound — a personal preference cannot override a route policy, capability profile, approval gate, or retention ceiling.
- **Secret and path-like fields never serialize.** A `secret`-sensitivity or path-like descriptor is authority-owned (`deployment`/`policy`), carries no browser-serializable default, and is dropped entirely by the actor/project projection (`workbench.contracts.settings_actor_view`) — defence-in-depth behind the schema guard. No secret, token, credential, endpoint, or local path can reach a preference API or a redacted export.
- **Capability defaults are typed references.** A route, worktree, workflow, skill, plugin, or capability default is an `id_ref` or `digest_ref` bound to a pattern-validated allowed id or SHA-256 digest, never free text.

`workbench.contracts.validate_settings_descriptor` is the reference fail-closed check (digest recompute, precedence total order, ceiling authority, secret/path exclusion, reference-kind completeness and typing). The catalog is a proposed contract resource until code and tests wire it into a Settings store and API.
