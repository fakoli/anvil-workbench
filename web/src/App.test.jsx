import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { approve, bootstrap } from './api'

vi.mock('./api', () => ({
  approve: vi.fn(),
  bootstrap: vi.fn(),
}))

describe('Workbench delivery cockpit', () => {
  beforeEach(() => {
    bootstrap.mockRejectedValue(new Error('offline test hub'))
    approve.mockResolvedValue({ status: 'approved' })
  })

  it('switches to the Runs workspace while keeping the PR action approval-gated', async () => {
    const user = userEvent.setup()
    render(<App />)

    await user.click(screen.getByRole('button', { name: 'Runs' }))

    expect(screen.getByRole('heading', { name: 'Runs' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Authorize action →' })).toBeTruthy()
  })

  it('adds a delivery direction locally and makes its queued state visible', async () => {
    const user = userEvent.setup()
    render(<App />)

    await user.type(
      screen.getByRole('textbox', { name: 'Add direction to this delivery' }),
      'Validate evidence before the PR gate.',
    )
    await user.click(screen.getByRole('button', { name: 'Send delivery direction' }))

    expect(screen.getByText('Validate evidence before the PR gate.')).toBeTruthy()
    expect(screen.getByText('Queued a delivery note: Validate evidence before the PR gate.')).toBeTruthy()
  })

  it('keeps PR creation behind an explicit approval action', async () => {
    const user = userEvent.setup()
    render(<App />)

    await user.click(screen.getByRole('button', { name: 'Authorize action →' }))

    expect(approve).toHaveBeenCalledWith('approval_7c4e')
    expect(screen.getByRole('heading', { name: 'PR action released' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Authorized →' })).toBeTruthy()
  })
})
