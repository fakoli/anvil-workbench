# Qualification record

Date: 2026-07-21 (final state of the multi-PRD delivery run)

This is an **evidence record, not a promotion decision**. It draws one hard line:

- **Locally / hermetically qualified** — proven by the in-process test suite with
  every external actor injected or mocked: contract tests green, adversarial
  inputs fail closed with stable typed codes, redaction holds, schemas are
  closed, drift is detected, and no test touches external infrastructure — no
  external network, no real `state.db` open/mutation, and no live bridge/provider.
  The only processes and sockets a test may use are local fixtures: fixture
  subprocesses (`git init` for the drift spine, `python -I -m workbench.codex_auth`,
  a fresh interpreter in `test_chat_runtime_integration`) and a loopback token
  broker socket.
- **Requires live qualification** — needs real infrastructure that does not exist
  in this environment: a tailnet identity, a real project bridge (Codex +
  GitHub), a real Anvil Serving model route, a real GitHub PR/merge, and a
  State-apply against canonical Anvil State.

**Nothing in this document claims a live, tailnet, real-model, PR-merge, or
State-apply qualification.** Where live infrastructure was touched on 2026-07-19,
it is recorded below as transport/smoke *observation only* and is explicitly not
a qualification claim.

## Run summary

This run delivered every hermetically-completable task across the six approved
PRDs — chat-first-voice, state-context-operations, preferences-configuration,
advanced-model-playground, plan-task-delivery, reviewed-tools-plugins — merged
through gate-reviewed pull requests **#9–#35** on `fakoli/anvil-workbench` (27
contiguous merges verified on `origin/main`; HEAD `11f787f`). Per the control
plane, **101 of 108 tasks are `done`** after this task; the remaining **7 are
BLOCKED-LIVE** and were never hermetically completable — they are the per-PRD
live-qualification gates listed below.

> State was not queryable from this worktree (`anvil` is not initialized here),
> so the 101/108 count and the exact blocked-task IDs are recorded as provided by
> the control plane. They are corroborated structurally: the merged branches are
> all the hermetic (contract / integration / component) tasks, and the seven
> blocked tasks are exactly the live-qualification finales, each of which depends
> on infrastructure this environment does not provide.

## Verification (this task)

| Check | Result |
| --- | --- |
| `python -m pytest -q` | **1135 passed** (1128 baseline + 7 new adversarial-qualification cases) |
| `web` `npm test -- --run` | **290 passed** (12 files) |
| `web` `npm run build` | clean (`vite build` OK) |
| `docker compose --env-file .env.example config -q` (with `ANVIL_ROUTER_TOKEN` in the host env) | exit 0 |

## Cross-cutting hermetic qualification (the spine)

These guarantees hold across every operation surface and are the backbone of the
run. All are hermetic.

| Guarantee | What is proven | Evidence |
| --- | --- | --- |
| **Fail-closed with stable typed codes, no external effect** | Every operation-spine adversarial input (stale catalog digest, lost/expired/fenced lease, changed work packet, replayed one-time approval, unprofiled capability, unknown outcome) refuses with a code that is a declared member of the closed `OPERATION_REFUSAL_CODES` set and dispatches **no** adapter effect (or reconciles an unknown outcome without a silent retry). | `tests/test_adversarial_qualification.py` (the consolidating index), `tests/test_typed_operation_integration.py` (per-gate), `workbench/models.py::OPERATION_REFUSAL_CODES` |
| **Redaction / no leak** | The proven leak corpus (AKIA-no-separator, JWT, PEM, `ip:port`, dotless `host:port`, `postgres://` URL, `/var/anvil/state.db`, UNC/`~` paths) is scrubbed from every free-text channel at construction time and re-scrubbed on the serialized API boundary; a rogue duck-typed `as_dict()` is still scrubbed. | `tests/test_security_contract.py`, `workbench/redaction.py::scrub_config_payload`/`redact_config_text` |
| **No existence oracle** | A cross-actor / cross-project / missing read is byte-identical to a genuinely absent record (conversations, run-context, project-context, response lifecycle, idempotency). | `tests/test_conversation_store.py`, `tests/test_run_context_adversarial.py`, `tests/test_project_context_adversarial.py` |
| **Closed schemas** | Every contract resource is `additionalProperties:false` throughout; a smuggled body field is a 422; secret/path descriptors carry no serializable default and drop from actor views. | `tests/test_contract_resources.py`, `tests/test_settings_descriptor.py`, `tests/test_advanced_contracts.py` |
| **State-internals isolation** | No hub, bridge, or browser source opens, copies, mounts, or mutates `state.db` (or its WAL/shm siblings, a `.anvil` workspace, or any SQLite driver); the only allowlisted form is a documentation string stating the prohibition. | `tests/test_security_contract.py::test_no_workbench_source_opens_copies_mounts_or_mutates_state_storage` |
| **No raw-provider path** | No Workbench module imports an HTTP client on a model path or embeds a provider host/URL; managed model traffic goes only through Anvil Serving. | `tests/test_chat_routes.py`, `tests/test_chat_stream.py`, `tests/test_security_contract.py` |
| **Drift / revert detection** | A changed catalog/profile/route/skill/verification-script digest fails closed before any effect with a stable `SnapshotDrift` / refusal code; adopting one skill digest never adopts a later change. | `tests/test_workflow_snapshot.py`, `tests/test_reviewed_tools_plugins.py`, `tests/test_harness_kernel.py` + `tests/test_security_contract.py` (verification-script drift, `test_sco_t008_*`) |
| **Approvals: one-time, hash-bound** | An approval binds a canonical payload hash, action, and bridge; a changed diff or a replay fails closed; consumption is atomic. | `tests/test_typed_operation_integration.py`, `tests/test_harness_kernel.py` |

## Per-PRD qualification matrix

| PRD | Locally / hermetically qualified (evidence) | Requires live qualification (blocked task) |
| --- | --- | --- |
| **chat-first-voice** | Durable conversation store, streaming runtime, reconnect-safe lifecycle, retention/deletion, idempotency, sequence/gap detection, organization metadata, divergence/export, browser conversation API, private voice-relay runtime. Component-proven Chat surface. Tests: `test_chat_*`, `test_conversation_*`, `test_response_lifecycle_store.py`, `test_stream_sequence.py`, `test_retention_enforcement.py`; web `App.test.jsx`/`chat-api.test.js`. PRs #11 (cfv-integration bundle), #15, #26, #34. | **T004, T005, T006** — a live rendered-browser chat+voice turn against a real hub: real `/api/conversations` persistence, a real streamed relay frame, a real Dark STT→Fast→TTS turn, real console/network capture. The `/api/conversations/{id}/send` route-resolution join and a `RelayEvent` `resolution` kind are **not mounted** (RelayEvent emits only `delta`/`terminal`); the mark surface `GET /api/chat/route-resolutions` **is** wired and tested. |
| **state-context-operations** | State read-adapter contract (discovery pins, snapshot + bounded PRD-content adapters), provider-catalog registry, capability profiles, workflow snapshots, run-context capture + read API, typed-operation spine (T006.1/.2/.3) with the full fail-closed matrix, verification-script drift gate (T008). Tests: `test_state_*`, `test_provider_catalogs.py`, `test_capability_profiles.py`, `test_workflow_snapshot.py`, `test_run_context_*`, `test_typed_operation_integration.py`, `test_adversarial_qualification.py`. PRs #10 (sco-context-projection bundle), #14, #16, #35. | Part of the shared live gate: the typed-operation spine, run-context capture, and project-context projection are hermetic and **not wired into the live bridge poll/queue loop** (`dispatch_with_run_context` has no production caller; the read routers stay `None`→503 until injected). Live qualification rides the chat/delivery live gates behind fakoli/anvil#178. |
| **preferences-configuration** | Settings-descriptor contract, optimistic-locking preference data models, effective-value resolver, policy-operation gates (external-read-only / stale-version), system-health + observational posture, searchable settings, export/import/reset. Tests: `test_settings_descriptor.py`, `test_preference_gates.py`, `test_system_health.py`, configuration-transfer (export/import/reset) coverage in `test_security_contract.py` / `test_harness_kernel.py` / `test_api.py`; web `settings*.test.*`, `configuration*.test.*`. PRs #13, #17, #22, #24, #31, #33. | **T007** — live operator qualification of the settings surfaces against a real hub + tailnet identity (rendered-browser Settings/Configuration, real project-scoped writes). |
| **advanced-model-playground** | Advanced-mode contract surface (branch/trace/preset/comparison), route-capability discovery, advanced runtime (seven durable states), parallel dispatch, mock read-only tools, advanced-chat controls, preset/comparison/export persistence. Tests: `test_advanced_*`; web `advanced-chat.test.js`, `advanced-playground.test.js`. PRs #19, #27, #29, #32. | **T007** — live wiring + qualification. The advanced **run/dispatch execution path** (`advanced_routes`/`advanced_runtime`/`advanced_dispatch`) is hermetic and **not wired into any HTTP endpoint**; the advanced preset/template/rating persistence routers are mounted but back injected stores (`None`→503 by default). The client degrades to 503 with no route. |
| **plan-task-delivery** | Delivery display projection (read-only, project-scoped), Deliver-from-a-task (atomic idempotent start + typed receipt), typed operator directives with packet high-water marker, PRD/plan/task/eligibility explorer, Deliver controls + setup sheet. Tests: `test_plan_task_delivery.py`, `test_api.py`; web `delivery-explorer.test.js`, `App.test.jsx`. PRs #9 (ptd-contracts bundle), #20, #21, #23. | **T007** — live delivery qualification: a real Deliver against a real project bridge that starts a leased bridge run, a real approval, and a State-apply after a real approved merge. The delivery projection stays `None`→503 and is **not wired into the live bridge poll loop**. |
| **reviewed-tools-plugins** | Approved-catalog discovery, chat capability-profile pinning, plugin catalog + permission UI, plugin host / chat-tool dispatch, skill-digest adoption gate. Tests: `test_reviewed_tools_plugins.py`, `test_plugin_host.py`; web `plugin-catalog*.test.*`. PRs #12 (rtp-contracts bundle), #18, #25, #28, #30. | **T007** — live operator-enable of the skill-adoption gate. The gate is **operator-enablable** (`--skill-adoption-ledger` opt-in from a reviewed local JSON ledger) but **ungated by default** (legacy); live qualification enables and exercises it on a real bridge. |

## The 7 blocked-live tasks

Each requires the full live stack (tailnet identity + real project bridge/Codex/
GitHub + real Anvil Serving model route + real PR/merge + State-apply) and was
never hermetically completable. Upstream blocker:
**[fakoli/anvil#178](https://github.com/fakoli/anvil/issues/178)**.

1. `chat-first-voice:T004`
2. `chat-first-voice:T005`
3. `chat-first-voice:T006`
4. `preferences-configuration:T007`
5. `advanced-model-playground:T007`
6. `plan-task-delivery:T007`
7. `reviewed-tools-plugins:T007`

## Prior live observations (2026-07-19) — transport/smoke only, NOT a qualification

On 2026-07-19 a loopback stack briefly touched live infrastructure. These are
recorded for provenance; **none is a qualification claim**, and each honest
caveat from the original run is retained.

- **Browser shell** at `http://127.0.0.1:8090`: the production bundle rendered
  Delivery and all secondary views; a bounded `chat-fast` Sandbox request
  completed through Serving and rendered its response; Voice correctly stayed
  disabled without a private relay. This is a render/transport observation, not a
  rendered-browser *qualification* of any surface (see UI-ACCEPTANCE-AUDIT.md).
- **State CLI** on a disposable `anvil-workbench-state-e2e-proven` fixture:
  claim/packet/verification/evidence/replay passed through the CLI only. No
  `state.db` was opened or modified; State accept/approve was **intentionally not
  performed**.
- **Model planes** (pinned Heavy `gpt-oss-puzzle-88B`, Fast `gemma-4-E4B`, Dark
  STT/TTS): smoke/JSON/context/preflight and a synthetic-silence voice loop
  completed. This is transport evidence only, **not** quality or
  speech-recognition validation.
- **Codex-through-Serving**: the bridge claimed the State task, created a Codex
  run through `/v1/responses`, and recorded correlated decisions, but the pinned
  Heavy emits `shell_command<|channel|>commentary` that Codex cannot execute
  locally; the bridge correctly refused to submit State evidence and marked the
  run for reconciliation. **Do not claim a passing PRD→edit→test→evidence run
  until a local model/template emits executable function calls throughout.**

## Deliberately not exercised (still requires live qualification)

- No GitHub commit, push, PR creation, merge, or State acceptance was executed as
  part of a delivery loop (the #9–#35 PRs are the run's own reviewed merges, not a
  bridge-driven delivery of a project under supervision).
- No tailnet identity proxy; the 2026-07-19 stack used the development-only
  loopback actor override.
- No live Dark Realtime voice turn; the relay's allowlist, no-raw-audio, and
  invalid-event rejection are contract-tested only.
- No live Neo4j projection/search; graph write-denial and redaction are
  contract-tested only.

## Requalification recipe

1. Start the hub only on the tailnet or the loopback development bind, with the
   env from `.env.example` (and secrets from the deployment store, never
   committed).
2. Verify the target model with the full Responses tool/result continuation and a
   small Codex `exec` loop before using it as the Workbench harness route.
3. Use a disposable Git repository and a State sample task; require a
   packet-declared changed file and a zero-exit packet verification command.
4. Confirm the run becomes `evidenced`, State shows `needs_review` with complete
   evidence, and Serving `/v1/decisions` retains `workbench_run_id`, task ID, and
   request ID.
5. Approve and run only the PR/merge fixture separately; confirm a post-merge
   State failure surfaces as `reconciliation`, never completion.
6. For each blocked-live task, wire its `None`→503 store/service into a real
   deployment and run the rendered-browser + live-infra checks its acceptance
   names.
