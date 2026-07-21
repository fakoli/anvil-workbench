import { act, render, screen, waitFor, within } from '@testing-library/react'
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
} from './api'

vi.mock('./api', () => ({
  addDirective: vi.fn(), approve: vi.fn(), bootstrap: vi.fn(), createProject: vi.fn(), createSession: vi.fn(),
  fetchRoutes: vi.fn(), probeSkills: vi.fn(), runSandbox: vi.fn(), searchEvidence: vi.fn(), startWorkflow: vi.fn(), taskLineage: vi.fn(),
  voiceSocketUrl: vi.fn(() => 'ws://workbench.test/api/sessions/session_1/voice/realtime'),
  archiveConversation: vi.fn(), branchTurn: vi.fn(), createConversation: vi.fn(), deleteConversation: vi.fn(),
  fetchChatRoutes: vi.fn(), getConversation: vi.fn(), listConversations: vi.fn(), renameConversation: vi.fn(),
  retryTurn: vi.fn(), searchConversations: vi.fn(), sendMessage: vi.fn(), unarchiveConversation: vi.fn(),
  fetchPrdContent: vi.fn(), fetchPrdTasks: vi.fn(), fetchTaskReference: vi.fn(), fetchTaskEligibility: vi.fn(),
}))

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
  sendMessage.mockResolvedValue({ text: '', terminal: 'completed', needsRefresh: false })
  retryTurn.mockResolvedValue(assistantTurn('turn_retry', 'second answer', 'complete', 'retry'))
  branchTurn.mockResolvedValue(assistantTurn('turn_branch', 'branched answer', 'complete', 'branch'))
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

  it('preserves the transcript when Advanced mode opens and shows a truthful unavailable state', async () => {
    const user = await renderChat()
    await openConversation(user, [assistantTurn('turn_1', 'kept answer')])
    expect(await screen.findByText('kept answer')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Toggle Advanced mode' }))
    expect(screen.getByRole('region', { name: 'Advanced controls' })).toBeTruthy()
    expect(screen.getByText(/not configured in this build/)).toBeTruthy()
    expect(screen.getByText('kept answer')).toBeTruthy() // transcript unchanged by Advanced mode
    await user.click(screen.getByRole('button', { name: 'Toggle Advanced mode' }))
    expect(screen.getByText('kept answer')).toBeTruthy()
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
    const live = screen.getByRole('status')
    expect(live.getAttribute('aria-live')).toBe('polite')
    sendMessage.mockResolvedValueOnce({ text: 'done', terminal: 'completed', needsRefresh: false })
    await user.type(composer, 'announce this')
    await user.keyboard('{Enter}')
    expect(await screen.findByText('Response complete')).toBeTruthy() // lifecycle announced in the live region
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
      : Promise.resolve(eligibilityVerdict('release-beta', 'T001', { eligible: true, state: 'ready', reasons: [{ class: 'ready', code: 'ready.beta_current', explanation: 'Beta is current.' }] })))
    const user = await renderExplorer()
    await openPrd(user, 'release-alpha')
    await screen.findByRole('article', { name: 'PRD Chat-first Workbench' })
    await openPrd(user, 'release-beta')
    await screen.findByRole('article', { name: 'PRD State context and operations' })
    // Open alpha (eligibility pending), then beta (eligibility resolves at once).
    await user.click(screen.getByRole('button', { name: /release-alpha:T001/ }))
    await user.click(screen.getByRole('button', { name: /release-beta:T001/ }))
    const detailB = await screen.findByRole('region', { name: /release-beta:T001/ })
    expect(await within(detailB).findByText('ready.beta_current')).toBeTruthy()
    // Now alpha's late fetch resolves. It must NOT repaint beta's open pane.
    await act(async () => { resolveAlpha(); await Promise.resolve() })
    expect(within(detailB).queryByText('blocked.alpha_stale')).toBeNull() // stale verdict dropped
    expect(within(detailB).getByText('ready.beta_current')).toBeTruthy() // beta's own verdict stands
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
  const readyVerdict = (prdId, taskId) => eligibilityVerdict(prdId, taskId, {
    eligible: true, state: 'ready',
    reasons: [{ class: 'ready', code: 'ready.all_clear', explanation: 'All preconditions pass.' }],
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
    await user.click(deliver)
    // The REAL wired route, called exactly once with the approved ids.
    expect(startWorkflow).toHaveBeenCalledTimes(1)
    expect(startWorkflow).toHaveBeenCalledWith('workflow_1', { task_id: 'T001', model: 'planning' })
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
    expect(document.activeElement).not.toBe(document.body) // focus returned to the opener, not dropped
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
})
