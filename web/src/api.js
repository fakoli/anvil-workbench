import { initialStreamState, reduceStreamState } from './chat-api'

export async function bootstrap() {
  const response = await fetch('/api/bootstrap')
  if (!response.ok) throw new Error('Workbench hub is not available')
  return response.json()
}

export async function createProject({ name, state_root }) {
  const response = await fetch('/api/projects', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, state_root }),
  })
  if (!response.ok) throw new Error('Project could not be created')
  return response.json()
}

export async function approve(approvalId) {
  const response = await fetch(`/api/approvals/${approvalId}/approve`, {
    method: 'POST',
  })
  if (!response.ok) throw new Error('Approval could not be recorded')
  return response.json()
}

export async function createSession({ project_id, title, worktree_id, workflow_definition, skills }) {
  const response = await fetch('/api/sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_id, title, worktree_id, workflow_definition, skills }),
  })
  if (!response.ok) throw new Error('Session could not be created')
  return response.json()
}

// An optional caller-owned AbortSignal lets a dismissed Deliver abort a hung
// start POST so the user is never trapped waiting on an unresponsive bridge
// (T006 a11y #4). It is threaded only into the fetch options; the request body
// is unchanged.
export async function startWorkflow(workflowId, { task_id, model }, { signal } = {}) {
  const response = await fetch(`/api/workflows/${workflowId}/start`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ task_id, model }),
    signal,
  })
  if (!response.ok) throw new Error('Workflow could not be started')
  return response.json()
}

export async function addDirective(sessionId, content) {
  const response = await fetch(`/api/sessions/${sessionId}/directives`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content }),
  })
  if (!response.ok) throw new Error('Delivery direction could not be recorded')
  return response.json()
}

export async function fetchRoutes() {
  const response = await fetch('/api/routes')
  if (!response.ok) throw new Error('Route decisions are unavailable')
  return response.json()
}

export async function searchEvidence(projectId, query) {
  const response = await fetch(`/api/evidence/search?project_id=${encodeURIComponent(projectId)}&query=${encodeURIComponent(query)}`)
  if (!response.ok) throw new Error('Evidence search is unavailable')
  return response.json()
}

export async function taskLineage(taskId) {
  const response = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/lineage`)
  if (!response.ok) throw new Error('Task lineage is unavailable')
  return response.json()
}

export async function runSandbox({ model, input }) {
  const response = await fetch('/api/sandbox', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model, input }),
  })
  if (!response.ok) throw new Error('Sandbox request was not accepted by Anvil Serving')
  return response.json()
}

export async function probeSkills(projectId) {
  const response = await fetch(`/api/projects/${projectId}/skills/probe`, { method: 'POST' })
  if (!response.ok) throw new Error('Bridge skills could not be checked')
  return response.json()
}

export function voiceSocketUrl(sessionId) {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/api/sessions/${sessionId}/voice/realtime`
}

// --- Actor-scoped conversation API client (chat-first-voice T004.1) ----------
//
// The browser half of the merged `/api/conversations` surface. Every request is
// actor-scoped server-side from the trusted tailnet identity header, so this
// client assembles no actor, token, endpoint, or credential — only the safe
// path, method, and bounded body. A failed request throws a non-leaking Error so
// the caller can render a distinct failure state instead of a partial success.

const CONVERSATIONS = '/api/conversations'

async function chatJson(response, failure) {
  if (!response.ok) throw new Error(failure)
  return response.json()
}

function jsonPost(path, body) {
  return fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export async function createConversation({ title, retention } = {}) {
  const body = {}
  if (title) body.title = title
  if (retention) body.retention = retention
  return chatJson(await jsonPost(CONVERSATIONS, body), 'Conversation could not be created')
}

// An optional caller-owned AbortSignal lets a superseding list/search abort the
// previous in-flight request. It is threaded in only when present so an
// unsignalled call keeps its single-argument `fetch(url)` shape.
function getSignalArgs(signal) {
  return signal ? [{ signal }] : []
}

export async function listConversations({ includeArchived = false, pinned, tag, folder, signal } = {}) {
  const params = new URLSearchParams()
  if (includeArchived) params.set('include_archived', 'true')
  if (pinned != null) params.set('pinned', String(pinned))
  if (tag) params.set('tag', tag)
  if (folder) params.set('folder', folder)
  const qs = params.toString()
  return chatJson(await fetch(`${CONVERSATIONS}${qs ? `?${qs}` : ''}`, ...getSignalArgs(signal)), 'Conversations are unavailable')
}

export async function searchConversations(query, { includeArchived = false, pinned, tag, folder, signal } = {}) {
  const params = new URLSearchParams()
  params.set('query', query)
  if (includeArchived) params.set('include_archived', 'true')
  if (pinned != null) params.set('pinned', String(pinned))
  if (tag) params.set('tag', tag)
  if (folder) params.set('folder', folder)
  return chatJson(await fetch(`${CONVERSATIONS}/search?${params.toString()}`, ...getSignalArgs(signal)), 'Conversation search is unavailable')
}

export async function getConversation(conversationId) {
  return chatJson(
    await fetch(`${CONVERSATIONS}/${encodeURIComponent(conversationId)}`),
    'Conversation is unavailable',
  )
}

export async function renameConversation(conversationId, title) {
  return chatJson(
    await jsonPost(`${CONVERSATIONS}/${encodeURIComponent(conversationId)}/rename`, { title }),
    'Conversation could not be renamed',
  )
}

export async function archiveConversation(conversationId) {
  return chatJson(
    await fetch(`${CONVERSATIONS}/${encodeURIComponent(conversationId)}/archive`, { method: 'POST' }),
    'Conversation could not be archived',
  )
}

export async function unarchiveConversation(conversationId) {
  return chatJson(
    await fetch(`${CONVERSATIONS}/${encodeURIComponent(conversationId)}/unarchive`, { method: 'POST' }),
    'Conversation could not be unarchived',
  )
}

export async function deleteConversation(conversationId, mode = 'purge_content_keep_tombstone') {
  return chatJson(
    await jsonPost(`${CONVERSATIONS}/${encodeURIComponent(conversationId)}/delete`, { mode }),
    'Conversation could not be deleted',
  )
}

export async function appendTurn(conversationId, turn) {
  return chatJson(
    await jsonPost(`${CONVERSATIONS}/${encodeURIComponent(conversationId)}/turns`, turn),
    'Turn could not be recorded',
  )
}

export async function retryTurn(conversationId, turnId, turn) {
  return chatJson(
    await jsonPost(`${CONVERSATIONS}/${encodeURIComponent(conversationId)}/turns/${encodeURIComponent(turnId)}/retry`, turn),
    'Retry could not be recorded',
  )
}

export async function branchTurn(conversationId, turnId, turn) {
  return chatJson(
    await jsonPost(`${CONVERSATIONS}/${encodeURIComponent(conversationId)}/turns/${encodeURIComponent(turnId)}/branch`, turn),
    'Branch could not be recorded',
  )
}

// The reviewed chat-route allowlist projection (chat_routes.py `as_dict`): safe
// identifiers, digests, and declared control names only — never an endpoint,
// URL, token, credential, or policy field.
export async function fetchChatRoutes() {
  return chatJson(await fetch('/api/chat/routes'), 'Chat routes are unavailable')
}

// Stream one assistant response over the merged relay (chat_stream.py RelayEvent
// frames: `{seq, kind: 'delta' | 'terminal', text, outcome}`). The response body
// is newline-delimited JSON; each frame is folded through the SAME reducer the
// hub mirrors (`reduceStreamState`), so a dropped frame is detected (surfacing
// `needsRefresh` for a snapshot reconnect) and a stale/replayed frame never
// duplicates the response. Cancellation is exposed through the caller-owned
// `signal`: aborting tears down the fetch — which the relay observes to settle
// `cancelled` upstream — and settles the local state `cancelled` here without
// ever emitting a later completion. A genuine transport failure throws so the
// caller renders a failed (not merely interrupted) state.
export async function sendMessage({ conversationId, routeId, prompt, controls, signal, onFrame, onState } = {}) {
  const settleCancelled = (state) => {
    const cancelled = { ...state, terminal: 'cancelled' }
    onState?.(cancelled)
    return cancelled
  }

  let response
  try {
    response = await jsonPostWithSignal(
      `${CONVERSATIONS}/${encodeURIComponent(conversationId)}/send`,
      { route_id: routeId, prompt, controls },
      signal,
    )
  } catch (error) {
    if (signal?.aborted || error?.name === 'AbortError') return settleCancelled(initialStreamState())
    throw new Error('The response stream could not be started')
  }
  if (!response.ok || !response.body) throw new Error('The response stream could not be started')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let state = initialStreamState()
  let buffer = ''

  const applyLine = (line) => {
    const trimmed = line.trim()
    if (!trimmed) return
    let frame
    try {
      frame = JSON.parse(trimmed)
    } catch {
      return // ignore an unparseable keepalive/comment line rather than fail the stream
    }
    state = reduceStreamState(state, frame)
    onFrame?.(frame)
    onState?.(state)
  }

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let index
      while ((index = buffer.indexOf('\n')) >= 0) {
        applyLine(buffer.slice(0, index))
        buffer = buffer.slice(index + 1)
      }
    }
    applyLine(buffer)
  } catch (error) {
    if (signal?.aborted || error?.name === 'AbortError') return settleCancelled(state)
    throw new Error('The response stream was interrupted')
  } finally {
    reader.releaseLock?.()
  }
  return state
}

function jsonPostWithSignal(path, body, signal) {
  return fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
}

// --- Delivery-projection explorer client (plan-task-delivery T003) ------------
//
// The browser half of the merged, read-only delivery-projection surface
// (workbench/api.py `build_delivery_projection_router`). Every endpoint is
// GET-only, project-scoped by the path, and redacted server-side on the last
// hop, so this client assembles only the safe path — no actor, token, endpoint,
// or credential — and returns the exact wrapped shape the router serves:
// `{content}`, `{tasks}`, `{task}`, `{eligibility}`. The surface fails closed
// with 503 until a projection store is configured (it is deliberately NOT wired
// into the live bridge poll loop); that is surfaced as a distinct, truthful
// "not configured" error so the UI renders an unconfigured degraded state
// rather than a generic failure. Any other non-2xx throws a non-leaking Error.

const PROJECTS = '/api/projects'

function prdBase(projectId, prdId) {
  return `${PROJECTS}/${encodeURIComponent(projectId)}/prds/${encodeURIComponent(prdId)}`
}

async function deliveryJson(response, failure) {
  if (response.status === 503) throw new Error('Delivery projection is not configured for this hub')
  if (!response.ok) throw new Error(failure)
  return response.json()
}

export async function fetchPrdContent(projectId, prdId) {
  return deliveryJson(await fetch(`${prdBase(projectId, prdId)}/content`), 'PRD content is unavailable')
}

export async function fetchPrdTasks(projectId, prdId) {
  return deliveryJson(await fetch(`${prdBase(projectId, prdId)}/tasks`), 'PRD tasks are unavailable')
}

export async function fetchTaskReference(projectId, prdId, taskId) {
  return deliveryJson(
    await fetch(`${prdBase(projectId, prdId)}/tasks/${encodeURIComponent(taskId)}`),
    'Task reference is unavailable',
  )
}

export async function fetchTaskEligibility(projectId, prdId, taskId) {
  return deliveryJson(
    await fetch(`${prdBase(projectId, prdId)}/tasks/${encodeURIComponent(taskId)}/eligibility`),
    'Task eligibility is unavailable',
  )
}
