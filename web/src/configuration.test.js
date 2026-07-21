import { describe, expect, it } from 'vitest'
import {
  EXPORT_EXCLUSIONS, exportStatementText, summarizeExport, exportIsRedacted,
  parseImportEnvelope, describeImportPreview, describeResetPreview, remediationFor,
} from './configuration'

describe('configuration pure module (T006.4)', () => {
  it('states the closed set of export exclusions before any download', () => {
    const text = exportStatementText().toLowerCase()
    expect(EXPORT_EXCLUSIONS.length).toBeGreaterThanOrEqual(5)
    expect(text).toMatch(/secrets/)
    expect(text).toMatch(/local filesystem paths/)
    expect(text).toMatch(/chat history/)
  })

  it('summarizes an export by opaque actor reference, never a raw identity', () => {
    const summary = summarizeExport({
      schema_version: 'workbench-configuration-export/v1',
      source: { scope: 'personal+project', actor_ref: 'actorref:0123456789abcdef', catalog_id: 'c1' },
      settings: [{ setting_id: 'personal.time_format', scope: 'personal', value: 'format_12h' }],
    })
    expect(summary.actorRef).toBe('actorref:0123456789abcdef')
    expect(summary.count).toBe(1)
    expect(summary.scope).toBe('personal+project')
  })

  it('rejects an export that carries a leak shape or a raw actor identity', () => {
    // Clean redacted export passes.
    expect(exportIsRedacted({ source: { actor_ref: 'actorref:0123456789ab' }, settings: [{ setting_id: 'personal.time_format', value: 'format_12h' }] })).toBe(true)
    // A raw (non-opaque) actor ref fails.
    expect(exportIsRedacted({ source: { actor_ref: 'alice@example.com' }, settings: [] })).toBe(false)
    // A URL / host:port leaking into a value fails.
    expect(exportIsRedacted({ source: { actor_ref: 'actorref:0123456789ab' }, settings: [{ setting_id: 'x', value: 'https://serving:8443/secret' }] })).toBe(false)
  })

  it('shows a clean export whose values include a portable INTEGER, and withholds a real leaked string', () => {
    // MIRRORS the real served shape: the export carries a numeric setting value
    // (`personal.chat_transcript_retention_days`, an int 1–90) ALONGSIDE a string
    // value. Scanning VALUES (not the serialized blob) means `:20` is a number and
    // can NEVER masquerade as a host:port — the export is shown, not withheld.
    const served = {
      schema_version: 'workbench-configuration-export/v1',
      source: { scope: 'personal', actor_ref: 'actorref:0123456789abcdef', catalog_id: 'workbench.settings.initial' },
      settings: [
        { setting_id: 'personal.chat_transcript_retention_days', scope: 'personal', value: 20 },
        { setting_id: 'personal.landing_surface', scope: 'personal', value: 'dashboard' },
      ],
    }
    expect(exportIsRedacted(served)).toBe(true)
    // A genuinely leaked dotless host:port inside a STRING value is still caught.
    const leaked = { ...served, settings: [
      { setting_id: 'personal.chat_transcript_retention_days', scope: 'personal', value: 20 },
      { setting_id: 'x', scope: 'personal', value: 'serving:8443' },
    ] }
    expect(exportIsRedacted(leaked)).toBe(false)
  })

  it('format-checks the opaque project_ref so a regression to a raw project id is caught', () => {
    // A project-scoped export carries an OPAQUE `projectref:` token; the clean case
    // passes.
    expect(exportIsRedacted({
      source: { scope: 'personal+project', actor_ref: 'actorref:0123456789ab', project_ref: 'projectref:abcdef0123456789' },
      settings: [{ setting_id: 'project.delivery_route', scope: 'project', value: 'route.delivery-heavy' }],
    })).toBe(true)
    // A raw project id leaked into project_ref is NOT the opaque shape → withheld.
    expect(exportIsRedacted({
      source: { scope: 'personal+project', actor_ref: 'actorref:0123456789ab', project_ref: 'project_1' },
      settings: [],
    })).toBe(false)
  })

  it('parses a pasted envelope and rejects non-JSON / non-object input', () => {
    expect(parseImportEnvelope('   ').ok).toBe(false)
    expect(parseImportEnvelope('not json').ok).toBe(false)
    expect(parseImportEnvelope('[1,2]').ok).toBe(false)
    const parsed = parseImportEnvelope('{"schema_version":"workbench-configuration-export/v1","settings":[]}')
    expect(parsed.ok).toBe(true)
    expect(parsed.envelope.schema_version).toBe('workbench-configuration-export/v1')
  })

  it('keeps import preview categories DISTINCT and derives canApply', () => {
    const described = describeImportPreview({
      valid: true,
      creates: [{ setting_id: 'a' }],
      changes: [{ setting_id: 'b' }],
      resets: [{ setting_id: 'c' }],
      skipped_read_only: [{ setting_id: 'd' }],
      unavailable_references: [{ setting_id: 'e' }],
      repairable: [],
      no_ops: [{ setting_id: 'f' }],
      base_versions: { a: 0 },
    })
    expect(described.creates).toHaveLength(1)
    expect(described.changes).toHaveLength(1)
    expect(described.resets).toHaveLength(1)
    expect(described.skippedReadOnly).toHaveLength(1)
    expect(described.unavailableRefs).toHaveLength(1)
    expect(described.applyCount).toBe(3)
    expect(described.canApply).toBe(true)
  })

  it('an invalid preview is never applyable and an empty preview has nothing to apply', () => {
    expect(describeImportPreview({ valid: false, repairable: [{ setting_id: 'x', reason: 'bad' }] }).canApply).toBe(false)
    expect(describeImportPreview({ valid: true, creates: [], changes: [], resets: [] }).canApply).toBe(false)
  })

  it('describes a scoped reset preview and its applyability', () => {
    const described = describeResetPreview({
      scope: 'personal',
      changes: [{ setting_id: 'personal.time_format', scope: 'personal', from: 'format_12h', to_default: 'format_24h', expected_version: 2 }],
      base_versions: { 'personal.time_format': 2 },
    })
    expect(described.canApply).toBe(true)
    expect(described.changes[0].toDefault).toBe('format_24h')
    expect(describeResetPreview({ scope: 'personal', changes: [] }).canApply).toBe(false)
  })

  it('reports scope + result + next remediation, distinctly per status', () => {
    expect(remediationFor({ status: 'applied', appliedCount: 2 })).toMatch(/2 preferences updated/)
    // An applied import reports its affected scope(s), as a reset does (T006.4 #3).
    expect(remediationFor({ status: 'applied', appliedCount: 1, scopes: ['personal'] })).toMatch(/updated in your personal scope\b/)
    expect(remediationFor({ status: 'applied', appliedCount: 3, scopes: ['personal', 'project'] })).toMatch(/updated in your personal and project scopes/)
    expect(remediationFor({ status: 'reset', scope: 'personal', appliedCount: 1 })).toMatch(/personal preferences were reset/)
    expect(remediationFor({ status: 'stale' })).toMatch(/changed since you previewed/)
    expect(remediationFor({ status: 'invalid', message: 'fix it' })).toBe('fix it')
  })
})
