# Session handoff

Use this file to resume work in a new Anvil Workbench coding session.

## Where to start

- Repository: `C:\Users\sdoum\ai-code\anvil-workbench`
- Product overview: [PROJECT.md](PROJECT.md)
- System contracts: [CONTRACTS.md](CONTRACTS.md)
- Workflow operation-layer proposal: [WORKFLOW-OPERATION-LAYER.md](WORKFLOW-OPERATION-LAYER.md)
- Immediate roadmap: [ROADMAP.md](ROADMAP.md)
- Agent rules: [../AGENTS.md](../AGENTS.md)

## Current product state

The repository contains the v1 hub, bridge, frontend shell, Compose stack, and contract tests. Workbench is explicitly a private, tailnet-first agent harness whose web UI is the primary operator entry point. The Workbench code is intentionally separate from Anvil Serving and Anvil State. The separate hub is designed to be started by Anvil Serving as an optional product stack, but Workbench business logic must not move into the Serving router.

The current candidate has no seeded delivery mock. `Runs`, `Routes`, `Approvals`, `Evidence`, `Skills`, and `Sandbox` each call a concrete hub capability or state their configuration boundary. Delivery directions are durable session events for the next work packet; selected skills are explicit bridge-local `SKILL.md` roots whose ids and digests are forwarded into that packet, then checked again locally by a non-mutating probe. The optional sandbox is a bounded Serving-only Responses request; Compose passes its explicit model allowlist through to the hub. The setup guide derives completion only from live hub records.

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

## Best next engineering tasks

1. Resolve the Codex/local-model tool-call compatibility blocker recorded in [QUALIFICATION.md](QUALIFICATION.md); preserve the current Heavy as a qualified general Responses route but do not claim Codex harness parity.
2. Add a local identity-proxy test fixture that proves browser header spoofing is rejected.
3. Add a Postgres/Neo4j Compose integration test that runs in CI or a dedicated release job.
4. Build the published hub image and wire it to the Anvil Serving optional `workbench up` lifecycle command.
5. Qualify a live Dark voice endpoint and two bridge-configured worktrees. The implementation and hermetic contracts exist; neither is a substitute for live hardware/State qualification.
6. Implement the provider-owned workflow operation catalog proposed in [WORKFLOW-OPERATION-LAYER.md](WORKFLOW-OPERATION-LAYER.md) before adding more bespoke browser-to-bridge command paths.
