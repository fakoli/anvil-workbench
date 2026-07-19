# Harness adversarial review

Date: 2026-07-19
Scope: session, workflow, bridge, and voice additions on `codex/workbench-release-readiness`.

This review attacked the delivery harness at its trust boundaries rather than
reusing its feature tests: a bridge must not cross projects, a fetched command
must survive a worker loss, a long run must keep a valid worktree fence, and a
browser must not gain a privileged path through voice or the graph.

## Findings and disposition

| Rank | Probe | Result | Disposition |
| --- | --- | --- | --- |
| Confirmed | Authenticate bridge B, then submit a transcript for a run owned by bridge A. | The original event endpoint accepted only the run id. | Remediated: store and API now verify run ownership against the authenticated bridge; covered by a cross-project API test. |
| Confirmed | Authenticate bridge B, then project evidence into project A. | The original evidence endpoint trusted the request project id. | Remediated: the hub now checks `project.bridge_id`; covered by the same isolation test. |
| Confirmed | Bridge process exits after fetching a command but before work begins. | The old queue deleted the command at fetch time, stranding a queued run. | Remediated: commands are delivery-leased, retryable after expiry, and acknowledged only after a terminal run state. |
| Confirmed | Codex takes longer than the original five-minute worktree lease. | A second session could eventually acquire the worktree. | Remediated: the bridge renews its exact lease epoch every minute; a renewal failure blocks evidence submission and reconciles the run. |
| Confirmed | Start a workflow whose selected skill has not been published by its project bridge. | A run and workflow could be created before queueing rejected the missing skill, leaving misleading durable state. | Remediated: the API now verifies every selected skill against the project bridge before creating a run or starting the workflow; covered by a regression test. |
| Confirmed | Change a local skill after a probe is queued, then let the bridge resolve its digest. | The command failed closed but stayed leased/retryable without evidence, making the Skills view look indefinitely queued. | Remediated: the bridge records a reconciliation evidence artifact, acknowledges the command, and fails closed; covered by a regression test. |
| Confirmed | Choose a published skill in the new-session UI and inspect the browser request. | The UI showed a selected skill, but its API helper silently omitted `skills`, so the eventual work packet never received it. | Remediated: the helper now serializes selected skill ids; covered by a direct browser API test. |
| Confirmed | Read a standard Anvil Serving `/v1/responses` result through the Sandbox UI. | Serving emitted the documented `output[]` message shape, while Workbench only read an optional `output_text` convenience field and rendered an empty result. | Remediated: the server-side adapter now extracts `output_text` parts from the standard response shape; covered by a router adapter test and live Fast-route request. |
| Confirmed | Refresh Routes against the current Anvil Serving router. | Serving's redacted decision summary used `records`, while Workbench only accepted a legacy `decisions` field and reported a false availability error. | Remediated: the adapter accepts the shipped safe summary shape and preserves correlation filtering; covered by a router adapter test and live browser refresh. |
| Refuted | Browser-controlled worktree path such as `..\\other-project` reaches a local runner. | The browser sends an id only; the bridge resolves it exclusively from `--worktree ID=PATH`. | Guarded by bridge resolution and unit coverage. |
| Refuted | Voice client injects a model, tool, or arbitrary prompt through a Realtime event. | Relay accepts a small event allowlist and replaces `response.create` with fixed audio/text modalities. | Guarded by sanitization and invalid-event contract test. |
| Plausible deployment risk | A tailnet identity proxy forwards user-supplied identity headers instead of stripping/re-injecting them. | The app intentionally trusts only the configured proxy header. | Remains an operator deployment requirement; Compose is loopback-only and production must place a verified tailnet identity proxy in front of the hub. |

## Review result

No unresolved confirmed Workbench code-boundary issue remains in this candidate.
The live Fast sandbox route is qualified, but there is not yet a
Workbench-correlated delivery decision after the router's current start. The
configured Heavy serve is stopped on its own incompatible MTP quantization
recipe, and live Dark Realtime microphone/STT/TTS plus the full local-model
Codex tool loop remain separate qualification work; none is represented as
passed by this review.
