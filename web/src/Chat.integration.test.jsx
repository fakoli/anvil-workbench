import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'

// --- Chat-first integration fixture (chat-first-voice T004) -------------------
//
// This file proves the chat-first acceptance criteria AT THE WIRED PATH. Unlike
// App.test.jsx (which mocks the whole `./api` module), it renders the REAL <App />
// and mocks only `global.fetch` — the exact transport boundary the real client
// uses. That means the production `web/src/api.js` client AND the production
// `web/src/chat-api.js` `reduceStreamState` reducer run for real:
//
//   * `sendMessage` really POSTs `/api/conversations/{id}/send`, reads the
//     streaming body via `response.body.getReader()`, splits newline-delimited
//     JSON, and folds each frame through `reduceStreamState`. So a "streamed
//     delta" here is a real relay frame parsed by real code, not a mocked
//     `onState` call.
//   * Every fixture frame is shaped EXACTLY as `workbench/chat_stream.py`
//     `RelayEvent` serializes: `{seq, kind:'delta', text}` and
//     `{seq, kind:'terminal', outcome}`. That relay emits ONLY `delta`/`terminal`
//     (chat_stream.py:157-197 / the T010 NOTE at :165-172), and there is no
//     `/send` route wired server-side yet (conversation_api.py has no `/send`),
//     so this mock is the faithful FE-ahead-of-BE boundary the client targets.
//   * `needsRefresh` (the dropped-frame reconnect) is produced by a REAL seq gap
//     through `reduceStreamState` (chat-api.js:17-34,45-60), never a hand-set
//     flag — so the reconnect test exercises the genuine recovery path.
//   * Conversation records match `conversation_json` and turns match `turn_json`
//     (workbench/conversation_api.py:202-273) field-for-field.
//   * Cancellation aborts the real fetch: the mock reader rejects the pending
//     `read()` with an `AbortError` when the caller-owned signal fires, exactly
//     as an aborted fetch body does, so the client's cancel path runs for real.

const encoder = new TextEncoder()

// Exactly one `RelayEvent` (chat_stream.py:157-197): a sequenced delta or terminal.
function deltaFrame(text, seq) { return { seq, kind: 'delta', text } }
function terminalFrame(outcome, seq) { return { seq, kind: 'terminal', outcome } }

// A controlled streaming body reader. `read()` resolves one pushed chunk at a
// time and otherwise pends; when the caller-owned AbortSignal fires it rejects the
// pending read with a DOM-shaped AbortError, mirroring how a real aborted fetch
// body tears down — which is what drives the client's `settleCancelled` path.
function makeStream(signal) {
  const queue = []
  let waiter = null
  let aborted = false
  const abortError = () => { const error = new Error('The operation was aborted'); error.name = 'AbortError'; return error }
  const settle = () => { if (waiter && queue.length) { const w = waiter; waiter = null; w.resolve(queue.shift()) } }
  if (signal) {
    if (signal.aborted) aborted = true
    signal.addEventListener('abort', () => { aborted = true; if (waiter) { const w = waiter; waiter = null; w.reject(abortError()) } })
  }
  return {
    reader: {
      read() {
        if (aborted) return Promise.reject(abortError())
        if (queue.length) return Promise.resolve(queue.shift())
        return new Promise((resolve, reject) => { waiter = { resolve, reject } })
      },
      releaseLock() {}, cancel() {},
    },
    push(frame) { queue.push({ done: false, value: encoder.encode(JSON.stringify(frame) + '\n') }); settle() },
    end() { queue.push({ done: true, value: undefined }); settle() },
  }
}

function streamingResponse(reader) {
  return { ok: true, status: 200, body: { getReader: () => reader }, json: async () => ({}) }
}
function jsonResponse(data, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => data, body: null }
}

// `conversation_json` shape (conversation_api.py:202-231) — title/state primary,
// the canonical id present but never the primary label.
function conv(id, title, overrides = {}) {
  return {
    id, status: 'active', title, pinned: false, tags: [], folder: null, ephemeral: false,
    retention: { policy_id: 'workbench.default', transcript_text: 'retained_redacted', voice_transcript_text: 'retained_redacted', delete_after: null },
    deletion: null, created_at: '2026-07-21T00:00:00Z', updated_at: '2026-07-21T00:00:00Z', ...overrides,
  }
}

// `turn_json` shape (conversation_api.py:234-273).
function turn(id, conversationId, role, text, { status = 'complete', kind = 'initial', mode = 'ordinary' } = {}) {
  return {
    id, conversation_id: conversationId, role, mode, status,
    committed: status === 'complete', interrupted: false, terminal: null, content_purged: false,
    lineage: { parent_turn_id: null, sibling_index: 0, kind },
    content: [{ kind: 'text', text, content_trust: 'trusted_actor_input' }],
    voice_events: [], created_at: '2026-07-21T00:00:00Z', completed_at: '2026-07-21T00:00:00Z',
  }
}

// `deletion_json` shape (conversation_api.py:276-285).
function deletionJson(id) {
  return { conversation_id: id, status: 'deleted', deletion_mode: 'purge_content_keep_tombstone', turn_count: 0, committed_turn_count: 0, interrupted_turn_count: 0 }
}

const BOOTSTRAP = {
  actor: 'operator',
  projects: [{ id: 'project_1', name: 'Live qualification', state_root: '.anvil', bridge_id: 'bridge_1' }],
  sessions: [{ id: 'session_1', project_id: 'project_1', title: 'Router qualification', worktree_id: 'default', status: 'active' }],
  workflows: [{ id: 'workflow_1', project_id: 'project_1', session_id: 'session_1', version: 1, status: 'draft', cursor: [] }],
  runs: [{ id: 'run_1', project_id: 'project_1', session_id: 'session_1', task_id: 'TASK-1', model: 'heavy-local', status: 'evidenced' }],
  approvals: [], skills: [], directives: [], audit: [],
  router_configured: true,
  sandbox: { available: false, models: [] },
  voice: { available: false, transport: 'not_configured', retains_transcripts: false },
}

const CHAT_ROUTES = { routes: [{ route_id: 'route.fast', display_name: 'Fast local' }, { route_id: 'route.deep', display_name: 'Deep local' }] }
// Served `/api/preferences` payload (autoplay OFF) — the resolver's effective-row shape.
const PREFERENCES = { catalog: { settings: [] }, effective: [{ setting_id: 'personal.voice_autoplay', scope: 'personal', value: false, source: 'default' }] }

// A stateful in-memory fake of the hub over the REAL serializer shapes. Its
// mutations (create/rename/archive/delete) change what a subsequent wired
// list/search returns, so the rail is asserted against genuine server state — not
// a local optimistic guess.
function makeServer({ conversations = [], turns = {} } = {}) {
  const state = { conversations: conversations.map((c) => ({ ...c })), turns: { ...turns }, calls: [], latestStream: null, searchResults: null }

  async function fetchImpl(url, options = {}) {
    const method = (options.method || 'GET').toUpperCase()
    const parsed = new URL(url, 'http://localhost')
    const path = parsed.pathname
    state.calls.push({ method, path, url: String(url) })

    if (path === '/api/bootstrap') return jsonResponse(BOOTSTRAP)
    if (path === '/api/chat/routes') return jsonResponse(CHAT_ROUTES)
    if (path === '/api/preferences') return jsonResponse(PREFERENCES)
    // Advanced surfaces are fail-closed (503) — the shipped not-wired state.
    if (path.startsWith('/api/chat/advanced')) return jsonResponse({ detail: 'not configured' }, 503)
    // Safe empty delivery shapes so navigating to Delivery never crashes the mock.
    if (path === '/api/routes') return jsonResponse({ routes: [] })
    if (path.startsWith('/api/evidence')) return jsonResponse({ results: [] })

    if (path === '/api/conversations') {
      if (method === 'POST') {
        const body = options.body ? JSON.parse(options.body) : {}
        const record = conv(`conv_new_${state.conversations.length + 1}`, body.title || 'Untitled conversation')
        state.conversations = [record, ...state.conversations]
        return jsonResponse(record, 201)
      }
      const includeArchived = parsed.searchParams.get('include_archived') === 'true'
      return jsonResponse({ conversations: state.conversations.filter((c) => includeArchived || c.status !== 'archived') })
    }
    if (path === '/api/conversations/search') {
      if (state.searchResults) return jsonResponse({ conversations: state.searchResults })
      const query = (parsed.searchParams.get('query') || '').toLowerCase()
      const includeArchived = parsed.searchParams.get('include_archived') === 'true'
      return jsonResponse({ conversations: state.conversations.filter((c) => (includeArchived || c.status !== 'archived') && c.title.toLowerCase().includes(query)) })
    }

    const match = path.match(/^\/api\/conversations\/([^/]+)(\/.*)?$/)
    if (match) {
      const id = decodeURIComponent(match[1])
      const sub = match[2] || ''
      const record = state.conversations.find((c) => c.id === id) || conv(id, id)
      if (sub === '' && method === 'GET') return jsonResponse({ conversation: record, turns: state.turns[id] || [] })
      if (sub === '/rename' && method === 'POST') {
        const updated = { ...record, title: JSON.parse(options.body).title }
        state.conversations = state.conversations.map((c) => (c.id === id ? updated : c))
        return jsonResponse(updated)
      }
      if (sub === '/archive' && method === 'POST') {
        const updated = { ...record, status: 'archived' }
        state.conversations = state.conversations.map((c) => (c.id === id ? updated : c))
        return jsonResponse(updated)
      }
      if (sub === '/unarchive' && method === 'POST') {
        const updated = { ...record, status: 'active' }
        state.conversations = state.conversations.map((c) => (c.id === id ? updated : c))
        return jsonResponse(updated)
      }
      if (sub === '/delete' && method === 'POST') {
        state.conversations = state.conversations.filter((c) => c.id !== id)
        return jsonResponse(deletionJson(id))
      }
      if (sub === '/send' && method === 'POST') {
        const stream = makeStream(options.signal)
        state.latestStream = stream
        return streamingResponse(stream.reader)
      }
      const successor = sub.match(/^\/turns\/([^/]+)\/(retry|branch)$/)
      if (successor && method === 'POST') {
        const op = successor[2]
        const body = JSON.parse(options.body)
        return jsonResponse(turn(`turn_${op}_${id}`, id, body.role, op === 'retry' ? 'second answer' : 'branched answer', { kind: op }), 201)
      }
    }
    return jsonResponse({ detail: 'not found' }, 404)
  }

  return { state, fetchImpl }
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0))
// Push one or more real relay frames, then let the client's read loop + React
// flush. The frames are parsed by the REAL client; this only advances the clock.
async function emit(stream, ...frames) {
  await act(async () => { frames.forEach((frame) => stream.push(frame)); await tick() })
}
async function finish(stream) {
  await act(async () => { stream.end(); await tick() })
}

const activeConv = conv('conv_active', 'Router planning')

// Land a NEW actor (empty rail → create + select) or a RETURNING actor (existing
// conversation → open it), then leave the transcript open and empty, ready to send.
async function land(kind) {
  const server = kind === 'new'
    ? makeServer({ conversations: [] })
    : makeServer({ conversations: [activeConv], turns: { conv_active: [] } })
  vi.stubGlobal('fetch', server.fetchImpl)
  const user = userEvent.setup()
  render(<App />)
  if (kind === 'new') {
    await screen.findByText('No conversations yet. Start one to begin.')
    await user.click(screen.getByRole('button', { name: 'Start a new conversation' }))
    await screen.findByLabelText('Empty conversation')
    return { server, user, convId: server.state.conversations[0].id }
  }
  await screen.findByText('Router planning')
  await user.click(screen.getByRole('button', { name: 'Open Router planning' }))
  await screen.findByLabelText('Empty conversation')
  return { server, user, convId: 'conv_active' }
}

beforeEach(() => { window.location.hash = '' })
afterEach(() => { vi.unstubAllGlobals() })

// --- Criterion 1: new + returning actors complete the conversation loop -------
describe('Criterion 1 — a new and a returning actor complete the wired chat loop', () => {
  it.each(['new', 'returning'])('%s actor: streamed deltas render progressively through the real reducer', async (kind) => {
    const { user, server } = await land(kind)
    await user.type(screen.getByRole('textbox', { name: 'Message composer' }), 'hello there{Enter}')
    // The composer is streaming before any frame arrives.
    expect(screen.getByText('Streaming response…')).toBeTruthy()
    const stream = server.state.latestStream
    expect(stream).toBeTruthy()

    // Frame 1 (delta, seq 1): the real reducer appends 'Alpha'.
    await emit(stream, deltaFrame('Alpha', 1))
    expect(await screen.findByText('Alpha')).toBeTruthy()
    // Frame 2 (delta, seq 2): reducer appends → 'AlphaBeta'. Progressive REPLACE,
    // not a second appended node: the standalone 'Alpha' node is gone.
    await emit(stream, deltaFrame('Beta', 2))
    expect(await screen.findByText('AlphaBeta')).toBeTruthy()
    expect(screen.queryByText('Alpha')).toBeNull()
    // Terminal (seq 3, completed) settles the turn as complete.
    await emit(stream, terminalFrame('completed', 3))
    await finish(stream)
    expect(await screen.findByText('Response complete')).toBeTruthy()
    // One transcript, and the streamed text settled into a normal turn.
    expect(screen.getAllByRole('list', { name: 'Transcript' })).toHaveLength(1)
    expect(screen.getByText('AlphaBeta')).toBeTruthy()
  })

  it.each(['new', 'returning'])('%s actor: a mid-stream cancel leaves a consistent transcript', async (kind) => {
    const { user, server } = await land(kind)
    await user.type(screen.getByRole('textbox', { name: 'Message composer' }), 'summarize this{Enter}')
    const stream = server.state.latestStream
    await emit(stream, deltaFrame('Partial thought', 1))
    expect(await screen.findByText('Partial thought')).toBeTruthy()
    // The real fetch is aborted; the client settles `cancelled` (never `complete`).
    await user.click(screen.getByRole('button', { name: 'Cancel streaming response' }))
    expect(await screen.findByText('Response cancelled')).toBeTruthy()
    expect(screen.getByText('cancelled')).toBeTruthy() // the turn's settled status chip
    expect(screen.getByText('Partial thought')).toBeTruthy() // partial text preserved, consistent
    expect(screen.queryByText('Streaming response…')).toBeNull()
  })

  it.each(['new', 'returning'])('%s actor: a dropped frame reconnects from the snapshot without duplicating a turn', async (kind) => {
    const { user, server, convId } = await land(kind)
    await user.type(screen.getByRole('textbox', { name: 'Message composer' }), 'recap the thread{Enter}')
    const stream = server.state.latestStream
    // seq 1 delta arrives; seq 2 is DROPPED; seq 3 (terminal) skips ahead. The
    // REAL reducer's gap detection flags needsRefresh and does NOT apply the
    // gapped terminal — the client then refreshes from the committed snapshot.
    await emit(stream, deltaFrame('partial answer', 1))
    server.state.turns[convId] = [
      turn('t_user', convId, 'user', 'recap the thread'),
      turn('t_ref', convId, 'assistant', 'reconnected answer'),
    ]
    await emit(stream, terminalFrame('completed', 3))
    await finish(stream)
    // The reconnected transcript comes from the snapshot, and the dropped partial
    // is NOT duplicated alongside it.
    expect(await screen.findByText('reconnected answer')).toBeTruthy()
    expect(screen.queryByText('partial answer')).toBeNull()
    // Exactly two GETs of this conversation: once on open, once on the reconnect.
    const gets = server.state.calls.filter((c) => c.method === 'GET' && c.path === `/api/conversations/${convId}`)
    expect(gets.length).toBe(2)
  })

  it('manages conversations (create / rename / archive / delete) through the wired client and reflects server state', async () => {
    const { user, server } = await land('returning')
    // Create routes through the real POST client.
    await user.click(screen.getByRole('button', { name: 'Start a new conversation' }))
    expect(server.state.calls.some((c) => c.method === 'POST' && c.path === '/api/conversations')).toBe(true)
    await screen.findByRole('button', { name: 'Open Untitled conversation' })

    // Rename: the row title actually changes in the rendered rail.
    await user.click(screen.getByRole('button', { name: 'Rename Router planning' }))
    const field = screen.getByRole('textbox', { name: 'Rename Router planning' })
    await user.clear(field)
    await user.type(field, 'Router evidence{Enter}')
    expect(await screen.findByText('Router evidence')).toBeTruthy()
    expect(screen.queryByText('Router planning')).toBeNull()

    // Archive: after the wired refresh the row moves into the Archived section.
    await user.click(screen.getByRole('checkbox', { name: 'Show archived conversations' }))
    await user.click(screen.getByRole('button', { name: 'Archive Router evidence' }))
    const archived = await screen.findByRole('region', { name: 'Archived conversations' })
    expect(within(archived).getByText('Router evidence')).toBeTruthy()
    const active = screen.getByRole('region', { name: 'Active conversations' })
    expect(within(active).queryByText('Router evidence')).toBeNull()

    // Delete is a two-step confirm; after the wired refresh the row is gone.
    await user.click(within(archived).getByRole('button', { name: 'Delete Router evidence' }))
    await user.click(screen.getByRole('button', { name: 'Confirm delete Router evidence' }))
    await waitFor(() => expect(screen.queryByText('Router evidence')).toBeNull())
    expect(server.state.calls.some((c) => c.method === 'POST' && c.path === '/api/conversations/conv_active/delete')).toBe(true)
  })

  it('searches over titles and retained content and renders the wired server results', async () => {
    const { user, server } = await land('returning')
    // A content-only match: the title does not contain the query, but the server
    // (which searches retained content) returns it and the rail surfaces it.
    server.state.searchResults = [conv('conv_c', 'Weekly sync')]
    await user.type(screen.getByRole('searchbox', { name: 'Search conversations' }), 'invoices')
    expect(await screen.findByText('Weekly sync')).toBeTruthy()
    const searchCall = server.state.calls.find((c) => c.path === '/api/conversations/search')
    expect(searchCall.url).toContain('query=invoices')
  })
})

// --- Criterion 2: context + Advanced are secondary; no delivery effect --------
describe('Criterion 2 — project/PRD/task context and Advanced mode stay secondary to the conversation', () => {
  it('opens Advanced within chat without a parallel transcript, and fires no delivery/bridge call', async () => {
    const { user, server } = await land('returning')
    // With no emitted delivery binding, the context panel is truthfully unlinked
    // rather than fabricating a project/PRD/task — context is secondary, not primary.
    expect(screen.getByText('No linked delivery context.')).toBeTruthy()

    await user.type(screen.getByRole('textbox', { name: 'Message composer' }), 'plan it{Enter}')
    const stream = server.state.latestStream
    await emit(stream, deltaFrame('answer one', 1), terminalFrame('completed', 2))
    await finish(stream)
    await screen.findByText('answer one')

    // Advanced mode opens INSIDE chat; it does not spawn a second transcript.
    await user.click(screen.getByRole('button', { name: 'Toggle Advanced mode' }))
    expect(screen.getByRole('region', { name: 'Advanced controls' })).toBeTruthy()
    expect(screen.getAllByRole('list', { name: 'Transcript' })).toHaveLength(1)
    expect(screen.getByText('answer one')).toBeTruthy()

    // Every network call the chat surface made is chat-scoped: no delivery or
    // bridge effect (no /workflows/start, /sandbox, /approvals, /sessions POST,
    // /tasks, /projects). Inspect the fetch call log by URL.
    const allowed = ['/api/bootstrap', '/api/conversations', '/api/chat', '/api/preferences']
    const stray = server.state.calls.filter((call) => !allowed.some((prefix) => call.path.startsWith(prefix)))
    expect(stray).toEqual([])
  })
})

// --- Criterion 3: title/state primary, id secondary ---------------------------
describe('Criterion 3 — titles and state drive the hierarchy; ids are secondary disclosure', () => {
  it('makes the title the primary label and never the bare id', async () => {
    await land('returning')
    // The human-facing title is what the actor reads (rail row + page heading).
    expect(screen.getAllByText('Router planning').length).toBeGreaterThan(0)
    expect(screen.getAllByText('active').length).toBeGreaterThan(0) // lifecycle state visible
    // The canonical id renders only as a muted <small>, and is NOT the accessible
    // name of any control — the row's accessible name is title-first.
    const id = screen.getByText('conv_active')
    expect(id.tagName).toBe('SMALL')
    expect(screen.queryByRole('button', { name: 'conv_active' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Open Router planning' })).toBeTruthy()
    // The selected conversation surfaces the TITLE as the page heading, not the id.
    expect(screen.getByRole('heading', { level: 1, name: 'Router planning' })).toBeTruthy()
    expect(screen.queryByRole('heading', { name: 'conv_active' })).toBeNull()
  })
})

// --- Criterion 4: Chat first in nav; Delivery reachable lower -----------------
describe('Criterion 4 — Chat is first in navigation and Delivery remains reachable', () => {
  it('renders Chat before Delivery in the one primary nav and keeps Delivery reachable at a narrow viewport', async () => {
    const { user } = await land('returning')
    const chat = screen.getByRole('button', { name: 'Chat' })
    const delivery = screen.getByRole('button', { name: 'Delivery' })
    // Real rendered order: Chat precedes Delivery (Delivery follows Chat in the DOM).
    expect(chat.compareDocumentPosition(delivery) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // Both live in the same primary <nav>; Delivery sits below Chat.
    expect(chat.parentElement.tagName).toBe('NAV')
    expect(chat.parentElement).toBe(delivery.parentElement)
    // The app's responsive nav is CSS-only (no JS prunes items by width), so a
    // narrow viewport keeps Delivery in the DOM and reachable. Prove reachability
    // by activating it — the workspace switches to the Delivery cockpit.
    Object.defineProperty(window, 'innerWidth', { configurable: true, writable: true, value: 375 })
    window.dispatchEvent(new Event('resize'))
    expect(delivery.disabled).toBe(false)
    await user.click(delivery)
    expect(await screen.findByText('Delivery cockpit')).toBeTruthy()
  })
})

// --- Criterion 5: keyboard-only send, focus, live announcements ---------------
describe('Criterion 5 — keyboard-only operation with predictable focus and live announcements', () => {
  it('completes a full send with the keyboard, moves focus deterministically, and announces streaming + completion', async () => {
    const { user, server } = await land('returning')
    const composer = screen.getByRole('textbox', { name: 'Message composer' })
    // Keyboard-only: Enter submits (no Send button click); the aria-live region
    // announces the streaming state.
    await user.type(composer, 'keyboard send{Enter}')
    expect(screen.getByText('Streaming response…')).toBeTruthy()
    // Focus moves to Cancel when streaming starts, so it is never dropped to <body>.
    const cancel = screen.getByRole('button', { name: 'Cancel streaming response' })
    expect(document.activeElement).toBe(cancel)

    const stream = server.state.latestStream
    await emit(stream, deltaFrame('done', 1), terminalFrame('completed', 2))
    await finish(stream)
    // The aria-live region announces completion.
    expect(await screen.findByText('Response complete')).toBeTruthy()
    // Focus returns to the composer for the next message.
    expect(document.activeElement).toBe(composer)
  })

  it('submits on Enter but keeps Shift+Enter as a newline (keyboard submission contract)', async () => {
    const { user, server } = await land('returning')
    const composer = screen.getByRole('textbox', { name: 'Message composer' })
    await user.type(composer, 'first line{Shift>}{Enter}{/Shift}second line')
    // Shift+Enter inserted a newline and did NOT start a stream.
    expect(composer.value).toContain('\n')
    expect(server.state.latestStream).toBeNull()
    // A bare Enter submits.
    await user.type(composer, '{Enter}')
    expect(screen.getByText('Streaming response…')).toBeTruthy()
    expect(server.state.latestStream).toBeTruthy()
  })
})
