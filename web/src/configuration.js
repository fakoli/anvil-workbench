// Configuration export / import / reset helpers (preferences-configuration T006.4).
//
// Pure projections for the Backup & transfer workflows. They live here (not in
// api.js) so they stay real logic when a component test mocks the network client
// `./api`. None of them touches fetch, a token, or an endpoint.
//
// Every shape mirrors the redacted configuration-transfer contract served by
// workbench/configuration_transfer.py: the versioned export envelope
// (`{schema_version, source:{scope, actor_ref, ...}, settings:[{setting_id, scope,
// value}]}`) and the typed import/reset previews (`creates / changes / resets /
// skipped_read_only / unavailable_references / repairable / base_versions`). The
// export carries ONLY portable actor/project settings and an OPAQUE actor
// reference server-side, so this module never renders, accepts, echoes, or caches
// a raw credential, provider key, path, or actor identity: there is no such field.

// What an export never carries — STATED to the actor BEFORE any download so the
// no-leak guarantee is explicit at the point of action (T006.4 criterion 1). This
// mirrors the server's structural exclusion (settings_actor_view drops every
// secret / path-like / authority-owned descriptor) plus the last-hop scrub.
export const EXPORT_EXCLUSIONS = Object.freeze([
  'Secrets, credentials, and API tokens',
  'Local filesystem paths',
  'Raw or sensitive service URLs and endpoints',
  'Chat history and raw prompts',
  'Owner-managed system, policy, and deployment configuration',
  'Anything outside your portable personal and project preferences',
])

// A short sentence naming the safe contents of an export, for the UI header.
export function exportStatementText() {
  return 'This export contains only your portable personal and project preferences. It never includes ' +
    EXPORT_EXCLUSIONS.map((item) => item.toLowerCase()).join(', ') + '.'
}

// Normalize a served export envelope into a display projection. Reads only the
// declared fields; the actor is referenced solely by the opaque `source.actor_ref`.
export function summarizeExport(envelope) {
  const source = envelope?.source || {}
  const settings = Array.isArray(envelope?.settings) ? envelope.settings : []
  return {
    schemaVersion: envelope?.schema_version ?? null,
    scope: source.scope ?? null,
    actorRef: source.actor_ref ?? null,
    projectRef: source.project_ref ?? null,
    catalogId: source.catalog_id ?? null,
    count: settings.length,
    settings: settings.map((entry) => ({
      settingId: entry?.setting_id ?? null,
      scope: entry?.scope ?? null,
      value: entry?.value,
    })),
  }
}

// The leak-shape detector applied ONLY to STRING values (never to a number, a
// boolean, or JSON punctuation): a URL, a Windows/Unix path, a dotless host:port
// (`serving:8443`), a password/secret/token/api-key marker, an AWS access-key id,
// a PEM header, or an embedded `@` identity. A numeric setting value can never
// trip this — it is not a string, so it is never scanned.
const SUSPECT_STRING_VALUE = /(https?:\/\/|[a-z]:\\|\/(etc|var|home|opt|usr)\/|:\d{2,5}\b|password|secret|token|api[_-]?key|akia|-----BEGIN|@)/i

// The opaque scope references are format-checked (below), not value-scanned: their
// keyed hex is intentional and must not be walked by the string detector.
const REDACTION_EXEMPT_KEYS = new Set(['actor_ref', 'project_ref'])

// Walk the export structure and report whether any STRING value looks like a leak.
// Numbers, booleans, and null never match; the opaque-ref keys are skipped.
function hasSuspectStringValue(node) {
  if (typeof node === 'string') return SUSPECT_STRING_VALUE.test(node)
  if (Array.isArray(node)) return node.some(hasSuspectStringValue)
  if (node && typeof node === 'object') {
    return Object.entries(node).some(([key, value]) =>
      REDACTION_EXEMPT_KEYS.has(key) ? false : hasSuspectStringValue(value))
  }
  return false
}

// A defensive guard used in tests + before render: an export must expose NO secret
// / path / host:port / raw-actor / raw-project identity. It scans VALUES, not the
// serialized JSON blob — so a portable INTEGER setting value (e.g.
// `personal.chat_transcript_retention_days: 20`) can never be mistaken for a
// `host:port` leak and wrongly withhold a legitimate export. The `actor_ref` and
// `project_ref` are the opaque keyed tokens (`actorref:` / `projectref:`),
// format-checked here rather than exempted, so a regression to a raw identity is
// caught. Returns true when clean.
export function exportIsRedacted(envelope) {
  const source = (envelope && typeof envelope === 'object' && envelope.source) || {}
  const actorRef = source.actor_ref
  if (actorRef != null && !/^actorref:[0-9a-f]{8,}$/.test(String(actorRef))) return false
  const projectRef = source.project_ref
  if (projectRef != null && !/^projectref:[0-9a-f]{8,}$/.test(String(projectRef))) return false
  // Scan only string values; a numeric/boolean value and JSON punctuation never
  // trip the guard, and the opaque refs are format-checked above.
  return !hasSuspectStringValue(envelope)
}

// Parse a pasted import envelope. Never trusts a filesystem path: the actor pastes
// or uploads JSON text; this parses it and returns a typed result the UI can show
// as a distinct invalid state (T006.4 — invalid imports cannot be applied).
export function parseImportEnvelope(text) {
  const raw = String(text ?? '').trim()
  if (!raw) return { ok: false, message: 'Paste an exported configuration to import.' }
  let envelope
  try {
    envelope = JSON.parse(raw)
  } catch {
    return { ok: false, message: 'That is not valid JSON. Paste a configuration exported from Workbench.' }
  }
  if (!envelope || typeof envelope !== 'object' || Array.isArray(envelope)) {
    return { ok: false, message: 'A configuration import must be a JSON object.' }
  }
  return { ok: true, envelope }
}

function asArray(value) {
  return Array.isArray(value) ? value : []
}

// Normalize a served import preview into the distinct typed categories the UI
// renders. Keeps creates / changes / resets / skipped-read-only /
// unavailable-references / repairable DISTINCT (never a collapsed diff), and
// derives `canApply` so an invalid or empty preview can never be applied.
export function describeImportPreview(preview) {
  const p = preview || {}
  const creates = asArray(p.creates)
  const changes = asArray(p.changes)
  const resets = asArray(p.resets)
  const skippedReadOnly = asArray(p.skipped_read_only)
  const unavailableRefs = asArray(p.unavailable_references)
  const repairable = asArray(p.repairable)
  const noOps = asArray(p.no_ops)
  const applyCount = creates.length + changes.length + resets.length
  return {
    valid: p.valid === true,
    creates,
    changes,
    resets,
    skippedReadOnly,
    unavailableRefs,
    repairable,
    noOps,
    applyCount,
    // An import can be applied ONLY when it is valid AND has at least one
    // applicable change. A read-only/unavailable-only or empty preview is not
    // applyable (nothing to do), and an invalid preview is never applyable.
    canApply: p.valid === true && applyCount > 0,
    baseVersions: p.base_versions || {},
  }
}

// Normalize a served scoped-reset preview: the exact values + scope that will
// change, and whether there is anything to reset.
export function describeResetPreview(preview) {
  const changes = asArray(preview?.changes)
  return {
    scope: preview?.scope ?? null,
    changes: changes.map((change) => ({
      settingId: change?.setting_id ?? null,
      scope: change?.scope ?? null,
      from: change?.from,
      toDefault: change?.to_default ?? null,
      expectedVersion: change?.expected_version ?? 0,
    })),
    canApply: changes.length > 0,
    baseVersions: preview?.base_versions || {},
  }
}

// The human "next remediation" line a completed/failed action reports (T006.4:
// valid imports and resets report scope, result, and next remediation). Derived
// from the typed result status — never fabricated.
export function remediationFor(result) {
  const status = result?.status
  const scope = result?.scope || 'selected'
  if (status === 'applied') {
    const n = Number.isInteger(result.appliedCount) ? result.appliedCount : (result.applied?.length ?? 0)
    // Report the affected scope(s) (T006.4 #3: an import reports scope, result, and
    // remediation, as a reset does). The scopes are carried from the apply result.
    const scopes = Array.isArray(result.scopes) ? result.scopes.filter(Boolean) : []
    const scopeLabel = scopes.length ? scopes.join(' and ') : scope
    return `${n} preference${n === 1 ? '' : 's'} updated in your ${scopeLabel} scope${scopes.length > 1 ? 's' : ''}. Reopen Settings to see the resolved values.`
  }
  if (status === 'reset') {
    const n = Number.isInteger(result.appliedCount) ? result.appliedCount : (result.applied?.length ?? 0)
    return `Your ${scope} preferences were reset to their inherited defaults (${n} cleared). Reopen Settings to confirm.`
  }
  if (status === 'stale') {
    return 'The stored configuration changed since you previewed. Reload, preview again, then apply.'
  }
  if (status === 'invalid') {
    return result.message || 'Repair every flagged field and preview again. Nothing was applied.'
  }
  if (status === 'unavailable') {
    return result.message || 'The configuration service is not available. Nothing was applied.'
  }
  return result?.message || 'Nothing was applied.'
}
