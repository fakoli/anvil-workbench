import { describe, expect, it } from 'vitest'
import {
  applySnapshot,
  detectGap,
  initialStreamState,
  isStaleFrame,
  needsSnapshotRefresh,
  reduceStreamState,
  routeProvenanceLabel,
  isRouteDiverged,
  divergenceAnnouncement,
} from './chat-api'

describe('route-resolution divergence surface (T010)', () => {
  it('folds a Serving-reported resolution frame onto the stream state', () => {
    let state = initialStreamState()
    expect(state.routeResolution).toBe(null)
    const resolution = { requested_route: 'route.fast', served_route: 'route.heavy', provenance: 'explicit', diverged: true, episode_id: 'ep_1' }
    state = reduceStreamState(state, { seq: 1, kind: 'resolution', resolution })
    // Surface-only: the reducer records EXACTLY what Serving reported.
    expect(state.routeResolution).toEqual(resolution)
    expect(state.routeResolution.served_route).toBe('route.heavy')
  })

  it('labels explicit vs preference-defaulted provenance and nothing when unreported', () => {
    expect(routeProvenanceLabel({ provenance: 'explicit' })).toBe('Explicitly selected route')
    expect(routeProvenanceLabel({ provenance: 'preference_default' })).toBe('Defaulted from preference')
    expect(routeProvenanceLabel({ provenance: null })).toBe(null)
    expect(routeProvenanceLabel(null)).toBe(null)
  })

  it('announces a divergence episode exactly once (idempotent per episode)', () => {
    const diverged = { requested_route: 'route.fast', served_route: 'route.heavy', diverged: true, episode_id: 'ep_1' }
    expect(isRouteDiverged(diverged)).toBe(true)
    const first = divergenceAnnouncement([], diverged)
    expect(first.episodeId).toBe('ep_1')
    expect(first.message).toContain('route.fast → route.heavy')
    // The SAME episode is not announced again.
    expect(divergenceAnnouncement(['ep_1'], diverged)).toBe(null)
    // A non-diverged turn never announces.
    expect(divergenceAnnouncement([], { diverged: false })).toBe(null)
  })
})

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
