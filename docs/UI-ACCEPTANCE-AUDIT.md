# Workbench UI acceptance audit

Date: 2026-07-21 (component final state) · **2026-07-22 rendered-browser live
addendum** (see [the live-run section](#2026-07-22-rendered-browser-live-run-real-tailnet-stack))
Scope: every browser surface delivered across PRs #9–#35.

This audit draws one line per surface: **component-proven** (jsdom/vitest, HTTP
boundary mocked) versus **rendered-browser-deferred** (a real render against a
live hub + browser with console/network capture). Every browser surface in this
run is proven at the component level with the network mocked; the
rendered-browser interaction + console-health check is deferred to each PRD's
live-qualification task. Nothing here is a synthetic delivery, a raw-provider
escape hatch, or a browser-side GitHub action.

## Result

| Measure | Result |
| --- | --- |
| Web component test files | 12 |
| Web component scenarios | **290 / 290 passed** |
| Production web build (`vite build`) | passed, no console errors |
| Backend + bridge contract tests | **1216 / 1216 passed** (2026-07-22, after the voice request/response wiring; 1135 at the 2026-07-21 final state) |
| Web component scenarios (2026-07-22) | **310 passed** |

Component suites live under `web/src/*.test.js[x]`. They mock only the HTTP
boundary, then assert the exact request a real control makes; none relies on a
delivery seed.

## Per-surface acceptance

| Surface | Component-proven (vitest files — count) | What the component tests prove | Rendered-browser deferred to |
| --- | --- | --- | --- |
| **Chat** (default view) | `App.test.jsx` (98, shared with nav), `chat-api.test.js` (9), `api.test.js` (59, shared) | Nav 9/9 with Chat default (`aria-current="page"`); rail list/search/create/rename/archive/two-step delete; transcript states (empty/streaming/interrupted/failed/cancelled); composer Enter/Shift+Enter + IME guard + disabled-while-streaming; route allowlist with undeclared-route refusal before send; `role="status"` live region; retry/branch post the exact `TurnBodyInput` body (revert-detecting) and never rewrite the prior turn; a stream settling after a conversation switch is dropped, not mislanded. | **chat-first-voice:T004 / T006** · chat **rendered live 2026-07-22** (see below) |
| **Settings** | `settings-view.test.jsx` (18), `settings.test.js` (13) | Scope-precedence rendering (policy>deployment>project>personal); a policy-ceiling value cannot be exceeded from a personal write; secret/path descriptors are not rendered/editable; optimistic-version conflict surfaces without a fabricated success. | **preferences-configuration:T007** · a real personal write **rendered live 2026-07-22** (see below) |
| **Configuration** (export / import / reset) | `configuration-view.test.jsx` (8), `configuration.test.js` (10) | Export renders a redacted, scrubbed payload; import validates a closed schema and refuses unknown fields; reset is explicit and scoped. | **preferences-configuration:T007** |
| **Plugins catalog** | `plugin-catalog-view.test.jsx` (12), `plugin-catalog.test.js` (15) | Approved-catalog discovery renders reviewed plugins only; permission UI shows declared read-only/mock tool kinds; no browser path can grant a new privilege by name. | **reviewed-tools-plugins:T007** · catalog **served live 2026-07-22** (see below) |
| **Advanced Chat / playground** | `advanced-chat.test.js` (14), `advanced-playground.test.js` (11) | Advanced controls are the declared, in-bounds route capabilities only; a vanished/drifted route surfaces `repair_required`, never a silent substitution; parallel-dispatch preflight refuses undeclared/over-budget/over-concurrency before any transport; an advanced record is refused as authoritative evidence. Client degrades to 503 when the run/dispatch path is unrouted. | **advanced-model-playground:T007** · persistence + Sandbox **rendered live 2026-07-22** (see below) |
| **Deliver** (delivery explorer + controls) | `delivery-explorer.test.js` (23), `App.test.jsx` (shared) | PRD/plan/task/eligibility browse over the read-only, project-scoped, re-scrubbed projection; Deliver control persists a typed accepted/denied receipt; a denied `safe_summary` is scrubbed; setup sheet walks to the next incomplete live gate and cannot manufacture completion. | **plan-task-delivery:T007** · Explorer/Runs **rendered live 2026-07-22**; Deliver fails closed, no bridge (see below) |
| **Voice** | `App.test.jsx` (guard, shared) | Push-to-talk is available only when the private same-origin realtime relay is configured; otherwise microphone capture is disabled and the control is truthfully off. No model/tool control on the relay; no raw-audio persistence. | **chat-first-voice:T005 / T006** · request/response STT-dictation + read-aloud **LIVE 2026-07-22** (transcribe/speak 200, no-leak, no-mutation, correct sample-rate); realtime relay **connected live** (transport only); the sole pending item is an operator-confirmed audible spoken turn — see below |

## 2026-07-22 rendered-browser live run (real tailnet stack)

On **2026-07-22** several surfaces were rendered live in a **real browser**
against a real hub (`127.0.0.1:8090`, Postgres + Neo4j, rebuilt from the merged
live-qualification branch) wired to a real Anvil Serving router
(`100.87.34.66:8000`, 8 routes). This is the first rendered-browser evidence for
these surfaces; it is a **surface** record, not a delivery-harness qualification.
No PR/merge, State-apply, real tailnet identity, or real project-bridge effect
occurred. Full evidence and caveats live in
[QUALIFICATION.md → live-qualification section](QUALIFICATION.md#live-qualification-observed-2026-07-22-real-tailnet-stack).

| Surface | 2026-07-22 disposition | What rendered live | Named leg still deferred |
| --- | --- | --- | --- |
| **Chat** | **LIVE** | Multi-turn streamed chat on both a Fast route (`chat`→`fast-local`) and a Heavy route (`planning`→`heavy-local`, 251 tokens) via the now-mounted `POST /api/conversations/{id}/send`; conversation memory (a follow-up recalled "42"); a real mid-stream cancel settled `cancelled` with partial text preserved; `role="status"` announced "Response complete"; keyboard-only send. Decisions carried safe metadata only (prompt/messages/response omitted). | A real Dark STT→Fast→TTS audible turn (see Voice). |
| **Voice — realtime relay** | **LIVE-PARTIAL** | The realtime speech-to-speech relay (dedicated panel, via `ANVIL_VOICE_REALTIME_URL`) connected end-to-end — the hub accepted the session websocket `/api/sessions/{id}/voice/realtime`; the proxy counter showed `claims_total=1` (transport/connection). | A **clean audible spoken round-trip**, not operator-confirmed post the 16 kHz sample-rate fix (`VOICE_RELAY_SAMPLE_RATE=16000`); Chrome mic needs a trusted user gesture, so it can't be script-driven. |
| **Voice — request/response** (STT dictation + read-aloud) | **LIVE** | Now wired (`voice_relay_service` + `workbench/serving_audio.py` `DarkServingAudioTransport` → parakeet STT `:30010` / kokoro TTS `:30011`, env-configured). `POST /api/chat/voice/transcribe` → 200 with a real draft transcript, **no** conversation turn (`turns=0`); the rendered-browser "Read this response aloud" button fired `POST /api/chat/voice/speak` → 200, kokoro returned ~1.64 s PCM at `sample_rate=24000`, the client honored the server sample-rate (fixes the 16 k/24 k garble class), playback succeeded with no console errors, **no** message/conversation mutation (byte-identical). No-leak verified live (no raw audio/base64 in the record; content-free api logs). | An **operator-confirmed audible spoken dictation** turn — browser mic needs a real user gesture, so it can't be script-driven. (The interim hub-side adapter's clean long-term replacement is a unified Anvil Serving `/v1/audio/*` gateway, [fakoli-serving#280](https://github.com/fakoli/anvil-serving/issues/280) — an improvement, not a blocker.) |
| **Settings** | **LIVE** | The reviewed catalog (12 settings, scope-ownership chips) rendered; a real write — Voice auto-play Off→On, `PUT /api/preferences/personal.voice_autoplay` 200 — flipped provenance to "Set at the personal scope" and surfaced the optimistic version. Leak-scans of live `/api/preferences`, `/api/chat/routes`, `/api/system/configuration` found zero secrets/paths/tokens. | Project-scoped writes against a **real tailnet identity** (the run used the dev loopback actor override). |
| **Configuration** | not separately exercised | — | Rendered-browser export/import/reset against a real hub. |
| **Plugins catalog** | **LIVE** | The digest-pinned reviewed capability catalog served live (`WORKBENCH_PLUGIN_CATALOG_FILE` + `WORKBENCH_PLUGIN_CAPABILITY_FILE`): reviewed plugin(s) version/digest/publisher-pinned, credentials by reference only, Serving routes available, delivery operations hash-bound. | Live operator-enable of the skill-adoption gate on a **real bridge**. |
| **Advanced Chat / playground** | **LIVE-PARTIAL** | The preset/template/rating persistence surfaces served 200 live; a bounded `chat-fast` Sandbox Responses turn ran through Serving and rendered (`WORKBENCH_BROWSER_LIVE_OK`). | The advanced **run/dispatch execution path** stays unrouted (client degrades to 503) — no live trace / route-supported-vs-assumed distinction. |
| **Deliver** | **LIVE-PARTIAL** | The Explorer rendered the named PRD→task hierarchy; the Runs list now leads each row with a human title ("Planning run for task T001") and demotes ids to secondary machine text, preserving a durable reconciliation row; console clean; keyboard nav. | A real Deliver against a **live project bridge** (correctly fails closed `deliver_no_session` with no bridge); live PRD **content** is fail-closed on the seed (fakoli/anvil#178). |

## Why rendered-browser is deferred (not-wired-live boundaries)

- The chat send/stream join `POST /api/conversations/{id}/send` **is now mounted**
  (as of 2026-07-22; `RelayEvent` emits `delta`/`terminal`, each `seq`-stamped).
  The inline `RelayEvent` `resolution` kind is still **not added**; the mark
  surface `GET /api/chat/route-resolutions` **is** wired and tested.
- The advanced **run/dispatch execution path** (`advanced_routes`/
  `advanced_runtime`/`advanced_dispatch`) is **not wired into any HTTP
  endpoint**; advanced preset/template/rating persistence routers are mounted but
  back injected stores that stay `None`→503 by default.
- The delivery projection, run-context, project-context, and voice-relay routers
  are **inject-or-503**: they receive a store/service that is `None` in
  `create_app`'s live loop, so each returns 503 until a real backend is injected.
- The skill-adoption gate is **operator-enablable** (`--skill-adoption-ledger`)
  but **ungated by default**.
- No local Compose deployment has a live tailnet identity, a real project bridge
  with worktrees, a non-production GitHub PR/merge fixture, a live Dark voice
  endpoint, or a live Neo4j — all four remain live-qualification work behind
  [fakoli/anvil#178](https://github.com/fakoli/anvil/issues/178).

## Prior live render observation (2026-07-19)

The rebuilt loopback stack served the bundle and all navigation views rendered in
the in-app browser; a bounded `chat-fast` sandbox request completed through Anvil
Serving and rendered its response; Voice correctly showed disabled without a
relay. This is a **render/transport observation, not a rendered-browser
qualification** of any surface — the per-surface deferrals above still stand.

## Re-run recipe

```powershell
Set-Location C:\Users\sdoum\ai-code\anvil-workbench
python -m pytest -q
Set-Location web
npm ci
npm test -- --run
npm run build
Set-Location ..
$env:ANVIL_ROUTER_TOKEN = "placeholder-host-token"
docker compose --env-file .env.example config -q
```

Then, for a live qualification, start the stack with real secrets and use the
browser to inspect the guide, every navigation view, a persisted delivery
direction, the voice/sandbox availability states, and console output. Do not
consume a real approval or create an external PR for a UI smoke check.
