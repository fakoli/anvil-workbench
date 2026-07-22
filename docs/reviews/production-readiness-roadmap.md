# Anvil Workbench — Production-Readiness Roadmap

Date: 2026-07-22 · Synthesized from the independent UX review (`scratchpad/product-ux-review.md`)
and strategic SWOT (`scratchpad/swot-analysis.md`), ground-verified against
`docs/QUALIFICATION.md`, `docs/SESSION-HANDOFF.md`, `docs/CONTRACTS.md`, `README.md`,
`AGENTS.md`, and `workbench/api.py` route prefixes.

Audience: **multiple independent LLM/coding agents**. Every item below is
self-contained — pick one by `ID`, read `problem` → `recommendation` → `affected`,
check `dependencies`, and stop when `acceptance` is verifiably true. The end-of-file
JSON manifest is the machine-parseable dispatch index.

---

## 1. Orientation

**Production thesis.** Workbench is a supervision-and-governance plane for a delivery
loop it deliberately does not own (State owns canonical project state, Serving owns
model policy, the bridge owns the worktree and credentials). Its engineering —
hash-bound one-time approvals, fenced leases, defense-in-depth redaction, a fail-closed
adversarial gate, and best-in-class LIVE/BLOCKED honesty — is excellent. But its one
defining move, *deliver a real change under governance with a credential-less,
less-capable runner*, **has never run once end-to-end** (`QUALIFICATION.md` live
section; `SESSION-HANDOFF.md:90` live Deliver fails closed `deliver_no_session`). Until
it does, Workbench is a superbly-built dashboard-plus-approval-console guarding a door
that has not been installed — and Anvil State already ships a working PRD→apply loop
with a capable in-session agent that needs none of this.

**Single highest-leverage priority.** Close **ONE** vertical slice of the real loop
end-to-end (task → leased bridge run → evidence → hash-bound approval → PR →
compare-and-swap merge → State-accept) against a disposable repo, and resolve
**model-executability co-critically** — the pinned Heavy model emits
non-executable `shell_command<|channel|>commentary` (`SESSION-HANDOFF.md:465`;
`QUALIFICATION.md` 2026-07-19), so there is no working runner even with perfect infra.
Everything else is P1/P2 and most of it should wait. **Freeze** the proposed v2
operation-layer apparatus and the gate-polishing backlog until a live caller exists.

---

## 2. P0 — Blocks production

The loop-closure cluster is deliberately decomposed so different agents can own
adjacent legs. `LOOP-*` are sequential; `MODEL-*` and `EXT-178` run in parallel and
gate the run leg.

### LOOP-01 — Stand up a disposable end-to-end vertical-slice rig
- `type`: FEATURE · `priority`: P0 · `effort`: M
- `problem`: No harness exists to run one real supervised delivery end-to-end; the live
  Deliver flow fails closed with `deliver_no_session` because no bridge is registered
  (`SESSION-HANDOFF.md:90`, `QUALIFICATION.md` live section).
- `recommendation`: Assemble a reproducible rig: a disposable GitHub repo, one local
  Serving route, a registered project bridge (real or a faithful stub honoring the
  poll/packet/lease/evidence contract), and a seeded State task. Script it so the whole
  slice can be re-run per the repo's own requalification recipe (`QUALIFICATION.md`
  requalification steps).
- `affected`: new `tests/` or `scripts/` harness; `workbench/bridge.py`,
  `workbench/cli.py`; `docker-compose.yml`/`.env.example`; disposable repo.
- `dependencies`: none
- `acceptance`: A single documented command provisions the rig and reaches the point of
  registering a bridge and seeding a task; a subsequent `Deliver` no longer returns
  `deliver_no_session` but a real accepted start receipt.

### LOOP-02 — Wire `dispatch_with_run_context` to a production caller
- `type`: ARCH · `priority`: P0 · `effort`: M
- `problem`: The capture-before-dispatch spine exists but `dispatch_with_run_context`
  "has no production caller yet" (`CONTRACTS.md:56`, `SESSION-HANDOFF.md:144,322`) —
  the apparatus-to-liveness inversion at the core of both reviews.
- `recommendation`: Wire the live bridge poll/queue loop to resolve→persist→dispatch a
  `RunContext` through `dispatch_with_run_context` before any effect, feeding the
  trusted/untrusted split into the packet. Do NOT expand the schema; connect the
  existing one.
- `affected`: `workbench/run_context_store.py`, `workbench/bridge.py`,
  `workbench/api.py` (queue/poll path), `workbench/models.py`.
- `dependencies`: LOOP-01
- `acceptance`: A run started via the live path produces a persisted immutable
  `RunContext` for `(project_id, run_id)` readable at
  `GET /api/projects/{id}/runs/{run_id}/context`, and a context that cannot resolve
  required authority fails closed with no dispatched effect (integration test proves
  both).

### LOOP-03 — Execute one leased bridge Codex run → real evidence submission
- `type`: FEATURE · `priority`: P0 · `effort`: L
- `problem`: No leased bridge Codex run has ever produced a real evidence submission;
  the bridge correctly refuses because the model output is non-executable
  (`QUALIFICATION.md` 2026-07-19; `SESSION-HANDOFF.md:465`).
- `recommendation`: Drive one packet through the fenced-lease Codex `exec` loop with
  lease preflight-and-renew before the effect, capturing an independent typed evidence
  receipt (git tree snapshot incl. untracked files). Depends on a runner that emits
  executable calls (MODEL-01).
- `affected`: `workbench/bridge.py`, `workbench/router.py`, `workbench/retrieval.py`
  (evidence), lease logic.
- `dependencies`: LOOP-01, LOOP-02, MODEL-01
- `acceptance`: One task run yields a persisted redacted evidence record from a real
  edit+test cycle; lease is re-checked immediately before the effect and the run halts
  if it cannot renew (test proves the fence).

### LOOP-04 — Consume one hash-bound approval live → open a real PR
- `type`: FEATURE · `priority`: P0 · `effort`: M
- `problem`: The one-time hash-bound approval is proven hermetically but "never consumed
  live" (`QUALIFICATION.md` deliberately-not-exercised section); no PR has been opened
  under a consumed approval.
- `recommendation`: Bind the canonical payload hash + run + worktree + lease epoch +
  task id + PR-head SHA, consume the approval exactly once, and open the GitHub PR from
  the bridge (never from the hub). A changed diff/head must fail closed into
  reconciliation.
- `affected`: `workbench/store.py` (approval consume), `workbench/bridge.py` (GitHub),
  `workbench/api.py` approvals path, `web/` approval UI.
- `dependencies`: LOOP-03
- `acceptance`: A single approval authorizes exactly one PR open; a replay or an altered
  diff/head is refused with a typed reconciliation code and opens no second PR
  (live-run log + test).

### LOOP-05 — Compare-and-swap merge + State-accept, end to end, once
- `type`: FEATURE · `priority`: P0 · `effort`: M
- `problem`: No compare-and-swap merge or State acceptance has ever executed
  (`QUALIFICATION.md`); completion is *defined* to require both a real merge and a State
  accept (`AGENTS.md:37`, `CONTRACTS.md:102`), and neither has happened.
- `recommendation`: Perform the CAS merge (fail closed if head moved), then the State
  acceptance strictly AFTER the merge succeeds, then requalify. This is the event that
  converts "dashboard" into "orchestrator."
- `affected`: `workbench/bridge.py` (merge + State CLI/MCP), `workbench/store.py`,
  `docs/QUALIFICATION.md` (record the live pass).
- `dependencies`: LOOP-04, EXT-178
- `acceptance`: One disposable-repo task reaches `merged` + State `accepted` via the
  supervised path; QUALIFICATION.md records a dated LIVE delivery-harness pass; a
  head-moved merge attempt fails closed with no acceptance written.

### MODEL-01 — Qualify a local model/template that emits executable function calls
- `type`: FIX · `priority`: P0 · `effort`: L
- `problem`: The pinned Heavy model emits `shell_command<|channel|>commentary` that
  Codex cannot execute, so the harness has no working runner even with perfect infra
  (`QUALIFICATION.md` 2026-07-19; `SESSION-HANDOFF.md:465`). This is a compatibility
  unknown, not a provisioning task — arguably the single biggest risk in the stack.
- `recommendation`: Qualify at least one local model + chat template that emits
  executable function/tool calls through a FULL Codex `exec` loop (edit→test→evidence).
  Adjust the served template or swap the model; pin the qualified combination.
- `affected`: Serving route/template config, `workbench/router.py`, bridge Codex launch;
  `docs/QUALIFICATION.md`.
- `dependencies`: none (parallel to LOOP-01/02)
- `acceptance`: A recorded local run drives a multi-step Codex `exec` loop that produces
  an executable tool call, applies an edit, runs a test, and submits evidence — with the
  model/template pinned and documented.

### MODEL-02 — Build a repeatable executable-tool-loop qualifier for local models
- `type`: FEATURE · `priority`: P0 · `effort`: M
- `problem`: There is no repeatable screen to tell whether a given local model can drive
  the tool loop; today the failure is only discovered by a full manual live run
  (`QUALIFICATION.md` 2026-07-19).
- `recommendation`: Add a small harness that points a candidate model at a fixed
  Codex-style tool-loop fixture and reports pass/fail on emitting executable calls
  throughout. Use it to screen models before a live delivery attempt, and to fail closed
  legibly at bridge preflight when a run's model is unqualified.
- `affected`: new qualifier under `tests/`/`scripts/`; `workbench/bridge.py` preflight.
- `dependencies`: none
- `acceptance`: Running the qualifier against the current pinned Heavy model reports a
  FAIL with a specific reason (non-executable channel output); running it against the
  MODEL-01 qualified model reports PASS.

### EXT-178 — State operation catalog from `anvil describe` (fakoli/anvil#178)
- `type`: EXTERNAL-DEP · `priority`: P0 · `effort`: M
- `problem`: anvil 0.6.0 "advertises no operation catalog," blocking Explorer live PRD
  content AND the delivery-projection seed (`SESSION-HANDOFF.md:99-100,182-185`).
  Verified against the live CLI: `describe` catalog is not in 0.6.0.
- `recommendation`: Land the operation catalog in State so `anvil describe` returns a
  typed operation list; then seed the delivery projection and Explorer PRD content from
  it. Track as an upstream blocker with a pinned, digest-versioned adapter (see
  ARCH-01).
- `affected`: fakoli/anvil (upstream); `workbench/delivery_projection.py`,
  `workbench/api.py` (delivery projection router), Explorer view.
- `dependencies`: none (upstream)
- `acceptance`: `anvil describe` returns an operation catalog; Workbench's Explorer
  renders live PRD content and the delivery projection seeds from it in the LOOP-01 rig.

### FREEZE-01 — Freeze the v2 operation-layer apparatus and gate-polishing backlog
- `type`: ARCH · `priority`: P0 · `effort`: S
- `problem`: An enormous typed-operation/contract apparatus and six "proposed contract
  resource, not implemented endpoint" blocks exist ahead of any live caller
  (`CONTRACTS.md:56,158-201`); the follow-up backlog (`SESSION-HANDOFF.md:175-190`) is
  gate-polishing — negative-leverage until one task merges. Both reviews call this the
  apparatus-to-liveness inversion.
- `recommendation`: Declare a freeze: no new operation-layer schemas, no new fail-closed
  gate cases, no v2 contract-resource expansion until LOOP-05 passes. Redirect that
  effort into the LOOP-* legs. Security-class backlog items (FIX-03/04/05) are the only
  exceptions.
- `affected`: team process; `docs/CLAUDE.md` V2 section note; `docs/ROADMAP.md`.
- `dependencies`: none
- `acceptance`: A short freeze note is added to the roadmap/handoff; no PR between now
  and LOOP-05 adds a new operation-layer schema or gate case except FIX-03/04/05.

---

## 3. Fixes

### FIX-01 — Wire the Advanced run/dispatch execution path (currently 503)
- `type`: FIX · `priority`: P2 · `effort`: M
- `problem`: The advanced run/dispatch execution path is unwired; no HTTP endpoint
  invokes `advanced_routes`/`advanced_runtime`/`advanced_dispatch`, so a run with no
  route degrades to 503 (`SESSION-HANDOFF.md:87,168-170`).
- `recommendation`: Mount an HTTP endpoint that invokes the advanced runtime/dispatch
  modules with the existing route validation. This is a *console* feature, not the
  delivery loop — do it AFTER LOOP-05 unless it is the live-demo surface.
- `affected`: `workbench/advanced_routes.py`, `workbench/advanced_runtime.py`,
  `workbench/advanced_dispatch.py`, `workbench/api.py`, `web/` advanced playground.
- `dependencies`: FREEZE-01 (do not start before loop closes)
- `acceptance`: A POST to the advanced run endpoint executes a real Responses turn on a
  reviewed route and returns a validated advanced-trace record instead of 503.

### FIX-02 — Redaction case-smuggle: capitalized single-label `host:port`
- `type`: FIX · `priority`: P1 · `effort`: S
- `problem`: The dotless `host:port` pattern `\b[a-z][a-z0-9-]*:\d{2,5}\b` is
  lowercase-anchored and misses a capitalized single-label host like `Serving:8443`
  (`SESSION-HANDOFF.md:177-180`). A real leak class.
- `recommendation`: Anchor the pattern case-insensitively and re-verify no
  over-redaction of `sha256:` digests, timestamps, or `scoped_id`s.
- `affected`: `workbench/redaction.py` (`redact_config_text`), `tests/test_security_contract.py`.
- `dependencies`: none
- `acceptance`: `Serving:8443` is scrubbed; the full proven-leak corpus test passes and
  no `sha256:`/timestamp/`scoped_id` is over-redacted.

### FIX-03 — Add `Cookie`/`Set-Cookie` to the redaction corpus
- `type`: FIX · `priority`: P1 · `effort`: S
- `problem`: `Cookie`/`Set-Cookie` are not in the export/redaction proven-leak corpus or
  the scrubber (`SESSION-HANDOFF.md:181-182`).
- `recommendation`: Add both header shapes to the scrubber and the proven-leak corpus as
  negative assertions.
- `affected`: `workbench/redaction.py`, `tests/test_security_contract.py`.
- `dependencies`: none
- `acceptance`: A seeded `Set-Cookie: …` value is scrubbed at construction and at the
  serialized API boundary; a revert to the old pattern fails the new test.

### FIX-04 — Gate project-scope preference writes by project membership
- `type`: FIX · `priority`: P1 · `effort`: M
- `problem`: A project-scoped preference write on the shared spine is not gated by
  project membership (`SESSION-HANDOFF.md:183-185`) — a real multi-tenant safety gap and
  a blocker for the governance-buyer opportunity.
- `recommendation`: Add a membership check before any project-scoped write; fail closed
  (no existence oracle) when the actor is not a member.
- `affected`: `workbench/api.py` (`/api/preferences` router), preference store, policy gate.
- `dependencies`: none
- `acceptance`: A non-member project-scoped `PUT /api/preferences/{project}...` is
  refused indistinguishably from a missing project; a member's write succeeds (test
  proves both).

### FIX-05 — Extend the script-drift gate to inline `-c`/`-e` verification
- `type`: FIX · `priority`: P2 · `effort`: M
- `problem`: The drift gate checks a resolvable script-file operand only; inline
  `python -c "…"` / `node -e "…"` verification has no file to check
  (`SESSION-HANDOFF.md:186-190`).
- `recommendation`: At minimum document the operator note (declare inline verification
  code in the packet); ideally extend the gate to hash/drift-check inline code bodies.
- `affected`: drift-gate logic (T008 gate), `workbench/bridge.py`, packet schema docs.
- `dependencies`: FREEZE-01 (gate work is frozen; ship the doc note now, gate extension after LOOP-05)
- `acceptance`: The packet contract documents inline verification declaration; a changed
  inline verification body is either drift-detected or explicitly documented as
  out-of-gate with the operator note present.

### FIX-06 — Implement the `RelayEvent` `resolution` kind for `/send`
- `type`: FIX · `priority`: P2 · `effort`: S
- `problem`: `RelayEvent` emits only `delta`/`terminal`; the `resolution` kind is
  docstring-only future work, so the `/api/conversations/{id}/send` resolution join is
  incomplete (`SESSION-HANDOFF.md:171-173`).
- `recommendation`: Add the `resolution` frame kind and join it in the send/stream path.
- `affected`: `workbench/chat_stream.py`, `workbench/conversation_api.py`, `web/chat-api.js`.
- `dependencies`: none
- `acceptance`: A `/send` stream emits a `resolution` frame that the web reducer
  consumes; a test asserts the frame ordering and terminal immutability.

---

## 4. Low-hanging fruit

### LHF-01 — Add `route` to the `/api/bootstrap` voice block
- `type`: LOW-HANGING-FRUIT · `priority`: P2 · `effort`: S
- `problem`: The bootstrap `voice` block emits `available`/`transport`/
  `retains_transcripts` but no `route` (`workbench/api.py:2363-2367`), so the Voice tab
  shows a generic "fast route" instead of the actual configured realtime route.
- `recommendation`: Include the configured realtime route id in the bootstrap voice
  payload and render it in the Voice view.
- `affected`: `workbench/api.py` (`bootstrap`), `web/src/App.jsx` (Voice view).
- `dependencies`: none
- `acceptance`: The Voice tab displays the actual configured route id from bootstrap; a
  test asserts the `voice.route` field is present and scrubbed.

### LHF-02 — First-class "not configured — wire X" state for every inject-or-503 surface
- `type`: LOW-HANGING-FRUIT · `priority`: P1 · `effort`: M
- `problem`: Advanced run/dispatch, delivery projection, run-context, project-context,
  and voice-relay all 503 by default (`UI-ACCEPTANCE-AUDIT.md:70-75`), rendering as dead
  panels on a fresh hub — at odds with the product's honesty ethos.
- `recommendation`: Extend the pattern the Sandbox and Voice unconfigured panels already
  use — render a first-class "not configured, here is the exact env var/backend to wire"
  state — to Advanced, delivery projection, run-context, and project-context.
- `affected`: `web/src/App.jsx` and the per-surface views; the 503 detail strings in
  `workbench/api.py` routers.
- `dependencies`: none
- `acceptance`: Opening each inject-or-503 tab on an unconfigured hub shows a named
  config-checklist panel (env var/backend) instead of a raw error or blank panel.

---

## 5. Information architecture

Target ~5-6 primary surfaces organized around the operator journey (pick task →
deliver → watch run/trace → review evidence → authorize → confirm accept), not the
backend capability map. All IA items depend on FREEZE-01 not blocking web work
(it does not — freeze is scoped to the operation-layer apparatus, not the UI).

### IA-01 — Merge Delivery + Explorer into one "Plan & Deliver" surface
- `type`: ARCH · `priority`: P1 · `effort`: M
- `problem`: `Delivery` and `ExplorerView` are two answers to "where is my plan?" — both
  browse the plan and act on a task, and `DeliverSheet` already shares Explorer's data
  source (`fetchPrdTasks`/`fetchTaskEligibility`) (`App.jsx:503,554-556,1858`).
- `recommendation`: Collapse them into one surface: the PRD→task hierarchy with per-task
  eligibility and a single Deliver affordance.
- `affected`: `web/src/App.jsx`, `web/src/delivery-explorer.js`.
- `dependencies`: none
- `acceptance`: The left rail has one plan surface, not two; delivering from a task and
  browsing the hierarchy happen in the same view; web tests pass.

### IA-02 — Merge Sessions + Runs into one "Runs" surface
- `type`: ARCH · `priority`: P1 · `effort`: M
- `problem`: `SessionsView` and `RunsView` are two views of the same object graph and
  force the operator to learn an internal session/run distinction the product claims to
  own on their behalf (`App.jsx:526,528`).
- `recommendation`: One "Runs" surface with sessions as a grouping/filter, not a
  separate tab.
- `affected`: `web/src/App.jsx`.
- `dependencies`: none
- `acceptance`: Sessions is no longer a top-level tab; runs are grouped by session within
  one surface; the "Start delivery" action lives there once.

### IA-03 — Give the approve action exactly one home
- `type`: ARCH · `priority`: P1 · `effort`: S
- `problem`: Authorization lives in two places — the standalone Approvals tab and the
  Delivery Trace aside — and `selectApproval` navigates the user away from Approvals back
  to Delivery to authorize (`App.jsx:2043,518-521`), confusing where approval "really"
  happens.
- `recommendation`: Pick one canonical home (the run Trace is the natural one, where the
  binding context lives) and make the other a read-only link into it.
- `affected`: `web/src/App.jsx`.
- `dependencies`: none
- `acceptance`: The authorize button renders in exactly one surface; the other surface
  links to it read-only; a web test asserts a single authorize control.

### IA-04 — Separate the model-console cluster from the delivery rail
- `type`: ARCH · `priority`: P1 · `effort`: M
- `problem`: Chat/Voice/Sandbox/Advanced are a second product (a private model console)
  grafted onto the delivery harness, roughly doubling the 13-item nav and diluting the
  delivery story (`App.jsx:43-46`; both reviews §4/§WEAKNESSES-6).
- `recommendation`: Visually separate the model-console cluster from the delivery rail
  (a distinct section or a secondary drawer) so the delivery journey reads as the primary
  product; decide explicitly whether the console is co-equal or supporting.
- `affected`: `web/src/App.jsx` (nav array).
- `dependencies`: none
- `acceptance`: The rail groups delivery surfaces separately from the model-console
  surfaces; primary delivery surfaces number ~5-6.

### IA-05 — Demote Routes / Evidence / Skills to inspection sub-panels
- `type`: ARCH · `priority`: P2 · `effort`: M
- `problem`: The Delivery Trace already surfaces the selected approval, an Evidence
  mini-panel, and route/skill status, yet Routes, Evidence, and Skills are also
  standalone top-level tabs (`App.jsx:518-521`).
- `recommendation`: Fold Routes and Evidence into the run Trace (where they partly live);
  demote Skills to an inspection sub-panel.
- `affected`: `web/src/App.jsx`.
- `dependencies`: IA-01, IA-02
- `acceptance`: Routes/Evidence/Skills are no longer top-level tabs; their content is
  reachable from the run Trace; web tests pass.

---

## 6. Features / product

### ARCH-01 — Pinned, digest-versioned contract adapters for State/Serving/Codex/GitHub
- `type`: ARCH · `priority`: P1 · `effort`: L
- `problem`: Workbench is a pure integrator of four moving contracts; State's own docs
  warn packet paths and CLI syntax vary per project (SWOT Threat #4), so a schema bump or
  wire-API change is a silent Workbench outage.
- `recommendation`: Wrap each upstream (State CLI/MCP, Serving Responses/MCP, Codex,
  GitHub) in a pinned, digest-versioned adapter that fails closed and legibly when the
  upstream contract drifts — turning "owns nothing" fragility into a monitored,
  reconcilable dependency (SWOT ST cross-move).
- `affected`: `workbench/bridge.py`, `workbench/router.py`, `workbench/cli.py`; new
  adapter/version-pin module; `docs/CONTRACTS.md`.
- `dependencies`: LOOP-05 (build against the proven live path, not ahead of it)
- `acceptance`: Each upstream adapter carries a pinned contract digest; a simulated
  upstream contract change fails closed with a typed, actionable reconciliation message
  rather than a 500 or a silent wrong result.

### FEAT-01 — Onboarding checklist derived from live hub records
- `type`: FEATURE · `priority`: P2 · `effort`: M
- `problem`: The BYO-everything, tailnet-first setup (tailnet proxy, registered bridge,
  local Serving route, Postgres+Neo4j, per-project State CLI) is a steep first-run wall,
  and every hollow 503 panel makes it feel taller (SWOT Threat #6).
- `recommendation`: Turn the inject-or-503 signals (LHF-02) plus system-health posture
  (`workbench/system_health.py`) into a single onboarding checklist that derives
  completion only from live hub records (as the existing setup guide does).
- `affected`: `web/src/App.jsx`, `workbench/system_health.py`, `workbench/api.py`
  (`/api/system/posture`).
- `dependencies`: LHF-02
- `acceptance`: A fresh hub shows a checklist of the exact remaining config steps, each
  flipping to done from a live hub record; no step is derived from a static assumption.

### FEAT-02 — Governance-buyer packaging via Serving's install base
- `type`: FEATURE · `priority`: P2 · `effort`: M
- `problem`: Workbench's differentiator is governance (credential-absent, hash-bound,
  redacted, multi-operator), not "it delivers" — State already delivers with a capable
  agent (SWOT Threat #2, Opportunity #1/#2). It needs no separate install funnel: Serving
  already packages it (`anvil-serving workbench up`).
- `recommendation`: Position and document Workbench as the governance/audit layer inside
  Serving's stack; lead with the audit story (closed refusal-code set, LIVE/BLOCKED
  discipline) for team/regulated/untrusted-runner buyers.
- `affected`: `README.md`, `docs/PROJECT.md`, Serving `workbench up` integration.
- `dependencies`: LOOP-05, FIX-04 (multi-tenant membership gate)
- `acceptance`: Docs present Workbench as Serving's governance overlay with a concrete
  audit-story section; the `workbench up` path is documented against a proven live loop.

### FEAT-03 — Session-bound voice supervision of a delivery run
- `type`: FEATURE · `priority`: P2 · `effort`: M
- `problem`: Request/response STT+TTS is LIVE and realtime is connected
  (`SESSION-HANDOFF.md:62-102`), but voice is a model-console side-channel, not yet a
  supervision surface — a differentiated interaction few delivery tools attempt (SWOT
  Opportunity #6).
- `recommendation`: Add session-bound push-to-talk supervision ("what's blocked, approve
  the PR") over the already-boundary-safe relay (no model/tool controls, no raw-audio
  storage), gated by the same approval discipline as the UI.
- `affected`: `workbench/voice.py`, `web/` Voice view, approval path.
- `dependencies`: LOOP-04, EXT-281
- `acceptance`: A voice command can surface a run's blocked state and drive an approval
  through the same hash-bound consume path as the UI; no model/tool control or raw audio
  is exposed on the relay.

---

## 7. External dependencies

(EXT-178 is in §2 because it gates the loop.)

### EXT-280 — Unified Anvil Serving `/v1/audio/*` gateway (fakoli/anvil-serving#280) — DONE
- `type`: EXTERNAL-DEP · `priority`: P2 · `effort`: M · `status`: RESOLVED
- `problem`: Request/response voice was wired live via an interim hub-side adapter
  (`workbench/serving_audio.py` `DarkServingAudioTransport`) to the raw Dark STT/TTS
  serves; the clean path was a unified Serving audio gateway.
- `resolution`: #280 is deployed on the router. `voice_relay_service` now uses the
  stock `workbench/voice.py` `ServingVoiceTransport` over
  `workbench/router.py` `voice_transcribe`/`voice_synthesize`, reaching ONLY the
  router base (`ANVIL_ROUTER_BASE_URL` + `ANVIL_ROUTER_TOKEN`) via
  `POST {base}/audio/transcriptions` (`purpose:"stt"`, `audio_b64`) and
  `POST {base}/audio/speech` (`purpose:"tts"`, `response_format`). The interim
  `DarkServingAudioTransport` is removed; the Voice-tab picker still reads
  `ANVIL_VOICE_VOICES_URL` (#280 exposes no `/audio/voices`).
- `affected`: `workbench/router.py`, `workbench/voice.py`, `workbench/deployment.py`,
  `workbench/serving_audio.py` (now catalog-only), `.env.example`, `docker-compose.yml`.
- `acceptance`: STT/TTS route through the unified `/v1/audio/*` gateway; the interim
  adapter is removed and voice STT/TTS still return 200 no-leak through the hub.
  Verified: full pytest green, live gateway round-trip through `ServingVoiceTransport`
  (STT → `{text,is_final,duration_ms}`; TTS → pcm16 @ 24000).

### EXT-281 — Realtime assistant transcript text (fakoli/anvil-serving#281)
- `type`: EXTERNAL-DEP · `priority`: P2 · `effort`: M
- `problem`: The realtime voice relay is LIVE-PARTIAL — connected as transport only, with
  no operator-confirmed audible round-trip and no assistant transcript text
  (`SESSION-HANDOFF.md:76-77`, `QUALIFICATION.md` realtime section).
- `recommendation`: Consume the realtime transcript text from #281 once available to
  complete the realtime supervision surface.
- `affected`: fakoli/anvil-serving (upstream); `workbench/voice.py`, `web/` Voice view.
- `dependencies`: none (upstream)
- `acceptance`: The realtime surface renders assistant transcript text from #281 and an
  operator-confirmed audible round-trip is recorded in QUALIFICATION.md.

---

## 8. Sequencing

**Critical path (unblocks the most):**
`LOOP-01 → LOOP-02 → LOOP-03 → LOOP-04 → LOOP-05`, with `MODEL-01`+`MODEL-02` and
`EXT-178` running in parallel and joining at LOOP-03 (runner) and LOOP-05 (State-accept
content), respectively. **LOOP-05 is the single event that converts "elaborate
dashboard" into "orchestrator"** and retires the deepest weakness (loop never ran) and
threat (State already delivers) simultaneously. Nothing in §6 ARCH-01/FEAT-* should
start before LOOP-05; they are explicitly built against the proven live path.

- **MODEL-01/02 are co-critical, not sequential.** Start them on day one — the runner
  gates LOOP-03, and model-executability is a compatibility unknown, not a wiring task.
- **EXT-178 is upstream and out of Workbench's control** — begin it immediately and, if
  it slips, LOOP-05's State-accept content is the fallback risk; stub the catalog in the
  LOOP-01 rig so the other legs can proceed.
- **Do in parallel, cheaply:** FIX-02, FIX-03 (security scrubber, tiny), LHF-01, LHF-02
  (honesty panels) — these are safe, high-ratio, and independent of the loop.
- **Security-class fixes before any multi-tenant claim:** FIX-04 (membership gate) gates
  FEAT-02.

**FREEZE explicitly (per both reviews):**
- The proposed **v2 workflow operation-layer** schemas and the six "proposed contract
  resource" blocks (`CONTRACTS.md:158-201`) — no expansion until a live caller exists.
- The **adversarial gate-polishing backlog** (`SESSION-HANDOFF.md:175-190`), EXCEPT the
  security-class items FIX-02/03/04 which are cheap real-leak/multi-tenant fixes.
- New top-level UI surfaces — the nav should shrink (§5), not grow.
Redirect all of that effort into the LOOP-* critical path. Polishing doors before the
happy path that walks through them exists is negative-leverage work right now.

---

## 9. Machine-readable manifest

```json
[
  {"id": "LOOP-01", "title": "Stand up a disposable end-to-end vertical-slice rig", "type": "FEATURE", "priority": "P0", "effort": "M", "dependencies": [], "acceptance": "One command provisions the rig, registers a bridge, seeds a task; Deliver returns an accepted start receipt instead of deliver_no_session."},
  {"id": "LOOP-02", "title": "Wire dispatch_with_run_context to a production caller", "type": "ARCH", "priority": "P0", "effort": "M", "dependencies": ["LOOP-01"], "acceptance": "A live-path run persists an immutable RunContext readable at the context endpoint; an unresolvable-authority context fails closed with no dispatched effect."},
  {"id": "LOOP-03", "title": "Execute one leased bridge Codex run to real evidence submission", "type": "FEATURE", "priority": "P0", "effort": "L", "dependencies": ["LOOP-01", "LOOP-02", "MODEL-01"], "acceptance": "One task run yields a persisted redacted evidence record from a real edit+test; lease is re-checked before the effect and halts if it cannot renew."},
  {"id": "LOOP-04", "title": "Consume one hash-bound approval live and open a real PR", "type": "FEATURE", "priority": "P0", "effort": "M", "dependencies": ["LOOP-03"], "acceptance": "One approval authorizes exactly one PR open; a replay or altered diff/head is refused with a typed reconciliation code and opens no second PR."},
  {"id": "LOOP-05", "title": "Compare-and-swap merge plus State-accept, end to end, once", "type": "FEATURE", "priority": "P0", "effort": "M", "dependencies": ["LOOP-04", "EXT-178"], "acceptance": "One disposable-repo task reaches merged + State accepted via the supervised path; QUALIFICATION.md records a dated LIVE pass; a head-moved merge fails closed with no acceptance."},
  {"id": "MODEL-01", "title": "Qualify a local model/template that emits executable function calls", "type": "FIX", "priority": "P0", "effort": "L", "dependencies": [], "acceptance": "A recorded local run drives a full Codex exec loop with executable tool calls, applies an edit, runs a test, submits evidence; the model/template is pinned and documented."},
  {"id": "MODEL-02", "title": "Repeatable executable-tool-loop qualifier for local models", "type": "FEATURE", "priority": "P0", "effort": "M", "dependencies": [], "acceptance": "The qualifier reports FAIL (non-executable channel output) for the current Heavy model and PASS for the MODEL-01 qualified model."},
  {"id": "EXT-178", "title": "State operation catalog from anvil describe (fakoli/anvil#178)", "type": "EXTERNAL-DEP", "priority": "P0", "effort": "M", "dependencies": [], "acceptance": "anvil describe returns an operation catalog; Explorer renders live PRD content and the delivery projection seeds from it in the rig."},
  {"id": "FREEZE-01", "title": "Freeze v2 operation-layer apparatus and gate-polishing backlog", "type": "ARCH", "priority": "P0", "effort": "S", "dependencies": [], "acceptance": "A freeze note is recorded; no PR before LOOP-05 adds a new operation-layer schema or gate case except FIX-02/03/04."},
  {"id": "FIX-01", "title": "Wire the Advanced run/dispatch execution path (currently 503)", "type": "FIX", "priority": "P2", "effort": "M", "dependencies": ["FREEZE-01"], "acceptance": "A POST to the advanced run endpoint executes a real Responses turn on a reviewed route and returns a validated advanced-trace record instead of 503."},
  {"id": "FIX-02", "title": "Redaction case-smuggle: capitalized single-label host:port", "type": "FIX", "priority": "P1", "effort": "S", "dependencies": [], "acceptance": "Serving:8443 is scrubbed; the proven-leak corpus test passes with no over-redaction of sha256:/timestamps/scoped_id."},
  {"id": "FIX-03", "title": "Add Cookie/Set-Cookie to the redaction corpus", "type": "FIX", "priority": "P1", "effort": "S", "dependencies": [], "acceptance": "A seeded Set-Cookie value is scrubbed at construction and at the API boundary; reverting the pattern fails the new test."},
  {"id": "FIX-04", "title": "Gate project-scope preference writes by project membership", "type": "FIX", "priority": "P1", "effort": "M", "dependencies": [], "acceptance": "A non-member project-scoped preference write is refused indistinguishably from a missing project; a member's write succeeds."},
  {"id": "FIX-05", "title": "Extend the script-drift gate to inline -c/-e verification", "type": "FIX", "priority": "P2", "effort": "M", "dependencies": ["FREEZE-01"], "acceptance": "The packet contract documents inline verification declaration; a changed inline body is drift-detected or explicitly documented out-of-gate with the operator note."},
  {"id": "FIX-06", "title": "Implement the RelayEvent resolution kind for /send", "type": "FIX", "priority": "P2", "effort": "S", "dependencies": [], "acceptance": "A /send stream emits a resolution frame the web reducer consumes; a test asserts frame ordering and terminal immutability."},
  {"id": "LHF-01", "title": "Add route to the /api/bootstrap voice block", "type": "LOW-HANGING-FRUIT", "priority": "P2", "effort": "S", "dependencies": [], "acceptance": "The Voice tab displays the actual configured route id from bootstrap; a test asserts voice.route is present and scrubbed."},
  {"id": "LHF-02", "title": "First-class not-configured state for every inject-or-503 surface", "type": "LOW-HANGING-FRUIT", "priority": "P1", "effort": "M", "dependencies": [], "acceptance": "Each inject-or-503 tab on an unconfigured hub shows a named config-checklist panel (env var/backend) instead of a raw error or blank panel."},
  {"id": "IA-01", "title": "Merge Delivery + Explorer into one Plan & Deliver surface", "type": "ARCH", "priority": "P1", "effort": "M", "dependencies": [], "acceptance": "One plan surface in the rail; delivering from a task and browsing the hierarchy happen in the same view; web tests pass."},
  {"id": "IA-02", "title": "Merge Sessions + Runs into one Runs surface", "type": "ARCH", "priority": "P1", "effort": "M", "dependencies": [], "acceptance": "Sessions is no longer a top-level tab; runs are grouped by session in one surface; Start delivery lives there once."},
  {"id": "IA-03", "title": "Give the approve action exactly one home", "type": "ARCH", "priority": "P1", "effort": "S", "dependencies": [], "acceptance": "The authorize button renders in exactly one surface; the other links to it read-only; a web test asserts a single authorize control."},
  {"id": "IA-04", "title": "Separate the model-console cluster from the delivery rail", "type": "ARCH", "priority": "P1", "effort": "M", "dependencies": [], "acceptance": "The rail groups delivery surfaces separately from model-console surfaces; primary delivery surfaces number ~5-6."},
  {"id": "IA-05", "title": "Demote Routes/Evidence/Skills to inspection sub-panels", "type": "ARCH", "priority": "P2", "effort": "M", "dependencies": ["IA-01", "IA-02"], "acceptance": "Routes/Evidence/Skills are no longer top-level tabs; their content is reachable from the run Trace; web tests pass."},
  {"id": "ARCH-01", "title": "Pinned digest-versioned contract adapters for State/Serving/Codex/GitHub", "type": "ARCH", "priority": "P1", "effort": "L", "dependencies": ["LOOP-05"], "acceptance": "Each upstream adapter carries a pinned contract digest; a simulated upstream contract change fails closed with a typed reconciliation message rather than a 500 or silent wrong result."},
  {"id": "FEAT-01", "title": "Onboarding checklist derived from live hub records", "type": "FEATURE", "priority": "P2", "effort": "M", "dependencies": ["LHF-02"], "acceptance": "A fresh hub shows a checklist of remaining config steps, each flipping to done from a live hub record; no step is derived from a static assumption."},
  {"id": "FEAT-02", "title": "Governance-buyer packaging via Serving's install base", "type": "FEATURE", "priority": "P2", "effort": "M", "dependencies": ["LOOP-05", "FIX-04"], "acceptance": "Docs present Workbench as Serving's governance overlay with a concrete audit-story section; the workbench up path is documented against a proven live loop."},
  {"id": "FEAT-03", "title": "Session-bound voice supervision of a delivery run", "type": "FEATURE", "priority": "P2", "effort": "M", "dependencies": ["LOOP-04", "EXT-281"], "acceptance": "A voice command surfaces a run's blocked state and drives an approval through the same hash-bound consume path as the UI; no model/tool control or raw audio is exposed."},
  {"id": "EXT-280", "title": "Unified Anvil Serving /v1/audio/* gateway (anvil-serving#280)", "type": "EXTERNAL-DEP", "priority": "P2", "effort": "M", "dependencies": [], "acceptance": "STT/TTS route through the unified /v1/audio/* gateway; the interim adapter is removed and voice STT/TTS still return 200 no-leak through the hub."},
  {"id": "EXT-281", "title": "Realtime assistant transcript text (anvil-serving#281)", "type": "EXTERNAL-DEP", "priority": "P2", "effort": "M", "dependencies": [], "acceptance": "The realtime surface renders assistant transcript text from #281 and an operator-confirmed audible round-trip is recorded in QUALIFICATION.md."}
]
```
