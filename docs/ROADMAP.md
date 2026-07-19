# Workbench roadmap

## V1 implementation status

### Implemented in this repository

- Hub API with Postgres store, bridge registry, immutable audit events, approvals, and transcript redaction.
- Project-local bridge with State CLI/event read boundary, Codex runner, structured activity/evidence, and gated GitHub actions.
- Anvil Serving Responses-compatible Codex configuration and correlation-header contract.
- Derived Neo4j projection shape and narrow retrieval/lineage/failure tools.
- React desktop workbench shell, Docker Compose deployment, and hermetic API/security tests.

### Validated locally

- Python API/security contract tests use `MemoryStore` and `NullGraph`.
- The frontend builds with Vite.
- The Compose topology is configured for loopback browser access, Postgres, Neo4j, API, and web proxy.

### Still requiring a live qualification

1. Start the hub behind a tailnet identity proxy and verify the injected identity header is stripped and re-injected correctly.
2. Register one real project bridge and verify State work packet retrieval and event tailing without opening `state.db`.
3. Run Codex through Anvil Serving’s `/v1/responses` route using the selected local model.
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
