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

// The batch bound the LIVE parallel dispatch enforces (mirrors the server's
// MAX_DISPATCH_ROUTES): at least 2 routes (one route is just a single Run branch)
// and at most 4, so the picker can never assemble an out-of-bounds batch.
export const MIN_DISPATCH_ROUTES = 2
export const MAX_DISPATCH_ROUTES = 4

export default function AdvancedPanel({
  unavailable = '', routes = [], streaming = false,
  branches = [], onRun, onRunDispatch, onRerun, onCancel, onInspect, inspectingId,
  onSave, onReopen, onToggleCompare, compareIds = [],
}) {
  const [routeId, setRouteId] = useState('')
  const [values, setValues] = useState({})
  const [prompt, setPrompt] = useState('')
  const [instructions, setInstructions] = useState('')
  // The reviewed routes checked for a LIVE parallel dispatch. Bounded to
  // MAX_DISPATCH_ROUTES so the picker cannot assemble a batch the server rejects.
  const [dispatchIds, setDispatchIds] = useState([])
  const [pending, setPending] = useState(null) // { route, stale, carried } — a previewed, not-yet-committed route change
  const [announce, setAnnounce] = useState('')
  const [reopenConfirm, setReopenConfirm] = useState(null) // branch id whose reopen would clobber unsaved editor content
  const previewRef = useRef(null)
  const inspectorHeadingRef = useRef(null)
  const compareHeadingRef = useRef(null)
  const routeSelectRef = useRef(null)
  // Every transient pane captures the control that OPENED it and restores focus to
  // it on close, so no close path (Apply/Keep/Escape/Close) ever drops focus to
  // <body> when the pane unmounts. The preview always restores to the route select
  // (its only trigger); inspector/compare restore to the specific invoking button.
  const inspectorTriggerRef = useRef(null)
  const compareTriggerRef = useRef(null)
  // Per-branch Reopen button nodes, so a Save can move focus to the now-enabled
  // Reopen instead of letting the Save button disable itself out from under focus.
  const reopenRefs = useRef({})
  const justSavedRef = useRef(null)

  const selectedRoute = useMemo(() => routes.find((route) => route.route_id === routeId) || null, [routes, routeId])

  // Seed the editor from the first served route once routes resolve.
  useEffect(() => {
    if (!routeId && routes.length) {
      setRouteId(routes[0].route_id)
      setValues(initialControlValues(routes[0]))
    }
  }, [routes, routeId])

  // Move focus onto the preview when a route change is proposed, so a keyboard
  // operator lands on the "what will be dropped" summary before committing. The
  // close paths (applyRouteChange/keepRoute) restore focus to the route select.
  useEffect(() => { if (pending) previewRef.current?.focus() }, [pending])
  // Inspector focus: move to its heading on open; RESTORE to the invoking Inspect
  // button on close (Close/Escape both route through onInspect(null)), never body.
  useEffect(() => {
    if (inspectingId) inspectorHeadingRef.current?.focus()
    else if (inspectorTriggerRef.current) { inspectorTriggerRef.current.focus?.(); inspectorTriggerRef.current = null }
  }, [inspectingId])
  // Compare focus: move to its heading when the pair opens; RESTORE to the invoking
  // Compare control when the pane is fully closed (length 0), never body.
  useEffect(() => {
    if (compareIds.length === 2) compareHeadingRef.current?.focus()
    else if (compareIds.length === 0 && compareTriggerRef.current) { compareTriggerRef.current.focus?.(); compareTriggerRef.current = null }
  }, [compareIds.length])
  // Save must not self-destruct focus: when the just-saved branch flips to saved,
  // the Save button disables (→ body) so we move focus to its now-enabled Reopen.
  useEffect(() => {
    const id = justSavedRef.current
    if (!id) return
    const saved = branches.find((branch) => branch.id === id)
    if (saved?.saved) {
      const el = reopenRefs.current[id]
      if (el && !el.disabled) el.focus()
      justSavedRef.current = null
    }
  }, [branches])
  // The Run↔Cancel focus swap is owned by the shared Composer (App.jsx): its
  // effect runs after this panel's (later sibling wins), and it moves focus to a
  // Cancel control on the streaming edge and back to the textarea on settle. A
  // duplicate panel-level effect here would be immediately overridden, so it is
  // deliberately NOT present — the composer path is the single real one.

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
    // Restore focus to the trigger (the route select) so closing the preview never
    // drops focus to <body>.
    routeSelectRef.current?.focus()
  }
  const keepRoute = () => {
    setPending(null)
    setAnnounce('Kept the current route. No tuned values were dropped.')
    routeSelectRef.current?.focus()
  }

  const setControl = (name, value) => setValues((current) => ({ ...current, [name]: value }))

  const canRun = Boolean(selectedRoute && prompt.trim() && !streaming && !pending)
  const run = () => {
    if (!canRun) return
    onRun?.({ route: selectedRoute, routeId, values, prompt: prompt.trim(), instructions: instructions.trim() })
    setAnnounce('Advanced branch run started in the transcript.')
  }

  // Multi-route dispatch: check a route to include it in the batch. A route already
  // at the MAX_DISPATCH_ROUTES cap cannot add more; unchecking always works.
  const toggleDispatchRoute = (id) => {
    setDispatchIds((current) => {
      if (current.includes(id)) return current.filter((value) => value !== id)
      if (current.length >= MAX_DISPATCH_ROUTES) {
        setAnnounce(`A parallel dispatch runs at most ${MAX_DISPATCH_ROUTES} routes at once.`)
        return current
      }
      return [...current, id]
    })
  }
  // "Run on N routes" is enabled ONLY with at least MIN_DISPATCH_ROUTES checked and
  // a prompt entered (and not mid-stream / mid-preview) — a single route is just the
  // ordinary Run branch above.
  const canDispatch = Boolean(prompt.trim() && !streaming && !pending && dispatchIds.length >= MIN_DISPATCH_ROUTES)
  const runDispatch = () => {
    if (!canDispatch) return
    // Preserve the panel's checked order so the assembled batch is deterministic.
    const routeIds = routes.map((route) => route.route_id).filter((id) => dispatchIds.includes(id))
    onRunDispatch?.({ routeIds, prompt: prompt.trim(), instructions: instructions.trim() })
    setAnnounce(`Parallel dispatch started across ${routeIds.length} routes in the transcript.`)
  }

  const reopen = (branch) => {
    // Reopen loads a saved branch's route + tuned values back into the editor for
    // further tuning; it never mutates the transcript.
    setReopenConfirm(null)
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
  // Reopen clobbers the in-progress editor. It is only destructive when the editor
  // already holds non-empty content that DIFFERS from what this branch would load
  // (loading identical content is not a clobber). In that case, confirm first (S3);
  // a pristine editor — or one that already matches — reopens directly.
  const reopenWouldClobber = (branch) => {
    const p = prompt.trim(); const i = instructions.trim()
    if (!p && !i) return false
    return p !== (branch.prompt || '').trim() || i !== (branch.instructions || '').trim()
  }
  const requestReopen = (branch) => {
    if (reopenWouldClobber(branch)) {
      setReopenConfirm(branch.id)
      setAnnounce(`Reopening ${branch.label} will replace your current prompt and instructions. Confirm to continue, or keep editing.`)
    } else {
      reopen(branch)
    }
  }
  // Inspect/Compare capture the CLICKED control as the pane's focus-restore target
  // before handing off to the parent's open/toggle state.
  const openInspector = (event, branchId) => { inspectorTriggerRef.current = event.currentTarget; onInspect?.(branchId) }
  const clickCompare = (event, branch) => {
    compareTriggerRef.current = event.currentTarget
    if (compareIds.includes(branch.id)) {
      setAnnounce(`Branch ${branch.label} removed from comparison.`)
    } else {
      // Announce the SELECTION (not just aria-pressed) as it is picked (S2).
      const next = Math.min(compareIds.length + 1, 2)
      setAnnounce(`Branch ${branch.label} selected for comparison, ${next} of 2`)
    }
    onToggleCompare?.(branch.id)
  }
  // Save announces to the live region and, because the Save button disables itself
  // on save, hands focus to the now-enabled Reopen via the justSaved effect (MUST-2).
  const clickSave = (branch) => {
    justSavedRef.current = branch.id
    setAnnounce(`Branch ${branch.label} saved.`)
    onSave?.(branch)
  }

  const settledCount = branches.filter((branch) => ['complete', 'cancelled', 'interrupted', 'failed'].includes(branch.status)).length
  const comparePair = compareIds.map((id) => branches.find((branch) => branch.id === id)).filter(Boolean)
  const inspecting = branches.find((branch) => branch.id === inspectingId) || null

  // The Escape-to-close/keep handler for the panel's transient panes. Each close
  // path routes through a handler that restores focus to the pane's trigger
  // (keepRoute → route select; onInspect(null)/onToggleCompare(null) → the
  // captured invoking button via the focus effects), so Escape never drops to body.
  //
  // Tradeoff (NOTE): the blanket SELECT guard means Escape on the route <select>
  // yields to the native listbox dismissal even when the select is CLOSED. React
  // exposes no open/closed state for a native <select>, so we cannot tell a
  // closed-select Escape from a listbox-dismissal one; deferring to the browser is
  // the safe choice (a preview is re-openable, a mid-dropdown discard is not).
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
        <select ref={routeSelectRef} aria-label="Advanced route" value={pending ? pending.route.route_id : routeId}
          onChange={(event) => onRouteSelect(event.target.value)}>
          {routes.map((route) => <option key={route.route_id} value={route.route_id}>{route.display_name || route.route_id}</option>)}
        </select>
      </label>
    </div>

    {pending && <div className="adv-preview" role="alertdialog" aria-label="Route change preview" aria-describedby="adv-preview-desc" tabIndex={-1} ref={previewRef}>
      <h3 className="adv-preview-head">Switching to {pending.route.display_name || pending.route.route_id}</h3>
      {pending.stale.length
        ? <>
            <p id="adv-preview-desc">These tuned values are <b>not supported</b> on that route and will be dropped if you apply the change:</p>
            <ul className="adv-stale-list" aria-label="Values that will be dropped">
              {pending.stale.map((entry) => <li key={entry.name}>
                <b>{entry.name}</b> = <code>{String(entry.value)}</code> — {staleReasonLabel(entry.reason)}
              </li>)}
            </ul>
          </>
        : <p id="adv-preview-desc">All current tuned values are supported on that route; none will be dropped.</p>}
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
        ? <button type="button" className="adv-cancel" aria-label="Cancel advanced run" onClick={onCancel}>Cancel run</button>
        : <button type="button" className="adv-run" aria-label="Run advanced branch" disabled={!canRun} onClick={run}>Run branch</button>}
    </div>

    {/* LIVE parallel multi-route dispatch: fan the SAME prompt/instructions across
        2..MAX_DISPATCH_ROUTES reviewed routes at once, each streaming as its own
        `mode="advanced"` sibling in the shared transcript. The checkbox list picks
        the batch; "Run on N routes" enables only with >=2 checked and a prompt. */}
    <fieldset className="adv-dispatch" disabled={Boolean(pending)}>
      <legend>Run across multiple routes</legend>
      <p className="adv-muted" id="adv-dispatch-desc">
        Pick 2–{MAX_DISPATCH_ROUTES} reviewed routes to run this prompt on in parallel and compare side by side.
      </p>
      <ul className="adv-dispatch-list" aria-label="Routes for parallel dispatch" aria-describedby="adv-dispatch-desc">
        {routes.map((route) => {
          const checked = dispatchIds.includes(route.route_id)
          const capped = !checked && dispatchIds.length >= MAX_DISPATCH_ROUTES
          return <li key={route.route_id} className="adv-dispatch-item">
            <label className="adv-dispatch-label">
              <input type="checkbox" checked={checked} disabled={capped}
                aria-label={`Include ${route.display_name || route.route_id} in the parallel dispatch`}
                onChange={() => toggleDispatchRoute(route.route_id)} />
              <span>{route.display_name || route.route_id}</span>
            </label>
          </li>
        })}
      </ul>
      <button type="button" className="adv-dispatch-run"
        aria-label={`Run on ${dispatchIds.length} routes`}
        disabled={!canDispatch} onClick={runDispatch}>
        Run on {dispatchIds.length} route{dispatchIds.length === 1 ? '' : 's'}
      </button>
    </fieldset>

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
              <button type="button" title="Inspect" aria-label={`Inspect ${branch.label}`} disabled={!ops.inspect} onClick={(event) => openInspector(event, branch.id)}><span aria-hidden="true">◎</span><span className="btn-label">Inspect</span></button>
              <button type="button" title="Retry" aria-label={`Retry ${branch.label}`} disabled={!ops.retry} onClick={() => onRerun?.(branch, 'retry')}><span aria-hidden="true">↻</span><span className="btn-label">Retry</span></button>
              <button type="button" title="Fork" aria-label={`Fork ${branch.label}`} disabled={!ops.fork} onClick={() => onRerun?.(branch, 'fork')}><span aria-hidden="true">⋔</span><span className="btn-label">Fork</span></button>
              <button type="button" title="Compare" aria-label={`Compare ${branch.label}`} aria-pressed={comparing} disabled={!ops.compare && !comparing} onClick={(event) => clickCompare(event, branch)}><span aria-hidden="true">⇄</span><span className="btn-label">{comparing ? 'Comparing' : 'Compare'}</span></button>
              {/* Save's accessible name tracks its state so the visible "Saved"
                  always matches the name (WCAG 2.5.3); aria-pressed carries the
                  saved state. On save, focus moves to Reopen (MUST-2). */}
              <button type="button" title={branch.saved ? 'Saved' : 'Save'} aria-label={branch.saved ? `Saved ${branch.label}` : `Save ${branch.label}`} aria-pressed={branch.saved ? true : undefined} disabled={!ops.save} onClick={() => clickSave(branch)}><span aria-hidden="true">{branch.saved ? '★' : '☆'}</span><span className="btn-label">{branch.saved ? 'Saved' : 'Save'}</span></button>
              {reopenConfirm === branch.id
                ? <>
                    <button type="button" className="adv-reopen-confirm" aria-label={`Confirm reopen ${branch.label}`} onClick={() => reopen(branch)}>Confirm reopen</button>
                    <button type="button" aria-label={`Keep editing instead of reopening ${branch.label}`} onClick={() => { setReopenConfirm(null); setAnnounce('Kept your current editor content.') }}>Keep editing</button>
                  </>
                : <button type="button" title="Reopen" ref={(el) => { if (el) reopenRefs.current[branch.id] = el; else delete reopenRefs.current[branch.id] }} aria-label={`Reopen ${branch.label}`} disabled={!ops.reopen} onClick={() => requestReopen(branch)}><span aria-hidden="true">↺</span><span className="btn-label">Reopen</span></button>}
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
