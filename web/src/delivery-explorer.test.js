import { describe, expect, it } from 'vitest'
import {
  deliverBlockReason,
  describeEligibility,
  describePrdContent,
  describeTaskReference,
  filterDescribedTasks,
  freshnessLabel,
  nextDeliverCandidate,
  progressSummaryLabel,
  scopedTaskId,
  summarizeProgress,
  workflowEntryModel,
} from './delivery-explorer'

// A task reference exactly as the router serves it (task-reference.v1), so these
// tests break if the shape drifts from the merged contract.
function taskRef(prdId, taskId, overrides = {}) {
  return {
    schema_version: 'workbench-task-reference/v1',
    ref: { prd_id: prdId, task_id: taskId, prd_revision: overrides.revision ?? 4 },
    scoped_id: `${prdId}:${taskId}`,
    run_label: `${prdId}:${taskId}@r${overrides.revision ?? 4}`,
    source: { provider: 'anvil-state', snapshot_digest: `sha256:${'a'.repeat(64)}` },
    hierarchy: { prd_id: prdId, prd_title: overrides.prdTitle ?? 'Chat-first Workbench', feature_id: `${prdId}:F001` },
    summary: {
      content_trust: 'untrusted_task_data',
      title: overrides.title ?? 'Add routed chat',
      status: overrides.status ?? 'ready',
      priority: overrides.priority ?? 'critical',
      latest_delivery_status: overrides.delivery ?? 'not_started',
      acceptance_criteria_count: overrides.ac ?? 3,
      verification_summary: overrides.vs ?? 'Three acceptance criteria; one automated verification defined.',
      depends_on: overrides.deps ?? [{ prd_id: prdId, task_id: 'T000', prd_revision: 4 }],
    },
  }
}

describe('scopedTaskId (R004 / criterion 2)', () => {
  it('prefers the served scoped_id and never returns a bare task number', () => {
    expect(scopedTaskId(taskRef('release-alpha', 'T001'))).toBe('release-alpha:T001')
  })

  it('derives the scoped id from the typed ref when scoped_id is absent', () => {
    expect(scopedTaskId({ ref: { prd_id: 'release-beta', task_id: 'T001' } })).toBe('release-beta:T001')
  })

  it('keeps two PRDs T001 tasks distinct', () => {
    expect(scopedTaskId(taskRef('release-alpha', 'T001'))).not.toBe(scopedTaskId(taskRef('release-beta', 'T001')))
  })
})

describe('describeTaskReference', () => {
  it('projects title, lineage, status, delivery, criteria, verification, and deps', () => {
    const described = describeTaskReference(taskRef('release-alpha', 'T001'))
    expect(described.scopedId).toBe('release-alpha:T001')
    expect(described.title).toBe('Add routed chat')
    expect(described.prdTitle).toBe('Chat-first Workbench')
    expect(described.featureId).toBe('release-alpha:F001')
    expect(described.status).toBe('ready')
    expect(described.latestDeliveryStatus).toBe('not_started')
    expect(described.acceptanceCriteriaCount).toBe(3)
    expect(described.verificationSummary).toContain('automated verification')
    expect(described.dependsOn).toEqual([{ scopedId: 'release-alpha:T000', prdRevision: 4 }])
    expect(described.runLabel).toBe('release-alpha:T001@r4')
  })

  it('falls back to the scoped id (never a bare number) for a titleless task', () => {
    const described = describeTaskReference(taskRef('release-alpha', 'T007', { title: '   ' }))
    expect(described.title).toBe('release-alpha:T007')
  })
})

describe('describePrdContent', () => {
  const content = {
    schema_version: 'workbench-prd-content/v1',
    provider: 'anvil-state',
    generated_at: '2026-07-19T00:00:00Z',
    prd: { prd_id: 'release-beta', title: 'State context and operations', status: 'approved', revision: 5 },
    content: { format: 'markdown', body: '# State context', truncated: true, total_bytes: 18211 },
    redaction: { status: 'redacted', ruleset: 'workbench-default-v1' },
  }

  it('projects title, release, revision, status, freshness, and truncation', () => {
    const described = describePrdContent(content)
    expect(described.title).toBe('State context and operations')
    expect(described.release).toBe('release-beta')
    expect(described.revision).toBe(5)
    expect(described.status).toBe('approved')
    expect(described.generatedAt).toBe('2026-07-19T00:00:00Z')
    expect(described.truncated).toBe(true)
    expect(described.totalBytes).toBe(18211)
    expect(described.body).toBe('# State context')
  })
})

describe('summarizeProgress / progressSummaryLabel', () => {
  it('tallies delivery statuses across the PRD task rows', () => {
    const tasks = [
      taskRef('p', 'T001', { delivery: 'accepted' }),
      taskRef('p', 'T002', { delivery: 'accepted' }),
      taskRef('p', 'T003', { delivery: 'not_started' }),
    ]
    const progress = summarizeProgress(tasks)
    expect(progress.total).toBe(3)
    expect(progress.accepted).toBe(2)
    expect(progress.byDelivery).toEqual({ accepted: 2, not_started: 1 })
    expect(progressSummaryLabel(tasks)).toBe('2/3 tasks at accepted delivery')
  })

  it('is truthful for an empty projection', () => {
    expect(progressSummaryLabel([])).toBe('No tasks in this PRD projection')
  })
})

describe('freshnessLabel', () => {
  it('reads the source timestamp and never fabricates one when absent', () => {
    expect(freshnessLabel('2026-07-19T00:00:00Z')).toBe('source as of 2026-07-19T00:00:00Z')
    expect(freshnessLabel(null)).toBe('source freshness unknown')
  })
})

describe('describeEligibility', () => {
  it('projects the derived state, flag, and human-safe reasons', () => {
    const verdict = describeEligibility({
      schema_version: 'workbench-delivery-eligibility/v1',
      scoped_id: 'release-alpha:T001',
      eligible: false,
      state: 'blocked',
      reasons: [{ class: 'blocked', code: 'blocked.dependency_unmet', explanation: 'A dependency has not merged.' }],
    })
    expect(verdict.state).toBe('blocked')
    expect(verdict.eligible).toBe(false)
    expect(verdict.reasons).toEqual([{ class: 'blocked', code: 'blocked.dependency_unmet', explanation: 'A dependency has not merged.' }])
  })

  it('returns null when no verdict is present so the UI degrades truthfully', () => {
    expect(describeEligibility(null)).toBeNull()
    expect(describeEligibility(undefined)).toBeNull()
  })
})

describe('nextDeliverCandidate (T006 criterion 2)', () => {
  it('previews the FIRST State-ranked row as the single candidate', () => {
    const cand = nextDeliverCandidate([
      taskRef('release-alpha', 'T001', { title: 'Add routed chat' }),
      taskRef('release-alpha', 'T002', { title: 'Persist retention' }),
    ])
    expect(cand.scopedId).toBe('release-alpha:T001')
    expect(cand.title).toBe('Add routed chat')
  })

  it('never skips a blocked ranked head to reach a later ready task', () => {
    // The head is blocked and a later row is ready; the candidate is still the
    // blocked head — the flow must surface it blocked, not silently skip it.
    const cand = nextDeliverCandidate([
      taskRef('release-alpha', 'T001', { title: 'Blocked head', status: 'blocked' }),
      taskRef('release-alpha', 'T002', { title: 'Ready later', status: 'ready' }),
    ])
    expect(cand.scopedId).toBe('release-alpha:T001')
    expect(cand.status).toBe('blocked')
  })

  it('is truthfully null for an empty or absent list', () => {
    expect(nextDeliverCandidate([])).toBeNull()
    expect(nextDeliverCandidate(undefined)).toBeNull()
  })
})

describe('deliverBlockReason (T006 criteria 2 + 4)', () => {
  const candidate = describeTaskReference(taskRef('release-alpha', 'T001'))
  // The ONLY contract-valid eligible verdict: state 'eligible' with an info.ready
  // reason (delivery-eligibility.v1 enums + the derived-state cross-check). The
  // old {state:'ready', class:'ready', code:'ready.all_clear'} shape is one the
  // server can never serve, so it must not appear even in a fixture.
  const ready = { status: 'loaded', value: describeEligibility({ eligible: true, state: 'eligible', reasons: [{ class: 'info', code: 'info.ready', explanation: 'All clear.' }] }), message: null }

  it('requires a startable session first', () => {
    expect(deliverBlockReason({ candidate, eligibility: ready, hasSession: false })).toMatchObject({ code: 'deliver.no_session' })
  })

  it('requires a loaded candidate', () => {
    expect(deliverBlockReason({ candidate: null, eligibility: ready, hasSession: true })).toMatchObject({ code: 'deliver.no_candidate' })
  })

  it('blocks while eligibility is still loading or failed', () => {
    expect(deliverBlockReason({ candidate, eligibility: { status: 'loading' }, hasSession: true })).toMatchObject({ code: 'deliver.eligibility_loading' })
    expect(deliverBlockReason({ candidate, eligibility: { status: 'error', message: 'down' }, hasSession: true })).toMatchObject({ code: 'deliver.eligibility_unavailable', text: 'down' })
  })

  it('surfaces a blocked verdict’s own leading reason verbatim (no fabrication)', () => {
    const eligibility = { status: 'loaded', value: describeEligibility({ eligible: false, state: 'blocked', reasons: [{ class: 'blocked', code: 'blocked.dependency_unmet', explanation: 'A dependency has not merged.' }] }), message: null }
    expect(deliverBlockReason({ candidate, eligibility, hasSession: true })).toEqual({ code: 'blocked.dependency_unmet', text: 'A dependency has not merged.' })
  })

  it('returns null (deliverable) only when session, candidate, and an eligible verdict all hold', () => {
    expect(deliverBlockReason({ candidate, eligibility: ready, hasSession: true })).toBeNull()
  })
})

describe('workflowEntryModel (T006 SHOULD #2)', () => {
  const withDefinition = (model) => ({
    id: 'workflow_1', session_id: 'session_1', status: 'draft',
    definition: { entry: 'implement', steps: [{ id: 'implement', kind: 'agent', model, skills: [], next: ['review'] }, { id: 'review', kind: 'approval_wait', next: [] }] },
  })

  it('reads the model off the entry agent step of the version-pinned definition', () => {
    expect(workflowEntryModel(withDefinition('heavy-local'))).toBe('heavy-local')
  })

  it('is null (truthful default) when the definition, entry step, or model is missing', () => {
    expect(workflowEntryModel({ id: 'workflow_1', status: 'draft' })).toBeNull() // the served shape with no definition
    expect(workflowEntryModel(withDefinition(''))).toBeNull() // entry step pins no model
    expect(workflowEntryModel(undefined)).toBeNull()
    // A non-agent entry step never yields a route (v1 requires an agent entry).
    expect(workflowEntryModel({ definition: { entry: 'x', steps: [{ id: 'x', kind: 'reconcile' }] } })).toBeNull()
  })
})

describe('filterDescribedTasks', () => {
  const described = [
    describeTaskReference(taskRef('release-alpha', 'T001', { title: 'Add routed chat', status: 'ready' })),
    describeTaskReference(taskRef('release-alpha', 'T002', { title: 'Persist retention', status: 'blocked' })),
  ]

  it('returns every row for an empty query', () => {
    expect(filterDescribedTasks(described, '  ')).toHaveLength(2)
  })

  it('filters case-insensitively over title, scoped id, and status', () => {
    expect(filterDescribedTasks(described, 'routed').map((task) => task.scopedId)).toEqual(['release-alpha:T001'])
    expect(filterDescribedTasks(described, 'BLOCKED').map((task) => task.scopedId)).toEqual(['release-alpha:T002'])
    expect(filterDescribedTasks(described, 'T002').map((task) => task.scopedId)).toEqual(['release-alpha:T002'])
  })
})
