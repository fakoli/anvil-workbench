# Session handoff

Use this file to resume work in a new Anvil Workbench coding session.

## Where to start

- Repository: `C:\Users\sdoum\ai-code\anvil-workbench`
- Coding-session guide: [../CLAUDE.md](../CLAUDE.md)
- Architecture resources: [architecture/README.md](architecture/README.md)
- Contract resources: [contracts/README.md](contracts/README.md)
- Product overview: [PROJECT.md](PROJECT.md)
- System contracts: [CONTRACTS.md](CONTRACTS.md)
- Workflow operation-layer proposal: [WORKFLOW-OPERATION-LAYER.md](WORKFLOW-OPERATION-LAYER.md)
- Immediate roadmap: [ROADMAP.md](ROADMAP.md)
- Agent rules: [../AGENTS.md](../AGENTS.md)

## Current product state

The repository contains the v1 hub, bridge, frontend shell, Compose stack, and contract tests. Workbench is explicitly a private, tailnet-first agent harness whose web UI is the primary operator entry point. The Workbench code is intentionally separate from Anvil Serving and Anvil State. The separate hub is designed to be started by Anvil Serving as an optional product stack, but Workbench business logic must not move into the Serving router.

The current candidate has no seeded delivery mock. `Runs`, `Routes`, `Approvals`, `Evidence`, `Skills`, and `Sandbox` each call a concrete hub capability or state their configuration boundary. Approval review requires selecting one exact approval ID and renders its safe payload and run/worktree binding before authorization. Delivery directions are durable session events for the next work packet; selected skills are explicit bridge-local `SKILL.md` roots whose ids and digests are forwarded into that packet, then checked again locally by a non-mutating probe. Workflow start, fan-out cursor advancement, and run-command finalization use durable atomic store paths. Git evidence and approval hashes include untracked files through one isolated-index tree snapshot. Managed Codex receives only an allowlisted non-credential process environment; command-backed authentication retrieves the run-scoped router token from an ephemeral bridge-owned loopback broker that network-disabled tool subprocesses cannot reach. The optional sandbox is a bounded Serving-only Responses request; Compose passes its explicit model allowlist through to the hub. The setup guide derives completion only from live hub records.

The local qualification record is [QUALIFICATION.md](QUALIFICATION.md). On 2026-07-19 the pinned Heavy and Fast were independently preflighted; Dark STT/TTS completed a synthetic-silence pipeline; State claim/packet/evidence/replay passed through the CLI only; and the Workbench browser shell was exercised at `http://127.0.0.1:8090`. Its bounded `chat-fast` Sandbox request completed live through Serving and the Routes view reads Serving's safe `records` summary. The remaining live harness blocker is explicit: the pinned GPT-OSS Puzzle Heavy routes Codex Responses traffic and preserves correlation, but its full Codex loop returns unsupported `shell_command<|channel|>commentary` calls. Do not mark the agentic delivery flow qualified until a local model/template passes a real edit/test/evidence submission.

The browser shell is intentionally usable in two modes:

- **Production:** a tailnet identity proxy injects an authenticated identity header. No insecure fallback is enabled.
- **Loopback development:** an untracked `.env` sets `WORKBENCH_ALLOW_INSECURE_DEV_ACTOR=true`, allowing the configured owner to exercise the API without a proxy. This is only for local validation.

## Observed Serving dependency state

The 2026-07-19 browser sandbox qualification used the existing Dark router at
`http://100.87.34.66:8000/v1` with its server-held router token and an explicit
`WORKBENCH_SANDBOX_MODELS=chat-fast` allowlist. Do not commit that token or copy
it into browser code. Provision both values from the deployment secret store
when restarting the hub; a stale token or a `host.docker.internal` alias cannot
reach this router's tailnet-only bind.

At that observation point, the router container and Fast
`gemma4-e4b-it` serve were healthy. The configured `heavy` serve was stopped
because its ThinkingCap Qwen3.6 FP8 MTP start passed an incompatible
`compressed-tensors` quantization setting; fix and independently preflight the
serve recipe before attempting a heavy restart. The `anvil-serving` source
checkout was also dirty and behind `origin/main`, so do not pull, switch, or
rewrite it as part of Workbench work without a separate review of those changes.

## Resume checklist

1. Confirm the current branch and worktree are clean with `git status --short --branch`.
2. Read the README, project brief, and contracts before making a design change.
3. Run Python tests, frontend build, and Compose configuration validation.
4. For a UI change, start the local stack, use the browser validation loop, and save screenshots outside the repository.
5. For a State or Serving change, treat the contract document as a hard boundary and update it in the same PR when the interface changes.
6. For a delivery-flow change, add or extend a test that proves rejection of stale/replayed approvals and no direct State database access.

## Do not regress

- No raw provider path for Workbench-managed model traffic.
- No project worktree or GitHub token in the hub.
- No raw transcript in Neo4j.
- No direct State database use.
- No auto-PR, auto-merge, State apply, deploy, or model-policy update without a consumed approval.
- No “completed” label when merge and State acceptance disagree.
- No bridge-inherited Codex plugins, apps, MCP servers, browser tools, hosted web search, or user/project rule files.
- No browser-supplied worktree paths, model/tool controls on the voice relay, raw audio storage, or transcript retention without the explicit environment switch.

## 2026-07-20 autonomous anvil-driven run (actor `claude`)

Seven State-managed tasks were delivered through the full anvil lifecycle
(claim -> implement -> adversarial multi-agent review gate -> fix rounds ->
independent re-verification -> evidence submit -> apply -> merge), each merge
with the full suite green (61 -> 154 tests): `state-context-operations`
T001/T002.1/T002.2/T002.3/T002.4 (feature F002, the State project-context
projection, is complete but deliberately not wired into the live bridge loop
pending fakoli/anvil#178) and `chat-first-voice` T001/T002.1 (contract
schemas + conversation/turn domain models). All six PRDs are now approved;
`plan-task-delivery` and `reviewed-tools-plugins` were adversarially
reviewed, revised (r2), and approved with their open questions resolved into
Decisions. Signed proof records live in the anvil workspace `proofs/` dir.

## Best next engineering tasks

1. Resolve the Codex/local-model tool-call compatibility blocker recorded in [QUALIFICATION.md](QUALIFICATION.md); preserve the current Heavy as a qualified general Responses route but do not claim Codex harness parity.
2. Add a local identity-proxy test fixture that proves browser header spoofing is rejected.
3. Add a Postgres/Neo4j Compose integration test that runs in CI or a dedicated release job.
4. Merge and observe the hub-image publication workflow, then set the first GHCR package to public once. It builds `deploy/Dockerfile.hub`, publishes `latest`, `main`, and immutable SHA tags with an attestation, and supplies the image consumed by Anvil Serving's optional `workbench up` lifecycle command. Do not put registry credentials in `workbench.env`.
5. Qualify a live Dark voice endpoint and two bridge-configured worktrees. The implementation and hermetic contracts exist; neither is a substitute for live hardware/State qualification.
6. Implement the provider-owned workflow operation catalog proposed in [WORKFLOW-OPERATION-LAYER.md](WORKFLOW-OPERATION-LAYER.md) before adding more bespoke browser-to-bridge command paths.
7. Start that implementation from the versioned resources in [contracts/README.md](contracts/README.md): catalog/profile discovery, `operation` workflow steps, run-context snapshot, typed receipts, and bridge preflight. Do not turn the design into a generic tool runner. The State-side discovery half is implemented: `workbench/state_manifest.py` runs the bridge-configured `--state-describe-command`, fail-closed validates the advertised `anvil-operation-catalog/v1` catalog (digests, `read` effect class, contract major, draft 2020-12 object schemas), and pins the immutable `state.project.snapshot` / `state.prd.read_content` descriptor set that downstream adapters must take by constructor (`tests/test_state_manifest_discovery.py` is hermetic). The snapshot-adapter half now exists too: `workbench/state_snapshot_adapter.py` takes that pinned set by constructor, runs the bridge-configured snapshot command against only the pinned `state.project.snapshot` descriptor, and fail-closed validates the `workbench-state-snapshot/v1` payload (closed-object contract schema and prose bounds so no full PRD Markdown, digest recompute, owning-PRD references, scoped task identity, source provenance) into an immutable `PublishableSnapshot` keyed by `snapshot_digest` (`tests/test_state_snapshot_adapter.py` is hermetic). The PRD-content adapter half now exists too: `workbench/prd_content_adapter.py` takes the same pinned set by constructor, validates the scoped `prd_id` request against the pinned `state.prd.read_content` input schema before any CLI call, and fail-closed validates the `workbench-prd-content/v1` payload (closed-object contract schema, requested-PRD equality, optional expected-revision freshness, UTF-8 encoding, digest recompute, the 64 KiB byte bound, truncation coherence) into an immutable `PublishablePrdContent` keyed by `content_digest` (`tests/test_prd_content_adapter.py` is hermetic; the catalog example's `state.prd.read_content` output schema was corrected to nest `truncated` under `content` per the prd-content contract, with digests recomputed). None of these halves is wired into the live bridge poll loop; live qualification stays gated on the upstream State CLI actually advertising that catalog from `anvil describe` (fakoli/anvil#178); today's envelope only reports CLI/MCP surface names. The State-context read feature (F002) is now complete on the Workbench side: State-internals isolation is proven by a repository-scan contract test (`tests/test_security_contract.py::test_no_workbench_source_opens_copies_mounts_or_mutates_state_storage` — no hub, bridge, or browser source may reference `state.db`, its journal/WAL/shm siblings, a `.anvil` state workspace path, or any SQLite driver; the only allowlisted form is a documentation string stating the prohibition), and the three-adapter contract (discovery pins, snapshot adapter, bounded PRD-content adapter, shared injectable-runner/UTF-8 transport rule, not-wired-live gate) is documented as the "State read-adapter contract" section of [CONTRACTS.md](CONTRACTS.md). The only remaining F002 work is live wiring and qualification once fakoli/anvil#178 lands. The multi-provider catalog registry now exists too: `workbench/provider_catalogs.py` loads reviewed catalogs for the configured provider set (anvil-state via the describe command, others via operator-declared `--provider-catalog` local JSON files; http/mcp transports are declared but fail closed as not implemented), fail-closed validates identity/versions/effects/schemas/digests against the shared operation-catalog contract, and publishes only safe frozen metadata (`tests/test_provider_catalogs.py` is hermetic) — implemented, not wired into the live bridge loop.
8. The chat-first-voice contract foundation is proposed, not implemented:
   `chat-conversation.v1` and `chat-turn.v1` under `docs/contracts/` define one
   mode-agnostic conversation identity, append-only `(parent_turn_id,
   sibling_index)` turn lineage, Serving-ID/digest-only route references,
   display-only project/PRD-revision/task context, retention/deletion
   semantics, typed voice events, and hard prohibitions on persisting raw
   audio or hidden reasoning (tests: `tests/test_contract_resources.py`,
   `tests/test_security_contract.py`). Build hub persistence and API
   projection against these shapes; do not present them as live endpoints.
   The first implementation slice exists: `workbench/conversation_models.py`
   defines frozen conversation/turn/retention/deletion domain values
   mirroring those schemas, a deterministic domain-separated `sha256:`
   content hash, a fail-closed `validate_turn_append` gate enforcing the
   four append-time lineage invariants plus the conversation-ownership
   boundary and the retention-to-content-kind mapping, and content-free
   `TurnAudit`/`ConversationAudit` shapes (`tests/test_conversation_models.py`
   is hermetic). The store slice now exists too:
   `workbench/conversation_store.py` is the actor-scoped hub persistence layer
   (create/list/search/rename/archive, appends and branch/retry routed through
   `validate_turn_append`, cross-actor probes indistinguishable from a missing
   conversation, streaming turns recovered as `interrupted` after reload,
   content-free audit; `tests/test_conversation_store.py` is hermetic) — the
   API projection and the production Postgres backend are still pending.
