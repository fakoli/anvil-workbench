# Workbench roadmap

## V1 implementation status

### Implemented in this repository

- Hub API with Postgres store, bridge registry, immutable audit events, approvals, transcript redaction, and bridge-authenticated run states (`queued -> running -> evidenced|reconciliation`).
- Project-local bridge with State CLI/event read boundary, Codex runner, structured activity/evidence, and gated GitHub actions.
- Anvil Serving Responses-compatible Codex configuration, correlation-header contract, and isolated Codex tool surface.
- Derived Neo4j projection shape and narrow retrieval/lineage/failure tools.
- React desktop workbench with purpose-specific delivery, run, route, approval, evidence, and policy-boundary views; Docker Compose deployment; and hermetic API/security tests.

### Validated locally

- Python API/security/bridge contract tests use `MemoryStore` and `NullGraph`.
- The browser shell was exercised on the loopback Compose stack; its 22 interactive controls and eight UI scenarios are covered in the [UI acceptance audit](UI-ACCEPTANCE-AUDIT.md).
- State CLI qualification proved claim, packet retrieval, evidence capture, submit, strict review, and replay without opening `state.db`; State acceptance was intentionally not approved.
- Serving qualification passed pinned Heavy/Fast preflights, Responses normal/function-continuation/JSON/SSE/cancellation checks, and a synthetic-silence STT -> Fast -> TTS voice pipeline.

### Still requiring a live qualification

1. Start the hub behind a tailnet identity proxy and verify the injected identity header is stripped and re-injected correctly.
2. Register one real project bridge and verify State work packet retrieval and event tailing without opening `state.db`.
3. Requalify Codex-through-Serving tool execution using a local model/tool template combination that emits executable Codex shell calls. The pinned Heavy accepts and traces Responses turns but emits unsupported `shell_command<|channel|>commentary` calls in a full Codex loop; it is not a passing delivery harness yet.
4. Exercise a full approved PR and merge flow against a non-production GitHub repository.
5. Verify State apply-after-merge failure creates a reconciliation item.
6. Enable Neo4j and local embeddings/reranking only after redaction and citation checks pass.

## Near-term milestones

| Milestone | Outcome | Exit evidence |
| --- | --- | --- |
| Local hub readiness | Repeatable Compose startup and browser smoke test | Health endpoints, authenticated bootstrap, responsive UI screenshot, no console errors |
| Bridge qualification | One project bridge reads State and runs a bounded Codex work packet | Redacted events, correlation trace, State database untouched |
| Delivery approval | One hash-bound PR action and merge/accept reconciliation path | Diff invalidation, replay failure, PR/check/merge evidence |
| Graph retrieval | Redacted evidence search with citations and lineage | Projection idempotency, write denial, redaction test |
| Tailnet release | Hub reachable only through the identity-aware private entry point | Header provenance test, no browser secrets, operator runbook |

## Product decisions that remain intentionally open

- Exact State CLI syntax is bridge configuration, not a hub dependency.
- The first harness is Codex; other harnesses must implement the same bridge runner contract.
- The UI remains a desktop cockpit for v1; mobile support is not a launch requirement.
- Model/policy changes remain human approvals even when initiated from Workbench.
