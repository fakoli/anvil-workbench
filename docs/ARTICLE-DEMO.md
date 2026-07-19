# Future article and demo outline

The public story should lead with a real delivery record, not architecture promises. Use this document once the live Codex tool-loop qualification in [QUALIFICATION.md](QUALIFICATION.md) is passing.

## Thesis

Anvil Workbench makes a private delivery loop legible: State owns what must be true, Serving owns where the model ran, the bridge owns local execution, and a human owns irreversible actions.

## Recommended narrative

1. Start with a small PRD and its State task graph, not a generic chat window.
2. Show the Workbench run receiving a canonical State work packet and the selected Fast/Heavy route.
3. Show Codex editing and testing locally through Anvil Serving, then surface the exact verification evidence and route trace.
4. Show a failure before a success: changed diff invalidates approval, failed verification cannot submit evidence, or a merge/State mismatch becomes reconciliation work.
5. Show the human hash-bound PR approval and, only in a disposable repository, merge followed by State acceptance.
6. Close with the graph as retrieval and lineage, not as an autonomous authority.

## Capture checklist

- State PRD, task, claim, work packet, evidence, and acceptance identifiers.
- Anvil Serving normal, SSE, tool-result continuation, schema, cancellation, and correlation evidence.
- Workbench diff, redacted transcript, route trace, run status, and approval hash.
- GitHub PR/check/merge evidence from a non-production fixture.
- Graph citations and a rejected graph-write attempt.
- Voice transport timings only if the demo uses a clearly disclosed synthetic or real audio sample.

## Claims that require a demonstrated record

- One project completed PRD -> task -> local edit/test -> State evidence -> approved PR -> merge -> State acceptance.
- Every Workbench model call used Anvil Serving and no raw provider path.
- Exact model revisions, hardware, engine settings, and the measured test result.

## Claims to avoid until separately proven

- General quality superiority of GPT-OSS Puzzle over the rollback model.
- Production-grade voice quality, STT accuracy, latency under load, or tailnet identity hardening.
- Autonomous approval, autonomous deployment, or Neo4j-authorized actions.
