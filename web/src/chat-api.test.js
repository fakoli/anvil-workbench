import { describe, expect, it } from 'vitest'
import {
  applySnapshot,
  detectGap,
  initialStreamState,
  isStaleFrame,
  needsSnapshotRefresh,
  reduceStreamState,
} from './chat-api'

describe('sequenced stream gap detection (T008)', () => {
  it('detects a skipped frame but not a contiguous or stale one', () => {
    expect(detectGap(1, 2)).toBe(false) // contiguous
    expect(detectGap(1, 3)).toBe(true) // frame 2 dropped
    expect(detectGap(0, 5)).toBe(true) // frames 1-4 dropped
    expect(detectGap(3, 3)).toBe(false) // duplicate is stale, not a gap
    expect(detectGap(3, 2)).toBe(false) // older is stale, not a gap
    expect(detectGap(1, 1.5)).toBe(false) // non-integer -> no gap
  })

  it('flags stale/duplicate frames', () => {
    expect(isStaleFrame(3, 3)).toBe(true)
    expect(isStaleFrame(3, 2)).toBe(true)
    expect(isStaleFrame(3, 4)).toBe(false)
  })

  it('needs a snapshot refresh only on a gap', () => {
    expect(needsSnapshotRefresh(1, 3)).toBe(true)
    expect(needsSnapshotRefresh(1, 2)).toBe(false)
    expect(needsSnapshotRefresh(3, 3)).toBe(false)
  })

  it('ignores a stale frame so the response is not duplicated', () => {
    let state = initialStreamState()
    state = reduceStreamState(state, { seq: 1, kind: 'delta', text: 'A' })
    state = reduceStreamState(state, { seq: 2, kind: 'delta', text: 'B' })
    // A replayed frame 2 must not re-append 'B'.
    const replayed = reduceStreamState(state, { seq: 2, kind: 'delta', text: 'B' })
    expect(replayed.text).toBe('AB')
    expect(replayed.lastSeq).toBe(2)
  })

  it('flags a gap and resyncs to the last-committed snapshot without duplicating', () => {
    let state = initialStreamState()
    state = reduceStreamState(state, { seq: 1, kind: 'delta', text: 'A' })
    // seq 2 dropped in transit; seq 3 arrives.
    const gapped = reduceStreamState(state, { seq: 3, kind: 'delta', text: 'C' })
    expect(gapped.needsRefresh).toBe(true)
    // The gapped frame was not applied (no 'C' appended, lastSeq unchanged).
    expect(gapped.text).toBe('A')
    expect(gapped.lastSeq).toBe(1)

    // Refresh from the snapshot: adopt last-committed state + seq, no frame replay.
    const resynced = applySnapshot(gapped, {
      state: 'completed',
      is_terminal: true,
      last_committed_seq: 4,
      state_version: 2,
    })
    expect(resynced.lastSeq).toBe(4)
    expect(resynced.terminal).toBe('completed')
    expect(resynced.needsRefresh).toBe(false)
    // The response text was never duplicated by the resync.
    expect(resynced.text).toBe('A')
  })

  it('does not mark an in-progress snapshot as terminal', () => {
    // The realistic mid-stream drop: the last committed state is in_progress,
    // so applySnapshot must leave `terminal` null (a UI reading `if (terminal)`
    // must not treat a live stream as finished).
    const state = { lastSeq: 1, text: 'A', terminal: null, needsRefresh: true }
    const resynced = applySnapshot(state, {
      state: 'in_progress',
      is_terminal: false,
      last_committed_seq: 3,
      state_version: 1,
    })
    expect(resynced.lastSeq).toBe(3)
    expect(resynced.terminal).toBe(null)
    expect(resynced.needsRefresh).toBe(false)
  })
})
