# Anvil Workbench agent guide

Anvil Workbench is a private delivery-control product. It is not the owner of project state, model policy, provider credentials, or GitHub credentials.

## Read first

1. [README.md](README.md)
2. [docs/PROJECT.md](docs/PROJECT.md)
3. [docs/CONTRACTS.md](docs/CONTRACTS.md)
4. [docs/SESSION-HANDOFF.md](docs/SESSION-HANDOFF.md)
5. The files you plan to change.

## Non-negotiable boundaries

- **Anvil State is canonical.** Read State through its documented CLI and canonical event stream. Never open, mount, or mutate `state.db`.
- **Anvil Serving owns model routing.** Workbench-managed model calls use the configured Responses-compatible Anvil Serving endpoint only. Never add a raw-provider fallback.
- **The browser is untrusted for credentials.** Do not return model, GitHub, bridge, database, or provider secrets from an API response. Use a tailnet identity proxy in production.
- **The project bridge is the worktree authority.** Git operations, Codex invocation, State CLI calls, and local credentials remain on the bridge host. The hub has neither a worktree nor GitHub credentials.
- **Approvals are data, not decoration.** PR creation, merge, State apply, deployment, and model/policy changes require a one-time, hash-bound approval. A changed payload invalidates its grant.
- **Neo4j is a derived projection.** Index only redacted evidence/lineage metadata. Never index raw transcripts, expose generic Cypher to an agent, or let graph output approve an action.

## Development rules

- Keep production configuration in environment variables; `.env` is local and ignored.
- Use `127.0.0.1` for host-local URLs. A container that must reach a host service should use the explicitly configured tailnet or Docker host endpoint, not an assumed loopback.
- Prefer small, testable API/bridge changes. Preserve `MemoryStore` + `NullGraph` hermetic tests for the fast feedback path.
- Run `python -m pytest -q`, `npm run build` from `web`, and `docker compose config -q` before a PR.
- For browser changes, prove a rendered page, console health, and at least one interaction. Do not treat a frontend build alone as UI validation.

## Delivery rule

No task becomes “complete” because a model says so. Completion requires the matching State acceptance and merged PR; partial completion is a visible reconciliation item.
