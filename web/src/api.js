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
export async function sendMessage({ conversationId, routeId, routeProvenance, prompt, controls, signal, onFrame, onState } = {}) {
  const settleCancelled = (state) => {
    const cancelled = { ...state, terminal: 'cancelled' }
    onState?.(cancelled)
    return cancelled
  }

  let response
  try {
    response = await jsonPostWithSignal(
      `${CONVERSATIONS}/${encodeURIComponent(conversationId)}/send`,
      // `route_selection` records whether the route was EXPLICITLY chosen or
      // DEFAULTED from preference, so Serving can echo the provenance back on the
      // turn's resolution mark (chat-first-voice T010). It never selects a route.
      { route_id: routeId, route_selection: routeProvenance, prompt, controls },
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

// --- Advanced playground client (advanced-model-playground T005) -------------
//
// The browser half of the Advanced playground surface: the reviewed advanced-route
// allowlist (workbench/advanced_routes.py `DiscoveredAdvancedRoutes.browser_projection`)
// and a per-branch tuned run streamed over the SAME relay contract as ordinary
// chat (workbench/advanced_runtime.run_advanced_stream via chat_stream). Every
// request is actor-scoped server-side from the trusted tailnet identity, so this
// client assembles NO actor, token, endpoint, or credential — only the safe path
// and a CLOSED body of declared fields (route id, the submitted_controls array,
// prompt, optional instructions). The controls are the reviewed closed set: the
// browser never round-trips a connector host, a raw command, or a credential.
//
// The advanced runtime is DELIBERATELY not wired into the live bridge poll loop
// yet; the surface fails closed with 503 until it is configured. That is surfaced
// as this SHARED not-configured sentinel so the panel degrades truthfully
// (unavailable) while the ordinary transcript stays fully usable — the view keys
// its degrade branch off value equality, so a reword moves both sides together.
export const ADVANCED_NOT_CONFIGURED = 'The advanced playground is not configured for this hub'

// The reviewed advanced-route allowlist: `{routes: [browser_projection…]}` where
// each route carries identifiers, digests, and declared control metadata ONLY
// (control_view: type/bounds/allowed_values/default/editable/source) — never an
// endpoint, URL, token, credential, or policy-internal field.
export async function fetchAdvancedRoutes() {
  const response = await fetch('/api/chat/advanced/routes')
  if (response.status === 503) throw new Error(ADVANCED_NOT_CONFIGURED)
  if (!response.ok) throw new Error('Advanced routes are unavailable')
  return response.json()
}

// Stream one bounded, tuned Advanced attempt on an EXISTING conversation, forked as
// a `mode="advanced"` sibling turn (advanced_runtime.open_advanced_branch +
// run_advanced_stream). The response body is the SAME newline-delimited relay-frame
// stream ordinary chat uses, folded through the SAME `reduceStreamState` reducer,
// so a dropped frame is detected and a stale/replayed frame never duplicates the
// attempt. The terminal frame additionally carries the settled `turn_id`,
// `branch_id`, and the redacted `advanced-trace.v1` record for the inspector.
//
// Cancellation is exposed through the caller-owned `signal`: aborting tears down
// THIS fetch — which the relay observes to settle `cancelled` upstream — and
// settles the local state `cancelled` here without ever emitting a later
// completion, so Cancel genuinely aborts the in-flight run (never a local flip).
// A 503 before the stream degrades truthfully; any other transport failure throws
// so the caller renders a failed (not merely interrupted) state.
export async function runAdvancedBranch({
  conversationId, parentTurnId, branchId, routeId, controls, prompt, instructions,
  structuredOutputMode, signal, onFrame, onState,
} = {}) {
  const settleCancelled = (state) => {
    const cancelled = { ...state, terminal: 'cancelled' }
    onState?.(cancelled)
    return cancelled
  }
  const body = {
    parent_turn_id: parentTurnId, branch_id: branchId, route_id: routeId,
    controls, prompt,
  }
  if (instructions) body.instructions = instructions
  if (structuredOutputMode) body.structured_output_mode = structuredOutputMode

  let response
  try {
    response = await jsonPostWithSignal(
      `${CONVERSATIONS}/${encodeURIComponent(conversationId)}/advanced/run`, body, signal,
    )
  } catch (error) {
    if (signal?.aborted || error?.name === 'AbortError') return settleCancelled(initialStreamState())
    throw new Error('The advanced attempt could not be started')
  }
  if (response.status === 503) throw new Error(ADVANCED_NOT_CONFIGURED)
  if (!response.ok || !response.body) throw new Error('The advanced attempt could not be started')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let state = initialStreamState()
  let trace = null
  let turnId = null
  let settledBranchId = branchId
  let buffer = ''

  const applyLine = (line) => {
    const trimmed = line.trim()
    if (!trimmed) return
    let frame
    try { frame = JSON.parse(trimmed) } catch { return }
    // The terminal frame carries the settled ids + the redacted trace alongside
    // the relay outcome; capture them without disturbing the shared reducer.
    if (frame.trace) trace = frame.trace
    if (frame.turn_id) turnId = frame.turn_id
    if (frame.branch_id) settledBranchId = frame.branch_id
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
    if (signal?.aborted || error?.name === 'AbortError') return { ...settleCancelled(state), trace, turnId, branchId: settledBranchId }
    throw new Error('The advanced attempt was interrupted')
  } finally {
    reader.releaseLock?.()
  }
  return { ...state, trace, turnId, branchId: settledBranchId }
}

// --- Advanced model playground: presets + comparison (T006), instruction
// templates (T009), declared-criterion route ratings (T010) -------------------
//
// The browser half of the actor-private playground surfaces
// (workbench/api.py build_advanced_preset_router / build_advanced_template_router
// / build_advanced_rating_router). Every request is actor-scoped server-side from
// the trusted tailnet identity, so this client assembles NO actor, token, or
// endpoint. Each surface fails closed with 503 until wired; that is surfaced as
// this SHARED sentinel so the panel degrades truthfully while the transcript
// stays usable (the view keys its degrade branch off value equality).
export const ADVANCED_PLAYGROUND_NOT_CONFIGURED = 'The advanced playground surfaces are not configured for this hub'

async function playgroundGet(path, failure) {
  const response = await fetch(path)
  if (response.status === 503) throw new Error(ADVANCED_PLAYGROUND_NOT_CONFIGURED)
  if (!response.ok) throw new Error(failure)
  return response.json()
}

async function playgroundPost(path, body, failure) {
  const response = await jsonPost(path, body)
  if (response.status === 503) throw new Error(ADVANCED_PLAYGROUND_NOT_CONFIGURED)
  if (!response.ok) throw new Error(failure)
  return response.json()
}

const ADV_PRESETS = '/api/chat/advanced/presets'
const ADV_TEMPLATES = '/api/chat/advanced/templates'
const ADV_RATINGS = '/api/chat/advanced/ratings'

// The actor's saved presets: `{presets:[advanced-preset.v1 records]}` — each a
// digest-pinned selection, never an endpoint/credential/raw-prompt field.
export async function fetchAdvancedPresets() {
  return playgroundGet(ADV_PRESETS, 'Advanced presets are unavailable')
}

// Resolve a preset against the SERVER's own current live digests. The browser
// sends NO live_digests: the server derives readiness from its own advanced
// route/tool/template registry, so the client can neither spoof a drifted pin to
// "ready" nor fabricate drift from a partial view. Returns `{status:"ready",
// preset}`, `{status:"repair_required", preset_id, drifted_refs}` (NO `preset` —
// the server never substitutes a drifted route/tool), or `{status:"unverifiable",
// ...}` when the server cannot verify a referenced digest.
export async function resolveAdvancedPreset(presetId) {
  return playgroundPost(`${ADV_PRESETS}/${encodeURIComponent(presetId)}/resolve`,
    {}, 'The preset could not be resolved')
}

// A FACTUAL side-by-side comparison. The server admits a ranking (a winner) ONLY
// alongside a declared, non-qualification criterion, so the returned record never
// carries an inferred winner.
export async function buildAdvancedComparison(comparison) {
  return playgroundPost(`${ADV_PRESETS}/comparison`, { comparison }, 'The comparison is not valid')
}

// The actor's saved instruction templates: `{templates:[advanced-template.v1]}`
// with the FULL body text + declared substitutions visible pre-send.
export async function fetchAdvancedTemplates() {
  return playgroundGet(ADV_TEMPLATES, 'Advanced templates are unavailable')
}

// Resolve a pinned template reference; a removed or digest-drifted template
// returns `{status:"repair_required", ...}` with NO `template` (no substitution).
export async function resolveAdvancedTemplate(templateId, pinnedDigest) {
  return playgroundPost(`${ADV_TEMPLATES}/${encodeURIComponent(templateId)}/resolve`,
    { pinned_digest: pinnedDigest }, 'The template could not be resolved')
}

// Render a template as DECLARED, pre-send-visible instructions: the resolved full
// text + the named substitution bindings, marked `provenance:"declared"`. A value
// bound to an undeclared substitution is refused server-side (422).
export async function renderAdvancedDeclaredInstructions(templateId, bindings = {}) {
  return playgroundPost(`${ADV_TEMPLATES}/${encodeURIComponent(templateId)}/declared-instructions`,
    { bindings }, 'The template instructions could not be rendered')
}

// The CLOSED set of declared criteria a rating may name (no free-text rating).
export async function fetchRatingCriteria() {
  return playgroundGet(`${ADV_RATINGS}/criteria`, 'Rating criteria are unavailable')
}

// Record an actor-local, non-qualification rating. The server refuses a rating
// that names no declared criterion (422).
export async function recordAdvancedRating({ routeId, criterionId, score, note } = {}) {
  const body = { route_id: routeId, criterion_id: criterionId, score }
  if (note) body.note = note
  return playgroundPost(ADV_RATINGS, body, 'The rating could not be recorded')
}

// Per route/criterion aggregates, each carrying the non_qualification label +
// disclaimer — never model qualification or delivery evidence.
export async function fetchRatingAggregates() {
  return playgroundGet(`${ADV_RATINGS}/aggregates`, 'Rating aggregates are unavailable')
}

// --- Chat voice relay client (chat-first-voice T005.2 / T005.3) ---------------
//
// The browser half of the STT/TTS relay (workbench/api.py build_voice_relay_router
// at /api/chat/voice). Every request is actor-scoped server-side from the trusted
// tailnet identity, so this client assembles NO actor, token, endpoint, provider
// key, or credential — only the safe path and a CLOSED body. The relay reaches
// Anvil Serving only; the browser never talks to a provider.
//
// The audio a request carries (STT input) and the audio a response returns (TTS
// playback) are transient: this module holds no state and nothing is cached, so
// no audio is ever written to localStorage or a browser cache by this client.
// The surface fails closed with 503 until the relay is configured; that is
// surfaced as a distinct, truthful "not configured" Error so the UI degrades
// truthfully while the textual chat stays fully usable.

// Transcribe one in-memory audio chunk into an EDITABLE draft. It returns
// `{draft: {text, is_final, duration_ms}}` and creates NO turn — the caller
// places the draft into the composer for the actor to review, edit, and submit.
export async function transcribeVoice({ conversationId, audioBase64, audioFormat, isFinal = false, durationMs } = {}) {
  const body = { conversation_id: conversationId, audio_base64: audioBase64, audio_format: audioFormat, is_final: Boolean(isFinal) }
  if (durationMs != null) body.duration_ms = durationMs
  const response = await jsonPost('/api/chat/voice/transcribe', body)
  if (response.status === 503) throw new Error('Voice input is not configured for this hub')
  if (!response.ok) throw new Error('Your recording could not be transcribed')
  return response.json()
}

// Synthesize transient playback audio for one already-rendered message. It
// returns `{audio_base64, audio_format, sample_rate}` and mutates NO message
// state — producing audio changes nothing about the conversation.
export async function speakMessage({ conversationId, messageRef, text, outputFormat = 'mp3' } = {}) {
  const body = { conversation_id: conversationId, message_ref: messageRef, text }
  if (outputFormat) body.output_format = outputFormat
  const response = await jsonPost('/api/chat/voice/speak', body)
  if (response.status === 503) throw new Error('Read-aloud is not configured for this hub')
  if (!response.ok) throw new Error('Read-aloud could not be produced')
  return response.json()
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

// --- Settings / preferences API client (preferences-configuration T005.1) -----
//
// The browser half of the actor-scoped preference surface
// (workbench/api.py `build_preferences_router` at /api/preferences) plus the
// typed policy-operation spine (`build_policy_operations_router` at
// /api/policy-operations) that an approval-gated change routes through. Every
// endpoint is actor-scoped server-side from the trusted tailnet identity, so
// this client assembles NO actor, token, endpoint, provider key, or credential —
// only the safe path, method, and a CLOSED body of declared fields. It can never
// smuggle a deployment credential or raw provider key: the write/reset/preview
// bodies are built field-by-field from the declared closed set (mirroring the
// server's `extra="forbid"` models), and this module holds no state, so nothing
// is cached between calls.
//
// The distinct server outcomes stay DISTINGUISHABLE to the UI as typed results
// rather than a single collapsed error (T005.1): a value that is rejected (422),
// a stale optimistic write (409, keep the draft + reload), an unknown setting
// (404), and an unavailable/unconfigured surface (503 / network) each map to a
// separate `status`. Reads throw a distinct non-leaking Error so the view can
// render a loading/empty/error state.

const PREFERENCES = '/api/preferences'
const POLICY_OPERATIONS = '/api/policy-operations'

async function preferencesReadJson(response, failure) {
  if (response.status === 503) throw new Error('The settings service is not configured for this hub')
  if (!response.ok) throw new Error(failure)
  return response.json()
}

// The settings actor-view catalog + the resolved effective actor-scope values:
// `{catalog: {schema_version, catalog_id, revision, settings:[descriptor…]}, effective:[value…]}`.
// `project_id` is optional; it is only needed to resolve project-scope values.
export async function fetchPreferences(projectId) {
  const qs = projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''
  return preferencesReadJson(await fetch(`${PREFERENCES}${qs}`), 'Settings are unavailable')
}

// One stored preference record in the actor's own namespace. A missing record or
// a foreign namespace is the same indistinct 404 upstream; surfaced here as a
// distinct 'unknown' Error the caller can show without leaking existence.
export async function fetchPreference(settingId, { scope = 'personal', projectId } = {}) {
  const params = new URLSearchParams({ scope })
  if (projectId) params.set('project_id', projectId)
  const response = await fetch(`${PREFERENCES}/${encodeURIComponent(settingId)}?${params.toString()}`)
  if (response.status === 404) throw new Error('This preference is not set in your namespace')
  return preferencesReadJson(response, 'This preference is unavailable')
}

// Classify a preference write/reset response into a typed result the UI keeps
// distinguishable. NEVER collapses stale/invalid/unknown/unavailable/
// approval-required together (T005.1 criterion 2).
//
// The backend emits TWO distinct 409s from the SAME endpoints and they must not
// be conflated:
//   * OBJECT detail `{reload_required, current_version}` — a genuine optimistic-
//     concurrency STALE write (api.py:812-820 / :843-851). Keep the draft; the
//     actor reloads/compares.
//   * STRING detail (or an object lacking those fields) — an authority REFUSAL,
//     e.g. an approval-gated/env-managed write the store rejects with a plain
//     message (api.py:825-826 PUT, :856-857 reset). This is NOT stale: reloading
//     changes nothing. Surfaced as `approval_required` with the server's reason
//     so the UI never shows a fabricated "changed elsewhere — reload" dead-end.
async function classifyPreferenceWrite(response) {
  if (response.ok) return { status: 'saved', preference: (await response.json()).preference }
  let body = {}
  try { body = await response.json() } catch { /* non-JSON error body */ }
  const detail = body?.detail
  if (response.status === 409) {
    const isStale = detail && typeof detail === 'object'
      && ('reload_required' in detail || 'current_version' in detail)
    if (isStale) {
      // Stale optimistic write: the local draft must be preserved and the actor
      // prompted to reload/compare, never silently overwritten.
      return {
        status: 'stale',
        reloadRequired: detail.reload_required === true,
        currentVersion: Number.isInteger(detail.current_version) ? detail.current_version : null,
        message: 'This setting changed elsewhere. Reload to compare before saving.',
      }
    }
    // Authority refusal (approval-gated / env-managed). Reloading is useless; the
    // server's own message says why the write cannot proceed here.
    return {
      status: 'approval_required',
      message: typeof detail === 'string' && detail
        ? detail
        : 'This change requires an approval and cannot be saved directly.',
    }
  }
  if (response.status === 422) {
    return { status: 'invalid', message: typeof detail === 'string' ? detail : 'That value is not allowed for this setting.' }
  }
  if (response.status === 404) return { status: 'unknown', message: 'This setting is not available.' }
  if (response.status === 503) return { status: 'unavailable', message: 'The settings service is not configured for this hub.' }
  return { status: 'unavailable', message: 'The setting could not be saved.' }
}

// Commit one scoped preference write under optimistic concurrency. The body is a
// CLOSED set — `{scope, value, expected_version, project_id?}` — so a caller can
// never smuggle an actor, credential, provider key, or any other field past this
// edge. Returns a typed result; only a genuine network failure yields
// 'unavailable' via the catch.
export async function writePreference(settingId, { scope, value, expectedVersion, projectId } = {}) {
  const body = { scope, value, expected_version: expectedVersion }
  if (projectId) body.project_id = projectId
  try {
    const response = await jsonPut(`${PREFERENCES}/${encodeURIComponent(settingId)}`, body)
    return classifyPreferenceWrite(response)
  } catch {
    return { status: 'unavailable', message: 'The settings service could not be reached.' }
  }
}

// Reset one preference to its inherited/default state at ONLY the named scope.
// Same closed body (no value) and the same typed-result contract as a write.
export async function resetPreference(settingId, { scope, expectedVersion, projectId } = {}) {
  const body = { scope, expected_version: expectedVersion }
  if (projectId) body.project_id = projectId
  try {
    const response = await jsonPost(`${PREFERENCES}/${encodeURIComponent(settingId)}/reset`, body)
    if (response.ok) return { status: 'reset', effective: (await response.json()).effective }
    return classifyPreferenceWrite(response)
  } catch {
    return { status: 'unavailable', message: 'The settings service could not be reached.' }
  }
}

function jsonPut(path, body) {
  return fetch(path, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

// The typed policy-operation body an approval-gated change routes through. It is
// CLOSED — only the declared operation fields — mirroring the server's
// `extra="forbid"` `PolicyOperationBody`; there is no provider-credential field.
function policyOperationBody({ settingId, scope, operation, opVersion, value, projectId, provider }) {
  const body = { setting_id: settingId, scope, operation, op_version: opVersion }
  if (operation === 'preference.set') body.value = value
  if (projectId) body.project_id = projectId
  if (provider) body.provider = provider
  return body
}

async function classifyPolicyBuild(response, okStatus, okKey) {
  if (response.ok) return { status: okStatus, [okKey]: await response.json() }
  let body = {}
  try { body = await response.json() } catch { /* non-JSON */ }
  const detail = body?.detail
  if (response.status === 422) return { status: 'invalid', message: typeof detail === 'string' ? detail : 'That change is not allowed.' }
  if (response.status === 404) return { status: 'unknown', message: 'This setting is not available.' }
  if (response.status === 503) return { status: 'unavailable', message: 'The approval surface is not configured for this hub.' }
  return { status: 'unavailable', message: 'The change could not be previewed.' }
}

// Preview an approval-gated change WITHOUT mutating anything. Returns the served
// preview envelope (operation type, effect summary, target scope, and the
// payload-hash fingerprint the approval will bind) as a typed result.
export async function previewPolicyOperation(request) {
  try {
    const response = await jsonPost(`${POLICY_OPERATIONS}/preview`, policyOperationBody(request))
    return classifyPolicyBuild(response, 'previewed', 'preview')
  } catch {
    return { status: 'unavailable', message: 'The approval surface could not be reached.' }
  }
}

// The exact (action, payload_hash, actor, scope_key) an out-of-band approval
// grant must bind to. No secret; the payload_hash is a digest.
export async function policyApprovalBinding(request) {
  try {
    const response = await jsonPost(`${POLICY_OPERATIONS}/approval-binding`, policyOperationBody(request))
    return classifyPolicyBuild(response, 'bound', 'binding')
  } catch {
    return { status: 'unavailable', message: 'The approval surface could not be reached.' }
  }
}

// --- Configuration export / import / reset client (preferences-configuration T006.4) --
//
// The browser half of the configuration-transfer surface (workbench/api.py
// build_configuration_transfer_router at /api/configuration). Every endpoint is
// actor-scoped server-side from the trusted tailnet identity, so this client
// assembles NO actor, token, endpoint, or credential — only the safe path, method,
// and a CLOSED body (an envelope of declared fields, an optional project id, an
// optional base-version snapshot). The export it returns is already redacted and
// closed server-side; this module holds no state, so nothing is cached.
//
// The distinct server outcomes stay DISTINGUISHABLE as typed results: an invalid
// import (422 — applies nothing, repair the fields), a stale apply (409 — reload
// and preview again), and an unavailable/unconfigured surface (503 / network)
// each map to a separate `status`, so the workflows never collapse them together.

const CONFIGURATION = '/api/configuration'

async function configurationReadJson(response, failure) {
  if (response.status === 503) throw new Error('The configuration service is not configured for this hub')
  if (!response.ok) throw new Error(failure)
  return response.json()
}

// The actor's CLOSED, versioned, redacted portable-settings export:
// `{schema_version, source:{scope, actor_ref, ...}, settings:[{setting_id, scope, value}]}`.
// `projectId` is optional; it is only needed to include project-scope settings.
export async function fetchConfigurationExport(projectId) {
  const qs = projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''
  return configurationReadJson(await fetch(`${CONFIGURATION}/export${qs}`), 'The configuration export is unavailable')
}

// Classify an import/reset apply response into a typed result the UI keeps
// distinguishable. NEVER collapses invalid/stale/unavailable together.
async function classifyConfigurationApply(response, okStatus) {
  if (response.ok) {
    const body = await response.json()
    const applied = Array.isArray(body.applied) ? body.applied : []
    // `scope` is the reset's single affected scope; `scopes` is the import's set of
    // affected scope(s) so the applied result can report scope (T006.4 #3).
    return { status: okStatus, result: body, applied, appliedCount: applied.length, scope: body.scope, scopes: body.scopes }
  }
  let body = {}
  try { body = await response.json() } catch { /* non-JSON error body */ }
  const detail = body?.detail
  if (response.status === 409) {
    const isStale = detail && typeof detail === 'object' && ('reload_required' in detail || 'current_version' in detail)
    if (isStale) {
      return {
        status: 'stale',
        reloadRequired: detail.reload_required === true,
        currentVersion: Number.isInteger(detail.current_version) ? detail.current_version : null,
        message: 'The stored configuration changed elsewhere. Reload and preview again before applying.',
      }
    }
    return { status: 'invalid', message: typeof detail === 'string' ? detail : 'This change could not be applied.' }
  }
  if (response.status === 422) {
    return { status: 'invalid', message: typeof detail === 'string' ? detail : 'This import is invalid; nothing was applied.' }
  }
  if (response.status === 503) return { status: 'unavailable', message: 'The configuration service is not configured for this hub.' }
  return { status: 'unavailable', message: 'The change could not be applied.' }
}

// Preview an import as typed categories WITHOUT applying anything. Returns the
// served typed preview (`{valid, creates, changes, resets, skipped_read_only,
// unavailable_references, repairable, base_versions}`) or a distinct error status.
export async function previewConfigurationImport(envelope, { projectId } = {}) {
  const body = { envelope }
  if (projectId) body.project_id = projectId
  try {
    const response = await jsonPost(`${CONFIGURATION}/import/preview`, body)
    if (response.ok) return { status: 'previewed', preview: await response.json() }
    let error = {}
    try { error = await response.json() } catch { /* non-JSON */ }
    if (response.status === 422) return { status: 'invalid', message: typeof error?.detail === 'string' ? error.detail : 'This import could not be read.' }
    if (response.status === 503) return { status: 'unavailable', message: 'The configuration service is not configured for this hub.' }
    return { status: 'unavailable', message: 'The import could not be previewed.' }
  } catch {
    return { status: 'unavailable', message: 'The configuration service could not be reached.' }
  }
}

// Apply a previewed import. The body echoes the preview's `base_versions` so a
// store that moved since preview fails closed (stale). Returns a typed result.
export async function applyConfigurationImport(envelope, { projectId, baseVersions } = {}) {
  const body = { envelope }
  if (projectId) body.project_id = projectId
  if (baseVersions) body.base_versions = baseVersions
  try {
    return classifyConfigurationApply(await jsonPost(`${CONFIGURATION}/import/apply`, body), 'applied')
  } catch {
    return { status: 'unavailable', message: 'The configuration service could not be reached.' }
  }
}

// Preview the exact values + scope a scoped reset will change; mutate nothing.
export async function previewConfigurationReset({ scope, projectId } = {}) {
  const body = { scope }
  if (projectId) body.project_id = projectId
  try {
    const response = await jsonPost(`${CONFIGURATION}/reset/preview`, body)
    if (response.ok) return { status: 'previewed', preview: await response.json() }
    let error = {}
    try { error = await response.json() } catch { /* non-JSON */ }
    if (response.status === 422) return { status: 'invalid', message: typeof error?.detail === 'string' ? error.detail : 'This reset could not be previewed.' }
    if (response.status === 503) return { status: 'unavailable', message: 'The configuration service is not configured for this hub.' }
    return { status: 'unavailable', message: 'The reset could not be previewed.' }
  } catch {
    return { status: 'unavailable', message: 'The configuration service could not be reached.' }
  }
}

// Apply a scoped reset atomically. Echoes the preview `base_versions` for the
// version check. Returns a typed result (`reset` on success).
export async function applyConfigurationReset({ scope, projectId, baseVersions } = {}) {
  const body = { scope }
  if (projectId) body.project_id = projectId
  if (baseVersions) body.base_versions = baseVersions
  try {
    return classifyConfigurationApply(await jsonPost(`${CONFIGURATION}/reset/apply`, body), 'reset')
  } catch {
    return { status: 'unavailable', message: 'The configuration service could not be reached.' }
  }
}

// --- Reviewed plugin catalog + tool-dispatch receipts (reviewed-tools-plugins T006) --
//
// The browser half of the read-only plugin surface (workbench/api.py
// build_plugin_router at /api/plugins). Every endpoint is GET-only and redacted
// server-side on the last hop (scrub_config_payload), so this client assembles
// only the safe path — no actor, token, endpoint, or credential — and returns
// the exact wrapped shape the router serves: `{plugins}`, `{plugin}`,
// `{receipt}`. The catalog is the redacted `PluginDiscovery.published()`
// projection (approved + capability-enabled entries only); a plugin carries its
// credential handling BY REFERENCE ONLY (requirement/owner_host/credential_refs),
// never a value — the closed catalog schema makes a secret unrepresentable.
//
// The surface fails closed with 503 until a plugin host is configured (it is
// deliberately NOT wired into the live bridge poll loop); that is surfaced as a
// distinct, truthful "not configured" error so the UI renders an unconfigured
// degraded state rather than a crash. A missing plugin/receipt is a plain 404
// surfaced as a distinct not-found error (never an existence oracle). Any other
// non-2xx throws a non-leaking Error.

const PLUGINS = '/api/plugins'

// The exact 503 "not configured" sentinel the fail-closed plugin surface emits.
// It is a SHARED constant so the view can key its unconfigured-degrade branch off
// value equality rather than a private regex: rewording the message here can no
// longer silently break the view's 503 handling — both sides move together.
export const PLUGIN_NOT_CONFIGURED = 'The plugin catalog is not configured for this hub'

async function pluginJson(response, failure) {
  if (response.status === 503) throw new Error(PLUGIN_NOT_CONFIGURED)
  if (!response.ok) throw new Error(failure)
  return response.json()
}

// The approved, capability-enabled plugin catalog: `{plugins: [projection…]}`.
export async function fetchPlugins() {
  return pluginJson(await fetch(PLUGINS), 'The plugin catalog is unavailable')
}

// One approved, enabled plugin by id: `{plugin: projection}`. A 404 (unknown or
// reviewed-but-not-enabled — the same indistinct body upstream) is surfaced as a
// distinct not-found error so it never becomes an existence oracle in the UI.
export async function fetchPlugin(pluginId) {
  const response = await fetch(`${PLUGINS}/${encodeURIComponent(pluginId)}`)
  if (response.status === 404) throw new Error('This plugin is not in the reviewed catalog')
  return pluginJson(response, 'This plugin is unavailable')
}

// One stored tool-dispatch / lifecycle receipt by request digest:
// `{receipt: projection}`. A missing digest is a plain 404 surfaced as a
// distinct not-found error. The receipt reports credential use BY REFERENCE only
// and every human-readable field is a bounded safe string; no secret, endpoint,
// or path is representable.
export async function fetchPluginReceipt(requestDigest) {
  const response = await fetch(`${PLUGINS}/receipts/${encodeURIComponent(requestDigest)}`)
  if (response.status === 404) throw new Error('No receipt is stored for that request digest')
  return pluginJson(response, 'The tool receipt is unavailable')
}
