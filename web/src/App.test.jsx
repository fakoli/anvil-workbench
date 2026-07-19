import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { approve, bootstrap, createProject, createSession, startWorkflow } from './api'

vi.mock('./api', () => ({
  approve: vi.fn(),
  bootstrap: vi.fn(),
  createProject: vi.fn(),
  createSession: vi.fn(),
  startWorkflow: vi.fn(),
  voiceSocketUrl: vi.fn(() => 'ws://workbench.test/api/sessions/session_test/voice/realtime'),
}))

describe('Workbench delivery cockpit', () => {
  beforeEach(() => {
    bootstrap.mockRejectedValue(new Error('offline test hub'))
    approve.mockResolvedValue({ status: 'approved' })
    createProject.mockResolvedValue({
      id: 'project_checkout',
      name: 'Checkout reliability',
      state_root: '.anvil',
      bridge_id: null,
    })
    createSession.mockResolvedValue({
      session: { id: 'session_checkout', project_id: 'project_anvil', title: 'Checkout fix', worktree_id: 'checkout', status: 'active' },
      workflow: { id: 'workflow_checkout', project_id: 'project_anvil', session_id: 'session_checkout', version: 1, status: 'draft', cursor: [] },
    })
    startWorkflow.mockResolvedValue({
      workflow: { id: 'workflow_7f2a', project_id: 'project_anvil', session_id: 'session_7f2a', version: 1, status: 'running', cursor: ['implement'] },
      run: { id: 'run_checkout', project_id: 'project_anvil', session_id: 'session_7f2a', task_id: 'task_checkout', model: 'planning', status: 'queued' },
    })
  })

  it('opens each workspace with a purpose-specific review surface', async () => {
    const user = userEvent.setup()
    render(<App />)

    const views = [
      ['Delivery', 'Responses compatibility'],
      ['Sessions', 'Concurrent sessions'],
      ['Runs', 'Runs'],
      ['Routes', 'Routes'],
      ['Approvals', 'Approvals'],
      ['Evidence', 'Evidence'],
      ['Sandbox', 'Model sandbox'],
    ]

    for (const [control, heading] of views) {
      await user.click(screen.getByRole('button', { name: control }))
      expect(screen.getByRole('heading', { name: heading })).toBeTruthy()
    }
  })

  it('creates and starts isolated sessions through the harness API', async () => {
    const user = userEvent.setup()
    render(<App />)

    await user.click(screen.getByRole('button', { name: 'Sessions' }))
    await user.click(screen.getByRole('button', { name: 'New concurrent session' }))
    await user.type(screen.getByRole('textbox', { name: 'Session title' }), 'Checkout fix')
    await user.clear(screen.getByRole('textbox', { name: 'Configured worktree id' }))
    await user.type(screen.getByRole('textbox', { name: 'Configured worktree id' }), 'checkout')
    await user.click(screen.getByRole('button', { name: 'Create session' }))

    expect(createSession).toHaveBeenCalledWith({ project_id: 'project_anvil', title: 'Checkout fix', worktree_id: 'checkout' })
    expect(screen.getByText(/Created Checkout fix/)).toBeTruthy()

    await user.click(screen.getByRole('button', { name: 'Start delivery Checkout fix' }))
    await user.type(screen.getByRole('textbox', { name: 'State task id' }), 'task_checkout')
    await user.click(screen.getByRole('button', { name: 'Start bridge delivery' }))

    expect(startWorkflow).toHaveBeenCalledWith('workflow_checkout', { task_id: 'task_checkout', model: 'planning' })
    expect(screen.getByText(/Started task_checkout through the local bridge/)).toBeTruthy()
  })

  it('does not offer microphone access until the private voice relay is configured', () => {
    render(<App />)
    const voice = screen.getByRole('button', { name: 'Voice not configured' })
    expect(voice.disabled).toBe(true)
    expect(screen.getByText(/Configure a private Anvil Voice Realtime endpoint/)).toBeTruthy()
  })

  it('uses live hub data instead of displaying seeded route or evidence artifacts', async () => {
    const user = userEvent.setup()
    bootstrap.mockResolvedValueOnce({
      projects: [{ id: 'project_live', name: 'Live qualification', state_root: '.anvil', bridge_id: 'bridge_live' }],
      runs: [{ id: 'run_live', task_id: 'T-live', model: 'fast-local', status: 'reconciliation' }],
      approvals: [],
      router_configured: true,
    })
    render(<App />)

    expect(await screen.findByText('Implement T-live')).toBeTruthy()
    expect(screen.getByText('Awaiting redacted evidence from an evidenced bridge run.')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Routes' }))
    expect(screen.getAllByText('fast-local')).toHaveLength(2)
    await user.click(screen.getByRole('button', { name: 'Evidence' }))
    expect(screen.getByText('No evidence packet yet')).toBeTruthy()
  })

  it('creates a project record through the hub API without exposing bridge credentials', async () => {
    const user = userEvent.setup()
    render(<App />)

    await user.click(screen.getByRole('button', { name: /New delivery/ }))
    expect(screen.getByRole('dialog', { name: 'New delivery' })).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Close New delivery' }))
    expect(screen.queryByRole('dialog', { name: 'New delivery' })).toBeNull()

    await user.click(screen.getByRole('button', { name: /New delivery/ }))
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog', { name: 'New delivery' })).toBeNull()

    await user.click(screen.getByRole('button', { name: /New delivery/ }))
    await user.type(screen.getByRole('textbox', { name: 'Project name' }), 'Checkout reliability')
    await user.click(screen.getByRole('button', { name: 'Create project' }))

    expect(createProject).toHaveBeenCalledWith({ name: 'Checkout reliability', state_root: '.anvil' })
    expect(screen.getByText(/Created Checkout reliability/)).toBeTruthy()
    expect(screen.queryByRole('dialog', { name: 'New delivery' })).toBeNull()
  })

  it('opens and closes every utility control', async () => {
    const user = userEvent.setup()
    render(<App />)

    await user.click(screen.getByRole('button', { name: 'Operator menu' }))
    expect(screen.getByRole('region', { name: 'Operator menu' })).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Close menu' }))
    expect(screen.queryByRole('region', { name: 'Operator menu' })).toBeNull()

    await user.click(screen.getByRole('button', { name: 'Help' }))
    expect(screen.getByRole('dialog', { name: 'Delivery cockpit help' })).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Close Delivery cockpit help' }))
    expect(screen.queryByRole('dialog', { name: 'Delivery cockpit help' })).toBeNull()

    await user.click(screen.getByRole('button', { name: 'Notifications' }))
    expect(screen.getByRole('region', { name: 'Notifications' })).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Mark all read' }))
    expect(screen.getByText('All caught up.')).toBeTruthy()
  })

  it('reveals task and correlation context, then routes evidence review to its workspace', async () => {
    const user = userEvent.setup()
    render(<App />)

    await user.click(screen.getByRole('button', { name: /Task task_48/ }))
    expect(screen.getByLabelText('Task task_48 details')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: /Task task_48/ }))
    expect(screen.queryByLabelText('Task task_48 details')).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Show correlation trace' }))
    expect(screen.getByText('workbench_run_id: run_7f2a')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Show correlation trace' }))
    expect(screen.queryByText('workbench_run_id: run_7f2a')).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Show correlation trace' }))
    await user.click(screen.getByRole('button', { name: 'View all' }))
    expect(screen.getByRole('heading', { name: 'Evidence' })).toBeTruthy()
  })

  it('adds a delivery direction locally and lets the operator dismiss the queued notice', async () => {
    const user = userEvent.setup()
    render(<App />)

    await user.type(
      screen.getByRole('textbox', { name: 'Add direction to this delivery' }),
      'Validate evidence before the PR gate.',
    )
    await user.click(screen.getByRole('button', { name: 'Send delivery direction' }))

    expect(screen.getByText('Validate evidence before the PR gate.')).toBeTruthy()
    expect(screen.getByRole('status').textContent).toContain('Queued a delivery note: Validate evidence before the PR gate.')
    await user.click(screen.getByRole('button', { name: 'Dismiss notification' }))
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('keeps PR creation behind an explicit approval action', async () => {
    const user = userEvent.setup()
    render(<App />)

    await user.click(screen.getByRole('button', { name: 'Authorize action' }))

    expect(approve).toHaveBeenCalledWith('approval_7c4e')
    expect(screen.getByRole('heading', { name: 'PR action released' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Authorized' })).toBeTruthy()
  })

  it('does not display authorization when the hub rejects the approval', async () => {
    const user = userEvent.setup()
    approve.mockRejectedValueOnce(new Error('hub rejected approval'))
    render(<App />)

    await user.click(screen.getByRole('button', { name: 'Authorize action' }))

    expect(screen.getByRole('heading', { name: 'Create GitHub PR' })).toBeTruthy()
    expect(screen.getByRole('status').textContent).toContain('Approval was not recorded.')
    expect(screen.getByRole('button', { name: 'Authorize action' })).toBeTruthy()
  })
})
