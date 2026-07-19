# Anvil Workbench agent guide

Anvil Workbench is a private delivery-control product. It is not the owner of project state, model policy, provider credentials, or GitHub credentials.

## Read first

1. [README.md](README.md)
2. [CLAUDE.md](CLAUDE.md) for the compact task-routing and runtime-inference guide.
3. [docs/architecture/README.md](docs/architecture/README.md) for the relevant design decision.
4. [docs/contracts/README.md](docs/contracts/README.md) before changing a workflow, bridge, receipt, or provider operation.
5. [docs/PROJECT.md](docs/PROJECT.md)
6. [docs/CONTRACTS.md](docs/CONTRACTS.md)
7. [docs/SESSION-HANDOFF.md](docs/SESSION-HANDOFF.md)
8. The files you plan to change.

## Non-negotiable boundaries

- **Anvil State is canonical.** Read State through its documented CLI and canonical event stream. Never open, mount, or mutate `state.db`.
- **Anvil Serving owns model routing.** Workbench-managed model calls use the configured Responses-compatible Anvil Serving endpoint only. Never add a raw-provider fallback.
- **The browser is untrusted for credentials.** Do not return model, GitHub, bridge, database, or provider secrets from an API response. Use a tailnet identity proxy in production.
- **The project bridge is the worktree authority.** Git operations, Codex invocation, State CLI calls, and local credentials remain on the bridge host. The hub has neither a worktree nor GitHub credentials.
- **Approvals are data, not decoration.** PR creation, merge, State apply, deployment, and model/policy changes require a one-time, hash-bound approval. A changed payload invalidates its grant.
- **Neo4j is a derived projection.** Index only redacted evidence/lineage metadata. Never index raw transcripts, expose generic Cypher to an agent, or let graph output approve an action.
- **V2 model output is a proposal.** When the proposed operation layer is implemented, a model may select only capability-profiled, version-pinned operations. The bridge must independently validate the workflow/catalog/skill/lease/approval snapshot before every effect. Until then, describe the existing v1 behavior from [docs/CONTRACTS.md](docs/CONTRACTS.md), not this target invariant.

## Development rules

- Keep production configuration in environment variables; `.env` is local and ignored.
- Use `127.0.0.1` for host-local URLs. A container that must reach a host service should use the explicitly configured tailnet or Docker host endpoint, not an assumed loopback.
- Prefer small, testable API/bridge changes. Preserve `MemoryStore` + `NullGraph` hermetic tests for the fast feedback path.
- Keep new reusable operation, workflow, bridge-command, receipt, and run-context shapes versioned under `docs/contracts/` until production code owns them. Do not add a browser control or raw bridge command first and document it later.
- Run `python -m pytest -q`, `npm run build` from `web`, and `docker compose config -q` before a PR.
- For browser changes, prove a rendered page, console health, and at least one interaction. Do not treat a frontend build alone as UI validation.

## Delivery rule

No task becomes “complete” because a model says so. Completion requires the matching State acceptance and merged PR; partial completion is a visible reconciliation item.
