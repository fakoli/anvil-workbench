# Workbench UI acceptance audit

Date: 2026-07-21  
Scope: `codex/workbench-release-readiness` candidate + chat-first-voice T004 chat surface

This audit distinguishes a functional hub-backed control from a configuration boundary. Nothing in this UI is a synthetic delivery, a raw-provider escape hatch, or a browser-side GitHub action.

## Result

| Measure | Result |
| --- | --- |
| Main navigation surfaces covered | 9 / 9 |
| Hub-backed interactive workflows covered | 7 / 7 |
| Utility and onboarding controls covered | 5 / 5 |
| Web component scenarios | **50 / 50 passed** |
| Backend and bridge contract tests | **49 / 49 passed** |
| Production web build | passed |

The component suites are [`web/src/App.test.jsx`](../web/src/App.test.jsx), [`web/src/api.test.js`](../web/src/api.test.js), and [`web/src/chat-api.test.js`](../web/src/chat-api.test.js). They mock only the HTTP boundary, then assert the request a real control makes; they do not rely on a delivery seed. Backend tests assert the corresponding durable commands and security checks, including exact verification-command allowlisting without a shell, typed operation inputs, atomic approval consumption plus lease renewal, session-lease-bound GitHub worktrees, PR-to-merge lease retention, and action-failure reconciliation.

## Control matrix

| Surface | Control | Backed operation or boundary | Test status |
| --- | --- | --- | --- |
| Chat | Rail management, transcript, composer, route select, retry/branch, live region | The default surface. Actor-scoped `/api/conversations` list/search/create/rename/archive/unarchive/delete, streamed send with cancel, and retry/branch successors — each mocked at the HTTP boundary and asserted against the request a real control makes. | Component-passed; live render deferred to T006 (below) |
| Delivery | Send delivery direction | Persists an `operator.directive` event. It is copied into the next bridge work packet for that session. | Passed |
| Sessions | New concurrent session; Start delivery | Creates a version-pinned session workflow, then queues a leased bridge run for a State task. | Passed |
| Runs | Refresh runs | Reloads durable hub run state. | Passed |
| Routes | Refresh decisions | Reads server-side Serving decision metadata filtered to known Workbench run IDs. | Passed |
| Approvals | Review action; Authorize action | Brings the hash-bound action into view; calls the approval endpoint only for a pending grant. | Passed |
| Evidence | Search evidence; Show lineage | Calls narrow cited evidence and lineage APIs. Graph output cannot approve actions. | Passed |
| Skills | Select skills; Verify bridge skills | Sends selected bridge-published ids when creating a session, then queues a non-mutating local digest probe. Names/descriptions/digests only reach the hub. | Passed |
| Sandbox | Run through Anvil Serving | Makes a bounded, audited Responses call through the server-held Serving token and allowlisted model. | Passed |
| Voice | Connect / hold to talk | Available only when the private same-origin realtime relay is configured; otherwise microphone capture is disabled. | Guard passed; live Dark endpoint remains unqualified |
| Project | New delivery | Creates only a hub project record. It never retrieves a bridge secret into the web UI after registration. | Passed |
| Setup | Help / setup guide | Walks the operator to the next incomplete *live* setup gate; it cannot manufacture completion. | Passed |
| Utilities | Operator menu; activity; mark viewed | Shows server-returned actor/audit metadata; “mark viewed” is browser-local read state only. | Passed |

## Chat surface (chat-first-voice T004)

The chat surface is the default view. Its acceptance criterion is "component AND
rendered-browser checks." This entry records the hermetic (jsdom/component) half
honestly and states plainly what is NOT yet done.

What the jsdom/component tests prove (in `web/src/App.test.jsx`,
`web/src/api.test.js`, `web/src/chat-api.test.js`):

- **Navigation 9/9, Chat default.** Chat is first in the nav and selected on
  load (`aria-current="page"`); Delivery and the other seven surfaces stay
  reachable and each render their heading.
- **Rail.** List (active/archived sections kept visibly distinct), debounced
  latest-wins search, create, rename (rendered row title updates), archive
  (row moves to Archived), and a two-step-confirm delete (row disappears).
- **Transcript.** Distinct no-conversation / empty / streaming / interrupted /
  failed / cancelled states; retry and branch render as visible successors and
  never rewrite the prior turn.
- **Composer.** Multiline with Enter-to-send / Shift+Enter newline, an IME
  composition guard, and disabled send while streaming.
- **Route.** Only the reviewed allowlist appears; an undeclared route is refused
  before any send; changing the route preserves the transcript.
- **Live region.** `role="status" aria-live="polite"` announces each lifecycle
  transition (streaming / complete / cancelled / interrupted / failed).
- **Correctness invariants.** Retry/branch post the exact `{kind:'text', text}`
  body the server's `TurnBodyInput`/`ContentBlockInput` accept (retry → sibling
  assistant, branch → follow-up user), with a revert-detecting body-equality
  assertion; and a stream that settles after the operator switches conversations
  is dropped rather than landing in — or cancelling — the wrong conversation.
- **Console health.** The Vite production build and the vitest run complete with
  no errors or unhandled rejections.

**Deferred — live rendered-browser qualification.** A rendered interaction
against a live hub + browser (real `/api/conversations` persistence, a real
streamed relay frame, real console/network capture) is NOT performed here. That
live qualification is **chat-first-voice:T006's** scope, consistent with the
not-wired-live constraint on the rest of this run. This entry does not claim a
browser render that was not done.

## Workflow coverage

1. Empty hub → no seeded delivery is rendered; setup guide opens a real project-creation flow.
2. Project with published skill metadata → session selects a skill → queued `run_codex` command contains matching digest and recorded directions.
3. Router decision refresh → only correlated Workbench IDs appear.
4. Evidence search/lineage → cited results render without a graph write or approval path.
5. Skills probe → bridge resolves local content/digest without running Codex.
6. Sandbox → explicit allowlisted Serving request returns redacted output; disallowed models are rejected server-side.
7. Pending hash-bound approval → a browser request records approval only; the local bridge still owns the GitHub action.

## Remaining live qualifications

- The local Compose hub has no configured `ANVIL_VOICE_REALTIME_URL`, so a real microphone/STT/TTS turn through Dark is not yet evidence. The relay protocol and its guard are covered by unit tests.
- The current pinned Heavy route reaches Anvil Serving and preserves correlation, but its full Codex tool loop still emits unsupported `shell_command<|channel|>commentary`; the bridge moves that attempt to reconciliation. Do not claim a successful PRD → edit → test → evidence run until a local model/template completes it.
- The rebuilt loopback stack served the current bundle and all eight navigation views rendered in the in-app browser. The live Routes refresh reads Serving's safe `records` summary and correctly shows that there is not yet a Workbench-correlated decision. A bounded `chat-fast` sandbox request completed through Anvil Serving and rendered its exact response in the browser. A correlated Routes row still requires a bridge delivery after the router's current start.
- A tailnet identity proxy, a real bridge with two worktrees, a non-production GitHub PR/merge fixture, and Neo4j retrieval still require separate live qualification.

## Re-run recipe

```powershell
Set-Location C:\Users\sdoum\ai-code\anvil-workbench
python -m pytest -q
Set-Location web
npm test -- --run
npm run build
Set-Location ..
docker compose up -d --build
```

Then use the browser to inspect the guide, all eight navigation views, a persisted delivery direction, the deliberate voice/sandbox availability states, and console output. Do not consume a real approval or create an external PR for a UI smoke check.
