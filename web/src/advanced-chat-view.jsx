import { useEffect, useMemo, useRef, useState } from 'react'
import {
  selectAdvancedRoute, controlDescriptors, initialControlValues, previewRouteChange,
  valuesForRoute, coerceControlValue, staleReasonLabel, branchOps, formatTrace,
} from './advanced-chat'

// Advanced controls + inspector panel (advanced-model-playground T005).
//
// The visible half of the Advanced playground. It lives INSIDE the Chat shell: a
// tuned run is dispatched through the parent (`onRun` / `onRerun` / `onCancel`),
// which forks a `mode="advanced"` sibling turn into the SAME transcript — this
// panel never renders a second transcript. It renders the served route's CLOSED
// control set, previews which tuned values a route change would drop BEFORE
// dropping them, drives the branch operation set (run/cancel/retry/fork/compare/
// inspect/save/reopen), and renders the redacted advanced-trace.v1 inspector
// (digests + summaries only). When the advanced runtime is unconfigured it degrades
// truthfully to an unavailable state and the ordinary transcript is untouched.

// One tunable control's editor, native and keyboard-accessible. A policy-owned
// control is a real disabled native input with an id-matched `aria-describedby`
// reason — never a hidden policy field.
function ControlEditor({ descriptor, value, onChange }) {
  const disabled = descriptor.editable === false || descriptor.source === 'policy_owned'
  const reasonId = disabled ? `adv-ctl-reason-${descriptor.name}` : undefined
  const label = descriptor.name
  let input
  if (descriptor.type === 'int') {
    const bounds = descriptor.bounds || {}
    input = <input
      type="number" aria-label={label} aria-describedby={reasonId} disabled={disabled}
      min={bounds.min} max={bounds.max} value={value ?? ''}
      onChange={(event) => onChange(coerceControlValue(descriptor, event.target.value))} />
  } else if (descriptor.type === 'enum') {
    input = <select aria-label={label} aria-describedby={reasonId} disabled={disabled} value={value ?? ''}
      onChange={(event) => onChange(coerceControlValue(descriptor, event.target.value))}>
      {(descriptor.allowed_values || []).map((option) => <option key={option} value={option}>{option}</option>)}
    </select>
  } else { // bool
    input = <input type="checkbox" aria-label={label} aria-describedby={reasonId} disabled={disabled}
      checked={Boolean(value)} onChange={(event) => onChange(coerceControlValue(descriptor, event.target.checked))} />
  }
  return <div className={`adv-control ${disabled ? 'adv-control-locked' : ''}`}>
    <label className="adv-control-label">
      <span className="adv-control-name">{label}</span>
      {input}
    </label>
    {disabled && <small id={reasonId} className="adv-control-reason">Fixed by policy ({descriptor.disabled_reason || 'policy_owned'}); shown read-only.</small>}
  </div>
}

// The redacted advanced-trace.v1 inspector. Renders ONLY the contract's ids,
// digests, bounded counters, declared control values, and safe (scrubbed) summaries
// — there is no branch that reads a raw response, a raw tool output, a header, a
// credential, an endpoint, or a path.
function TraceInspector({ trace, headingRef, onClose }) {
  const model = formatTrace(trace)
  if (!model.available) {
    return <section className="adv-inspector" aria-label="Advanced trace inspector">
      <div className="adv-pane-head"><h3 tabIndex={-1} ref={headingRef}>Trace inspector</h3><button className="adv-close" aria-label="Close inspector" onClick={onClose}>Close</button></div>
      <p className="adv-muted">No trace is available for this branch yet.</p>
    </section>
  }
  const rd = model.routeDecision
  const rq = model.request
  return <section className="adv-inspector" aria-label="Advanced trace inspector">
    <div className="adv-pane-head">
      <h3 tabIndex={-1} ref={headingRef}>Trace inspector · {model.statusLabel}</h3>
      <button className="adv-close" aria-label="Close inspector" onClick={onClose}>Close</button>
    </div>
    <p className="adv-redaction-note">Redacted digests and safe summaries only — no raw output, secret, host, or path is carried.</p>
    <dl className="adv-kv">
      <div><dt>Provider</dt><dd>{rd.provider || '—'}</dd></div>
      <div><dt>Route</dt><dd>{rd.routeId || '—'}</dd></div>
      {rd.modelProfile && <div><dt>Model profile</dt><dd>{rd.modelProfile}</dd></div>}
      {rd.servedTier && <div><dt>Served tier</dt><dd>{rd.servedTier}</dd></div>}
      {rd.requestId && <div><dt>Request id</dt><dd>{rd.requestId}</dd></div>}
      <div><dt>Route digest</dt><dd className="adv-digest">{rd.routeDigest || '—'}</dd></div>
      <div><dt>Profile digest</dt><dd className="adv-digest">{rd.profileDigest || '—'}</dd></div>
    </dl>
    <h4 className="adv-subhead">Request (redacted)</h4>
    <dl className="adv-kv">
      <div><dt>Content trust</dt><dd>{rq.contentTrust || '—'}</dd></div>
      <div><dt>Redacted</dt><dd>{rq.redacted ? 'yes' : 'no'}</dd></div>
      {rq.inputChars != null && <div><dt>Input chars</dt><dd>{rq.inputChars}</dd></div>}
      {rq.instructionsChars != null && <div><dt>Instruction chars</dt><dd>{rq.instructionsChars}</dd></div>}
      {rq.structuredOutputMode && <div><dt>Output mode</dt><dd>{rq.structuredOutputMode}</dd></div>}
    </dl>
    {rq.controlValues.length > 0 && <>
      <h4 className="adv-subhead">Control values</h4>
      <ul className="adv-control-values">
        {rq.controlValues.map((cv) => <li key={cv.name}><b>{cv.name}</b>: {String(cv.value)}</li>)}
      </ul>
    </>}
    <h4 className="adv-subhead">Events</h4>
    <ol className="adv-events" aria-label="Trace events">
      {model.events.map((event, index) => <li key={`${event.seq}-${index}`} className={`adv-event adv-event-${event.kind}`}>
        <div className="adv-event-head"><span className="adv-event-seq">#{event.seq}</span><b>{event.label}</b></div>
        {event.summary && <p className="adv-event-summary">{event.summary}</p>}
        {event.fields.length > 0 && <dl className="adv-event-fields">
          {event.fields.map(([key, val], fieldIndex) => <div key={`${key}-${fieldIndex}`}><dt>{key}</dt><dd className={key.includes('digest') ? 'adv-digest' : undefined}>{val}</dd></div>)}
        </dl>}
      </li>)}
    </ol>
    <h4 className="adv-subhead">Usage</h4>
    <dl className="adv-kv">
      <div><dt>Input tokens</dt><dd>{model.usage.inputTokens ?? '—'}</dd></div>
      <div><dt>Output tokens</dt><dd>{model.usage.outputTokens ?? '—'}</dd></div>
      <div><dt>Latency ms</dt><dd>{model.usage.latencyMs ?? '—'}</dd></div>
    </dl>
  </section>
}

// A compact, redaction-safe result card used inside the side-by-side compare pane.
function CompareCard({ branch }) {
  const model = formatTrace(branch?.trace)
  return <article className="adv-compare-card" aria-label={`Comparison ${branch?.label || 'branch'}`}>
    <header className="adv-compare-head"><b>{branch?.label}</b><span className={`adv-branch-status adv-branch-${branch?.status}`}>{branch?.status}</span></header>
    <dl className="adv-kv">
      <div><dt>Route</dt><dd>{branch?.routeId || '—'}</dd></div>
      {model.available && <div><dt>Trace status</dt><dd>{model.statusLabel}</dd></div>}
      {model.available && model.usage.outputTokens != null && <div><dt>Output tokens</dt><dd>{model.usage.outputTokens}</dd></div>}
    </dl>
    {/* The response text is the operator's own experiment output rendered in the
        shared transcript; here the compare card shows the redacted result summary. */}
    <p className="adv-compare-text">{branch?.text ? branch.text : <span className="adv-muted">No output text.</span>}</p>
  </article>
}

export default function AdvancedPanel({
  unavailable = '', routes = [], streaming = false,
  branches = [], onRun, onRerun, onCancel, onInspect, inspectingId,
  onSave, onReopen, onToggleCompare, compareIds = [],
}) {
  const [routeId, setRouteId] = useState('')
  const [values, setValues] = useState({})
  const [prompt, setPrompt] = useState('')
  const [instructions, setInstructions] = useState('')
  const [pending, setPending] = useState(null) // { route, stale, carried } — a previewed, not-yet-committed route change
  const [announce, setAnnounce] = useState('')
  const previewRef = useRef(null)
  const inspectorHeadingRef = useRef(null)
  const compareHeadingRef = useRef(null)
  const cancelRef = useRef(null)
  const wasStreamingRef = useRef(false)

  const selectedRoute = useMemo(() => routes.find((route) => route.route_id === routeId) || null, [routes, routeId])

  // Seed the editor from the first served route once routes resolve.
  useEffect(() => {
    if (!routeId && routes.length) {
      setRouteId(routes[0].route_id)
      setValues(initialControlValues(routes[0]))
    }
  }, [routes, routeId])

  // Move focus onto the preview when a route change is proposed, so a keyboard
  // operator lands on the "what will be dropped" summary before committing.
  useEffect(() => { if (pending) previewRef.current?.focus() }, [pending])
  // Focus the inspector / compare headings when they open (never drop to <body>).
  useEffect(() => { if (inspectingId) inspectorHeadingRef.current?.focus() }, [inspectingId])
  useEffect(() => { if (compareIds.length === 2) compareHeadingRef.current?.focus() }, [compareIds.length])
  // Focus follows the Run↔Cancel swap: when a run starts move focus to Cancel so
  // it is never dropped; the panel does not steal focus back on settle (the shared
  // composer owns that), so we only push to Cancel on the streaming edge.
  useEffect(() => {
    if (streaming && !wasStreamingRef.current) cancelRef.current?.focus()
    wasStreamingRef.current = streaming
  }, [streaming])

  if (unavailable) {
    return <section className="advanced-panel" aria-label="Advanced controls">
      <p className="adv-unavailable">{unavailable}</p>
      <p className="adv-muted">Advanced controls are not configured in this build. The transcript and its route are unchanged.</p>
    </section>
  }
  if (!routes.length) {
    return <section className="advanced-panel" aria-label="Advanced controls">
      <p className="adv-muted">No reviewed advanced routes are available. The transcript and its route are unchanged.</p>
    </section>
  }

  const descriptors = controlDescriptors(selectedRoute)

  const onRouteSelect = (nextId) => {
    if (nextId === routeId) { setPending(null); return }
    let nextRoute
    try { nextRoute = selectAdvancedRoute(routes, nextId) } catch { setAnnounce('That route is not in the reviewed allowlist.'); return }
    const { carried, stale } = previewRouteChange(values, nextRoute)
    // Never a silent wipe: stage the change and PREVIEW which tuned values would be
    // dropped. The operator commits or keeps the current route explicitly.
    setPending({ route: nextRoute, stale, carried })
    setAnnounce(stale.length
      ? `Switching to ${nextRoute.display_name || nextRoute.route_id} would drop ${stale.length} tuned value${stale.length === 1 ? '' : 's'}. Review before applying.`
      : `Switching to ${nextRoute.display_name || nextRoute.route_id} keeps all tuned values.`)
  }

  const applyRouteChange = () => {
    if (!pending) return
    const nextRoute = pending.route
    setRouteId(nextRoute.route_id)
    setValues(valuesForRoute(nextRoute, pending.carried))
    setAnnounce(`Applied ${nextRoute.display_name || nextRoute.route_id}. ${pending.stale.length} value${pending.stale.length === 1 ? '' : 's'} dropped as unsupported.`)
    setPending(null)
  }
  const keepRoute = () => { setPending(null); setAnnounce('Kept the current route. No tuned values were dropped.') }

  const setControl = (name, value) => setValues((current) => ({ ...current, [name]: value }))

  const canRun = Boolean(selectedRoute && prompt.trim() && !streaming && !pending)
  const run = () => {
    if (!canRun) return
    onRun?.({ route: selectedRoute, routeId, values, prompt: prompt.trim(), instructions: instructions.trim() })
    setAnnounce('Advanced branch run started in the transcript.')
  }

  const reopen = (branch) => {
    // Reopen loads a saved branch's route + tuned values back into the editor for
    // further tuning; it never mutates the transcript.
    const branchRoute = routes.find((route) => route.route_id === branch.routeId)
    if (branchRoute) {
      setRouteId(branch.routeId)
      setValues({ ...initialControlValues(branchRoute), ...(branch.controlsValues || {}) })
    }
    setPrompt(branch.prompt || '')
    setInstructions(branch.instructions || '')
    onReopen?.(branch)
    setAnnounce(`Reopened ${branch.label} for tuning.`)
  }

  const settledCount = branches.filter((branch) => ['complete', 'cancelled', 'interrupted', 'failed'].includes(branch.status)).length
  const comparePair = compareIds.map((id) => branches.find((branch) => branch.id === id)).filter(Boolean)
  const inspecting = branches.find((branch) => branch.id === inspectingId) || null

  // The Escape-to-close/keep handler for the panel's transient panes; yields to a
  // native select's own Escape (which closes its listbox first).
  const onPanelKeyDown = (event) => {
    if (event.key !== 'Escape') return
    if (event.target?.tagName === 'SELECT') return
    if (pending) { event.preventDefault(); keepRoute() }
    else if (inspectingId) { event.preventDefault(); onInspect?.(null) }
    else if (compareIds.length) { event.preventDefault(); onToggleCompare?.(null) }
  }

  return <section className="advanced-panel" aria-label="Advanced controls" onKeyDown={onPanelKeyDown}>
    <div className="adv-live" role="status" aria-live="polite">{announce}</div>

    <div className="adv-route-row">
      <label className="adv-route-select"><span>Advanced route</span>
        <select aria-label="Advanced route" value={pending ? pending.route.route_id : routeId}
          onChange={(event) => onRouteSelect(event.target.value)}>
          {routes.map((route) => <option key={route.route_id} value={route.route_id}>{route.display_name || route.route_id}</option>)}
        </select>
      </label>
    </div>

    {pending && <div className="adv-preview" role="alertdialog" aria-label="Route change preview" tabIndex={-1} ref={previewRef}>
      <h3 className="adv-preview-head">Switching to {pending.route.display_name || pending.route.route_id}</h3>
      {pending.stale.length
        ? <>
            <p>These tuned values are <b>not supported</b> on that route and will be dropped if you apply the change:</p>
            <ul className="adv-stale-list" aria-label="Values that will be dropped">
              {pending.stale.map((entry) => <li key={entry.name}>
                <b>{entry.name}</b> = <code>{String(entry.value)}</code> — {staleReasonLabel(entry.reason)}
              </li>)}
            </ul>
          </>
        : <p>All current tuned values are supported on that route; none will be dropped.</p>}
      <div className="adv-preview-actions">
        <button type="button" className="adv-apply" onClick={applyRouteChange}>Apply route change</button>
        <button type="button" className="adv-keep secondary-button" onClick={keepRoute}>Keep current route</button>
      </div>
    </div>}

    <fieldset className="adv-controls" disabled={Boolean(pending)}>
      <legend>Controls for {selectedRoute?.display_name || routeId}</legend>
      {descriptors.length
        ? descriptors.map((descriptor) => <ControlEditor key={descriptor.name} descriptor={descriptor} value={values[descriptor.name]} onChange={(value) => setControl(descriptor.name, value)} />)
        : <p className="adv-muted">This route declares no tunable controls.</p>}
    </fieldset>

    <label className="adv-field"><span>Experiment prompt</span>
      <textarea aria-label="Advanced prompt" rows="3" value={prompt} disabled={Boolean(pending)}
        onChange={(event) => setPrompt(event.target.value)} placeholder="Prompt for this tuned attempt…" />
    </label>
    <label className="adv-field"><span>Instructions (optional)</span>
      <textarea aria-label="Advanced instructions" rows="2" value={instructions} disabled={Boolean(pending)}
        onChange={(event) => setInstructions(event.target.value)} placeholder="System instructions, kept separate from the prompt…" />
    </label>

    <div className="adv-run-row">
      {streaming
        ? <button ref={cancelRef} type="button" className="adv-cancel" aria-label="Cancel advanced run" onClick={onCancel}>Cancel run</button>
        : <button type="button" className="adv-run" aria-label="Run advanced branch" disabled={!canRun} onClick={run}>Run branch</button>}
    </div>

    {branches.length > 0 && <section className="adv-branches" aria-label="Advanced branches">
      <h3 className="adv-subhead">Branches in this transcript</h3>
      <ul className="adv-branch-list">
        {branches.map((branch) => {
          const ops = branchOps(branch, { settledCount, streaming })
          const comparing = compareIds.includes(branch.id)
          return <li key={branch.id} className="adv-branch-row">
            <div className="adv-branch-meta">
              <b>{branch.label}</b>
              <span className={`adv-branch-status adv-branch-${branch.status}`}>{branch.status}</span>
              <small>{branch.routeId}{branch.saved ? ' · saved' : ''}</small>
            </div>
            <div className="adv-branch-actions">
              <button type="button" aria-label={`Inspect ${branch.label}`} disabled={!ops.inspect} onClick={() => onInspect?.(branch.id)}>Inspect</button>
              <button type="button" aria-label={`Retry ${branch.label}`} disabled={!ops.retry} onClick={() => onRerun?.(branch, 'retry')}>Retry</button>
              <button type="button" aria-label={`Fork ${branch.label}`} disabled={!ops.fork} onClick={() => onRerun?.(branch, 'fork')}>Fork</button>
              <button type="button" aria-label={`Compare ${branch.label}`} aria-pressed={comparing} disabled={!ops.compare && !comparing} onClick={() => onToggleCompare?.(branch.id)}>{comparing ? 'Comparing' : 'Compare'}</button>
              <button type="button" aria-label={`Save ${branch.label}`} disabled={!ops.save} onClick={() => onSave?.(branch)}>{branch.saved ? 'Saved' : 'Save'}</button>
              <button type="button" aria-label={`Reopen ${branch.label}`} disabled={!ops.reopen} onClick={() => reopen(branch)}>Reopen</button>
            </div>
          </li>
        })}
      </ul>
    </section>}

    {comparePair.length === 2 && <section className="adv-compare" aria-label="Compare branches">
      <div className="adv-pane-head">
        <h3 tabIndex={-1} ref={compareHeadingRef}>Comparing two branches</h3>
        <button className="adv-close" aria-label="Close comparison" onClick={() => onToggleCompare?.(null)}>Close</button>
      </div>
      <div className="adv-compare-grid">
        {comparePair.map((branch) => <CompareCard key={branch.id} branch={branch} />)}
      </div>
    </section>}

    {inspecting && <TraceInspector trace={inspecting.trace} headingRef={inspectorHeadingRef} onClose={() => onInspect?.(null)} />}
  </section>
}
