// Pure Advanced-mode chat logic (advanced-model-playground T005).
//
// The browser-side, network-free half of the Advanced playground. It mirrors the
// merged backend contracts BYTE-FOR-SHAPE so the rendered controls, the stale-on-
// route-change preview, the branch operation state machine, and the inspector
// projection all reason over EXACTLY the shapes the hub serves — never an invented
// field:
//
//  * A route + its supported controls is the `AdvancedRouteCapability.browser_projection()`
//    shape (workbench/advanced_routes.py): `{provider, route_id, display_name,
//    route_digest, profile_digest, serving_contract_version, model_profile,
//    structured_output_supported, tools_supported, controls:[control_view…]}`, and
//    each control is the `control_view()` shape `{name, type, default, editable,
//    source, disabled_reason, bounds?, allowed_values?}` with `type ∈ int|enum|bool`.
//  * A submitted selection is the `AdvancedRouteSelection.submitted_controls()` array
//    `[{name, value, provenance}]`; a policy-owned control may only echo its default
//    with `policy_override` provenance — mirroring `validate_advanced_selection`.
//  * The inspector renders the `advanced-trace.v1` record (contracts.validate_advanced_trace):
//    ids/digests, bounded counters, safe summaries, and per-event digests ONLY —
//    never raw tool output, a credential, an endpoint, or a path.
//
// Nothing here touches fetch, a token, or an endpoint; the controls are a CLOSED
// set derived from the served route, so the browser can never mint a new control,
// a free-text command, or a host:port.

// The three declared control types (advanced_routes._CONTROL_TYPES).
export const ADVANCED_CONTROL_TYPES = Object.freeze(['int', 'enum', 'bool'])

// Closed-set guard over the reviewed advanced-route allowlist (mirrors
// chat-api.selectChatRoute). A route id that is not one of the served descriptors
// is refused before any run, so a smuggled/undeclared route can never reach the
// advanced runtime — adding a route to the UI cannot widen the allowlist.
export function selectAdvancedRoute(routes, routeId) {
  const match = (Array.isArray(routes) ? routes : []).find((route) => route.route_id === routeId)
  if (!match) throw new Error(`advanced route is not in the reviewed allowlist: ${routeId}`)
  return match
}

// The served control descriptors for one route (already the browser-safe
// control_view shape). Always an array so callers can map safely.
export function controlDescriptors(route) {
  return Array.isArray(route?.controls) ? route.controls : []
}

// The initial control values for a route: every control seeded to its declared
// default (advanced_routes descriptor.default). This is the canonical starting
// point an operator tunes from.
export function initialControlValues(route) {
  const values = {}
  for (const control of controlDescriptors(route)) values[control.name] = control.default
  return values
}

function isInteger(value) {
  return Number.isInteger(value)
}

// Validate one value against its served descriptor, returning a typed reason
// string when it is unacceptable, or `null` when it is fine. Mirrors
// `AdvancedControlDescriptor.check_value` (advanced_routes.py) so the browser
// refuses exactly what the hub would refuse — before any run request exists.
export function checkControlValue(descriptor, value) {
  if (!descriptor) return 'unsupported'
  if (descriptor.type === 'int') {
    if (typeof value === 'boolean' || !isInteger(value)) return 'type'
    const bounds = descriptor.bounds
    if (bounds && (value < bounds.min || value > bounds.max)) return 'out_of_bounds'
    return null
  }
  if (descriptor.type === 'enum') {
    const allowed = Array.isArray(descriptor.allowed_values) ? descriptor.allowed_values : []
    if (typeof value !== 'string' || !allowed.includes(value)) return 'not_allowed'
    return null
  }
  if (descriptor.type === 'bool') {
    if (typeof value !== 'boolean') return 'type'
    return null
  }
  return 'type'
}

// Human-readable, closed-set reason for a stale/unsupported control value. Used by
// the route-change preview so the operator sees WHY a value cannot carry over
// BEFORE it is dropped.
export function staleReasonLabel(reason) {
  switch (reason) {
    case 'unsupported': return 'not offered by this route'
    case 'out_of_bounds': return 'outside this route’s allowed range'
    case 'not_allowed': return 'not an allowed value for this route'
    case 'type': return 'wrong type for this route'
    case 'policy_owned': return 'fixed by policy on this route'
    default: return 'unsupported on this route'
  }
}

// Compute what happens to the CURRENT tuned values when the operator picks a
// different route — WITHOUT dropping anything yet. Returns `{carried, stale}`:
//   * `carried` — the `{name: value}` subset that is still declared, editable, and
//     valid on `nextRoute` (these survive the switch);
//   * `stale`   — `[{name, value, reason}]` for every current value that the next
//     route no longer supports, bounds-rejects, type-rejects, or fixes by policy.
// The caller renders `stale` as a PREVIEW so the operator sees exactly which of
// their tuned values will be dropped before they commit the route change — never a
// silent wipe (T005 criterion 1). Backed by the same per-value check the hub runs.
export function previewRouteChange(currentValues, nextRoute) {
  const carried = {}
  const stale = []
  const byName = new Map(controlDescriptors(nextRoute).map((control) => [control.name, control]))
  for (const [name, value] of Object.entries(currentValues || {})) {
    const descriptor = byName.get(name)
    if (!descriptor) { stale.push({ name, value, reason: 'unsupported' }); continue }
    // A policy-owned control on the next route is not operator-tunable: a carried
    // non-default value cannot ride onto it, so it is surfaced as stale.
    if (descriptor.editable === false || descriptor.source === 'policy_owned') {
      if (value !== descriptor.default) { stale.push({ name, value, reason: 'policy_owned' }); continue }
      continue // an echoed default simply defers to the policy default; nothing to carry
    }
    const reason = checkControlValue(descriptor, value)
    if (reason) { stale.push({ name, value, reason }); continue }
    carried[name] = value
  }
  return { carried, stale }
}

// Merge the carried values from a route change onto the next route's full default
// set, so every control the next route declares has a concrete value (carried
// where compatible, default otherwise). This is what the editor binds to AFTER the
// operator commits a previewed route change.
export function valuesForRoute(nextRoute, carried = {}) {
  const values = initialControlValues(nextRoute)
  for (const [name, value] of Object.entries(carried)) {
    if (name in values) values[name] = value
  }
  return values
}

// Coerce a raw form value into the type its descriptor declares, for editing. A
// range/number input yields a string; a checkbox yields a boolean; a select yields
// a string. This keeps the tuned value the correct JS type so `checkControlValue`
// and the submitted-controls builder agree with the hub.
export function coerceControlValue(descriptor, raw) {
  if (!descriptor) return raw
  if (descriptor.type === 'int') {
    const parsed = typeof raw === 'number' ? raw : parseInt(raw, 10)
    return Number.isNaN(parsed) ? raw : parsed
  }
  if (descriptor.type === 'bool') return Boolean(raw)
  return String(raw)
}

// Build the `submitted_controls` array the run request carries, mirroring
// `AdvancedRouteSelection.submitted_controls()` (advanced_routes.py). A
// policy-owned control is emitted ONLY as its declared default with
// `policy_override` provenance (the hub refuses any other value); an editable
// control carries the tuned value with `declared` provenance. The result is sorted
// by name so the selection is canonical, exactly like the backend. There is no
// path for a free-text command, credential, or host to enter here.
export function submittedControls(route, values) {
  const out = []
  for (const descriptor of controlDescriptors(route)) {
    if (descriptor.editable === false || descriptor.source === 'policy_owned') {
      out.push({ name: descriptor.name, value: descriptor.default, provenance: 'policy_override' })
    } else if (values && descriptor.name in values) {
      out.push({ name: descriptor.name, value: values[descriptor.name], provenance: 'declared' })
    }
  }
  out.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))
  return out
}

// --- Advanced branch operation state machine ---------------------------------
//
// An advanced branch is a per-experiment sibling turn on the SAME conversation
// (advanced_runtime.open_advanced_branch forks a `mode="advanced"` sibling — never
// a second conversation or transcript). This machine tracks which OPERATIONS an
// operator may perform on a branch from its current lifecycle status, so every
// rendered button reflects a genuinely-available action. The run OUTCOME statuses
// map from the served chat-turn terminal (chat-api.terminalToStatus) and the
// advanced-trace `status`.

// The closed set of branch operations the panel offers.
export const BRANCH_OPS = Object.freeze(['run', 'cancel', 'retry', 'fork', 'compare', 'inspect', 'save', 'reopen'])

// A branch is settled once its single attempt reached a terminal turn status.
const SETTLED_STATUSES = Object.freeze(['complete', 'cancelled', 'interrupted', 'failed'])

export function isBranchSettled(branch) {
  return SETTLED_STATUSES.includes(branch?.status)
}

// Which operations are available for `branch`, given the surrounding context
// (`settledCount` — how many settled branches exist for compare; `streaming` —
// whether any stream is in flight). Every panel button keys its `disabled` off
// this, so a rendered control is enabled ONLY when its effect is genuinely
// available — no dead affordance, no reducer-only op the UI never invokes.
export function branchOps(branch, { settledCount = 0, streaming = false } = {}) {
  const status = branch?.status
  const settled = isBranchSettled(branch)
  return {
    run: status === 'draft' && !streaming,
    cancel: status === 'streaming',
    retry: settled && !streaming,
    fork: settled && !streaming,
    compare: settled && settledCount >= 2,
    inspect: Boolean(branch?.trace),
    save: settled && branch?.saved !== true,
    reopen: branch?.saved === true && !streaming,
  }
}

// A fresh, not-yet-run branch descriptor bound to a route selection.
export function newBranch({ id, routeId, controls, prompt = '', instructions = '', label }) {
  return {
    id, routeId, controls, prompt, instructions,
    label: label || 'Branch',
    status: 'draft', text: '', trace: null, turnId: null, branchId: null, saved: false,
  }
}

// --- Inspector: the redacted advanced-trace.v1 projection --------------------
//
// The inspector renders `formatTrace(trace)`. It is DELIBERATELY a whitelist: it
// surfaces ONLY the ids/digests, bounded counters, declared control values, safe
// summaries, and per-event digests the closed advanced-trace.v1 schema carries.
// There is no branch that reads a raw response, a raw tool output, a header, a
// credential, an endpoint, or a filesystem path — the schema cannot represent one,
// and this projection never invents one. So the rendered inspector shows digests +
// summaries only (T005 criterion 2 / redaction gate).

// The advanced-trace `status` → a human label. Closed set (schema `status` enum).
export function traceStatusLabel(status) {
  switch (status) {
    case 'complete': return 'Completed'
    case 'streaming': return 'Streaming'
    case 'cancelled': return 'Cancelled'
    case 'timed_out': return 'Timed out'
    case 'failed': return 'Failed'
    case 'interrupted': return 'Interrupted'
    case 'serving_unavailable': return 'Serving unavailable'
    default: return status || 'unknown'
  }
}

// A closed-set human label for one normalized lifecycle event kind
// (advanced-trace.v1 event `kind` enum).
export function eventKindLabel(kind) {
  switch (kind) {
    case 'request_start': return 'Request started'
    case 'response_delta_meta': return 'Streaming output'
    case 'tool_request': return 'Tool requested'
    case 'tool_result': return 'Tool result'
    case 'tool_refusal': return 'Tool refused'
    case 'continuation': return 'Continuation'
    case 'cancellation': return 'Cancellation'
    case 'schema_validation': return 'Schema validation'
    case 'error': return 'Error'
    case 'response_complete': return 'Response complete'
    default: return kind || 'event'
  }
}

// Abbreviate a `sha256:<64hex>` digest for display without losing that it is a
// digest (never a raw value). A non-digest string is returned untouched-but-bounded.
export function shortDigest(digest) {
  if (typeof digest !== 'string') return ''
  const match = /^sha256:([a-f0-9]{64})$/.exec(digest)
  if (match) return `sha256:${match[1].slice(0, 12)}…`
  return digest.slice(0, 24)
}

// Project ONE trace event into a closed, redaction-safe set of display fields.
// Whitelisted: seq, kind, timestamp, bounded char counters, opaque digests, the
// safe (already-scrubbed, schema-pattern-guarded) summary, the schema-valid flag,
// a tool token + kind, and a typed error code. A raw output / text / header /
// credential is NOT in the schema and is NOT read here.
export function formatTraceEvent(event) {
  const fields = []
  if (typeof event.text_chars === 'number') fields.push(['output chars', String(event.text_chars)])
  if (typeof event.output_chars === 'number') fields.push(['output chars', String(event.output_chars)])
  if (event.tool_id) fields.push(['tool', String(event.tool_id)])
  if (event.tool_kind) fields.push(['tool kind', String(event.tool_kind)])
  if (event.input_digest) fields.push(['input digest', shortDigest(event.input_digest)])
  if (event.output_digest) fields.push(['output digest', shortDigest(event.output_digest)])
  if (typeof event.schema_valid === 'boolean') fields.push(['schema valid', event.schema_valid ? 'yes' : 'no'])
  if (event.error && typeof event.error === 'object') {
    fields.push(['error code', String(event.error.code)])
    fields.push(['retryable', event.error.retryable ? 'yes' : 'no'])
  }
  if (event.usage && typeof event.usage === 'object') {
    if (typeof event.usage.input_tokens === 'number') fields.push(['input tokens', String(event.usage.input_tokens)])
    if (typeof event.usage.output_tokens === 'number') fields.push(['output tokens', String(event.usage.output_tokens)])
    if (typeof event.usage.latency_ms === 'number') fields.push(['latency ms', String(event.usage.latency_ms)])
  }
  return {
    seq: event.seq,
    kind: event.kind,
    label: eventKindLabel(event.kind),
    at: event.at,
    summary: typeof event.safe_summary === 'string' ? event.safe_summary : null,
    fields,
  }
}

// Project the whole advanced-trace.v1 record into the closed inspector view model.
// Every branch is a whitelist of contract fields; there is no field carrying raw
// output. A malformed/absent trace yields `{available:false}` so the inspector
// renders a truthful empty state rather than guessing.
export function formatTrace(trace) {
  if (!trace || typeof trace !== 'object') return { available: false }
  const route = trace.route_decision || {}
  const request = trace.request || {}
  const usage = trace.usage || {}
  return {
    available: true,
    traceId: trace.trace_id || null,
    status: trace.status || null,
    statusLabel: traceStatusLabel(trace.status),
    branchRef: {
      branchId: trace.branch_ref?.branch_id || null,
      conversationId: trace.branch_ref?.conversation_id || null,
      turnId: trace.branch_ref?.turn_id || null,
    },
    routeDecision: {
      provider: route.provider || null,
      routeId: route.route_id || null,
      modelProfile: route.model_profile || null,
      servedTier: route.served_tier || null,
      requestId: route.request_id || null,
      routeDigest: shortDigest(route.route_digest),
      profileDigest: shortDigest(route.profile_digest),
    },
    request: {
      contentTrust: request.content_trust || null,
      redacted: request.redacted === true,
      inputChars: typeof request.input_chars === 'number' ? request.input_chars : null,
      instructionsChars: typeof request.instructions_chars === 'number' ? request.instructions_chars : null,
      structuredOutputMode: request.structured_output_mode || null,
      controlValues: Array.isArray(request.control_values)
        ? request.control_values.map((entry) => ({ name: entry.name, value: entry.value }))
        : [],
    },
    events: Array.isArray(trace.events) ? trace.events.map(formatTraceEvent) : [],
    usage: {
      inputTokens: typeof usage.input_tokens === 'number' ? usage.input_tokens : null,
      outputTokens: typeof usage.output_tokens === 'number' ? usage.output_tokens : null,
      latencyMs: typeof usage.latency_ms === 'number' ? usage.latency_ms : null,
    },
    redaction: {
      status: trace.redaction?.status || null,
      ruleset: trace.redaction?.ruleset || null,
    },
  }
}
