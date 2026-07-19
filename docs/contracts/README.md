# Contract resources

These resources are the implementation-facing companion to
[CONTRACTS.md](../CONTRACTS.md) and
[WORKFLOW-OPERATION-LAYER.md](../WORKFLOW-OPERATION-LAYER.md).

- `CONTRACTS.md` is the current v1 product boundary.
- This directory contains **proposed**, versioned shapes for the next operation
  layer. They do not create an endpoint, grant a capability, or supersede an
  owner’s own contract until code and tests adopt them.
- Schemas validate payload shape. Bridge code must additionally enforce every
  semantic rule: catalog/profile authorization, worktree lease, digest match,
  approval binding, idempotency, and reconciliation.

## Read the smallest relevant resource

| Need | Schema | Example |
| --- | --- | --- |
| Publish a reviewed State, Serving, or bridge operation | [operation catalog](schemas/operation-catalog.v1.schema.json) | [State catalog](examples/anvil-state.catalog.v1.json), [Serving catalog](examples/anvil-serving.catalog.v1.json) |
| Define which operations, skills, and model profiles one project may use | [capability profile](schemas/capability-profile.v1.schema.json) | [project profile](examples/project-capability-profile.v1.json) |
| Add a durable declarative workflow | [workflow v2](schemas/workflow.v2.schema.json) | [delivery workflow](examples/delivery.workflow.v2.json) |
| Improve a coding model’s next inference turn | [run context](schemas/run-context.v1.schema.json) | [run context](examples/run-context.v1.json) |
| Validate a model’s bounded next action | [model proposal](schemas/model-proposal.v1.schema.json) | [operation request](examples/model-proposal.operation-request.v1.json) |
| Deliver a command to a local bridge | [bridge command](schemas/bridge-command.v1.schema.json) | [bridge command](examples/bridge-command.invoke-operation.v1.json) |
| Return evidence for an operation/effect | [operation receipt](schemas/operation-receipt.v1.schema.json) | [operation receipt](examples/operation-receipt.v1.json) |

## Normative conventions

1. All contract identifiers are stable lowercase dot-separated strings. The
   owner controls a provider operation ID; Workbench cannot redefine it.
2. Versioning has two levels: a **contract version** for payload compatibility
   and a **catalog/workflow revision plus SHA-256 digest** for a run snapshot.
3. A run pins catalog, profile, workflow, selected-skill, and route identifiers
   before bridge delivery. A changed digest blocks execution; it never silently
   upgrades a paused workflow.
4. The browser sends intent and selected IDs only. It never sends an executable
   shell command, filesystem path, credential, arbitrary HTTP URL, raw Cypher,
   or approval payload to the bridge.
5. Every effect has an idempotency key, bounded delivery attempt, redacted typed
   receipt, and `unknown`/reconciliation behavior. Do not claim distributed
   exactly-once execution.
6. State transitions remain State CLI/MCP operations on the bridge. Serving
   routes and policy remain Serving operations. GitHub credentials remain local.
7. Add `traceparent` when telemetry supports it, alongside the existing
   `workbench_run_id`, `task_id`, and `request_id`; correlation fields are not
   credentials or policy overrides.
8. Store and project only redacted receipts/evidence. Never put raw transcript,
   local skill body/path, token, or unredacted provider payload in a contract
   artifact.

## Contract-extension checklist

Before exposing a new operation in a workflow or UI:

1. Name the provider/authority and classify the effect: `read`,
   `state_mutation`, `external_effect`, or `policy_mutation`.
2. Add a catalog descriptor with input/output schemas, local bridge adapter,
   preconditions, gates, receipt kinds, timeout/retry/idempotency, and docs.
3. Decide whether the project capability profile permits it and whether a
   hash-bound approval is required.
4. Add an example plus consumer/bridge tests for valid input, denial, replay,
   drift, and unknown outcome.
5. Render the UI from the descriptor’s title/effect/gate/evidence metadata;
   do not add an untracked bespoke button.
6. Update the owning provider integration documentation and qualification plan.

## Use by code generation or a lower-capability model

Use schemas to generate validators/forms/test fixtures and examples to assemble
a minimal valid payload. Do not use them as prompts to call an operation. The
run context must first identify a profile-authorized operation; then the bridge
must validate the same descriptor locally immediately before the effect.
