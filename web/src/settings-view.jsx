import { useEffect, useMemo, useRef, useState } from 'react'
import {
  fetchPreferences, fetchPreference, writePreference, resetPreference, previewPolicyOperation,
} from './api'
import {
  buildSettingsModel, filterSettings, describeSetting, indexEffective, explainEffective,
  changeAffordance, validateSettingValue, staleDraftState, formatApprovalPreview,
} from './settings'

// --- Searchable Settings surface (preferences-configuration T005) -------------
//
// The thin component half of the Settings experience. All non-trivial logic
// (descriptor→control mapping, search/filter, scope/inheritance resolution,
// stale-draft handling, approval-preview formatting, validation) lives in the
// pure `./settings` module; this file is presentation + wiring over the REAL
// `/api/preferences` and `/api/policy-operations` shapes, with only the network
// mocked in tests. Every interactive control is native, every state transition
// announces through a live region, and no width constraint locks the surface out
// at narrow/zoomed viewports.

function domId(settingId) {
  return String(settingId || '').replace(/[^a-z0-9]/gi, '-')
}

function AffordanceBadge({ affordance }) {
  if (affordance === 'approval') return <span className="setting-badge setting-badge-approval">Approval required</span>
  if (affordance === 'read_only') return <span className="setting-badge setting-badge-readonly">Owner-managed · read only</span>
  return <span className="setting-badge setting-badge-save">Editable</span>
}

// Render the current effective value in a human-readable, type-aware way.
function renderEffectiveValue(setting, effective) {
  if (!effective || effective.value === undefined || effective.value === null) return 'not set'
  if (setting.type === 'bool') return effective.value ? 'On' : 'Off'
  return String(effective.value)
}

// The editor panel for one setting. Opened from its row; manages its own draft,
// version, validation, and the distinct save / stale / invalid / unavailable /
// approval result states. Focus moves in on open and is restored to the opener
// on close.
function SettingEditor({ setting, projectId, effective, onClose, onSaved, announce }) {
  const affordance = changeAffordance(setting)
  const idBase = domId(setting.id)
  const headingRef = useRef(null)
  const mountedRef = useRef(true)
  const [expectedVersion, setExpectedVersion] = useState(0)
  const [versionKnown, setVersionKnown] = useState(false)
  const [draft, setDraft] = useState(() => (effective ? effective.value : setting.default))
  const [repair, setRepair] = useState(null)
  const [stale, setStale] = useState(null)
  const [notice, setNotice] = useState(null)
  const [preview, setPreview] = useState(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])

  // Move focus into the editor on open and restore it to the opener on close, so
  // a keyboard user is never dropped to <body> (a11y focus management).
  useEffect(() => {
    const opener = document.activeElement
    headingRef.current?.focus()
    return () => { opener?.focus?.() }
  }, [])

  // Load the current stored record's write_version before an optimistic write so
  // the first save is not a guaranteed stale 409. A 404 (never set) means version
  // 0 and the descriptor default seeds the draft.
  useEffect(() => {
    let cancelled = false
    if (affordance === 'read_only') { setVersionKnown(true); return () => {} }
    fetchPreference(setting.id, { scope: setting.scope, projectId })
      .then((data) => {
        if (cancelled || !mountedRef.current) return
        const record = data.preference || {}
        setExpectedVersion(Number.isInteger(record.write_version) ? record.write_version : 0)
        if ('value' in record) setDraft(record.value)
        setVersionKnown(true)
      })
      .catch(() => {
        if (cancelled || !mountedRef.current) return
        // Not set in the actor namespace: a first write expects version 0 and the
        // draft keeps the effective/default seed.
        setExpectedVersion(0)
        setVersionKnown(true)
      })
    return () => { cancelled = true }
  }, [setting.id, setting.scope, projectId, affordance])

  // Escape closes the editor, but yields to an open native <select> so the
  // dropdown's own Escape is not stolen (a11y both-ways).
  const onKeyDown = (event) => {
    if (event.key !== 'Escape') return
    if (event.defaultPrevented) return
    if (event.target && event.target.tagName === 'SELECT') return
    onClose()
  }

  const runSave = async () => {
    setRepair(null); setNotice(null)
    const check = validateSettingValue(setting, draft)
    if (!check.valid) {
      setRepair(check.message)
      announce(`${setting.title}: ${check.message}`)
      return
    }
    setBusy(true)
    const result = await writePreference(setting.id, {
      scope: setting.scope, value: check.value, expectedVersion, projectId,
    })
    if (!mountedRef.current) return
    setBusy(false)
    if (result.status === 'saved') {
      setStale(null)
      setExpectedVersion(result.preference.write_version)
      setNotice({ tone: 'ok', text: `Saved. Now at version ${result.preference.write_version}.` })
      announce(`${setting.title} saved. Now at version ${result.preference.write_version}.`)
      onSaved(setting.id, result.preference)
    } else if (result.status === 'stale') {
      // Keep the draft; prompt reload/compare. Never discard what was typed.
      setStale(staleDraftState(check.value, result))
      announce(`${setting.title}: ${result.message}`)
    } else if (result.status === 'invalid') {
      setRepair(result.message)
      announce(`${setting.title}: ${result.message}`)
    } else if (result.status === 'unknown') {
      setNotice({ tone: 'error', text: result.message })
      announce(`${setting.title}: ${result.message}`)
    } else {
      setNotice({ tone: 'error', text: result.message })
      announce(`${setting.title}: ${result.message}`)
    }
  }

  // Reload the current record after a stale write so the actor can compare the
  // server value with their preserved draft, then save again.
  const runReload = async () => {
    setBusy(true)
    try {
      const data = await fetchPreference(setting.id, { scope: setting.scope, projectId })
      const record = data.preference || {}
      if (!mountedRef.current) return
      setExpectedVersion(Number.isInteger(record.write_version) ? record.write_version : 0)
      setStale((current) => current ? { ...current, serverValue: 'value' in record ? record.value : undefined, reloaded: true } : current)
      announce(`${setting.title}: reloaded server value for comparison.`)
    } catch {
      if (!mountedRef.current) return
      setExpectedVersion(0)
      setStale((current) => current ? { ...current, serverValue: undefined, reloaded: true } : current)
    } finally {
      if (mountedRef.current) setBusy(false)
    }
  }

  const runReset = async () => {
    setBusy(true)
    const result = await resetPreference(setting.id, { scope: setting.scope, expectedVersion, projectId })
    if (!mountedRef.current) return
    setBusy(false)
    if (result.status === 'reset') {
      setStale(null); setRepair(null)
      setNotice({ tone: 'ok', text: `Reset to inherited default at the ${setting.scope} scope.` })
      announce(`${setting.title} reset to its inherited default at the ${setting.scope} scope.`)
      onSaved(setting.id, null, result.effective)
    } else if (result.status === 'stale') {
      setStale(staleDraftState(draft, result))
      announce(`${setting.title}: ${result.message}`)
    } else {
      setNotice({ tone: 'error', text: result.message })
      announce(`${setting.title}: ${result.message}`)
    }
  }

  const runPreview = async () => {
    setBusy(true); setNotice(null)
    const check = validateSettingValue(setting, draft)
    if (!check.valid) {
      setBusy(false); setRepair(check.message); announce(`${setting.title}: ${check.message}`)
      return
    }
    setRepair(null)
    const result = await previewPolicyOperation({
      settingId: setting.id, scope: setting.scope, operation: 'preference.set',
      opVersion: expectedVersion + 1, value: check.value, projectId,
    })
    if (!mountedRef.current) return
    setBusy(false)
    if (result.status === 'previewed') {
      setPreview(formatApprovalPreview(result.preview))
      announce(`${setting.title}: approval preview ready. This change needs an operator approval before it applies.`)
    } else {
      setNotice({ tone: 'error', text: result.message })
      announce(`${setting.title}: ${result.message}`)
    }
  }

  const repairId = `${idBase}-repair`
  const timingId = `${idBase}-timing`

  return (
    <div className="setting-editor" role="group" aria-labelledby={`${idBase}-editor-title`} onKeyDown={onKeyDown}>
      <div className="setting-editor-head">
        <h4 id={`${idBase}-editor-title`} tabIndex={-1} ref={headingRef}>Change {setting.title}</h4>
        <button className="setting-editor-close" aria-label={`Close ${setting.title} editor`} onClick={onClose}>Done</button>
      </div>

      {affordance === 'read_only' ? (
        <p className="setting-readonly-note">
          This setting is owner-managed and configured outside the browser. It is shown for context and cannot be changed here.
        </p>
      ) : (
        <>
          <label className="setting-control-label" htmlFor={`${idBase}-control`}>
            {setting.title}
            <span className="setting-timing" id={timingId}>Applies: {setting.applicationTiming.replaceAll('_', ' ')}</span>
          </label>
          <SettingControl
            setting={setting}
            value={draft}
            controlId={`${idBase}-control`}
            describedBy={[repair ? repairId : null, timingId].filter(Boolean).join(' ') || undefined}
            invalid={Boolean(repair)}
            onChange={(next) => { setDraft(next); setRepair(null) }}
          />
          {repair && <p className="setting-repair" id={repairId} role="alert">{repair}</p>}

          {stale && (
            <div className="setting-stale" role="alert">
              <p>{stale.message}</p>
              <p className="setting-stale-compare">
                Your unsaved value: <b>{String(stale.draftValue)}</b>
                {stale.reloaded ? <> · Current server value: <b>{stale.serverValue === undefined ? 'not set' : String(stale.serverValue)}</b></> : null}
                {stale.currentVersion != null ? <> · server version {stale.currentVersion}</> : null}
              </p>
              <button onClick={runReload} disabled={busy}>Reload &amp; compare</button>
            </div>
          )}

          {preview && <ApprovalPreview preview={preview} />}

          <div className="setting-editor-actions">
            {affordance === 'approval' ? (
              <button className="setting-preview-btn" onClick={runPreview} disabled={busy || !versionKnown}>
                {busy ? 'Preparing…' : 'Preview approval-gated change'}
              </button>
            ) : (
              <button className="setting-save-btn" onClick={runSave} disabled={busy || !versionKnown}>
                {busy ? 'Saving…' : 'Save change'}
              </button>
            )}
            <button className="setting-reset-btn" onClick={runReset} disabled={busy || !versionKnown} aria-label={`Reset ${setting.title} to inherited default at the ${setting.scope} scope`}>
              Reset {setting.scope} scope
            </button>
          </div>
        </>
      )}

      {notice && <p className={`setting-notice setting-notice-${notice.tone}`} role="status">{notice.text}</p>}
    </div>
  )
}

// One native control rendered strictly from its descriptor (type/bounds/
// allowed_values). No path, command, model, or credential textbox can appear —
// the control set is closed to checkbox/select/number/reference/text.
function SettingControl({ setting, value, controlId, describedBy, invalid, onChange }) {
  if (setting.control === 'checkbox') {
    return (
      <input
        id={controlId}
        type="checkbox"
        checked={Boolean(value)}
        aria-describedby={describedBy}
        onChange={(event) => onChange(event.target.checked)}
      />
    )
  }
  if (setting.control === 'select') {
    return (
      <select id={controlId} value={value ?? ''} aria-describedby={describedBy} aria-invalid={invalid || undefined} onChange={(event) => onChange(event.target.value)}>
        {(setting.allowedValues || []).map((option) => <option key={option} value={option}>{option}</option>)}
      </select>
    )
  }
  if (setting.control === 'number') {
    return (
      <input
        id={controlId}
        type="number"
        value={value ?? ''}
        min={setting.bounds?.min ?? undefined}
        max={setting.bounds?.max ?? undefined}
        aria-describedby={describedBy}
        aria-invalid={invalid || undefined}
        onChange={(event) => onChange(event.target.value)}
      />
    )
  }
  // reference (id_ref/digest_ref) and plain string: a constrained id text field.
  return (
    <input
      id={controlId}
      type="text"
      value={value ?? ''}
      aria-describedby={describedBy}
      aria-invalid={invalid || undefined}
      placeholder={setting.refKind ? `${setting.refKind} reference id` : undefined}
      onChange={(event) => onChange(event.target.value)}
    />
  )
}

// The approval preview: operation type, material change, target scope, expiry,
// and the payload-hash fingerprint — no secret, no endpoint, no path.
function ApprovalPreview({ preview }) {
  return (
    <section className="setting-approval-preview" aria-label="Approval preview">
      <p className="setting-approval-title">This change needs an operator approval before it applies.</p>
      <dl>
        <div><dt>Operation</dt><dd>{preview.operationType}</dd></div>
        <div><dt>Target scope</dt><dd>{preview.targetScope}</dd></div>
        <div><dt>Material change</dt><dd>{preview.materialChange.summary || (preview.materialChange.hasValue ? String(preview.materialChange.value) : '—')}</dd></div>
        <div><dt>Expiry</dt><dd>{preview.expiry || 'set when the operator grants approval'}</dd></div>
        <div><dt>Payload fingerprint</dt><dd className="setting-fingerprint">{preview.fingerprint}</dd></div>
      </dl>
    </section>
  )
}

// One setting row: label, description, owner scope, effective value + why, the
// change affordance, and an expand-to-edit control.
function SettingRow({ setting, effective, expanded, projectId, onToggle, onSaved, announce }) {
  const idBase = domId(setting.id)
  const affordance = changeAffordance(setting)
  const why = explainEffective(setting, effective)
  return (
    <li className={`setting-row ${expanded ? 'expanded' : ''}`}>
      <div className="setting-row-head">
        <div className="setting-row-main">
          <button
            className="setting-row-toggle"
            aria-expanded={expanded}
            aria-controls={`${idBase}-panel`}
            id={`${idBase}-toggle`}
            onClick={() => onToggle(setting.id)}
          >
            <span className="setting-title">{setting.title}</span>
            <span className="setting-owner">{setting.scope === 'project' ? 'Project-owned' : 'Personal'}</span>
            <AffordanceBadge affordance={affordance} />
          </button>
          {setting.description && <p className="setting-desc">{setting.description}</p>}
          <p className="setting-effective">
            <b>{renderEffectiveValue(setting, effective)}</b>
            <span className="setting-why"> — {why.text}</span>
          </p>
        </div>
      </div>
      {expanded && (
        <div id={`${idBase}-panel`} role="region" aria-labelledby={`${idBase}-toggle`} className="setting-row-panel">
          <SettingEditor
            setting={setting}
            projectId={projectId}
            effective={effective}
            onClose={() => onToggle(setting.id)}
            onSaved={onSaved}
            announce={announce}
          />
        </div>
      )}
    </li>
  )
}

export default function SettingsView({ data, append }) {
  const project = data?.projects?.[0] || null
  const projectId = project?.id || null
  const [status, setStatus] = useState('loading')
  const [errorMessage, setErrorMessage] = useState(null)
  const [payload, setPayload] = useState(null)
  const [query, setQuery] = useState('')
  const [expandedId, setExpandedId] = useState(null)
  const [announce, setAnnounce] = useState('')
  const [effectiveOverrides, setEffectiveOverrides] = useState(new Map())
  const mountedRef = useRef(true)
  const loadSeq = useRef(0)

  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])

  const load = async () => {
    const seq = (loadSeq.current += 1)
    setStatus('loading'); setErrorMessage(null); setAnnounce('Loading settings…')
    try {
      const value = await fetchPreferences(projectId)
      if (seq !== loadSeq.current || !mountedRef.current) return
      setPayload(value)
      setEffectiveOverrides(new Map())
      setStatus('ready')
      const count = value?.catalog?.settings?.length || 0
      setAnnounce(count ? `Loaded ${count} settings.` : 'No settings are available.')
    } catch (error) {
      if (seq !== loadSeq.current || !mountedRef.current) return
      const unconfigured = /not configured/i.test(error.message || '')
      setStatus(unconfigured ? 'unavailable' : 'error')
      setErrorMessage(error.message)
      setAnnounce(error.message || 'Settings are unavailable.')
    }
  }

  useEffect(() => { load() /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [projectId])

  const model = useMemo(() => (payload ? buildSettingsModel(payload) : null), [payload])

  // Effective map with local post-save/reset overrides layered on top, so the row
  // reflects the just-written value without a full reload.
  const effectiveMap = useMemo(() => {
    const base = model ? new Map(model.effective) : new Map()
    for (const [id, value] of effectiveOverrides) base.set(id, value)
    return base
  }, [model, effectiveOverrides])

  const groups = useMemo(() => {
    if (!model) return []
    return model.groups
      .map((group) => ({ ...group, settings: filterSettings(group.settings, query) }))
      .filter((group) => group.settings.length)
  }, [model, query])

  // Announce the filtered result count on query change (local, synchronous filter
  // — no network, so no stale-repaint race to guard).
  useEffect(() => {
    if (!model) return
    const needle = query.trim()
    if (!needle) return
    const total = groups.reduce((sum, group) => sum + group.settings.length, 0)
    setAnnounce(total ? `${total} setting${total === 1 ? '' : 's'} match "${needle}".` : `No settings match "${needle}".`)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query])

  const onSaved = (settingId, record, effective) => {
    // Layer the new effective value locally. A save records the stored value; a
    // reset records the returned inherited effective value.
    setEffectiveOverrides((current) => {
      const next = new Map(current)
      if (effective) {
        const indexed = indexEffective([effective])
        next.set(settingId, indexed.get(settingId))
      } else if (record) {
        next.set(settingId, { settingId, scope: record.scope, value: record.value, source: 'stored', repair: null })
      }
      return next
    })
  }

  const onToggle = (settingId) => setExpandedId((current) => (current === settingId ? null : settingId))

  return (
    <main className="settings" aria-label="Settings">
      <div className="settings-inner">
        <header className="settings-header">
          <span className="crumb">Settings / preferences</span>
          <h1>Settings</h1>
          <p className="settings-intro">
            Search and change your Chat, voice, delivery, and privacy preferences. Project and system controls show which
            scope owns them and whether they are inherited, read-only, or approval-gated.
          </p>
          <label className="settings-search">
            <span className="settings-search-label">Search settings</span>
            <input
              type="search"
              aria-label="Search settings"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search by name, description, section, or keyword"
              disabled={status !== 'ready'}
            />
          </label>
        </header>

        {status === 'loading' && <p className="settings-state" role="status">Loading settings…</p>}
        {status === 'unavailable' && (
          <div className="settings-state settings-unavailable" role="status">
            <b>Settings are unavailable</b>
            <span>{errorMessage || 'The settings service is not configured for this hub.'}</span>
            <button onClick={load}>Retry</button>
          </div>
        )}
        {status === 'error' && (
          <div className="settings-state settings-error" role="alert">
            <b>Settings could not be loaded</b>
            <span>{errorMessage}</span>
            <button onClick={load}>Retry</button>
          </div>
        )}

        {status === 'ready' && (
          groups.length ? (
            <div className="settings-groups">
              {groups.map((group) => (
                <section key={group.section} className="settings-group" aria-labelledby={`group-${domId(group.section)}`}>
                  <h2 id={`group-${domId(group.section)}`} className="settings-group-title">
                    {group.section}
                    <span className={`settings-tier settings-tier-${group.tier}`}>{group.tier === 'common' ? 'Your preferences' : 'Project & system'}</span>
                  </h2>
                  <ul className="settings-list">
                    {group.settings.map((setting) => (
                      <SettingRow
                        key={setting.id}
                        setting={setting}
                        effective={effectiveMap.get(setting.id)}
                        expanded={expandedId === setting.id}
                        projectId={projectId}
                        onToggle={onToggle}
                        onSaved={onSaved}
                        announce={setAnnounce}
                      />
                    ))}
                  </ul>
                </section>
              ))}
            </div>
          ) : (
            <p className="settings-state settings-empty" role="status">
              {query.trim() ? `No settings match "${query.trim()}".` : 'No settings are available.'}
            </p>
          )
        )}
      </div>
      <div className="settings-live" role="status" aria-live="polite">{announce}</div>
    </main>
  )
}
