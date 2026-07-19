# Anvil Workbench

Anvil Workbench is a private, tailnet-first **agent harness for software delivery**. Its web UI is the main entry point for moving one project from a PRD and Anvil State task plan through a local Codex implementation, evidence review, approved GitHub PR, merge, and State acceptance.

Start with the [project brief](docs/PROJECT.md), [integration contracts](docs/CONTRACTS.md), [harness foundations](docs/HARNESS-FOUNDATIONS.md), [workflow operation-layer proposal](docs/WORKFLOW-OPERATION-LAYER.md), [local qualification record](docs/QUALIFICATION.md), [UI acceptance audit](docs/UI-ACCEPTANCE-AUDIT.md), and [session handoff](docs/SESSION-HANDOFF.md). They are the canonical orientation set for a new operator or coding session.

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

For concurrent sessions, assign each mutable worktree an operator-configured name. The browser sends only that name; it never supplies a path.

```powershell
workbench-bridge ... --worktree checkout-a=C:\path\to\project-a --worktree checkout-b=C:\path\to\project-b
```

The session engine permits one active run per session and leases each named worktree. A second session cannot start against an unexpired lease for the same worktree.

### Bridge-local skills

Skills are explicit operator-reviewed instructions, not browser-installed plugins. Configure the local bridge with one or more roots containing `SKILL.md` files:

```powershell
workbench-bridge ... --skills-root C:\path\to\project\.agents\skills --skills-root C:\path\to\reviewed-skills
```

The bridge publishes only each skill's name, short description, and SHA-256 digest. Its path and body stay local. A session selects from that published list; its selected skills and digests are bound into the next `run_codex` command. The bridge rejects a missing or changed skill before it launches Codex. **Verify bridge skills** queues a non-mutating local digest check and projects cited evidence; it does not run model code.

### Serving-only sandbox

`WORKBENCH_SANDBOX_MODELS` is an optional comma-separated allowlist. When it is set along with the hub's Serving URL and token, the Sandbox page makes a bounded, audited `/v1/responses` call through Anvil Serving. It has no raw-provider fallback and cannot access a worktree, State, GitHub, or approvals. With the variable unset, the button is explicitly unavailable rather than simulated.

## Voice

Workbench supports session-bound push-to-talk through a private Anvil Voice Realtime endpoint. Set `ANVIL_VOICE_REALTIME_URL` on the hub (and `ANVIL_VOICE_REALTIME_TOKEN` only when the upstream requires it). The browser connects to a same-origin Workbench relay; it never receives the upstream URL or token. The relay filters model and tool controls, forwards only the narrow audio protocol, and stores lifecycle summaries rather than raw audio. Transcript retention is opt-in with `WORKBENCH_VOICE_RETAIN_TRANSCRIPTS=true`; it is off by default.

For each project, explicitly configure the State work-packet and State-apply commands if its CLI syntax differs from the defaults. Both commands are executed locally in the project worktree; a State apply occurs only after an approved merge action has completed.

## V1 delivery gates

1. Workbench creates a run and the bridge reads the State work packet.
2. Codex runs in the local worktree via Anvil Serving's Responses endpoint.
3. The bridge returns redacted run activity and evidence. Route/evaluation/state metadata can be projected to Neo4j.
4. A named approver authorizes a payload hash for `commit_pr`; the bridge checks the exact diff before committing, pushing, and creating the PR.
5. A named approver authorizes `merge_and_accept`; the bridge verifies required GitHub checks, merges first, then applies State acceptance.
6. If merge or State application fails after an approval is consumed, the bridge projects a reconciliation-required failure. It never silently marks a task complete.

For each Codex run the bridge writes only provider-local configuration overrides: `wire_api = "responses"`, the Anvil router base URL, `ANVIL_ROUTER_TOKEN` as the local credential source, and static `http_headers` carrying the workbench run/task correlation. It never writes a provider API key or a GitHub token to Codex configuration.

The bridge starts Codex with a clean local tool surface: it ignores user configuration and project rules, disables plugins, apps, multi-agent, browser, computer-use, image generation, and hosted web search, and keeps the workspace-write sandbox. Those restrictions prevent a bridge run from inheriting arbitrary credential-bearing integrations from an operator desktop.

## Development

```powershell
python -m pip install -e ".[dev]"
python -m pytest
Set-Location web
npm install
npm run dev
```

`MemoryStore` and `NullGraph` make the API and bridge contract tests hermetic. Production startup always initializes Postgres; there is no silent in-memory fallback.

For a loopback-only UI/API smoke test, set `WORKBENCH_ALLOW_INSECURE_DEV_ACTOR=true` in the untracked `.env` file. This permits the configured owner to use the browser shell without a tailnet identity proxy. It is deliberately passed through only by the local Compose stack and must remain `false` in a deployed hub.

When `WORKBENCH_EMBEDDING_MODEL` is configured, evidence retrieval uses Anvil Serving's existing `/v1/embeddings` purpose route. If `WORKBENCH_RERANK_MODEL` is also configured, the fixed evidence-search tool reranks the vector/graph candidates through `/v1/rerank`. Raw transcripts are excluded before either request; an unavailable local retrieval serve falls back only to Neo4j's redacted keyword/lineage query, never a provider API.

## Repository map

| Document | Use it for |
| --- | --- |
| [Project brief](docs/PROJECT.md) | Product promise, boundaries, users, and the v1 delivery flow. |
| [Contracts](docs/CONTRACTS.md) | The exact Anvil State, Anvil Serving, bridge, graph, and approval contracts. |
| [Harness foundations](docs/HARNESS-FOUNDATIONS.md) | Research-backed requirements, V1 workflow vocabulary, session isolation, and voice boundaries. |
| [Roadmap](docs/ROADMAP.md) | What is implemented, what requires a live qualification, and the next milestones. |
| [Qualification](docs/QUALIFICATION.md) | Dated local test evidence, passed gates, and the remaining Codex/model compatibility blocker. |
| [UI acceptance audit](docs/UI-ACCEPTANCE-AUDIT.md) | Button-level coverage, exercised workflows, and explicit UI boundaries. |
| [Article demo](docs/ARTICLE-DEMO.md) | An evidence-first outline and capture list for the future public walkthrough. |
| [Session handoff](docs/SESSION-HANDOFF.md) | A concise restart point for the next coding session. |
| [Contributing](CONTRIBUTING.md) | Local setup, test commands, Compose validation, and PR expectations. |
| [Agent guide](AGENTS.md) | Non-negotiable product and safety rules for coding agents. |
