// Gap-detectable stream sequence helper (chat-first-voice T008).
//
// The hub stamps every streamed frame with a strictly-monotonic per-conversation
// `seq` and tracks the last-committed seq + state on the response lifecycle
// record. These pure functions are the client half: they let a reconnecting
// client detect a dropped frame and decide to refresh from the last-committed
// snapshot WITHOUT re-applying frames or duplicating the response. They mirror
// `workbench/stream_sequence.py` byte-for-byte so client and hub agree.

function isInt(value) {
  return Number.isInteger(value)
}

// True when `frameSeq` skips past the next expected frame (a drop). A
// strictly-monotonic stream delivers `lastSeq + 1` next; anything higher means
// at least one frame was dropped in transit.
export function detectGap(lastSeq, frameSeq) {
  if (!isInt(lastSeq) || !isInt(frameSeq)) return false
  return frameSeq > lastSeq + 1
}

// True when `frameSeq` is at or below the last seen seq (stale/duplicate). A
// stale frame carries no forward progress; ignore it so a replayed frame never
// duplicates the response.
export function isStaleFrame(lastSeq, frameSeq) {
  if (!isInt(lastSeq) || !isInt(frameSeq)) return false
  return frameSeq <= lastSeq
}

// True when a detected gap means the client must refresh from the snapshot to
// resync without duplicating the response. A contiguous or stale frame does not.
export function needsSnapshotRefresh(lastSeq, frameSeq) {
  return detectGap(lastSeq, frameSeq)
}

// The client's initial stream-consumption state.
export function initialStreamState() {
  return { lastSeq: 0, text: '', terminal: null, needsRefresh: false, routeResolution: null }
}

// Pure reducer for one arriving sequenced frame. A stale frame is ignored (no
// duplication); a gap flags `needsRefresh` without applying the frame; a
// contiguous frame advances `lastSeq` and appends delta text / records the
// terminal / records the SURFACE-ONLY route-resolution mark Serving reported.
export function reduceStreamState(state, frame) {
  if (isStaleFrame(state.lastSeq, frame.seq)) {
    return state // ignore a stale/duplicate frame -> no duplicated response
  }
  if (detectGap(state.lastSeq, frame.seq)) {
    return { ...state, needsRefresh: true } // missed frames -> must snapshot-refresh
  }
  const next = { ...state, lastSeq: frame.seq, needsRefresh: false }
  if (frame.kind === 'delta') next.text = state.text + (frame.text || '')
  if (frame.kind === 'terminal') next.terminal = frame.outcome ?? null
  // A `resolution` frame carries the route-resolution mark Serving REPORTED
  // (requested vs served route + provenance). The client only records it; it
  // never picks a route (chat-first-voice T010 / no failover in Workbench).
  if (frame.kind === 'resolution') next.routeResolution = frame.resolution ?? null
  return next
}

// Resync to the last-committed server snapshot after a detected gap. The client
// jumps `lastSeq` to the committed position and adopts the committed state
// WITHOUT re-applying the dropped frames, so the response is not duplicated.
export function applySnapshot(state, snapshot) {
  // `terminal` mirrors reduceStreamState's outcome vocabulary: it is set only
  // when the committed snapshot is actually terminal. A non-terminal (e.g.
  // in_progress) snapshot must NOT populate `terminal`, or a UI reading
  // `if (state.terminal)` would treat a live stream as finished.
  return {
    ...state,
    lastSeq: snapshot.last_committed_seq,
    terminal: snapshot.is_terminal ? snapshot.state : null,
    needsRefresh: false,
  }
}

// --- Owner-facing display helpers (chat-first-voice T004.1) ------------------
//
// Pure projections used by the browser client and the rail/transcript UI. They
// live here (not in api.js) so they stay real logic when a component test mocks
// the network client `./api`. None of them touches fetch, a token, or an
// endpoint.

// The chat-turn.v1 relay outcomes (chat_stream.StreamOutcome) mapped to the
// lifecycle status a turn is rendered with. Only a genuine completion reads as
// `complete`; a cancelled, timed-out, or unavailable stream (even one that
// carried partial text) is never rendered as a completed response. A missing or
// unknown terminal is treated as interrupted, never silently complete.
const OUTCOME_TO_STATUS = {
  completed: 'complete',
  complete: 'complete',
  cancelled: 'cancelled',
  timed_out: 'interrupted',
  interrupted: 'interrupted',
  serving_unavailable: 'failed',
  failed: 'failed',
}

export function terminalToStatus(terminal) {
  if (!terminal) return 'interrupted'
  return OUTCOME_TO_STATUS[terminal] || 'interrupted'
}

// Prioritize the human-facing title and lifecycle state over identifiers: the
// rail and transcript read `title` + `state` first and expose the canonical
// `id` only as secondary disclosure. A titleless conversation gets a truthful
// readable fallback rather than surfacing its opaque id as a heading.
export function describeConversation(record) {
  const rawTitle = record && typeof record.title === 'string' ? record.title.trim() : ''
  const archived = record?.status === 'archived'
  const deleted = Boolean(record?.deletion)
  return {
    title: rawTitle || 'Untitled conversation',
    state: deleted ? 'deleted' : archived ? 'archived' : 'active',
    archived,
    deleted,
    ephemeral: Boolean(record?.ephemeral),
    pinned: Boolean(record?.pinned),
    tags: Array.isArray(record?.tags) ? record.tags : [],
    folder: record?.folder ?? null,
    updatedAt: record?.updated_at ?? null,
    // Canonical id is carried, but as the LAST field: it is secondary
    // disclosure, never the primary label a human reads.
    id: record?.id ?? null,
  }
}

// Closed-set guard over the reviewed chat-route allowlist (chat_routes.py
// `as_dict`). A route id that is not one of the reviewed descriptors is refused
// before any send, so a smuggled or undeclared route can never reach Serving —
// adding a route to the UI cannot bypass the allowlist.
export function selectChatRoute(routes, routeId) {
  const match = (Array.isArray(routes) ? routes : []).find((route) => route.route_id === routeId)
  if (!match) throw new Error(`chat route is not in the reviewed allowlist: ${routeId}`)
  return match
}

// --- Route-resolution divergence (chat-first-voice T010) ---------------------
//
// SURFACE-ONLY display projections over the route-resolution mark Serving reports
// (workbench/router.py `route_resolution`): whether a turn's route was EXPLICITLY
// selected or DEFAULTED from a preference, and whether Serving served a DIFFERENT
// route than requested. These NEVER pick a route — Workbench performs no failover
// and no retry-to-alternate — they only project what Serving actually resolved.

// The human-facing chip distinguishing an explicitly-selected route from a
// preference-defaulted one, read from Serving's reported provenance. An
// unreported provenance shows nothing rather than guessing.
export function routeProvenanceLabel(resolution) {
  switch (resolution?.provenance) {
    case 'explicit': return 'Explicitly selected route'
    case 'preference_default': return 'Defaulted from preference'
    default: return null
  }
}

// True when Serving reported it served a different route than requested. Read
// straight off the served mark; the client never computes a substitute route.
export function isRouteDiverged(resolution) {
  return Boolean(resolution?.diverged)
}

// The ONCE-PER-EPISODE announcement decision. Given the list of already-announced
// divergence episode ids and an arriving resolution, returns the notice to show
// or `null` when the turn did NOT diverge OR its episode was already announced —
// so the divergence notice is idempotent per episode and never re-shown each turn.
export function divergenceAnnouncement(announcedEpisodeIds, resolution) {
  if (!isRouteDiverged(resolution)) return null
  const episodeId = resolution?.episode_id ?? null
  const seen = Array.isArray(announcedEpisodeIds) ? announcedEpisodeIds : []
  if (episodeId && seen.includes(episodeId)) return null
  const requested = resolution?.requested_route || 'the requested route'
  const served = resolution?.served_route || 'another route'
  const reason = typeof resolution?.divergence_reason === 'string' && resolution.divergence_reason
    ? ` ${resolution.divergence_reason}` : ''
  return {
    episodeId,
    message: `Serving served a different route than requested (${requested} → ${served}).${reason}`,
  }
}

// Normalize the text of a rendered turn into the EXACT content-block shape the
// server's `ContentBlockInput` accepts (`extra="forbid"`, `kind` required):
// `{kind: 'text', text}` only. This strips every other field — notably the
// server-projected `content_trust` (which a server-loaded turn carries and
// which the append models forbid) — and supplies a `kind` for a locally
// streamed block that has only `{text}`. Posting a block verbatim off a
// `getConversation` read or a local stream is otherwise rejected 422
// (extra_forbidden `content_trust`, or missing `kind`).
export function toTurnContent(blocks) {
  return (Array.isArray(blocks) ? blocks : [])
    .map((block) => ({ kind: 'text', text: typeof block?.text === 'string' ? block.text : '' }))
}

// Build the caller-supplied slice of a retry/branch successor turn in the shape
// `TurnBodyInput` requires (workbench/conversation_api.py:152). The server
// derives the lineage (kind/parent/sibling) itself, so the body carries only
// `role`, `status`, `mode`, and normalized `content`. The role differs by
// operation to match the server's turn tree (test_conversation_api.py:140-155):
// a `retry` appends a sibling ASSISTANT regeneration of the retried answer; a
// `branch` opens a new USER turn continuing from that answer. Neither reposts a
// server-loaded block verbatim (that leaked `content_trust` → 422).
export function successorTurnBody(turn, { role, mode = 'ordinary' } = {}) {
  return { role, status: 'complete', mode, content: toTurnContent(turn?.content) }
}

// --- Voice push-to-talk state machine (chat-first-voice T005.2) ---------------
//
// Pure record/draft logic, kept here (not in App.jsx) so it stays real when a
// component test mocks the network. The invariant the machine encodes: capturing
// audio produces an EDITABLE DRAFT and NEVER a sent turn. Recording moves
// ready → listening → transcribing → draft; the draft is then editable text the
// actor must EXPLICITLY submit through the ordinary composer. No transition here
// sends a turn, and a permission/STT failure is a non-blocking 'error' that
// leaves the textual composer fully usable.

// "connected" criterion NOTE: this push-to-talk surface is request/response STT
// over the hub relay, NOT a persistent socket, so there is deliberately no
// "connected" state. The acceptance word "connected" maps to `ready` (the mic
// path is available and idle) and `listening` (a hold is actively capturing); a
// hop is a bounded per-request call, not a held connection. This is distinct from
// the Realtime websocket relay (RealtimeVoice), which is genuinely connection-based.
export const VOICE_INPUT_STATES = Object.freeze(['ready', 'listening', 'transcribing', 'draft', 'error'])

export function initialVoiceInputState() {
  return { status: 'ready', interim: '', draft: '', isFinal: false, error: '' }
}

export function voiceInputReducer(state, action) {
  switch (action?.type) {
    case 'press':
      // Hold-to-talk begins: a fresh capture, clearing any prior draft/error.
      return { status: 'listening', interim: '', draft: '', isFinal: false, error: '' }
    case 'interim':
      // Interim captions only render while listening; they are not a committable
      // draft and never a turn.
      return state.status === 'listening' ? { ...state, interim: String(action.text ?? '') } : state
    case 'release':
      // Release ends capture and awaits the final transcript.
      return state.status === 'listening' ? { ...state, status: 'transcribing', interim: state.interim } : state
    case 'final':
      // The interim captions settle into an EDITABLE final draft. Still no turn.
      return { ...state, status: 'draft', draft: String(action.text ?? ''), interim: '', isFinal: true, error: '' }
    case 'edit':
      // The actor edits the reviewed draft before deciding to submit.
      return state.status === 'draft' ? { ...state, draft: String(action.text ?? '') } : state
    case 'error':
      // Permission denial or an STT failure is non-blocking: surfaced as text,
      // the textual composer stays usable.
      return { ...initialVoiceInputState(), status: 'error', error: String(action.message ?? 'Voice input failed.') }
    case 'reset':
      return initialVoiceInputState()
    default:
      return state
  }
}

// Human-readable, closed-set live-region label for each voice-input status.
export function voiceInputLabel(state) {
  switch (state?.status) {
    case 'listening': return 'Listening — release to review your transcript'
    case 'transcribing': return 'Transcribing your recording…'
    case 'draft': return 'Transcript ready to review, edit, and send'
    case 'error': return state.error || 'Voice input failed.'
    default: return 'Voice input ready'
  }
}

// True only when a reviewed final draft exists to submit. A turn is sent through
// the ordinary composer path; this never sends one itself.
export function voiceDraftReady(state) {
  return state?.status === 'draft' && typeof state.draft === 'string' && state.draft.trim().length > 0
}

// --- Voice read-aloud (TTS) playback state machine (chat-first-voice T005.3) --
//
// Pure playback logic. The invariant: playback and captions NEVER change message
// or conversation state — this machine tracks only transient audio status keyed
// to a message ref. start / pause / resume / stop / replay / interrupt all move
// only playback status; none touches a turn.

export const PLAYBACK_STATES = Object.freeze(['idle', 'loading', 'playing', 'paused', 'stopped', 'error'])

export function initialPlaybackState() {
  return { status: 'idle', messageRef: null, error: '' }
}

export function playbackReducer(state, action) {
  switch (action?.type) {
    case 'load':
      // Begin fetching audio for one message. Switching messages interrupts any
      // prior playback cleanly.
      return { status: 'loading', messageRef: action.messageRef ?? null, error: '' }
    case 'play':
      return { ...state, status: 'playing', error: '' }
    case 'pause':
      return state.status === 'playing' ? { ...state, status: 'paused' } : state
    case 'resume':
      return state.status === 'paused' ? { ...state, status: 'playing' } : state
    case 'stop':
      return state.status === 'idle' ? state : { ...state, status: 'stopped' }
    case 'ended':
      // Natural completion returns to idle so the same message can replay.
      return { ...state, status: 'idle' }
    case 'replay':
      return { status: 'playing', messageRef: action.messageRef ?? state.messageRef, error: '' }
    case 'interrupt':
      // A hard interrupt (new playback, navigation) resets cleanly.
      return initialPlaybackState()
    case 'error':
      return { ...state, status: 'error', error: String(action.message ?? 'Audio playback failed.') }
    default:
      return state
  }
}

// Closed-set live-region label for playback status.
export function playbackLabel(state) {
  switch (state?.status) {
    case 'loading': return 'Preparing audio…'
    case 'playing': return 'Playing audio'
    case 'paused': return 'Audio paused'
    case 'stopped': return 'Audio stopped'
    case 'error': return state.error || 'Audio playback failed.'
    default: return 'Audio idle'
  }
}

// Whether a given message is the one currently loading/playing/paused.
export function isPlaybackActiveFor(state, messageRef) {
  return Boolean(messageRef) && state?.messageRef === messageRef
    && ['loading', 'playing', 'paused'].includes(state.status)
}

// Autoplay is OPTIONAL and follows the operator's saved preference when one is
// available; absent a preference it stays off (never surprises the operator).
export function shouldAutoplay(preferences) {
  return preferences?.voice_autoplay === true
}

// Adapter: extract the effective `personal.voice_autoplay` value from the REAL
// served `/api/preferences` payload into the `{voice_autoplay: boolean}` shape
// `shouldAutoplay` reads. The served payload is `{catalog, effective:[…]}` where
// each effective row is `{setting_id, scope, value, source, …}` (the resolver's
// `EffectiveValue.as_dict()`), so this finds the row whose `setting_id` is
// `personal.voice_autoplay` and reports its resolved value. Absent row, a
// non-boolean value, or a missing/503/unconfigured surface all default OFF — the
// operator is never surprised by autoplay they did not opt into.
export const VOICE_AUTOPLAY_SETTING_ID = 'personal.voice_autoplay'

export function voiceAutoplayFromPreferences(payload) {
  const rows = Array.isArray(payload?.effective) ? payload.effective : []
  const row = rows.find((entry) => entry?.setting_id === VOICE_AUTOPLAY_SETTING_ID)
  return { voice_autoplay: row?.value === true }
}
