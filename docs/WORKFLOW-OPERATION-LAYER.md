# Workflow operation layer

- **Status:** Proposed
- **Date:** 2026-07-19
- **Scope:** Anvil State, Anvil Serving, Workbench hub, and the project bridge

## Decision in one sentence

Workbench should orchestrate a version-pinned workflow of **provider-owned,
machine-readable operations**, not invoke arbitrary CLI text or absorb Anvil State
and Anvil Serving as in-process libraries.

State remains the authority for task/evidence transitions. Serving remains the
authority for routing, evaluations, and operational policy. Workbench owns the
workflow instance, approval boundaries, operator view, and reconciliation. The
bridge is the only executor that can touch a worktree or local credentials.

## Problem

The current integration is intentionally safe but still hand-shaped:

- State work packets and commands are configured per bridge.
- Serving exposes a rich command tree, controller/MCP tools, a command manifest,
  and operation contracts, but Workbench knows only a narrow set of bespoke
  calls.
- Workbench's version-pinned workflow vocabulary prevents arbitrary graphs, yet a
  generic `tool` step does not say which owner defines an operation, its inputs,
  evidence, gate, or retry semantics.

That leaves every newly useful State or Serving command needing a new Workbench
code path. It also creates a temptation to make the browser or model construct
shell commands. Both outcomes weaken the existing safety design.

The desired experience is different: when a product adds a reviewed operation,
Workbench can expose it through a template or visual workflow with a small
declarative mapping. The model may choose among already-approved paths or
propose a workflow for review; it never obtains a new effect merely by naming a
command.

## Boundaries that do not change

| System | Continues to own | Workbench may do | Workbench must not do |
| --- | --- | --- | --- |
| Anvil State | PRDs, tasks, claims, evidence, acceptance, canonical events | Reference State IDs, request bridge execution, show receipts | Reimplement transitions or open `state.db` |
| Anvil Serving | Model route, tool path, quality/evaluation evidence, resource operations | Request declared operations through an approved adapter and display safe evidence | Select an unreviewed provider route or change policy directly |
| Workbench | Workflow definition/instance, approvals, redacted supervision, reconciliation | Compile, pin, pause, and visualize a delivery workflow | Invent operation semantics or grant itself authority |
| Project bridge | Worktree, State CLI, Codex, local Serving reachability, GitHub credentials | Resolve and execute a pinned operation locally | Accept browser shell text, raw credentials, or a stale workflow |

This is a cross-product protocol boundary, not a shared Python package. Each
product remains separately deployable and versioned. The connection is made
through JSON contracts, authenticated bridge requests, and evidence receipts.

## The two layers

The existing correlation path is the **data/evidence layer**:

```text
Codex run -> Anvil Serving /v1/responses -> route/evaluation evidence
         -> Workbench run, State task, request correlation
```

This proposal adds the **control/workflow layer**:

```text
State operation catalog  ----\
                              -> Workbench workflow compiler -> pinned run packet
Serving operation catalog ----/                                  |
                                                                  v
                                               bridge preflight -> local execution
                                                                  |
                                                                  v
                                          State/Serving receipts -> evidence -> cursor/reconciliation
```

The two layers meet only through stable IDs: `workflow_id`, `workflow_version`,
`run_id`, `task_id`, `operation_id`, `operation_digest`, and an execution or
approval receipt. They never pass raw provider or GitHub credentials through the
hub or browser.

## Provider-owned operation catalogs

Each provider publishes a compact, versioned catalog of the operations it
explicitly permits Workbench to orchestrate. The catalog is not a copy of every
CLI leaf. It is a reviewed projection with product intent.

An operation descriptor needs at least:

```json
{
  "schema_version": "anvil-operation/v1",
  "provider": "anvil-serving",
  "id": "serving.eval.preflight",
  "title": "Preflight a declared routed model",
  "contract_version": "1",
  "input_schema": {"type": "object"},
  "execution": {
    "bridge_adapter": "serving.mcp.preflight_probe",
    "fallback": "none"
  },
  "effect": "read",
  "gates": {
    "preview": "required",
    "confirmation": "not_required",
    "human_approval": "not_required"
  },
  "preconditions": ["declared-model-route", "bounded-target"],
  "receipts": ["preflight-artifact", "route-correlation"],
  "failure": "reconcile",
  "docs": "docs/OPERATOR-SKILLS-AND-SUBAGENTS.md#evaluation"
}
```

The descriptor describes an operation; it does not include a shell string,
secret, host path, or provider credential. The bridge adapter owns the local
translation into a typed State CLI, Serving MCP/controller call, or a documented
safe CLI fallback.

### Why not mirror every command automatically?

Anvil Serving's command manifest is an excellent discovery source, but a public
CLI command is not automatically a safe Workbench action. Some leaves are
diagnostic, some are host-specific, and some persist a model or policy change.
Auto-importing all of them would recreate a generic remote shell.

Instead, an owner opts in with a Workbench projection that supplies:

- the stable product intent and input schema;
- the permitted transport and bridge adapter;
- preview, confirmation, human-approval, idempotency, and timeout rules;
- the required evidence receipts and reconciliation behavior; and
- the catalog and operation digests that Workbench must pin.

A new Serving or State operation becomes available to Workbench when its owning
product adds this reviewed projection and its bridge adapter. Workbench then
needs a template/UI mapping, not a bespoke end-to-end implementation.

## Initial operation families

### Anvil State

State's catalog should expose task-level intent, never database implementation:

| Operation | Effect | Required result |
| --- | --- | --- |
| `state.task.claim` | bounded task transition | State claim receipt and canonical event ID |
| `state.task.packet` | read | Immutable work-packet digest and declared verification plan |
| `state.verification.capture` | evidence capture | Redacted verification receipt with exit status |
| `state.evidence.submit` | task evidence transition | State evidence/event ID; refuses failed verification or undeclared changes |
| `state.task.accept_merged` | acceptance transition | Requires merged-PR receipt plus consumed `merge_and_accept` approval |

The bridge still calls the supported State CLI and tails canonical events. An
operation catalog does not permit Workbench to write State directly, duplicate
its state machine, or apply acceptance before a merge.

### Anvil Serving

Serving should project only operations that have an existing declared contract:

| Operation family | Typical use in a workflow | Gate expectation |
| --- | --- | --- |
| `serving.route.explain` / decision summary | Attach model/profile rationale to a run | Read-only, bounded |
| `serving.eval.preflight` / benchmark artifact | Gather capability evidence before a delivery or policy decision | Declared target, bounded execution; independent evidence |
| `serving.harness.sync` | Render or apply a reviewed harness configuration | Preview first; mutation follows Serving's existing gate |
| `serving.voice.status` / bounded logs | Add voice readiness evidence to a session | Read-only, bounded |
| `serving.model.policy` / serve promotion | Change a route, serve, or policy | Always a separate human approval; never model-selected |

The existing Responses correlation contract remains mandatory for every agent
run. A workflow operation may cite correlation evidence, but it cannot forge a
route decision or treat a sandbox response as delivery evidence.

### Workbench-native operations

Workbench keeps only control-plane operations that it truly owns: start a
session, acquire/renew a worktree lease, enqueue a run, wait for a hash-bound
approval, record redacted evidence, reconcile, and cancel. It does not acquire
new State or Serving authority by wrapping their operations.

## Workflow definition v2

The current allowlisted graph remains the safety boundary. Add an `operation`
step as a precise replacement for the ambiguous use of a generic `tool` step.
The existing `agent`, `approval_wait`, `evidence_submit`, `reconcile`,
`fan_out`, `join`, `condition`, and `cancel` semantics remain useful.

```json
{
  "schema_version": "workbench-workflow/v2",
  "entry": "claim",
  "steps": [
    {
      "id": "claim",
      "kind": "operation",
      "operation": {
        "provider": "anvil-state",
        "id": "state.task.claim",
        "contract_version": "1"
      },
      "inputs": {"task_id": {"from": "run.task_id"}},
      "next": ["implement"]
    },
    {
      "id": "implement",
      "kind": "agent",
      "model": "planning",
      "skills": ["anvil:execute"],
      "next": ["evidence"]
    },
    {
      "id": "evidence",
      "kind": "operation",
      "operation": {
        "provider": "anvil-state",
        "id": "state.evidence.submit",
        "contract_version": "1"
      },
      "inputs": {"task_id": {"from": "run.task_id"}},
      "next": ["pr_approval"]
    },
    {
      "id": "pr_approval",
      "kind": "approval_wait",
      "approval_action": "commit_pr",
      "next": ["reconcile"]
    },
    {"id": "reconcile", "kind": "reconcile", "next": []}
  ]
}
```

`inputs` is a deliberately small binding language: literals plus allowlisted
references to the run, selected work packet, or earlier redacted receipt. It is
not a template engine, JavaScript expression, shell interpolation, or arbitrary
JSONPath evaluator. Output fields must be declared by the operation descriptor.

## Compile and execution protocol

1. **Discover.** At bridge registration or explicit refresh, the bridge reads
   provider catalogs through their documented local interfaces. It submits only
   descriptors, versions, and digests to the hub.
2. **Author.** A human or model may propose a template. Workbench validates the
   graph, operation IDs, typed bindings, gates, skills, resource limits, and
   provider availability. A model proposal is never executable by itself.
3. **Review.** The operator approves the template and selected project capability
   profile. The profile names which provider operations are allowed for that
   project/worktree.
4. **Compile.** Starting a session resolves each operation to an exact provider
   descriptor and records a catalog snapshot digest in the immutable workflow
   version and queued bridge command.
5. **Preflight.** Before every effect, the bridge rechecks its local catalog
   digest, project ownership, lease epoch, typed inputs, declared target, and any
   required preview or approval receipt. A mismatch blocks the run; it never
   falls back to a newer command shape or a raw shell command.
6. **Execute.** The bridge invokes the named adapter once with an idempotency key
   where the provider supports one. It stores a redacted, typed receipt before
   advancing the cursor.
7. **Reconcile.** Any unknown outcome, expired lease, failed verification,
   changed contract, provider failure, or merge/State split becomes a visible
   reconciliation item. Effects are not silently retried.

## Dynamic workflows without dynamic privilege

This preserves the useful part of dynamic workflows: an agent can propose a
different sequence, fan-out plan, or additional evidence step for an unusual
task. It removes the dangerous part: the agent cannot add a new command,
privilege, host, model route, or approval type.

- A proposal can reference only operations in the project capability profile.
- A proposed workflow is validated and reviewed before it starts.
- Once running, the definition and catalog snapshot are immutable.
- A paused workflow resumes from a durable cursor; it is not reinterpreted
  against a newer State or Serving catalog.
- A new capability requires a new workflow version and, when its effect or
  policy requires it, an explicit human review/approval.

Skills remain advisory local instructions. A skill can help a model choose or
prepare inputs for an approved operation, but a skill name, digest, or model
tool call cannot itself authorize an effect.

## Security and failure rules

| Situation | Required behavior |
| --- | --- |
| Unknown provider, operation, schema, or catalog digest | Refuse before bridge execution |
| State work-packet digest changes before evidence submission | Stop and reconcile; fetch/review a new packet |
| Serving profile/route changes before an evaluation or model-policy operation | Re-preflight or require a new approval according to the descriptor |
| Approval payload differs, expires, or was consumed | Refuse; request a new hash-bound approval |
| Bridge loses lease or cannot persist a receipt | Stop before evidence/side effect; reconciliation |
| Browser/model submits CLI text, a path, or a credential | Reject at the hub; bridge resolves only configured adapters and worktree names |
| Provider result is unavailable after an external effect | Record an unknown outcome and reconcile; never assume success or retry blindly |

## Delivery plan

1. **Catalog contract.** Define `anvil-operation/v1`, catalog digesting, and a
   project capability profile. Start with static fixture catalogs plus validation
   tests in Workbench.
2. **State adapter.** Have the bridge publish the five State operations above
   from its configured supported CLI commands. Prove no direct database access,
   stale-packet refusal, and merge-before-acceptance ordering.
3. **Serving projection.** Add a deliberate Workbench projection over Serving's
   command manifest and `operation_contracts`, beginning with route/evidence,
   preflight, bounded status/logs, and harness render. Do not expose all CLI
   leaves by default.
4. **Workflow v2.** Add validated `operation` steps, snapshot persistence,
   typed receipt storage, and bridge preflight. Keep v1 template support during
   migration.
5. **Delivery template.** Migrate the default PRD-to-merge template to explicit
   State operations and independent approval actions. Keep GitHub effects local
   to the bridge.
6. **Editor and review.** Build a visual editor that emits the same validated
   JSON and shows each owner, effect class, gate, input binding, and expected
   evidence. It must not become a second execution engine.
7. **Qualification.** Run an end-to-end non-production project through State
   claim, packet, Codex, verification, evidence, approved PR, merge, and State
   acceptance with no raw-provider path.

## Acceptance criteria

- Adding a reviewed State or Serving operation requires an owner catalog entry,
  bridge adapter, schema/gate/receipt tests, and docs—but not a new arbitrary
  browser-to-shell path.
- Every started workflow stores the exact provider catalog digests and cannot
  execute when they drift.
- Browser controls render intent and gates from the operation descriptor; they
  never expose a raw command, worktree path, token, or provider credential.
- Every external effect has a typed receipt or a reconciliation item.
- State remains the authority for task acceptance, Serving remains the authority
  for model policy, and Workbench cannot promote itself into either role.

## Open decisions

1. Should providers serve catalogs over a versioned local HTTP/MCP endpoint, or
   should the bridge read a signed JSON export from each installed product? The
   recommended first implementation is a bridge-read, versioned JSON export:
   it works offline and keeps the hub out of the control path.
2. Which Serving operations are safe enough for the first project capability
   profile? Start read-only and preflight/evidence operations; add mutations only
   after their existing human-gate and reconciliation contracts are represented.
3. What is the equivalent State operation-catalog command? It should be a
   supported CLI/JSON surface, not an inspection of State's database or internal
   Python types.

These decisions are intentionally implementation-facing. They preserve the
product promise: Workbench makes delivery repeatable and visible without
centralizing authority that belongs to State, Serving, or the project bridge.
