import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import {
  addDirective, approve, bootstrap, createProject, createSession, fetchRoutes, probeSkills,
  runSandbox, searchEvidence, startWorkflow, taskLineage,
  archiveConversation, branchTurn, createConversation, deleteConversation, fetchChatRoutes,
  getConversation, listConversations, renameConversation, retryTurn, searchConversations,
  sendMessage, unarchiveConversation,
} from './api'

vi.mock('./api', () => ({
  addDirective: vi.fn(), approve: vi.fn(), bootstrap: vi.fn(), createProject: vi.fn(), createSession: vi.fn(),
  fetchRoutes: vi.fn(), probeSkills: vi.fn(), runSandbox: vi.fn(), searchEvidence: vi.fn(), startWorkflow: vi.fn(), taskLineage: vi.fn(),
  voiceSocketUrl: vi.fn(() => 'ws://workbench.test/api/sessions/session_1/voice/realtime'),
  archiveConversation: vi.fn(), branchTurn: vi.fn(), createConversation: vi.fn(), deleteConversation: vi.fn(),
  fetchChatRoutes: vi.fn(), getConversation: vi.fn(), listConversations: vi.fn(), renameConversation: vi.fn(),
  retryTurn: vi.fn(), searchConversations: vi.fn(), sendMessage: vi.fn(), unarchiveConversation: vi.fn(),
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
  bootstrap.mockResolvedValue(fixture)
  approve.mockResolvedValue({ status: 'approved' })
  addDirective.mockResolvedValue({ id: 'event_2', session_id: 'session_1', sequence: 4, kind: 'operator.directive', data: { content: 'Check route evidence.' } })
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
    const user = userEvent.setup(); await renderLive()
    await user.type(screen.getByRole('textbox', { name: 'Add direction to this delivery' }), 'Check route evidence.')
    await user.click(screen.getByRole('button', { name: 'Send delivery direction' }))
    expect(addDirective).toHaveBeenCalledWith('session_1', 'Check route evidence.')
    expect((await screen.findByRole('status')).textContent).toContain('included only in the next bridge work packet')
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
    await user.click(screen.getByRole('button', { name: 'Delete Router planning' }))
    expect(deleteConversation).toHaveBeenCalledWith('conv_active')
    // Rename last: it changes the row's accessible name, so it cannot precede the
    // by-title archive/delete lookups above.
    await user.click(screen.getByRole('button', { name: 'Rename Router planning' }))
    const renameField = screen.getByRole('textbox', { name: 'Rename Router planning' })
    await user.clear(renameField); await user.type(renameField, 'Router evidence')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(renameConversation).toHaveBeenCalledWith('conv_active', 'Router evidence')
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
    expect(searchConversations).toHaveBeenCalledWith('invoices', expect.objectContaining({ includeArchived: false }))
    expect(await screen.findByText('Weekly sync')).toBeTruthy()
  })

  it('keeps active and archived conversations visibly distinct', async () => {
    const user = await renderChat()
    expect(screen.queryByText('Old triage')).toBeNull() // archived hidden by default
    await user.click(screen.getByRole('checkbox', { name: 'Show archived conversations' }))
    expect(listConversations).toHaveBeenLastCalledWith({ includeArchived: true })
    const archivedSection = await screen.findByRole('region', { name: 'Archived conversations' })
    expect(within(archivedSection).getByText('Old triage')).toBeTruthy()
    const activeSection = screen.getByRole('region', { name: 'Active conversations' })
    expect(within(activeSection).queryByText('Old triage')).toBeNull()
  })

  it('shows bound project, PRD, and task titles with ids as secondary disclosure', async () => {
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
    expect(retryTurn).toHaveBeenCalledWith('conv_active', 'turn_1', expect.objectContaining({ role: 'assistant' }))
    expect(await screen.findByText('second answer')).toBeTruthy()
    expect(screen.getByText('first answer')).toBeTruthy() // the original turn is preserved, not rewritten
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
