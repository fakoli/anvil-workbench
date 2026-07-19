# Architecture research and alternatives

- **Status:** Decision support
- **Observed:** 2026-07-19
- **Scope:** Anvil State, Anvil Serving, Workbench hub, project bridge, and the
  operator-facing web UI

## Executive recommendation

Build Workbench as a **modular supervision hub plus a project-local bridge**.
Keep its durable run state, approvals, audit, and redaction in one deployable
hub for v1. Treat State and Serving as independently versioned authority
boundaries. Use a versioned operation catalog and typed receipts between those
systems rather than a shared Python package, arbitrary tool shell, or generic
remote workflow runner.

The architecture is service-oriented at its boundaries, but it should not be
microservice-heavy inside Workbench. The operational cost of a queue, scheduler,
workflow worker, event bus, policy service, and UI service is not justified
until there is evidence that the current hub cannot provide durable recovery or
availability. A deterministic outbox/receipt/reconciliation protocol is the
right first durability layer.

## The product being built

```text
PRD -> State plan -> State claim -> immutable work packet
    -> Workbench session/workflow snapshot -> local Codex through Serving
    -> independently captured verification/evidence -> approval
    -> bridge creates PR -> checks -> approved merge -> State acceptance
```

This is a delivery harness, not a chat UI, agent framework, CI runner, or task
database. Its differentiator is making the route, context, tools, effects,
evidence, and human gates visible while retaining the authority of their owner.

## Comparable systems and lessons

| System | What it proves | Adopt | Do not copy |
| --- | --- | --- | --- |
| [Temporal](https://docs.temporal.io/) | Durable workflows can resume after process or network loss. | Persist cursor before effects; explicit retry, timeout, and compensation semantics. | Add a second workflow service before Workbench has a measured need for timers, multi-day retries, or worker scale. |
| [LangGraph persistence and interrupts](https://langchain-ai.github.io/langgraph/cloud/concepts/threads/) | Agent graphs need durable checkpoints, thread identity, and resumable human input. | Immutable run snapshots, session cursor, approval pause/resume, idempotent effect boundaries. | Let model-authored graph code mutate a running workflow or become a privilege boundary. |
| [GitHub Actions concurrency](https://docs.github.com/en/actions/concepts/workflows-and-actions/concurrency) and [deployment protection](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments) | Concurrent effects need a named concurrency key and protected human authorization. | Fenced worktree lease, hash-bound approval, checks-before-merge, and clear waiting state. | Use hosted CI as the source of truth for live local worktrees, State claims, or bridge credentials. |
| [Backstage Scaffolder](https://backstage.io/docs/features/software-templates/) | A catalog of templates/actions can drive a UI and task log from declared metadata. | Provider-owned operation descriptors, input schema, dry-run/preview, task event stream, per-action authorization. | General template expressions or auto-importing every plugin action into a delivery runtime. |
| [Open WebUI's agent connection](https://docs.openwebui.com/getting-started/quick-start/connect-an-agent/) | A polished chat UI can connect to autonomous agents through a compatible API. | Keep a bounded sandbox/secondary exploration surface separate from delivery. | Use arbitrary server-side Python Functions as a worktree/credential control plane; Open WebUI warns these execute with server access. |
| [CloudEvents](https://cloudevents.io/) | Event consumers benefit from stable source, type, subject, ID, and time metadata. | An envelope vocabulary for redacted cross-product event projection and correlation. | Introduce a broker or claim CloudEvents compatibility before every producer/consumer needs it. |
| [OpenAI's Codex loop](https://openai.com/index/unrolling-the-codex-agent-loop/) | Agent execution is an iterative context -> model -> tool -> result loop. | Make context assembly, tool result continuation, and loop stop conditions first-class runtime components. | Treat the model loop as a durable delivery transaction or a substitute for evidence/approval. |

## Alternatives evaluated

| Alternative | Upside | Cost/risk | Decision |
| --- | --- | --- | --- |
| **Custom Workbench hub + bridge (recommended now)** | Fits State/Serving ownership; tailnet-friendly; least new infrastructure; can pin exact contracts. | Workbench must correctly implement cursor, leases, receipts, and reconciliation. | Adopt for v1/v2. |
| **Temporal as the central runtime** | Mature timers, retries, signals, durable workers, and visibility. | New service/worker model; workflow code can accidentally centralize authority; bridge still required. | Defer until long waits, scheduled work, and cross-project recovery exceed hub capacity. |
| **LangGraph as the workflow engine** | Useful graph/checkpoint/HITL ergonomics for an agent loop. | Python-library coupling, model-defined graph temptation, and it does not solve local credentials/State authority. | Borrow patterns; do not make it the control plane. |
| **GitHub Actions / CI as orchestrator** | Native PR/check integration and environment gates. | Hosted runners cannot own private worktrees/State CLI; expensive/slow iterative agent loop; CI is not the canonical state engine. | Use for verification/checks, not delivery orchestration. |
| **Open WebUI with Pipes/Functions** | Fast chat/sandbox and broad integrations. | Its plugins run arbitrary Python server-side; no native State/bridge/approval/reconciliation authority model. | Optional exploration UI only; not the delivery control plane. |
| **Generic automation product (n8n, Windmill, etc.)** | Fast connector composition. | Generic credentials, code nodes, and retries conflict with least authority and evidence rules. | Reject for privileged delivery effects. |
| **Shared State/Serving Python libraries in Workbench** | Lower initial call overhead. | Version lockstep, boundary bypass, accidental database/provider access, deployment coupling. | Reject; use narrow contracts and bridge adapters. |

## Key architectural decisions

### 1. A modular hub, not internal microservices

Keep API, scheduler, durable store, redaction, and graph projection co-deployed
until a real scaling or isolation reason appears. Use interfaces inside the hub
(`Store`, `BridgeTransport`, `OperationCatalog`, `GraphProjection`) so a later
split does not change the product protocol. This preserves a simple local
Compose deploy while avoiding a monolithic code tangle.

### 1a. Transactional outbox before an event broker

When the hub changes durable state and needs to deliver a bridge command or
project an event, write the state transition and an outbox record in the same
Postgres transaction. A delivery worker may deliver more than once; consumers
deduplicate by command/event ID and idempotency key. This is the practical v1
answer to the failure window between database commit and network delivery. It
does not claim exactly-once external execution, which remains impossible across
independent systems without their cooperation. The
[transactional-outbox pattern](https://microservices.io/patterns/data/transactional-outbox.html)
and idempotent-consumer practice are the relevant reference patterns; the
versioned receipt/reconciliation contract is what makes them safe for delivery
effects.

### 2. Local bridge as a data-plane executor

The bridge runs beside the project because the worktree, State CLI/event stream,
Codex process, local Serving reachability, and GitHub authentication are local
facts. It keeps an outbound authenticated connection and receives a declared
command, never a browser-supplied path or shell string. This avoids inbound
network exposure and keeps secrets off the hub/browser.

### 3. Catalog before workflow editor

The unit of reuse is a reviewed operation descriptor, not a custom UI button or
an opaque agent prompt. Each descriptor names an owner, typed inputs/outputs,
effect class, gate, adapter, idempotency/retry behavior, required evidence, and
version/digest. A workflow simply composes profile-authorized descriptors. This
mirrors the safe part of Backstage's action catalog while rejecting its generic
templating risk for privileged effects.

### 4. Receipts and reconciliation, not distributed transactions

PR creation, merge, and State acceptance cross independent systems. Do not try
to make them one transaction. Persist an intent before dispatch, persist a
redacted receipt after the owner returns, and create a reconciliation item for
an unknown or partial outcome. This is a bounded saga: merge is observed first;
only then can State acceptance be requested.

### 5. Capability profiles constrain runtime inference

A project profile is a small signed/reviewed allowlist of provider operation
IDs, skill digests, model profiles, resource limits, and approval rules. The
model can choose or propose within it; the bridge independently enforces it.
This improves weak-model reliability without giving the model a larger attack
surface.

## When to introduce Temporal or a separate worker

Revisit the central runtime when at least two of these are true:

- workflows must reliably sleep/retry for hours or days while no bridge is
  connected;
- many bridge hosts require fair dispatch and backpressure beyond Postgres
  leases/outbox delivery;
- execution spans multiple independently deployed workers with durable signals;
- recovery engineering for timers/retries is repeatedly delaying product work;
- availability goals require hub components to scale independently.

Even then, retain the operation catalog, approval, bridge, receipt, and State
acceptance contracts. A durable engine replaces scheduling mechanics, not the
authority model.

## Research-backed test consequences

- Test a bridge restart at every effect boundary; a resumed run must use the
  original workflow and catalog digests.
- Test duplicate delivery of a bridge command and an unavailable result after an
  external effect. There must be one receipt or one reconciliation item.
- Test that an approval pause does not hold an in-memory process/credential open
  and that changed diff/input invalidates the grant.
- Test worktree concurrency as a fenced lease, not merely a UI disabled state.
- Test catalog/profile denial before a bridge starts an adapter, not only at the
  browser.
- Test that a model/tool result cannot cause a State transition or policy change
  without the owner operation and required approval.

## Sources and limits

Sources above are primary project documentation, observed on 2026-07-19. They
establish reusable implementation patterns, not a claim that those products
provide Anvil State, Serving, or Workbench semantics. In particular, no external
framework is a replacement for Anvil's ownership and approval boundaries.
