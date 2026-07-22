# Workbench UI acceptance audit

Date: 2026-07-21 (final state of the multi-PRD delivery run)
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
| Backend + bridge contract tests | **1135 / 1135 passed** |

Component suites live under `web/src/*.test.js[x]`. They mock only the HTTP
boundary, then assert the exact request a real control makes; none relies on a
delivery seed.

## Per-surface acceptance

| Surface | Component-proven (vitest files — count) | What the component tests prove | Rendered-browser deferred to |
| --- | --- | --- | --- |
| **Chat** (default view) | `App.test.jsx` (98, shared with nav), `chat-api.test.js` (9), `api.test.js` (59, shared) | Nav 9/9 with Chat default (`aria-current="page"`); rail list/search/create/rename/archive/two-step delete; transcript states (empty/streaming/interrupted/failed/cancelled); composer Enter/Shift+Enter + IME guard + disabled-while-streaming; route allowlist with undeclared-route refusal before send; `role="status"` live region; retry/branch post the exact `TurnBodyInput` body (revert-detecting) and never rewrite the prior turn; a stream settling after a conversation switch is dropped, not mislanded. | **chat-first-voice:T004 / T006** |
| **Settings** | `settings-view.test.jsx` (18), `settings.test.js` (13) | Scope-precedence rendering (policy>deployment>project>personal); a policy-ceiling value cannot be exceeded from a personal write; secret/path descriptors are not rendered/editable; optimistic-version conflict surfaces without a fabricated success. | **preferences-configuration:T007** |
| **Configuration** (export / import / reset) | `configuration-view.test.jsx` (8), `configuration.test.js` (10) | Export renders a redacted, scrubbed payload; import validates a closed schema and refuses unknown fields; reset is explicit and scoped. | **preferences-configuration:T007** |
| **Plugins catalog** | `plugin-catalog-view.test.jsx` (12), `plugin-catalog.test.js` (15) | Approved-catalog discovery renders reviewed plugins only; permission UI shows declared read-only/mock tool kinds; no browser path can grant a new privilege by name. | **reviewed-tools-plugins:T007** |
| **Advanced Chat / playground** | `advanced-chat.test.js` (14), `advanced-playground.test.js` (11) | Advanced controls are the declared, in-bounds route capabilities only; a vanished/drifted route surfaces `repair_required`, never a silent substitution; parallel-dispatch preflight refuses undeclared/over-budget/over-concurrency before any transport; an advanced record is refused as authoritative evidence. Client degrades to 503 when the run/dispatch path is unrouted. | **advanced-model-playground:T007** |
| **Deliver** (delivery explorer + controls) | `delivery-explorer.test.js` (23), `App.test.jsx` (shared) | PRD/plan/task/eligibility browse over the read-only, project-scoped, re-scrubbed projection; Deliver control persists a typed accepted/denied receipt; a denied `safe_summary` is scrubbed; setup sheet walks to the next incomplete live gate and cannot manufacture completion. | **plan-task-delivery:T007** |
| **Voice** | `App.test.jsx` (guard, shared) | Push-to-talk is available only when the private same-origin realtime relay is configured; otherwise microphone capture is disabled and the control is truthfully off. No model/tool control on the relay; no raw-audio persistence. | **chat-first-voice:T005 / T006** |

## Why rendered-browser is deferred (not-wired-live boundaries)

- The chat send/route-resolution join (`/api/conversations/{id}/send`, a
  `RelayEvent` `resolution` kind) is **not mounted** — `RelayEvent` emits only
  `delta`/`terminal`. The mark surface `GET /api/chat/route-resolutions` **is**
  wired and tested.
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
