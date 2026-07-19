import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import {
  addDirective, approve, bootstrap, createProject, createSession, fetchRoutes, probeSkills,
  runSandbox, searchEvidence, startWorkflow, taskLineage,
} from './api'

vi.mock('./api', () => ({
  addDirective: vi.fn(), approve: vi.fn(), bootstrap: vi.fn(), createProject: vi.fn(), createSession: vi.fn(),
  fetchRoutes: vi.fn(), probeSkills: vi.fn(), runSandbox: vi.fn(), searchEvidence: vi.fn(), startWorkflow: vi.fn(), taskLineage: vi.fn(),
  voiceSocketUrl: vi.fn(() => 'ws://workbench.test/api/sessions/session_1/voice/realtime'),
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

async function renderLive() {
  render(<App />)
  await screen.findByRole('heading', { name: 'Task TASK-1' })
}

describe('Workbench delivery cockpit', () => {
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
  })

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
    bootstrap.mockResolvedValueOnce({ projects: [] }); render(<App />)
    expect(await screen.findByRole('heading', { name: 'Start a private delivery' })).toBeTruthy(); expect(screen.getByText(/no synthetic delivery/)).toBeTruthy()
  })
})
