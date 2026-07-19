# Anvil Workbench project brief

## Product promise

Anvil Workbench is the private, tailnet-first agent harness for one project’s path from a product requirement to a merged, evidenced change. The web UI is its primary operator entry point. It makes the model route, local agent activity, evidence, and approval gates visible without taking authority away from the systems that already own them.

The v1 path is:

```text
PRD -> Anvil State plan -> claimed task -> local Codex run -> evidence
    -> hash-bound PR approval -> bridge-created GitHub PR -> merge
    -> State acceptance -> completed delivery
```

If either the merge or State acceptance fails, Workbench records a reconciliation item instead of claiming success.

## Product ownership

| System | Owns | Does not own |
| --- | --- | --- |
| Anvil State | PRDs, tasks, claims, evidence, acceptance, canonical event history | UI sessions, model routing, GitHub execution |
| Anvil Serving | Local model routes, tool path, quality/evaluation evidence, request correlation | Task transitions, browser approvals, GitHub credentials |
| Workbench hub | UI, run supervision, redacted activity, approval/audit records, bridge registry | Project worktrees, provider credentials, State database |
| Project bridge | Local worktree, Codex subprocess, State CLI/event reader, GitHub actions | Cross-project control, browser identity, global graph authority |
| Neo4j | Derived redacted evidence, retrieval, lineage, related-failure lookup | Canonical task state, approval, generic agent database |

## Users and job to be done

- **Owner:** starts a delivery, sees status and evidence, and assigns approvers.
- **Approver:** authorizes a specific PR, merge, State apply, deployment, or model/policy action only after seeing the exact payload hash and context.
- **Operator:** connects the hub to a tailnet identity proxy, Anvil Serving, State-aware bridge, and local GitHub credentials.
- **Future coding session:** can begin with the session handoff and contracts, understand the boundaries, then work on a clearly scoped milestone.

## V1 surface

- Delivery, Runs, Routes, Approvals, Evidence, and a secondary Sandbox UI.
- Durable, concurrent harness sessions: one active run per session, a named worktree lease, a version-pinned workflow cursor, and an append-only redacted event sequence.
- An allowlisted workflow vocabulary (`agent`, `tool`, `condition`, `fan_out`, `join`, `approval_wait`, `evidence_submit`, `reconcile`, `cancel`), not arbitrary model-authored executable graphs.
- Session-bound push-to-talk through a Workbench relay to private Anvil Voice Realtime; raw audio is never retained and transcript retention is disabled by default.
- Postgres persistence for run state, approvals, audits, bridge registration, and redacted transcripts.
- A project-local bridge that polls the hub, tails canonical State events, invokes Codex against Anvil Serving, and carries out approved local GitHub actions.
- Narrow graph tools for evidence search with citations, task-to-PR lineage, model/profile rationale, and related-failure lookup.

## Explicit non-goals

- Replacing Anvil State’s task model or direct access to its database.
- Replacing Anvil Serving’s router, policy, or model catalog.
- Exposing generic database or Cypher access to a model.
- Keeping raw credentials or raw unredacted transcripts in the browser or graph.
- Automatically creating a PR, merging, applying State acceptance, deploying, or changing model policy.
