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
  return { lastSeq: 0, text: '', terminal: null, needsRefresh: false }
}

// Pure reducer for one arriving sequenced frame. A stale frame is ignored (no
// duplication); a gap flags `needsRefresh` without applying the frame; a
// contiguous frame advances `lastSeq` and appends delta text / records the
// terminal.
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
