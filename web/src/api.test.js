import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  archiveConversation,
  branchTurn,
  createConversation,
  createSession,
  deleteConversation,
  fetchChatRoutes,
  getConversation,
  listConversations,
  renameConversation,
  retryTurn,
  searchConversations,
  sendMessage,
  unarchiveConversation,
} from './api'
import { describeConversation, selectChatRoute } from './chat-api'

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

// A JSON success response the fetch spy can return.
function ok(json) {
  return { ok: true, json: async () => json }
}

// A newline-delimited-JSON stream body from a list of relay frames, exposed
// through the same `getReader()` contract `sendMessage` consumes.
function ndjsonBody(frames) {
  const chunks = frames.map((frame) => new TextEncoder().encode(`${JSON.stringify(frame)}\n`))
  let index = 0
  return {
    ok: true,
    body: {
      getReader() {
        return {
          read: async () =>
            index < chunks.length
              ? { done: false, value: chunks[index++] }
              : { done: true, value: undefined },
          releaseLock() {},
        }
      },
    },
  }
}

describe('conversation API client (T004.1)', () => {
  afterEach(() => vi.restoreAllMocks())

  it('creates a conversation with only the supplied title in the body', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(ok({ id: 'conv_1', title: 'Route planning' }))
    const record = await createConversation({ title: 'Route planning' })
    expect(fetch).toHaveBeenCalledWith('/api/conversations', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ title: 'Route planning' }),
    }))
    expect(record.id).toBe('conv_1')
  })

  it('lists active conversations by default and opts archived in explicitly', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(ok({ conversations: [] }))
    await listConversations()
    expect(fetch).toHaveBeenLastCalledWith('/api/conversations')
    await listConversations({ includeArchived: true })
    expect(fetch).toHaveBeenLastCalledWith('/api/conversations?include_archived=true')
  })

  it('searches by query string over the conversations surface', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(ok({ conversations: [] }))
    await searchConversations('router evidence')
    expect(fetch).toHaveBeenLastCalledWith('/api/conversations/search?query=router+evidence')
  })

  it('encodes the conversation id on get, rename, archive, unarchive, and delete', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(ok({ id: 'a/b' }))
    await getConversation('a/b')
    expect(fetch).toHaveBeenLastCalledWith('/api/conversations/a%2Fb')
    await renameConversation('a/b', 'Renamed')
    expect(fetch).toHaveBeenLastCalledWith('/api/conversations/a%2Fb/rename', expect.objectContaining({
      method: 'POST', body: JSON.stringify({ title: 'Renamed' }),
    }))
    await archiveConversation('a/b')
    expect(fetch).toHaveBeenLastCalledWith('/api/conversations/a%2Fb/archive', { method: 'POST' })
    await unarchiveConversation('a/b')
    expect(fetch).toHaveBeenLastCalledWith('/api/conversations/a%2Fb/unarchive', { method: 'POST' })
    await deleteConversation('a/b')
    expect(fetch).toHaveBeenLastCalledWith('/api/conversations/a%2Fb/delete', expect.objectContaining({
      method: 'POST', body: JSON.stringify({ mode: 'purge_content_keep_tombstone' }),
    }))
  })

  it('routes retry and branch through the successor endpoints, not a rewrite', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(ok({ id: 'turn_2' }))
    await retryTurn('conv_1', 'turn_1', { role: 'assistant', status: 'complete' })
    expect(fetch).toHaveBeenLastCalledWith('/api/conversations/conv_1/turns/turn_1/retry', expect.any(Object))
    await branchTurn('conv_1', 'turn_1', { role: 'assistant', status: 'complete' })
    expect(fetch).toHaveBeenLastCalledWith('/api/conversations/conv_1/turns/turn_1/branch', expect.any(Object))
  })

  it('surfaces a distinct failure message when a management request is rejected', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({ ok: false, status: 409, json: async () => ({}) })
    await expect(renameConversation('conv_1', 'x')).rejects.toThrow('Conversation could not be renamed')
    await expect(deleteConversation('conv_1')).rejects.toThrow('Conversation could not be deleted')
  })

  it('streams incremental deltas and settles a completed terminal', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(ndjsonBody([
      { seq: 1, kind: 'delta', text: 'Hel' },
      { seq: 2, kind: 'delta', text: 'lo' },
      { seq: 3, kind: 'terminal', outcome: 'completed' },
    ]))
    const seen = []
    const state = await sendMessage({
      conversationId: 'conv_1', routeId: 'route.fast', prompt: 'hi',
      onState: (s) => seen.push(s.text),
    })
    expect(seen).toEqual(['Hel', 'Hello', 'Hello']) // incremental, not one final blob
    expect(state.text).toBe('Hello')
    expect(state.terminal).toBe('completed')
  })

  it('ignores a replayed frame so the streamed response is not duplicated', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(ndjsonBody([
      { seq: 1, kind: 'delta', text: 'A' },
      { seq: 2, kind: 'delta', text: 'B' },
      { seq: 2, kind: 'delta', text: 'B' }, // replay of seq 2
    ]))
    const state = await sendMessage({ conversationId: 'conv_1', routeId: 'route.fast', prompt: 'hi' })
    expect(state.text).toBe('AB')
  })

  it('flags a dropped frame for a snapshot reconnect instead of applying it', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(ndjsonBody([
      { seq: 1, kind: 'delta', text: 'A' },
      { seq: 3, kind: 'delta', text: 'C' }, // seq 2 dropped
    ]))
    const state = await sendMessage({ conversationId: 'conv_1', routeId: 'route.fast', prompt: 'hi' })
    expect(state.needsRefresh).toBe(true)
    expect(state.text).toBe('A') // the gapped frame was not applied
  })

  it('settles cancelled without a later completion when the stream aborts mid-flight', async () => {
    let index = 0
    const chunks = [new TextEncoder().encode(`${JSON.stringify({ seq: 1, kind: 'delta', text: 'partial' })}\n`)]
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      body: {
        getReader() {
          return {
            read: async () => {
              if (index < chunks.length) return { done: false, value: chunks[index++] }
              throw new DOMException('The user aborted a request.', 'AbortError')
            },
            releaseLock() {},
          }
        },
      },
    })
    const controller = new AbortController()
    const state = await sendMessage({
      conversationId: 'conv_1', routeId: 'route.fast', prompt: 'hi', signal: controller.signal,
    })
    expect(state.text).toBe('partial') // partial text preserved
    expect(state.terminal).toBe('cancelled') // never 'completed'
  })

  it('throws a distinct failure when the stream cannot be started (network error)', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('Failed to fetch'))
    await expect(
      sendMessage({ conversationId: 'conv_1', routeId: 'route.fast', prompt: 'hi' }),
    ).rejects.toThrow('The response stream could not be started')
  })

  it('lists only the reviewed chat routes', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(ok({ routes: [{ route_id: 'route.fast', display_name: 'Fast' }] }))
    const value = await fetchChatRoutes()
    expect(value.routes).toHaveLength(1)
    expect(value.routes[0].route_id).toBe('route.fast')
  })
})

describe('conversation display helpers (T004.1)', () => {
  it('prioritizes a readable title and state over the opaque id', () => {
    const described = describeConversation({ id: 'conv_9', title: '  ', status: 'archived', updated_at: 't' })
    expect(described.title).toBe('Untitled conversation') // never the raw id as a heading
    expect(described.state).toBe('archived')
    expect(described.id).toBe('conv_9') // still available, but as secondary disclosure
  })

  it('marks an active conversation and reflects the ephemeral badge from policy', () => {
    const described = describeConversation({ id: 'conv_1', title: 'Live', status: 'active', ephemeral: true })
    expect(described.state).toBe('active')
    expect(described.ephemeral).toBe(true)
  })

  it('refuses a route id outside the reviewed allowlist (closed set)', () => {
    const routes = [{ route_id: 'route.fast' }, { route_id: 'route.deep' }]
    expect(selectChatRoute(routes, 'route.fast').route_id).toBe('route.fast')
    expect(() => selectChatRoute(routes, 'route.smuggled')).toThrow('reviewed allowlist')
  })
})
