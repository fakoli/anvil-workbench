import { describe, expect, it } from 'vitest'
import {
  resolvePresetView, resolveTemplateView, formatComparison,
  declaredInstructionsView, canRecordRating,
} from './advanced-playground'

// Fixtures mirror the served backend shapes (workbench/advanced_playground.py):
// a preset/template resolution `{status, ...}` and an advanced-comparison.v1 record.

describe('resolvePresetView (T006 drift opens repair, never a substitute)', () => {
  it('projects a ready resolution with the selectable preset', () => {
    const view = resolvePresetView({ status: 'ready', preset: { preset_id: 'advpreset_x', name: { text: 'Fast' } } })
    expect(view.repairRequired).toBe(false)
    expect(view.preset.preset_id).toBe('advpreset_x')
    expect(view.drifted).toEqual([])
  })

  it('opens repair mode on drift and exposes NO selectable/substituted preset', () => {
    const view = resolvePresetView({
      status: 'repair_required', preset_id: 'advpreset_x',
      drifted_refs: [{ ref_kind: 'tool', id: 'echo_fixture', pinned_digest: 'sha256:' + 'c'.repeat(64) }],
    })
    expect(view.repairRequired).toBe(true)
    expect(view.preset).toBeNull() // never a silent substitution
    expect(view.drifted).toEqual([{ refKind: 'tool', id: 'echo_fixture', pinnedDigest: 'sha256:' + 'c'.repeat(64) }])
  })
})

describe('resolveTemplateView (T009 drift/removal opens repair)', () => {
  it('is ready when the pin matches', () => {
    const view = resolveTemplateView({ status: 'ready', template: { template_id: 'strict_reviewer' } })
    expect(view.repairRequired).toBe(false)
    expect(view.template.template_id).toBe('strict_reviewer')
  })

  it('opens repair with no template on a removed/drifted pin', () => {
    const removed = resolveTemplateView({ status: 'repair_required', template_id: 'strict_reviewer', reason: 'removed', drifted_refs: [] })
    expect(removed.repairRequired).toBe(true)
    expect(removed.template).toBeNull()
    expect(removed.reason).toBe('removed')
  })
})

describe('formatComparison (T006 factual, no winner without a declared criterion)', () => {
  const attempts = [
    { turn_id: 'turn_' + 'a'.repeat(10), route: { route_id: 'route.chat-fast' }, status: 'complete', metrics: { output_tokens: 200, latency_ms: 850 } },
    { turn_id: 'turn_' + 'b'.repeat(10), route: { route_id: 'route.chat-heavy' }, status: 'complete', metrics: { output_tokens: 512, latency_ms: 2600 } },
  ]

  it('shows FACTUAL metrics and NO winner when no criterion is declared', () => {
    const view = formatComparison({ attempts })
    expect(view.available).toBe(true)
    expect(view.hasWinner).toBe(false)
    expect(view.ranking).toBeNull()
    expect(view.attempts[0].metrics.output_tokens).toBe(200)
    expect(view.attempts.every((attempt) => attempt.rank === null)).toBe(true)
  })

  it('shows a ranking ONLY with a declared non-qualification criterion', () => {
    const view = formatComparison({
      attempts,
      criterion: { criterion_id: 'instruction_following', label: { text: 'Instruction following' }, non_qualification: true },
      ranking: [{ turn_id: attempts[0].turn_id, rank: 1 }, { turn_id: attempts[1].turn_id, rank: 2 }],
    })
    expect(view.hasWinner).toBe(true)
    expect(view.criterion.label).toBe('Instruction following')
    expect(view.criterion.nonQualification).toBe(true)
    expect(view.attempts.find((attempt) => attempt.turnId === attempts[0].turn_id).rank).toBe(1)
  })

  it('drops a ranking that arrives WITHOUT a criterion (never an inferred winner)', () => {
    const view = formatComparison({ attempts, ranking: [{ turn_id: attempts[0].turn_id, rank: 1 }] })
    expect(view.hasWinner).toBe(false)
    expect(view.ranking).toBeNull()
  })
})

describe('declaredInstructionsView (T009 declared, visible)', () => {
  it('exposes the resolved text + named bindings marked declared', () => {
    const view = declaredInstructionsView({
      provenance: 'declared', template_id: 'strict_reviewer', text: 'Review the PR carefully.',
      substitutions: [{ name: 'target', value: 'PR' }],
    })
    expect(view.available).toBe(true)
    expect(view.provenance).toBe('declared')
    expect(view.text).toBe('Review the PR carefully.')
    expect(view.substitutions).toEqual([{ name: 'target', value: 'PR' }])
  })
})

describe('canRecordRating (T010 criterion-required)', () => {
  it('is false without a declared criterion', () => {
    expect(canRecordRating({ criterionId: '', score: 4 })).toBe(false)
  })
  it('is false for an out-of-range score', () => {
    expect(canRecordRating({ criterionId: 'latency', score: 9 })).toBe(false)
  })
  it('is true with a declared criterion and an in-range score', () => {
    expect(canRecordRating({ criterionId: 'latency', score: 4 })).toBe(true)
  })
})
