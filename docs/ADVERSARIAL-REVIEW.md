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
| Refuted | Browser-controlled worktree path such as `..\\other-project` reaches a local runner. | The browser sends an id only; the bridge resolves it exclusively from `--worktree ID=PATH`. | Guarded by bridge resolution and unit coverage. |
| Refuted | Voice client injects a model, tool, or arbitrary prompt through a Realtime event. | Relay accepts a small event allowlist and replaces `response.create` with fixed audio/text modalities. | Guarded by sanitization and invalid-event contract test. |
| Plausible deployment risk | A tailnet identity proxy forwards user-supplied identity headers instead of stripping/re-injecting them. | The app intentionally trusts only the configured proxy header. | Remains an operator deployment requirement; Compose is loopback-only and production must place a verified tailnet identity proxy in front of the hub. |

## Review result

No unresolved confirmed harness-boundary issue remains in this candidate. The
live Dark Realtime microphone/STT/TTS test and the full local-model Codex tool
loop remain separate qualification work; neither is represented as passed by
this review.
