# Session handoff

Use this file to resume work in a new Anvil Workbench coding session.

## Where to start

- Repository: `C:\Users\sdoum\ai-code\anvil-workbench`
- Product overview: [PROJECT.md](PROJECT.md)
- System contracts: [CONTRACTS.md](CONTRACTS.md)
- Immediate roadmap: [ROADMAP.md](ROADMAP.md)
- Agent rules: [../AGENTS.md](../AGENTS.md)

## Current product state

The repository contains the v1 hub, bridge, frontend shell, Compose stack, and contract tests. The Workbench code is intentionally separate from Anvil Serving and Anvil State. The separate hub is designed to be started by Anvil Serving as an optional product stack, but Workbench business logic must not move into the Serving router.

The browser shell is intentionally usable in two modes:

- **Production:** a tailnet identity proxy injects an authenticated identity header. No insecure fallback is enabled.
- **Loopback development:** an untracked `.env` sets `WORKBENCH_ALLOW_INSECURE_DEV_ACTOR=true`, allowing the configured owner to exercise the API without a proxy. This is only for local validation.

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

## Best next engineering tasks

1. Add an API-backed project/run/approval UI instead of the current Delivery seed content.
2. Add a bridge fixture that exercises Codex event parsing and evidence upload against a test hub.
3. Add a local identity-proxy test fixture that proves browser header spoofing is rejected.
4. Add a Postgres/Neo4j Compose integration test that runs in CI or a dedicated release job.
5. Build the published hub image and wire it to the Anvil Serving optional `workbench up` lifecycle command.
