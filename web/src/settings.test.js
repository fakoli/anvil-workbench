import { describe, expect, it } from 'vitest'
import {
  ACTOR_VIEW_FIELDS, ACTOR_SCOPES, describeSetting, controlKind, changeAffordance, ownerLabel,
  settingTier, indexEffective, explainEffective, settingMatchesQuery, filterSettings, groupSettings,
  buildSettingsModel, validateSettingValue, staleDraftState, formatApprovalPreview, approvalPreviewIsRedacted,
} from './settings'

// A fixture matching EXACTLY what workbench/contracts.py settings_actor_view
// emits: personal- and project-owned descriptors only (authority/secret/path-like
// dropped), each carrying only the _SETTINGS_ACTOR_VIEW_FIELDS. Traceable to
// docs/contracts/examples/settings-descriptor.v1.json after that projection.
const actorView = {
  schema_version: 'workbench-settings-descriptor/v1',
  catalog_id: 'workbench.settings.initial',
  revision: '1.0.0',
  settings: [
    { id: 'personal.landing_surface', title: 'Default landing surface', description: 'The surface an actor opens on.', type: 'enum', scope: 'personal', sensitivity: 'public', mutability: 'mutable', application_timing: 'immediate', allowed_values: ['chat', 'delivery', 'dashboard'], default: 'chat' },
    { id: 'personal.voice_autoplay', title: 'Voice auto-play', description: 'Play synthesized replies aloud automatically.', type: 'bool', scope: 'personal', sensitivity: 'public', mutability: 'mutable', application_timing: 'next_session', default: false },
    { id: 'personal.default_chat_route', title: 'Default chat route', description: 'An allowed Anvil Serving route id.', type: 'id_ref', scope: 'personal', sensitivity: 'public', mutability: 'mutable', application_timing: 'next_turn', ref_kind: 'route', default: 'route.chat-fast', policy_ceiling: { ceiling_setting: 'policy.route_allowlist_profile', note: 'Route choice stays within the approved profile.' } },
    { id: 'personal.chat_transcript_retention_days', title: 'Chat transcript retention (days)', description: 'Actor-chosen retention, capped by the operator ceiling.', type: 'int', scope: 'personal', sensitivity: 'public', mutability: 'mutable', application_timing: 'next_run', bounds: { min: 1, max: 90 }, default: 30, policy_ceiling: { ceiling_setting: 'policy.transcript_retention_max_days' } },
    { id: 'project.delivery_route', title: 'Default delivery route', type: 'id_ref', scope: 'project', sensitivity: 'public', mutability: 'mutable', application_timing: 'next_run', ref_kind: 'route', default: 'route.delivery-heavy', policy_ceiling: { ceiling_setting: 'policy.route_allowlist_profile' } },
    { id: 'project.default_capability_profile', title: 'Default capability profile', description: 'The reviewed capability profile digest a run pins.', type: 'digest_ref', scope: 'project', sensitivity: 'public', mutability: 'mutable', application_timing: 'next_run', ref_kind: 'capability', default: 'sha256:' + 'a1'.repeat(32) },
  ],
}

const effective = [
  { setting_id: 'personal.landing_surface', scope: 'personal', value: 'chat', source: 'default' },
  { setting_id: 'personal.voice_autoplay', scope: 'personal', value: true, source: 'stored' },
  { setting_id: 'personal.chat_transcript_retention_days', scope: 'personal', value: 30, source: 'clamped' },
  { setting_id: 'personal.default_chat_route', scope: 'personal', value: 'route.chat-fast', source: 'repaired', repair: 'A removed route fell back to the reviewed default.' },
  { setting_id: 'project.delivery_route', scope: 'project', value: 'route.delivery-heavy', source: 'default' },
]

describe('settings descriptor projection (T005.1/T005.3)', () => {
  it('projects only the served actor-view fields and derives section/tier/control', () => {
    const described = describeSetting(actorView.settings[0])
    expect(described.title).toBe('Default landing surface')
    expect(described.scope).toBe('personal')
    expect(described.tier).toBe('common')
    expect(described.control).toBe('select')
    expect(described.allowedValues).toEqual(['chat', 'delivery', 'dashboard'])
    // Only declared actor-view fields inform the projection.
    expect(ACTOR_VIEW_FIELDS).toContain('policy_ceiling')
  })

  it('maps every descriptor type to a CLOSED native control set (no free path/command box)', () => {
    expect(controlKind({ type: 'bool' })).toBe('checkbox')
    expect(controlKind({ type: 'enum' })).toBe('select')
    expect(controlKind({ type: 'int' })).toBe('number')
    expect(controlKind({ type: 'id_ref' })).toBe('reference')
    expect(controlKind({ type: 'digest_ref' })).toBe('reference')
    expect(controlKind({ type: 'string' })).toBe('text')
    const kinds = new Set(actorView.settings.map((s) => controlKind(s)))
    for (const kind of kinds) expect(['checkbox', 'select', 'number', 'reference', 'text']).toContain(kind)
  })

  it('never surfaces an authority scope in the actor view (closed-set)', () => {
    const model = buildSettingsModel({ catalog: actorView, effective })
    for (const setting of model.settings) expect(ACTOR_SCOPES).toContain(setting.scope)
  })

  it('distinguishes save / approval / read-only affordances so none masquerades as the others (T005.4)', () => {
    expect(changeAffordance({ mutability: 'mutable' })).toBe('save')
    expect(changeAffordance({ mutability: 'approval_gated' })).toBe('approval')
    expect(changeAffordance({ mutability: 'read_only' })).toBe('read_only')
    expect(changeAffordance({ mutability: 'env_only' })).toBe('read_only')
    expect(ownerLabel({ scope: 'project' })).toMatch(/project/i)
    expect(settingTier({ scope: 'project' })).toBe('advanced')
  })
})

describe('effective-value explanation (T005.4)', () => {
  it('explains WHY a value is effective from the resolver source, naming the policy ceiling', () => {
    const model = buildSettingsModel({ catalog: actorView, effective })
    const byId = Object.fromEntries(model.settings.map((s) => [s.id, s]))
    const map = indexEffective(effective)
    expect(explainEffective(byId['personal.voice_autoplay'], map.get('personal.voice_autoplay')).source).toBe('stored')
    expect(explainEffective(byId['personal.landing_surface'], map.get('personal.landing_surface')).source).toBe('default')
    const clamped = explainEffective(byId['personal.chat_transcript_retention_days'], map.get('personal.chat_transcript_retention_days'))
    expect(clamped.source).toBe('clamped')
    expect(clamped.text).toContain('policy.transcript_retention_max_days')
    const repaired = explainEffective(byId['personal.default_chat_route'], map.get('personal.default_chat_route'))
    expect(repaired.source).toBe('repaired')
    expect(repaired.text).toContain('fell back')
  })
})

describe('search + grouping (T005.2)', () => {
  it('finds settings by label, description, section, and safe keyword', () => {
    const described = actorView.settings.map(describeSetting)
    const bySection = filterSettings(described, 'voice') // section keyword
    expect(bySection.map((s) => s.id)).toContain('personal.voice_autoplay')
    expect(filterSettings(described, 'retention').map((s) => s.id)).toContain('personal.chat_transcript_retention_days') // label
    expect(filterSettings(described, 'privacy').map((s) => s.id)).toContain('personal.chat_transcript_retention_days') // section keyword
    expect(filterSettings(described, 'landing surface').map((s) => s.id)).toContain('personal.landing_surface') // description/label
    expect(filterSettings(described, 'zzzz')).toHaveLength(0)
    expect(settingMatchesQuery(described[0], '')).toBe(true) // empty matches all
  })

  it('groups COMMON actor preferences ahead of ADVANCED project/system controls', () => {
    const described = actorView.settings.map(describeSetting)
    const groups = groupSettings(described)
    const tiers = groups.map((g) => g.tier)
    const firstAdvanced = tiers.indexOf('advanced')
    const lastCommon = tiers.lastIndexOf('common')
    expect(lastCommon).toBeLessThan(firstAdvanced) // every common section precedes any advanced one
    expect(groups.every((g) => g.settings.length > 0)).toBe(true) // no empty sections
  })
})

describe('value validation (T005.3)', () => {
  it('rejects an out-of-set enum and an out-of-bounds int with an accessible repair message', () => {
    const enumSetting = describeSetting(actorView.settings[0])
    expect(validateSettingValue(enumSetting, 'chat')).toEqual({ valid: true, value: 'chat' })
    const badEnum = validateSettingValue(enumSetting, 'nope')
    expect(badEnum.valid).toBe(false)
    expect(badEnum.message).toMatch(/chat/)

    const intSetting = describeSetting(actorView.settings[3])
    expect(validateSettingValue(intSetting, '45')).toEqual({ valid: true, value: 45 })
    expect(validateSettingValue(intSetting, '0').valid).toBe(false)
    expect(validateSettingValue(intSetting, '9999').valid).toBe(false)
    expect(validateSettingValue(intSetting, '1.5').valid).toBe(false)
  })

  it('keeps a reference id-shaped — no path or command can be submitted (closed-set)', () => {
    const refSetting = describeSetting(actorView.settings[2])
    expect(validateSettingValue(refSetting, 'route.chat-fast')).toEqual({ valid: true, value: 'route.chat-fast' })
    expect(validateSettingValue(refSetting, '/etc/passwd').valid).toBe(false)
    expect(validateSettingValue(refSetting, 'rm -rf /').valid).toBe(false)
    expect(validateSettingValue(refSetting, '').valid).toBe(false)
  })

  it('treats a bool as always-valid boolean', () => {
    const boolSetting = describeSetting(actorView.settings[1])
    expect(validateSettingValue(boolSetting, true)).toEqual({ valid: true, value: true })
    expect(validateSettingValue(boolSetting, '')).toEqual({ valid: true, value: false })
  })
})

describe('stale-draft + approval-preview formatting (T005.3/T005.4)', () => {
  it('preserves the local draft on a stale write', () => {
    const state = staleDraftState('format_12h', { currentVersion: 7, reloadRequired: true, message: 'changed elsewhere' })
    expect(state.draftValue).toBe('format_12h')
    expect(state.currentVersion).toBe(7)
    expect(state.reloadRequired).toBe(true)
  })

  it('formats an approval preview with operation type, material change, target scope, expiry, and a REDACTED fingerprint', () => {
    const response = {
      preview: {
        digest: 'sha256:' + 'c'.repeat(64),
        operation: { operation: 'preference.set', setting_id: 'project.delivery_route', scope: 'project', value: 'route.delivery-heavy' },
        effect_summary: 'set project.delivery_route (project) via hub-local',
      },
      target: 'anvil-preferences', hub_local: true, requires_approval: true, idempotency_key: 'policyop:project_1:sha256...',
    }
    const formatted = formatApprovalPreview(response)
    expect(formatted.operationType).toBe('preference.set')
    expect(formatted.targetScope).toBe('project')
    expect(formatted.materialChange.value).toBe('route.delivery-heavy')
    expect(formatted.fingerprint).toMatch(/^sha256:[0-9a-f]{64}$/)
    expect(approvalPreviewIsRedacted(formatted)).toBe(true)
  })

  it('flags a preview that would leak a host:port or url as NOT redacted', () => {
    expect(approvalPreviewIsRedacted({ fingerprint: 'sha256:' + 'd'.repeat(64), materialChange: { summary: 'connect https://provider.example:8443/key' } })).toBe(false)
    expect(approvalPreviewIsRedacted({ fingerprint: 'not-a-hash', materialChange: { summary: 'ok' } })).toBe(false)
    // A DOTLESS host:port (e.g. an internal `serving:8443`) has no URL scheme and
    // no dotted domain, but the `:PORT` still leaks a network endpoint and must be
    // caught by the denylist.
    expect(approvalPreviewIsRedacted({ fingerprint: 'sha256:' + 'd'.repeat(64), materialChange: { summary: 'target serving:8443' } })).toBe(false)
  })
})
