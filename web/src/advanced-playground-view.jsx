import { useMemo, useState } from 'react'
import {
  resolvePresetView, resolveTemplateView, formatComparison,
  declaredInstructionsView, canRecordRating,
} from './advanced-playground'

// The visible half of the Advanced playground extensions (advanced-model-playground
// T006 presets + comparison + export, T009 instruction templates, T010 declared-
// criterion route ratings). It lives INSIDE the Chat shell beside AdvancedPanel and
// dispatches through the parent callbacks; it never renders a second transcript.
//
//  * A preset whose pinned route/tool/profile digest DRIFTED opens an explicit
//    REPAIR banner listing the drifted references — never a silently substituted
//    route or tool (the resolution carries no selectable preset on drift).
//  * A comparison shows FACTUAL per-attempt metrics; a ranking (a winner) appears
//    ONLY with a declared, non-qualification criterion — no winner is inferred.
//  * A template's FULL body text + a declared-instructions preview (resolved text +
//    named substitutions) are visible PRE-SEND, marked declared — never a covert
//    injected prompt; a drifted/removed template opens repair mode.
//  * A rating cannot be recorded without a declared criterion (the submit control
//    is disabled until one is chosen); aggregates carry the non-qualification label.
//
// When a surface is unconfigured it degrades truthfully and the transcript is
// untouched.

function driftList(drifted) {
  return <ul className="adv-drift-list">
    {drifted.map((ref) => (
      <li key={`${ref.refKind}:${ref.id}`}>
        <b>{ref.refKind}</b> <code>{ref.id}</code> drifted from its pinned digest
      </li>
    ))}
  </ul>
}

export default function AdvancedPlaygroundPanel({
  unavailable = '', routes = [], presets = [], templates = [], criteria = [], aggregates = null,
  onResolvePreset, onBuildComparison, onResolveTemplate, onDeclaredInstructions, onRecordRating,
}) {
  const [presetResolution, setPresetResolution] = useState(null)
  const [comparison, setComparison] = useState(null)
  const [templateId, setTemplateId] = useState('')
  const [templateResolution, setTemplateResolution] = useState(null)
  const [bindings, setBindings] = useState({})
  const [declared, setDeclared] = useState(null)
  const [ratingRouteId, setRatingRouteId] = useState('')
  const [ratingCriterionId, setRatingCriterionId] = useState('')
  const [ratingScore, setRatingScore] = useState(3)
  const [announce, setAnnounce] = useState('')

  const comparisonView = useMemo(() => formatComparison(comparison), [comparison])
  const declaredView = useMemo(() => declaredInstructionsView(declared), [declared])
  const selectedTemplate = useMemo(
    () => templates.find((template) => template.template_id === templateId) || null,
    [templates, templateId],
  )

  if (unavailable) {
    return <section className="advanced-playground" aria-label="Advanced playground">
      <div className="config-note" role="note">
        <b>Advanced playground is not available</b>
        <span>{unavailable}</span>
        <span>The preset, template, and rating surfaces return 503 until their stores are injected on the hub; no browser setting enables them. The transcript is unchanged.</span>
      </div>
    </section>
  }

  const resolvePreset = async (presetId) => {
    try {
      const result = await onResolvePreset?.(presetId)
      const view = resolvePresetView(result)
      setPresetResolution(view)
      if (view.repairRequired) {
        setAnnounce(`Preset ${presetId} needs repair: ${view.drifted.length} reference${view.drifted.length === 1 ? '' : 's'} drifted.`)
      } else if (view.unverifiable) {
        setAnnounce(`Preset ${presetId} could not be verified right now — not applied. The hub cannot verify its pinned references.`)
      } else if (view.preset) {
        setAnnounce(`Preset ${presetId} is ready.`)
      } else {
        setAnnounce(`Preset ${presetId} could not be resolved.`)
      }
    } catch {
      setAnnounce('The preset could not be resolved.')
    }
  }

  const buildComparison = async () => {
    try {
      const record = await onBuildComparison?.()
      setComparison(record)
      const view = formatComparison(record)
      setAnnounce(view.hasWinner
        ? `Comparison ranked by ${view.criterion.label}.`
        : 'Comparison shown with factual metrics only — no winner without a declared criterion.')
    } catch {
      setAnnounce('The comparison is not valid.')
    }
  }

  const selectTemplate = async (nextId) => {
    setTemplateId(nextId)
    setBindings({})
    setDeclared(null)
    setTemplateResolution(null)
    if (!nextId) return
    const template = templates.find((entry) => entry.template_id === nextId)
    if (!template) return
    try {
      const result = await onResolveTemplate?.(nextId, template.template_digest)
      setTemplateResolution(resolveTemplateView(result))
    } catch {
      setAnnounce('The template could not be resolved.')
    }
  }

  const previewDeclared = async () => {
    if (!templateId) return
    try {
      const di = await onDeclaredInstructions?.(templateId, bindings)
      setDeclared(di)
      setAnnounce('Declared instructions previewed — visible before send.')
    } catch {
      setAnnounce('Those substitutions are not declared by the template.')
    }
  }

  const canRate = canRecordRating({ criterionId: ratingCriterionId, score: ratingScore }) && Boolean(ratingRouteId)
  const recordRating = async () => {
    if (!canRate) return
    try {
      await onRecordRating?.({ routeId: ratingRouteId, criterionId: ratingCriterionId, score: Number(ratingScore) })
      setAnnounce('Rating recorded — informal preference evidence only.')
    } catch {
      setAnnounce('The rating could not be recorded.')
    }
  }

  return <section className="advanced-playground" aria-label="Advanced playground">
    <div className="adv-live" role="status" aria-live="polite">{announce}</div>

    {/* ---- Presets (T006) ---- */}
    <section aria-label="Presets">
      <h3>Presets</h3>
      {presets.length === 0
        ? <p className="adv-muted">No saved presets.</p>
        : <ul className="adv-preset-list">
            {presets.map((preset) => (
              <li key={preset.preset_id}>
                <button type="button" onClick={() => resolvePreset(preset.preset_id)}>
                  Select {preset.name?.text || preset.preset_id}
                </button>
              </li>
            ))}
          </ul>}
      {presetResolution && presetResolution.repairRequired && (
        <div role="alert" className="adv-repair" aria-label="Preset repair required">
          <p><b>Repair required.</b> This preset pins references that have drifted; it will not be applied. Repair the preset before selecting it — no substitute route or tool is chosen.</p>
          {driftList(presetResolution.drifted)}
          <button type="button" onClick={() => resolvePreset(presetResolution.presetId)}>Re-check preset</button>
        </div>
      )}
      {presetResolution && presetResolution.unverifiable && (
        <div role="status" className="adv-unverifiable" aria-label="Preset could not be verified">
          <p><b>Could not be verified.</b> The hub cannot verify this preset&apos;s pinned references right now, so nothing was applied — no route, tool, or profile was selected or substituted. Try again once digest verification is available.</p>
          {presetResolution.unverifiableRefs && presetResolution.unverifiableRefs.length > 0 && (
            <ul className="adv-unverifiable-list">
              {presetResolution.unverifiableRefs.map((ref) => (
                <li key={`${ref.refKind}:${ref.id}`}>
                  <b>{ref.refKind}</b> <code>{ref.id}</code> could not be verified
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {presetResolution && !presetResolution.repairRequired && !presetResolution.unverifiable && presetResolution.preset && (
        <p className="adv-preset-ready">Preset <b>{presetResolution.preset.name?.text || presetResolution.presetId}</b> is ready to apply.</p>
      )}
    </section>

    {/* ---- Comparison (T006) ---- */}
    <section aria-label="Comparison">
      <h3>Comparison</h3>
      <button type="button" onClick={buildComparison}>Build comparison</button>
      {comparisonView.available && (
        <div className="adv-comparison">
          {comparisonView.hasWinner
            ? <p className="adv-comparison-winner">Ranked by declared criterion: <b>{comparisonView.criterion.label}</b> (informal preference — not qualification).</p>
            : <p className="adv-comparison-nowinner">Factual metrics only — no winner. Add a declared evaluation criterion to rank the attempts.</p>}
          <ul className="adv-comparison-attempts">
            {comparisonView.attempts.map((attempt) => (
              <li key={attempt.turnId}>
                <code>{attempt.routeId}</code> — status {attempt.status};
                {' '}output tokens {attempt.metrics.output_tokens ?? 0}, latency {attempt.metrics.latency_ms ?? 0}ms
                {attempt.rank != null && <span className="adv-rank"> — rank {attempt.rank}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>

    {/* ---- Instruction templates (T009) ---- */}
    <section aria-label="Instruction templates">
      <h3>Instruction templates</h3>
      <label>
        Template
        <select aria-label="Instruction template" value={templateId} onChange={(event) => selectTemplate(event.target.value)}>
          <option value="">Select a template…</option>
          {templates.map((template) => (
            <option key={template.template_id} value={template.template_id}>
              {template.name?.text || template.template_id}
            </option>
          ))}
        </select>
      </label>
      {templateResolution && templateResolution.repairRequired && (
        <div role="alert" className="adv-repair" aria-label="Template repair required">
          <p><b>Repair required.</b> This template was {templateResolution.reason === 'removed' ? 'removed' : 'changed'}; its pinned digest no longer matches. It will not be used — repair before sending. No substitute template is chosen.</p>
          {driftList(templateResolution.drifted)}
        </div>
      )}
      {selectedTemplate && (!templateResolution || !templateResolution.repairRequired) && (
        <div className="adv-template-body">
          <h4>Full template text (visible before send)</h4>
          <pre className="adv-template-text">{selectedTemplate.body?.text}</pre>
          {Array.isArray(selectedTemplate.substitutions) && selectedTemplate.substitutions.length > 0 && (
            <fieldset className="adv-substitutions">
              <legend>Declared substitutions</legend>
              {selectedTemplate.substitutions.map((sub) => (
                <label key={sub.name}>
                  {sub.name}
                  <input
                    aria-label={`substitution ${sub.name}`}
                    value={bindings[sub.name] || ''}
                    onChange={(event) => setBindings((current) => ({ ...current, [sub.name]: event.target.value }))}
                  />
                </label>
              ))}
            </fieldset>
          )}
          <button type="button" onClick={previewDeclared}>Preview declared instructions</button>
          {declaredView.available && (
            <section className="adv-declared" aria-label="Declared instructions">
              <h4>Declared instructions ({declaredView.provenance}) — recorded as declared, not a hidden prompt</h4>
              <pre className="adv-declared-text">{declaredView.text}</pre>
              {declaredView.substitutions.length > 0 && (
                <ul className="adv-declared-subs">
                  {declaredView.substitutions.map((sub) => (
                    <li key={sub.name}><b>{sub.name}</b> = <code>{sub.value}</code></li>
                  ))}
                </ul>
              )}
            </section>
          )}
        </div>
      )}
    </section>

    {/* ---- Ratings (T010) ---- */}
    <section aria-label="Route ratings">
      <h3>Route ratings</h3>
      <p className="adv-muted">Informal preference evidence only — never model qualification or delivery evidence.</p>
      <label>
        Route
        <select aria-label="Rating route" value={ratingRouteId} onChange={(event) => setRatingRouteId(event.target.value)}>
          <option value="">Select a route…</option>
          {routes.map((route) => (
            <option key={route.route_id} value={route.route_id}>{route.display_name || route.route_id}</option>
          ))}
        </select>
      </label>
      <label>
        Criterion
        <select aria-label="Rating criterion" value={ratingCriterionId} onChange={(event) => setRatingCriterionId(event.target.value)}>
          <option value="">Select a declared criterion…</option>
          {criteria.map((criterion) => (
            <option key={criterion.criterion_id} value={criterion.criterion_id}>
              {criterion.label?.text || criterion.criterion_id}
            </option>
          ))}
        </select>
      </label>
      <label>
        Score
        <input aria-label="Rating score" type="number" min={1} max={5} value={ratingScore}
          onChange={(event) => setRatingScore(event.target.value)} />
      </label>
      <button type="button" onClick={recordRating} disabled={!canRate} aria-label="Record rating">Record rating</button>
      {!ratingCriterionId && <p className="adv-muted">Choose a declared criterion to record a rating.</p>}

      {aggregates && Array.isArray(aggregates.aggregates) && (
        <section className="adv-aggregates" aria-label="Rating aggregates">
          <p className="adv-nonqual"><b>Non-qualification.</b> {aggregates.disclaimer?.text || 'Informal preference evidence only.'}</p>
          {aggregates.aggregates.length === 0
            ? <p className="adv-muted">No ratings yet.</p>
            : <ul className="adv-aggregate-rows">
                {aggregates.aggregates.map((row) => (
                  <li key={`${row.route_id}:${row.criterion_id}`}>
                    <code>{row.route_id}</code> — {row.criterion_label?.text || row.criterion_id}:
                    {' '}{row.count} rating{row.count === 1 ? '' : 's'}, avg {(row.average_score_milli / 1000).toFixed(1)}
                    {' '}<span className="adv-nonqual-tag">(non-qualification)</span>
                  </li>
                ))}
              </ul>}
        </section>
      )}
    </section>
  </section>
}
