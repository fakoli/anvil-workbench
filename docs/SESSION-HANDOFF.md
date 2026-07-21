# Session handoff

Use this file to resume work in a new Anvil Workbench coding session.

## Where to start

- Repository: `C:\Users\sdoum\ai-code\anvil-workbench`
- Coding-session guide: [../CLAUDE.md](../CLAUDE.md)
- Architecture resources: [architecture/README.md](architecture/README.md)
- Contract resources: [contracts/README.md](contracts/README.md)
- Product overview: [PROJECT.md](PROJECT.md)
- System contracts: [CONTRACTS.md](CONTRACTS.md)
- Workflow operation-layer proposal: [WORKFLOW-OPERATION-LAYER.md](WORKFLOW-OPERATION-LAYER.md)
- Immediate roadmap: [ROADMAP.md](ROADMAP.md)
- Agent rules: [../AGENTS.md](../AGENTS.md)

## 2026-07-20 autonomous PR-delivery run summary (actor `claude`)

This run drove backend tasks all the way to **merged GitHub pull requests** on
`fakoli/anvil-workbench`, each gated by an independent 3-lens adversarial review
before push. Merged this run:

| PR | Task(s) | What landed |
| --- | --- | --- |
| [#4](https://github.com/fakoli/anvil-workbench/pull/4) | 20 tasks | M1/M2/M4/M6 State-managed foundations (contracts, conversation/turn store, retention, response lifecycle, advanced-branch + settings resources) |
| [#5](https://github.com/fakoli/anvil-workbench/pull/5) | chat-first-voice:T007 | Idempotency keys on side-effecting chat APIs |
| [#6](https://github.com/fakoli/anvil-workbench/pull/6) | chat-first-voice:T008 | Gap-detectable sequence + `state_version` stream metadata |
| [#7](https://github.com/fakoli/anvil-workbench/pull/7) | chat-first-voice:T009 | Off-read-path retention enforcement + one-action ephemeral chat |
| [#8](https://github.com/fakoli/anvil-workbench/pull/8) | chat-first-voice:T011 | Conversation pinning, tags, folders, filterable search |

**24 tasks are `done` in State and on `origin/main`.** The suite is 452 tests
green (verified deterministic ×3). Every merge followed the same pipeline:
worktree branch → adversarial gate PASS → State submit/apply (signed proof) →
`git merge main` + full suite → push → `gh pr create` → `gh pr merge --merge` →
`git merge --ff-only origin/main` → worktree/branch cleanup. **Every gate that
ran found at least one real defect a green suite had missed; zero upheld false
positives across the run** (scoreboard in the post-session findings).

The hard ceiling is unchanged and honest: all merged work is **backend/contract
persistence slices**. None is wired into `create_app`, a browser endpoint, the
relay turn-append path, or the production Postgres backend, and the agentic
delivery loop remains unqualified (see below). No `integrate-and-qualify`,
browser-render, or voice task is completable without live infra — tracked at
[fakoli/anvil#178](https://github.com/fakoli/anvil/issues/178).

## Current product state

The repository contains the v1 hub, bridge, frontend shell, Compose stack, and contract tests. Workbench is explicitly a private, tailnet-first agent harness whose web UI is the primary operator entry point. The Workbench code is intentionally separate from Anvil Serving and Anvil State. The separate hub is designed to be started by Anvil Serving as an optional product stack, but Workbench business logic must not move into the Serving router.

The current candidate has no seeded delivery mock. `Runs`, `Routes`, `Approvals`, `Evidence`, `Skills`, and `Sandbox` each call a concrete hub capability or state their configuration boundary. Approval review requires selecting one exact approval ID and renders its safe payload and run/worktree binding before authorization. Delivery directions are durable session events for the next work packet; selected skills are explicit bridge-local `SKILL.md` roots whose ids and digests are forwarded into that packet, then checked again locally by a non-mutating probe. Workflow start, fan-out cursor advancement, and run-command finalization use durable atomic store paths. Git evidence and approval hashes include untracked files through one isolated-index tree snapshot. Managed Codex receives only an allowlisted non-credential process environment; command-backed authentication retrieves the run-scoped router token from an ephemeral bridge-owned loopback broker that network-disabled tool subprocesses cannot reach. The optional sandbox is a bounded Serving-only Responses request; Compose passes its explicit model allowlist through to the hub. The setup guide derives completion only from live hub records.

The local qualification record is [QUALIFICATION.md](QUALIFICATION.md). On 2026-07-19 the pinned Heavy and Fast were independently preflighted; Dark STT/TTS completed a synthetic-silence pipeline; State claim/packet/evidence/replay passed through the CLI only; and the Workbench browser shell was exercised at `http://127.0.0.1:8090`. Its bounded `chat-fast` Sandbox request completed live through Serving and the Routes view reads Serving's safe `records` summary. The remaining live harness blocker is explicit: the pinned GPT-OSS Puzzle Heavy routes Codex Responses traffic and preserves correlation, but its full Codex loop returns unsupported `shell_command<|channel|>commentary` calls. Do not mark the agentic delivery flow qualified until a local model/template passes a real edit/test/evidence submission.

The browser shell is intentionally usable in two modes:

- **Production:** a tailnet identity proxy injects an authenticated identity header. No insecure fallback is enabled.
- **Loopback development:** an untracked `.env` sets `WORKBENCH_ALLOW_INSECURE_DEV_ACTOR=true`, allowing the configured owner to exercise the API without a proxy. This is only for local validation.

## Observed Serving dependency state

The 2026-07-19 browser sandbox qualification used the existing Dark router at
`http://100.87.34.66:8000/v1` with its server-held router token and an explicit
`WORKBENCH_SANDBOX_MODELS=chat-fast` allowlist. Do not commit that token or copy
it into browser code. Provision both values from the deployment secret store
when restarting the hub; a stale token or a `host.docker.internal` alias cannot
reach this router's tailnet-only bind.

At that observation point, the router container and Fast
`gemma4-e4b-it` serve were healthy. The configured `heavy` serve was stopped
because its ThinkingCap Qwen3.6 FP8 MTP start passed an incompatible
`compressed-tensors` quantization setting; fix and independently preflight the
serve recipe before attempting a heavy restart. The `anvil-serving` source
checkout was also dirty and behind `origin/main`, so do not pull, switch, or
rewrite it as part of Workbench work without a separate review of those changes.

## Resume checklist

1. Confirm the current branch and worktree are clean with `git status --short --branch`.
2. Read the README, project brief, and contracts before making a design change.
3. Run Python tests, frontend build, and Compose configuration validation.
4. For a UI change, start the local stack, use the browser validation loop, and save screenshots outside the repository.
5. For a State or Serving change, treat the contract document as a hard boundary and update it in the same PR when the interface changes.
6. For a delivery-flow change, add or extend a test that proves rejection of stale/replayed approvals and no direct State database access.

## Do not regress

- No raw provider path for Workbench-managed model traffic.
- No project worktree or GitHub token in the hub.
- No raw transcript in Neo4j.
- No direct State database use.
- No auto-PR, auto-merge, State apply, deploy, or model-policy update without a consumed approval.
- No “completed” label when merge and State acceptance disagree.
- No bridge-inherited Codex plugins, apps, MCP servers, browser tools, hosted web search, or user/project rule files.
- No browser-supplied worktree paths, model/tool controls on the voice relay, raw audio storage, or transcript retention without the explicit environment switch.

## 2026-07-20 chat-first-voice T011 — conversation pinning, tags, folders, filterable search

Conversation-level ORGANIZATION metadata (a `pinned` flag, a bounded set of
safe `tags`, one optional `folder` label) plus actor-scoped filters on
list/search. All backend + API; the rail UI that renders/edits these is T004.2
and is NOT in this task, so no `web/src` change was made.

- **`workbench/conversation_models.py` (+~55):** `Conversation` gains
  `pinned: bool`, `tags: tuple[str, ...]`, `folder: str | None`. Tags/folder are
  safe `^[a-z0-9][a-z0-9._-]{0,63}$` tokens (`_ORG_TOKEN`) — structurally no
  prose/path/URL; `tags` normalized to a sorted, unique tuple via `normalize_tags`
  and bounded to `MAX_TAGS = 32`. A `deleted` tombstone must retain NO
  organization metadata (mirrors the existing title-drop rule). Deliberately did
  NOT extend `ConversationAudit`: its scalar-only field set is locked by
  `test_audit_models_structurally_carry_no_message_content_field`, and a `tags`
  tuple would breach that safety guard — so organization changes are audited
  through the store event `kind` over the unchanged content-free shape instead
  (consistent with how a title never appears in audit either).
- **`workbench/conversation_store.py` (+~70):** six actor-scoped mutations —
  `pin`/`unpin`, `add`/`remove_conversation_tag`, `set`/`clear_conversation_folder`
  — each routed through `_owned`/`_mutate_conversation` (unsafe label → fail
  closed; non-listable status → refused) and `_store_conversation` under a
  distinct `kind` (`conversation.pinned|unpinned|tagged|untagged|foldered|unfoldered`).
  `list_conversations`/`search_conversations` gain `pinned`/`tag`/`folder`
  keyword filters applied AFTER actor scoping (a cross-actor filter matches
  nothing; no existence oracle); pinned rows sort ahead of the rest. `_complete_deletion`
  clears org metadata into the tombstone. All six added to the `ConversationStore`
  Protocol and the `_synchronize` reentrant-lock list.
- **`workbench/conversation_api.py` (+~90):** `conversation_json` now emits
  `pinned`/`tags`/`folder`. New thin endpoints `POST /{id}/pin`, `/unpin`,
  `/tags`, `/tags/remove`, `/folder`, `/folder/clear` (each idempotency-aware
  like its siblings); `GET ""` and `GET /search` gain `pinned`/`tag`/`folder`
  query filters. `TagInput`/`FolderInput` re-pin the safe token at the edge (422
  on an unsafe label).
- **Criteria → proving tests (`tests/test_conversation_organization.py`, +11):**
  (1) org metadata absent from the assembled Serving request/turn content →
  `test_organization_metadata_is_absent_from_the_assembled_serving_request`
  (marker scan of `build_bounded_request`, which takes only route selection +
  prompt — no conversation), `test_organization_metadata_is_absent_from_persisted_turn_content`;
  (2) actor-scoped filters, cross-actor probe leaks nothing →
  `test_filters_return_only_the_owning_actors_matching_conversations`,
  `test_cross_actor_filter_probe_leaks_nothing_and_is_no_oracle`,
  `test_api_filtered_list_is_actor_scoped_and_cross_actor_probes_return_nothing`;
  (3) content-free lifecycle audit →
  `test_organization_mutations_are_audited_content_free_with_lifecycle_kind`.
  Plus bounding/idempotence/lock: unsafe+over-count fail closed, dedup/idempotent
  remove, a `sys.setswitchinterval` concurrency test proving the reentrant lock
  serializes concurrent tag adds with no lost update (set/restore in try/finally),
  tombstone drop, and API mutation endpoints.
- **Rail UI (pending T004.2):** the backend + API shape the rail needs
  (`conversation.pinned|tags|folder`, filter query params, mutation endpoints)
  is delivered here; rendering/editing the pin/tag/folder rail is T004.2 and not
  merged. No `web/src` touched.
- **Verification:** FULL Python suite green at 452 (441 baseline + 11 new), run
  3x for the concurrency test — stable. No `web/src` touched, so `npm test` was
  not required.

## 2026-07-20 chat-first-voice T009 — off-read-path retention enforcement, content-free preview, one-action ephemeral chat

Retention expiry is batched-only (never triggered by a read), a content-free
preview lets an operator review the pass's scope, and a one-action ephemeral
affordance creates a `metadata_only` conversation whose badge is the true
durable policy. All backend + hermetic tests; no `web/src` change.

- **Read-path expiry needed NO fix.** Audited `get_conversation`,
  `list_conversations`, `search_conversations`, `get_conversation_with_turns`:
  none expire a still-live conversation past `delete_after`. `_reconciled`
  only completes a crashed `deletion_pending` (an already-REQUESTED deletion),
  which is correct and preserved. Only `enforce_retention` initiates
  retention-expiry deletion. Added regression tests locking this in.
- **`workbench/conversation_models.py` (+~70):** new `RetentionPreview` frozen
  dataclass (conversation_id, reason, turn/committed/interrupted counts,
  created/updated/`delete_after` — deliberately NO title/content field) plus
  `retention_preview_of(...)` projector; `is_metadata_only(retention)` (true
  only when BOTH content kinds are `metadata_only`) and
  `ephemeral_retention_policy()`; `RETENTION_PREVIEW_REASONS`,
  `EPHEMERAL_RETENTION_POLICY_ID`.
- **`workbench/conversation_store.py` (+~35):** `retention_preview(now)` — pure,
  side-effect-free preview of exactly what `enforce_retention(now)` would act on
  (expired-by-ceiling live rows + crashed `deletion_pending`), fails closed on a
  naive `now`, mutates nothing. `create_ephemeral_conversation(actor)` — one call
  → `metadata_only` conversation (reuses `create_conversation`; the append gate
  then forbids any transcript block, so the badge cannot lie). Both added to the
  Protocol and the `_synchronize` reentrant-lock list.
- **`workbench/conversation_api.py` (+~70):** truthful `ephemeral` badge added to
  `conversation_json` (computed from the durable policy). New actor endpoint
  `POST /api/conversations/ephemeral` (one action, no body). New OPERATOR-ONLY
  `build_hub_retention_router` mounted at `/api/hub/retention` — `GET /preview`
  (content-free) and `POST /enforce` (the explicit batched pass). These are the
  store's HUB-INTERNAL ops, so they are wired behind the hub `owner` dependency
  and kept OFF `/api/conversations` (whose T002.4 guard test forbids any
  retention/enforce/audit path and which must never let one actor delete across
  actors). 503 when chat persistence is unconfigured. Wired in
  `workbench/api.py`.
- **Criteria → proving tests (`tests/test_retention_enforcement.py`, +11):**
  (1) content-free preview →
  `test_retention_preview_is_content_free_ids_counts_and_timestamps_only`,
  `test_api_hub_preview_is_operator_only_and_content_free` (forbidden-marker
  scan, mirrors T002.3);
  (2) reads never expire, only the batched pass does →
  `test_reads_do_not_expire_a_conversation_past_its_delete_after_ceiling`,
  `test_api_read_does_not_expire_only_the_enforce_endpoint_does`, plus
  `test_crashed_pending_deletion_still_reconciles_on_read_unlike_expiry`
  (proves the distinct correct behavior is preserved);
  (3) one-action ephemeral + true badge →
  `test_create_ephemeral_conversation_is_one_action_metadata_only_with_true_badge`,
  `test_api_ephemeral_endpoint_creates_metadata_only_in_one_action_with_true_badge`.
- **Badge rail (pending T004.2):** the backend + API/policy shape the badge reads
  (`conversation.ephemeral: bool`, truthful from policy) is delivered here. The
  browser rail that RENDERS the badge is T004.2 and is not merged; no `web/src`
  change was made in this task. When T004.2 lands, its badge should read the
  `ephemeral` field rather than re-deriving policy.
- **Verification:** FULL Python suite green at 441 (430 baseline + 11 new). No
  `web/src` touched, so `npm test` was not required.

## 2026-07-20 chat-first-voice T008 — gap-detectable sequence + state-version stream metadata

Added strictly-monotonic per-conversation sequence numbers and a state-version
to the streaming/lifecycle metadata so a reconnecting client can detect a
dropped frame and refresh to last-committed state without duplicating the
response. All backend, all hermetic; the client half is a pure helper.

- **`workbench/response_lifecycle_store.py` (+~80):** `ResponseLifecycle` gained
  two bounded non-negative int fields — `state_version` (bumps on each committed
  *state change*; begun `in_progress` = 1, `in_progress -> terminal` = 2) and
  `last_committed_seq` (the highest per-conversation seq committed for the
  response). `ResponseLifecycleRows` gained `conversation_seq: dict[str,int]`,
  the per-conversation high-water mark that lives in the shared row container so
  it survives a restart. New `next_seq(actor, request_id)` allocates the next
  strictly-monotonic per-conversation seq (ownership-scoped, so it is not a
  cross-actor oracle; bounded by `MAX_SEQUENCE`). New `snapshot(...)` returns a
  frozen `ResponseSnapshot` (state + state_version + last_committed_seq +
  is_terminal) as a pure read — no mutation, no frame replay. `advance(...)`
  gained an optional keyword `seq`: a committed seq must STRICTLY advance
  `last_committed_seq`, so a stale frame (seq ≤ last committed) is refused before
  any state change, and terminal-immutability still refuses ANY advance from a
  terminal — a stale-sequence frame can never regress a terminal.
- **`workbench/stream_sequence.py` (new, +~110):** pure gap-detection contract —
  `detect_gap(last_seq, frame_seq)` (a skip = a drop), `is_stale_frame(...)`,
  `needs_snapshot_refresh(...)` — plus `sequence_events(events, allocate)` which
  stamps each relay frame with the next `seq`. `workbench/chat_stream.py`
  `RelayEvent` gained an optional bounded `seq` field (default None).
- **Criteria → proving tests (`tests/test_stream_sequence.py`, +15):**
  (1) monotonic-per-conversation + survives-reconnect →
  `test_seq_continues_strictly_above_last_committed_after_reconnect`,
  `test_seq_is_monotonic_across_two_requests_in_one_conversation`;
  (2) dropped-frame detectable client-side + snapshot returns last-committed
  state with no duplication →
  `test_dropped_frame_is_detectable_and_snapshot_returns_last_committed`;
  (3) stale-seq cannot regress a terminal →
  `test_stale_sequence_frame_cannot_regress_a_terminal`,
  `test_racing_seq_commits_keep_a_stable_terminal` (tight `sys.setswitchinterval`
  restored in `finally`).
- **Client helper:** the frontend toolchain WORKS in this environment (`npm ci`
  clean, vitest green). `web/src/chat-api.js` (new) mirrors the gap-detection
  contract (`detectGap`/`isStaleFrame`/`needsSnapshotRefresh`) plus a pure
  `reduceStreamState`/`applySnapshot` reducer; `web/src/chat-api.test.js` (new,
  +5) proves a stale frame is ignored (no duplication) and a gap resyncs to the
  snapshot. No new HTTP endpoint was added — `snapshot`/`next_seq` are the store
  contract; wiring them onto `/api/conversations` (and a `GET .../snapshot`
  route) is the next thin slice.
- **No JSON schema change:** the response lifecycle store (T003.3) has no schema
  mirror, so `state_version`/`last_committed_seq` stay internal store metadata,
  consistent with the existing `state`/`usage` fields. `chat-turn.v1` unchanged.
- **Verification:** FULL Python suite green at 429 (414 baseline + 15 new); web
  vitest green at 13 (8 baseline + 5 new); all doc JSON parses.

## 2026-07-20 chat-first-voice T007 — idempotency keys on side-effecting chat APIs

Delivered actor-scoped idempotency keys for the side-effecting chat endpoints.
New `workbench/idempotency_store.py` adds a hermetic row-backed
`MemoryIdempotencyStore` in the `MemoryResponseLifecycleStore`/`MemoryStore`
idiom (frozen `IdempotencyRecord`, restart-simulating rows, a reentrant lock
wrapping the one public `run` method). It reuses the delivery-path discipline,
not a parallel one: dedup identity is the triple `(actor_id, operation, key)`
and the stored result is bound to the canonical `workbench.store.payload_hash`
(sorted-key SHA-256) of the material request, compared in constant time with
`secrets.compare_digest` exactly like approval consume. `run` executes the
caller's operation INSIDE the lock, so two concurrent same-key requests resolve
to exactly one record (verified: with the lock disabled the race double-executes
14/20 trials; with it, always one). A failed execution stores nothing and stays
retriable. The result is deep-copied in and out so a persisted record cannot be
mutated through a returned reference.

Wiring is thin: every side-effecting `/api/conversations` POST (create, rename,
archive, unarchive, delete, append_turn, retry, branch, advance_turn_status)
now honours an OPTIONAL `Idempotency-Key` header. Without a key the endpoint
executes normally (no dedup) — so the 399-test baseline is unchanged; with a
key it deduplicates per `(actor, operation, key)`, replays the identical stored
response on a same-payload retry, and refuses a same-key/different-payload reuse
with a 409 `IdempotencyConflictError` (a `StoreError`, mapped to 409 by the
existing app-level handler). Cross-actor reuse lands in a disjoint scope and
executes fresh — one actor can never read or replay another's result. The store
is threaded through `create_app`/`build_conversation_router` (injectable for
tests; a fresh in-memory store when not injected).

Files: `workbench/idempotency_store.py` (+215), `workbench/conversation_api.py`
(+~90 net over the 9 endpoints + helper), `workbench/api.py` (+4),
`tests/test_chat_idempotency.py` (+~300, 14 tests). Verification: the three
named suites (62), the new suite (14), and the FULL suite all green at 413
(399 baseline + 14). Implemented as a hermetic hub slice; the durable Postgres
projection (a unique constraint on the triple + a row lock) lands with the
production API store, and the keys are not yet surfaced in the browser client.

## 2026-07-20 preferences-configuration T001 — settings descriptor contract

Delivered the first `preferences-configuration` (milestone-4, F001) task: a
versioned, digest-bearing settings-descriptor contract resource.

- **Implemented (as a proposed contract resource, not a live endpoint):**
  `docs/contracts/schemas/settings-descriptor.v1.schema.json` (draft 2020-12,
  `additionalProperties:false` throughout) and
  `docs/contracts/examples/settings-descriptor.v1.json` (16 descriptors across
  personal/project/deployment/policy). `workbench/contracts.py` gained the
  `settings-descriptor` digest kind (prefix + canonical normalization: omit
  `catalog_digest`, sort `settings` by `id`, preserve `scope_precedence`
  order), a `settings_descriptor_contract_validator()` with a closed-root/
  closed-descriptor guard and a `_reset_...cache` hook (mirroring the
  profile/workflow siblings), the `validate_settings_descriptor()` fail-closed
  semantic validator, and the `settings_actor_view()` actor/project projection.
- **Criteria proof:** each descriptor owns exactly one `scope` and
  `scope_precedence` is a total order (`policy>deployment>project>personal`),
  with a `policy_ceiling` forced to outrank its setting so a personal value can
  never exceed a policy/retention/route bound (criterion 1). Secret and
  path-like descriptors are authority-owned, carry no serializable default, and
  are dropped by `settings_actor_view()` — proven by a forbidden-marker scan and
  a defence-in-depth rogue-secret test (criterion 2). Route/worktree/workflow/
  skill/plugin/capability defaults are `id_ref`/`digest_ref` pattern-validated
  references, and a free-text capability default is refused (criterion 3).
- **Evidence:** `python -m pytest -q tests/test_contract_resources.py
  tests/test_security_contract.py tests/test_settings_descriptor.py` = 44
  passed; full suite `python -m pytest -q` = 332 passed (318 baseline + 14 new
  in `tests/test_settings_descriptor.py`). Docs updated: contracts README table,
  DIGESTING.md row, and a "Settings and preferences descriptor (proposed)"
  section in CONTRACTS.md.
- **Remaining gate / next step:** the catalog is proposed only. T002.x must
  wire it into a durable actor/project preference store, an effective-value
  resolver, and the preference/export APIs (the actor view is the only
  serialization those APIs may emit).

## 2026-07-20 autonomous anvil-driven run (actor `claude`) — run 3

Twenty State-managed tasks are now merged through the gate-reviewed lifecycle;
the full suite is at 399. All six PRDs carry merged work: milestone-1
(state context, provider catalogs, capability profiles, workflow snapshots),
milestone-2 (chat contracts, conversation store, retention, APIs, route
discovery, streaming relay, reconnect-safe lifecycle), milestone-4
(settings-descriptor contract), milestone-6 (advanced-mode contracts);
milestones 3 and 5 are approved and fully planned. Every implemented slice is
hermetic and deliberately not wired into the live loop pending its integration
task; live qualification stays gated on fakoli/anvil#178. Research-derived
backlog (16 items across all PRDs) is planned; upstream anvil frictions filed
as fakoli/anvil#180. Run reports live in
post-session-findings/2026-07-20-autonomous-prd-run/ (gitignored).

## 2026-07-20 autonomous anvil-driven run (actor `claude`) — continued

Run #2 (same day): three more tasks delivered through the identical
gate-reviewed lifecycle — `chat-first-voice` T002.2 (actor-scoped
conversation store) and T002.3 (retention/deletion/irreversibility + the
R008 keyed-HMAC content fingerprint), and `state-context-operations` T004.1
(reviewed provider catalog registry with a shared catalog-contract
validator and $ref fail-closed checks). Ten tasks total are merged; the
full suite is at 213. A research pass (OpenClaw, Open WebUI) produced 16
adversarially-filtered backlog items inserted across all six PRDs
(project now 108 tasks / 98 ready); reports live in
post-session-findings/2026-07-20-autonomous-prd-run/ (gitignored).
Upstream anvil frictions are filed as fakoli/anvil#180.

## 2026-07-20 autonomous anvil-driven run (actor `claude`)

Seven State-managed tasks were delivered through the full anvil lifecycle
(claim -> implement -> adversarial multi-agent review gate -> fix rounds ->
independent re-verification -> evidence submit -> apply -> merge), each merge
with the full suite green (61 -> 154 tests): `state-context-operations`
T001/T002.1/T002.2/T002.3/T002.4 (feature F002, the State project-context
projection, is complete but deliberately not wired into the live bridge loop
pending fakoli/anvil#178) and `chat-first-voice` T001/T002.1 (contract
schemas + conversation/turn domain models). All six PRDs are now approved;
`plan-task-delivery` and `reviewed-tools-plugins` were adversarially
reviewed, revised (r2), and approved with their open questions resolved into
Decisions. Signed proof records live in the anvil workspace `proofs/` dir.

## Best next engineering tasks

1. Resolve the Codex/local-model tool-call compatibility blocker recorded in [QUALIFICATION.md](QUALIFICATION.md); preserve the current Heavy as a qualified general Responses route but do not claim Codex harness parity.
2. Add a local identity-proxy test fixture that proves browser header spoofing is rejected.
3. Add a Postgres/Neo4j Compose integration test that runs in CI or a dedicated release job.
4. Merge and observe the hub-image publication workflow, then set the first GHCR package to public once. It builds `deploy/Dockerfile.hub`, publishes `latest`, `main`, and immutable SHA tags with an attestation, and supplies the image consumed by Anvil Serving's optional `workbench up` lifecycle command. Do not put registry credentials in `workbench.env`.
5. Qualify a live Dark voice endpoint and two bridge-configured worktrees. The implementation and hermetic contracts exist; neither is a substitute for live hardware/State qualification.
6. Implement the provider-owned workflow operation catalog proposed in [WORKFLOW-OPERATION-LAYER.md](WORKFLOW-OPERATION-LAYER.md) before adding more bespoke browser-to-bridge command paths.
7. Start that implementation from the versioned resources in [contracts/README.md](contracts/README.md): catalog/profile discovery, `operation` workflow steps, run-context snapshot, typed receipts, and bridge preflight. Do not turn the design into a generic tool runner. The State-side discovery half is implemented: `workbench/state_manifest.py` runs the bridge-configured `--state-describe-command`, fail-closed validates the advertised `anvil-operation-catalog/v1` catalog (digests, `read` effect class, contract major, draft 2020-12 object schemas), and pins the immutable `state.project.snapshot` / `state.prd.read_content` descriptor set that downstream adapters must take by constructor (`tests/test_state_manifest_discovery.py` is hermetic). The snapshot-adapter half now exists too: `workbench/state_snapshot_adapter.py` takes that pinned set by constructor, runs the bridge-configured snapshot command against only the pinned `state.project.snapshot` descriptor, and fail-closed validates the `workbench-state-snapshot/v1` payload (closed-object contract schema and prose bounds so no full PRD Markdown, digest recompute, owning-PRD references, scoped task identity, source provenance) into an immutable `PublishableSnapshot` keyed by `snapshot_digest` (`tests/test_state_snapshot_adapter.py` is hermetic). The PRD-content adapter half now exists too: `workbench/prd_content_adapter.py` takes the same pinned set by constructor, validates the scoped `prd_id` request against the pinned `state.prd.read_content` input schema before any CLI call, and fail-closed validates the `workbench-prd-content/v1` payload (closed-object contract schema, requested-PRD equality, optional expected-revision freshness, UTF-8 encoding, digest recompute, the 64 KiB byte bound, truncation coherence) into an immutable `PublishablePrdContent` keyed by `content_digest` (`tests/test_prd_content_adapter.py` is hermetic; the catalog example's `state.prd.read_content` output schema was corrected to nest `truncated` under `content` per the prd-content contract, with digests recomputed). None of these halves is wired into the live bridge poll loop; live qualification stays gated on the upstream State CLI actually advertising that catalog from `anvil describe` (fakoli/anvil#178); today's envelope only reports CLI/MCP surface names. The State-context read feature (F002) is now complete on the Workbench side: State-internals isolation is proven by a repository-scan contract test (`tests/test_security_contract.py::test_no_workbench_source_opens_copies_mounts_or_mutates_state_storage` — no hub, bridge, or browser source may reference `state.db`, its journal/WAL/shm siblings, a `.anvil` state workspace path, or any SQLite driver; the only allowlisted form is a documentation string stating the prohibition), and the three-adapter contract (discovery pins, snapshot adapter, bounded PRD-content adapter, shared injectable-runner/UTF-8 transport rule, not-wired-live gate) is documented as the "State read-adapter contract" section of [CONTRACTS.md](CONTRACTS.md). The only remaining F002 work is live wiring and qualification once fakoli/anvil#178 lands. The multi-provider catalog registry now exists too: `workbench/provider_catalogs.py` loads reviewed catalogs for the configured provider set (anvil-state via the describe command, others via operator-declared `--provider-catalog` local JSON files; http/mcp transports are declared but fail closed as not implemented), fail-closed validates identity/versions/effects/schemas/digests against the shared operation-catalog contract, and publishes only safe frozen metadata (`tests/test_provider_catalogs.py` is hermetic) — implemented, not wired into the live bridge loop. The profile-validation half now exists too: `workbench/capability_profiles.py` fail-closed pins a reviewed project capability profile (digest recompute, closed contract schema, exact per-operation resolution against the registry's discovered catalog set, operator-configured model-profile/skill-digest/approval-action allowlists taken as explicit parameters, duplicate/conflict refusal) into a frozen `PinnedCapabilityProfile` (`tests/test_capability_profiles.py` is hermetic) — implemented, not wired into workflow queueing; profile v1 deliberately excludes plugin descriptors (bridge-disabled, schema-refused) and per-route digests (Serving-owned). The snapshot half now exists too: `workbench/workflow_snapshot.py` compiles a workflow plus the pinned profile and discovered catalogs into one frozen, source-attributed, self-digested `WorkflowSnapshot` (every selected operation/skill/route/approval-action/limit pinned at its exact digest, immune to later catalog or profile refreshes) and `preflight_snapshot` fail-closed refuses any missing or changed pin with stable typed `SnapshotDrift` records before an effect (`tests/test_workflow_snapshot.py` is hermetic) — implemented, not wired into live workflow queueing. The derived project-context display projection now exists too: `workbench/project_context.py` turns a validated `PublishableSnapshot` into a frozen, explicitly non-canonical `ProjectContextProjection` of readable PRD/feature/task display summaries keyed by `(project_id, source_kind, scoped_id, source_digest)` — each carrying source revision, the source snapshot digest, project ownership, and a `canonical: false`/`non_canonical: true` marker in its serialization — with a fixed-field closure and a round-tripping `as_dict()`/`from_dict()` that structurally cannot carry a State path, credential, or executable provider payload (`tests/test_project_context.py` is hermetic); implemented as a display read-model, not wired into the browser projection or live poll loop. The project-scoped persistence half now exists too (T003.2): `workbench/project_context_store.py` is a hermetic row-backed `MemoryProjectContextStore` (in the `MemoryConversationStore`/`MemoryStore` idiom — frozen values, restart-simulating rows handed to a fresh instance, a reentrant lock wrapping every public method) that publishes and fetches projections per project. `publish` is idempotent by `(project_id, source_digest)` (an identical digest returns the already-stored record and creates no duplicate; a mismatched-content digest collision fails closed); a strictly newer `ProjectContextProjection.source_revision` (a new derived property: the max summary revision) supersedes only the acting project's latest display projection while every prior projection stays addressable by its own digest, so historical source attribution is never rewritten; an equal revision under a new digest also supersedes (e.g. a task-status flip that bumps no PRD revision), while only a strictly-lower revision under a new digest fails closed (`StaleProjectionError`) rather than clobbering the latest; and a cross-project publish/read/overwrite is refused with the same indistinct `UnknownProjectionError` a genuinely missing record raises (mirroring the conversation store's cross-owner probe), so one project can neither learn of nor touch another's projection (`tests/test_project_context_store.py` is hermetic, 11 tests). Implemented as a display persistence slice, not wired into the browser projection or live poll loop. The integrate-and-qualify halves are now done too (T002/T003/T003.3/T003.4): `tests/test_state_context_integration.py` wires discovery -> snapshot adapter -> PRD-content adapter through one pinned descriptor set with source/runtime probes that prove the pipeline opens no subprocess and creates no `state.db`; `workbench/api.py` exposes a read-only, project-scoped browser surface (`build_project_context_router`: `GET /api/projects/{project_id}/context` latest + `/{source_digest}` by digest) authenticated by the trusted `actor` dependency, rendering only the non-canonical projection, returning the indistinct 404 for a foreign-or-missing record (no existence oracle), and failing closed with 503 when no projection store is configured (it stays `None` unless injected, preserving the not-wired-live gate); `tests/test_project_context_adversarial.py` and `tests/test_project_context_projection.py` qualify idempotency, owning-project-only supersession, cross-project publish/read/overwrite fail-closure, and response safety end to end; the response-safety scan lives in `tests/test_security_contract.py`. The criterion-1 proof for T003.4 (idempotency, forged-digest collision, owning-project-only supersession, and the lock-serialized same-digest concurrency race) is `tests/test_project_context_adversarial.py`; T003.4's declared per-task verification command does not name that module, but it is exercised by the full `python -m pytest -q` suite that gates the bundle. F002's Workbench side is now complete through the browser read API; only live wiring/qualification behind fakoli/anvil#178 remains.
8. The chat-first-voice foundation is implemented through the hub API slice,
   with durable production persistence still pending: `chat-conversation.v1`
   and `chat-turn.v1` under `docs/contracts/` define one
   mode-agnostic conversation identity, append-only `(parent_turn_id,
   sibling_index)` turn lineage, Serving-ID/digest-only route references,
   display-only project/PRD-revision/task context, retention/deletion
   semantics, typed voice events, and hard prohibitions on persisting raw
   audio or hidden reasoning (tests: `tests/test_contract_resources.py`,
   `tests/test_security_contract.py`). The schema features the domain slices
   below do not model (route/usage/advanced-controls/context blocks) remain
   proposed, not implemented.
   The first implementation slice exists: `workbench/conversation_models.py`
   defines frozen conversation/turn/retention/deletion domain values
   mirroring those schemas, a deterministic domain-separated `sha256:`
   content hash, a fail-closed `validate_turn_append` gate enforcing the
   four append-time lineage invariants plus the conversation-ownership
   boundary and the retention-to-content-kind mapping, and content-free
   `TurnAudit`/`ConversationAudit` shapes (`tests/test_conversation_models.py`
   is hermetic). The store slice now exists too:
   `workbench/conversation_store.py` is the actor-scoped hub persistence layer
   (create/list/search/rename/archive, appends and branch/retry routed through
   `validate_turn_append`, cross-actor probes indistinguishable from a missing
   conversation, streaming turns recovered as `interrupted` after reload,
   content-free audit; `tests/test_conversation_store.py` is hermetic). The
   retention/deletion slice (T002.3) now exists too: the turn content hash
   was converted from an unkeyed domain-separated SHA-256 to a server-keyed
   HMAC-SHA256 fingerprint (`hmac-sha256:<hex>`, PRD R008 — the hub key is
   constructor-injected into `MemoryConversationStore`, held on the instance
   only, never persisted next to the hashes, and reads re-verify live turns'
   fingerprints fail-closed; identical content under different keys yields
   different values, closing the audit content-equality/dictionary oracle for
   parties without the key), `delete_conversation` implements the contract's
   two deletion modes (`purge_content_keep_tombstone` leaves the identity row
   plus content-purged tombstone turns keeping only lifecycle, lineage, voice
   events, and the keyed fingerprint; `purge_all_records` removes the
   conversation and turns entirely) through a persisted, audited
   `deletion_pending` -> `deleted` lifecycle, and `enforce_retention(now)`
   applies exactly the ceilings `chat-conversation.v1` declares — the
   per-conversation `retention.delete_after` instant plus reconciliation of a
   crashed pending deletion; no invented policy fields. Purges remove content
   blocks and titles from the rows themselves (a purged record refuses
   content at construction, so the purge is one-way), and
   `tests/test_conversation_retention.py` proves each criterion hermetically,
   including that a fresh store instance over the same rows recovers nothing.
   The actor-scoped API slice (T002.4) now exists too:
   `workbench/conversation_api.py` mounts `/api/conversations` in
   `create_app` — create, list (archived filter), search, rename,
   archive/unarchive, delete (both contract modes in the request body),
   get-with-turns, turn append, retry, branch, and streaming-to-terminal
   status advance. Every endpoint derives the actor from the hub's trusted
   identity dependency (the tailnet header + approver allowlist); the input
   models forbid unknown fields so a smuggled body `actor` is a 422, and a
   cross-actor probe renders the store's `UnknownConversationError` as a
   fixed 404 body byte-identical to a truly missing id. Responses carry the
   owner's own content plus truthful `committed`/`interrupted` state and
   `(parent_turn_id, sibling_index, kind)` lineage pointers, and never
   serialize the keyed content fingerprint, the key, another actor's
   records, or the store's HUB-INTERNAL operations
   (`list_audit`/`recover_streaming_turns`/`enforce_retention` stay
   unwired). The content-hash key is hub configuration only
   (`WORKBENCH_CHAT_HASH_KEY` -> `Settings.chat_content_hash_key`); when it
   is unset there is no conversation store and every chat endpoint refuses
   with 503 instead of serving (`tests/test_conversation_api.py` proves each
   criterion hermetically). The production Postgres conversation backend is
   still pending — `create_app` currently builds the in-memory
   `MemoryConversationStore` (with recover-on-open), so durable chat
   persistence across hub restarts does not exist yet.
   The chat-route discovery slice (T003.1) now exists too:
   `workbench/chat_routes.py` fail-closed validates the operator-reviewed
   `WORKBENCH_CHAT_ROUTES` JSON allowlist into a frozen browser-safe
   snapshot (chat-turn.v1 route identifiers/digests plus declared
   Advanced-control names only — no endpoint, URL, token, credential, or
   policy field is representable) and refuses an unknown route or
   undeclared control before any Serving request, with no raw-provider
   fallback path (`tests/test_chat_routes.py` is hermetic, including a
   workbench-wide raw-provider-host scan) — implemented, not yet wired to
   a browser endpoint or the turn-append path.
   The bounded Responses stream relay (T003.2) now exists too:
   `workbench/chat_stream.py` assembles a bounded Responses request from a
   T003.1-validated `ChatRouteSelection` (Serving `model_profile`/`route_id`
   and validated controls only, bounded prompt) and relays an injected Anvil
   Serving SSE sequence as typed relay events, settling into exactly one
   distinct `StreamOutcome` (`completed`/`cancelled`/`timed_out`/
   `serving_unavailable`) whose turn-status mapping never renders a cancelled,
   timed-out, or partial stream as `complete`; a `CancellationToken` checked
   before every upstream read terminates the injected transport and guarantees
   no later completion, and every failure settles through the Serving runtime
   (the module imports no HTTP client and embeds no URL/provider literal) —
   `tests/test_chat_stream.py` is hermetic (scripted SSE transport, no
   network), and the relay is stateless: persistence stays in the store, not
   yet wired to a browser endpoint or the turn-append path.
   The Advanced-mode contract surface (advanced-model-playground T001) now
   exists too, as **proposed** contract resources — not an implemented
   endpoint. Four versioned schemas plus examples under `docs/contracts/`
   extend (never fork) the Chat contract: `advanced-branch.v1` (a versioned
   Advanced-mode branch that references an EXISTING `conversation_id` + parent
   turn and whose `advbranch_` id cannot mint a `conv_` identity — it carries
   no turns/transcript array; route-capability descriptors declare each control
   with type/bounds/default plus the route/profile digest; only mock/read-only
   tool kinds; ephemeral vs durable retention; structured-output mode;
   effective-value provenance; budgets; a repair marker), `advanced-trace.v1`
   (a closed redaction-only request/route/tool/usage trace with no field for a
   credential, raw header, hidden reasoning, path, or unredacted payload),
   `advanced-preset.v1` (digest-bearing `preset_digest`; pins exact
   route/profile/tool digests and repairs deterministically on drift), and
   `advanced-comparison.v1` (factual metrics over 2–4 sibling turns; a ranking
   is representable only with a named declared criterion). `workbench.contracts`
   gains the reference validators `validate_advanced_branch` (declared-control /
   bounds / policy-owned enforcement — criterion 1) and
   `validate_advanced_preset` (digest recompute + deterministic drift/repair —
   criterion 3), plus the `advanced-preset` digest kind (prefix +
   DIGESTING.md row) and closed-object trust-root guards mirroring the
   settings-descriptor sibling. `tests/test_advanced_contracts.py` binds each
   acceptance criterion to a proof and the four examples are registered in
   `tests/test_contract_resources.py::SCHEMA_FOR_EXAMPLE`. Full suite is 370
   green (346 baseline + 24). These are shape-and-authority resources only:
   no router, store, or API reads or writes them yet (T002–T010 remain).
   The reconnect-safe response lifecycle store (T003.3) now exists too:
   `workbench/response_lifecycle_store.py` is a hermetic row-backed
   `MemoryResponseLifecycleStore` (in the `MemoryConversationStore`/`MemoryStore`
   idiom — frozen values, restart-simulating rows handed to a fresh instance, a
   reentrant lock wrapping every public method, typed `ResponseLifecycleError`/
   `UnknownResponseError` subclasses) that persists one actor-owned,
   conversation-scoped response-request lifecycle keyed by `(actor_id,
   request_id)` (disjoint per-actor namespaces, so a cross-actor probe can never
   become an existence oracle). The state machine is `begin` -> `in_progress`
   then `advance` to exactly one terminal (`completed`/`cancelled`/`timed_out`/
   `interrupted`), after which the record is immutable; `reconnect` returns the
   last committed in-progress or terminal state and never mutates, restarts, or
   re-streams (criteria 1-2). Lifecycle is monotonic — `in_progress -> terminal`
   is allowed once, `terminal -> anything` fails closed, and the instance lock
   makes a terminal stable under a race (criterion 3, proven by a two-thread
   advance race). `recover_interrupted` (bindable via `recover_on_open`) flips a
   post-restart `in_progress` record to `interrupted`, never a silent
   completion, mirroring the conversation store's streaming -> `interrupted`
   reload recovery. Only bounded SAFE usage is persisted — a `SafeUsage` of
   non-negative bounded integer token counts plus an optional duration; there is
   no free-form string field on any persisted row, so no credential/bearer/
   authorization is representable (criterion 4, proven by an auth-marker scan and
   a closed-field-set assertion). `LIFECYCLE_STATE_FOR_OUTCOME` bridges the
   relay's settled `StreamOutcome` values to lifecycle terminals
   (`serving_unavailable` -> `interrupted`) without importing the relay.
   `tests/test_response_lifecycle_store.py` is hermetic (25 tests). Implemented
   as a persistence slice; the production Postgres backend is still pending and
   it is not yet wired into `create_app`, a browser endpoint, or the relay's
   turn-append path.
