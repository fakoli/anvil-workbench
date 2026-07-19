# Workbench UI acceptance audit

Date: 2026-07-19  
Scope: current `codex/workbench-release-readiness` candidate, exercised against the React component suite and loopback Compose shell.

This is a control-level audit, not evidence that an external GitHub or model action occurred. Browser controls may review or request an action; the project-local bridge remains the only component permitted to perform GitHub actions.

## Result

| Measure | Result |
| --- | --- |
| Existing delivery controls covered | 22 / 22 |
| New harness controls covered | 6 / 6 |
| Total interactive controls covered | **28 / 28 (100%)** |
| UI scenarios | **10 / 10 passed** |
| Browser build | passed |
| Backend and bridge contract suite | 23 / 23 passed |

The exhaustive control matrix is automated in `web/src/App.test.jsx`. The loopback browser check is a separate visual and console smoke test; it does not consume an approval or create an external project, PR, or merge.

## Control matrix

| Surface | Control(s) | Expected result | Status |
| --- | --- | --- | --- |
| Live data | Hub bootstrap | Live runs replace seeded route and evidence placeholders; an unevidenced run says so explicitly. | Passed |
| Navigation rail | Delivery, Runs, Routes, Approvals, Evidence, Sandbox | Each view renders its own review surface rather than an empty placeholder. | Passed |
| Delivery | Task task_48 | Expands and collapses State task context. | Passed |
| Delivery | Send delivery direction | Adds the note locally and shows a dismissible queued notice. | Passed |
| Trace | Show correlation trace | Expands and collapses run, task, and request correlation IDs. | Passed |
| Trace | View all | Opens Evidence review. | Passed |
| Approval | Authorize action | Calls the approval API and releases the action only on success. | Passed |
| Approval failure | Authorize action | Keeps the PR action pending and explains the failure when the hub rejects it. | Passed |
| Project creation | New delivery, dialog close, Cancel, Create project | Opens and closes safely; creates only a hub project record through the API. | Passed |
| Utilities | Operator menu, Help, Notifications, Mark all read, dialog/menu close | Reveals, updates, and closes the relevant utility surfaces. | Passed |
| Notice | Dismiss notification | Clears the queued/failure notice. | Passed |
| Harness sessions | Sessions, New concurrent session, Create session | Creates a durable named-worktree session without ever accepting a path from the browser. | Passed live against the loopback Compose stack |
| Harness sessions | Start delivery, Cancel | Opens the version-pinned bridge-delivery form for the selected session; it stays disabled until a State task id is supplied. | Passed live; no bridge delivery was started |
| Voice | Voice unavailable | Explicitly refuses microphone access until a private Anvil Voice Realtime endpoint is configured. | Passed live as an intentional guard |

## Workflows exercised

| Workflow | What was exercised | Status |
| --- | --- | --- |
| Orient and review a delivery | Navigation, task context, correlation trace, route metadata, evidence view. | Passed |
| Give delivery direction | A new operator note appears in the conversation with a visible queued state. | Passed |
| Create a Workbench project record | Form validation and the API request contract, without bridge credentials in the browser. | Passed |
| Review and authorize a PR action | Success and hub-rejection paths; rejection must not make the UI look authorized. | Passed |
| Inspect the sandbox policy | The sandbox is explicitly read-only and does not permit a browser-to-provider bypass. | Passed as an intentional boundary |
| Supervise concurrent sessions | Two independently named bridge worktrees render at once; each has a separate Start delivery control. | Passed live against the loopback Compose stack |
| Start a session workflow safely | The selected session opens a task-id-required bridge form; cancelling makes no run or State claim. | Passed live |
| Voice policy guard | Voice is visibly disabled in the development stack until its private Realtime upstream is configured. | Passed as an intentional boundary |

## Deliberately unavailable or still unqualified

These are visible product boundaries, not broken buttons:

- The model sandbox has no request button yet. It needs a server-side redaction and immutable-audit contract before it can make routed model calls.
- The local Compose test stack intentionally has no Dark Anvil Voice Realtime endpoint. The push-to-talk relay and redaction policy are covered by contract tests; a live microphone/STT/TTS qualification still requires the Dark endpoint and tailnet identity configuration.
- Creating a project does not auto-register a bridge, launch Codex, or receive a token. Those are local bridge operations and must stay outside the browser credential boundary.
- The browser cannot commit, create a GitHub PR, merge, apply State acceptance, or change a model policy. It can only record a hash-bound approval; the local bridge performs the approved action.
- Tailnet identity proxy, a live project bridge, non-production GitHub PR/merge, and Neo4j retrieval require separate live qualification.
- The pinned Heavy route accepts the Responses subset but remains blocked for a complete Codex tool loop: it can emit the non-executable tool name `shell_command<|channel|>commentary`. The bridge correctly marks that run for reconciliation rather than submitting evidence or creating a PR.

## Re-run recipe

```powershell
Set-Location C:\Users\sdoum\ai-code\anvil-workbench\web
npm test
npm run build
Set-Location ..
python -m pytest -q
docker compose up -d --build
```

Then open `http://127.0.0.1:8090`, confirm the page renders without console errors, and perform a non-destructive smoke check such as create two disposable named sessions, open/cancel **Start delivery**, open/close **New delivery**, navigate to **Runs**, and submit/dismiss a local delivery direction. Do not use the browser to consume a real approval merely to smoke-test the visual shell.
