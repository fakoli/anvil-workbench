# Anvil Workbench coding-session guide

This file is the high-signal entry point for a coding session working on Anvil
Workbench. It is intentionally an index and a decision guide, not a substitute
for the product contracts.

## Read in this order

1. [AGENTS.md](AGENTS.md) for non-negotiable boundaries.
2. [README.md](README.md) for the product promise and local setup.
3. [Architecture index](docs/architecture/README.md), then the document that
   matches the task.
4. [Contract resource index](docs/contracts/README.md) before changing an API,
   bridge command, workflow, receipt, or model-facing context.
5. [Integration contracts](docs/CONTRACTS.md) for the implemented v1 boundary.
6. [Session handoff](docs/SESSION-HANDOFF.md) for current qualification state.

`docs/CONTRACTS.md` describes the current product contract. Files under
`docs/contracts/` and the workflow operation-layer document are proposed,
versioned implementation resources until code and tests adopt them. Do not
silently present a proposal as an implemented endpoint.

## Product in one paragraph

Workbench is a private, tailnet-first delivery harness. It supervises a
project's PRD-to-merged-change loop, but it does not become the owner of State,
Serving, a worktree, provider credentials, or GitHub credentials. Anvil State
is canonical for project lifecycle. Anvil Serving is the only managed model
path and owns model policy. The project bridge owns local execution. Workbench
owns durable supervision, redacted visibility, approvals, workflow snapshots,
and reconciliation.

## Choose the right layer

| If the task changes... | Work in... | Never do... |
| --- | --- | --- |
| PRD, task, claim, evidence, acceptance | State through its supported CLI/MCP on the bridge | Read or write `state.db` |
| Model route, evaluation, serve/policy lifecycle | Anvil Serving's declared Responses/MCP/CLI surface | Add a provider fallback in Workbench |
| Worktree edits, Codex, GitHub actions, local skills | Project bridge | Send browser paths, raw commands, or credentials |
| Sessions, workflow cursor, approvals, redacted events, UI | Workbench hub/browser | Reimplement State or Serving policy |
| Retrieval/lineage | Neo4j projection | Make it canonical or approval-capable |

## Build for a less-capable runtime model

Do not compensate for a weak model by granting it more authority. Improve its
runtime inference with deterministic, typed context instead:

1. Assemble a bounded run context from the immutable work packet, selected
   skills, capability profile, current workflow cursor, and recent receipts.
2. Give it a small allowlist of declared operations and their effects/gates,
   not raw shell syntax or an entire command tree.
3. Require a structured plan or tool request, then have the bridge preflight
   and execute the named adapter.
4. Feed back a redacted typed receipt and independently captured verification,
   not a prose claim of success.
5. Pause for an approval or reconciliation when the next effect exceeds the
   capability profile.

The full model-facing design is [runtime inference](docs/architecture/RUNTIME-INFERENCE.md).

## Current implementation map

| Area | Primary files |
| --- | --- |
| HTTP API and browser projection | `workbench/api.py`, `web/` |
| Session/workflow validation | `workbench/workflows.py`, `workbench/models.py` |
| Durable records, approvals, audit | `workbench/store.py` |
| Local bridge and Codex/GitHub/State execution | `workbench/bridge.py`, `workbench/cli.py` |
| Anvil Serving client and evidence | `workbench/router.py`, `workbench/retrieval.py` |
| Skill discovery/digest checks | `workbench/skills.py` |
| Redaction and graph projection | `workbench/redaction.py`, `workbench/graph.py` |
| Voice relay | `workbench/voice.py` |

Read the target file and its tests before editing. Prefer extending an existing
boundary over adding an unreviewed browser-to-bridge path.

## V2 target runtime and workflow invariants

The rules in this section are the implementation target for the proposed
operation layer. They are not a claim that the current v1 bridge dispatches
`operation` steps or persists catalog/profile snapshots. For current behavior,
read [CONTRACTS.md](docs/CONTRACTS.md) and the implementation map before making
an assertion or a code change.

- A workflow is a reviewed, version-pinned definition. A running definition and
  its provider-catalog snapshot are immutable.
- A model may propose a workflow or select from an approved capability profile;
  it cannot create a new privilege by emitting a command name, a skill, or
  arbitrary JSON.
- Every bridge effect has an idempotency key where the owner supports one, a
  redacted typed receipt, and a reconciliation path for an unknown outcome.
- Approvals bind a canonical payload hash, bridge, action, expiry, and
  one-time consumption. A changed diff or replay must fail closed.
- A worktree lease is fenced and expiring. A bridge must recheck it immediately
  before an effect and stop if it cannot renew.
- A State acceptance follows a successful approved merge; it never precedes it.

See [workflow operation layer](docs/WORKFLOW-OPERATION-LAYER.md) and the
[contract resources](docs/contracts/README.md) for the proposed next step.

## Verification and handoff

For a documentation/contract change, parse all JSON resources, check links,
and run `python -m pytest -q`. For a PR, also run `npm run build` in `web` and
`docker compose config -q`. Browser changes additionally need a rendered
interaction, console health, and an explicit note in the UI acceptance audit.

Before handing off, update the session handoff when current behavior,
qualification, an API boundary, or the next engineering step changes. Record
what is implemented, what is proposed, evidence, and the exact remaining gate.
