import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import {
  addDirective, approve, bootstrap, createProject, createSession, fetchRoutes, probeSkills,
  runSandbox, searchEvidence, startWorkflow, taskLineage,
  archiveConversation, branchTurn, createConversation, deleteConversation, fetchChatRoutes,
  getConversation, listConversations, renameConversation, retryTurn, searchConversations,
  sendMessage, unarchiveConversation,
  fetchPrdContent, fetchPrdTasks, fetchTaskEligibility,
  transcribeVoice, speakMessage, fetchPreferences,
  fetchConfigurationExport, previewConfigurationImport, applyConfigurationImport,
  previewConfigurationReset, applyConfigurationReset,
  fetchAdvancedRoutes, runAdvancedBranch,
} from './api'

vi.mock('./api', () => ({
  addDirective: vi.fn(), approve: vi.fn(), bootstrap: vi.fn(), createProject: vi.fn(), createSession: vi.fn(),
  fetchRoutes: vi.fn(), probeSkills: vi.fn(), runSandbox: vi.fn(), searchEvidence: vi.fn(), startWorkflow: vi.fn(), taskLineage: vi.fn(),
  voiceSocketUrl: vi.fn(() => 'ws://workbench.test/api/sessions/session_1/voice/realtime'),
  archiveConversation: vi.fn(), branchTurn: vi.fn(), createConversation: vi.fn(), deleteConversation: vi.fn(),
  fetchChatRoutes: vi.fn(), getConversation: vi.fn(), listConversations: vi.fn(), renameConversation: vi.fn(),
  retryTurn: vi.fn(), searchConversations: vi.fn(), sendMessage: vi.fn(), unarchiveConversation: vi.fn(),
  fetchPrdContent: vi.fn(), fetchPrdTasks: vi.fn(), fetchTaskReference: vi.fn(), fetchTaskEligibility: vi.fn(),
  transcribeVoice: vi.fn(), speakMessage: vi.fn(), fetchPreferences: vi.fn(),
  fetchConfigurationExport: vi.fn(), previewConfigurationImport: vi.fn(), applyConfigurationImport: vi.fn(),
  previewConfigurationReset: vi.fn(), applyConfigurationReset: vi.fn(),
  fetchAdvancedRoutes: vi.fn(), runAdvancedBranch: vi.fn(),
  // The panel keys its unconfigured-degrade branch off this SHARED sentinel by
  // value equality; the mock must export the exact string api.js throws on 503.
  ADVANCED_NOT_CONFIGURED: 'The advanced playground is not configured for this hub',
}))

// The served advanced-route allowlist shape, traceable to
// `AdvancedRouteCapability.browser_projection()` / `control_view()`
// (workbench/advanced_routes.py): identifiers, digests, and declared control
// metadata (type/bounds/allowed_values/default/editable/source) ONLY. One int
// control (temperature_milli) with bounds, one enum (reasoning_effort), and one
// policy-owned bool the operator cannot override.
const advancedRoutes = [
  {
    provider: 'anvil-serving', route_id: 'route.chat-fast', display_name: 'Chat fast',
    route_digest: 'sha256:' + 'a'.repeat(64), profile_digest: 'sha256:' + 'b'.repeat(64),
    serving_contract_version: '1.0.0', model_profile: 'chat-fast',
    structured_output_supported: false, tools_supported: false,
    controls: [
      { name: 'temperature_milli', type: 'int', default: 300, editable: true, source: 'route_default', disabled_reason: null, bounds: { min: 0, max: 1000 } },
      { name: 'reasoning_effort', type: 'enum', default: 'low', editable: true, source: 'route_default', disabled_reason: null, allowed_values: ['low', 'medium', 'high'] },
      { name: 'response_streaming', type: 'bool', default: true, editable: false, source: 'policy_owned', disabled_reason: 'policy_owned' },
    ],
  },
  {
    provider: 'anvil-serving', route_id: 'route.chat-heavy', display_name: 'Chat heavy',
    route_digest: 'sha256:' + 'c'.repeat(64), profile_digest: 'sha256:' + 'd'.repeat(64),
    serving_contract_version: '1.0.0', model_profile: 'chat-heavy',
    structured_output_supported: true, tools_supported: true,
    controls: [
      // A NARROWER int range so a temperature tuned high on fast becomes stale here.
      { name: 'temperature_milli', type: 'int', default: 200, editable: true, source: 'route_default', disabled_reason: null, bounds: { min: 0, max: 500 } },
      // No reasoning_effort here — a tuned reasoning_effort becomes unsupported.
      { name: 'max_output_tokens', type: 'int', default: 1024, editable: true, source: 'route_default', disabled_reason: null, bounds: { min: 1, max: 4096 } },
    ],
  },
]

// The served advanced-trace.v1 record (contracts.validate_advanced_trace): ids,
// digests, bounded counters, safe summaries, per-event digests — NO raw output.
const advancedTrace = {
  schema_version: 'workbench-advanced-trace/v1',
  trace_id: 'advtrace_fast_0001',
  branch_ref: { branch_id: 'advbranch_' + 'f'.repeat(10), conversation_id: 'conv_' + 'a'.repeat(10), turn_id: 'turn_' + 'b'.repeat(10) },
  route_decision: { provider: 'anvil-serving', route_id: 'route.chat-fast', route_digest: 'sha256:' + 'a'.repeat(64), profile_digest: 'sha256:' + 'b'.repeat(64), model_profile: 'chat-fast', request_id: 'req_abc123' },
  request: { content_trust: 'untrusted_task_data', redacted: true, input_chars: 42, structured_output_mode: 'text', control_values: [{ name: 'temperature_milli', value: 300 }, { name: 'reasoning_effort', value: 'high' }] },
  events: [
    { seq: 0, kind: 'request_start', at: '2026-07-21T10:01:00Z' },
    { seq: 1, kind: 'tool_result', at: '2026-07-21T10:01:02Z', tool_id: 'echo_fixture', tool_kind: 'mock', output_digest: 'sha256:' + 'c'.repeat(64), output_chars: 32, safe_summary: 'mock fixture returned a bounded result' },
    { seq: 2, kind: 'response_complete', at: '2026-07-21T10:01:05Z', usage: { input_tokens: 12, output_tokens: 20, latency_ms: 120 } },
  ],
  usage: { input_tokens: 12, output_tokens: 20, latency_ms: 120 },
  status: 'complete',
  redaction: { status: 'redacted', ruleset: 'advanced-trace-v1' },
  created_at: '2026-07-21T10:01:00Z', completed_at: '2026-07-21T10:01:05Z',
}

const fixture = {
  actor: 'operator',
  projects: [{ id: 'project_1', name: 'Workbench qualification', state_root: '.anvil', bridge_id: 'bridge_1' }],
  sessions: [{ id: 'session_1', project_id: 'project_1', title: 'Router qualification', worktree_id: 'default', status: 'active' }],
  workflows: [{ id: 'workflow_1', project_id: 'project_1', session_id: 'session_1', version: 1, status: 'draft', cursor: [] }],
  runs: [{ id: 'run_1', project_id: 'project_1', session_id: 'session_1', task_id: 'TASK-1', model: 'heavy-local', status: 'evidenced' }],
  approvals: [
    { id: 'approval_decoy', project_id: 'project_1', action_type: 'merge_and_accept', status: 'pending', payload_hash: 'decoy123', payload: { run_id: 'run_decoy', worktree_id: 'other', pr: '99' } },
    { id: 'approval_1', project_id: 'project_1', action_type: 'commit_pr', status: 'pending', payload_hash: 'abc123', payload: { run_id: 'run_1', session_id: 'session_1', worktree_id: 'default', lease_epoch: 3, diff_hash: 'tree123', branch: 'codex/review' } },
  ],
  skills: [{ bridge_id: 'bridge_1', skill_id: 'anvil:review', description: 'Review evidence.', content_sha256: 'a'.repeat(64) }],
  directives: [{ id: 'event_1', session_id: 'session_1', sequence: 3, kind: 'operator.directive', data: { content: 'Run the evidence gate.' } }],
  audit: [{ id: 'audit_1', kind: 'bridge.skills_published', actor: 'bridge:bridge_1' }],
  router_configured: true,
  sandbox: { available: true, models: ['fast-local'] },
  voice: { available: false, transport: 'not_configured', retains_transcripts: false },
}

// Chat fixtures: an active conversation bound to a delivery context, plus an
// archived one that only appears when archived are opted in.
const activeConversation = {
  id: 'conv_active', title: 'Router planning', status: 'active', ephemeral: false, pinned: false, tags: [], folder: null, updated_at: 't2',
  context: { project: { title: 'Checkout revamp', id: 'project_1' }, prd: { title: 'PRD: chat-first', id: 'prd_1' }, task: { title: 'Route selection', id: 'TASK-1' } },
}
const archivedConversation = { id: 'conv_arch', title: 'Old triage', status: 'archived', ephemeral: false, pinned: false, tags: [], folder: null, updated_at: 't1' }
const secondConversation = { id: 'conv_b', title: 'Second thread', status: 'active', ephemeral: false, pinned: false, tags: [], folder: null, updated_at: 't3' }
const chatRoutes = [{ route_id: 'route.fast', display_name: 'Fast local' }, { route_id: 'route.deep', display_name: 'Deep local' }]

function assistantTurn(id, text, status = 'complete', kind = 'initial') {
  return { id, conversation_id: 'conv_active', role: 'assistant', status, content: [{ text }], lineage: { kind } }
}

function resetChatMocks() {
  listConversations.mockImplementation(({ includeArchived } = {}) =>
    Promise.resolve({ conversations: includeArchived ? [activeConversation, archivedConversation] : [activeConversation] }))
  searchConversations.mockResolvedValue({ conversations: [activeConversation] })
  getConversation.mockResolvedValue({ conversation: activeConversation, turns: [] })
  createConversation.mockResolvedValue({ id: 'conv_new', title: 'Untitled conversation', status: 'active', tags: [] })
  renameConversation.mockImplementation((id, title) => Promise.resolve({ id, title, status: 'active', tags: [] }))
  archiveConversation.mockResolvedValue({})
  unarchiveConversation.mockResolvedValue({})
  deleteConversation.mockResolvedValue({})
  fetchChatRoutes.mockResolvedValue({ routes: chatRoutes })
  // Default served /api/preferences payload with autoplay OFF (the effective row
  // shape the hub actually serves). Individual tests override for autoplay-ON.
  fetchPreferences.mockResolvedValue({ catalog: { settings: [] }, effective: [{ setting_id: 'personal.voice_autoplay', scope: 'personal', value: false, source: 'default' }] })
  // The configuration (backup & transfer) workflows default to safe, empty
  // served shapes so navigating to Settings never crashes; individual tests
  // override for the export/import/reset flows.
  fetchConfigurationExport.mockResolvedValue({ schema_version: 'workbench-configuration-export/v1', source: { scope: 'personal', actor_ref: 'actorref:0123456789abcdef', catalog_id: 'workbench.settings.initial' }, settings: [] })
  previewConfigurationImport.mockResolvedValue({ status: 'previewed', preview: { valid: true, creates: [], changes: [], resets: [], skipped_read_only: [], unavailable_references: [], repairable: [], no_ops: [], base_versions: {} } })
  applyConfigurationImport.mockResolvedValue({ status: 'applied', result: { applied: [] }, applied: [], appliedCount: 0 })
  previewConfigurationReset.mockResolvedValue({ status: 'previewed', preview: { scope: 'personal', changes: [], base_versions: {} } })
  applyConfigurationReset.mockResolvedValue({ status: 'reset', result: { applied: [] }, applied: [], appliedCount: 0, scope: 'personal' })
  sendMessage.mockResolvedValue({ text: '', terminal: 'completed', needsRefresh: false })
  retryTurn.mockResolvedValue(assistantTurn('turn_retry', 'second answer', 'complete', 'retry'))
  branchTurn.mockResolvedValue(assistantTurn('turn_branch', 'branched answer', 'complete', 'branch'))
  // Advanced runtime is unconfigured BY DEFAULT (503 sentinel) so the panel
  // degrades truthfully; the advanced-flow tests opt into a configured runtime.
  fetchAdvancedRoutes.mockRejectedValue(new Error('The advanced playground is not configured for this hub'))
  runAdvancedBranch.mockResolvedValue({ text: 'tuned answer', terminal: 'completed', needsRefresh: false, trace: advancedTrace, turnId: 'turn_adv', branchId: 'advbranch_srv' })
}

beforeEach(() => {
  vi.clearAllMocks()
  // The explorer writes window.location.hash; jsdom persists it across tests in a
  // file, so reset it here (suite NOTE #4) to keep hash assertions independent.
  window.location.hash = ''
  bootstrap.mockResolvedValue(fixture)
  approve.mockResolvedValue({ status: 'approved' })
  addDirective.mockResolvedValue({ outcome: 'directive.queued_pending', recorded: true, event: { id: 'event_2', session_id: 'session_1', sequence: 4, kind: 'operator.directive', data: { content: 'Check route evidence.' } } })
  createProject.mockResolvedValue({ id: 'project_2', name: 'Checkout', state_root: '.anvil', bridge_id: null })
  createSession.mockResolvedValue({ session: { id: 'session_2', project_id: 'project_1', title: 'Checkout', worktree_id: 'checkout', status: 'active' }, workflow: { id: 'workflow_2', session_id: 'session_2', version: 1, status: 'draft' } })
  startWorkflow.mockResolvedValue({ run: { id: 'run_2', project_id: 'project_1', session_id: 'session_2', task_id: 'TASK-2', model: 'planning', status: 'queued' } })
  fetchRoutes.mockResolvedValue({ routes: [{ request_id: 'req_1', workbench_run_id: 'run_1', task_id: 'TASK-1', model: 'heavy-local', status: 'served' }] })
  searchEvidence.mockResolvedValue({ results: [{ citation: 'evidence:run_1', title: 'Verification result' }] })
  taskLineage.mockResolvedValue({ task_id: 'TASK-1', lineage: [{ kind: 'pull_request' }] })
  runSandbox.mockResolvedValue({ model: 'fast-local', status: 'completed', output_text: 'sandbox response' })
  probeSkills.mockResolvedValue({ accepted: true })
  resetChatMocks()
})

// Chat is the default surface now, so the delivery suite navigates to Delivery
// first. This keeps the delivery assertions faithful while proving Chat-default.
async function renderLive() {
  const user = userEvent.setup()
  render(<App />)
  await user.click(await screen.findByRole('button', { name: 'Delivery' }))
  await screen.findByRole('heading', { name: 'Task TASK-1' })
}

// Render the default Chat surface and wait for its conversation list to load.
async function renderChat() {
  const user = userEvent.setup()
  render(<App />)
  await screen.findByText('Router planning')
  return user
}

async function openConversation(user, turns) {
  if (turns) getConversation.mockResolvedValueOnce({ conversation: activeConversation, turns })
  await user.click(screen.getByRole('button', { name: 'Open Router planning' }))
}

describe('Workbench delivery cockpit', () => {
  it('gives every main navigation button a live operating surface', async () => {
    const user = userEvent.setup(); await renderLive()
    const views = [['Sessions', 'Concurrent sessions'], ['Runs', 'Runs'], ['Routes', 'Routes'], ['Approvals', 'Approvals'], ['Evidence', 'Evidence'], ['Skills', 'Reviewed skills'], ['Sandbox', 'Model sandbox']]
    for (const [button, heading] of views) { await user.click(screen.getByRole('button', { name: button })); expect(screen.getByRole('heading', { name: heading })).toBeTruthy() }
  })

  it('persists a delivery direction for the next local bridge work packet', async () => {
    // The reload after submit reflects the recorded directive (the real API
    // returns {outcome, recorded, event}; the app must read result.event, not
    // append the raw response).
    bootstrap.mockResolvedValue({ ...fixture, directives: [...fixture.directives, { id: 'event_2', session_id: 'session_1', sequence: 4, kind: 'operator.directive', data: { content: 'Check route evidence.' } }] })
    const user = userEvent.setup(); await renderLive()
    await user.type(screen.getByRole('textbox', { name: 'Add direction to this delivery' }), 'Check route evidence.')
    await user.click(screen.getByRole('button', { name: 'Send delivery direction' }))
    expect(addDirective).toHaveBeenCalledWith('session_1', 'Check route evidence.')
    expect((await screen.findByRole('status')).textContent).toContain('included only in the next bridge work packet')
    // The recorded directive is rendered as a delivery direction, proving the
    // {outcome, recorded, event} shape flowed through without dropping it.
    expect(await screen.findByText('Check route evidence.')).toBeTruthy()
  })

  it('surfaces a typed directive refusal truthfully and never announces it was recorded', async () => {
    // A recorded:false outcome is served 202 with no event row: the UI must not
    // claim "Direction recorded." and must surface the typed outcome code. A
    // regression to the old raw-event shape would append the refusal object and
    // still announce success, failing this test.
    addDirective.mockResolvedValueOnce({ outcome: 'directive.rejected_too_long', recorded: false })
    const user = userEvent.setup(); await renderLive()
    await user.type(screen.getByRole('textbox', { name: 'Add direction to this delivery' }), 'way too long')
    await user.click(screen.getByRole('button', { name: 'Send delivery direction' }))
    const notice = (await screen.findByRole('status')).textContent
    expect(notice).toContain('was not recorded')
    expect(notice).toContain('directive.rejected_too_long')
    expect(notice).not.toContain('Direction recorded')
  })

  it('operates routes, evidence, skills, and sandbox through their dedicated APIs', async () => {
    const user = userEvent.setup(); await renderLive()
    await user.click(screen.getByRole('button', { name: 'Routes' })); await user.click(screen.getByRole('button', { name: 'Refresh decisions' })); expect(fetchRoutes).toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Evidence' })); await user.type(screen.getByRole('textbox', { name: 'Evidence query' }), 'verification'); await user.click(screen.getByRole('button', { name: 'Search evidence' })); expect(searchEvidence).toHaveBeenCalledWith('project_1', 'verification'); expect(await screen.findByText('Verification result')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Show lineage for TASK-1' })); expect(taskLineage).toHaveBeenCalledWith('TASK-1')
    await user.click(screen.getByRole('button', { name: 'Skills' })); await user.click(screen.getByRole('button', { name: 'Verify bridge skills' })); expect(probeSkills).toHaveBeenCalledWith('project_1')
    await user.click(screen.getByRole('button', { name: 'Sandbox' })); await user.type(screen.getByRole('textbox', { name: 'Sandbox prompt' }), 'summarize this'); await user.click(screen.getByRole('button', { name: 'Run through Anvil Serving' })); expect(runSandbox).toHaveBeenCalledWith({ model: 'fast-local', input: 'summarize this' }); expect(await screen.findByText('sandbox response')).toBeTruthy()
  })

  it('creates sessions with selected bridge-published skills and starts the bridge workflow', async () => {
    const user = userEvent.setup(); await renderLive(); await user.click(screen.getByRole('button', { name: 'Sessions' })); await user.click(screen.getByRole('button', { name: 'New concurrent session' }))
    await user.type(screen.getByRole('textbox', { name: 'Session title' }), 'Checkout'); await user.clear(screen.getByRole('textbox', { name: 'Configured worktree id' })); await user.type(screen.getByRole('textbox', { name: 'Configured worktree id' }), 'checkout'); await user.click(screen.getByRole('checkbox', { name: 'anvil:review' })); await user.click(screen.getByRole('button', { name: 'Create session' }))
    expect(createSession).toHaveBeenCalledWith({ project_id: 'project_1', title: 'Checkout', worktree_id: 'checkout', skills: ['anvil:review'] })
    await user.click(screen.getByRole('button', { name: 'Start delivery Router qualification' })); await user.type(screen.getByRole('textbox', { name: 'State task id' }), 'TASK-2'); await user.click(screen.getByRole('button', { name: 'Start bridge delivery' })); expect(startWorkflow).toHaveBeenCalledWith('workflow_1', { task_id: 'TASK-2', model: 'planning' })
  })

  it('uses the guide, creation action, notifications, profile menu, and hash-bound approval intentionally', async () => {
    const user = userEvent.setup(); await renderLive()
    await user.click(screen.getByRole('button', { name: 'Help' })); expect(screen.getByRole('dialog', { name: 'Workbench setup guide' })).toBeTruthy(); await user.click(screen.getByRole('button', { name: 'Close Workbench setup guide' }))
    await user.click(screen.getByRole('button', { name: 'New delivery' })); await user.type(screen.getByRole('textbox', { name: 'Project name' }), 'Checkout'); await user.click(screen.getByRole('button', { name: 'Create project' })); expect(createProject).toHaveBeenCalledWith({ name: 'Checkout', state_root: '.anvil' })
    await user.click(screen.getByRole('button', { name: 'Operator menu' })); expect(screen.getByRole('region', { name: 'Operator menu' })).toBeTruthy(); await user.click(screen.getByRole('button', { name: 'Close menu' }))
    await user.click(screen.getByRole('button', { name: 'Notifications' })); expect(screen.getByRole('region', { name: 'Notifications' })).toBeTruthy(); await user.click(screen.getByRole('button', { name: 'Mark viewed' })); expect(screen.getByText(/marked viewed/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Authorize selected action' }).disabled).toBe(true)
    await user.click(screen.getByRole('button', { name: 'Approvals' })); await user.click(screen.getByRole('button', { name: 'Review action approval_1' }))
    expect(screen.getByLabelText('Selected approval payload').textContent).toContain('codex/review')
    expect(screen.getByText('run_1')).toBeTruthy(); expect(screen.getByText('default')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Authorize selected action' })); expect(approve).toHaveBeenCalledWith('approval_1'); expect(approve).not.toHaveBeenCalledWith('approval_decoy')
  })

  it('does not offer voice capture without a configured private relay', async () => {
    await renderLive(); expect(screen.getByRole('button', { name: 'Voice not configured' }).disabled).toBe(true); expect(screen.getByText(/Configure a private Anvil Voice Realtime endpoint/)).toBeTruthy()
  })

  it('shows a truthful empty state instead of a seeded delivery when the hub has no projects', async () => {
    bootstrap.mockResolvedValueOnce({ projects: [] }); const user = userEvent.setup(); render(<App />)
    await user.click(await screen.findByRole('button', { name: 'Delivery' }))
    expect(await screen.findByRole('heading', { name: 'Start a private delivery' })).toBeTruthy(); expect(screen.getByText(/no synthetic delivery/)).toBeTruthy()
  })
})

describe('Chat conversation rail (T004.2)', () => {
  it('manages conversations directly from the rail', async () => {
    const user = await renderChat()
    expect(screen.getByRole('navigation', { name: 'Conversations' })).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Start a new conversation' }))
    expect(createConversation).toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Archive Router planning' }))
    expect(archiveConversation).toHaveBeenCalledWith('conv_active')
    // Delete is a two-step confirm (a11y #7): the first press only arms it, so a
    // single stray keypress cannot destroy a conversation.
    await user.click(screen.getByRole('button', { name: 'Delete Router planning' }))
    expect(deleteConversation).not.toHaveBeenCalled() // not yet — armed, not fired
    await user.click(screen.getByRole('button', { name: 'Confirm delete Router planning' }))
    expect(deleteConversation).toHaveBeenCalledWith('conv_active')
    // Rename last: it changes the row's accessible name, so it cannot precede the
    // by-title archive/delete lookups above.
    await user.click(screen.getByRole('button', { name: 'Rename Router planning' }))
    const renameField = screen.getByRole('textbox', { name: 'Rename Router planning' })
    await user.clear(renameField); await user.type(renameField, 'Router evidence')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(renameConversation).toHaveBeenCalledWith('conv_active', 'Router evidence')
    // Render assertion (correctness #5): the setConversations map must actually
    // update the rendered row title — deleting App.jsx's rename map fails here.
    expect(await screen.findByText('Router evidence')).toBeTruthy()
    expect(screen.queryByText('Router planning')).toBeNull()
  })

  it('reflects archive and delete outcomes in the rendered rail, not just the call', async () => {
    // Correctness #5: assert the rail re-renders from the refreshed server list —
    // after archive the row moves into the Archived section; after delete it is
    // gone. Deleting App.jsx's setConversations refresh path fails these.
    const user = await renderChat()
    // Show archived so both sections render, then archive the active row; the
    // post-archive refresh returns it with an archived status.
    await user.click(screen.getByRole('checkbox', { name: 'Show archived conversations' }))
    listConversations.mockResolvedValue({
      conversations: [{ ...activeConversation, status: 'archived' }, archivedConversation],
    })
    await user.click(screen.getByRole('button', { name: 'Archive Router planning' }))
    const archived = await screen.findByRole('region', { name: 'Archived conversations' })
    expect(within(archived).getByText('Router planning')).toBeTruthy() // moved to Archived
    const active = screen.getByRole('region', { name: 'Active conversations' })
    expect(within(active).queryByText('Router planning')).toBeNull()
    // Delete it from the Archived section: the post-delete refresh omits it.
    listConversations.mockResolvedValue({ conversations: [archivedConversation] })
    await user.click(within(archived).getByRole('button', { name: 'Delete Router planning' }))
    await user.click(screen.getByRole('button', { name: 'Confirm delete Router planning' }))
    expect(deleteConversation).toHaveBeenCalledWith('conv_active')
    await waitFor(() => expect(screen.queryByText('Router planning')).toBeNull()) // row disappeared
    expect(screen.getByText('Old triage')).toBeTruthy() // the other row remains
  })

  it('keeps the title and state prominent while the id is secondary disclosure', async () => {
    await renderChat()
    expect(screen.getByText('Router planning')).toBeTruthy()
    expect(screen.getByText('active')).toBeTruthy()
    const id = screen.getByText('conv_active')
    expect(id.tagName).toBe('SMALL') // the canonical id renders only as a muted <small>, never the heading
  })

  it('searches over titles and retained content and renders the server results', async () => {
    const user = await renderChat()
    // A content-only match: the title does not contain the query, but the server
    // (which searches retained content) returns it, and the rail surfaces it.
    searchConversations.mockResolvedValue({ conversations: [{ id: 'conv_c', title: 'Weekly sync', status: 'active', tags: [] }] })
    await user.type(screen.getByRole('searchbox', { name: 'Search conversations' }), 'invoices')
    // The search is debounced (a11y #9), so wait for the rendered result, then
    // assert the one settled request carried the full query.
    expect(await screen.findByText('Weekly sync')).toBeTruthy()
    expect(searchConversations).toHaveBeenCalledWith('invoices', expect.objectContaining({ includeArchived: false }))
  })

  it('keeps active and archived conversations visibly distinct', async () => {
    const user = await renderChat()
    expect(screen.queryByText('Old triage')).toBeNull() // archived hidden by default
    await user.click(screen.getByRole('checkbox', { name: 'Show archived conversations' }))
    expect(listConversations).toHaveBeenLastCalledWith(expect.objectContaining({ includeArchived: true }))
    const archivedSection = await screen.findByRole('region', { name: 'Archived conversations' })
    expect(within(archivedSection).getByText('Old triage')).toBeTruthy()
    const activeSection = screen.getByRole('region', { name: 'Active conversations' })
    expect(within(activeSection).queryByText('Old triage')).toBeNull()
  })

  it('shows bound project, PRD, and task titles with ids as secondary disclosure', async () => {
    // NOTE: `activeConversation.context` ({project,prd,task}:{title,id}) is a
    // PROPOSED projection shape — the merged conversation projection does not yet
    // emit it (see conversation_api.py:turn_json/conversation_json, which carry no
    // delivery binding). This pins the intended DeliveryContext render for when
    // the binding is emitted; the truthful "No linked delivery context" degrade
    // (asserted where context is absent) is the current shipped behavior.
    const user = await renderChat()
    await openConversation(user, [])
    const context = await screen.findByRole('region', { name: 'Linked delivery context' })
    expect(within(context).getByText('Checkout revamp')).toBeTruthy()
    expect(within(context).getByText('PRD: chat-first')).toBeTruthy()
    expect(within(context).getByText('Route selection')).toBeTruthy()
    // The canonical id lives inside a collapsed <details>, not beside the title.
    expect(within(context).getByText('project_1').closest('details')).toBeTruthy()
  })
})

describe('Chat transcript, composer, and streaming (T004.3)', () => {
  it('submits a multiline composer with Enter and keeps Shift+Enter as a newline', async () => {
    const user = await renderChat()
    await openConversation(user, [])
    const composer = screen.getByRole('textbox', { name: 'Message composer' })
    await user.type(composer, 'first line{Shift>}{Enter}{/Shift}second line')
    expect(sendMessage).not.toHaveBeenCalled() // Shift+Enter must not submit
    expect(composer.value).toContain('\n')
    await user.type(composer, '{Enter}') // a bare Enter submits
    expect(sendMessage).toHaveBeenCalledTimes(1)
    expect(sendMessage.mock.calls[0][0]).toMatchObject({ conversationId: 'conv_active', routeId: 'route.fast' })
  })

  it('renders streaming output incrementally and cancels it into a distinct state', async () => {
    const user = await renderChat()
    await openConversation(user, [])
    sendMessage.mockImplementation(({ onState, signal }) => {
      onState({ text: 'Hel', terminal: null })
      onState({ text: 'Hello', terminal: null })
      return new Promise((resolve) => { signal.addEventListener('abort', () => resolve({ text: 'Hello', terminal: 'cancelled' })) })
    })
    await user.type(screen.getByRole('textbox', { name: 'Message composer' }), 'hi there')
    await user.click(screen.getByRole('button', { name: 'Send message' }))
    expect(await screen.findByText('Hello')).toBeTruthy() // incremental streamed text
    expect(screen.getByText('Streaming response…')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Cancel streaming response' }))
    expect(await screen.findByText('Response cancelled')).toBeTruthy()
    expect(screen.getByText('cancelled')).toBeTruthy() // the turn settled cancelled, never complete
  })

  it('refreshes the transcript from the snapshot after a dropped-frame reconnect', async () => {
    const user = await renderChat()
    await openConversation(user, [])
    sendMessage.mockResolvedValueOnce({ text: 'partial', terminal: 'completed', needsRefresh: true })
    getConversation.mockResolvedValueOnce({ conversation: activeConversation, turns: [assistantTurn('t_ref', 'reconnected answer')] })
    await user.type(screen.getByRole('textbox', { name: 'Message composer' }), 'summarize')
    await user.click(screen.getByRole('button', { name: 'Send message' }))
    expect(await screen.findByText('reconnected answer')).toBeTruthy()
    expect(getConversation).toHaveBeenCalledTimes(2) // once on open, once on reconnect refresh
  })

  it('records a retry as a visible successor instead of rewriting the prior turn', async () => {
    const user = await renderChat()
    await openConversation(user, [assistantTurn('turn_1', 'first answer')])
    expect(await screen.findByText('first answer')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Retry this response' }))
    // Retry posts a normalized {kind:'text',text} slice as an assistant sibling —
    // never the server-loaded block verbatim (which carries content_trust → 422).
    expect(retryTurn).toHaveBeenCalledWith('conv_active', 'turn_1', expect.objectContaining({
      role: 'assistant', status: 'complete', content: [{ kind: 'text', text: 'first answer' }],
    }))
    expect(await screen.findByText('second answer')).toBeTruthy()
    expect(screen.getByText('first answer')).toBeTruthy() // the original turn is preserved, not rewritten
  })

  it('records a branch as a follow-up user turn with a normalized body', async () => {
    const user = await renderChat()
    await openConversation(user, [assistantTurn('turn_1', 'first answer')])
    expect(await screen.findByText('first answer')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Branch from this response' }))
    // Branch opens a USER successor (server turn tree), body normalized to
    // {kind:'text',text} — not an assistant repost of the prior answer (#1).
    expect(branchTurn).toHaveBeenCalledWith('conv_active', 'turn_1', expect.objectContaining({
      role: 'user', status: 'complete', content: [{ kind: 'text', text: 'first answer' }],
    }))
    expect(await screen.findByText('branched answer')).toBeTruthy()
  })

  it('does not let a settled stream from one conversation land in another (state-drift #2)', async () => {
    listConversations.mockResolvedValue({ conversations: [activeConversation, secondConversation] })
    const user = await renderChat()
    await openConversation(user, [])

    // A's stream emits 'A-answer', then stays pending until the test settles it.
    let settleA
    sendMessage.mockImplementation(({ conversationId, onState }) => {
      if (conversationId === 'conv_active') {
        onState({ text: 'A-answer', terminal: null })
        return new Promise((resolve) => { settleA = () => resolve({ text: 'A-answer', terminal: 'completed' }) })
      }
      // Conversation B: pending until its Cancel aborts the signal.
      return new Promise((resolve) => { /* B settles via signal below */ })
    })
    await user.type(screen.getByRole('textbox', { name: 'Message composer' }), 'hi A')
    await user.click(screen.getByRole('button', { name: 'Send message' }))
    expect(await screen.findByText('A-answer')).toBeTruthy() // A streaming in A's view

    // Switch to B mid-stream: A's in-flight stream is aborted and B opens empty.
    getConversation.mockResolvedValueOnce({ conversation: secondConversation, turns: [] })
    await user.click(screen.getByRole('button', { name: 'Open Second thread' }))
    expect(await screen.findByLabelText('Empty conversation')).toBeTruthy()

    // Start a stream in B that settles cancelled when its signal aborts.
    sendMessage.mockImplementation(({ signal }) =>
      new Promise((resolve) => { signal.addEventListener('abort', () => resolve({ text: 'B', terminal: 'cancelled' })) }))
    await user.type(screen.getByRole('textbox', { name: 'Message composer' }), 'hi B')
    await user.click(screen.getByRole('button', { name: 'Send message' }))
    expect(screen.getByText('Streaming response…')).toBeTruthy()

    // Now settle A. Its answer must NOT append to B, and must not clear B's stream.
    await act(async () => { settleA(); await Promise.resolve() })
    expect(screen.queryByText('A-answer')).toBeNull() // A's answer never lands in B

    // B's Cancel still works — A's finally must not have wiped B's controller.
    await user.click(screen.getByRole('button', { name: 'Cancel streaming response' }))
    expect(await screen.findByText('Response cancelled')).toBeTruthy()
  })

  it('preserves the transcript when Advanced mode opens and shows a truthful unavailable state (not-wired 503 degrade)', async () => {
    // fetchAdvancedRoutes rejects with the not-configured sentinel by default.
    const user = await renderChat()
    await openConversation(user, [assistantTurn('turn_1', 'kept answer')])
    expect(await screen.findByText('kept answer')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Toggle Advanced mode' }))
    expect(screen.getByRole('region', { name: 'Advanced controls' })).toBeTruthy()
    // Degrades truthfully: the not-configured sentinel plus the unchanged note.
    expect(await screen.findByText('The advanced playground is not configured for this hub')).toBeTruthy()
    expect(screen.getByText(/not configured in this build/)).toBeTruthy()
    expect(screen.getByText('kept answer')).toBeTruthy() // transcript unchanged by Advanced mode
    await user.click(screen.getByRole('button', { name: 'Toggle Advanced mode' }))
    expect(screen.getByText('kept answer')).toBeTruthy()
  })

  // --- Advanced playground, wired over the REAL served route/control/branch/trace
  // shapes (advanced-model-playground T005). The runtime is configured for these. --
  async function openAdvanced(user, turns) {
    fetchAdvancedRoutes.mockResolvedValue({ routes: advancedRoutes })
    const u = user || await renderChat()
    await openConversation(u, turns || [assistantTurn('turn_1', 'base answer')])
    await u.click(screen.getByRole('button', { name: 'Toggle Advanced mode' }))
    await screen.findByRole('combobox', { name: 'Advanced route' })
    return u
  }

  it('drives visible controls from the selected route and PREVIEWS stale values before dropping them (criterion 1)', async () => {
    const user = await openAdvanced()
    // The fast route's declared controls are rendered from the served descriptors.
    const temp = screen.getByRole('spinbutton', { name: 'temperature_milli' })
    expect(temp).toBeTruthy()
    expect(screen.getByRole('combobox', { name: 'reasoning_effort' })).toBeTruthy()
    // A policy-owned control is a real disabled input with a safe reason, never editable.
    expect(screen.getByRole('checkbox', { name: 'response_streaming' }).disabled).toBe(true)

    // Tune temperature to 800 (valid on fast, out of the heavy route's [0,500]).
    fireEvent.change(temp, { target: { value: '800' } })
    // Switch route: the panel PREVIEWS what will drop BEFORE it is removed.
    fireEvent.change(screen.getByRole('combobox', { name: 'Advanced route' }), { target: { value: 'route.chat-heavy' } })
    const preview = await screen.findByRole('alertdialog', { name: 'Route change preview' })
    // The stale values are visible in the preview WITH the current value, pre-drop.
    expect(within(preview).getByText('temperature_milli')).toBeTruthy()
    expect(within(preview).getByText('800')).toBeTruthy() // the value is shown before removal
    expect(within(preview).getByText(/outside this route’s allowed range/)).toBeTruthy()
    expect(within(preview).getByText('reasoning_effort')).toBeTruthy() // unsupported on heavy
    expect(within(preview).getAllByText(/not offered by this route/).length).toBeGreaterThan(0)

    // Nothing dropped until the operator commits.
    await user.click(within(preview).getByRole('button', { name: 'Apply route change' }))
    // After apply, heavy's controls render and reasoning_effort is gone.
    expect(await screen.findByRole('spinbutton', { name: 'max_output_tokens' })).toBeTruthy()
    expect(screen.queryByRole('combobox', { name: 'reasoning_effort' })).toBeNull()
  })

  it('keeps the current route (no drop) when the preview is declined', async () => {
    const user = await openAdvanced()
    fireEvent.change(screen.getByRole('spinbutton', { name: 'temperature_milli' }), { target: { value: '800' } })
    fireEvent.change(screen.getByRole('combobox', { name: 'Advanced route' }), { target: { value: 'route.chat-heavy' } })
    const preview = await screen.findByRole('alertdialog', { name: 'Route change preview' })
    await user.click(within(preview).getByRole('button', { name: 'Keep current route' }))
    // Fast route controls remain; reasoning_effort was never dropped.
    expect(screen.getByRole('combobox', { name: 'reasoning_effort' })).toBeTruthy()
    expect(screen.getByRole('combobox', { name: 'Advanced route' }).value).toBe('route.chat-fast')
  })

  it('runs an advanced branch as a sibling in the ONE transcript — no second transcript (criterion 2)', async () => {
    const user = await openAdvanced()
    await user.type(screen.getByRole('textbox', { name: 'Advanced prompt' }), 'try a tuned attempt')
    await user.click(screen.getByRole('button', { name: 'Run advanced branch' }))
    // The tuned answer appears in the SAME transcript alongside the base turn.
    expect(await screen.findByText('tuned answer')).toBeTruthy()
    expect(screen.getByText('base answer')).toBeTruthy()
    // Exactly ONE transcript exists — the branch did not spawn a duplicate.
    expect(screen.getAllByRole('list', { name: 'Transcript' })).toHaveLength(1)
    // The run request carried the closed submitted_controls, not free text.
    const call = runAdvancedBranch.mock.calls.at(-1)[0]
    expect(call.routeId).toBe('route.chat-fast')
    expect(call.controls).toEqual(expect.arrayContaining([
      expect.objectContaining({ name: 'temperature_milli', provenance: 'declared' }),
      expect.objectContaining({ name: 'response_streaming', provenance: 'policy_override', value: true }),
    ]))
  })

  it('Cancel genuinely aborts an in-flight advanced run via a threaded AbortController (criterion 2)', async () => {
    const user = await openAdvanced()
    // A run that only settles when its signal aborts — proving the component
    // threads a real AbortController whose signal reaches the client.
    let sawSignal = null
    runAdvancedBranch.mockImplementation(({ signal, onState }) => new Promise((resolve) => {
      sawSignal = signal
      signal.addEventListener('abort', () => {
        const cancelled = { text: 'partial', terminal: 'cancelled', needsRefresh: false }
        onState?.(cancelled)
        resolve({ ...cancelled, trace: null })
      })
    }))
    await user.type(screen.getByRole('textbox', { name: 'Advanced prompt' }), 'never resolves until cancelled')
    await user.click(screen.getByRole('button', { name: 'Run advanced branch' }))
    // The panel's Cancel is now shown (Run↔Cancel swap).
    const cancelBtn = await screen.findByRole('button', { name: 'Cancel advanced run' })
    expect(sawSignal).toBeTruthy()
    expect(sawSignal.aborted).toBe(false)
    await user.click(cancelBtn)
    expect(sawSignal.aborted).toBe(true) // the real signal reached — not a local flip
    expect(await screen.findByText('partial')).toBeTruthy()
  })

  it('inspects a branch trace as digests + safe summaries only — no raw output/secret (redaction gate)', async () => {
    const user = await openAdvanced()
    await user.type(screen.getByRole('textbox', { name: 'Advanced prompt' }), 'inspect me')
    await user.click(screen.getByRole('button', { name: 'Run advanced branch' }))
    await screen.findByText('tuned answer')
    await user.click(await screen.findByRole('button', { name: /Inspect/ }))
    const inspector = await screen.findByRole('region', { name: 'Advanced trace inspector' })
    // Digests are abbreviated sha256 tokens; the safe summary is shown.
    expect(within(inspector).getAllByText(/^sha256:[a-f0-9]{12}…$/).length).toBeGreaterThan(0)
    expect(within(inspector).getByText('mock fixture returned a bounded result')).toBeTruthy()
    // The redaction note is present and no raw endpoint/secret shape appears.
    expect(within(inspector).getByText(/Redacted digests and safe summaries only/)).toBeTruthy()
    expect(inspector.textContent).not.toMatch(/https?:\/\//)
    expect(inspector.textContent).not.toMatch(/Bearer\s/)
  })

  it('compares two settled branches side by side within the shell (criterion 2)', async () => {
    const user = await openAdvanced()
    const prompt = screen.getByRole('textbox', { name: 'Advanced prompt' })
    await user.type(prompt, 'first')
    await user.click(screen.getByRole('button', { name: 'Run advanced branch' }))
    await screen.findByText('tuned answer')
    // Run a second branch (a fork of the first is a new sibling run).
    runAdvancedBranch.mockResolvedValueOnce({ text: 'second tuned answer', terminal: 'completed', needsRefresh: false, trace: advancedTrace, turnId: 'turn_adv2', branchId: 'advbranch_srv2' })
    await user.click(screen.getByRole('button', { name: /Fork/ }))
    await screen.findByText('second tuned answer')
    // Select both to compare.
    const compareButtons = screen.getAllByRole('button', { name: /Compare/ })
    await user.click(compareButtons[0])
    await user.click(compareButtons[1])
    expect(await screen.findByRole('region', { name: 'Compare branches' })).toBeTruthy()
    expect(screen.getAllByRole('article', { name: /Comparison/ })).toHaveLength(2)
    // Still one transcript.
    expect(screen.getAllByRole('list', { name: 'Transcript' })).toHaveLength(1)
  })

  it('saves and reopens a branch without leaving the chat shell', async () => {
    const user = await openAdvanced()
    await user.type(screen.getByRole('textbox', { name: 'Advanced prompt' }), 'save me')
    await user.click(screen.getByRole('button', { name: 'Run advanced branch' }))
    await screen.findByText('tuned answer')
    await user.click(await screen.findByRole('button', { name: /Save/ }))
    // Saved is reflected and Reopen becomes available.
    const reopen = await screen.findByRole('button', { name: /Reopen/ })
    expect(reopen.disabled).toBe(false)
    await user.click(reopen)
    // Reopen loads the saved config back into the editor prompt.
    expect(screen.getByRole('textbox', { name: 'Advanced prompt' }).value).toBe('save me')
  })

  it('keeps no-conversation, empty, error, and interrupted states distinct', async () => {
    const user = await renderChat()
    expect(screen.getByLabelText('No conversation selected')).toBeTruthy()
    await openConversation(user, [])
    expect(screen.getByLabelText('Empty conversation')).toBeTruthy()
    sendMessage.mockRejectedValueOnce(new Error('transport down'))
    await user.type(screen.getByRole('textbox', { name: 'Message composer' }), 'one')
    await user.click(screen.getByRole('button', { name: 'Send message' }))
    expect(await screen.findByText('Response failed')).toBeTruthy()
    sendMessage.mockResolvedValueOnce({ text: 'partial', terminal: 'timed_out', needsRefresh: false })
    await user.type(screen.getByRole('textbox', { name: 'Message composer' }), 'two')
    await user.click(screen.getByRole('button', { name: 'Send message' }))
    expect(await screen.findByText('Response interrupted')).toBeTruthy() // distinct from 'Response failed'
  })

  // --- Focus + live-region behavior for the transient panes (a11y MUST-1/2/3) ---
  // These assert document.activeElement and the live region's textContent directly:
  // the earlier suite never did, so the focus-to-<body> regressions on every close
  // path shipped green. Each is written to FAIL against a build without the
  // focus-restore / save-announce and PASS with it (revert-detection demonstrated
  // for the preview-restore and the save-announce).

  it('MUST-1: the stale preview takes focus on open and RESTORES focus to the route select on Keep and Escape', async () => {
    const user = await openAdvanced()
    const routeSelect = screen.getByRole('combobox', { name: 'Advanced route' })
    fireEvent.change(screen.getByRole('spinbutton', { name: 'temperature_milli' }), { target: { value: '800' } })
    // Open the preview by proposing a route switch.
    fireEvent.change(routeSelect, { target: { value: 'route.chat-heavy' } })
    const preview = await screen.findByRole('alertdialog', { name: 'Route change preview' })
    expect(document.activeElement).toBe(preview) // focus moved INTO the pane, not left on body
    // Keep restores focus to the invoking route select.
    await user.click(within(preview).getByRole('button', { name: 'Keep current route' }))
    expect(document.activeElement).toBe(routeSelect)

    // Re-open and dismiss with Escape → focus restored to the route select again.
    fireEvent.change(routeSelect, { target: { value: 'route.chat-heavy' } })
    const preview2 = await screen.findByRole('alertdialog', { name: 'Route change preview' })
    expect(document.activeElement).toBe(preview2)
    await user.keyboard('{Escape}')
    expect(document.activeElement).toBe(routeSelect)
  })

  it('MUST-1: Apply restores focus to the route select (never body) after the preview commits', async () => {
    const user = await openAdvanced()
    const routeSelect = screen.getByRole('combobox', { name: 'Advanced route' })
    fireEvent.change(screen.getByRole('spinbutton', { name: 'temperature_milli' }), { target: { value: '800' } })
    fireEvent.change(routeSelect, { target: { value: 'route.chat-heavy' } })
    const preview = await screen.findByRole('alertdialog', { name: 'Route change preview' })
    await user.click(within(preview).getByRole('button', { name: 'Apply route change' }))
    expect(document.activeElement).toBe(routeSelect)
    expect(document.activeElement).not.toBe(document.body)
  })

  it('MUST-1: inspector and compare focus their heading on open and restore focus to the invoking button on close', async () => {
    const user = await openAdvanced()
    await user.type(screen.getByRole('textbox', { name: 'Advanced prompt' }), 'inspect focus')
    await user.click(screen.getByRole('button', { name: 'Run advanced branch' }))
    await screen.findByText('tuned answer')

    // Inspector: opening focuses its heading; Close restores to the Inspect button.
    const inspectBtn = await screen.findByRole('button', { name: /Inspect/ })
    await user.click(inspectBtn)
    const inspectHeading = await screen.findByRole('heading', { name: /Trace inspector/ })
    expect(document.activeElement).toBe(inspectHeading)
    await user.click(screen.getByRole('button', { name: 'Close inspector' }))
    expect(document.activeElement).toBe(inspectBtn)

    // Escape also closes the inspector and restores focus to the Inspect button.
    await user.click(inspectBtn)
    await screen.findByRole('heading', { name: /Trace inspector/ })
    await user.keyboard('{Escape}')
    expect(document.activeElement).toBe(inspectBtn)

    // Compare: run a second settled branch, select both, heading takes focus, and
    // Close restores focus to the second (opening) Compare control.
    runAdvancedBranch.mockResolvedValueOnce({ text: 'second tuned answer', terminal: 'completed', needsRefresh: false, trace: advancedTrace, turnId: 'turn_adv2', branchId: 'advbranch_srv2' })
    await user.click(screen.getByRole('button', { name: /Fork/ }))
    await screen.findByText('second tuned answer')
    const compareButtons = screen.getAllByRole('button', { name: /Compare/ })
    await user.click(compareButtons[0])
    await user.click(compareButtons[1])
    const compareHeading = await screen.findByRole('heading', { name: 'Comparing two branches' })
    expect(document.activeElement).toBe(compareHeading)
    await user.click(screen.getByRole('button', { name: 'Close comparison' }))
    expect(document.activeElement).toBe(compareButtons[1])
  })

  it('MUST-2: saving a branch ANNOUNCES in the live region and moves focus to Reopen with a correct accessible name (never body)', async () => {
    const user = await openAdvanced()
    await user.type(screen.getByRole('textbox', { name: 'Advanced prompt' }), 'save me')
    await user.click(screen.getByRole('button', { name: 'Run advanced branch' }))
    await screen.findByText('tuned answer')
    const saveBtn = await screen.findByRole('button', { name: /^Save/ })
    await user.click(saveBtn)
    // Focus is NOT dropped to <body>; it lands on the now-enabled Reopen control.
    const reopen = screen.getByRole('button', { name: /Reopen/ })
    expect(document.activeElement).toBe(reopen)
    expect(document.activeElement).not.toBe(document.body)
    // The save is announced in the panel's role=status live region.
    const live = document.querySelector('.adv-live')
    expect(live.textContent).toMatch(/saved/i)
    // The saved control's accessible name matches its visible text (WCAG 2.5.3).
    const saved = screen.getByRole('button', { name: /^Saved/ })
    expect(saved.textContent).toBe('Saved')
  })

  it('S1: the Retry control re-runs the SAME branch config as a sibling in the one transcript', async () => {
    const user = await openAdvanced()
    await user.type(screen.getByRole('textbox', { name: 'Advanced prompt' }), 'retry me')
    await user.click(screen.getByRole('button', { name: 'Run advanced branch' }))
    await screen.findByText('tuned answer')
    // Scope to the branch row's OWN Retry control (distinct from the transcript's
    // per-turn "Retry this response"): this drives Retry's own enabled state + path.
    const branchRegion = screen.getByRole('region', { name: 'Advanced branches' })
    const retryBtn = await within(branchRegion).findByRole('button', { name: /Retry/ })
    expect(retryBtn.disabled).toBe(false) // its own enabled state, driven by branchOps
    runAdvancedBranch.mockResolvedValueOnce({ text: 'retried answer', terminal: 'completed', needsRefresh: false, trace: advancedTrace, turnId: 'turn_adv_r', branchId: 'advbranch_r' })
    await user.click(retryBtn)
    expect(await screen.findByText('retried answer')).toBeTruthy()
    // Still exactly one transcript — Retry is a sibling, not a duplicate transcript.
    expect(screen.getAllByRole('list', { name: 'Transcript' })).toHaveLength(1)
    // Retry re-ran the identical route + prompt config.
    const call = runAdvancedBranch.mock.calls.at(-1)[0]
    expect(call.routeId).toBe('route.chat-fast')
    expect(call.prompt).toBe('retry me')
  })

  it('S3: Reopen confirms before clobbering DIFFERENT in-progress editor content, then loads on confirm', async () => {
    const user = await openAdvanced()
    const promptField = screen.getByRole('textbox', { name: 'Advanced prompt' })
    await user.type(promptField, 'original tuned prompt')
    await user.click(screen.getByRole('button', { name: 'Run advanced branch' }))
    await screen.findByText('tuned answer')
    await user.click(await screen.findByRole('button', { name: /^Save/ }))
    // Replace the editor with DIFFERENT unsaved work.
    await user.clear(promptField)
    await user.type(promptField, 'unsaved different work')
    // Reopen now would clobber → it confirms instead of overwriting immediately.
    await user.click(screen.getByRole('button', { name: /Reopen/ }))
    expect(screen.getByRole('textbox', { name: 'Advanced prompt' }).value).toBe('unsaved different work')
    const live = document.querySelector('.adv-live')
    expect(live.textContent).toMatch(/replace your current prompt/i)
    // Confirming performs the reopen and loads the saved branch's prompt.
    await user.click(await screen.findByRole('button', { name: /Confirm reopen/ }))
    expect(screen.getByRole('textbox', { name: 'Advanced prompt' }).value).toBe('original tuned prompt')
  })
})

describe('Chat routing, navigation, and accessibility (T004.4)', () => {
  it('offers only the reviewed chat routes and rejects an undeclared route', async () => {
    await renderChat()
    const select = screen.getByRole('combobox', { name: 'Chat route' })
    const values = within(select).getAllByRole('option').map((option) => option.value)
    expect(values).toEqual(['route.fast', 'route.deep'])
    expect(values).not.toContain('route.smuggled') // closed set: an undeclared route never appears
  })

  it('preserves the transcript when the route changes', async () => {
    const user = await renderChat()
    await openConversation(user, [assistantTurn('turn_1', 'route-safe answer')])
    expect(await screen.findByText('route-safe answer')).toBeTruthy()
    await user.selectOptions(screen.getByRole('combobox', { name: 'Chat route' }), 'route.deep')
    expect(screen.getByText('route-safe answer')).toBeTruthy()
    expect(screen.getByRole('combobox', { name: 'Chat route' }).value).toBe('route.deep')
  })

  it('makes Chat first and default while Delivery stays reachable lower in the nav', async () => {
    const user = await renderChat()
    const chatNav = screen.getByRole('button', { name: 'Chat' })
    const deliveryNav = screen.getByRole('button', { name: 'Delivery' })
    expect(chatNav.getAttribute('aria-current')).toBe('page') // Chat selected by default
    expect(deliveryNav.getAttribute('aria-current')).toBeNull()
    // Chat precedes Delivery in document order (first in the nav).
    expect(chatNav.compareDocumentPosition(deliveryNav) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    await user.click(deliveryNav)
    expect(await screen.findByRole('heading', { name: 'Task TASK-1' })).toBeTruthy()
  })

  it('scopes the narrow-layout relax to the chat route and keeps actions reachable (responsive #4)', async () => {
    const user = await renderChat()
    // The chat route stamps `chat-active` on the shell: the ≤900px media query
    // relaxes the 980px min-width and unfixes the nav rail ONLY under this class,
    // so the composer and row actions are reachable (not occluded) when narrow.
    const shell = document.querySelector('.app-shell')
    expect(shell.classList.contains('chat-active')).toBe(true)
    expect(screen.getByRole('textbox', { name: 'Message composer' })).toBeTruthy()
    const del = screen.getByRole('button', { name: 'Delete Router planning' })
    expect(del).toBeTruthy()
    expect(del.style.display).not.toBe('none') // not removed from the layout
    // Delivery keeps its own 980px assumption: the class is dropped off-route.
    await user.click(screen.getByRole('button', { name: 'Delivery' }))
    await screen.findByRole('heading', { name: 'Task TASK-1' })
    expect(document.querySelector('.app-shell').classList.contains('chat-active')).toBe(false)
  })

  it('exposes keyboard focus and an announced live region for lifecycle states', async () => {
    const user = await renderChat()
    await openConversation(user, [])
    const composer = screen.getByRole('textbox', { name: 'Message composer' })
    composer.focus()
    expect(document.activeElement).toBe(composer) // composer is keyboard-focusable
    // The lifecycle live region (voice controls add their own status regions, so
    // target this one specifically rather than assuming a single status role).
    const live = document.querySelector('.chat-live')
    expect(live.getAttribute('role')).toBe('status')
    expect(live.getAttribute('aria-live')).toBe('polite')
    sendMessage.mockResolvedValueOnce({ text: 'done', terminal: 'completed', needsRefresh: false })
    await user.type(composer, 'announce this')
    await user.keyboard('{Enter}')
    expect(await screen.findByText('Response complete')).toBeTruthy() // lifecycle announced in the live region
  })
})

// --- Voice push-to-talk + read-aloud (chat-first-voice T005.2 / T005.3 / T005.4)
//
// The voice controls are exercised through the RENDERED chat component with the
// network client mocked, and the browser media APIs stubbed. The invariants they
// prove: push-to-talk yields an EDITABLE draft and sends NO turn until explicit
// submission; read-aloud playback NEVER mutates message state; both degrade
// truthfully (a 503 becomes textual error state) and never block textual chat.

class _FakeMediaRecorder {
  constructor(stream) { this.stream = stream; this.ondataavailable = null; this.onstop = null; _FakeMediaRecorder.last = this }
  // Accepts an optional timeslice arg (as the real API does when emitting periodic
  // chunks); the fake ignores it and lets a test drive interim chunks manually.
  start() {}
  // Simulate one interim chunk delivered DURING the hold (before release).
  emitInterimChunk() { this.ondataavailable?.({ data: new Blob([new Uint8Array([9, 9])]) }) }
  stop() {
    this.ondataavailable?.({ data: new Blob([new Uint8Array([1, 2, 3, 4])]) })
    this.onstop?.()
  }
}

class _FakeAudio {
  constructor(src) { this.src = src; this.currentTime = 0; this.onended = null; this.onerror = null; this.paused = false; this.playCount = 0; _FakeAudio.last = this; (_FakeAudio.instances ||= []).push(this) }
  play() { this.paused = false; this.playCount += 1; return Promise.resolve() }
  pause() { this.paused = true }
}

describe('Chat voice push-to-talk and read-aloud (chat-first-voice T005)', () => {
  let originalMediaDevices
  let originalMediaRecorder
  let originalAudio

  beforeEach(() => {
    transcribeVoice.mockResolvedValue({ draft: { text: 'dictated draft words', is_final: true, duration_ms: 900 } })
    speakMessage.mockResolvedValue({ audio_base64: 'QUJDRA==', audio_format: 'mp3', sample_rate: 24000 })
    originalMediaDevices = navigator.mediaDevices
    originalMediaRecorder = window.MediaRecorder
    originalAudio = window.Audio
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [{ stop: vi.fn() }] }) },
    })
    window.MediaRecorder = _FakeMediaRecorder
    global.MediaRecorder = _FakeMediaRecorder
    window.Audio = _FakeAudio
    global.Audio = _FakeAudio
    _FakeAudio.instances = []
    _FakeAudio.last = null
    _FakeMediaRecorder.last = null
  })

  afterEach(() => {
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: originalMediaDevices })
    window.MediaRecorder = originalMediaRecorder
    global.MediaRecorder = originalMediaRecorder
    window.Audio = originalAudio
    global.Audio = originalAudio
  })

  it('offers a keyboard-operable hold-to-talk control with an announced live region', async () => {
    const user = await renderChat()
    await openConversation(user, [])
    const ptt = screen.getByRole('button', { name: 'Hold to talk' })
    expect(ptt.tagName).toBe('BUTTON') // a real, focusable button
    expect(ptt.getAttribute('aria-pressed')).toBe('false')
    const status = ptt.parentElement.querySelector('.voice-ptt-status')
    expect(status.getAttribute('role')).toBe('status')
    expect(status.getAttribute('aria-live')).toBe('polite')
    expect(status.textContent).toMatch(/ready/i)
  })

  it('drops an EDITABLE transcript into the composer and sends NO turn until submit', async () => {
    const user = await renderChat()
    await openConversation(user, [])
    const ptt = screen.getByRole('button', { name: 'Hold to talk' })
    fireEvent.pointerDown(ptt)
    // Capture start awaits getUserMedia, so wait for the listening state.
    await waitFor(() => expect(ptt.getAttribute('aria-pressed')).toBe('true'))
    expect(screen.getByText(/release to review your transcript/)).toBeTruthy() // live-region announcement
    fireEvent.pointerUp(ptt)
    await waitFor(() => expect(transcribeVoice).toHaveBeenCalledTimes(1))
    // The draft becomes editable composer text the actor reviews.
    const composer = screen.getByRole('textbox', { name: 'Message composer' })
    await waitFor(() => expect(composer.value).toBe('dictated draft words'))
    await screen.findByText(/Transcript ready to review/)
    // CRUCIAL (T005.2 / T005.4): capturing audio created NO turn.
    expect(sendMessage).not.toHaveBeenCalled()
    // The actor can edit the draft, then explicitly submit -> now a turn is sent.
    await user.type(composer, ' edited')
    expect(composer.value).toBe('dictated draft words edited')
    sendMessage.mockResolvedValueOnce({ text: 'ok', terminal: 'completed', needsRefresh: false })
    await user.click(screen.getByRole('button', { name: 'Send message' }))
    await waitFor(() => expect(sendMessage).toHaveBeenCalledTimes(1))
    expect(sendMessage.mock.calls[0][0].prompt).toBe('dictated draft words edited')
  })

  it('keeps textual chat usable when microphone permission is denied', async () => {
    navigator.mediaDevices.getUserMedia.mockRejectedValueOnce(new Error('denied'))
    const user = await renderChat()
    await openConversation(user, [])
    fireEvent.pointerDown(screen.getByRole('button', { name: 'Hold to talk' }))
    await screen.findByText(/Microphone access was not granted/)
    // The textual composer is fully usable; no audio left the browser.
    const composer = screen.getByRole('textbox', { name: 'Message composer' })
    await user.type(composer, 'typed instead')
    expect(composer.value).toBe('typed instead')
    expect(transcribeVoice).not.toHaveBeenCalled()
    expect(sendMessage).not.toHaveBeenCalled()
  })

  it('degrades truthfully when the voice relay is not configured (503)', async () => {
    transcribeVoice.mockRejectedValueOnce(new Error('Voice input is not configured for this hub'))
    const user = await renderChat()
    await openConversation(user, [])
    const ptt = screen.getByRole('button', { name: 'Hold to talk' })
    fireEvent.pointerDown(ptt)
    await waitFor(() => expect(ptt.getAttribute('aria-pressed')).toBe('true'))
    fireEvent.pointerUp(ptt)
    await screen.findByText('Voice input is not configured for this hub')
    // The textual composer still works.
    const composer = screen.getByRole('textbox', { name: 'Message composer' })
    await user.type(composer, 'fallback text')
    expect(composer.value).toBe('fallback text')
    expect(sendMessage).not.toHaveBeenCalled()
  })

  it('reads a response aloud without mutating any message state', async () => {
    const user = await renderChat()
    await openConversation(user, [assistantTurn('t1', 'the assistant answer')])
    // The text is available before any audio.
    expect(screen.getByText('the assistant answer')).toBeTruthy()
    const play = screen.getByRole('button', { name: 'Read this response aloud' })
    await user.click(play)
    await waitFor(() => expect(speakMessage).toHaveBeenCalledTimes(1))
    expect(speakMessage.mock.calls[0][0]).toMatchObject({ messageRef: 't1', text: 'the assistant answer' })
    await screen.findByText('Playing audio')
    // The text stays available DURING audio, and no message-state mutation fired.
    expect(screen.getByText('the assistant answer')).toBeTruthy()
    expect(sendMessage).not.toHaveBeenCalled()
    expect(retryTurn).not.toHaveBeenCalled()
    expect(branchTurn).not.toHaveBeenCalled()
    expect(getConversation).toHaveBeenCalledTimes(1) // only the initial open; playback re-read nothing
  })

  it('pauses, stops, and replays playback without changing the conversation', async () => {
    const user = await renderChat()
    await openConversation(user, [assistantTurn('t1', 'answer to hear')])
    await user.click(screen.getByRole('button', { name: 'Read this response aloud' }))
    await screen.findByText('Playing audio')
    await user.click(screen.getByRole('button', { name: 'Pause audio' }))
    await screen.findByText('Audio paused')
    await user.click(screen.getByRole('button', { name: 'Stop audio' }))
    await screen.findByText('Audio stopped')
    // The text remains available after audio stops, and nothing mutated.
    expect(screen.getByText('answer to hear')).toBeTruthy()
    expect(sendMessage).not.toHaveBeenCalled()
    // Replay restarts the already-fetched audio without touching message state.
    await user.click(screen.getByRole('button', { name: 'Replay audio' }))
    await screen.findByText('Playing audio')
    expect(screen.getByText('answer to hear')).toBeTruthy()
    expect(retryTurn).not.toHaveBeenCalled()
  })

  it('surfaces a read-aloud failure truthfully without blocking the text', async () => {
    speakMessage.mockRejectedValueOnce(new Error('Read-aloud is not configured for this hub'))
    const user = await renderChat()
    await openConversation(user, [assistantTurn('t1', 'still readable text')])
    await user.click(screen.getByRole('button', { name: 'Read this response aloud' }))
    await screen.findByText('Read-aloud is not configured for this hub')
    expect(screen.getByText('still readable text')).toBeTruthy() // text always available
  })

  // MUST-2 (autoplay-per-saved-preference, driven through the served row shape).
  // The preference is loaded from the REAL /api/preferences payload via the
  // adapter, not a hand-built object the runtime never produces.
  it('autoplays a newly-arrived response when the saved voice_autoplay preference is ON', async () => {
    fetchPreferences.mockResolvedValue({ catalog: { settings: [] }, effective: [
      { setting_id: 'personal.voice_autoplay', scope: 'personal', value: true, source: 'stored' },
    ] })
    sendMessage.mockResolvedValue({ text: 'the autoplayed reply', terminal: 'completed', needsRefresh: false })
    const user = await renderChat()
    await openConversation(user, [])
    const composer = screen.getByRole('textbox', { name: 'Message composer' })
    await user.type(composer, 'hello')
    await user.click(screen.getByRole('button', { name: 'Send message' }))
    // The response arrives and is read aloud WITHOUT any Play click.
    await waitFor(() => expect(speakMessage).toHaveBeenCalledTimes(1))
    expect(speakMessage.mock.calls[0][0].text).toBe('the autoplayed reply')
    // Autoplay never sent a turn or mutated history.
    expect(retryTurn).not.toHaveBeenCalled()
  })

  it('does not autoplay when the saved voice_autoplay preference is OFF', async () => {
    // The default fetchPreferences payload has voice_autoplay OFF.
    sendMessage.mockResolvedValue({ text: 'a quiet reply', terminal: 'completed', needsRefresh: false })
    const user = await renderChat()
    await openConversation(user, [])
    await user.type(screen.getByRole('textbox', { name: 'Message composer' }), 'hello')
    await user.click(screen.getByRole('button', { name: 'Send message' }))
    await screen.findByText('a quiet reply')
    expect(speakMessage).not.toHaveBeenCalled() // no click, preference OFF -> silent
  })

  it('does not autoplay when the preferences surface is unavailable (503)', async () => {
    fetchPreferences.mockRejectedValue(new Error('The settings service is not configured for this hub'))
    sendMessage.mockResolvedValue({ text: 'still silent', terminal: 'completed', needsRefresh: false })
    const user = await renderChat()
    await openConversation(user, [])
    await user.type(screen.getByRole('textbox', { name: 'Message composer' }), 'hello')
    await user.click(screen.getByRole('button', { name: 'Send message' }))
    await screen.findByText('still silent')
    expect(speakMessage).not.toHaveBeenCalled() // 503 -> default OFF, never surprises
  })

  // MUST-3 (interim captions are genuinely rendered and flow into the editable
  // final). The interim is an isFinal:false relay call rendered as a live caption;
  // the final is isFinal:true and becomes the editable composer draft.
  it('renders a live interim caption during recording that becomes the editable final draft', async () => {
    transcribeVoice.mockImplementation(({ isFinal }) => Promise.resolve({
      draft: { text: isFinal ? 'final dictated sentence' : 'interim partial words', is_final: isFinal, duration_ms: 500 },
    }))
    const user = await renderChat()
    await openConversation(user, [])
    const ptt = screen.getByRole('button', { name: 'Hold to talk' })
    fireEvent.pointerDown(ptt)
    await waitFor(() => expect(ptt.getAttribute('aria-pressed')).toBe('true'))
    // A chunk delivered DURING the hold drives a live interim caption.
    await act(async () => { _FakeMediaRecorder.last.emitInterimChunk() })
    const caption = await screen.findByLabelText('Interim transcript')
    expect(caption.textContent).toBe('interim partial words')
    expect(caption.getAttribute('aria-live')).toBe('polite') // live-region caption
    // Release -> the FINAL transcript becomes the EDITABLE composer draft.
    fireEvent.pointerUp(ptt)
    const composer = screen.getByRole('textbox', { name: 'Message composer' })
    await waitFor(() => expect(composer.value).toBe('final dictated sentence'))
    // Both the interim (isFinal:false) and final (isFinal:true) relay paths ran.
    expect(transcribeVoice.mock.calls.some(([a]) => a.isFinal === false)).toBe(true)
    expect(transcribeVoice.mock.calls.some(([a]) => a.isFinal === true)).toBe(true)
    // Capturing audio created NO turn; the actor can still edit before submit.
    expect(sendMessage).not.toHaveBeenCalled()
    await user.type(composer, ' edited')
    expect(composer.value).toBe('final dictated sentence edited')
  })

  // SHOULD (playback is singleton: starting message B interrupts message A so the
  // two never overlap; interrupt mutates NO message/conversation state).
  it('interrupts an in-progress playback when another message starts (singleton audio)', async () => {
    const user = await renderChat()
    await openConversation(user, [assistantTurn('tA', 'answer A'), assistantTurn('tB', 'answer B')])
    const turnA = screen.getByText('answer A').closest('li')
    const turnB = screen.getByText('answer B').closest('li')
    // Play A.
    await user.click(within(turnA).getByRole('button', { name: 'Read this response aloud' }))
    await waitFor(() => expect(within(turnA).getByText('Playing audio')).toBeTruthy())
    const audioA = _FakeAudio.instances.at(-1)
    // Start B -> A is interrupted (its audio stopped) and only B plays.
    await user.click(within(turnB).getByRole('button', { name: 'Read this response aloud' }))
    await waitFor(() => expect(within(turnB).getByText('Playing audio')).toBeTruthy())
    const audioB = _FakeAudio.instances.at(-1)
    expect(audioA).not.toBe(audioB)
    expect(audioA.paused).toBe(true)  // A was interrupted (stopped)
    expect(audioB.paused).toBe(false) // only B plays
    // A returned to idle: its Play affordance is back and the region says idle.
    await waitFor(() => expect(within(turnA).getByRole('button', { name: 'Read this response aloud' })).toBeTruthy())
    expect(within(turnA).getByText('Audio idle')).toBeTruthy()
    // Interrupt changed NO message/conversation state.
    expect(sendMessage).not.toHaveBeenCalled()
    expect(retryTurn).not.toHaveBeenCalled()
    expect(branchTurn).not.toHaveBeenCalled()
    expect(getConversation).toHaveBeenCalledTimes(1) // only the initial open
  })
})

// --- Delivery explorer (plan-task-delivery T003) -----------------------------
//
// Fixtures mirror the EXACT wrapped shapes the merged delivery-projection router
// serves (workbench/api.py: {content}, {tasks}, {task}, {eligibility}) over
// task-reference.v1 / prd-content.v1 / delivery-eligibility.v1, so a drift from
// the served contract breaks these tests rather than passing vacuously.

function prdContent(prdId, title, overrides = {}) {
  return {
    content: {
      schema_version: 'workbench-prd-content/v1',
      provider: 'anvil-state',
      generated_at: overrides.generatedAt ?? '2026-07-19T00:00:00Z',
      prd: { prd_id: prdId, title, status: overrides.status ?? 'approved', revision: overrides.revision ?? 5 },
      content_trust: 'untrusted_task_data',
      content: { format: 'markdown', body: overrides.body ?? `# ${title}\n\nRedacted body.`, truncated: overrides.truncated ?? false, total_bytes: overrides.totalBytes ?? 42 },
      redaction: { status: 'redacted', ruleset: 'workbench-default-v1' },
    },
  }
}

function taskRef(prdId, taskId, title, overrides = {}) {
  const revision = overrides.revision ?? 4
  return {
    schema_version: 'workbench-task-reference/v1',
    ref: { prd_id: prdId, task_id: taskId, prd_revision: revision },
    scoped_id: `${prdId}:${taskId}`,
    run_label: `${prdId}:${taskId}@r${revision}`,
    source: { provider: 'anvil-state', snapshot_digest: `sha256:${'a'.repeat(64)}` },
    hierarchy: { prd_id: prdId, prd_title: overrides.prdTitle ?? 'PRD', feature_id: `${prdId}:F001` },
    summary: {
      content_trust: 'untrusted_task_data',
      title,
      status: overrides.status ?? 'ready',
      priority: overrides.priority ?? 'critical',
      latest_delivery_status: overrides.delivery ?? 'not_started',
      acceptance_criteria_count: overrides.ac ?? 3,
      verification_summary: overrides.vs ?? 'Three acceptance criteria; one automated verification defined.',
      depends_on: overrides.deps ?? [],
    },
  }
}

function eligibilityVerdict(prdId, taskId, overrides = {}) {
  return {
    eligibility: {
      schema_version: 'workbench-delivery-eligibility/v1',
      ref: { prd_id: prdId, task_id: taskId, prd_revision: 4 },
      scoped_id: `${prdId}:${taskId}`,
      eligible: overrides.eligible ?? false,
      state: overrides.state ?? 'blocked',
      reasons: overrides.reasons ?? [{ class: 'blocked', code: 'blocked.dependency_unmet', content_trust: 'untrusted_task_data', explanation: 'A dependency has not merged.' }],
    },
  }
}

// Two PRDs, each with its own T001, so duplicate task-number disambiguation is
// exercised against a real two-PRD fixture (T003 criterion 2 / R004).
function setupTwoPrds() {
  fetchPrdContent.mockImplementation((_projectId, prdId) =>
    Promise.resolve(prdId === 'release-alpha'
      ? prdContent('release-alpha', 'Chat-first Workbench')
      : prdContent('release-beta', 'State context and operations')))
  fetchPrdTasks.mockImplementation((_projectId, prdId) =>
    Promise.resolve(prdId === 'release-alpha'
      ? { tasks: [taskRef('release-alpha', 'T001', 'Add routed chat', { prdTitle: 'Chat-first Workbench', deps: [{ prd_id: 'release-alpha', task_id: 'T000', prd_revision: 4 }] })] }
      : { tasks: [taskRef('release-beta', 'T001', 'Persist retention', { prdTitle: 'State context and operations' })] }))
  fetchTaskEligibility.mockImplementation((_projectId, prdId, taskId) => Promise.resolve(eligibilityVerdict(prdId, taskId)))
}

async function renderExplorer() {
  const user = userEvent.setup()
  render(<App />)
  await user.click(await screen.findByRole('button', { name: 'Explorer' }))
  await screen.findByRole('heading', { name: 'Delivery explorer' })
  await screen.findByRole('button', { name: 'Select project Workbench qualification' })
  return user
}

async function openPrd(user, prdId) {
  // Input is named by its wrapping <label> visible text (a11y #10): no aria-label
  // override, so the accessible name IS the visible label "Open a PRD by id".
  const input = screen.getByRole('textbox', { name: 'Open a PRD by id' })
  await waitFor(() => expect(input.disabled).toBe(false))
  await user.type(input, prdId)
  await user.click(screen.getByRole('button', { name: 'Open PRD' }))
}

describe('Delivery explorer (plan-task-delivery T003)', () => {
  it('opens a PRD by id and renders its card + scoped tasks from the served projection shape', async () => {
    setupTwoPrds()
    const user = await renderExplorer()
    await openPrd(user, 'release-alpha')
    expect(fetchPrdContent).toHaveBeenCalledWith('project_1', 'release-alpha')
    expect(fetchPrdTasks).toHaveBeenCalledWith('project_1', 'release-alpha')
    const card = await screen.findByRole('article', { name: 'PRD Chat-first Workbench' })
    // PRD card: title, release, revision, State status, source freshness (criterion 3).
    expect(within(card).getByText('release-alpha')).toBeTruthy() // release id
    expect(within(card).getByText('r5')).toBeTruthy() // served prd.revision
    expect(within(card).getByText('approved')).toBeTruthy() // State status
    expect(within(card).getByText(/source as of 2026-07-19/)).toBeTruthy() // freshness
    // Progress summary is rendered and asserted (acceptance #8): one task, none at
    // accepted delivery. Deleting the progress <dd> now fails here, not silently.
    expect(within(card).getByText('0/1 tasks at accepted delivery')).toBeTruthy()
    // Title leads; the scoped id is muted secondary disclosure (criterion 1).
    expect(within(card).getByText('Add routed chat')).toBeTruthy()
    const scoped = within(card).getByText('release-alpha:T001')
    expect(scoped.tagName).toBe('SMALL')
  })

  it('keeps two PRDs T001 tasks distinguishable in navigation, detail state, and the URL (criterion 2)', async () => {
    setupTwoPrds()
    const user = await renderExplorer()
    await openPrd(user, 'release-alpha')
    await screen.findByRole('article', { name: 'PRD Chat-first Workbench' })
    await openPrd(user, 'release-beta')
    await screen.findByRole('article', { name: 'PRD State context and operations' })
    // Both T001 rows exist and are addressed by their SCOPED id, never a bare number.
    // The row's accessible name now comes from content (no aria-label override,
    // a11y #1), so it carries BOTH the scoped id and the title.
    expect(screen.getByRole('button', { name: /release-alpha:T001/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /release-beta:T001/ })).toBeTruthy()
    // Revert-detection (a11y #1): the name is title-first, so a regression back to
    // an id-only `aria-label` (which drops the title from the accessible name)
    // fails these two — the row would announce only "Open task <scoped id>".
    expect(screen.getByRole('button', { name: /Add routed chat/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Persist retention/ })).toBeTruthy()
    // Open alpha's T001: detail shows its distinct title, scoped state, and URL.
    await user.click(screen.getByRole('button', { name: /release-alpha:T001/ }))
    const detailA = await screen.findByRole('region', { name: /release-alpha:T001/ })
    expect(within(detailA).getByRole('heading', { name: 'Add routed chat' })).toBeTruthy()
    expect(detailA.getAttribute('data-scoped-id')).toBe('release-alpha:T001')
    expect(window.location.hash).toContain('release-alpha:T001')
    // Open beta's T001: a DIFFERENT title, scoped state, and URL — no collapse.
    await user.click(screen.getByRole('button', { name: /release-beta:T001/ }))
    const detailB = await screen.findByRole('region', { name: /release-beta:T001/ })
    expect(within(detailB).getByRole('heading', { name: 'Persist retention' })).toBeTruthy()
    expect(detailB.getAttribute('data-scoped-id')).toBe('release-beta:T001')
    expect(window.location.hash).toContain('release-beta:T001')
    expect(window.location.hash).not.toContain('release-alpha')
  })

  it('opens task detail with dependencies, acceptance criteria, verification, delivery status, and eligibility', async () => {
    setupTwoPrds()
    const user = await renderExplorer()
    await openPrd(user, 'release-alpha')
    await user.click(await screen.findByRole('button', { name: /release-alpha:T001/ }))
    const detail = await screen.findByRole('region', { name: /release-alpha:T001/ })
    expect(within(detail).getByText(/release-alpha:T000/)).toBeTruthy() // dependency, scoped
    expect(within(detail).getByText('3 acceptance criteria')).toBeTruthy()
    expect(within(detail).getByText(/automated verification defined/)).toBeTruthy()
    expect(within(detail).getByText('delivery: not_started')).toBeTruthy() // latest delivery status
    // Eligibility is fetched from its OWN endpoint and rendered, not fabricated.
    expect(fetchTaskEligibility).toHaveBeenCalledWith('project_1', 'release-alpha', 'T001')
    expect(await within(detail).findByText('blocked.dependency_unmet')).toBeTruthy()
  })

  it('reads a PRD content body through the redacted projection (screen-reader smoke: reading the PRD)', async () => {
    setupTwoPrds()
    const user = await renderExplorer()
    await openPrd(user, 'release-alpha')
    await user.click(await screen.findByRole('button', { name: 'Read PRD content' }))
    const reading = await screen.findByRole('region', { name: 'PRD Chat-first Workbench' })
    expect(within(reading).getByRole('heading', { name: 'Chat-first Workbench' })).toBeTruthy()
    expect(within(reading).getByText(/Redacted body/)).toBeTruthy()
  })

  it('filters the task list by title, scoped id, or status (screen-reader smoke: filtering tasks)', async () => {
    fetchPrdContent.mockResolvedValue(prdContent('release-alpha', 'Chat-first Workbench'))
    fetchPrdTasks.mockResolvedValue({ tasks: [
      taskRef('release-alpha', 'T001', 'Add routed chat', { status: 'ready' }),
      taskRef('release-alpha', 'T002', 'Persist retention', { status: 'blocked' }),
    ] })
    fetchTaskEligibility.mockResolvedValue(eligibilityVerdict('release-alpha', 'T001'))
    const user = await renderExplorer()
    await openPrd(user, 'release-alpha')
    expect(await screen.findByRole('button', { name: /release-alpha:T001/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /release-alpha:T002/ })).toBeTruthy()
    await user.type(screen.getByRole('searchbox', { name: 'Filter tasks' }), 'routed')
    // Real render assertion: the non-matching row is removed, not merely a call.
    expect(screen.getByRole('button', { name: /release-alpha:T001/ })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /release-alpha:T002/ })).toBeNull()
  })

  it('announces lifecycle in a live region and moves focus to detail, with Escape returning focus to the rail', async () => {
    setupTwoPrds()
    const user = await renderExplorer()
    const live = screen.getByRole('status')
    expect(live.getAttribute('aria-live')).toBe('polite')
    await openPrd(user, 'release-alpha')
    expect(await screen.findByText(/Loaded PRD Chat-first Workbench/)).toBeTruthy() // live region updated
    await user.click(await screen.findByRole('button', { name: /release-alpha:T001/ }))
    const heading = await screen.findByRole('heading', { name: 'Add routed chat' })
    await waitFor(() => expect(document.activeElement).toBe(heading)) // focus not dropped to <body>
    expect(screen.getByText(/Opened task release-alpha:T001/)).toBeTruthy() // announced
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('region', { name: /release-alpha:T001/ })).toBeNull())
    expect(document.activeElement).not.toBe(document.body) // focus returned to the rail
  })

  it('renders a truthful degraded state when the projection is not configured (no fabrication)', async () => {
    fetchPrdContent.mockRejectedValue(new Error('Delivery projection is not configured for this hub'))
    fetchPrdTasks.mockRejectedValue(new Error('Delivery projection is not configured for this hub'))
    const user = await renderExplorer()
    await openPrd(user, 'release-alpha')
    const card = await screen.findByRole('article', { name: 'PRD release-alpha' })
    expect(within(card).getByText('unavailable')).toBeTruthy()
    expect(within(card).getAllByText(/not configured/).length).toBeGreaterThan(0)
    // No fabricated task card is shown for the failed load.
    expect(screen.queryByText('Add routed chat')).toBeNull()
  })

  it('shows a truthful no-projects state instead of a fabricated project list', async () => {
    bootstrap.mockResolvedValueOnce({ projects: [] })
    const user = userEvent.setup()
    render(<App />)
    await user.click(await screen.findByRole('button', { name: 'Explorer' }))
    expect(await screen.findByText(/No projects yet/)).toBeTruthy()
    expect(screen.getByRole('textbox', { name: 'Open a PRD by id' }).disabled).toBe(true)
  })

  it('drops a stale out-of-order eligibility fetch so the open task shows its own verdict (#2)', async () => {
    // Reproduces the T001-vs-T001 hazard: alpha's eligibility resolves SLOWLY,
    // beta's resolves immediately. Open alpha then beta. Without the latest-wins
    // guard, alpha's late resolve overwrites beta's verdict under beta's pane.
    let resolveAlpha
    fetchPrdContent.mockImplementation((_p, prdId) => Promise.resolve(prdId === 'release-alpha'
      ? prdContent('release-alpha', 'Chat-first Workbench')
      : prdContent('release-beta', 'State context and operations')))
    fetchPrdTasks.mockImplementation((_p, prdId) => Promise.resolve(prdId === 'release-alpha'
      ? { tasks: [taskRef('release-alpha', 'T001', 'Add routed chat')] }
      : { tasks: [taskRef('release-beta', 'T001', 'Persist retention')] }))
    fetchTaskEligibility.mockImplementation((_p, prdId) => prdId === 'release-alpha'
      ? new Promise((resolve) => { resolveAlpha = () => resolve(eligibilityVerdict('release-alpha', 'T001', { reasons: [{ class: 'blocked', code: 'blocked.alpha_stale', explanation: 'Alpha resolved late.' }] })) })
      : Promise.resolve(eligibilityVerdict('release-beta', 'T001', { eligible: true, state: 'eligible', reasons: [{ class: 'info', code: 'info.ready', explanation: 'Beta is current.' }] })))
    const user = await renderExplorer()
    await openPrd(user, 'release-alpha')
    await screen.findByRole('article', { name: 'PRD Chat-first Workbench' })
    await openPrd(user, 'release-beta')
    await screen.findByRole('article', { name: 'PRD State context and operations' })
    // Open alpha (eligibility pending), then beta (eligibility resolves at once).
    await user.click(screen.getByRole('button', { name: /release-alpha:T001/ }))
    await user.click(screen.getByRole('button', { name: /release-beta:T001/ }))
    const detailB = await screen.findByRole('region', { name: /release-beta:T001/ })
    expect(await within(detailB).findByText('info.ready')).toBeTruthy() // beta's contract-valid eligible verdict
    // Now alpha's late fetch resolves. It must NOT repaint beta's open pane.
    await act(async () => { resolveAlpha(); await Promise.resolve() })
    expect(within(detailB).queryByText('blocked.alpha_stale')).toBeNull() // stale verdict dropped
    expect(within(detailB).getByText('info.ready')).toBeTruthy() // beta's own verdict stands
  })

  it('closes the detail with a visible Close button, returns focus to the rail, and clears the hash (#3/#4)', async () => {
    setupTwoPrds()
    const user = await renderExplorer()
    await openPrd(user, 'release-alpha')
    await user.click(await screen.findByRole('button', { name: /release-alpha:T001/ }))
    await screen.findByRole('region', { name: /release-alpha:T001/ })
    expect(window.location.hash).toContain('release-alpha:T001')
    const close = screen.getByRole('button', { name: 'Close' }) // a real, discoverable control
    await user.click(close)
    await waitFor(() => expect(screen.queryByRole('region', { name: /release-alpha:T001/ })).toBeNull())
    expect(document.activeElement).not.toBe(document.body) // focus not dropped
    expect(window.location.hash).not.toContain('release-alpha') // stale hash cleared, no longer lies
  })

  it('announces a truthful partial failure when the PRD loads but its tasks fail (#6)', async () => {
    fetchPrdContent.mockResolvedValue(prdContent('release-alpha', 'Chat-first Workbench'))
    fetchPrdTasks.mockRejectedValue(new Error('tasks projection unavailable'))
    const user = await renderExplorer()
    await openPrd(user, 'release-alpha')
    expect(await screen.findByText(/tasks failed to load/)).toBeTruthy()
    expect(screen.queryByText(/with 0 tasks/)).toBeNull() // never the misleading healthy-empty line
  })

  it('announces the filter result in the live region on filter change (#7)', async () => {
    fetchPrdContent.mockResolvedValue(prdContent('release-alpha', 'Chat-first Workbench'))
    fetchPrdTasks.mockResolvedValue({ tasks: [
      taskRef('release-alpha', 'T001', 'Add routed chat'),
      taskRef('release-alpha', 'T002', 'Persist retention'),
    ] })
    fetchTaskEligibility.mockResolvedValue(eligibilityVerdict('release-alpha', 'T001'))
    const user = await renderExplorer()
    await openPrd(user, 'release-alpha')
    await screen.findByRole('button', { name: /release-alpha:T001/ })
    const live = screen.getByRole('status')
    await user.type(screen.getByRole('searchbox', { name: 'Filter tasks' }), 'routed')
    await waitFor(() => expect(live.textContent).toMatch(/1 task\b/)) // one match announced
    await user.clear(screen.getByRole('searchbox', { name: 'Filter tasks' }))
    await user.type(screen.getByRole('searchbox', { name: 'Filter tasks' }), 'zzz')
    await waitFor(() => expect(live.textContent).toMatch(/No tasks match/)) // empty result announced
  })

  it('applies the narrow-posture shell class on the Explorer route and drops it off-route (#5)', async () => {
    setupTwoPrds()
    const user = await renderExplorer()
    const shell = document.querySelector('.app-shell')
    // The Explorer route stamps `explorer-active`: the ≤900px rule unfixes the nav
    // rail under this class so it cannot pin a narrow column or let the live region
    // paint over the rail buttons. The class is scoped, so it drops off-route.
    expect(shell.classList.contains('explorer-active')).toBe(true)
    expect(shell.classList.contains('chat-active')).toBe(false)
    await user.click(screen.getByRole('button', { name: 'Delivery' }))
    await screen.findByRole('heading', { name: 'Task TASK-1' })
    expect(document.querySelector('.app-shell').classList.contains('explorer-active')).toBe(false)
  })
})

// --- Deliver controls, setup sheet, and truthful blocked states (T006) --------
//
// The Deliver sheet reads the SAME merged delivery-projection GET shapes the
// explorer proves (fetchPrdTasks → {tasks}, fetchTaskEligibility → {eligibility},
// via the taskRef/eligibilityVerdict fixtures above), and starts through the REAL
// wired POST /api/workflows/{id}/start (startWorkflow → {workflow, run}). There is
// no separate Deliver route, so a drift in any of these served shapes breaks these
// tests rather than passing against a mock's own return.

describe('Deliver controls (plan-task-delivery T006)', () => {
  // The ONLY contract-valid eligible verdict (delivery-eligibility.v1): state is
  // 'eligible' (never 'ready'), the reason class is 'info' and its code is
  // 'info.ready'. `state`/`code`/`class` of 'ready' is a shape the server can
  // NEVER serve — the state enum is ["eligible","blocked","stale"], the code
  // enum ends at "info.ready", and workbench/contracts.py requires state to
  // equal the derived "eligible" — so it must not appear even in a fixture.
  const readyVerdict = (prdId, taskId) => eligibilityVerdict(prdId, taskId, {
    eligible: true, state: 'eligible',
    reasons: [{ class: 'info', code: 'info.ready', explanation: 'All preconditions pass.' }],
  })

  async function openSheet(user) {
    await user.click(screen.getByRole('button', { name: 'Deliver next task' }))
    return screen.getByRole('dialog', { name: 'Deliver next task' })
  }

  async function loadCandidate(user, prdId, { session = 'workflow_1' } = {}) {
    await user.selectOptions(screen.getByRole('combobox', { name: 'Deliver into session' }), session)
    await user.type(screen.getByRole('textbox', { name: 'PRD id' }), prdId)
    await user.click(screen.getByRole('button', { name: 'Load ranked candidate' }))
  }

  it('starts a ready candidate in one activation through the real workflow-start route and routes to the run', async () => {
    fetchPrdTasks.mockResolvedValue({ tasks: [taskRef('release-alpha', 'T001', 'Add routed chat', { status: 'ready' })] })
    fetchTaskEligibility.mockResolvedValue(readyVerdict('release-alpha', 'T001'))
    const user = userEvent.setup(); await renderLive()
    await openSheet(user)
    await loadCandidate(user, 'release-alpha')
    // The started run appears on reload so "routes to the run" is a rendered fact.
    const startedRun = { id: 'run_2', project_id: 'project_1', session_id: 'session_1', task_id: 'T001', model: 'planning', status: 'queued' }
    startWorkflow.mockResolvedValue({ workflow: {}, run: startedRun })
    bootstrap.mockResolvedValue({ ...fixture, runs: [startedRun, ...fixture.runs] })
    const deliver = await screen.findByRole('button', { name: /Deliver Add routed chat/ })
    await waitFor(() => expect(deliver.disabled).toBe(false))
    // The eligible verdict renders its state as 'eligible' (the ONLY
    // contract-servable eligible state). Reverting the fixture to the unservable
    // state:'ready' regresses the eligibility Status to render 'ready', leaving no
    // 'eligible' node, so this getByText throws and fails (MUST #1 regression
    // guard). The candidate's own status pill is a separate 'ready', so we assert
    // on the presence of the distinct 'eligible' verdict state rather than its
    // absence.
    const dialog = screen.getByRole('dialog', { name: 'Deliver next task' })
    expect(within(dialog).getByText('eligible')).toBeTruthy()
    await user.click(deliver)
    // The REAL wired route, called exactly once with the approved ids (plus the
    // dismissal AbortSignal threaded for the hung-start escape hatch, #4).
    expect(startWorkflow).toHaveBeenCalledTimes(1)
    expect(startWorkflow).toHaveBeenCalledWith('workflow_1', { task_id: 'T001', model: 'planning' }, { signal: expect.any(AbortSignal) })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Deliver next task' })).toBeNull())
    expect(await screen.findByRole('heading', { name: 'Task T001' })).toBeTruthy() // routed to the resulting run
  })

  it('previews exactly one State-ranked candidate and never a batch of tasks', async () => {
    fetchPrdTasks.mockResolvedValue({ tasks: [
      taskRef('release-alpha', 'T001', 'Add routed chat', { status: 'ready' }),
      taskRef('release-alpha', 'T002', 'Persist retention', { status: 'ready' }),
      taskRef('release-alpha', 'T003', 'Third candidate', { status: 'ready' }),
    ] })
    fetchTaskEligibility.mockResolvedValue(readyVerdict('release-alpha', 'T001'))
    const user = userEvent.setup(); await renderLive()
    await openSheet(user)
    await loadCandidate(user, 'release-alpha')
    expect(await screen.findByText('Add routed chat')).toBeTruthy() // the single ranked head
    expect(screen.queryByText('Persist retention')).toBeNull() // no batch/list
    expect(screen.queryByText('Third candidate')).toBeNull()
    // Eligibility is checked for the one candidate only, never the whole plan.
    expect(fetchTaskEligibility).toHaveBeenCalledWith('project_1', 'release-alpha', 'T001')
    expect(fetchTaskEligibility).not.toHaveBeenCalledWith('project_1', 'release-alpha', 'T002')
  })

  it('shows a blocked ranked head truthfully, disables Deliver with an accessible reason, and never skips it', async () => {
    fetchPrdTasks.mockResolvedValue({ tasks: [
      taskRef('release-alpha', 'T001', 'Blocked head', { status: 'blocked' }),
      taskRef('release-alpha', 'T002', 'Ready later task', { status: 'ready' }),
    ] })
    fetchTaskEligibility.mockResolvedValue(eligibilityVerdict('release-alpha', 'T001')) // eligible:false, blocked.dependency_unmet
    const user = userEvent.setup(); await renderLive()
    await openSheet(user)
    await loadCandidate(user, 'release-alpha')
    // The candidate is the blocked head, not the later ready row (no silent skip).
    expect(await screen.findByText('Blocked head')).toBeTruthy()
    expect(screen.queryByText('Ready later task')).toBeNull()
    const deliver = screen.getByRole('button', { name: /Deliver Blocked head/ })
    await waitFor(() => expect(deliver.disabled).toBe(true))
    expect(deliver.getAttribute('aria-disabled')).toBe('true')
    // The reason is real TEXT bound to the control (not colour alone).
    const describedById = deliver.getAttribute('aria-describedby')
    expect(describedById).toBeTruthy()
    const reason = document.getElementById(describedById)
    expect(reason.textContent).toMatch(/dependency has not merged/i)
    expect(reason.textContent).toMatch(/blocked\.dependency_unmet/)
    // A blocked Deliver never starts a run, even when clicked.
    await user.click(deliver)
    expect(startWorkflow).not.toHaveBeenCalled()
  })

  it('offers only approved session ids/titles and never asks for a path or a raw command', async () => {
    const user = userEvent.setup(); await renderLive()
    await openSheet(user)
    const sessionSelect = screen.getByRole('combobox', { name: 'Deliver into session' })
    const values = within(sessionSelect).getAllByRole('option').map((option) => option.value)
    expect(values.filter(Boolean)).toEqual(['workflow_1']) // only an approved workflow/session id
    // The one free-text field is the approved PRD id — no path, command, or route field.
    expect(screen.getByRole('textbox', { name: 'PRD id' })).toBeTruthy()
    expect(screen.queryByRole('textbox', { name: /path|command|model|route/i })).toBeNull()
    expect(screen.queryByLabelText(/path|command/i)).toBeNull()
  })

  it('fires the Deliver call exactly once even when activated twice quickly (no double-submit)', async () => {
    fetchPrdTasks.mockResolvedValue({ tasks: [taskRef('release-alpha', 'T001', 'Add routed chat', { status: 'ready' })] })
    fetchTaskEligibility.mockResolvedValue(readyVerdict('release-alpha', 'T001'))
    let resolveStart
    startWorkflow.mockImplementation(() => new Promise((resolve) => {
      resolveStart = () => resolve({ workflow: {}, run: { id: 'run_2', session_id: 'session_1', task_id: 'T001', model: 'planning', status: 'queued' } })
    }))
    const user = userEvent.setup(); await renderLive()
    await openSheet(user)
    await loadCandidate(user, 'release-alpha')
    const deliver = await screen.findByRole('button', { name: /Deliver Add routed chat/ })
    await waitFor(() => expect(deliver.disabled).toBe(false))
    await user.click(deliver)
    await user.click(deliver) // second activation while the first start is in flight
    expect(startWorkflow).toHaveBeenCalledTimes(1)
    resolveStart()
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Deliver next task' })).toBeNull())
  })

  it('opens a focus-managed setup sheet closable by a visible Close and by Escape', async () => {
    const user = userEvent.setup(); await renderLive()
    await user.click(screen.getByRole('button', { name: 'Deliver next task' }))
    const dialog = screen.getByRole('dialog', { name: 'Deliver next task' })
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true)) // focus moved into the sheet
    await user.keyboard('{Escape}') // Escape works even with focus inside the sheet
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Deliver next task' })).toBeNull())
    // Focus is restored to the specific opener, not merely "not body" (NOTE #8).
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Deliver next task' }))
    // Reopen and close via the discoverable Close control.
    await user.click(screen.getByRole('button', { name: 'Deliver next task' }))
    await user.click(screen.getByRole('button', { name: 'Close Deliver next task' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Deliver next task' })).toBeNull())
  })

  it('announces the blocked state in an updating live region', async () => {
    fetchPrdTasks.mockResolvedValue({ tasks: [taskRef('release-alpha', 'T001', 'Blocked head', { status: 'blocked' })] })
    fetchTaskEligibility.mockResolvedValue(eligibilityVerdict('release-alpha', 'T001'))
    const user = userEvent.setup(); await renderLive()
    const dialog = await openSheet(user)
    const live = within(dialog).getByRole('status')
    expect(live.getAttribute('aria-live')).toBe('polite')
    await loadCandidate(user, 'release-alpha')
    await waitFor(() => expect(live.textContent).toMatch(/blocked/i)) // start/blocked announced, not silent
  })

  // SHOULD #2: the displayed route + the start payload derive from the workflow's
  // entry agent step (workbench/store.py `step.get("model")`), the real source,
  // not a phantom top-level `workflow.model` that the served shape never carries.
  const withEntryModel = (model) => ({
    ...fixture,
    workflows: [{ ...fixture.workflows[0], definition: { entry: 'implement', steps: [{ id: 'implement', kind: 'agent', model, skills: [], next: [] }] } }],
  })

  it('derives the displayed route and the start payload from the workflow entry-step model (#2)', async () => {
    bootstrap.mockResolvedValue(withEntryModel('heavy-local'))
    fetchPrdTasks.mockResolvedValue({ tasks: [taskRef('release-alpha', 'T001', 'Add routed chat', { status: 'ready' })] })
    fetchTaskEligibility.mockResolvedValue(readyVerdict('release-alpha', 'T001'))
    const user = userEvent.setup(); await renderLive()
    await openSheet(user)
    await loadCandidate(user, 'release-alpha')
    // The candidate meta shows the entry-step route verbatim, not the default.
    expect(await screen.findByText(/route heavy-local/)).toBeTruthy()
    expect(screen.queryByText(/route heavy-local \(default\)/)).toBeNull()
    startWorkflow.mockResolvedValue({ workflow: {}, run: { id: 'run_2', session_id: 'session_1', task_id: 'T001', model: 'heavy-local', status: 'queued' } })
    bootstrap.mockResolvedValue({ ...withEntryModel('heavy-local'), runs: [{ id: 'run_2', session_id: 'session_1', task_id: 'T001', model: 'heavy-local', status: 'queued' }, ...fixture.runs] })
    const deliver = await screen.findByRole('button', { name: /Deliver Add routed chat/ })
    await waitFor(() => expect(deliver.disabled).toBe(false))
    await user.click(deliver)
    // The derived route feeds the REAL POST, proving the derivation is not cosmetic.
    expect(startWorkflow).toHaveBeenCalledWith('workflow_1', { task_id: 'T001', model: 'heavy-local' }, { signal: expect.any(AbortSignal) })
  })

  it('labels the route as the hub default when the definition pins no entry-step model (#2 fallback)', async () => {
    // The default fixture workflow carries no `definition`, so no route is derived.
    fetchPrdTasks.mockResolvedValue({ tasks: [taskRef('release-alpha', 'T001', 'Add routed chat', { status: 'ready' })] })
    fetchTaskEligibility.mockResolvedValue(readyVerdict('release-alpha', 'T001'))
    const user = userEvent.setup(); await renderLive()
    await openSheet(user)
    await loadCandidate(user, 'release-alpha')
    expect(await screen.findByText(/route planning \(default\)/)).toBeTruthy() // honest default, not a claimed derivation
  })

  it('drops a superseded out-of-order candidate load so the sheet shows the current PRD (#3 loadSeq)', async () => {
    // Reproduces the sheet's own T001-vs-T001 hazard: a slow alpha load then a fast
    // beta load. Without the loadSeq guard, alpha's late resolve repaints the sheet
    // with the superseded candidate under the current (beta) selection.
    let resolveAlphaTasks
    fetchPrdTasks.mockImplementation((_p, prdId) => prdId === 'release-alpha'
      ? new Promise((resolve) => { resolveAlphaTasks = () => resolve({ tasks: [taskRef('release-alpha', 'T001', 'Alpha head', { status: 'ready' })] }) })
      : Promise.resolve({ tasks: [taskRef('release-beta', 'T002', 'Beta head', { status: 'ready' })] }))
    fetchTaskEligibility.mockImplementation((_p, prdId, taskId) => Promise.resolve(readyVerdict(prdId, taskId)))
    const user = userEvent.setup(); await renderLive()
    await openSheet(user)
    await user.selectOptions(screen.getByRole('combobox', { name: 'Deliver into session' }), 'workflow_1')
    await user.type(screen.getByRole('textbox', { name: 'PRD id' }), 'release-alpha')
    await user.click(screen.getByRole('button', { name: 'Load ranked candidate' })) // slow alpha load starts
    await user.clear(screen.getByRole('textbox', { name: 'PRD id' }))
    await user.type(screen.getByRole('textbox', { name: 'PRD id' }), 'release-beta')
    await user.click(screen.getByRole('button', { name: 'Load ranked candidate' })) // fast beta load supersedes it
    expect(await screen.findByText('Beta head')).toBeTruthy()
    // Alpha's late resolve must be dropped by the loadSeq guard.
    await act(async () => { resolveAlphaTasks(); await Promise.resolve() })
    expect(screen.getByText('Beta head')).toBeTruthy()
    expect(screen.queryByText('Alpha head')).toBeNull() // superseded head never repaints the current sheet
  })

  it('lets the user dismiss a hung Deliver whose start never resolves, without trapping them (#4)', async () => {
    fetchPrdTasks.mockResolvedValue({ tasks: [taskRef('release-alpha', 'T001', 'Add routed chat', { status: 'ready' })] })
    fetchTaskEligibility.mockResolvedValue(readyVerdict('release-alpha', 'T001'))
    startWorkflow.mockImplementation(() => new Promise(() => {})) // hung bridge: the start never settles
    const user = userEvent.setup(); await renderLive()
    const dialog = await openSheet(user)
    await loadCandidate(user, 'release-alpha')
    const deliver = await screen.findByRole('button', { name: /Deliver Add routed chat/ })
    await waitFor(() => expect(deliver.disabled).toBe(false))
    await user.click(deliver)
    await waitFor(() => expect(deliver.disabled).toBe(true)) // busy: the start is in flight
    // Cancel stays enabled during busy so a hung POST is never a permanent trap.
    const cancel = within(dialog).getByRole('button', { name: 'Cancel' })
    expect(cancel.disabled).toBe(false)
    await user.click(cancel)
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Deliver next task' })).toBeNull())
  })

  it('does not close the sheet when Escape is pressed on the session select (native dropdown dismissal) (#5)', async () => {
    const user = userEvent.setup(); await renderLive()
    await openSheet(user)
    const select = screen.getByRole('combobox', { name: 'Deliver into session' })
    select.focus()
    await user.keyboard('{Escape}') // Escape here dismisses the select's own listbox, not the sheet
    expect(screen.getByRole('dialog', { name: 'Deliver next task' })).toBeTruthy() // the sheet's loaded state is preserved
  })

  it('binds an accessible reason to the disabled Deliver in every pre-load state (#6)', async () => {
    const user = userEvent.setup(); await renderLive()
    const dialog = await openSheet(user)
    // No session chosen yet: the disabled Deliver still names why, bound by id.
    const deliverNoSession = within(dialog).getByRole('button', { name: 'Deliver' })
    expect(deliverNoSession.disabled).toBe(true)
    const noSessionId = deliverNoSession.getAttribute('aria-describedby')
    expect(noSessionId).toBeTruthy()
    expect(document.getElementById(noSessionId).textContent).toMatch(/startable session/i)
    expect(document.getElementById(noSessionId).textContent).toMatch(/deliver\.no_session/)
    // Choose a session; the bound reason now names the missing candidate.
    await user.selectOptions(within(dialog).getByRole('combobox', { name: 'Deliver into session' }), 'workflow_1')
    const noCandidateId = within(dialog).getByRole('button', { name: 'Deliver' }).getAttribute('aria-describedby')
    expect(noCandidateId).toBeTruthy()
    expect(document.getElementById(noCandidateId).textContent).toMatch(/Load a PRD/i)
    expect(document.getElementById(noCandidateId).textContent).toMatch(/deliver\.no_candidate/)
  })

  it('traps Tab focus within the sheet so keyboard users cannot reach the occluded background (#7)', async () => {
    const user = userEvent.setup(); await renderLive()
    const dialog = await openSheet(user)
    // Tabbing repeatedly cycles within the dialog and never lands on a background control.
    for (let i = 0; i < 12; i += 1) {
      await user.tab()
      expect(dialog.contains(document.activeElement)).toBe(true)
    }
  })
})

// --- Configuration backup & transfer workflows (preferences-configuration T006.4) ---
describe('Configuration backup & transfer workflows', () => {
  async function openSettings() {
    const user = userEvent.setup()
    render(<App />)
    await screen.findByRole('button', { name: 'Settings' })
    await user.click(screen.getByRole('button', { name: 'Settings' }))
    await screen.findByRole('main', { name: 'Backup and transfer' })
    return user
  }

  it('states what is excluded from exports before any download control is shown', async () => {
    await openSettings()
    const region = screen.getByRole('note', { name: 'What an export excludes' })
    // The exclusion statement names secrets, paths, tokens, URLs, chat history.
    expect(within(region).getByText(/Secrets, credentials, and API tokens/)).toBeTruthy()
    expect(within(region).getByText(/Local filesystem paths/)).toBeTruthy()
    expect(within(region).getByText(/Chat history and raw prompts/)).toBeTruthy()
    // No Download control exists until an export is prepared — the exclusions are
    // stated first, and download appears only after Prepare export.
    expect(screen.queryByRole('link', { name: 'Download export' })).toBeNull()
  })

  it('exposes a download only after preparing a redacted, opaque-actor export', async () => {
    fetchConfigurationExport.mockResolvedValue({
      schema_version: 'workbench-configuration-export/v1',
      source: { scope: 'personal', actor_ref: 'actorref:0123456789abcdef', catalog_id: 'workbench.settings.initial' },
      settings: [{ setting_id: 'personal.time_format', scope: 'personal', value: 'format_12h' }],
    })
    const user = await openSettings()
    await user.click(screen.getByRole('button', { name: 'Prepare export' }))
    const download = await screen.findByRole('link', { name: 'Download export' })
    expect(download.getAttribute('download')).toBe('workbench-configuration.json')
    // The opaque actor reference is shown; no raw actor identity anywhere.
    expect(screen.getByText('actorref:0123456789abcdef')).toBeTruthy()
  })

  it('previews an import as typed categories and blocks apply until previewed', async () => {
    previewConfigurationImport.mockResolvedValue({ status: 'previewed', preview: {
      valid: true,
      creates: [{ setting_id: 'personal.time_format', scope: 'personal', value: 'format_12h' }],
      changes: [{ setting_id: 'personal.landing_surface', scope: 'personal', from: 'chat', to: 'delivery' }],
      resets: [{ setting_id: 'personal.chat_transcript_retention_days', scope: 'personal', from: 20, to_default: 30 }],
      skipped_read_only: [{ setting_id: 'policy.transcript_retention_max_days', reason: 'owner-managed; not importable' }],
      unavailable_references: [{ setting_id: 'personal.default_chat_route', ref_kind: 'route', value: 'route.ghost' }],
      repairable: [], no_ops: [], base_versions: { 'personal.time_format': 0 },
    } })
    const user = await openSettings()
    // Apply is disabled before any preview (no early apply).
    expect(screen.getByRole('button', { name: 'Apply import' }).disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Exported configuration'), { target: { value: '{"schema_version":"workbench-configuration-export/v1","settings":[]}' } })
    await user.click(screen.getByRole('button', { name: 'Preview import' }))
    const preview = await screen.findByRole('group', { name: 'Import preview' })
    // Every typed category is distinctly rendered.
    expect(within(preview).getByText('Will create', { exact: false })).toBeTruthy()
    expect(within(preview).getByText('Will change', { exact: false })).toBeTruthy()
    expect(within(preview).getByText('Will reset to default', { exact: false })).toBeTruthy()
    expect(within(preview).getByText('policy.transcript_retention_max_days')).toBeTruthy()
    expect(within(preview).getByText('personal.default_chat_route')).toBeTruthy()
    // A valid preview enables apply.
    expect(screen.getByRole('button', { name: 'Apply import' }).disabled).toBe(false)
  })

  it('cannot apply an invalid import and lists every repairable field', async () => {
    previewConfigurationImport.mockResolvedValue({ status: 'previewed', preview: {
      valid: false,
      creates: [], changes: [], resets: [], skipped_read_only: [], unavailable_references: [],
      repairable: [
        { setting_id: 'personal.time_format', reason: 'not one of its allowed values' },
        { setting_id: 'personal.chat_transcript_retention_days', reason: 'out of bounds' },
      ],
      no_ops: [], base_versions: {},
    } })
    const user = await openSettings()
    fireEvent.change(screen.getByLabelText('Exported configuration'), { target: { value: '{"schema_version":"workbench-configuration-export/v1","settings":[]}' } })
    await user.click(screen.getByRole('button', { name: 'Preview import' }))
    await screen.findByRole('group', { name: 'Import preview' })
    // Both repairable fields are named, and apply stays disabled.
    expect(screen.getByText(/not one of its allowed values/)).toBeTruthy()
    expect(screen.getByText(/out of bounds/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Apply import' }).disabled).toBe(true)
    expect(applyConfigurationImport).not.toHaveBeenCalled()
  })

  it('previews a scoped reset and reports scope + result after applying', async () => {
    previewConfigurationReset.mockResolvedValue({ status: 'previewed', preview: {
      scope: 'personal',
      changes: [{ setting_id: 'personal.landing_surface', scope: 'personal', from: 'dashboard', to_default: 'chat', expected_version: 1 }],
      base_versions: { 'personal.landing_surface': 1 },
    } })
    applyConfigurationReset.mockResolvedValue({ status: 'reset', result: { applied: [{ setting_id: 'personal.landing_surface', scope: 'personal', op: 'reset' }] }, applied: [{ setting_id: 'personal.landing_surface' }], appliedCount: 1, scope: 'personal' })
    const user = await openSettings()
    const resetPanel = screen.getByRole('region', { name: 'Reset preferences' })
    await user.click(within(resetPanel).getByRole('button', { name: 'Preview reset' }))
    const preview = await screen.findByRole('group', { name: 'Reset preview' })
    expect(within(preview).getByText('personal.landing_surface')).toBeTruthy()
    await user.click(within(resetPanel).getByRole('button', { name: 'Apply reset' }))
    // The result reports the scope and next remediation.
    expect(await within(resetPanel).findByText(/personal preferences were reset/i)).toBeTruthy()
    expect(applyConfigurationReset).toHaveBeenCalledWith(expect.objectContaining({ scope: 'personal', baseVersions: { 'personal.landing_surface': 1 } }))
  })
})
