// Settings display + resolution helpers (preferences-configuration T005).
//
// Pure projections used by the searchable Settings surface. They live here (not
// in api.js) so they stay real logic when a component test mocks the network
// client `./api`. None of them touches fetch, a token, or an endpoint.
//
// Every shape mirrors the redacted settings actor-view contract served by
// workbench/contracts.py `settings_actor_view` (the ONLY descriptor projection a
// preference API may serialize) and the resolved `EffectiveValue.as_dict()` the
// `/api/preferences` route returns. The actor view carries ONLY personal- and
// project-owned, non-secret descriptors — an authority (deployment/policy),
// secret, or path-like descriptor is dropped server-side — so this module never
// renders, accepts, echoes, or caches a raw credential, provider key, or path:
// there is no such field in the shape.

// The exact descriptor field set `_SETTINGS_ACTOR_VIEW_FIELDS` emits. Rendering
// against this closed set (never a superset) keeps the UI honest to production.
export const ACTOR_VIEW_FIELDS = Object.freeze([
  'id', 'title', 'description', 'type', 'scope', 'sensitivity', 'mutability',
  'application_timing', 'ref_kind', 'allowed_values', 'bounds', 'default',
  'depends_on', 'migration', 'policy_ceiling',
])

// The two scopes a descriptor in the actor view can own. Authority scopes
// (deployment/policy) are never present; assert that if one ever appears.
export const ACTOR_SCOPES = Object.freeze(['personal', 'project'])

// Human sections. `personal.*` settings are the COMMON actor preferences shown
// ahead of the ADVANCED `project.*` controls (T005.2). The section is derived —
// the served descriptor carries no `section` field — with a scope fallback so a
// catalog change never leaves a setting unsectioned.
const SECTION_BY_ID = {
  'personal.landing_surface': 'Appearance & accessibility',
  'personal.appearance_density': 'Appearance & accessibility',
  'personal.time_format': 'Appearance & accessibility',
  'personal.voice_autoplay': 'Voice',
  'personal.default_chat_route': 'Chat',
  'personal.chat_transcript_retention_days': 'Chat & privacy',
  'project.preferred_worktree': 'Delivery',
  'project.workflow_template': 'Delivery',
  'project.delivery_route': 'Delivery',
  'project.default_capability_profile': 'Advanced delivery',
  'project.reviewed_skill_default': 'Advanced delivery',
  'project.enabled_plugin_default': 'Advanced delivery',
}

// Safe keyword synonyms per section so a search for a category term surfaces the
// relevant settings even when the word is not in the label/description. Every
// keyword is a plain topic word — never a path, command, endpoint, or id secret.
const SECTION_KEYWORDS = {
  'Chat': ['chat', 'conversation', 'message', 'route'],
  'Chat & privacy': ['chat', 'privacy', 'retention', 'transcript', 'data', 'delete'],
  'Voice': ['voice', 'audio', 'speech', 'accessibility', 'sound', 'autoplay'],
  'Appearance & accessibility': ['appearance', 'accessibility', 'layout', 'theme', 'density', 'time', 'clock', 'landing', 'navigation'],
  'Delivery': ['delivery', 'worktree', 'workflow', 'route', 'bridge'],
  'Advanced delivery': ['advanced', 'capability', 'skill', 'plugin', 'profile', 'system'],
}

// The rendered order of common (actor) sections, then advanced (project) ones.
const SECTION_ORDER = [
  'Chat', 'Chat & privacy', 'Voice', 'Appearance & accessibility', 'Personal',
  'Delivery', 'Advanced delivery', 'Project',
]

function sectionForSetting(descriptor) {
  const id = descriptor?.id
  if (id && SECTION_BY_ID[id]) return SECTION_BY_ID[id]
  return descriptor?.scope === 'project' ? 'Project' : 'Personal'
}

// A stable tier for grouping: personal-owned settings are COMMON, project-owned
// are ADVANCED. Derived from the OWNING SCOPE so it holds if the catalog grows.
export function settingTier(descriptor) {
  return descriptor?.scope === 'project' ? 'advanced' : 'common'
}

function trimmedOr(value, fallback) {
  return typeof value === 'string' && value.trim() ? value.trim() : fallback
}

function tokenize(text) {
  return String(text || '')
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter(Boolean)
}

// Normalize one served actor-view descriptor into a display projection. Only the
// declared actor-view fields are read; `section`, `keywords`, and control kind
// are DERIVED. A titleless descriptor falls back to its id — never blank.
export function describeSetting(descriptor) {
  const raw = descriptor || {}
  const id = typeof raw.id === 'string' ? raw.id : null
  const scope = raw.scope
  const section = sectionForSetting(raw)
  const bounds = raw.bounds && typeof raw.bounds === 'object'
    ? { min: Number.isFinite(raw.bounds.min) ? raw.bounds.min : null, max: Number.isFinite(raw.bounds.max) ? raw.bounds.max : null }
    : null
  const ceiling = raw.policy_ceiling && typeof raw.policy_ceiling === 'object'
    ? { ceilingSetting: raw.policy_ceiling.ceiling_setting ?? null, note: trimmedOr(raw.policy_ceiling.note, null) }
    : null
  const keywords = new Set([
    ...(SECTION_KEYWORDS[section] || []),
    ...tokenize(id),
    ...tokenize(raw.title),
    ...tokenize(raw.ref_kind),
  ])
  return {
    id,
    title: trimmedOr(raw.title, id || 'Untitled setting'),
    description: trimmedOr(raw.description, ''),
    type: raw.type ?? 'string',
    scope,
    section,
    tier: settingTier(raw),
    sensitivity: raw.sensitivity ?? 'public',
    mutability: raw.mutability ?? 'mutable',
    applicationTiming: raw.application_timing ?? 'immediate',
    refKind: raw.ref_kind ?? null,
    allowedValues: Array.isArray(raw.allowed_values) ? raw.allowed_values.slice() : null,
    bounds,
    default: 'default' in raw ? raw.default : undefined,
    dependsOn: Array.isArray(raw.depends_on) ? raw.depends_on.slice() : [],
    migration: raw.migration ?? null,
    policyCeiling: ceiling,
    keywords: Array.from(keywords),
    control: controlKind(raw),
  }
}

// Map a descriptor type to the native control the view should render. Reference
// kinds (id_ref/digest_ref) render as a constrained id text field — never a free
// path or command box. `bool` is a checkbox; `enum` a select; `int` a number
// field bounded by its descriptor.
export function controlKind(descriptor) {
  switch (descriptor?.type) {
    case 'bool': return 'checkbox'
    case 'enum': return 'select'
    case 'int': return 'number'
    case 'id_ref':
    case 'digest_ref': return 'reference'
    default: return 'text'
  }
}

// The change affordance a control gets, distinct so an owner-only, read-only, or
// approval-gated setting can NEVER masquerade as an ordinary save (T005.4):
//   'save'      — an ordinary optimistic PUT the actor owns
//   'approval'  — an approval-gated change routed through preview/approval/apply
//   'read_only' — env/owner-managed; no writable control at all
export function changeAffordance(descriptor) {
  const mutability = descriptor?.mutability
  if (mutability === 'approval_gated') return 'approval'
  // The backend actor-view enum is `mutable | env_only | approval_gated`, where
  // `env_only` IS the read-only (owner/env-managed) case. `read_only` is accepted
  // defensively — it is not currently emitted, but a future/foreign descriptor
  // carrying it must fail closed to the read-only affordance, never `save`.
  if (mutability === 'env_only' || mutability === 'read_only') return 'read_only'
  return 'save'
}

// A short, human ownership label naming which scope owns the setting.
export function ownerLabel(descriptor) {
  return descriptor?.scope === 'project' ? 'Project-owned' : 'Personal'
}

// --- effective values ---------------------------------------------------------

// Index the served `effective` list (EffectiveValue.as_dict entries) by setting
// id: `{setting_id, scope, value, source, repair?}`. `source` is one of
// stored | default | clamped | repaired | unset.
export function indexEffective(effective) {
  const map = new Map()
  for (const entry of Array.isArray(effective) ? effective : []) {
    if (entry && typeof entry.setting_id === 'string') {
      map.set(entry.setting_id, {
        settingId: entry.setting_id,
        scope: entry.scope ?? null,
        value: entry.value,
        source: entry.source ?? 'unset',
        repair: entry.repair ?? null,
      })
    }
  }
  return map
}

// The human reason a value is effective + which scope/ceiling owns it (T005.4).
// Derived from the resolver's own `source`, never fabricated.
export function explainEffective(descriptor, effective) {
  if (!effective) return { source: 'unset', text: 'No value is set; the reviewed default applies.' }
  const scope = effective.scope || descriptor?.scope || 'personal'
  switch (effective.source) {
    case 'stored':
      return { source: 'stored', text: `Set at the ${scope} scope.` }
    case 'default':
      return { source: 'default', text: 'Using the reviewed default (no override set).' }
    case 'clamped': {
      const ceiling = descriptor?.policyCeiling?.ceilingSetting
      return { source: 'clamped', text: ceiling ? `Capped to the ${ceiling} policy ceiling.` : 'Capped to the operator policy ceiling.' }
    }
    case 'repaired':
      return { source: 'repaired', text: effective.repair || 'A stale reference was repaired to a safe default.' }
    case 'unset':
    default:
      return { source: 'unset', text: 'No value set and no default; nothing is applied.' }
  }
}

// --- search + grouping --------------------------------------------------------

// Case-insensitive match of a described setting against a query over its label,
// description, section, and safe keywords (T005.2). An empty query matches all.
export function settingMatchesQuery(described, query) {
  const needle = String(query || '').trim().toLowerCase()
  if (!needle) return true
  const haystacks = [described.title, described.description, described.section, described.id, ...(described.keywords || [])]
  return haystacks.some((field) => String(field ?? '').toLowerCase().includes(needle))
}

// Filter a list of DESCRIBED settings by query (see settingMatchesQuery).
export function filterSettings(described, query) {
  return (Array.isArray(described) ? described : []).filter((setting) => settingMatchesQuery(setting, query))
}

// Group described settings into ordered sections with common (actor) sections
// ahead of advanced (project) ones (T005.2). Within a section, settings keep
// their catalog order. Empty sections are dropped.
export function groupSettings(described) {
  const bySection = new Map()
  for (const setting of Array.isArray(described) ? described : []) {
    if (!bySection.has(setting.section)) bySection.set(setting.section, [])
    bySection.get(setting.section).push(setting)
  }
  const ordered = []
  const seen = new Set()
  const pushSection = (section) => {
    if (seen.has(section) || !bySection.has(section)) return
    seen.add(section)
    const settings = bySection.get(section)
    ordered.push({ section, tier: settings[0].tier, settings })
  }
  SECTION_ORDER.forEach(pushSection)
  // Any section not in the fixed order (a future catalog addition) trails, common
  // before advanced, so it is never silently hidden.
  for (const tier of ['common', 'advanced']) {
    for (const section of bySection.keys()) {
      if (!seen.has(section) && bySection.get(section)[0].tier === tier) pushSection(section)
    }
  }
  return ordered
}

// Build the full described + grouped view model from a served
// `{catalog, effective}` payload. Returns `{catalogId, revision, settings,
// groups, effective}` where `effective` is the indexed Map.
export function buildSettingsModel(payload) {
  const catalog = payload?.catalog || {}
  const settings = (Array.isArray(catalog.settings) ? catalog.settings : []).map(describeSetting)
  return {
    schemaVersion: catalog.schema_version ?? null,
    catalogId: catalog.catalog_id ?? null,
    revision: catalog.revision ?? null,
    settings,
    groups: groupSettings(settings),
    effective: indexEffective(payload?.effective),
  }
}

// --- validation ---------------------------------------------------------------

// An id/digest reference must stay id-shaped: no whitespace, slash, or shell
// metacharacter that could smuggle a path or command into a reference control.
const REFERENCE_SHAPE = /^[a-z0-9][a-z0-9._:-]*$/i

// Validate a submitted value against its descriptor BEFORE any write, returning a
// coerced value and an accessible repair message on failure (T005.3). The same
// rules the server enforces, applied client-side so an invalid value can never be
// submitted and the actor gets a specific reason.
export function validateSettingValue(described, rawValue) {
  const type = described?.type
  if (type === 'bool') {
    return { valid: true, value: Boolean(rawValue) }
  }
  if (type === 'enum') {
    const allowed = described.allowedValues || []
    if (!allowed.includes(rawValue)) {
      return { valid: false, message: `Choose one of: ${allowed.join(', ') || 'no allowed values'}.` }
    }
    return { valid: true, value: rawValue }
  }
  if (type === 'int') {
    const text = String(rawValue).trim()
    if (!/^-?\d+$/.test(text)) {
      return { valid: false, message: 'Enter a whole number.' }
    }
    const num = Number(text)
    const min = described.bounds?.min
    const max = described.bounds?.max
    if (Number.isFinite(min) && num < min) {
      return { valid: false, message: `Enter a whole number of at least ${min}.` }
    }
    if (Number.isFinite(max) && num > max) {
      return { valid: false, message: `Enter a whole number of at most ${max}.` }
    }
    return { valid: true, value: num }
  }
  if (type === 'id_ref' || type === 'digest_ref') {
    const text = String(rawValue ?? '').trim()
    if (!text) return { valid: false, message: 'Enter a reference id.' }
    if (!REFERENCE_SHAPE.test(text)) {
      return { valid: false, message: 'Enter a valid reference id (letters, digits, dot, dash, colon) — not a path or command.' }
    }
    return { valid: true, value: text }
  }
  // Plain string: non-empty, no leading/trailing whitespace surprises.
  const text = String(rawValue ?? '').trim()
  if (!text) return { valid: false, message: 'Enter a value.' }
  return { valid: true, value: text }
}

// --- stale-draft handling -----------------------------------------------------

// Given the local draft and a 409 stale result, keep the draft and describe the
// reload/compare prompt (T005.3). Never discards what the actor typed.
export function staleDraftState(draftValue, staleResult) {
  return {
    draftValue,
    currentVersion: staleResult?.currentVersion ?? null,
    reloadRequired: staleResult?.reloadRequired === true,
    message: staleResult?.message || 'This setting changed elsewhere. Reload to compare before saving.',
  }
}

// --- approval-preview formatting ----------------------------------------------

// Format the served policy-operation preview envelope for display: operation
// type, material change, target scope, expiry, and the payload-hash FINGERPRINT
// (T005.4). The fingerprint is a sha256 digest — it carries no secret — and no
// field here is an endpoint, host, path, or credential.
export function formatApprovalPreview(response) {
  const preview = response?.preview || {}
  const operation = preview.operation || {}
  const hasValue = 'value' in operation
  return {
    operationType: operation.operation ?? null,
    settingId: operation.setting_id ?? null,
    targetScope: operation.scope ?? null,
    target: response?.target ?? null,
    hubLocal: response?.hub_local === true,
    requiresApproval: response?.requires_approval === true,
    materialChange: {
      hasValue,
      value: hasValue ? operation.value : undefined,
      summary: trimmedOr(preview.effect_summary, ''),
    },
    // The operation's own expiry if it carries one; otherwise the approval grant
    // minted out-of-band defines it (truthful — not fabricated as "never").
    expiry: operation.expires_at ?? null,
    fingerprint: preview.digest ?? null,
    idempotencyKey: response?.idempotency_key ?? null,
  }
}

// A defensive guard used in tests + before render: a formatted preview must
// expose NO secret/path/host:port. The fingerprint is a hash; assert it and the
// summary are safe (T005.4 redaction). Returns true when clean.
export function approvalPreviewIsRedacted(formatted) {
  const suspect = /(https?:\/\/|[a-z]:\\|\/[a-z]+\/|:\d{2,5}\b|password|secret|token|api[_-]?key)/i
  const fingerprint = formatted?.fingerprint || ''
  if (fingerprint && !/^sha256:[0-9a-f]{64}$/.test(fingerprint)) return false
  const summary = formatted?.materialChange?.summary || ''
  return !suspect.test(summary)
}
