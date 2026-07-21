import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  archiveConversation,
  branchTurn,
  createConversation,
  createSession,
  deleteConversation,
  fetchChatRoutes,
  fetchPrdContent,
  fetchPrdTasks,
  fetchTaskEligibility,
  fetchTaskReference,
  getConversation,
  listConversations,
  renameConversation,
  retryTurn,
  searchConversations,
  sendMessage,
  unarchiveConversation,
} from './api'
import {
  fetchPreferences, fetchPreference, writePreference, resetPreference,
  previewPolicyOperation, policyApprovalBinding,
} from './api'
import { describeConversation, selectChatRoute, successorTurnBody, toTurnContent } from './chat-api'

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

  // Correctness #1 (PROVEN 422): the real pydantic ContentBlockInput forbids
  // extra fields and requires `kind`. A server-loaded block carries
  // `content_trust` (turn_json) → 422 extra_forbidden; a locally-streamed block
  // is `{text}` with no `kind` → 422 missing. successorTurnBody must emit
  // `{kind:'text', text}` ONLY, for both origins.
  it('normalizes successor content to {kind:"text", text} only, from either turn origin', () => {
    expect(toTurnContent([{ kind: 'text', text: 'loaded', content_trust: 'untrusted_task_data' }]))
      .toEqual([{ kind: 'text', text: 'loaded' }]) // content_trust stripped
    expect(toTurnContent([{ text: 'streamed' }]))
      .toEqual([{ kind: 'text', text: 'streamed' }]) // missing kind supplied
    expect(toTurnContent(undefined)).toEqual([])
  })

  it('posts the exact retry/branch successor body the server accepts (revert-detecting)', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(ok({ id: 'turn_2' }))
    // A turn exactly as `getConversation` returns it: blocks carry content_trust.
    const loadedTurn = { id: 'turn_1', content: [{ kind: 'text', text: 'first answer', content_trust: 'untrusted_task_data' }] }

    // Retry appends a sibling ASSISTANT regeneration.
    await retryTurn('conv_1', 'turn_1', successorTurnBody(loadedTurn, { role: 'assistant' }))
    expect(fetch).toHaveBeenLastCalledWith('/api/conversations/conv_1/turns/turn_1/retry', expect.objectContaining({
      method: 'POST',
      // Exact body — a revert to `content: turn.content` re-introduces
      // content_trust and fails this equality (422 on the real server).
      body: JSON.stringify({ role: 'assistant', status: 'complete', mode: 'ordinary', content: [{ kind: 'text', text: 'first answer' }] }),
    }))

    // Branch opens a follow-up USER turn (test_conversation_api.py:149-151).
    await branchTurn('conv_1', 'turn_1', successorTurnBody(loadedTurn, { role: 'user', mode: 'advanced' }))
    expect(fetch).toHaveBeenLastCalledWith('/api/conversations/conv_1/turns/turn_1/branch', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ role: 'user', status: 'complete', mode: 'advanced', content: [{ kind: 'text', text: 'first answer' }] }),
    }))
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

describe('delivery-projection explorer client (T003)', () => {
  afterEach(() => vi.restoreAllMocks())

  it('reads PRD content, tasks, one task, and eligibility from the scoped GET paths', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(ok({ ok: true }))
    await fetchPrdContent('project_1', 'release-alpha')
    expect(fetch).toHaveBeenLastCalledWith('/api/projects/project_1/prds/release-alpha/content')
    await fetchPrdTasks('project_1', 'release-alpha')
    expect(fetch).toHaveBeenLastCalledWith('/api/projects/project_1/prds/release-alpha/tasks')
    await fetchTaskReference('project_1', 'release-alpha', 'T001')
    expect(fetch).toHaveBeenLastCalledWith('/api/projects/project_1/prds/release-alpha/tasks/T001')
    await fetchTaskEligibility('project_1', 'release-alpha', 'T001')
    expect(fetch).toHaveBeenLastCalledWith('/api/projects/project_1/prds/release-alpha/tasks/T001/eligibility')
  })

  it('returns the exact wrapped shape the router serves', async () => {
    const served = { content: { prd: { prd_id: 'release-alpha', title: 'PRD' } } }
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(ok(served))
    await expect(fetchPrdContent('project_1', 'release-alpha')).resolves.toEqual(served)
  })

  it('encodes ids that need escaping in the scoped path', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(ok({}))
    await fetchPrdContent('a/b', 'p/q')
    expect(fetch).toHaveBeenLastCalledWith('/api/projects/a%2Fb/prds/p%2Fq/content')
  })

  it('surfaces a 503 as a distinct not-configured degraded error (fail-closed surface)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({ ok: false, status: 503, json: async () => ({}) })
    await expect(fetchPrdContent('project_1', 'release-alpha')).rejects.toThrow('not configured')
    await expect(fetchTaskEligibility('project_1', 'release-alpha', 'T001')).rejects.toThrow('not configured')
  })

  it('throws a distinct non-leaking failure for any other non-2xx', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({ ok: false, status: 404, json: async () => ({}) })
    await expect(fetchPrdTasks('project_1', 'release-alpha')).rejects.toThrow('PRD tasks are unavailable')
    await expect(fetchTaskReference('project_1', 'release-alpha', 'T001')).rejects.toThrow('Task reference is unavailable')
  })
})

// A non-2xx JSON response the fetch spy can return.
function err(statusCode, json = {}) {
  return { ok: false, status: statusCode, json: async () => json }
}

// The parsed JSON body of the most recent fetch call (POST/PUT).
function lastBody(fetch) {
  return JSON.parse(fetch.mock.calls.at(-1)[1].body)
}

describe('settings / preferences API client (T005.1)', () => {
  afterEach(() => vi.restoreAllMocks())

  it('reads the settings actor-view + effective values from the scoped GET path', async () => {
    const served = { catalog: { schema_version: 'v1', settings: [] }, effective: [] }
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(ok(served))
    await expect(fetchPreferences('project_1')).resolves.toEqual(served)
    expect(fetch).toHaveBeenLastCalledWith('/api/preferences?project_id=project_1')
    await fetchPreferences()
    expect(fetch).toHaveBeenLastCalledWith('/api/preferences')
  })

  it('surfaces an unconfigured settings service (503) as a distinct not-configured error', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(err(503))
    await expect(fetchPreferences('project_1')).rejects.toThrow('not configured')
  })

  it('reads one preference record with scope + project id, and 404 stays a distinct not-set error', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(ok({ preference: { setting_id: 'personal.time_format', value: 'format_24h', write_version: 2 } }))
    await fetchPreference('personal.time_format', { scope: 'personal' })
    expect(fetch).toHaveBeenLastCalledWith('/api/preferences/personal.time_format?scope=personal')
    await fetchPreference('project.delivery_route', { scope: 'project', projectId: 'project_1' })
    expect(fetch).toHaveBeenLastCalledWith('/api/preferences/project.delivery_route?scope=project&project_id=project_1')
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(err(404, { detail: 'unknown preference' }))
    await expect(fetchPreference('personal.time_format', { scope: 'personal' })).rejects.toThrow('not set')
  })

  it('writes a preference with a CLOSED body — no actor, credential, or provider key can ride along', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(ok({ preference: { setting_id: 'personal.time_format', value: 'format_12h', write_version: 3 } }))
    const result = await writePreference('personal.time_format', { scope: 'personal', value: 'format_12h', expectedVersion: 2 })
    expect(fetch).toHaveBeenLastCalledWith('/api/preferences/personal.time_format', expect.objectContaining({ method: 'PUT' }))
    // Exactly the declared field set the server's extra="forbid" model accepts.
    expect(Object.keys(lastBody(fetch)).sort()).toEqual(['expected_version', 'scope', 'value'])
    expect(result).toEqual({ status: 'saved', preference: { setting_id: 'personal.time_format', value: 'format_12h', write_version: 3 } })
  })

  it('keeps stale (409), invalid (422), unknown (404), and unavailable (503/network) write results DISTINGUISHABLE', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(err(409, { detail: { detail: 'reload required before writing', reload_required: true, current_version: 7 } }))
    expect(await writePreference('personal.time_format', { scope: 'personal', value: 'x', expectedVersion: 2 }))
      .toEqual({ status: 'stale', reloadRequired: true, currentVersion: 7, message: expect.any(String) })

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(err(422, { detail: 'value not allowed' }))
    expect((await writePreference('personal.time_format', { scope: 'personal', value: 'x', expectedVersion: 2 })).status).toBe('invalid')

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(err(404, { detail: 'unknown preference' }))
    expect((await writePreference('nope', { scope: 'personal', value: 'x', expectedVersion: 0 })).status).toBe('unknown')

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(err(503))
    expect((await writePreference('personal.time_format', { scope: 'personal', value: 'x', expectedVersion: 2 })).status).toBe('unavailable')

    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('Failed to fetch'))
    expect((await writePreference('personal.time_format', { scope: 'personal', value: 'x', expectedVersion: 2 })).status).toBe('unavailable')
  })

  it('resets ONLY the named scope with a valueless closed body', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(ok({ effective: { setting_id: 'project.delivery_route', scope: 'project', value: 'route.delivery-heavy', source: 'default' } }))
    const result = await resetPreference('project.delivery_route', { scope: 'project', expectedVersion: 4, projectId: 'project_1' })
    expect(fetch).toHaveBeenLastCalledWith('/api/preferences/project.delivery_route/reset', expect.objectContaining({ method: 'POST' }))
    expect(Object.keys(lastBody(fetch)).sort()).toEqual(['expected_version', 'project_id', 'scope'])
    expect(lastBody(fetch)).not.toHaveProperty('value')
    expect(result.status).toBe('reset')
  })

  it('previews an approval-gated change through the typed policy-operation spine with a closed body', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(ok({ preview: { digest: 'sha256:' + 'a'.repeat(64), operation: { operation: 'preference.set', setting_id: 'project.delivery_route', scope: 'project', value: 'route.delivery-heavy' }, effect_summary: 'set project.delivery_route' }, target: 'anvil-preferences', hub_local: true, requires_approval: true }))
    const result = await previewPolicyOperation({ settingId: 'project.delivery_route', scope: 'project', operation: 'preference.set', opVersion: 1, value: 'route.delivery-heavy', projectId: 'project_1' })
    expect(fetch).toHaveBeenLastCalledWith('/api/policy-operations/preview', expect.objectContaining({ method: 'POST' }))
    expect(Object.keys(lastBody(fetch)).sort()).toEqual(['op_version', 'operation', 'project_id', 'scope', 'setting_id', 'value'])
    expect(result.status).toBe('previewed')
    // 422 stays distinct from a preview.
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(err(422, { detail: 'bad value' }))
    expect((await previewPolicyOperation({ settingId: 'x', scope: 'project', operation: 'preference.set', opVersion: 1, value: 'y' })).status).toBe('invalid')
  })

  it('exposes the approval binding (action, payload_hash) with no secret field', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(ok({ action: 'preference.set', payload_hash: 'sha256:' + 'b'.repeat(64), actor: 'operator', scope_key: 'project_1' }))
    const result = await policyApprovalBinding({ settingId: 'project.delivery_route', scope: 'project', operation: 'preference.set', opVersion: 1, value: 'route.delivery-heavy', projectId: 'project_1' })
    expect(result.status).toBe('bound')
    expect(result.binding).not.toHaveProperty('token')
    expect(result.binding.payload_hash).toMatch(/^sha256:/)
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
