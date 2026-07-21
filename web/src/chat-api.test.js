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
      last_committed_seq: 4,
      state_version: 2,
    })
    expect(resynced.lastSeq).toBe(4)
    expect(resynced.terminal).toBe('completed')
    expect(resynced.needsRefresh).toBe(false)
    // The response text was never duplicated by the resync.
    expect(resynced.text).toBe('A')
  })
})
