// Plugin-catalog display + permission-summary helpers (reviewed-tools-plugins T006).
//
// Pure projections used by the reviewed capability catalog / permission-review /
// tool-dispatch surface. They live here (not in api.js) so they stay real logic
// when a component test mocks the network client `./api`. None of them touches
// fetch, a token, or an endpoint.
//
// Every shape mirrors what the backend ACTUALLY serves, never the full contract:
//   * a plugin is the redacted `PluginDiscovery.published()` /
//     `_published_plugin` projection (workbench/plugin_host.py) — a strict subset
//     of the plugin-catalog schema, carrying identifiers, version, digest,
//     publisher, support status, credential-by-reference, and its enabled tools.
//   * a tool is the per-tool projection: tool_id, title, summary, effect, gates,
//     data_access, and typed input/output schemas.
//   * a receipt is the redacted `plugin-receipt.v1` projection served by
//     `/api/plugins/receipts/{request_digest}`.
//
// The primary safety rule these enforce (T003 / R004): credential handling is
// reported BY REFERENCE ONLY (requirement / owner_host / opaque credential_refs).
// There is no value/token/secret/key/password field anywhere in the served
// shape, and every `describe*` here re-projects a CLOSED field set, so an
// injected secret-bearing field can never reach a rendered card.

// --- closed served field sets -------------------------------------------------
//
// The exact field set each projection emits. Re-projecting against these (never a
// superset) keeps the UI honest to production and drops any undeclared field a
// hostile/malformed payload might carry (a credential value, a raw command).

export const PLUGIN_FIELDS = Object.freeze([
  'plugin_id', 'title', 'version', 'plugin_digest', 'publisher',
  'description', 'support_status', 'credential', 'tools',
])

export const TOOL_FIELDS = Object.freeze([
  'tool_id', 'title', 'summary', 'effect', 'gates',
  'data_access', 'input_schema', 'output_schema',
])

// Credential handling is reference-only. These are the ONLY keys the served
// `credential` / `credential_use` objects carry; there is deliberately no value,
// token, secret, key, or password key — a secret is unrepresentable.
export const CREDENTIAL_FIELDS = Object.freeze(['requirement', 'owner_host', 'credential_refs'])

export const GATE_FIELDS = Object.freeze(['preview', 'confirmation', 'human_approval', 'approval_action'])

// --- the five distinguishable capability categories ---------------------------
//
// skills / plugins / tools / routes / delivery-operations are NOT one flat list:
// the plugin-catalog contract itself documents that a plugin tool's `tool_kind`
// is non-equivalent to an Anvil provider operation (a delivery operation, which
// carries execution/operation_digest) or a bridge skill (a digest-pinned local
// body), and a Serving route is a read-only model-routing decision. Each
// category has a distinct label and a distinct permission model so an actor can
// always tell them apart from the label + permission summary alone (criterion 1).

export const CATEGORY_KINDS = Object.freeze(['skill', 'plugin', 'tool', 'route', 'delivery_operation'])

const CATEGORY_META = Object.freeze({
  skill: {
    label: 'Bridge skills',
    permissionModel: 'Digest-pinned local body discovered from an explicit bridge root. Runs on the project bridge; it has no network effect and no credential of its own.',
    source: 'bridge',
  },
  plugin: {
    label: 'Reviewed plugins',
    permissionModel: 'Version- and digest-pinned, operator-signed. Bundles tools under one publisher and one credential-owning host; a credential is held by reference only.',
    source: 'catalog',
  },
  tool: {
    label: 'Plugin tools',
    permissionModel: 'Each declares a mandatory effect class and gate set with typed input/output. An undeclared data or host scope is unrepresentable.',
    source: 'catalog',
  },
  route: {
    label: 'Serving routes',
    permissionModel: 'Read-only Anvil Serving routing decision. Selects a model tier; it performs no bridge, GitHub, or state effect and holds no credential in the browser.',
    source: 'serving',
  },
  delivery_operation: {
    label: 'Delivery operations',
    permissionModel: 'Anvil provider operation carrying execution / operation_digest. Gated and hash-bound to a one-time approval at the bridge; never invoked from the browser.',
    source: 'state',
  },
})

export function categoryLabel(kind) {
  return CATEGORY_META[kind]?.label ?? kind
}

export function categoryPermissionModel(kind) {
  return CATEGORY_META[kind]?.permissionModel ?? ''
}

// --- small helpers ------------------------------------------------------------

function trimmedOr(value, fallback) {
  return typeof value === 'string' && value.trim() ? value.trim() : fallback
}

function pick(source, fields) {
  const out = {}
  const raw = source && typeof source === 'object' ? source : {}
  for (const field of fields) if (field in raw) out[field] = raw[field]
  return out
}

// --- effect / gate / data-scope labels ----------------------------------------

const EFFECT_LABELS = Object.freeze({
  read: 'Read-only',
  external_effect: 'External effect',
  state_mutation: 'State mutation',
  plugin_lifecycle: 'Plugin lifecycle',
})

export function effectLabel(effect) {
  return EFFECT_LABELS[effect] || (typeof effect === 'string' && effect ? effect : 'unknown effect')
}

const DATA_SCOPE_LABELS = Object.freeze({
  none: 'No project data',
  read_project_metadata: 'Read project metadata',
  read_task_context: 'Read task context',
  read_conversation_context: 'Read conversation context',
  write_conversation_annotation: 'Write conversation annotation',
})

export function dataScopeLabel(scope) {
  return DATA_SCOPE_LABELS[scope] || (typeof scope === 'string' && scope ? scope : 'unknown scope')
}

// Normalize the closed gate object. Every gate value is a declared enum token
// (never free text); an absent gate reads as its safe not-required default.
export function describeGates(gates) {
  const raw = pick(gates, GATE_FIELDS)
  return {
    preview: raw.preview ?? 'not_supported',
    confirmation: raw.confirmation ?? 'not_required',
    humanApproval: raw.human_approval ?? 'not_required',
    approvalAction: raw.approval_action ?? null,
  }
}

// --- credential-by-reference --------------------------------------------------

// Project the served credential object to its CLOSED reference-only field set. A
// hostile/malformed payload carrying a `value`/`token`/`secret` key has it
// dropped here — the rendered card can only ever show requirement + owning host
// + opaque reference ids.
export function describeCredential(credential) {
  const raw = pick(credential, CREDENTIAL_FIELDS)
  const requirement = raw.requirement === 'host_owned' ? 'host_owned' : 'none'
  if (requirement !== 'host_owned') return { requirement: 'none', ownerHost: null, refs: [] }
  return {
    requirement: 'host_owned',
    ownerHost: raw.owner_host ?? null,
    refs: Array.isArray(raw.credential_refs) ? raw.credential_refs.filter((ref) => typeof ref === 'string') : [],
  }
}

// A defensive guard used in tests + before render: a projected credential must
// expose NO secret-bearing field and no path/host:port/token-shaped ref. Returns
// true when the object is reference-only. Because `describeCredential` already
// drops undeclared keys, this asserts the RAW served object never carried one and
// that every ref stays id-shaped (never a URL, path, or `label:port`).
const SECRET_KEYS = /^(value|secret|token|key|password|credential|api[_-]?key|authorization|bearer)$/i
const REF_SHAPE = /^[a-z][a-z0-9._-]{0,62}$/i

export function credentialIsReferenceOnly(credential) {
  const raw = credential && typeof credential === 'object' ? credential : {}
  for (const nameKey of Object.keys(raw)) {
    if (SECRET_KEYS.test(nameKey)) return false
  }
  const refs = Array.isArray(raw.credential_refs) ? raw.credential_refs : []
  return refs.every((ref) => typeof ref === 'string' && REF_SHAPE.test(ref))
}

// A short human line for a plugin's credential handling, by reference only.
export function credentialSummary(described) {
  if (!described || described.requirement !== 'host_owned') return 'No credential required'
  const refs = described.refs.length ? described.refs.join(', ') : 'no references declared'
  return `Host-owned by ${described.ownerHost || 'an unnamed host'} · references: ${refs}`
}

// --- approval state -----------------------------------------------------------

// The catalog only ever serves reviewed + capability-enabled plugins
// (`PluginDiscovery.published()`), so a catalog entry's own approval state is
// "approved" — it is honest, not fabricated: a not-enabled or unknown plugin is
// never in this projection.
export function pluginApprovalState() {
  return { state: 'approved', label: 'Reviewed & enabled', tone: 'green' }
}

// A tool's gate-level approval requirement: whether INVOKING it needs a human
// approval, derived from its declared `human_approval` gate. This distinguishes a
// tool that dispatches directly from one whose effect is approval-gated.
export function toolApprovalState(gatesDescribed) {
  const gates = gatesDescribed || describeGates(null)
  if (gates.humanApproval === 'required') {
    return { state: 'approval_required', label: 'Human approval required', tone: 'amber' }
  }
  return { state: 'no_approval', label: 'No human approval', tone: 'green' }
}

// A receipt's outcome / approval state: the served `status` maps to a distinct
// add/upgrade/tool-call disposition (approved / already-applied / denied /
// pending). This is the "approval state distinguishes approved/pending/etc" the
// add-upgrade review shows for a completed request.
const RECEIPT_STATE = Object.freeze({
  accepted: { state: 'accepted', label: 'Approved & applied', tone: 'green' },
  duplicate: { state: 'duplicate', label: 'Already applied (idempotent replay)', tone: 'green' },
  denied: { state: 'denied', label: 'Denied', tone: 'amber' },
  reconcile: { state: 'reconcile', label: 'Pending reconciliation', tone: 'amber' },
})

export function receiptApprovalState(status) {
  return RECEIPT_STATE[status] || { state: 'unknown', label: 'Unknown outcome', tone: 'amber' }
}

// --- describe a tool ----------------------------------------------------------

// Return the field names declared by a typed JSON schema, for a read-only
// disclosure of a tool's typed I/O. Never renders a value or an editable field —
// just the declared property names (or a truthful "no declared properties").
export function schemaFieldNames(schema) {
  const props = schema && typeof schema === 'object' ? schema.properties : null
  if (!props || typeof props !== 'object') return []
  return Object.keys(props)
}

export function describeTool(tool, pluginContext) {
  const raw = pick(tool, TOOL_FIELDS)
  const gates = describeGates(raw.gates)
  const dataAccess = Array.isArray(raw.data_access) ? raw.data_access.filter((scope) => typeof scope === 'string') : []
  const toolId = typeof raw.tool_id === 'string' ? raw.tool_id : null
  const pluginId = pluginContext?.pluginId ?? null
  return {
    // A stable, plugin-scoped key so two plugins' same tool_id never collide,
    // mirroring the delivery explorer's scoped-id discipline.
    key: pluginId && toolId ? `${pluginId}:${toolId}` : toolId,
    toolId,
    pluginId,
    pluginTitle: pluginContext?.pluginTitle ?? null,
    title: trimmedOr(raw.title, toolId || 'Untitled tool'),
    summary: trimmedOr(raw.summary, ''),
    effect: raw.effect ?? 'unknown',
    effectLabel: effectLabel(raw.effect),
    gates,
    approval: toolApprovalState(gates),
    dataAccess,
    dataAccessLabels: dataAccess.map(dataScopeLabel),
    inputFields: schemaFieldNames(raw.input_schema),
    outputFields: schemaFieldNames(raw.output_schema),
  }
}

// A compact permission summary for one described tool: effect + gate posture +
// data policy, in text (never colour alone), so a tool is distinguishable from a
// skill/route/operation by its permission summary (criterion 1) and an
// add/upgrade shows its effect + data policy + approval state (criterion 2).
export function toolPermissionSummary(describedTool) {
  const t = describedTool || {}
  const parts = [t.effectLabel || effectLabel(t.effect)]
  parts.push(t.approval?.label || 'No human approval')
  if (t.gates?.confirmation === 'required') parts.push('Confirmation required')
  if (t.gates?.preview === 'required') parts.push('Preview required')
  else if (t.gates?.preview === 'optional') parts.push('Preview optional')
  const data = (t.dataAccessLabels && t.dataAccessLabels.length) ? t.dataAccessLabels.join(', ') : 'No project data'
  parts.push(`Data: ${data}`)
  return parts.join(' · ')
}

// --- describe a plugin --------------------------------------------------------

export function describePublisher(publisher) {
  const raw = publisher && typeof publisher === 'object' ? publisher : {}
  const kind = raw.kind === 'first_party' || raw.kind === 'reviewed_third_party' ? raw.kind : null
  return {
    name: trimmedOr(raw.name, 'Unknown publisher'),
    kind,
    kindLabel: kind === 'first_party' ? 'First-party' : kind === 'reviewed_third_party' ? 'Reviewed third-party' : 'Unspecified',
  }
}

export function describePlugin(plugin) {
  const raw = pick(plugin, PLUGIN_FIELDS)
  const pluginId = typeof raw.plugin_id === 'string' ? raw.plugin_id : null
  const title = trimmedOr(raw.title, pluginId || 'Untitled plugin')
  const tools = (Array.isArray(raw.tools) ? raw.tools : [])
    .map((tool) => describeTool(tool, { pluginId, pluginTitle: title }))
  return {
    pluginId,
    title,
    version: typeof raw.version === 'string' ? raw.version : null,
    // The digest is shown VERBATIM (never truncated in the model); a card may
    // choose to wrap it, but the value carried here is the exact served string.
    digest: typeof raw.plugin_digest === 'string' ? raw.plugin_digest : null,
    publisher: describePublisher(raw.publisher),
    description: trimmedOr(raw.description, ''),
    supportStatus: raw.support_status ?? 'unknown',
    credential: describeCredential(raw.credential),
    approval: pluginApprovalState(),
    tools,
  }
}

// --- describe a receipt (tool-dispatch / lifecycle outcome) --------------------

// Project the served plugin-receipt into a display model. Exactly one of
// result / error / reconciliation is present per the receipt contract's status,
// so the caller renders exactly one card (result OR error OR reconcile).
export function describeReceipt(receipt) {
  const raw = receipt && typeof receipt === 'object' ? receipt : {}
  const status = raw.status ?? 'unknown'
  const plugin = raw.plugin && typeof raw.plugin === 'object' ? raw.plugin : {}
  const result = raw.result && typeof raw.result === 'object' ? raw.result : null
  const error = raw.error && typeof raw.error === 'object' ? raw.error : null
  const reconciliation = raw.reconciliation && typeof raw.reconciliation === 'object' ? raw.reconciliation : null
  return {
    receiptId: typeof raw.receipt_id === 'string' ? raw.receipt_id : null,
    requestDigest: typeof raw.request_digest === 'string' ? raw.request_digest : null,
    kind: raw.kind ?? 'unknown',
    toolId: typeof raw.tool_id === 'string' ? raw.tool_id : null,
    pluginId: typeof plugin.plugin_id === 'string' ? plugin.plugin_id : null,
    pluginDigest: typeof plugin.plugin_digest === 'string' ? plugin.plugin_digest : null,
    status,
    approval: receiptApprovalState(status),
    effect: raw.effect ?? 'unknown',
    effectLabel: effectLabel(raw.effect),
    credentialUse: describeCredential(raw.credential_use),
    redactionStatus: raw.redaction && typeof raw.redaction === 'object' ? raw.redaction.status ?? null : null,
    completedAt: typeof raw.completed_at === 'string' ? raw.completed_at : null,
    // A result is present on accepted/duplicate; carries an opaque output digest
    // plus an optional bounded safe summary — never the raw tool payload.
    result: result ? {
      outputDigest: typeof result.output_digest === 'string' ? result.output_digest : null,
      outputSummary: trimmedOr(result.output_summary, ''),
      producedReceipts: Array.isArray(result.produced_receipts) ? result.produced_receipts.filter((r) => typeof r === 'string') : [],
    } : null,
    // An error is present on denied; a stable code + a bounded safe summary.
    error: error ? {
      code: typeof error.code === 'string' ? error.code : 'unknown',
      safeSummary: trimmedOr(error.safe_summary, ''),
      retryable: error.retryable === true,
    } : null,
    // A reconciliation is present on reconcile; a stable code + a bounded summary.
    reconciliation: reconciliation ? {
      code: typeof reconciliation.code === 'string' ? reconciliation.code : 'unknown',
      safeSummary: trimmedOr(reconciliation.safe_summary, ''),
    } : null,
  }
}

// Which card a described receipt renders: 'result' | 'error' | 'reconcile'. Used
// so the view picks exactly one accessible card and never a mismatched pairing.
export function receiptCardKind(describedReceipt) {
  if (!describedReceipt) return null
  if (describedReceipt.error) return 'error'
  if (describedReceipt.reconciliation) return 'reconcile'
  if (describedReceipt.result) return 'result'
  return null
}

// --- the whole category model -------------------------------------------------

// Build the ordered five-category view model. Plugins and tools are the LIVE
// catalog projection (from `/api/plugins`); skills come from the bootstrap
// projection the hub already serves (names + descriptions + digests, never a
// path/body); routes reflect the hub's real router-configured flag; delivery
// operations are truthfully not enumerated in the reviewed plugin catalog. Every
// category is always present with its label + permission model so the surface is
// never a flat, undifferentiated list — a category with nothing to show renders a
// truthful availability note rather than a fabricated entry.
export function buildCategoryModel({ plugins, skills, routerConfigured } = {}) {
  const describedPlugins = (Array.isArray(plugins) ? plugins : []).map(describePlugin)
  const describedTools = describedPlugins.flatMap((plugin) => plugin.tools)
  const describedSkills = (Array.isArray(skills) ? skills : []).map(describeSkill)

  const category = (kind, extra) => ({
    kind,
    label: categoryLabel(kind),
    permissionModel: categoryPermissionModel(kind),
    ...extra,
  })

  return [
    category('skill', {
      available: describedSkills.length > 0,
      items: describedSkills,
      note: describedSkills.length
        ? null
        : 'No bridge skill is published. Start the local bridge with an explicit --skills-root to publish reviewed skills.',
    }),
    category('plugin', {
      available: describedPlugins.length > 0,
      items: describedPlugins,
      note: describedPlugins.length ? null : 'No plugin is enabled by the capability profile.',
    }),
    category('tool', {
      available: describedTools.length > 0,
      items: describedTools,
      note: describedTools.length ? null : 'No plugin tool is enabled by the capability profile.',
    }),
    category('route', {
      available: routerConfigured === true,
      items: [],
      note: routerConfigured === true
        ? 'Anvil Serving is configured. Read-only routing decisions are correlated under Routes; they are not part of the reviewed plugin catalog.'
        : 'Anvil Serving is not configured for this hub, so no routing decision is available.',
    }),
    category('delivery_operation', {
      available: false,
      items: [],
      note: 'Delivery operations are not enumerated in the reviewed plugin catalog. They are Anvil provider operations, surfaced through the delivery projection and released only by a hash-bound approval.',
    }),
  ]
}

// Normalize one bridge-published skill (bootstrap projection: skill_id,
// description, content_sha256, bridge_id). Only names/description/digest are
// served; a path or body stays local to the bridge and is never present.
export function describeSkill(skill) {
  const raw = skill && typeof skill === 'object' ? skill : {}
  const digest = typeof raw.content_sha256 === 'string' ? raw.content_sha256 : null
  return {
    skillId: typeof raw.skill_id === 'string' ? raw.skill_id : null,
    bridgeId: typeof raw.bridge_id === 'string' ? raw.bridge_id : null,
    description: trimmedOr(raw.description, ''),
    digest,
    permissionSummary: digest
      ? `Local body · digest ${digest.slice(0, 12)}…`
      : 'Local body · digest pending',
  }
}
