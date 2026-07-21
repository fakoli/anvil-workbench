// Delivery-explorer display helpers (plan-task-delivery T003).
//
// Pure projections used by the Project/PRD/plan/task explorer. They live here
// (not in api.js) so they stay real logic when a component test mocks the
// network client `./api`. None of them touches fetch, a token, or an endpoint.
//
// Every shape mirrors the merged, redacted delivery-projection contract served
// by workbench/api.py `build_delivery_projection_router` (task-reference.v1,
// prd-content.v1, delivery-eligibility.v1). The primary rule these enforce is
// R004 / T003 criterion 2: a task's identity is its SCOPED id `<prd_id>:<task_id>`,
// never the bare `T001`, so two PRDs' `T001` tasks can never collapse into one.

// The scoped identity `<prd_id>:<task_id>`. Prefer the server-emitted
// `scoped_id` (it is contract-validated to equal this), and fall back to
// deriving it from the typed ref so a row is never keyed on a bare task number.
export function scopedTaskId(task) {
  if (task && typeof task.scoped_id === 'string' && task.scoped_id) return task.scoped_id
  const ref = task?.ref || {}
  if (ref.prd_id && ref.task_id) return `${ref.prd_id}:${ref.task_id}`
  return null
}

function trimmedOr(value, fallback) {
  return typeof value === 'string' && value.trim() ? value.trim() : fallback
}

// Normalize one task reference into a display projection. The `title` and the
// lineage (`prdTitle` / `featureId`) are the primary human hierarchy; the
// scoped id, run label, and revision are carried for disambiguation and
// secondary disclosure. A titleless task falls back to its scoped id — never a
// bare task number — so it stays distinguishable across PRDs.
export function describeTaskReference(task) {
  const ref = task?.ref || {}
  const summary = task?.summary || {}
  const hierarchy = task?.hierarchy || {}
  const source = task?.source || {}
  const scopedId = scopedTaskId(task)
  return {
    scopedId,
    prdId: ref.prd_id ?? null,
    taskId: ref.task_id ?? null,
    prdRevision: Number.isInteger(ref.prd_revision) ? ref.prd_revision : null,
    runLabel: task?.run_label ?? null,
    prdTitle: trimmedOr(hierarchy.prd_title, null),
    featureId: hierarchy.feature_id ?? null,
    title: trimmedOr(summary.title, scopedId || 'Untitled task'),
    status: summary.status ?? 'unknown',
    priority: summary.priority ?? null,
    latestDeliveryStatus: summary.latest_delivery_status ?? 'unknown',
    acceptanceCriteriaCount: Number.isInteger(summary.acceptance_criteria_count) ? summary.acceptance_criteria_count : 0,
    verificationSummary: trimmedOr(summary.verification_summary, ''),
    dependsOn: (Array.isArray(summary.depends_on) ? summary.depends_on : []).map((dep) => ({
      scopedId: dep?.prd_id && dep?.task_id ? `${dep.prd_id}:${dep.task_id}` : null,
      prdRevision: Number.isInteger(dep?.prd_revision) ? dep.prd_revision : null,
    })),
    provider: source.provider ?? null,
    snapshotDigest: source.snapshot_digest ?? null,
  }
}

// Normalize one PRD content read-model into a display projection. The PRD title
// is the primary label; `release` is the PRD/release identifier (`prd_id`), and
// `revision` + `generatedAt` are the source-freshness signals a card shows.
export function describePrdContent(content) {
  const prd = content?.prd || {}
  const body = content?.content || {}
  const redaction = content?.redaction || {}
  return {
    prdId: prd.prd_id ?? null,
    title: trimmedOr(prd.title, prd.prd_id || 'Untitled PRD'),
    release: prd.prd_id ?? null,
    revision: Number.isInteger(prd.revision) ? prd.revision : null,
    status: prd.status ?? 'unknown',
    provider: content?.provider ?? null,
    generatedAt: content?.generated_at ?? null,
    format: body.format ?? 'text',
    body: typeof body.body === 'string' ? body.body : '',
    truncated: Boolean(body.truncated),
    totalBytes: Number.isInteger(body.total_bytes) ? body.total_bytes : null,
    redactionStatus: redaction.status ?? null,
  }
}

// A human freshness label from an ISO-8601 timestamp. Never fabricates a value:
// an absent timestamp reads as unknown rather than "now".
export function freshnessLabel(generatedAt) {
  return generatedAt ? `source as of ${generatedAt}` : 'source freshness unknown'
}

// A progress summary derived from a PRD's task references: the total task count
// and a per-delivery-status tally, plus the accepted count. This is a genuine
// projection over the served rows, not a fabricated percentage.
export function summarizeProgress(tasks) {
  const described = (Array.isArray(tasks) ? tasks : []).map(describeTaskReference)
  const byDelivery = {}
  for (const task of described) {
    byDelivery[task.latestDeliveryStatus] = (byDelivery[task.latestDeliveryStatus] || 0) + 1
  }
  return { total: described.length, accepted: byDelivery.accepted || 0, byDelivery }
}

// A short, human progress line: "1/4 tasks delivered" style, truthful when the
// task list is empty or unavailable.
export function progressSummaryLabel(tasks) {
  const { total, accepted } = summarizeProgress(tasks)
  if (total === 0) return 'No tasks in this PRD projection'
  return `${accepted}/${total} tasks at accepted delivery`
}

// Normalize an eligibility verdict for display. Carries the derived state, the
// eligible flag, and the human-safe reasons (class/code/explanation). Returns
// null when no verdict is present so the caller renders a truthful unavailable
// state instead of a fabricated verdict.
export function describeEligibility(eligibility) {
  if (!eligibility) return null
  const reasons = Array.isArray(eligibility.reasons) ? eligibility.reasons : []
  return {
    scopedId: eligibility.scoped_id ?? null,
    state: eligibility.state ?? 'unknown',
    eligible: Boolean(eligibility.eligible),
    reasons: reasons.map((reason) => ({
      class: reason?.class ?? 'unknown',
      code: reason?.code ?? 'unknown',
      explanation: trimmedOr(reason?.explanation, ''),
    })),
  }
}

// The single next Deliver candidate (plan-task-delivery T006 criterion 2).
//
// State serves its task references in plan/ranked order, so the ONE candidate a
// "Deliver next" flow previews is the FIRST served row — index 0 — described for
// display. Taking the head (never re-sorting by a fabricated score, never
// scanning ahead for the first `ready` row) is what makes the preview both
// "exactly one" AND "never silently skips a blocked dependency": if the ranked
// head is blocked, the blocked head IS the candidate and the flow surfaces it
// blocked rather than quietly reaching past it to a later ready task. Returns
// null for an empty/absent list so the caller renders a truthful no-candidate
// state instead of fabricating one.
export function nextDeliverCandidate(tasks) {
  const list = Array.isArray(tasks) ? tasks : []
  if (!list.length) return null
  return describeTaskReference(list[0])
}

// The truthful reason a Deliver is blocked, or null when it can start
// (plan-task-delivery T006 criteria 2 + 4). The order is what the operator must
// resolve first: a startable session, then a loaded candidate, then State's own
// eligibility verdict. Nothing here is fabricated — a blocked verdict's own
// leading reason (its code + human-safe explanation) is surfaced verbatim, so
// the disabled control always states WHY in text and never invents a cause.
//
// `eligibility` is the caller's async wrapper `{status, value, message}` (value
// is a describeEligibility() verdict), mirroring the explorer's eligibility
// state, so a still-loading or failed eligibility read blocks the start with its
// own distinct, truthful reason rather than silently enabling it.
export function deliverBlockReason({ candidate, eligibility, hasSession } = {}) {
  if (!hasSession) {
    return { code: 'deliver.no_session', text: 'Select a startable session (a draft workflow with no active run) to deliver into.' }
  }
  if (!candidate) {
    return { code: 'deliver.no_candidate', text: 'Load a PRD to preview its next ranked candidate.' }
  }
  const status = eligibility?.status
  if (!status || status === 'idle') {
    return { code: 'deliver.eligibility_unloaded', text: 'The candidate’s delivery eligibility has not been checked yet.' }
  }
  if (status === 'loading') {
    return { code: 'deliver.eligibility_loading', text: 'Checking the candidate’s delivery eligibility…' }
  }
  if (status === 'error') {
    return { code: 'deliver.eligibility_unavailable', text: eligibility.message || 'Delivery eligibility is unavailable for this candidate.' }
  }
  const verdict = eligibility.value
  if (!verdict) {
    return { code: 'deliver.eligibility_missing', text: 'No delivery eligibility verdict for this candidate.' }
  }
  if (!verdict.eligible) {
    const primary = verdict.reasons[0]
    return {
      code: primary?.code || 'deliver.blocked',
      text: primary?.explanation || `Blocked by State (${verdict.state}).`,
    }
  }
  return null
}

// The route a workflow's first run will actually use (plan-task-delivery T006
// SHOULD #2). The served Workflow shape has NO top-level `model` field; the hub
// pins the run's model from the ENTRY agent step of the version-pinned
// `definition` (workbench/store.py start path: `step.get("model")`), and the
// browser receives the whole `definition` (models.py `as_json` = `asdict`). So
// this reads that real source — the entry step's `model` — rather than a
// non-existent `workflow.model`. Returns null when the definition, its entry
// step, or that step's model is not reliably present, so the caller shows a
// truthful default label instead of claiming a derived route.
export function workflowEntryModel(workflow) {
  const definition = workflow?.definition
  if (!definition || typeof definition !== 'object') return null
  const entryId = definition.entry
  const steps = Array.isArray(definition.steps) ? definition.steps : []
  const entry = steps.find((step) => step && step.id === entryId)
  if (!entry || entry.kind !== 'agent') return null
  return typeof entry.model === 'string' && entry.model.trim() ? entry.model.trim() : null
}

// Filter already-described task rows by a case-insensitive query over the
// human-visible fields (title, scoped id, status, delivery status). An empty
// query returns the rows unchanged.
export function filterDescribedTasks(described, query) {
  const needle = (query || '').trim().toLowerCase()
  if (!needle) return described
  return (Array.isArray(described) ? described : []).filter((task) =>
    [task.title, task.scopedId, task.status, task.latestDeliveryStatus]
      .some((field) => String(field ?? '').toLowerCase().includes(needle)),
  )
}
