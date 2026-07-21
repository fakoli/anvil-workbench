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
