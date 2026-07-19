# Anvil Workbench

Anvil Workbench is a private, tailnet-only delivery cockpit for moving one project from a PRD and Anvil State task plan through a local Codex implementation, evidence review, approved GitHub PR, merge, and State acceptance.

It is deliberately a separate product:

- **Anvil State** is canonical for PRDs, claims, tasks, evidence, and acceptance.
- **Anvil Serving** owns local model routing and operational decisions.
- **Workbench** owns the browser UI, run supervision, redacted transcripts, approvals, and delivery orchestration.
- **Neo4j** is a derived, read-only evidence and lineage projection. It never approves or executes an action.

## Security boundary

The hub has no project worktree and no GitHub credential. A `workbench-bridge` runs beside each project and makes an outbound connection to the private hub. It only reads State through the CLI and canonical event stream; it never opens or writes State's SQLite database. GitHub actions execute locally in that bridge only after a hash-bound approval has been consumed exactly once.

The browser is restricted to the tailnet deployment and receives only redacted data. Put an identity-aware tailnet proxy in front of the hub and configure `WORKBENCH_IDENTITY_HEADER` to the header it injects after stripping client-supplied copies (the default is `Tailscale-User-Login`). The API rejects missing identities unless the explicitly development-only insecure override is enabled. Model and GitHub tokens are never returned from the API.

## Start the hub

Copy `.env.example` to `.env`, replace the passwords, then place `ANVIL_ROUTER_TOKEN` in the Docker host environment. The default bind is `127.0.0.1:8090`; publish it through your tailnet proxy rather than exposing it publicly.

```powershell
docker compose up -d --build
```

The `deploy/Dockerfile.hub` target creates the single `ghcr.io/fakoli/anvil-workbench` image consumed by Anvil Serving's packaged lifecycle template. It bundles the browser shell and API at one private port.

The optional lifecycle wrapper from Anvil Serving runs the same stack:

```powershell
anvil-serving workbench up --confirm --compose C:\Users\sdoum\ai-code\anvil-workbench\docker-compose.yml
```

## Start a project bridge

The owner registers a bridge in the Workbench UI/API and copies its one-time token into the project machine's environment. The bridge reads its State event file and retrieves the work packet using the configured State CLI command.

```powershell
$env:WORKBENCH_BRIDGE_TOKEN = "<one-time bridge token>"
$env:ANVIL_ROUTER_TOKEN = "<local router token>"
workbench-bridge `
  --hub https://workbench.tailnet.example `
  --bridge-id bridge_example `
  --project-id project_example `
  --project-root C:\path\to\project `
  --router-base-url http://100.87.34.66:8000/v1
```

For each project, explicitly configure the State work-packet and State-apply commands if its CLI syntax differs from the defaults. Both commands are executed locally in the project worktree; a State apply occurs only after an approved merge action has completed.

## V1 delivery gates

1. Workbench creates a run and the bridge reads the State work packet.
2. Codex runs in the local worktree via Anvil Serving's Responses endpoint.
3. The bridge returns redacted run activity and evidence. Route/evaluation/state metadata can be projected to Neo4j.
4. A named approver authorizes a payload hash for `commit_pr`; the bridge checks the exact diff before committing, pushing, and creating the PR.
5. A named approver authorizes `merge_and_accept`; the bridge verifies required GitHub checks, merges first, then applies State acceptance.
6. If merge or State application fails after an approval is consumed, the bridge projects a reconciliation-required failure. It never silently marks a task complete.

For each Codex run the bridge writes only provider-local configuration overrides: `wire_api = "responses"`, the Anvil router base URL, `ANVIL_ROUTER_TOKEN` as the local credential source, and static `http_headers` carrying the workbench run/task correlation. It never writes a provider API key or a GitHub token to Codex configuration.

## Development

```powershell
python -m pip install -e ".[dev]"
python -m pytest
Set-Location web
npm install
npm run dev
```

`MemoryStore` and `NullGraph` make the API and bridge contract tests hermetic. Production startup always initializes Postgres; there is no silent in-memory fallback.

When `WORKBENCH_EMBEDDING_MODEL` is configured, evidence retrieval uses Anvil Serving's existing `/v1/embeddings` purpose route. If `WORKBENCH_RERANK_MODEL` is also configured, the fixed evidence-search tool reranks the vector/graph candidates through `/v1/rerank`. Raw transcripts are excluded before either request; an unavailable local retrieval serve falls back only to Neo4j's redacted keyword/lineage query, never a provider API.
