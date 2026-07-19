# Harness foundations

This is the research-backed design note for Anvil Workbench. It turns the word
"harness" into a concrete boundary: a durable control loop that assembles
approved context, calls a routed agent, executes bounded local tools, records
evidence, and pauses for human authority where an external effect is involved.

## Product framing

Workbench is not a generic chat client, model router, or replacement task
database. It is a private, tailnet-first delivery harness whose web UI lets an
owner supervise sessions from PRD through State plan, local implementation,
evidence, approval, and merge. Anvil State remains canonical and Anvil Serving
remains the only Workbench-managed model path.

OpenAI's description of the Codex loop provides the core mental model: build
context and tools, call the model, execute a requested tool, append the result,
and repeat until the task is complete or needs intervention. Workbench makes
the durable parts of that loop inspectable rather than attempting to hide them.
See [Unrolling the Codex agent loop](https://openai.com/index/unrolling-the-codex-agent-loop/).

## V1 kernel

| Concern | V1 decision | Why it is bounded |
| --- | --- | --- |
| Session | Durable project/worktree context, one active run, independent trace and event sequence | Avoids two Codex processes editing one session context. |
| Worktree | Named bridge configuration plus fenced expiring lease | The browser never supplies a filesystem path. |
| Workflow | Version-pinned, acyclic definition with allowlisted steps | Model output cannot become arbitrary orchestration code. |
| Checkpoint | Persist event/command before bridge effect; State and approval actions stay idempotent | Recovers from browser or worker loss without guessing. |
| Approval | Exact one-time payload hash on the local bridge | PR, merge, acceptance, deployment, and policy remain human authority. |
| Voice | Session-bound, push-to-talk relay; no stored raw audio | Voice is a transport, not a route or approval bypass. |

The initial step vocabulary is deliberately small:

```text
agent | tool | condition | fan_out | join | approval_wait |
evidence_submit | reconcile | cancel
```

The initial delivery template is `agent -> approval_wait -> reconcile`. The
agent step can claim, edit, test, and submit evidence through the bridge. The
review gate does not itself make a PR or merge happen; existing hash-bound
approval actions retain those powers.

## Dynamic workflows: what we mean

"Dynamic workflow" means a model may propose a new instance of an approved,
versioned definition before it begins. It does **not** mean a model can mutate a
paused workflow, add arbitrary code, acquire a new privilege, or route around
an approval.

This follows the important durability lessons in LangGraph: persist execution
state, make effects idempotent, and stop for a resumable human interrupt rather
than holding an in-memory process open. LangGraph itself documents checkpointed
interrupt/resume, serial execution per thread, and idempotency guidance:
[interrupts](https://langchain-ai.github.io/langgraph/concepts/breakpoints/),
[server lifecycle](https://langchain-ai.github.io/langgraph/concepts/langgraph_server/),
and [idempotency](https://langchain-ai.github.io/langgraph/how-tos/state-reducers/).

Claude Code is a useful ergonomic reference for session IDs/resume, structured
stream output, per-run model selection, and tool policies. There is no stable
public contract to clone for a generic "dynamic workflow" feature, so
Workbench owns this constrained definition instead. See the
[Claude Code CLI reference](https://docs.anthropic.com/en/docs/claude-code/cli-usage).

## Voice

The first voice interaction is push-to-talk: connect a specific session, hold
to stream input audio, release to commit, then play the streamed response. The
relay permits interruption and disconnect but never grants a privileged action.
Stored events are lifecycle/evidence summaries; raw audio is transient and
transcripts are opt-in retention only.

LiveKit's agent-session and turn/interrupt documentation was used as a pattern
for keeping session ownership, turn interruption, and worker dispatch explicit:
[agent sessions](https://docs.livekit.io/agents/logic/sessions/),
[turns and interruption](https://docs.livekit.io/agents/logic/turns/), and
[agent dispatch](https://docs.livekit.io/agents/server/agent-dispatch/).

## Failure modes to test before calling it qualified

1. Duplicate run / expired or stolen worktree lease.
2. Browser refresh or bridge restart between a State claim, evidence submission,
   PR action, merge, and State acceptance.
3. A workflow-definition revision while paused at approval.
4. A voice interruption, invalid client event, upstream drop, or audio playback
   failure without persistence of raw audio.
5. Model tool calls that request unknown resources, malformed functions, or a
   prohibited effect.
6. Approval payload/diff drift, expiry, replay, check failure, merge failure,
   and merge-before-State-acceptance reconciliation.

## Next evolution, only after V1 qualification

- A visual workflow editor that produces the same validated JSON, not a second
  execution engine.
- Fan-out subagents with explicit resource leases and join evidence.
- Scheduled or long-lived workflows only when their checkpoint/retry semantics
  require a dedicated durable runtime such as Temporal.
- Additional harnesses through the same bridge runner contract.

Do not adopt a framework merely because it is an agent framework. The safety
boundary is the durable contract above, not a model vendor or orchestration
library.
