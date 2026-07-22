# Anvil Workbench — SWOT Analysis (in the context of Anvil State + Anvil Serving)

Date: 2026-07-22 · Independent product-strategy pass (read-only over docs, code, CLI, and the two upstream systems)
Convention: **EVIDENCE** = cited to a file/doc/CLI; **INFERENCE** = my strategic read. This is a strategy lens, not a UX review — I read the independent UX review (`scratchpad/product-ux-review.md`) as one input and diverge where the strategic frame differs.

---

## The one-paragraph strategic read

Workbench is a *disciplined, honestly-qualified supervision-and-governance plane* for a software-delivery loop it deliberately does not own — State owns canonical project state, Serving owns model policy, the bridge owns the worktree and credentials. The engineering is genuinely excellent: hash-bound one-time approvals, fenced leases, defense-in-depth redaction, a fail-closed adversarial gate, and best-in-class LIVE/BLOCKED honesty. But two facts dominate the strategy. First, **the loop it exists to supervise has never run once end-to-end** — the seven blocked-live tasks are the finale of every PRD, and even on live infra the pinned local model emits non-executable tool calls, so there is no working runner. Second, and underappreciated: **Anvil State already ships a complete, working delivery loop** through its CLI, MCP tools, and Claude Code skills, driven by a *capable* in-session agent. Workbench's differentiator is therefore not "it delivers" — a capable agent + anvil skills already delivers — but "it delivers *under governance, with a less-capable runner, holding no credentials.*" That is a real and defensible wedge, but it is precisely the unproven part, and it depends on a weak-model runtime bet that is currently squeezed from both sides.

---

## STRENGTHS (internal, real today)

1. **A crisp non-ownership thesis, enforced in code — not a slide.** Workbench defines itself by what it refuses to touch (`README.md:9-12`, CLAUDE.md "Choose the right layer"). This is enforced structurally: a repository-scan contract test fails if any hub/bridge/browser source so much as references `state.db`, a WAL/shm sibling, or *any* SQLite driver (`CONTRACTS.md:30`, `QUALIFICATION.md:240`). Most "AI delivery" products sprawl to own everything; this one is architecturally incapable of doing so. **INFERENCE:** the discipline is the product's identity and its best asset.

2. **Irreversible-effect safety engineering that is rare to see specified this tightly.** Approvals are one-time, hash-bound, bridge-scoped, and additionally bind the evidenced run, worktree name, lease epoch, task ID, and PR head SHA; any changed diff/head fails closed into reconciliation (`CONTRACTS.md:148`). Leases are fenced, expiring, and re-checked immediately before every effect with a renew-or-stop rule (`CONTRACTS.md:116-119`). Completion is *defined* to be impossible without both a real merge and a State acceptance (`AGENTS.md:37`, `CONTRACTS.md:102`). This is the correct shape for guarding irreversible operations.

3. **Redaction / no-leak done as defense-in-depth.** A proven leak corpus (AKIA-no-separator, JWT, PEM, `ip:port`, dotless `host:port`, `postgres://`, `state.db`, UNC/`~` paths) is scrubbed at construction *and* re-scrubbed at the serialized API boundary, so even a rogue duck-typed `as_dict()` cannot emit a secret (`QUALIFICATION.md:237`, `CONTRACTS.md:109`). Closed field sets structurally cannot carry a credential, endpoint, or path.

4. **The fail-closed adversarial gate actually catches defects.** Every operation-spine adversarial input (stale catalog digest, lost/expired/fenced lease, changed packet, replayed approval, unprofiled capability, unknown outcome) refuses with a member of a *closed* `OPERATION_REFUSAL_CODES` set and dispatches no effect (`QUALIFICATION.md:236`). The handoff records "Every gate that ran found at least one real defect a green suite had missed; zero upheld false positives" (`SESSION-HANDOFF.md:448-450`) — evidence the process has teeth.

5. **"Build for a less-capable runtime model via deterministic typed context" is a genuinely novel design.** `RUNTIME-INFERENCE.md` gives the model "better state and smaller choices, not unrestricted tools," with a trusted/untrusted split where PRDs, repo files, and tool output are *untrusted task data* that "cannot add an operation, change a route, relax a budget, reveal a credential, or override an approval" (lines 8-9, 57-64). This is a strong, principled answer to both prompt injection and weak-model reliability, and `RunContext` already serializes into separate `trusted`/`untrusted` structures (`CONTRACTS.md:52`).

6. **Best-in-class qualification honesty.** The QUALIFICATION/SESSION-HANDOFF docs draw one hard line between hermetic and live, refuse to promote any blocked task, and state plainly "This document still claims no delivery-harness, PR-merge, State-apply, or tailnet-identity qualification" (`QUALIFICATION.md:22-23`). **INFERENCE:** this is itself a strategic asset — it is exactly the posture a governance/compliance buyer trusts, and most teams never write it down.

7. **Tailnet-first, credential-absent posture.** No worktree or GitHub credential in the hub; an identity-aware proxy injects the actor header, client copies stripped, browser never sees a token (`README.md:14-18`, `AGENTS.md:20`). The bridge starts Codex with a stripped tool surface (no plugins/apps/MCP/browser/web-search, no ambient env) so a run cannot inherit desktop credentials (`README.md:95-97`).

---

## WEAKNESSES (internal, real today)

1. **The defining loop has never run end-to-end — and the gap is load-bearing, not long-tail.** "101 of 108 tasks `done`" reads as ~94% complete, but the remaining 7 are the per-PRD delivery/live-identity finales (`QUALIFICATION.md:210-211, 256-283`). The live Deliver flow correctly fails closed (`deliver_no_session`, no bridge). No leased bridge run, hash-bound PR, compare-and-swap merge, or State-accept has ever executed once (`QUALIFICATION.md:318-322`). **INFERENCE:** the entire product thesis is currently *unproven*, and the elaborate safety apparatus guards a door not yet installed.

2. **Dashboard-plus-approval-console, not orchestrator, today.** Everything a user can do live is a read (Explorer/Runs/Routes/Plugins render) or a side-channel that bypasses delivery (streamed Chat, a one-shot Sandbox Responses turn, a Settings write) — see the 2026-07-22 LIVE set (`QUALIFICATION.md:73-79`). The orchestration is the exact part that 503s until injected.

3. **An apparatus-to-liveness inversion.** A large typed-operation spine, `dispatch_with_run_context` (explicitly "no production caller yet," `CONTRACTS.md:56`), run-context / project-context / delivery-projection routers (all inject-or-503), and *six* "proposed contract resource, not implemented endpoint" blocks (`CONTRACTS.md:158-201`) exist ahead of the happy path that would walk through them. Even the best design — `RUNTIME-INFERENCE.md` — is status "Proposed implementation guide." **INFERENCE:** doors are being hardened and versioned faster than the loop they guard is being built; the follow-up backlog (`SESSION-HANDOFF.md:175-190`) is gate-polishing, which is negative-leverage until one real task merges.

4. **The whole bet rides on an unproven weak-model runtime — squeezed from both sides.** The runtime-inference design targets a *less-capable* model. But (a) the pinned Heavy local model emits `shell_command<|channel|>commentary` that Codex cannot execute, so the bridge has no working runner even with perfect infra (`QUALIFICATION.md:312-316`); and (b) State's *already-working* path uses a *capable* in-session agent (see Threats) that does not need the typed-context scaffolding at all. **INFERENCE:** the design aims at a model tier that today is either too weak to drive the loop or capable enough not to need the harness. Closing that squeeze is the core technical risk of the product.

5. **Model executability is a compatibility unknown, not a provisioning task.** "Do not claim a passing PRD→edit→test→evidence run until a local model/template emits executable function calls throughout" (`QUALIFICATION.md:316`). Unlike infra (which is a wiring job), this is an open question about whether *any* qualified local model can drive a full Codex `exec` loop — arguably the single biggest risk in the stack.

6. **IA grab-bag that mirrors the backend, not the operator journey.** 13 top-level surfaces where the actual journey is ~4 conceptual places; Delivery/Explorer and Sessions/Runs overlap; the approve action has two homes; a model-console cluster (Chat/Voice/Advanced/Sandbox) roughly doubles the nav and dilutes the delivery story (per the UX review, corroborated in `App.jsx`). **INFERENCE (strategy angle):** the second, *working* product (a private model console) is currently carrying the live demo, which can mask that the first, *headline* product does not yet run.

7. **Hollow-by-default surfaces.** Advanced run/dispatch, delivery projection, run-context, project-context, and the voice relay all 503 until every env var and backend is wired (`UI-ACCEPTANCE-AUDIT.md:70-75`). A fresh hub shows dead panels — at odds with the otherwise excellent honesty ethos.

---

## OPPORTUNITIES (external / future)

1. **Become THE supervision & governance plane for the whole Anvil stack — and ride Serving's distribution.** Serving already packages Workbench as an optional stack (`anvil-serving workbench up …` — confirmed in `anvil-serving workbench --help`) and owns a `harness` integration surface. **INFERENCE:** Workbench does not need its own install funnel; it can ship as the governance layer inside Serving's "right-size your local serving tier" install base. That is a rare built-in wedge — own the *approval, redaction, and reconciliation* plane that State's raw CLI loop and Serving's raw router both lack.

2. **Governance overlay is the true differentiator, and the market for it is real.** State's native loop (skills + Claude Code + MCP) is single-operator and trusts the runner completely. Workbench's credential-absent, hash-bound-approval, redacted, multi-operator posture is exactly what a *team*, a *regulated environment*, or an *untrusted-runner* deployment needs. **INFERENCE:** the boundary discipline that looks like over-engineering for one tailnet owner becomes the entire value proposition at multi-tenant / compliance scale.

3. **Tailnet-first private delivery as a wedge against cloud CI + hosted coding-agents.** No code, credentials, or transcripts leave the tailnet; the model path is local via Serving. For privacy-sensitive, IP-sensitive, or air-gapped-adjacent teams, "your repo and your model never touch a vendor cloud" is a differentiated position no hosted coding-agent platform can match.

4. **Honest-qualification + adversarial-gate as a trust/compliance moat.** The typed refusal codes, closed schemas, no-existence-oracle guarantees, and the LIVE/BLOCKED discipline are directly legible as an audit story. **INFERENCE:** "here is the closed set of ways every irreversible action can be refused, each proven to dispatch no effect" is a compliance artifact competitors would have to retrofit.

5. **Local-model-native delivery on cost and privacy.** If the model-executability blocker is solved, a supervised loop driven entirely by local models (Serving's whole reason to exist — "right-size and run a local LLM serving tier from your coding-agent usage") is a genuine cost/privacy story versus paying per-token for a hosted agent to do the same merges.

6. **Voice / ambient supervision as a distinctive surface.** Request/response STT+TTS is already LIVE through the hub; realtime is connected (`QUALIFICATION.md:98-137`). Session-bound push-to-talk supervision of a delivery run — "what's blocked, approve the PR" by voice — is a differentiated interaction few delivery tools attempt, and the relay is already boundary-safe (no model/tool controls, no raw-audio storage).

---

## THREATS (external)

1. **Its headline flow is gated on features in two repos it does not control.** **fakoli/anvil#178** (anvil 0.6.0 "advertises no operation catalog" from `anvil describe`) blocks Explorer live PRD content *and* the delivery-projection seed (`QUALIFICATION.md:182-185`). **fakoli/anvil-serving#280/#281** gate voice completeness. **INFERENCE:** Workbench cannot finish its finale on its own schedule — it is downstream of upstream roadmaps. Verified against the live CLI: `anvil-serving` is mature (router/serves/eval/edge/harness/workbench all present), but the State `describe` catalog #178 needs is simply not in 0.6.0 yet.

2. **The ecosystem already has a working delivery loop that does not need Workbench.** This is the sharpest competitive threat and it is *internal to the ecosystem*. Anvil State's `execute` skill carries a task "all the way to `needs_review`" and says plainly "**Do the work directly in this session**" (`execute/SKILL.md:8, 93-94`); `claim`/`submit`/`apply` and the full MCP tool set (`claim_task`, `submit_completion_evidence`, `generate_work_packet`, …) already close PRD→plan→claim→execute→submit→apply with a capable agent. **INFERENCE:** a capable Claude Code / Codex operator pointed at the anvil skills already *ships merges today*, with zero Workbench. Workbench must win on governance, multi-operator trust, redaction, and credential-absence — not on "it delivers" — or it is a heavyweight supervisor for a loop the ecosystem runs without it.

3. **The local-model quality/executability ceiling may never clear for the weak tier.** Hosted coding-agent platforms (Devin, Copilot Workspace, Cursor background agents, Claude Code itself) already close the full loop with frontier models. If local models remain unable to drive a reliable tool loop (`QUALIFICATION.md:312-316`), Workbench's local-model wedge stays theoretical while the capable path keeps using State directly.

4. **"Owns nothing" maximizes contract-surface fragility.** Workbench is a pure integrator of State CLI/events, Serving Responses/MCP, Codex, and GitHub. Every one of those can break it by changing a contract — and State's own docs already warn that packet paths and CLI syntax vary per project and layout (`state-ops/SKILL.md:66-73`, `execute/SKILL.md:55`). **INFERENCE:** the discipline that is Strength #1 is also a standing dependency on four moving contracts; a State schema bump or a Codex wire-API change is a Workbench outage.

5. **Three products must mature in lockstep — that is a lot of surface for one team.** State (canonical loop + #178), Serving (router + voice #280/#281), and Workbench (the governance plane) each have independent roadmaps and must all land together for the finale to work. **INFERENCE:** the binding constraint may not be any single product's quality but the *coordination cost* of advancing three at once; Workbench, being furthest downstream, absorbs every upstream slip.

6. **Adoption friction from BYO-everything + tailnet-first.** The setup demands a tailnet identity proxy, a registered bridge with one-time tokens, a local Serving router with a qualified model, Postgres+Neo4j, and per-project State CLI configuration (`README.md:20-84`). **INFERENCE:** the ceremony is defensible for the security posture but is a steep first-run wall versus "sign in to a cloud agent," and every hollow 503 panel makes that wall feel taller.

---

## Strategic synthesis — the cross-moves that matter

**The dominant frame:** Workbench's greatest strengths (boundary discipline, hash-bound approvals, redaction, honesty) are *governance* strengths, and its greatest threat (State already delivers with a capable in-session agent) is a *delivery* threat. The two only meet on one battlefield: proving that Workbench delivers **under governance a raw State loop cannot offer** — credential-absent, multi-operator, redacted, reconcilable. Everything strategic reduces to reaching that proof before the apparatus outgrows the loop.

- **WT (defend the most-endangered flank):** Weakness #1 (loop never ran) × Threat #2 (State already delivers) is the existential pairing. If Workbench never closes one supervised merge, the ecosystem's own working loop makes it redundant. **This is the product's central risk.** The only retort is a single end-to-end vertical slice: one ready State task → leased bridge run → evidence → hash-bound approval → PR → compare-and-swap merge → State-accept, against a disposable repo. That one event converts "elaborate dashboard" into "orchestrator" and is worth more than any additional surface or schema.

- **SO (press the strongest advantage into the biggest opening):** Strength #2/#3/#4/#6 (approvals, redaction, adversarial gate, honesty) × Opportunity #1/#2/#4 (governance plane, Serving distribution, compliance moat). Once the loop closes once, the *right* market is not the single tailnet owner — it is the team/regulated/untrusted-runner buyer for whom the governance overlay is the whole point, distributed through Serving's install base. Lead with the audit story, not the feature list.

- **ST (use a strength to blunt a threat):** Strength #6 (radical honesty) × Threat #4 (contract fragility). The same LIVE/BLOCKED discipline that documents the delivery gap should be turned outward into *pinned, digest-versioned* contract adapters against State/Serving/Codex so an upstream change fails closed and legibly rather than silently — converting the "owns nothing" fragility into a monitored, reconcilable dependency.

- **WO (the weakness that most endangers the opportunity):** Weakness #4 (the squeezed weak-model bet) endangers Opportunity #5 (local-model-native delivery). Resolve model-executability *in parallel with* the vertical slice — qualify one local model/template that emits executable function calls through a full Codex `exec` loop — because it may gate the slice itself, and because a proven local runner is what makes the cost/privacy wedge real rather than aspirational.

### Single highest-leverage priority

**Close one supervised delivery loop end-to-end, once — and treat the model-executability blocker as co-critical, not sequential.** Until a real task merges under supervision with a consumed hash-bound approval and a State acceptance, Workbench is a superbly-engineered governance apparatus around a loop the surrounding ecosystem already runs without it. That single proof point is what converts every listed Strength from *potential* to *moat*, retires the deepest Weakness and Threat simultaneously, and earns the right to build the thirteenth surface or the seventh contract schema. Freeze the proposed v2 apparatus and the gate-polishing backlog until that caller exists.
