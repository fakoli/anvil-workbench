# Anvil Workbench — Independent Product & UX Review

Date: 2026-07-22 · Reviewer: independent (read-only pass over docs + code)
Scope: vision-vs-reality assessment + UX/IA critique. Evidence is cited to files;
opinion is labelled INFERENCE.

---

## 1. Product thesis

**EVIDENCE.** `README.md:3` and `AGENTS.md:3` state it plainly: Workbench is "a
private, tailnet-first agent harness for software delivery" whose web UI moves one
project "from a PRD and Anvil State task plan through a local Codex implementation,
evidence review, approved GitHub PR, merge, and State acceptance." The defining
move is deliberate non-ownership (`README.md:9-12`, the CLAUDE.md "Choose the right
layer" table): **Anvil State** is canonical for PRDs/tasks/evidence/acceptance,
**Anvil Serving** owns model policy, the **project bridge** owns the worktree and
execution, **GitHub** owns merges, **Neo4j** is a read-only projection. Workbench
owns only "durable supervision, redacted visibility, approvals, workflow snapshots,
and reconciliation" (CLAUDE.md).

**INFERENCE.** This is a *crisp, disciplined thesis, not a grab-bag* — and that is
the single most impressive thing about the project. Most "AI delivery" products
sprawl because they try to own everything; Workbench inverts that and defines
itself by what it refuses to touch. The one-paragraph version: *a supervision plane
that watches a less-capable coding agent drive a real repo to a merged PR, and
whose entire job is to make every irreversible step gated, hash-bound, redacted,
and reconcilable — while never holding the credentials, the worktree, or the
canonical state that would let it cheat.* That is a real product idea with a real
reason to exist. The danger built into the thesis is that "owns almost nothing" sits
one step from "does almost nothing," and which one it is depends entirely on whether
the supervised loop actually runs.

---

## 2. The central tension: safe orchestrator, or read-only dashboard?

This is the make-or-break question, so I will take a clear position.

**EVIDENCE.** The boundary discipline is genuinely well-engineered. The approval
model is one-time, hash-bound, bridge-scoped, and binds run/worktree/lease/PR-head
(`docs/CONTRACTS.md:138-148`). Leases are fenced and expiring with a preflight
recheck before every effect (`CONTRACTS.md:116-119`). Completion is defined to be
impossible without both a real merge and a State acceptance (`AGENTS.md:37`,
`CONTRACTS.md:102`). None of this is decoration — the hermetic suite proves each
gate fails closed with a typed code and dispatches no effect
(`QUALIFICATION.md:236-243`).

But the same documents are unusually honest that the *effect side* is not wired.
`dispatch_with_run_context` "has no production caller yet" (`CONTRACTS.md:56`,
`SESSION-HANDOFF.md:144`). The delivery-projection, run-context, project-context,
advanced-dispatch, and voice-relay routers are all "inject-or-503" — they return
503 until a real backend is supplied (`UI-ACCEPTANCE-AUDIT.md:70-75`). The live
Deliver flow "correctly fails closed (`deliver_no_session`)" because no bridge is
registered (`QUALIFICATION.md:182`).

**INFERENCE / POSITION.** *Today, Workbench is closer to a read-only dashboard plus
an approval console than to a live orchestrator — because the orchestration is the
exact part that does not run yet.* Everything a user can actually do live is either
read (Explorer render, Routes, Plugins catalog, Runs list) or a side-channel that
bypasses the delivery loop (Sandbox one-shot, Chat, a Settings write). The
machinery that would make it "powerful safe orchestration" — the leased bridge run,
the hash-bound PR, the compare-and-swap merge, the State accept — has never executed
once end-to-end (`QUALIFICATION.md:318-328`, "Deliberately not exercised").

That does not make the boundary discipline wrong; it makes it *unproven*. The
design is the best part of the product and the most at-risk part, because a fenced
lease and a hash-bound approval only earn their complexity when there is a real
irreversible effect on the other side of them. Right now the elaborate safety
apparatus guards a door that has not been installed. The verdict is not "this is a
dashboard forever" — it is "this is a dashboard until one real task merges under
supervision, and that event is the entire product risk."

---

## 3. Vision vs. reality gap

**EVIDENCE.** Map the promised loop — PRD → plan → task → deliver → bridge run →
evidence → approval → merge → State-accept — against `QUALIFICATION.md`
(2026-07-22 live section) and its per-task dispositions:

| Loop stage | Live status | Evidence |
| --- | --- | --- |
| PRD content in Explorer | **BLOCKED** (fails closed) | fakoli/anvil#178: anvil 0.6.0 "advertises no operation catalog" (`QUAL:182-185`) |
| Plan/task hierarchy render | **LIVE** (names/ids only, no live PRD body) | `QUAL:171-176` |
| Deliver-from-task | **fails closed** (`deliver_no_session`, no bridge) | `QUAL:181-182` |
| Bridge run (Codex) | **BLOCKED** — no live bridge registered | `QUAL:256-283` |
| Evidence submission | **BLOCKED** | hermetic only |
| Approval (hash-bound) | hermetic only; never consumed live | `QUAL:318-322` |
| Merge + State-accept | **never executed** | `QUAL:318-321` |

What *is* LIVE (2026-07-22, real tailnet stack): multi-turn streamed Chat on Fast +
Heavy routes; request/response voice (STT transcribe + TTS read-aloud) through the
hub; realtime voice **transport only** (connected, no operator-confirmed audible
round-trip); one personal Settings write; a Sandbox Responses turn; Plugins catalog
served; Explorer/Runs render (`QUAL:73-79`).

The scoreboard: "**101 of 108 tasks `done`** ... the remaining **7 are
BLOCKED-LIVE**" (`QUAL:210-211`) — and those 7 are precisely the per-PRD
*delivery/live-identity finales* (`QUAL:256-283`).

**INFERENCE.** The headline promise — *supervise a PRD-to-merged-change loop* — is
the one thing not deliverable today. 101/108 sounds like ~94% done, but the 7
blocked tasks are not a long-tail remainder; they are the load-bearing finale of
every PRD. The product has built the entire perimeter (contracts, stores, gates,
browser surfaces at component level) and none of the center. There is also a
*deeper-than-infra* blocker that the docs flag honestly and that I want to amplify:
even on the 2026-07-19 live stack the pinned Heavy model emitted
`shell_command<|channel|>commentary` that "Codex cannot execute locally," so the
bridge correctly refused to submit evidence (`QUAL:312-316`). That means the loop is
blocked not only by "no bridge/tailnet identity" but by "no local model/template
yet emits executable function calls throughout." A harness whose runner model cannot
drive a tool loop has no runner — this is arguably a bigger risk than the missing
infra, because infra is a provisioning task and model-executability is a
compatibility unknown.

Credit where due: the qualification ethos is exemplary. The three dispositions
(LIVE / LIVE-PARTIAL / STILL-BLOCKED), the refusal to promote any blocked task, and
lines like "This document still claims no delivery-harness, PR-merge, State-apply,
or tailnet-identity qualification" (`QUAL:22-23`) are the kind of honesty most teams
never write down. The gap is real but it is *not hidden* — it is the most
well-documented thing in the repo.

---

## 4. Information architecture & UX

**EVIDENCE.** The left rail (`App.jsx:43-46`) has **13** top-level surfaces: Chat,
Voice, Delivery, Explorer, Sessions, Runs, Routes, Approvals, Settings, Evidence,
Skills, Plugins, Sandbox (Configuration is a sub-panel sharing the Settings tab,
`App.jsx:2053-2054`). The comment on the nav array admits the ordering is itself a
churn point: "Chat is first and selected by default; Delivery stays reachable
directly below it (chat-first-voice T004.4)."

Overlaps I can see directly in the code:

- **Delivery vs Explorer.** `Delivery` (`App.jsx:503`) shows a project's active
  task + a "Deliver next task" sheet; `ExplorerView` (`App.jsx:1858`) shows the
  PRD→task hierarchy with per-task eligibility and its own read-PRD/open-task flow.
  Both are "browse the plan and act on a task." The `DeliverSheet` even notes it
  duplicates Explorer's data source (`fetchPrdTasks`/`fetchTaskEligibility`,
  `App.jsx:554-556`).
- **Sessions vs Runs.** `SessionsView` (`App.jsx:526`) lists durable sessions with
  a "Start delivery" button; `RunsView` (`App.jsx:528`) lists the runs those
  sessions produce. To an operator these are two views of the same object graph.
- **Trace panel vs Approvals/Evidence/Routes tabs.** The Delivery cockpit's `Trace`
  aside (`App.jsx:518-521`) *already* surfaces the selected approval (with
  authorize button), an Evidence mini-panel, and route/skill status — yet
  Approvals, Evidence, and Routes are also standalone tabs. `selectApproval`
  navigates the user *away from* the Approvals tab back to Delivery to authorize
  (`App.jsx:2043`), so the approve action lives in two places.
- **Voice.** A code comment records exactly the drift the operator flagged: the
  realtime relay was "promoted from a panel bolted onto Delivery into its own
  voice-first page" (`App.jsx:133-136`).

**INFERENCE.** The IA is a **panel grab-bag that mirrors the backend capability
map and the six delivery PRDs, not the operator's journey.** Each milestone PRD
(chat-first-voice, advanced-model-playground, plan-task-delivery,
reviewed-tools-plugins, preferences-configuration, state-context-operations) appears
to have deposited one or more tabs, and the nav accreted rather than being
designed. The Voice-out-of-Delivery move is not a one-off; it is the visible symptom
of surfaces being placed by *which subsystem built them* rather than *when the user
needs them*.

Who is the user? A single tailnet owner/operator supervising one project's delivery
(`ProfileMenu`: "Allowlisted operator ... tailnet owner", `App.jsx:806`). For that
person the actual journey is roughly: *pick a ready task → deliver it → watch the
bridge run → review redacted evidence → authorize the PR → authorize merge →
confirm State accept*. That is four conceptual places (plan, run/trace, evidence,
approval), yet the rail offers thirteen. Chat, Voice, Sandbox, and Advanced
playground are a *second product* (a private model console) grafted onto the
delivery harness and sharing its shell — defensible as "the operator also wants to
talk to the model," but they roughly double the nav and dilute the delivery story
that is supposed to be the point.

Concrete friction: (1) the approve action's dual home (Approvals tab vs Delivery
Trace) will confuse a first-time operator about where authorization "really"
happens; (2) Delivery and Explorer will feel like two answers to "where is my
plan?"; (3) Sessions and Runs force the user to understand an internal
session/run/workflow distinction that the product's own thesis says it owns on the
user's behalf.

---

## 5. Standout strengths

These are genuinely good and, in a couple of cases, novel:

- **Adversarial-gate delivery discipline.** The fail-closed matrix (stale catalog
  digest, lost/expired/fenced lease, changed packet, replayed approval, unprofiled
  capability, unknown outcome) each refuses with a member of a *closed*
  `OPERATION_REFUSAL_CODES` set and dispatches no effect (`QUAL:236`). The handoff
  notes "Every gate that ran found at least one real defect a green suite had
  missed; zero upheld false positives" (`SESSION-HANDOFF.md:449-450`) — that is a
  process that actually catches things.
- **Redaction / no-leak boundary.** A proven leak corpus (AKIA, JWT, PEM, `ip:port`,
  dotless `host:port`, `postgres://`, `state.db`, UNC/`~` paths) scrubbed at
  *construction time and re-scrubbed at the serialized API boundary*, so even a
  rogue duck-typed `as_dict()` is scrubbed (`QUAL:237`, `CONTRACTS.md:109`).
  Defence-in-depth done properly.
- **One-time hash-bound approval + typed receipt.** Binds canonical payload hash,
  action, bridge, run, worktree, lease epoch, and PR head SHA; any changed diff/head
  fails closed into reconciliation (`CONTRACTS.md:148`). This is the correct shape
  for irreversible effects and it is rare to see it specified this tightly.
- **"Build for a less-capable runtime model via deterministic typed context."**
  `RUNTIME-INFERENCE.md` is the most intellectually interesting artifact in the
  repo: give the model "better state and smaller choices, not unrestricted tools"
  (line 8-9); a trusted/untrusted split where PRDs, repo files, and tool output are
  "untrusted task data" that "cannot add an operation, change a route, relax a
  budget, reveal a credential, or override an approval" (lines 57-64). This is a
  genuinely good answer to prompt injection and to weak-model reliability, and the
  RunContext already serializes into separate `trusted`/`untrusted` structures
  (`CONTRACTS.md:52`).
- **Identity / tailnet posture.** No worktree or GitHub credential in the hub; an
  identity-aware proxy injects the actor header, client copies stripped, browser
  never sees a token (`README.md:14-18`, `AGENTS.md:20`).
- **Honest local-vs-live ethos.** Covered above; worth restating as a *strength* —
  the QUALIFICATION/UI-ACCEPTANCE docs are a model of not overclaiming.

---

## 6. Risks & weaknesses

- **The apparatus-to-liveness ratio is inverted.** An enormous amount of typed
  contract, schema, and fail-closed machinery exists with no live caller: the whole
  typed-operation spine and `dispatch_with_run_context` (no production caller,
  `CONTRACTS.md:56`), the advanced run/dispatch path (unwired, 503, `QUAL:156-168`),
  a v2 "workflow operation layer" that is entirely proposed (`CLAUDE.md` V2 section,
  `docs/CONTRACTS.md:158-201` — six "proposed contract resources, not implemented
  endpoints" blocks). The team is hardening and versioning doors before the happy
  path that walks through them exists. RUNTIME-INFERENCE.md — the best design — is
  status "Proposed implementation guide" and none of it is wired.
- **Over-engineering signals.** RunContext deliberately carries *two*
  non-substitutable schemas (`workbench-run-context-internal/v1` vs the published
  flat `workbench-run-context/v1`, `CONTRACTS.md:54`) — a subtlety that only pays
  off once something consumes it. A follow-up backlog of adversarial-gate findings
  (case-smuggled `host:port`, missing `Cookie` in the corpus, un-gated project-scope
  writes, inline `-c` verification outside the drift gate — `SESSION-HANDOFF.md:175-190`)
  shows the gates are being polished faster than the loop they guard is being built.
- **Hard dependence on unbuilt upstreams.** The core loop is gated on other repos:
  **fakoli/anvil#178** (no operation catalog from `anvil describe`) blocks Explorer
  live PRD content *and* the delivery-projection seed (`QUAL:182-185`, `256-261`);
  **fakoli/anvil-serving#280** (unified `/v1/audio/*` gateway) and **#281**
  (realtime transcript text) gate voice completeness. The product cannot finish its
  headline flow on its own schedule.
- **Model executability is an open compatibility risk, not just an infra gap.** The
  pinned Heavy model's `shell_command<|channel|>commentary` output is not executable
  by Codex (`QUAL:312-316`). Until a local model/template is qualified to emit
  executable function calls throughout, the harness has no working runner even with
  perfect infra.
- **503-until-injected surfaces read as hollow to an operator.** Advanced
  run/dispatch, delivery projection, run-context, project-context, and voice-relay
  all 503 by default (`UI-ACCEPTANCE-AUDIT.md:70-75`). A real operator opening these
  tabs on a fresh hub sees dead panels unless every env var and backend is wired.
- **UX confusions** (from §4): approve-action dual path; Delivery/Explorer and
  Sessions/Runs overlaps; 13-item nav; Voice's recent relocation.

---

## 7. Prioritized recommendations

Ranked by leverage on making the product *real and coherent*.

1. **Close ONE vertical slice of the real loop, end to end, before building
   anything else.** A single ready State task → leased bridge run → evidence →
   hash-bound approval → PR → compare-and-swap merge → State accept, against a
   disposable repo and a real (or stub) bridge. The entire product thesis is
   unproven until one task merges under supervision. This retires more risk than any
   other single move and converts "elaborate dashboard" into "orchestrator." Follow
   the repo's own requalification recipe (`QUAL:330-346`).

2. **Resolve the model-executability blocker in parallel — it may gate #1.** Qualify
   a local model/template that emits executable function calls through a full
   Codex `exec` loop (`QUAL:337-338`). Without this the bridge run in #1 cannot pass,
   so treat it as co-critical, not sequential.

3. **Collapse the IA around the operator journey; target ~5-6 primary surfaces.**
   Merge Delivery + Explorer into one "Plan & Deliver" surface (they already share a
   data source). Merge Sessions + Runs into one "Runs" surface with sessions as
   grouping. Demote Routes, Evidence, Skills, Sandbox to secondary/inspection (or
   fold Routes+Evidence into the run Trace, where they already partly live). Give the
   approve action exactly one home. Decide whether the model-console cluster (Chat,
   Voice, Advanced, Sandbox) is a co-equal second product or a supporting drawer —
   and if co-equal, separate it visually from the delivery rail so the delivery
   story is not diluted.

4. **Freeze the proposed v2 / contract apparatus until it has a live caller.** Stop
   expanding the operation-layer schemas and the fail-closed matrix; instead wire
   `dispatch_with_run_context` and the typed-operation spine onto the live bridge
   path as part of #1, or explicitly shelve them. Polishing gates
   (`SESSION-HANDOFF.md:175-190`) ahead of the loop is negative-leverage work right
   now.

5. **Make 503-until-injected surfaces degrade honestly and legibly.** Every
   inject-or-503 tab should render a first-class "not configured — here is the exact
   env var / backend to wire" state (the Sandbox and Voice unconfigured panels
   already do this well; extend that pattern to Advanced, delivery projection,
   run-context). This turns hollow panels into an onboarding checklist and matches
   the product's otherwise excellent honesty ethos.

---

### One-line bottom line

A rare, *disciplined* product idea with best-in-class honesty and safety
engineering — whose single defining feature, the supervised delivery loop, is the
one thing that has never run; the next unit of work should be closing that loop
once, not building the thirteenth surface or the second contract schema.
