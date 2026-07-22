# Qualification record

Date: 2026-07-21 (hermetic final state) · **2026-07-22 live-qualification
addendum** (see [the live section](#live-qualification-observed-2026-07-22-real-tailnet-stack))

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

**This document still claims no delivery-harness, PR-merge, State-apply, or
tailnet-identity qualification.** What changed on **2026-07-22**: a real tailnet
stack was stood up and a bounded set of *surface* legs were exercised live
against it — real-model streamed chat (Fast and Heavy planes), a real preference
write, a live Sandbox Responses turn, live request/response voice (STT transcribe
+ TTS read-aloud through the hub), and live catalog-backed Plugins/Settings
render. Those legs are recorded as **LIVE** in the
[2026-07-22 live-qualification section](#live-qualification-observed-2026-07-22-real-tailnet-stack)
with their concrete evidence, each naming exactly what it did and did not prove.
The delivery loop (a real project bridge + Codex + GitHub PR/merge + State-apply)
and a real tailnet identity were **not** exercised and remain blocked. The
2026-07-19 observations below stay transport/smoke *observation only*.

## Live qualification observed 2026-07-22 (real tailnet stack)

This section is the honest record of what the **2026-07-22 live run** actually
exercised against a real tailnet stack, kept strictly separate from the hermetic
evidence below. It is a *surface* evidence record, not a promotion. It uses three
dispositions:

- **LIVE** — observed this session against the real stack, with named evidence.
- **LIVE-PARTIAL** — one leg observed live; a named leg was still not exercised.
- **STILL-BLOCKED** — no live evidence; the leg needs infra this run lacked.

No PR/merge, no State-apply, no real tailnet identity, and no real project
bridge/Codex/GitHub effect occurred. Nothing here is a delivery-harness or
production-model qualification.

### The live stack that was up

| Component | Where |
| --- | --- |
| Anvil Serving router | `100.87.34.66:8000`, 8 routes |
| Heavy plane | `gpt-oss-puzzle` served — work_class `planning` → `heavy-local` |
| Fast plane | `gemma` served — work_class `chat` → `fast-local` |
| Dark STT / TTS | `:30010` / `:30011` |
| Realtime voice proxy | `127.0.0.1:8766` (session-local topology; operator topology untouched) |
| Workbench hub | `127.0.0.1:8090`, Postgres + Neo4j, rebuilt from the merged live-qualification branch |

The hub was rebuilt from the merged live-qualification branch. The
`deploy/Dockerfile.api` fix that ships `docs/contracts/` into the image (so the
fail-closed schema loads no longer 500 the boot) is what unblocked booting
current `main`. Injectable surfaces were wired live through the
`WORKBENCH_LIVE_SURFACES` composition (`workbench/deployment.py`,
`LIVE_SURFACE_NAMES`); the send/stream join is now a real mounted endpoint
(`POST /api/conversations/{id}/send`, `workbench/conversation_api.py`).

### Per-task live disposition

| Task | Disposition | One-line summary |
| --- | --- | --- |
| **chat-first-voice:T006** (chat) | **LIVE** | Multi-turn streamed chat rendered live on both Fast and Heavy routes; safe-metadata decisions; memory, cancel, live-region, keyboard proven. |
| **chat-first-voice:T006** (voice — realtime) | **LIVE-PARTIAL** | Realtime speech-to-speech relay CONNECTED end-to-end (transport only); a clean audible spoken round-trip is NOT yet operator-confirmed post sample-rate fix. |
| **chat-first-voice:T006** (voice — request/response) | **LIVE** | STT dictation + read-aloud now wired (`voice_relay_service` + `serving_audio.py` adapter to parakeet/kokoro): `transcribe` 200 (no turn) and `speak` 200 (~1.64 s PCM @24000, correct client sample-rate) live through the hub, no leak, no mutation. Sole pending item: an operator-confirmed audible spoken **dictation** turn (mic needs a user gesture). |
| **preferences-configuration:T007** | **LIVE** | A real preference write through the rendered Settings UI; zero-leak scans clean; project-scoped/tailnet-identity writes still blocked. |
| **advanced-model-playground:T007** | **LIVE-PARTIAL (mixed)** | Advanced persistence surfaces + a live Sandbox Responses turn served; the advanced run/dispatch execution path is STILL not wired (503). |
| **plan-task-delivery:T007** | **LIVE-PARTIAL** | Explorer + human-titled Runs rendered live; Deliver correctly fails closed (no live bridge); live PRD content blocked on fakoli/anvil#178. |
| **reviewed-tools-plugins:T007** | **LIVE-PARTIAL** | Digest-pinned reviewed catalog served live; skill-adoption gate live-enable on a real bridge is still blocked (no bridge). |

### chat-first-voice:T006 — chat LIVE; voice request/response LIVE, realtime LIVE-PARTIAL

- **Chat, LIVE.** Multi-turn streamed chat rendered in a **real browser** on both
  a **Fast-served** route (work_class `chat` → `fast-local`) and a
  **Heavy-served** route (work_class `planning` → `heavy-local`, 251 completion
  tokens). Each turn was recorded in Serving's `/v1/decisions` with **safe
  metadata only** (work_class, served_tier, token counts); the decisions
  endpoint's `omitted_fields` lists `prompt`/`messages`/`content`/`response`/
  `api_key`/`authorization`/`token`, so prompt/messages/response were **omitted**.
  An honest `fast-local`-unavailable → `heavy-local` **fallback** was recorded
  earlier. **Conversation memory** verified: a follow-up "what is my favorite
  number?" returned `42` from prior-turn context. A real mid-stream **cancel**
  settled durably as `status=cancelled` with the partial text preserved; a stream
  that completed before the cancel was honestly `status=complete`. The live region
  announced "Response complete". **Keyboard-only send** exercised. Raw-audio
  persistence remains **hermetic** — the store never persists audio, proven by
  `tests/test_chat_voice_integration.py`.
- **Voice — split into two surfaces: request/response LIVE, realtime LIVE-PARTIAL.**
  The sole pending voice item for either surface is an operator-confirmed audible
  spoken round-trip (mic needs a user gesture).
  - **Realtime speech-to-speech relay: LIVE-PARTIAL (transport/connection only).**
    The realtime relay chain (the dedicated panel, via `ANVIL_VOICE_REALTIME_URL`)
    **connected end-to-end**: the hub accepted the browser session websocket
    `/api/sessions/{id}/voice/realtime` and the proxy usage counter showed
    `claims_total=1`. This is **transport/connection** evidence. An actual audible
    spoken round-trip **could not be script-driven** (Chrome requires a trusted
    user gesture for the mic). A real operator spoken turn earlier exposed a
    16 kHz/24 kHz **sample-rate mismatch**, since **fixed**
    (`VOICE_RELAY_SAMPLE_RATE=16000`) — but a **clean audible round-trip has NOT
    yet been operator-confirmed post-fix**. This is the one remaining
    human-in-the-loop datum for the realtime surface.
  - **Request/response STT dictation + read-aloud: LIVE.** `voice_relay_service`
    is now wired via `WORKBENCH_LIVE_SURFACES` plus a gated hub-side adapter
    (`workbench/serving_audio.py` `DarkServingAudioTransport`) to the Dark serves
    (parakeet STT `:30010`, kokoro TTS `:30011`), env-configured
    (`ANVIL_VOICE_STT_URL`/`TTS_URL`/models/`sample_rate`). Observed live through
    the hub against real serves:
    - **STT:** `POST /api/chat/voice/transcribe` → parakeet → **200** with a real
      draft transcript; STT creates **no** conversation turn (verified `turns=0`).
    - **TTS read-aloud:** the rendered-browser "Read this response aloud" button on
      an assistant turn fired `POST /api/chat/voice/speak` → **200**; kokoro
      returned ~1.64 s PCM at `sample_rate=24000`; the client honors the server
      `sample_rate` (fixing the 16 k/24 k garble class); playback succeeded with no
      console errors; TTS mutated **no** message/conversation state (byte-identical).
    - **No leak, LIVE:** after real transcribe + speak traffic, the conversation
      record contains no raw audio bytes/base64 blob, and the api logs are
      content-free (200s only, no audio).
    - **Interim adapter note:** the hub-side `DarkServingAudioTransport` bridges the
      split raw Dark STT/TTS serves (kokoro `:30011` returns raw `audio/pcm`; the
      hub audio contract is JSON `{audio_b64}`). A unified Anvil Serving
      `/v1/audio/*` gateway ([fakoli-serving#280](https://github.com/fakoli/anvil-serving/issues/280))
      is the clean long-term replacement for this interim adapter.
  - **The one remaining voice datum (both surfaces):** an **operator-confirmed
    audible spoken round-trip** — for request/response, an audible spoken
    **dictation** turn end-to-end; for realtime, an audible speech-to-speech turn.
    A browser mic requires a real user gesture, so scripted automation cannot grant
    mic permission; this is human-gated.
  - This live evidence is recorded separately from the unit/component evidence and
    does **not** claim delivery-harness qualification.

### preferences-configuration:T007 — LIVE

Settings rendered against the real hub serving the reviewed catalog (12 settings
with scope-ownership chips personal/project-owned and editable flags). A **real
preference write** through the rendered UI: Voice auto-play Off→On,
`PUT /api/preferences/personal.voice_autoplay` → 200, provenance flipped
`default` → "Set at the personal scope", the optimistic-lock "version 1"
surfaced. Effective-value precedence was shown per setting. **No leak**: automated
leak-scans of the live `/api/preferences`, `/api/chat/routes`, and
`/api/system/configuration` payloads found **zero** secrets/paths/`host:port`/
tokens. Live policy operations were **not** consumed against production targets.
**Still requires:** project-scoped writes against a real tailnet identity — the
dev loopback actor override was in use (`WORKBENCH_ALLOW_INSECURE_DEV_ACTOR=true`),
so tailnet-identity qualification is **not** done.

### advanced-model-playground:T007 — LIVE-PARTIAL (mixed)

**LIVE:** the advanced preset/template/rating persistence surfaces served (200)
live via the wired stores (`advanced_preset_store`/`advanced_template_store`/
`advanced_rating_store`), and the Sandbox made a bounded `chat-fast` Responses
request through Serving and rendered its reply (`WORKBENCH_BROWSER_LIVE_OK`),
recorded in `/v1/decisions`, performing no bridge/State/GitHub/deploy effect.
**STILL BLOCKED:** the advanced **run/dispatch execution path**
(`advanced_routes`/`advanced_runtime`/`advanced_dispatch`) remains **not wired to
any HTTP endpoint** (the client degrades to 503), so the live
route-supported-vs-assumed control distinction and live-trace qualification are
**not** done. The release record must state that Advanced Chat success is **not**
delivery-harness or production-model qualification.

### plan-task-delivery:T007 — LIVE-PARTIAL

**LIVE:** the rendered Explorer shows the named PRD→task hierarchy (ids
secondary); the Runs list now leads every row with a **human title** ("Planning
run for task T001") and demotes run/task ids to secondary machine text (the
ids-are-for-machines fix), preserving a durable **reconciliation** row; the
browser console health was clean; keyboard nav worked; the State-database
prohibition holds (the hermetic `tests/test_security_contract.py` proves no
source opens `state.db`). The **Deliver-from-a-task** flow renders and is
reachable but correctly **fails closed** (`deliver_no_session` / no startable
session) because **no live project bridge is registered** — so "one real ready
task queues exactly one bridge run" is **not** exercised (no bridge). Explorer
live PRD **content** is fail-closed on the delivery-projection seed (upstream
[fakoli/anvil#178](https://github.com/fakoli/anvil/issues/178): anvil 0.6.0
advertises no operation catalog), so live PRD-content rendering is **not** done —
the seed pipeline exists and is proven against a conforming CLI.

### reviewed-tools-plugins:T007 — LIVE-PARTIAL

**LIVE:** the Plugins surface now serves the digest-pinned reviewed capability
catalog live (`WORKBENCH_PLUGIN_CATALOG_FILE` + `WORKBENCH_PLUGIN_CAPABILITY_FILE`
wired): reviewed plugin(s) shown version/digest/publisher-pinned, credentials by
reference only, Serving routes AVAILABLE, delivery operations shown as hash-bound.
**Hermetic (unchanged):** a read-only plugin returns a redacted cited result; an
effect plugin cannot act before an exact approval; a skill select/deselect
changes no plugin/tool/credential/host/permission in the pinned profile and the
dispatch validator proves it; a bridge-managed Codex run's tool surface excludes
Chat plugins (`tests/test_reviewed_tools_plugins.py`, `tests/test_plugin_host.py`).
**STILL BLOCKED:** live operator-enable of the skill-adoption gate on a real
bridge (no live bridge) — the gate is operator-enablable but its live exercise is
**not** done.

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
| **chat-first-voice** | Durable conversation store, streaming runtime, reconnect-safe lifecycle, retention/deletion, idempotency, sequence/gap detection, organization metadata, divergence/export, browser conversation API, private voice-relay runtime. Component-proven Chat surface. Tests: `test_chat_*`, `test_conversation_*`, `test_response_lifecycle_store.py`, `test_stream_sequence.py`, `test_retention_enforcement.py`; web `App.test.jsx`/`chat-api.test.js`. PRs #11 (cfv-integration bundle), #15, #26, #34. | **T004, T005, T006** — a live rendered-browser chat+voice turn against a real hub. **2026-07-22: chat is now LIVE** — the `POST /api/conversations/{id}/send` send/stream join **is now mounted** (emits `delta`/`terminal`) and a multi-turn streamed chat rendered live on both a Fast and a Heavy route (see the [live-qualification section](#live-qualification-observed-2026-07-22-real-tailnet-stack)); the inline `RelayEvent` `resolution` kind is still **not added** (the polled mark surface `GET /api/chat/route-resolutions` **is** wired). **Voice splits**: the **request/response** STT-dictation + read-aloud surfaces are now **LIVE** — `voice_relay_service` + a `serving_audio.py` hub-side adapter to parakeet/kokoro give `transcribe` 200 (no turn) and `speak` 200 (~1.64 s PCM @24000, correct client sample-rate) live through the hub, no leak, no mutation; the **realtime** speech-to-speech relay stays LIVE-PARTIAL (connected transport). The sole pending voice item is an **operator-confirmed audible spoken round-trip** (mic needs a user gesture). |
| **state-context-operations** | State read-adapter contract (discovery pins, snapshot + bounded PRD-content adapters), provider-catalog registry, capability profiles, workflow snapshots, run-context capture + read API, typed-operation spine (T006.1/.2/.3) with the full fail-closed matrix, verification-script drift gate (T008). Tests: `test_state_*`, `test_provider_catalogs.py`, `test_capability_profiles.py`, `test_workflow_snapshot.py`, `test_run_context_*`, `test_typed_operation_integration.py`, `test_adversarial_qualification.py`. PRs #10 (sco-context-projection bundle), #14, #16, #35. | Part of the shared live gate: the typed-operation spine, run-context capture, and project-context projection are hermetic and **not wired into the live bridge poll/queue loop** (`dispatch_with_run_context` has no production caller; the read routers stay `None`→503 until injected). Live qualification rides the chat/delivery live gates behind fakoli/anvil#178. |
| **preferences-configuration** | Settings-descriptor contract, optimistic-locking preference data models, effective-value resolver, policy-operation gates (external-read-only / stale-version), system-health + observational posture, searchable settings, export/import/reset. Tests: `test_settings_descriptor.py`, `test_preference_gates.py`, `test_system_health.py`, configuration-transfer (export/import/reset) coverage in `test_security_contract.py` / `test_harness_kernel.py` / `test_api.py`; web `settings*.test.*`, `configuration*.test.*`. PRs #13, #17, #22, #24, #31, #33. | **T007 — 2026-07-22: LIVE (personal scope).** A real personal preference write landed through the rendered Settings UI against the real hub (`PUT /api/preferences/personal.voice_autoplay` 200, provenance + optimistic-version surfaced), zero-leak scans clean (see the [live-qualification section](#live-qualification-observed-2026-07-22-real-tailnet-stack)). **Still blocked:** project-scoped writes against a **real tailnet identity** — the run used the dev loopback actor override (`WORKBENCH_ALLOW_INSECURE_DEV_ACTOR=true`), so tailnet-identity qualification is not done. |
| **advanced-model-playground** | Advanced-mode contract surface (branch/trace/preset/comparison), route-capability discovery, advanced runtime (seven durable states), parallel dispatch, mock read-only tools, advanced-chat controls, preset/comparison/export persistence. Tests: `test_advanced_*`; web `advanced-chat.test.js`, `advanced-playground.test.js`. PRs #19, #27, #29, #32. | **T007 — 2026-07-22: LIVE-PARTIAL (mixed).** The advanced preset/template/rating persistence surfaces served 200 live via the wired stores, and a bounded `chat-fast` Sandbox Responses turn ran through Serving and rendered (`WORKBENCH_BROWSER_LIVE_OK`), no bridge/State/GitHub/deploy effect (see the [live-qualification section](#live-qualification-observed-2026-07-22-real-tailnet-stack)). **Still blocked:** the advanced **run/dispatch execution path** (`advanced_routes`/`advanced_runtime`/`advanced_dispatch`) is **not wired into any HTTP endpoint** (client degrades to 503), so live route-supported-vs-assumed control distinction and live-trace qualification are not done. Advanced Chat success is not delivery-harness or production-model qualification. |
| **plan-task-delivery** | Delivery display projection (read-only, project-scoped), Deliver-from-a-task (atomic idempotent start + typed receipt), typed operator directives with packet high-water marker, PRD/plan/task/eligibility explorer, Deliver controls + setup sheet. Tests: `test_plan_task_delivery.py`, `test_api.py`; web `delivery-explorer.test.js`, `App.test.jsx`. PRs #9 (ptd-contracts bundle), #20, #21, #23. | **T007 — 2026-07-22: LIVE-PARTIAL.** The Explorer rendered the named PRD→task hierarchy live and the Runs list now leads each row with a human title (ids demoted to machine detail), console-clean, keyboard-navigable (see the [live-qualification section](#live-qualification-observed-2026-07-22-real-tailnet-stack)). **Still blocked:** a real Deliver against a real project bridge (leased run → approval → State-apply after a real approved merge). The Deliver flow correctly **fails closed** (`deliver_no_session`) with no bridge registered; live PRD **content** is fail-closed on the seed pipeline (upstream [fakoli/anvil#178](https://github.com/fakoli/anvil/issues/178): anvil 0.6.0 advertises no operation catalog). |
| **reviewed-tools-plugins** | Approved-catalog discovery, chat capability-profile pinning, plugin catalog + permission UI, plugin host / chat-tool dispatch, skill-digest adoption gate. Tests: `test_reviewed_tools_plugins.py`, `test_plugin_host.py`; web `plugin-catalog*.test.*`. PRs #12 (rtp-contracts bundle), #18, #25, #28, #30. | **T007 — 2026-07-22: LIVE-PARTIAL.** The Plugins surface served the digest-pinned reviewed capability catalog live (`WORKBENCH_PLUGIN_CATALOG_FILE` + `WORKBENCH_PLUGIN_CAPABILITY_FILE` wired): reviewed plugin(s) version/digest/publisher-pinned, credentials by reference only, Serving routes available, delivery operations hash-bound (see the [live-qualification section](#live-qualification-observed-2026-07-22-real-tailnet-stack)). **Still blocked:** live operator-enable of the skill-adoption gate on a **real bridge** — the gate is operator-enablable (`--skill-adoption-ledger`) but its live exercise needs a bridge this run lacked. |

## The 7 blocked-live tasks

Each requires the full live stack (tailnet identity + real project bridge/Codex/
GitHub + real Anvil Serving model route + real PR/merge + State-apply) and was
never hermetically completable. Upstream blocker:
**[fakoli/anvil#178](https://github.com/fakoli/anvil/issues/178)**.

**2026-07-22 update:** five of these seven now carry **partial live surface
evidence** from the live run above — the *surface* leg is LIVE (or LIVE-PARTIAL),
while each task's **delivery-harness / tailnet-identity / dispatch-wiring** leg
stays blocked. See the
[2026-07-22 live-qualification section](#live-qualification-observed-2026-07-22-real-tailnet-stack)
for exactly which leg moved and which remains. None is promoted to done.

1. `chat-first-voice:T004` — chat surface exercised live under T006; still blocked as a delivery task.
2. `chat-first-voice:T005` — request/response STT/read-aloud now **LIVE** (200, no-leak, no-mutation); realtime voice relay connected (transport); an audible spoken round-trip is the sole operator-gated item.
3. `chat-first-voice:T006` — chat **LIVE**; voice **request/response LIVE** (wired via `serving_audio.py` to parakeet/kokoro); voice **realtime LIVE-PARTIAL** (transport only); an operator-confirmed audible turn is the sole pending voice datum.
4. `preferences-configuration:T007` — personal-scope write **LIVE**; project-scope + tailnet identity blocked.
5. `advanced-model-playground:T007` — persistence + Sandbox **LIVE**; run/dispatch execution path unwired.
6. `plan-task-delivery:T007` — Explorer/Runs render **LIVE**; real Deliver + live PRD content blocked.
7. `reviewed-tools-plugins:T007` — catalog served **LIVE**; skill-adoption gate live-enable on a bridge blocked.

The remaining blocked-live legs across the seven are precisely: a **real tailnet
identity**; a **live project bridge + Codex + GitHub** PR/merge and State-apply;
the **advanced run/dispatch HTTP wiring**; **Explorer live PRD content** (behind
fakoli/anvil#178); an **operator-confirmed clean audible voice round-trip**
(request/response dictation and realtime speech-to-speech — mic needs a user
gesture); and the **mini-side realtime voice proxy**.

The request/response STT/TTS surfaces are no longer blocked: `voice_relay_service`
is wired to the Dark serves through the interim hub-side adapter
(`workbench/serving_audio.py`). A unified Anvil Serving `/v1/audio/*` gateway
([fakoli-serving#280](https://github.com/fakoli/anvil-serving/issues/280)) remains
noted as the clean long-term replacement for that interim adapter — an
improvement, not a blocker.

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
