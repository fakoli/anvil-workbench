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
  return {
    ...state,
    lastSeq: snapshot.last_committed_seq,
    terminal: snapshot.state,
    needsRefresh: false,
  }
}
