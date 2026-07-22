// Pure Advanced-playground logic (advanced-model-playground T006 / T009 / T010).
//
// The browser-side, network-free half of the preset / comparison / template /
// rating surfaces. It mirrors the merged backend shapes BYTE-FOR-SHAPE so the
// rendered repair banner, the factual comparison, the declared-instruction
// preview, and the criterion-required rating form all reason over EXACTLY the
// shapes the hub serves — never an invented field:
//
//  * A preset resolution is `{status:"ready", preset}` or, on ANY pinned-digest
//    drift, `{status:"repair_required", preset_id, drifted_refs:[{ref_kind,id,
//    pinned_digest}]}` with NO `preset` — the server never substitutes a route or
//    tool, so the view opens repair mode instead of silently selecting a stale one.
//  * A comparison is the `advanced-comparison.v1` record: factual integer metrics,
//    and a `ranking` (a winner) present ONLY alongside a declared,
//    `non_qualification` `criterion` — so a winner is never inferred.
//  * A declared-instructions record is `{provenance:"declared", text, substitutions}`
//    — the full resolved instruction text + the named bindings, visible pre-send.
//  * A rating criterion is one of the CLOSED declared set; a rating with no
//    criterion cannot be recorded.
//
// Nothing here touches fetch, a token, or an endpoint.

// Project a preset resolution into a view model. `repairRequired` is TRUE on any
// drift; `preset` is null unless the resolution is ready — the UI must never show
// a selectable/substituted preset while a pinned digest has drifted.
//
// A THIRD status, `unverifiable`, is distinct from both: the server could not
// verify the pinned references at all (e.g. a preset store injected without a
// `live_digests_provider`). This is NOT ready — it MUST NOT collapse into the
// ready/else tail, or the panel would falsely announce a ready preset. It carries
// no selectable preset and opens no repair banner; it is a truthful "cannot
// verify right now" note.
export function resolvePresetView(result) {
  const status = result?.status
  if (status === 'repair_required') {
    const drifted = Array.isArray(result?.drifted_refs) ? result.drifted_refs : []
    return {
      status,
      repairRequired: true,
      unverifiable: false,
      presetId: result?.preset_id || '',
      drifted: drifted.map((ref) => ({ refKind: ref.ref_kind, id: ref.id, pinnedDigest: ref.pinned_digest })),
      preset: null,
    }
  }
  if (status === 'unverifiable') {
    const refs = Array.isArray(result?.unverifiable_refs) ? result.unverifiable_refs : []
    return {
      status,
      repairRequired: false,
      unverifiable: true,
      presetId: result?.preset_id || '',
      reason: result?.reason || 'cannot_verify',
      unverifiableRefs: refs.map((ref) => ({ refKind: ref.ref_kind, id: ref.id, pinnedDigest: ref.pinned_digest })),
      drifted: [],
      preset: null,
    }
  }
  if (status === 'ready' && result?.preset) {
    return { status, repairRequired: false, unverifiable: false, presetId: result.preset.preset_id, drifted: [], preset: result.preset }
  }
  return { status: status || 'unknown', repairRequired: false, unverifiable: false, presetId: '', drifted: [], preset: null }
}

// Project a template resolution into a view model, mirroring the preset shape.
// `template` is null unless ready — a removed or digest-drifted template opens
// repair mode, never a silent substitution.
export function resolveTemplateView(result) {
  const status = result?.status
  if (status === 'repair_required') {
    const drifted = Array.isArray(result?.drifted_refs) ? result.drifted_refs : []
    return {
      status,
      repairRequired: true,
      templateId: result?.template_id || '',
      reason: result?.reason || 'digest_drift',
      drifted: drifted.map((ref) => ({ refKind: ref.ref_kind, id: ref.id, pinnedDigest: ref.pinned_digest })),
      template: null,
    }
  }
  if (status === 'ready' && result?.template) {
    return { status, repairRequired: false, templateId: result.template.template_id, reason: '', drifted: [], template: result.template }
  }
  return { status: status || 'unknown', repairRequired: false, templateId: '', reason: '', drifted: [], template: null }
}

// Project an advanced-comparison.v1 record into a FACTUAL view model. `hasWinner`
// is TRUE only when a declared criterion is present; without one there is a
// factual side-by-side and NO ranking — a winner is never inferred.
export function formatComparison(record) {
  if (!record || !Array.isArray(record.attempts)) {
    return { available: false, attempts: [], criterion: null, ranking: null, hasWinner: false }
  }
  const criterion = record.criterion && typeof record.criterion === 'object'
    ? {
        criterionId: record.criterion.criterion_id,
        label: record.criterion.label?.text || record.criterion.criterion_id,
        nonQualification: record.criterion.non_qualification === true,
      }
    : null
  // A ranking is only meaningful — and only shown — with a declared criterion.
  const ranking = criterion && Array.isArray(record.ranking)
    ? record.ranking.map((entry) => ({ turnId: entry.turn_id, rank: entry.rank }))
    : null
  const rankByTurn = new Map((ranking || []).map((entry) => [entry.turnId, entry.rank]))
  const attempts = record.attempts.map((attempt) => ({
    turnId: attempt.turn_id,
    routeId: attempt.route?.route_id || '',
    status: attempt.status,
    metrics: { ...(attempt.metrics || {}) },
    rank: rankByTurn.has(attempt.turn_id) ? rankByTurn.get(attempt.turn_id) : null,
  }))
  return { available: true, attempts, criterion, ranking, hasWinner: Boolean(criterion && ranking) }
}

// Project a declared-instructions record for pre-send display. The text is the
// resolved full instruction body and the substitutions are the named bindings —
// both visible before send, marked declared (never a covert injected prompt).
export function declaredInstructionsView(di) {
  if (!di || typeof di.text !== 'string') {
    return { available: false, text: '', substitutions: [], provenance: '' }
  }
  return {
    available: true,
    text: di.text,
    provenance: di.provenance || 'declared',
    templateId: di.template_id || '',
    substitutions: Array.isArray(di.substitutions)
      ? di.substitutions.map((sub) => ({ name: sub.name, value: sub.value }))
      : [],
  }
}

// A rating can be recorded ONLY when a declared criterion is named and the score
// is within the declared 1..5 range — mirroring the server's criterion-required
// refusal, so the submit control is disabled until both hold.
export function canRecordRating({ criterionId, score } = {}) {
  if (!criterionId) return false
  const value = Number(score)
  return Number.isInteger(value) && value >= 1 && value <= 5
}
