import { useRef, useState } from 'react'
import {
  fetchConfigurationExport, previewConfigurationImport, applyConfigurationImport,
  previewConfigurationReset, applyConfigurationReset,
} from './api'
import {
  EXPORT_EXCLUSIONS, exportStatementText, summarizeExport, exportIsRedacted,
  parseImportEnvelope, describeImportPreview, describeResetPreview, remediationFor,
} from './configuration'

// --- Backup & transfer workflows (preferences-configuration T006.4) -----------
//
// The thin component half of the export / import / scoped-reset experience. All
// non-trivial logic (exclusion statement, envelope parsing, typed-category
// projection, remediation text, redaction guard) lives in the pure
// `./configuration` module; this file is presentation + wiring over the REAL
// `/api/configuration` shapes, with only the network mocked in tests. Every
// control is native and keyboard-operable, every state transition announces
// through a live region, and no width constraint locks the surface out at
// narrow / 200%-zoom viewports (the layout is flexible; nothing sets a min-width).

function CategoryList({ title, items, render, tone }) {
  if (!items.length) return null
  return (
    <div className={`config-category config-category-${tone}`}>
      <h4>{title} <span className="config-count">{items.length}</span></h4>
      <ul>{items.map((item, index) => <li key={`${item.setting_id || item.settingId || 'item'}-${index}`}>{render(item)}</li>)}</ul>
    </div>
  )
}

// --- Export workflow ---------------------------------------------------------
function ExportPanel({ projectId, announce }) {
  const [status, setStatus] = useState('idle')
  const [envelope, setEnvelope] = useState(null)
  const [message, setMessage] = useState(null)

  const prepare = async () => {
    setStatus('loading'); setMessage(null)
    announce('Preparing your configuration export…')
    try {
      const result = await fetchConfigurationExport(projectId)
      // Defence-in-depth: never surface an export that fails the redaction guard.
      if (!exportIsRedacted(result)) {
        setStatus('error'); setMessage('The export could not be shown safely and was withheld.')
        announce('The export was withheld because it failed the redaction check.')
        return
      }
      setEnvelope(result); setStatus('ready')
      const summary = summarizeExport(result)
      announce(`Export ready: ${summary.count} portable setting${summary.count === 1 ? '' : 's'}. Review what is excluded before you download.`)
    } catch (error) {
      setStatus(/not configured/i.test(error.message || '') ? 'unavailable' : 'error')
      setMessage(error.message)
      announce(error.message || 'The export is unavailable.')
    }
  }

  const summary = envelope ? summarizeExport(envelope) : null
  const serialized = envelope ? JSON.stringify(envelope, null, 2) : ''
  // A browser-only download: a Blob object URL, never a filesystem path from this
  // client. Rendered only AFTER the exclusion statement so the actor always sees
  // what is excluded before the download affordance (criterion 1).
  const downloadHref = serialized ? `data:application/json;charset=utf-8,${encodeURIComponent(serialized)}` : undefined

  return (
    <section className="config-panel" aria-labelledby="config-export-title">
      <h3 id="config-export-title">Export configuration</h3>
      {/* The exclusion statement is ALWAYS present, before any download control. */}
      <div className="config-exclusions" role="note" aria-label="What an export excludes">
        <p>{exportStatementText()}</p>
        <p className="config-exclusions-lead">An export never contains:</p>
        <ul>{EXPORT_EXCLUSIONS.map((item) => <li key={item}>{item}</li>)}</ul>
      </div>

      {status !== 'ready' && (
        <button className="config-prepare" onClick={prepare} disabled={status === 'loading'}>
          {status === 'loading' ? 'Preparing…' : 'Prepare export'}
        </button>
      )}
      {status === 'unavailable' && <p className="config-note" role="status">{message || 'The configuration service is not configured for this hub.'}</p>}
      {status === 'error' && <p className="config-error" role="alert">{message}</p>}

      {status === 'ready' && summary && (
        <div className="config-export-ready">
          <dl className="config-export-summary">
            <div><dt>Schema</dt><dd>{summary.schemaVersion}</dd></div>
            <div><dt>Scope</dt><dd>{summary.scope}</dd></div>
            <div><dt>Actor reference</dt><dd className="config-opaque">{summary.actorRef}</dd></div>
            <div><dt>Portable settings</dt><dd>{summary.count}</dd></div>
          </dl>
          <pre className="config-export-body" aria-label="Redacted export contents">{serialized}</pre>
          <div className="config-export-actions">
            <a className="config-download" href={downloadHref} download="workbench-configuration.json">Download export</a>
            <button className="config-refresh" onClick={prepare}>Refresh</button>
          </div>
        </div>
      )}
    </section>
  )
}

// --- Import workflow ---------------------------------------------------------
function ImportPanel({ projectId, announce, onApplied }) {
  const [text, setText] = useState('')
  const [preview, setPreview] = useState(null)
  const [parseError, setParseError] = useState(null)
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)
  const envelopeRef = useRef(null)

  // Preview is a distinct, non-mutating step. Apply is a SEPARATE explicit action
  // enabled only after a valid preview — an import can never apply early.
  const runPreview = async () => {
    setResult(null); setParseError(null)
    const parsed = parseImportEnvelope(text)
    if (!parsed.ok) {
      setPreview(null); setParseError(parsed.message)
      announce(parsed.message)
      return
    }
    envelopeRef.current = parsed.envelope
    setBusy(true)
    const response = await previewConfigurationImport(parsed.envelope, { projectId })
    setBusy(false)
    if (response.status === 'previewed') {
      const described = describeImportPreview(response.preview)
      setPreview(described)
      if (!described.valid) {
        announce(`This import is invalid: ${described.repairable.length} field${described.repairable.length === 1 ? '' : 's'} must be repaired. Nothing can be applied.`)
      } else {
        announce(`Import preview ready: ${described.creates.length} to create, ${described.changes.length} to change, ${described.resets.length} to reset. Review, then apply.`)
      }
    } else {
      setPreview(null); setParseError(response.message)
      announce(response.message)
    }
  }

  const runApply = async () => {
    if (!preview || !preview.canApply || !envelopeRef.current) return
    setBusy(true)
    const response = await applyConfigurationImport(envelopeRef.current, {
      projectId, baseVersions: preview.baseVersions,
    })
    setBusy(false)
    setResult(response)
    announce(remediationFor(response))
    if (response.status === 'applied') {
      setPreview(null); setText('')
      onApplied?.()
    }
  }

  return (
    <section className="config-panel" aria-labelledby="config-import-title">
      <h3 id="config-import-title">Import configuration</h3>
      <p className="config-help">Paste a configuration exported from Workbench. Previewing never changes anything — you apply as a separate, explicit step.</p>
      <label className="config-import-label" htmlFor="config-import-input">Exported configuration</label>
      <textarea
        id="config-import-input"
        className="config-import-input"
        value={text}
        rows={6}
        aria-describedby={parseError ? 'config-import-error' : undefined}
        aria-invalid={parseError ? 'true' : undefined}
        onChange={(event) => setText(event.target.value)}
        placeholder="Paste exported JSON here"
      />
      {parseError && <p className="config-error" id="config-import-error" role="alert">{parseError}</p>}

      <div className="config-import-actions">
        <button className="config-preview-btn" onClick={runPreview} disabled={busy || !text.trim()}>
          {busy && !preview ? 'Previewing…' : 'Preview import'}
        </button>
        <button
          className="config-apply-btn"
          onClick={runApply}
          disabled={busy || !preview || !preview.canApply}
          aria-disabled={busy || !preview || !preview.canApply ? 'true' : undefined}
          aria-describedby={preview && !preview.canApply ? 'config-import-cannot-apply' : undefined}
        >
          {busy && preview ? 'Applying…' : 'Apply import'}
        </button>
      </div>

      {preview && (
        <div className="config-import-preview" role="group" aria-label="Import preview">
          {!preview.valid && (
            <p className="config-invalid" id="config-import-cannot-apply" role="alert">
              This import is invalid and cannot be applied. Repair every flagged field, then preview again. Nothing has been changed.
            </p>
          )}
          {preview.valid && !preview.canApply && (
            <p className="config-note" id="config-import-cannot-apply" role="status">
              This import has nothing to apply. Every entry is already current, skipped, or unavailable.
            </p>
          )}
          <CategoryList tone="create" title="Will create" items={preview.creates} render={(i) => <><b>{i.setting_id}</b> → <code>{String(i.value)}</code></>} />
          <CategoryList tone="change" title="Will change" items={preview.changes} render={(i) => <><b>{i.setting_id}</b> <code>{String(i.from)}</code> → <code>{String(i.to)}</code></>} />
          <CategoryList tone="reset" title="Will reset to default" items={preview.resets} render={(i) => <><b>{i.setting_id}</b> <code>{String(i.from)}</code> → default</>} />
          <CategoryList tone="skip" title="Skipped — owner-managed / read-only" items={preview.skippedReadOnly} render={(i) => <><b>{i.setting_id}</b> — {i.reason}</>} />
          <CategoryList tone="unavailable" title="Skipped — unavailable reference" items={preview.unavailableRefs} render={(i) => <><b>{i.setting_id}</b> references an unavailable {i.ref_kind}</>} />
          <CategoryList tone="repair" title="Repair before applying" items={preview.repairable} render={(i) => <><b>{i.setting_id}</b> — {i.reason}</>} />
        </div>
      )}

      {result && (
        <p className={`config-result config-result-${result.status === 'applied' ? 'ok' : 'error'}`} role="status">
          {remediationFor(result)}
        </p>
      )}
    </section>
  )
}

// --- Scoped reset workflow ---------------------------------------------------
function ResetPanel({ projectId, announce, onApplied }) {
  const [scope, setScope] = useState('personal')
  const [preview, setPreview] = useState(null)
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)

  const runPreview = async () => {
    setResult(null)
    setBusy(true)
    const response = await previewConfigurationReset({ scope, projectId })
    setBusy(false)
    if (response.status === 'previewed') {
      const described = describeResetPreview(response.preview)
      setPreview(described)
      announce(described.canApply
        ? `Reset preview: ${described.changes.length} ${scope} setting${described.changes.length === 1 ? '' : 's'} will return to their inherited defaults. No other scope is affected.`
        : `Nothing to reset at the ${scope} scope.`)
    } else {
      setPreview(null)
      announce(response.message)
    }
  }

  const runApply = async () => {
    if (!preview || !preview.canApply) return
    setBusy(true)
    const response = await applyConfigurationReset({ scope, projectId, baseVersions: preview.baseVersions })
    setBusy(false)
    setResult({ ...response, scope })
    announce(remediationFor({ ...response, scope }))
    if (response.status === 'reset') {
      setPreview(null)
      onApplied?.()
    }
  }

  return (
    <section className="config-panel" aria-labelledby="config-reset-title">
      <h3 id="config-reset-title">Reset preferences</h3>
      <p className="config-help">Reset your preferences at one scope to their inherited defaults. This never touches another actor, project policy, or deployment configuration.</p>
      <label className="config-reset-scope" htmlFor="config-reset-scope-select">Scope to reset</label>
      <select id="config-reset-scope-select" value={scope} onChange={(event) => { setScope(event.target.value); setPreview(null); setResult(null) }}>
        <option value="personal">My personal preferences</option>
        <option value="project" disabled={!projectId}>This project’s preferences</option>
      </select>

      <div className="config-reset-actions">
        <button className="config-reset-preview-btn" onClick={runPreview} disabled={busy}>
          {busy && !preview ? 'Previewing…' : 'Preview reset'}
        </button>
        <button
          className="config-reset-apply-btn"
          onClick={runApply}
          disabled={busy || !preview || !preview.canApply}
          aria-disabled={busy || !preview || !preview.canApply ? 'true' : undefined}
        >
          {busy && preview ? 'Resetting…' : 'Apply reset'}
        </button>
      </div>

      {preview && (
        <div className="config-reset-preview" role="group" aria-label="Reset preview">
          {preview.canApply ? (
            <>
              <p>Resetting the <b>{preview.scope}</b> scope. These settings return to their inherited defaults; no other scope changes.</p>
              <ul className="config-reset-list">
                {preview.changes.map((change) => (
                  <li key={change.settingId}>
                    <b>{change.settingId}</b> <code>{String(change.from)}</code> → default <code>{change.toDefault === null ? 'unset' : String(change.toDefault)}</code>
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <p className="config-note" role="status">Nothing is set at the {preview.scope} scope, so there is nothing to reset.</p>
          )}
        </div>
      )}

      {result && (
        <p className={`config-result config-result-${result.status === 'reset' ? 'ok' : 'error'}`} role="status">
          {remediationFor(result)}
        </p>
      )}
    </section>
  )
}

export default function ConfigurationView({ data, append }) {
  const project = data?.projects?.[0] || null
  const projectId = project?.id || null
  const [announce, setAnnounce] = useState('')
  const notify = (message) => { setAnnounce(message); if (message) append?.(message) }

  return (
    // A labeled region, NOT a second <main>: ConfigurationView renders alongside
    // SettingsView (which owns the page's single <main>) in the Settings tab, so a
    // <main> here would create two main landmarks on one page (acceptance N7).
    <section className="configuration" aria-label="Backup and transfer">
      <div className="configuration-inner">
        <header className="configuration-header">
          <span className="crumb">Settings / backup &amp; transfer</span>
          <h1>Backup &amp; transfer</h1>
          <p className="configuration-intro">
            Export your portable preferences, import a saved configuration after previewing exactly what will change, or reset a scope
            to its inherited defaults. Every action is scoped to you and reports its result and next step.
          </p>
        </header>
        <div className="configuration-panels">
          <ExportPanel projectId={projectId} announce={notify} />
          <ImportPanel projectId={projectId} announce={notify} onApplied={() => notify('Import applied. Reopen Settings to see the resolved values.')} />
          <ResetPanel projectId={projectId} announce={notify} onApplied={() => notify('Reset applied. Reopen Settings to confirm.')} />
        </div>
      </div>
      <div className="configuration-live" role="status" aria-live="polite">{announce}</div>
    </section>
  )
}
