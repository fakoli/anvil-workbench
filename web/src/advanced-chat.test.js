import { describe, expect, it } from 'vitest'
import {
  selectAdvancedRoute, controlDescriptors, initialControlValues, checkControlValue,
  previewRouteChange, valuesForRoute, coerceControlValue, submittedControls,
  branchOps, BRANCH_OPS, isBranchSettled, newBranch, formatTrace, formatTraceEvent, shortDigest,
  traceStatusLabel, eventKindLabel,
} from './advanced-chat'

// Fixtures mirror `AdvancedRouteCapability.browser_projection()` / `control_view()`
// (workbench/advanced_routes.py): int with bounds, enum with allowed_values, and a
// policy-owned bool the operator cannot override.
const fastRoute = {
  route_id: 'route.chat-fast', display_name: 'Chat fast',
  controls: [
    { name: 'temperature_milli', type: 'int', default: 300, editable: true, source: 'route_default', bounds: { min: 0, max: 1000 } },
    { name: 'reasoning_effort', type: 'enum', default: 'low', editable: true, source: 'route_default', allowed_values: ['low', 'medium', 'high'] },
    { name: 'response_streaming', type: 'bool', default: true, editable: false, source: 'policy_owned', disabled_reason: 'policy_owned' },
  ],
}
const heavyRoute = {
  route_id: 'route.chat-heavy', display_name: 'Chat heavy',
  controls: [
    { name: 'temperature_milli', type: 'int', default: 200, editable: true, source: 'route_default', bounds: { min: 0, max: 500 } },
    { name: 'max_output_tokens', type: 'int', default: 1024, editable: true, source: 'route_default', bounds: { min: 1, max: 4096 } },
  ],
}
const routes = [fastRoute, heavyRoute]

describe('advanced route selection + controls (T005)', () => {
  it('refuses a route id outside the reviewed allowlist (closed set)', () => {
    expect(selectAdvancedRoute(routes, 'route.chat-fast').route_id).toBe('route.chat-fast')
    expect(() => selectAdvancedRoute(routes, 'route.smuggled')).toThrow('reviewed allowlist')
  })

  it('seeds control values from the served defaults', () => {
    expect(initialControlValues(fastRoute)).toEqual({ temperature_milli: 300, reasoning_effort: 'low', response_streaming: true })
    expect(controlDescriptors(fastRoute)).toHaveLength(3)
    expect(controlDescriptors({})).toEqual([])
  })

  it('checks a value against its declared type/bounds/allowed set (mirrors check_value)', () => {
    const [temp, effort, streaming] = fastRoute.controls
    expect(checkControlValue(temp, 300)).toBeNull()
    expect(checkControlValue(temp, 2000)).toBe('out_of_bounds')
    expect(checkControlValue(temp, 'x')).toBe('type')
    expect(checkControlValue(temp, true)).toBe('type') // a bool is not an int
    expect(checkControlValue(effort, 'high')).toBeNull()
    expect(checkControlValue(effort, 'extreme')).toBe('not_allowed')
    expect(checkControlValue(streaming, true)).toBeNull()
    expect(checkControlValue(streaming, 'yes')).toBe('type')
    expect(checkControlValue(undefined, 1)).toBe('unsupported')
  })

  it('coerces raw form values into the declared type', () => {
    expect(coerceControlValue(fastRoute.controls[0], '450')).toBe(450)
    expect(coerceControlValue(fastRoute.controls[2], true)).toBe(true)
    expect(coerceControlValue(fastRoute.controls[1], 'medium')).toBe('medium')
  })
})

describe('stale-on-route-change preview (criterion 1)', () => {
  it('reports which tuned values become stale BEFORE they are dropped', () => {
    // temperature 800 is valid on fast but out of heavy's [0,500]; reasoning_effort
    // is unsupported on heavy entirely.
    const current = { temperature_milli: 800, reasoning_effort: 'high', response_streaming: true }
    const { carried, stale } = previewRouteChange(current, heavyRoute)
    const byName = Object.fromEntries(stale.map((s) => [s.name, s]))
    expect(byName.temperature_milli.reason).toBe('out_of_bounds')
    expect(byName.temperature_milli.value).toBe(800) // the value is preserved for display pre-drop
    expect(byName.reasoning_effort.reason).toBe('unsupported')
    // response_streaming echoed its default: it neither carries nor is stale.
    expect(carried).toEqual({}) // nothing carried (temp out of bounds, effort unsupported)
  })

  it('carries a compatible value across a route change', () => {
    const { carried, stale } = previewRouteChange({ temperature_milli: 250 }, heavyRoute)
    expect(carried).toEqual({ temperature_milli: 250 })
    expect(stale).toEqual([])
  })

  it('flags a carried non-default onto a policy-owned control as stale (policy_owned)', () => {
    const policyRoute = { route_id: 'r', controls: [{ name: 'temperature_milli', type: 'int', default: 100, editable: false, source: 'policy_owned', bounds: { min: 0, max: 1000 } }] }
    const { stale } = previewRouteChange({ temperature_milli: 900 }, policyRoute)
    expect(stale[0]).toEqual({ name: 'temperature_milli', value: 900, reason: 'policy_owned' })
  })

  it('merges carried values onto the next route defaults', () => {
    expect(valuesForRoute(heavyRoute, { temperature_milli: 250 })).toEqual({ temperature_milli: 250, max_output_tokens: 1024 })
  })
})

describe('submitted controls builder (mirrors AdvancedRouteSelection.submitted_controls)', () => {
  it('emits declared provenance for editable controls and policy_override for policy-owned', () => {
    const submitted = submittedControls(fastRoute, { temperature_milli: 450, reasoning_effort: 'high', response_streaming: true })
    // Sorted by name, canonical.
    expect(submitted).toEqual([
      { name: 'reasoning_effort', value: 'high', provenance: 'declared' },
      { name: 'response_streaming', value: true, provenance: 'policy_override' }, // echoes the DEFAULT, never a crafted override
      { name: 'temperature_milli', value: 450, provenance: 'declared' },
    ])
  })

  it('forces a policy-owned control to its declared default even if a caller supplies another value', () => {
    const submitted = submittedControls(fastRoute, { response_streaming: false })
    const streaming = submitted.find((entry) => entry.name === 'response_streaming')
    expect(streaming).toEqual({ name: 'response_streaming', value: true, provenance: 'policy_override' })
  })
})

describe('branch operation state machine (criterion 2)', () => {
  it('exposes only the operations available from a branch status', () => {
    const draft = newBranch({ id: 'b1', routeId: 'route.chat-fast', controls: {}, prompt: 'x' })
    expect(draft.status).toBe('draft')
    // A not-yet-settled branch offers none of the settled operations, and the
    // dead `run`/`cancel` surface is gone (never bound to a rendered control).
    const draftOps = branchOps(draft, { streaming: false })
    expect(draftOps.run).toBeUndefined()
    expect(draftOps.cancel).toBeUndefined()
    expect(draftOps.retry).toBe(false)
    expect(BRANCH_OPS).not.toContain('run')
    expect(BRANCH_OPS).not.toContain('cancel')

    const streaming = { ...draft, status: 'streaming' }
    expect(branchOps(streaming).retry).toBe(false) // not settled yet
    expect(branchOps(streaming).inspect).toBe(false) // no trace yet

    const settled = { ...draft, status: 'complete', trace: { schema_version: 'workbench-advanced-trace/v1' } }
    expect(isBranchSettled(settled)).toBe(true)
    const ops = branchOps(settled, { settledCount: 2, streaming: false })
    expect(ops.retry).toBe(true)
    expect(ops.fork).toBe(true)
    expect(ops.compare).toBe(true) // needs >= 2 settled
    expect(ops.inspect).toBe(true) // has a trace
    expect(ops.save).toBe(true)
    // Retry/fork are disabled while another stream is in flight (one at a time).
    expect(branchOps(settled, { settledCount: 2, streaming: true }).retry).toBe(false)
    // Compare disabled when only one settled branch exists.
    expect(branchOps(settled, { settledCount: 1 }).compare).toBe(false)
    // A saved branch offers reopen and not save.
    const saved = { ...settled, saved: true }
    expect(branchOps(saved).reopen).toBe(true)
    expect(branchOps(saved).save).toBe(false)
  })
})

describe('advanced-trace.v1 inspector projection (redaction gate)', () => {
  const trace = {
    schema_version: 'workbench-advanced-trace/v1', trace_id: 'advtrace_x',
    branch_ref: { branch_id: 'advbranch_x', conversation_id: 'conv_x', turn_id: 'turn_x' },
    route_decision: { provider: 'anvil-serving', route_id: 'route.chat-fast', route_digest: 'sha256:' + 'a'.repeat(64), profile_digest: 'sha256:' + 'b'.repeat(64), model_profile: 'chat-fast', request_id: 'req_1' },
    request: { content_trust: 'untrusted_task_data', redacted: true, input_chars: 42, structured_output_mode: 'text', control_values: [{ name: 'temperature_milli', value: 300 }] },
    events: [
      { seq: 0, kind: 'request_start', at: 't0' },
      { seq: 1, kind: 'tool_result', at: 't1', tool_id: 'echo', tool_kind: 'mock', output_digest: 'sha256:' + 'c'.repeat(64), output_chars: 32, safe_summary: 'bounded result' },
      { seq: 2, kind: 'response_complete', at: 't2', usage: { input_tokens: 12, output_tokens: 20, latency_ms: 100 } },
    ],
    usage: { input_tokens: 12, output_tokens: 20, latency_ms: 100 },
    status: 'complete', redaction: { status: 'redacted', ruleset: 'advanced-trace-v1' },
  }

  it('projects ids, digests, counters, and safe summaries — never a raw output field', () => {
    const model = formatTrace(trace)
    expect(model.available).toBe(true)
    expect(model.statusLabel).toBe('Completed')
    expect(model.routeDecision.routeDigest).toBe('sha256:' + 'a'.repeat(12) + '…')
    expect(model.request.inputChars).toBe(42)
    expect(model.request.controlValues).toEqual([{ name: 'temperature_milli', value: 300 }])
    const toolEvent = model.events.find((event) => event.kind === 'tool_result')
    expect(toolEvent.summary).toBe('bounded result')
    // The event exposes only whitelisted digest/counter fields — no raw output key.
    const keys = toolEvent.fields.map(([key]) => key)
    expect(keys).toContain('output digest')
    expect(keys).toContain('output chars')
    // Serialize the projection: no raw-output / endpoint / secret leaks through.
    const serialized = JSON.stringify(model)
    expect(serialized).not.toMatch(/payload_json/)
    expect(serialized).not.toMatch(/https?:\/\//)
  })

  it('abbreviates digests and degrades a missing trace truthfully', () => {
    expect(shortDigest('sha256:' + 'd'.repeat(64))).toBe('sha256:' + 'd'.repeat(12) + '…')
    expect(shortDigest(undefined)).toBe('')
    expect(formatTrace(null)).toEqual({ available: false })
    expect(traceStatusLabel('serving_unavailable')).toBe('Serving unavailable')
    expect(eventKindLabel('tool_refusal')).toBe('Tool refused')
  })

  it('formats a single error event with its typed code, not a raw message', () => {
    const errorEvent = formatTraceEvent({ seq: 3, kind: 'error', at: 't', error: { code: 'serving_timeout', retryable: true } })
    expect(errorEvent.label).toBe('Error')
    expect(errorEvent.fields).toEqual(expect.arrayContaining([['error code', 'serving_timeout'], ['retryable', 'yes']]))
  })
})
