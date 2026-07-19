# Runtime inference design

- **Status:** Proposed implementation guide
- **Purpose:** Help a local or less-capable coding model make repeatable,
  evidence-producing delivery progress without receiving extra authority.

## The central rule

Give the model **better state and smaller choices**, not unrestricted tools or
more hidden prompt text. A model's output is a proposal until a typed bridge
adapter, an independent verifier, and the relevant authority accept it.

## Runtime components

| Component | Input | Deterministic responsibility | Output |
| --- | --- | --- | --- |
| Context assembler | State work packet, workflow snapshot, capability profile, selected skill digests, recent receipts | Build a bounded, ordered run-context document and reject missing/changed prerequisites. | `run-context/v1` |
| Capability resolver | Project profile + provider catalogs | Resolve only exact operation IDs/versions/digests permitted for this run. | Tool/operation menu |
| Agent runner | Run context + Serving Responses route | Ask the model to plan, edit, test, or request an allowed operation. | Structured activity / request |
| Bridge preflight | Requested operation + lease + local catalog + approval state | Verify worktree, profile, typed inputs, preview, approval, and idempotency key. | Executable command or refusal |
| Adapter executor | Declared bridge adapter | Call State CLI, Serving controller/MCP, Codex, GitHub, or verifier locally. | Typed receipt |
| Independent verifier | Declared verification plan + changed files | Capture commands and exit status independently of the model's self-report. | Verification receipt |
| Evidence compiler | Receipts, State IDs, Serving correlation, redaction | Produce cited delivery evidence and State-submission input. | Evidence bundle |
| Reconciler | Unknown/partial outcome | Stop automatic progress, present facts, and create the smallest safe next action. | Reconciliation item |

The components may live in one Workbench deployment at first. Their contracts,
not their process count, are the architecture.

## Context assembly order

The context assembler produces one versioned object. It should include only what
the current step needs, in this order:

1. **Identity and boundary:** run/session/task IDs, worktree name (not path),
   bridge ID, workflow/catalog/profile digests, and a concise authority rule.
2. **Goal:** State task title, acceptance criteria, dependencies, scope, and
   packet-declared files/verification plan.
3. **Current cursor:** the current workflow step, completed receipt summaries,
   retries remaining, and the reason for any pause.
4. **Allowed capabilities:** small operation cards with intent, effect, inputs,
   evidence, and gate—not raw CLI, credential, filesystem path, or general
   command tree.
5. **Selected skills:** name, purpose, immutable digest, and bridge-confirmed
   availability. The model sees content only when the local runner deliberately
   loads it; the hub/browser persist only metadata.
6. **Execution protocol:** structured response/tool format, verification
   requirement, stop conditions, and how to ask for an approval/reconciliation.
7. **Recent evidence:** bounded redacted receipts/citations relevant to the
   next choice. Do not attach full historical transcripts by default.

The assembler should prefer source material in this order: State work packet,
workflow snapshot, bridge-local capability/profile proof, independently captured
verification, then redacted model activity. Model prose is never the authority
for a task state or a prior external effect.

## Inference loop

```text
compile snapshot -> assemble run context -> model proposal
      ^                                           |
      |                                   bridge preflight
      |                                           |
      +-- typed receipt <- execute adapter <- approved request
                      |
             verifier/evidence/reconcile
```

1. The hub compiles a run with exact workflow, profile, catalog, skill, and
   route identifiers.
2. The bridge verifies the snapshot, worktree lease, and local skill/catalog
   digests before starting the model.
3. The model returns either bounded activity, a local edit/test request, an
   allowed operation request, a request for clarification, or a stop/reconcile
   request.
4. The bridge validates the request before each effect. It does not execute
   model-authored shell, path, policy, or credential text.
5. The adapter returns a typed redacted receipt. Independent verification runs
   where the State packet requires it.
6. The next inference turn receives the relevant receipt summary. The loop ends
   at evidence, an approval wait, a blocker, a resource limit, or reconciliation.

## Reliability techniques for smaller models

| Weak-model failure | Runtime guard | Evidence of success |
| --- | --- | --- |
| Loses task scope | Immutable State packet excerpt plus explicit acceptance checklist | Every final evidence item cites a packet criterion. |
| Hallucinates a tool/command | Catalog-derived menu and schema validation | Unknown ID rejected before bridge execution. |
| Skips tests | Verification plan is a required cursor transition, not a suggestion | Captured command/exit receipt. |
| Claims success early | Separate evidence compiler and State transition | No `completed` state without merge + State acceptance. |
| Loops or over-explores | Per-step turn/tool/time budgets and a no-progress detector | Bounded pause/reconcile event. |
| Uses stale information | Catalog/skill/packet/lease digest checks at every bridge preflight | Mismatch stops before effect. |
| Attempts privileged action | Capability profile plus hash-bound approval | Denied request or consumed approval receipt. |
| Receives too much context | Retrieve/attach only the current packet, operation cards, and cited receipts | Run-context size/budget telemetry. |

## A narrow structured response contract

Prefer a small response union over free-form instructions. The exact schema is
in [run-context and model proposal resources](../contracts/README.md).

```json
{
  "kind": "operation_request",
  "operation_id": "state.verification.capture",
  "input": {"verification_id": "pytest"},
  "reason": "The work packet requires this verification before evidence submission."
}
```

Valid kinds are `progress`, `operation_request`, `clarification_request`,
`approval_request`, and `reconcile`. The model cannot emit a raw command as an
operation. It can write and test only through the separately sandboxed local
agent runner; external effects remain bridge operations.

## Skills are context, not capability grants

Skills improve execution quality by supplying local instructions, checklists,
and domain conventions. They do not add a provider route, a command, access to
a path, or permission to perform an external effect. Bind skills by digest into
the queued command; have the bridge refuse missing or changed skills; attribute
evidence to a skill ID/digest without sending the skill body to the hub or graph.

## Stop and recovery policy

Stop normal execution and create a reconciliation item when any of these occur:

- lease/profile/catalog/packet/skill digest drift;
- malformed model operation request or an unavailable local adapter;
- failed required verification or no packet-declared changed file;
- expired, mismatched, or consumed approval;
- ambiguous bridge delivery or external-effect result;
- route/policy change that invalidates a descriptor precondition;
- repeated no-progress turns or a time, tool, or context budget limit.

The reconciler should show known facts, cited receipts, the owning system, and
the minimal safe action. It should not ask the model to guess whether an effect
happened.

## Implementation sequence

1. Persist a `run-context/v1` snapshot for every queued run and expose its
   redacted summary in the Runs view.
2. Add a capability resolver over the catalog/profile, then replace generic
   workflow `tool` use with `operation` steps.
3. Validate model proposals against the narrow union before bridge dispatch.
4. Make verification and evidence receipts first-class inputs to the next turn.
5. Add budget/no-progress telemetry and explicit reconcile paths.
6. Qualify with a deliberately weaker local model and compare completion,
   verification, and unsafe-request rejection rates against the current route.

This sequence turns runtime inference into a product capability rather than a
larger system prompt.
