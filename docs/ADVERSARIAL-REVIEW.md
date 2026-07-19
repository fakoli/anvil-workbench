# Harness adversarial review

Date: 2026-07-19
Scope: session, workflow, bridge, voice, and operation-contract additions on
`codex/workbench-release-readiness`.

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
| Confirmed | Put `python -m pytest -q; git push --force` in a State work packet. | Packet prose could reach command execution without a bridge-owned command policy. | Remediated: packet commands must exactly match an operator `--verification-command` allowlist and execute as argv with no shell; task/actor template values are restricted; covered by rejection tests. |
| Confirmed | Bind a PR action to `checkout-a` while the bridge defaulted to another checkout. | Approved GitHub actions could use the default project root instead of the session worktree. | Remediated: every GitHub effect resolves only the active run's locally configured worktree and executes State apply there too; covered by a two-worktree regression test. |
| Confirmed | Expire/reassign a worktree lease after action preflight but before PR/merge side effects. | A one-time lease check left a time-of-check/time-of-use window around GitHub effects. | Remediated: the hub atomically validates approval, run, session/worktree/epoch, and payload hash while renewing the lease; the bridge renews it for the full action. |
| Confirmed | Let a PR/merge/State-apply action fail after consuming its approval. | The run stayed `evidenced` and the delivery command could retry despite an unknown partial effect. | Remediated: `evidenced -> reconciliation` is explicit, the workflow is reconciled, the failed command is acknowledged, and no action is retried automatically; a successful PR retains its lease until merge/accept. |
| Confirmed | Send `raw_command` input or a self-declared approval to a V2 operation command. | Schema-shaped bridge commands did not enforce the selected operation input schema or a real grant consumption. | Remediated: typed input validation runs against the pinned descriptor and human-gated actions require a bridge-injected atomic approval consumer; covered by contract tests. |
| Confirmed | Push a new commit to an approved PR before merge, or point its approval at another State task. | The merge action accepted a mutable PR reference and browser-supplied task identifier without comparing them to the approval/run. | Remediated: PR creation records a head SHA; merge requires and checks that exact SHA, uses GitHub head compare-and-swap merge, and requires the task ID to match the evidenced run. |
| Confirmed | Approve `state_apply`, deployment, or model-policy action before its bridge adapter exists. | The generic approval endpoint queued a command the V1 bridge deliberately could not execute, leaving it delivery-leased forever. | Remediated: V1 rejects non-executable approval actions until an explicit adapter, contract, and reconciliation path exist. |
| Confirmed | Lose the bridge response after merge and State acceptance but before its command acknowledgement. | A completed delivery could be redelivered against an expired lease and remain stuck. | Remediated: consumed-merge completion deletes/acknowledges the matching bridge command in the same durable operation that marks the run completed and releases its lease. |
| Refuted | Browser-controlled worktree path such as `..\\other-project` reaches a local runner. | The browser sends an id only; the bridge resolves it exclusively from `--worktree ID=PATH`. | Guarded by bridge resolution and unit coverage. |
| Refuted | Voice client injects a model, tool, or arbitrary prompt through a Realtime event. | Relay accepts a small event allowlist and replaces `response.create` with fixed audio/text modalities. | Guarded by sanitization and invalid-event contract test. |
| Plausible deployment risk | A tailnet identity proxy forwards user-supplied identity headers instead of stripping/re-injecting them. | The app intentionally trusts only the configured proxy header. | Remains an operator deployment requirement; Compose is loopback-only and production must place a verified tailnet identity proxy in front of the hub. |

## Review result

No unresolved confirmed Workbench code-boundary issue remains in this candidate
after the second adversarial pass. The remediated action path still needs the
separate, explicitly scoped live GitHub/State fixture qualification below.
The live Fast sandbox route is qualified, but there is not yet a
Workbench-correlated delivery decision after the router's current start. The
configured Heavy serve is stopped on its own incompatible MTP quantization
recipe, and live Dark Realtime microphone/STT/TTS plus the full local-model
Codex tool loop remain separate qualification work; none is represented as
passed by this review.
