# Local qualification record

Date: 2026-07-19

This is an evidence record, not a promotion decision. It distinguishes the tested v1 boundaries from the remaining live agent-harness blocker.

## Passed local checks

| Area | Evidence | Result |
| --- | --- | --- |
| Workbench API and bridge contracts | `python -m pytest -q` | 31 passed; covers State packet/event isolation, independent verification capture, evidence-submit ordering, approval replay/diff invalidation, fenced/renewed worktree leases, bridge command recovery, bridge-project isolation, version-pinned workflows, voice redaction/error behavior, bridge-local skill digest binding/probing, selected-skill forwarding, router decision-summary compatibility, standard Responses output extraction, directives, and Serving-only sandbox boundaries. |
| Browser shell | Loopback stack at `http://127.0.0.1:8090` | The rebuilt production bundle rendered Delivery plus all seven secondary navigation views. Routes read the live safe summary and showed no correlated Workbench decision. Sandbox made a bounded `chat-fast` request through Serving and rendered `WORKBENCH_BROWSER_OK`; Voice accurately remained disabled without a private relay endpoint. |
| State CLI | Disposable `anvil-workbench-state-e2e-proven` fixture | Claim, packet, verification capture, evidence submit, strict review, and replay passed. No State database was opened or modified directly. State accept/approve was intentionally not performed. |
| Heavy model plane | `nvidia/gpt-oss-puzzle-88B@9c0e0746a0d2218b28cc7b2cb3ce4e1a2f50fdb2` on the pinned Anvil vLLM image | Smoke, JSON, 120K needle, and 20-tool preflight passed. This is not evidence of general quality superiority. |
| Fast model plane | `leon-se/gemma-4-E4B-it-FP8-Dynamic@56e30bf603d18a4972caffafa1bb4a4f9a841dee` | Smoke, JSON, 30K context, and 12-tool preflight passed. |
| Responses subset | Anvil Serving candidate router | Normal response, function call, function-result continuation, JSON schema, SSE order, cancellation, correlation headers, and explicit unsupported-feature failures passed. |
| Voice plane | Dark STT -> Fast -> TTS | Synthetic-silence loop completed: STT 58.42 ms, LLM 668.50 ms, TTS 547.73 ms, total 1274.66 ms. This is transport evidence only, not speech-recognition or voice-quality validation. |

## Codex-through-Serving result

The bridge successfully claimed the State task, fetched the State packet, created a local Codex run through Anvil Serving's `/v1/responses` endpoint, recorded correlated route decisions, and captured independent verification failure. Initial compatibility gaps were closed by accepting stateless Codex request controls and blocking hosted/non-bridge tools.

The remaining blocker is model/harness tool compatibility: the pinned Heavy produces `shell_command<|channel|>commentary` in a full Codex turn, which Codex cannot execute as a local shell call. The bridge correctly refuses to submit State evidence when the independent verification command fails and marks the run for reconciliation. Do not claim a passing PRD -> Codex edit -> test -> evidence run until a local model/template emits executable function calls throughout that loop.

## Deliberately not exercised

- No GitHub commit, push, PR creation, merge, or State acceptance was executed. Those actions require a human approval and a non-production remote fixture.
- No tailnet identity proxy was deployed; the local stack used the explicitly development-only loopback actor override.
- Workbench push-to-talk did not connect to a live Dark Realtime endpoint in this loopback stack. Its same-origin relay, allowlist, no-raw-audio persistence, and invalid-event rejection are contract-tested, but microphone/STT/TTS quality needs a tailnet qualification.
- Neo4j's live projection/search was not certified; graph write-denial and redaction boundaries remain contract-tested.

## Requalification recipe

1. Start the optional Workbench hub only on the tailnet or loopback development bind.
2. Verify the target model with the full Responses tool/result continuation and a small Codex `exec` tool loop before using it as the Workbench harness route.
3. Use a disposable Git repository and State sample task; require a changed packet-declared file and a zero-exit packet verification command.
4. Confirm the run becomes `evidenced`, State shows `needs_review` with complete evidence, and Serving `/v1/decisions` retains `workbench_run_id`, task ID, and request ID.
5. Separately approve and run only the PR/merge test fixture; confirm a post-merge State failure is represented as `reconciliation`, never completion.
