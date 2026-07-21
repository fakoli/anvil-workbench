# Contract resources

These resources are the implementation-facing companion to
[CONTRACTS.md](../CONTRACTS.md) and
[WORKFLOW-OPERATION-LAYER.md](../WORKFLOW-OPERATION-LAYER.md).

- `CONTRACTS.md` is the current v1 product boundary.
- This directory contains **proposed**, versioned shapes for the next operation
  layer. They do not create an endpoint, grant a capability, or supersede an
  owner’s own contract until code and tests adopt them.
- The State-side read half is adopted by code: manifest discovery plus the
  snapshot and bounded PRD-content adapters are implemented and hermetically
  tested, but not wired into the live bridge poll loop. Their authoritative
  description is the "State read-adapter contract" section of
  [CONTRACTS.md](../CONTRACTS.md); live qualification stays gated on
  fakoli/anvil#178.
- Schemas validate payload shape. Bridge code must additionally enforce every
  semantic rule: catalog/profile authorization, worktree lease, digest match,
  approval binding, idempotency, and reconciliation.

## Read the smallest relevant resource

| Need | Schema | Example |
| --- | --- | --- |
| Publish a reviewed State, Serving, or bridge operation | [operation catalog](schemas/operation-catalog.v1.schema.json) | [State catalog](examples/anvil-state.catalog.v1.json), [Serving catalog](examples/anvil-serving.catalog.v1.json), [bridge catalog](examples/project-bridge.catalog.v1.json) |
| Define which operations, skills, and model profiles one project may use | [capability profile](schemas/capability-profile.v1.schema.json) | [project profile](examples/project-capability-profile.v1.json) |
| Add a durable declarative workflow | [workflow v2](schemas/workflow.v2.schema.json) | [delivery workflow](examples/delivery.workflow.v2.json) |
| Improve a coding model’s next inference turn | [run context](schemas/run-context.v1.schema.json) | [run context](examples/run-context.v1.json) |
| Validate a model’s bounded next action | [model proposal](schemas/model-proposal.v1.schema.json) | [operation request](examples/model-proposal.operation-request.v1.json) |
| Deliver a command to a local bridge | [bridge command](schemas/bridge-command.v1.schema.json) | [bridge command](examples/bridge-command.invoke-operation.v1.json) |
| Return evidence for an operation/effect | [operation receipt](schemas/operation-receipt.v1.schema.json) | [operation receipt](examples/operation-receipt.v1.json), [preflight refusal](examples/operation-receipt.refusal.v1.json) |
| Display a project's PRD/plan/task hierarchy without touching State storage | [state snapshot](schemas/state-snapshot.v1.schema.json) | [project snapshot](examples/anvil-state.project-snapshot.v1.json) |
| Read one PRD's bounded, redacted content for display | [PRD content](schemas/prd-content.v1.schema.json) | [PRD content read](examples/anvil-state.prd-content.v1.json) |
| Persist one chat/voice conversation identity with display-only project/PRD/task context | [chat conversation](schemas/chat-conversation.v1.schema.json) | [conversation](examples/chat.conversation.v1.json) |
| Append one immutable chat/voice turn with lineage, route reference, and voice events | [chat turn](schemas/chat-turn.v1.schema.json) | [user voice turn](examples/chat.turn.user-voice.v1.json), [interrupted assistant turn](examples/chat.turn.assistant-interrupted.v1.json) |
| Declare every initial setting's type, scope, sensitivity, mutability, precedence, and typed reference defaults | [settings descriptor](schemas/settings-descriptor.v1.schema.json) | [settings descriptor catalog](examples/settings-descriptor.v1.json) |
| Open an Advanced-mode experiment over an existing conversation with route-declared controls and mock/read-only tools | [advanced branch](schemas/advanced-branch.v1.schema.json) | [advanced branch](examples/advanced-branch.v1.json) |
| Inspect one Advanced attempt as a normalized, redacted request/route/tool/usage trace | [advanced trace](schemas/advanced-trace.v1.schema.json) | [advanced trace](examples/advanced-trace.v1.json) |
| Save a digest-pinned, actor-private Advanced preset that repairs on route/tool drift | [advanced preset](schemas/advanced-preset.v1.schema.json) | [advanced preset](examples/advanced-preset.v1.json) |
| Compare two to four sibling attempts by factual metrics with no invented winner | [advanced comparison](schemas/advanced-comparison.v1.schema.json) | [advanced comparison](examples/advanced-comparison.v1.json) |
| Reference one task by its owning PRD, pinned revision, and source snapshot for a task row or detail view | [task reference](schemas/task-reference.v1.schema.json) | [task reference](examples/task-reference.v1.json) |
| State whether a scoped task may enter a Deliver flow, with stable blocked/stale codes and human-safe explanations | [delivery eligibility](schemas/delivery-eligibility.v1.schema.json) | [delivery eligibility](examples/delivery-eligibility.v1.json) |
| Start delivering a scoped task with an idempotent, ids-only Deliver intent | [deliver intent](schemas/deliver-intent.v1.schema.json) | [deliver intent](examples/deliver-intent.v1.json) |
| Acknowledge a Deliver intent with a typed accepted/duplicate/denied start receipt | [deliver start receipt](schemas/deliver-start-receipt.v1.schema.json) | [start receipt](examples/deliver-start-receipt.v1.json), [start refusal](examples/deliver-start-receipt.refusal.v1.json) |

## Normative conventions

1. All contract identifiers are stable lowercase dot-separated strings. The
   owner controls a provider operation ID; Workbench cannot redefine it.
2. A digest is trusted only when the bridge recomputes it from its configured
   local catalog/profile and recognizes the provider. The hub cannot introduce a
   catalog merely by sending a schema-valid payload. The required canonical
   algorithm and provider-snapshot rules are in [DIGESTING.md](DIGESTING.md).
3. Versioning has two levels: a **contract version** for payload compatibility
   and a **catalog/workflow revision plus mandatory SHA-256 digest** for a run snapshot.
4. A run pins every provider catalog, selected operation, profile, workflow,
   selected skill, and route identifier before bridge delivery. A changed or
   unknown digest blocks execution; it never silently
   upgrades a paused workflow. The selected operation's provider occurs exactly
   once in the snapshot, at the same digest as the bridge's configured local
   catalog, and the operation must be present at that exact digest in the
   pinned capability profile.
5. The browser sends intent and selected IDs only. It never sends an executable
   shell command, filesystem path, credential, arbitrary HTTP URL, raw Cypher,
   or approval payload to the bridge.
   Model proposals are held to the same rule: their inputs must validate against
   the selected operation's typed schema, and a bridge rejects raw command and
   secret fields before an adapter sees them.
6. Every non-read effect has a deadline, bounded delivery attempt, idempotency
   key, redacted typed
   receipt, and `unknown`/reconciliation behavior. Do not claim distributed
   exactly-once execution. An operation whose catalog gate requires human
   approval additionally needs the catalog-declared approval action, an
   unexpired one-time grant, and a payload digest binding its exact typed inputs.
   A bridge-injected atomic approval consumer must consume that grant; command
   fields that merely claim a grant are never authority to execute.
7. State transitions remain State CLI/MCP operations on the bridge. Serving
   routes and policy remain Serving operations. GitHub credentials remain local.
8. Add `traceparent` when telemetry supports it, alongside the existing
   `workbench_run_id`, `task_id`, and `request_id`; correlation fields are not
   credentials or policy overrides.
9. Schema validation does not prove a value has been redacted. Redact at bridge
   ingress and again before hub persistence/projection; store only allowlisted
   opaque references and short safe summaries. Never put raw transcript,
   local skill body/path, token, or unredacted provider payload in a contract
   artifact.
10. A task reference is an object that names its owning PRD: `{prd_id,
    task_id}` (plus `prd_revision` where a run pins a source revision). Task
    IDs are only unique within one PRD, so two PRDs may each own a `T001` and
    both remain unambiguous. A bare `task_id` string outside a reference
    object (for example in receipt correlation) is display/correlation data
    only and carries no authority; where present it should use the scoped
    `<prd_id>:<task_id>` form.
11. A chat conversation is one identity shared by ordinary and Advanced modes
    and by voice; mode is a per-turn attribute and turns are append-only with
    typed `(parent_turn_id, sibling_index)` lineage — a retry or branch is a
    new turn, never a rewrite. Raw audio frames and hidden/encrypted model
    reasoning are prohibited from durable chat/turn records and from every API
    response: voice persists typed lifecycle events plus, only where the
    conversation's retention policy permits, redacted transcript text. A turn's
    route reference carries Anvil Serving route IDs and digests only — never a
    provider endpoint, URL, or credential — and a conversation's context block
    is display-only: it pins readable titles plus canonical project,
    PRD-revision, and task IDs without implying a claim, lease, or effect
    grant. The conversation's retention fields map normatively onto persisted
    turn content kinds: `transcript_text` governs persisted `kind:
    "transcript"` content blocks on text turns, and `voice_transcript_text`
    governs persisted `kind: "transcript"` content blocks on voice-input
    turns; a value of `metadata_only` means NO transcript content block may
    persist for that kind — only bounded counters and metadata (for example
    `transcript_chars` on voice events) survive. The cross-record lineage
    invariants — exactly one null-parent root per conversation,
    `(parent_turn_id, sibling_index)` uniqueness, parent existence in the
    same conversation, and acyclicity — are schema-inexpressible and are
    enforced by the Workbench hub store at append time, fail-closed: a
    violating append is refused. Chat records are hub-durable records, not
    bridge-verified snapshots, so they are not contract-digest-bearing.

12. Advanced chat mode extends the chat contract; it never forks it. An
    advanced branch (`advanced-branch.v1`) references an EXISTING
    `conversation_id` and an existing parent turn, and its own turns are
    ordinary `chat-turn.v1` records under the same `(parent_turn_id,
    sibling_index)` lineage — the branch record carries no conversation-identity
    field and no turns/messages/transcript array, and its `advbranch_` id is
    grammatically disjoint from a `conv_` identity, so a branch can never mint a
    parallel transcript. A control is submittable only when the pinned route
    capability declares it with a type, bounds/allowed values, and a default,
    and the capability itself carries the route and profile digests; an
    undeclared, out-of-bounds, or policy-owned-overridden control is refused by
    `workbench.contracts.validate_advanced_branch`. The trace/export shape
    (`advanced-trace.v1`) is redaction-only: every object is closed and there is
    no field for a credential, a raw header, hidden/encrypted reasoning, a
    filesystem path, or an unredacted provider/tool payload — a tool result
    carries a digest and character count, never raw output. A preset
    (`advanced-preset.v1`) pins exact route/profile/tool digests and is
    digest-bearing (`preset_digest`, excluding the volatile repair block); on a
    live-digest drift `workbench.contracts.validate_advanced_preset` forces the
    deterministic `repair_required` state listing exactly the drifted
    references and never silently substitutes a route or tool. A comparison
    (`advanced-comparison.v1`) reports factual integer metrics over 2–4 existing
    sibling turns and can express a ranking only alongside a named declared
    criterion, so no winner is representable without its criterion. Only
    deterministic mock and installed read-only tool kinds are representable in
    any Advanced resource; there is no effectful, plugin-lifecycle, generic-HTTP,
    or arbitrary-schema tool. Advanced records are hub-durable records and carry
    no delivery or State authority.

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
