import { afterEach, describe, expect, it, vi } from 'vitest'
import { createSession } from './api'

describe('Workbench browser API', () => {
  afterEach(() => vi.restoreAllMocks())

  it('sends selected bridge-published skills when creating a session', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({ session: { id: 'session_1' } }),
    })

    await createSession({
      project_id: 'project_1', title: 'Evidence review', worktree_id: 'checkout-a',
      skills: ['anvil:review'],
    })

    expect(fetch).toHaveBeenCalledWith('/api/sessions', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({
        project_id: 'project_1', title: 'Evidence review', worktree_id: 'checkout-a',
        workflow_definition: undefined, skills: ['anvil:review'],
      }),
    }))
  })
})
