import { describe, expect, it } from 'vitest'
import {
  PLUGIN_FIELDS, TOOL_FIELDS, CREDENTIAL_FIELDS, CATEGORY_KINDS,
  describePlugin, describeTool, describeCredential, credentialIsReferenceOnly, credentialSummary,
  describeReceipt, receiptCardKind, receiptApprovalState, toolApprovalState, pluginApprovalState,
  describeGates, toolPermissionSummary, buildCategoryModel, categoryLabel, categoryPermissionModel,
  describeSkill, effectLabel, dataScopeLabel, schemaFieldNames,
} from './plugin-catalog'

// A plugin fixture traceable to `_published_plugin` (workbench/plugin_host.py):
// the EXACT projected field set, credentials by reference only.
const servedPlugin = {
  plugin_id: 'deploy-notify',
  title: 'Deploy Notifier',
  version: '1.2.0',
  plugin_digest: 'sha256:' + 'a'.repeat(64),
  publisher: { name: 'Anvil Labs', kind: 'first_party' },
  description: 'Posts deployment notes to a reviewed channel.',
  support_status: 'supported',
  credential: { requirement: 'host_owned', owner_host: 'notify-connector', credential_refs: ['deploy-channel-ref'] },
  tools: [
    {
      tool_id: 'post_note', title: 'Post note', summary: 'Post a redacted deploy note.',
      effect: 'external_effect',
      gates: { preview: 'required', confirmation: 'required', human_approval: 'required', approval_action: 'invoke_effect_tool' },
      data_access: ['read_task_context'],
      input_schema: { type: 'object', properties: { message: { type: 'string' } } },
      output_schema: { type: 'object', properties: { posted: { type: 'boolean' } } },
    },
  ],
}

describe('closed served field sets', () => {
  it('describePlugin projects ONLY the declared plugin fields and drops an injected one', () => {
    const hostile = { ...servedPlugin, command: 'rm -rf /', openapi_source: { document_digest: 'x' } }
    const described = describePlugin(hostile)
    // The model exposes no `command` and no undeclared passthrough.
    expect('command' in described).toBe(false)
    expect(described.pluginId).toBe('deploy-notify')
    expect(PLUGIN_FIELDS).toContain('plugin_digest')
    expect(TOOL_FIELDS).toContain('effect')
  })

  it('describeTool projects only the declared tool fields', () => {
    const tool = describeTool({ ...servedPlugin.tools[0], execution: 'shell', operation_digest: 'x' }, { pluginId: 'p' })
    expect('execution' in tool).toBe(false)
    expect(tool.toolId).toBe('post_note')
    expect(tool.key).toBe('p:post_note')
  })
})

describe('credentials by reference only (R004 / T003)', () => {
  it('describeCredential keeps only requirement/owner_host/refs and drops any secret-bearing field', () => {
    const hostile = { requirement: 'host_owned', owner_host: 'notify-connector', credential_refs: ['deploy-channel-ref'], value: 's3cr3t', token: 'ghp_leak', password: 'p' }
    const cred = describeCredential(hostile)
    expect(cred).toEqual({ requirement: 'host_owned', ownerHost: 'notify-connector', refs: ['deploy-channel-ref'] })
    expect('value' in cred).toBe(false)
    expect('token' in cred).toBe(false)
    expect(JSON.stringify(cred)).not.toMatch(/s3cr3t|ghp_leak/)
    expect(CREDENTIAL_FIELDS).toEqual(['requirement', 'owner_host', 'credential_refs'])
  })

  it('credentialIsReferenceOnly flags a secret-bearing raw object and a non-id-shaped ref', () => {
    expect(credentialIsReferenceOnly({ requirement: 'host_owned', owner_host: 'h', credential_refs: ['deploy-channel-ref'] })).toBe(true)
    expect(credentialIsReferenceOnly({ requirement: 'host_owned', value: 'secret' })).toBe(false)
    // A ref that smuggled a URL/host:port/path is rejected.
    expect(credentialIsReferenceOnly({ credential_refs: ['https://x:8443/tok'] })).toBe(false)
  })

  it('credentialSummary and a none-requirement credential never expose a value', () => {
    expect(credentialSummary(describeCredential(servedPlugin.credential))).toMatch(/notify-connector/)
    expect(credentialSummary(describeCredential(servedPlugin.credential))).toMatch(/deploy-channel-ref/)
    const none = describeCredential({ requirement: 'none' })
    expect(none).toEqual({ requirement: 'none', ownerHost: null, refs: [] })
    expect(credentialSummary(none)).toBe('No credential required')
  })
})

describe('approval + permission summaries', () => {
  it('a catalog plugin is approved (reviewed + enabled) since published() serves only those', () => {
    expect(pluginApprovalState()).toEqual({ state: 'approved', label: 'Reviewed & enabled', tone: 'green' })
  })

  it('tool approval state derives from the human_approval gate', () => {
    expect(toolApprovalState(describeGates({ human_approval: 'required' })).state).toBe('approval_required')
    expect(toolApprovalState(describeGates({ human_approval: 'not_required' })).state).toBe('no_approval')
  })

  it('toolPermissionSummary names effect, approval, gates, and data policy in text', () => {
    const tool = describePlugin(servedPlugin).tools[0]
    const summary = toolPermissionSummary(tool)
    expect(summary).toMatch(/External effect/)
    expect(summary).toMatch(/Human approval required/)
    expect(summary).toMatch(/Read task context/)
  })

  it('effect and data-scope labels come from the declared enums', () => {
    expect(effectLabel('state_mutation')).toBe('State mutation')
    expect(dataScopeLabel('read_conversation_context')).toBe('Read conversation context')
    expect(schemaFieldNames({ properties: { a: {}, b: {} } })).toEqual(['a', 'b'])
    expect(schemaFieldNames({})).toEqual([])
  })
})

describe('receipt projection (plugin-receipt.v1)', () => {
  const base = {
    schema_version: 'workbench-plugin-receipt/v1',
    receipt_id: 'plugrcpt_' + 'a'.repeat(10),
    request_digest: 'sha256:' + 'd'.repeat(64),
    plugin: { plugin_id: 'deploy-notify', plugin_digest: 'sha256:' + 'a'.repeat(64) },
    redaction: { status: 'redacted' },
  }

  it('an accepted receipt yields a result card carrying an output digest and credential-by-reference', () => {
    const receipt = describeReceipt({
      ...base, kind: 'tool_call', tool_id: 'post_note', status: 'accepted', effect: 'external_effect',
      credential_use: { requirement: 'host_owned', owner_host: 'notify-connector', credential_refs: ['deploy-channel-ref'] },
      result: { output_digest: 'sha256:' + 'c'.repeat(64), output_summary: 'Note posted.' },
    })
    expect(receiptCardKind(receipt)).toBe('result')
    expect(receipt.approval).toEqual({ state: 'accepted', label: 'Approved & applied', tone: 'green' })
    expect(receipt.result.outputDigest).toBe('sha256:' + 'c'.repeat(64))
    expect(receipt.credentialUse).toEqual({ requirement: 'host_owned', ownerHost: 'notify-connector', refs: ['deploy-channel-ref'] })
    expect('value' in receipt.credentialUse).toBe(false)
  })

  it('a denied receipt yields an error card with a stable code and safe summary', () => {
    const receipt = describeReceipt({
      ...base, kind: 'install', status: 'denied', effect: 'plugin_lifecycle',
      error: { code: 'digest_drift', safe_summary: 'The pinned plugin digest does not match the reviewed catalog.', retryable: false },
    })
    expect(receiptCardKind(receipt)).toBe('error')
    expect(receipt.error.code).toBe('digest_drift')
    expect(receipt.approval.label).toBe('Denied')
  })

  it('a reconcile receipt yields a reconcile card', () => {
    const receipt = describeReceipt({
      ...base, kind: 'install', status: 'reconcile', effect: 'plugin_lifecycle',
      reconciliation: { code: 'install_outcome_unknown', safe_summary: 'The install outcome is unknown and awaits reconciliation.' },
    })
    expect(receiptCardKind(receipt)).toBe('reconcile')
    expect(receiptApprovalState('reconcile').state).toBe('reconcile')
  })
})

describe('five distinguishable categories (criterion 1)', () => {
  it('buildCategoryModel returns the five categories in order with distinct labels + permission models', () => {
    const model = buildCategoryModel({
      plugins: [servedPlugin],
      skills: [{ skill_id: 'lint', description: 'Run linters', content_sha256: 'sha256:' + 'b'.repeat(64), bridge_id: 'br1' }],
      routerConfigured: true,
    })
    expect(model.map((c) => c.kind)).toEqual(CATEGORY_KINDS)
    const labels = model.map((c) => c.label)
    expect(new Set(labels).size).toBe(5) // all distinct
    const perms = model.map((c) => c.permissionModel)
    expect(new Set(perms).size).toBe(5) // distinct permission models
    // Plugins and tools are populated from the live catalog; skills from bootstrap.
    expect(model.find((c) => c.kind === 'plugin').items).toHaveLength(1)
    expect(model.find((c) => c.kind === 'tool').items).toHaveLength(1)
    expect(model.find((c) => c.kind === 'skill').available).toBe(true)
    // Routes reflect the real router flag; delivery operations are truthfully not
    // enumerated in the reviewed plugin catalog.
    expect(model.find((c) => c.kind === 'route').available).toBe(true)
    expect(model.find((c) => c.kind === 'delivery_operation').available).toBe(false)
  })

  it('an unconfigured router and no skills render truthful unavailable categories, never fabricated items', () => {
    const model = buildCategoryModel({ plugins: [], skills: [], routerConfigured: false })
    expect(model.find((c) => c.kind === 'route').available).toBe(false)
    expect(model.find((c) => c.kind === 'skill').available).toBe(false)
    expect(model.find((c) => c.kind === 'delivery_operation').items).toHaveLength(0)
    expect(categoryLabel('tool')).toBe('Plugin tools')
    expect(categoryPermissionModel('route')).toMatch(/Read-only/)
  })

  it('describeSkill exposes name/description/digest only — never a path or body', () => {
    const skill = describeSkill({ skill_id: 'lint', description: 'Run linters', content_sha256: 'sha256:' + 'b'.repeat(64), bridge_id: 'br1', path: '/home/x/skill' })
    expect('path' in skill).toBe(false)
    expect(skill.permissionSummary).toMatch(/digest/)
  })
})
