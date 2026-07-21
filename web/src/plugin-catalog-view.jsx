import { useEffect, useMemo, useRef, useState } from 'react'
import { fetchPlugins, fetchPluginReceipt, PLUGIN_NOT_CONFIGURED } from './api'
import {
  buildCategoryModel, describePlugin, toolPermissionSummary, describeReceipt, receiptCardKind,
  credentialSummary,
} from './plugin-catalog'

// --- Reviewed capability catalog / permission-review / tool-dispatch (RTP T006) ---
//
// The thin component half of the plugin surface. All non-trivial logic
// (catalog grouping, permission-summary formatting, credential-by-reference
// projection, receipt classification, category distinction) lives in the pure
// `./plugin-catalog` module; this file is presentation + wiring over the REAL
// `/api/plugins` and `/api/plugins/receipts/{request_digest}` shapes, with only
// the network mocked in tests.
//
// Every interactive control is native (keyboard-reachable, visible focus), every
// state transition announces through a live region, there is NO free-text
// command/path/credential input (only an id/digest lookup), and no width
// constraint locks the surface out at narrow/zoomed viewports. The plugin
// surface is 503-until-configured; the view degrades to a truthful unavailable
// state (never a crash) while the bridge-published Skills category still renders.

function toneClass(tone) {
  return tone === 'amber' ? 'status-amber' : ''
}

function Pill({ tone = 'green', children }) {
  return <span className={`pc-pill ${toneClass(tone)}`}><span className="pc-dot" />{children}</span>
}

// One plugin as an ADD / UPGRADE review card: it shows EXACTLY the served fields
// an add/upgrade decision needs — version, digest (verbatim), publisher, support
// status, approval state, and credential handling BY REFERENCE — plus each
// enabled tool's effect / data policy / approval permission summary (criterion 2).
function PluginCard({ plugin }) {
  return (
    <article className="pc-plugin-card" aria-label={`Plugin ${plugin.title}`}>
      <div className="pc-plugin-head">
        <div>
          <b className="pc-plugin-title">{plugin.title}</b>
          <small className="pc-plugin-id">{plugin.pluginId}</small>
        </div>
        <Pill tone={plugin.approval.tone}>{plugin.approval.label}</Pill>
      </div>
      {plugin.description && <p className="pc-plugin-desc">{plugin.description}</p>}
      <dl className="pc-kv">
        <div><dt>Version</dt><dd>{plugin.version || '—'}</dd></div>
        <div><dt>Publisher</dt><dd>{plugin.publisher.name} <span className="pc-muted">· {plugin.publisher.kindLabel}</span></dd></div>
        <div><dt>Support status</dt><dd>{plugin.supportStatus}</dd></div>
        <div><dt>Approval state</dt><dd>{plugin.approval.label}</dd></div>
        {/* The digest is shown verbatim (wrapped, never truncated) so an
            add/upgrade decision binds the exact reviewed bytes. */}
        <div className="pc-kv-wide"><dt>Digest</dt><dd className="pc-digest">{plugin.digest || '—'}</dd></div>
        {/* Credentials by reference only: requirement + owning host + opaque
            reference ids. There is no value/secret/token field in the shape. */}
        <div className="pc-kv-wide"><dt>Credential</dt><dd className="pc-credential">{credentialSummary(plugin.credential)}</dd></div>
      </dl>
      <section className="pc-plugin-tools" aria-label={`Tools in ${plugin.title}`}>
        <p className="pc-subhead">Tools ({plugin.tools.length})</p>
        <ul>
          {plugin.tools.map((tool) => (
            <li key={tool.key}>
              <b>{tool.title}</b>
              <span className="pc-tool-perm">{toolPermissionSummary(tool)}</span>
            </li>
          ))}
        </ul>
      </section>
    </article>
  )
}

// The result / error / reconcile card for a looked-up dispatch receipt. Exactly
// one card renders per the receipt status. The result and reconcile cards
// announce as role=status; the denied (error) card announces as role=alert — so
// both a success and a failure are surfaced accessibly to a screen reader
// (criterion 3). Credential use is shown by reference only.
function ReceiptCard({ receipt }) {
  const kind = receiptCardKind(receipt)
  const cred = receipt.credentialUse
  const credLine = cred.requirement === 'host_owned'
    ? `Host-owned by ${cred.ownerHost || 'an unnamed host'} · ${cred.refs.length ? cred.refs.join(', ') : 'no references'}`
    : 'No credential used'
  const meta = (
    <dl className="pc-kv">
      <div><dt>Receipt</dt><dd>{receipt.receiptId || '—'}</dd></div>
      <div><dt>Kind</dt><dd>{receipt.kind}</dd></div>
      <div><dt>Effect</dt><dd>{receipt.effectLabel}</dd></div>
      <div><dt>Outcome</dt><dd>{receipt.approval.label}</dd></div>
      {receipt.toolId && <div><dt>Tool</dt><dd>{receipt.toolId}</dd></div>}
      <div className="pc-kv-wide"><dt>Credential use</dt><dd className="pc-credential">{credLine}</dd></div>
    </dl>
  )
  if (kind === 'error') {
    return (
      <section className="pc-receipt pc-receipt-error" role="alert" aria-label="Dispatch denied">
        <div className="pc-receipt-head"><b>Denied</b><Pill tone="amber">{receipt.error.code}</Pill></div>
        <p className="pc-receipt-summary">{receipt.error.safeSummary || 'The request was denied.'}</p>
        <p className="pc-muted">{receipt.error.retryable ? 'Retryable.' : 'Not retryable.'}</p>
        {meta}
      </section>
    )
  }
  if (kind === 'reconcile') {
    return (
      <section className="pc-receipt pc-receipt-reconcile" role="status" aria-label="Dispatch pending reconciliation">
        <div className="pc-receipt-head"><b>Pending reconciliation</b><Pill tone="amber">{receipt.reconciliation.code}</Pill></div>
        <p className="pc-receipt-summary">{receipt.reconciliation.safeSummary || 'The outcome is unknown and awaits reconciliation.'}</p>
        {meta}
      </section>
    )
  }
  if (kind === 'result') {
    return (
      <section className="pc-receipt pc-receipt-result" role="status" aria-label="Dispatch result">
        <div className="pc-receipt-head"><b>{receipt.approval.label}</b><Pill tone="green">{receipt.status}</Pill></div>
        {receipt.result.outputSummary && <p className="pc-receipt-summary">{receipt.result.outputSummary}</p>}
        <p className="pc-muted pc-digest">Output digest: {receipt.result.outputDigest || '—'}</p>
        {meta}
      </section>
    )
  }
  return (
    <section className="pc-receipt" role="status" aria-label="Dispatch receipt">
      <p className="pc-receipt-summary">This receipt carries no result, error, or reconciliation body.</p>
      {meta}
    </section>
  )
}

// The selected tool's detail: its effect, gate posture, data policy, typed I/O
// field names (read-only, no value), approval state, and a request-digest lookup
// that resolves the tool-dispatch receipt into an accessible result/error card.
function ToolDetail({ tool, onClose, detailRef, announce }) {
  const [digest, setDigest] = useState('')
  const [receiptState, setReceiptState] = useState({ status: 'idle', receipt: null, message: null })
  const mountedRef = useRef(true)
  const lookupSeq = useRef(0)

  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])

  const lookup = async (event) => {
    event.preventDefault()
    const requestDigest = digest.trim()
    if (!requestDigest) return
    const seq = (lookupSeq.current += 1)
    setReceiptState({ status: 'loading', receipt: null, message: null })
    announce(`Looking up the dispatch receipt for ${tool.title}…`)
    try {
      const value = await fetchPluginReceipt(requestDigest)
      if (seq !== lookupSeq.current || !mountedRef.current) return
      const described = describeReceipt(value.receipt)
      setReceiptState({ status: 'loaded', receipt: described, message: null })
      announce(`Dispatch receipt: ${described.approval.label}.`)
    } catch (error) {
      if (seq !== lookupSeq.current || !mountedRef.current) return
      setReceiptState({ status: 'error', receipt: null, message: error.message })
      announce(error.message || 'The tool receipt is unavailable.')
    }
  }

  return (
    <section className="pc-tool-detail" role="group" aria-labelledby="pc-tool-detail-title">
      <div className="pc-tool-detail-head">
        <h3 id="pc-tool-detail-title" tabIndex={-1} ref={detailRef}>{tool.title}</h3>
        <button className="pc-close" aria-label={`Close ${tool.title} detail`} onClick={onClose}>Close</button>
      </div>
      <small className="pc-tool-scoped">{tool.key}</small>
      {tool.summary && <p className="pc-tool-summary">{tool.summary}</p>}
      <p className="pc-tool-perm-line">{toolPermissionSummary(tool)}</p>
      <dl className="pc-kv">
        <div><dt>Effect</dt><dd>{tool.effectLabel}</dd></div>
        <div><dt>Approval</dt><dd>{tool.approval.label}</dd></div>
        <div><dt>Preview gate</dt><dd>{tool.gates.preview.replaceAll('_', ' ')}</dd></div>
        <div><dt>Confirmation gate</dt><dd>{tool.gates.confirmation.replaceAll('_', ' ')}</dd></div>
        <div className="pc-kv-wide"><dt>Data policy</dt><dd>{tool.dataAccessLabels.length ? tool.dataAccessLabels.join(', ') : 'No project data'}</dd></div>
        <div className="pc-kv-wide"><dt>Typed input</dt><dd>{tool.inputFields.length ? tool.inputFields.join(', ') : 'no declared properties'}</dd></div>
        <div className="pc-kv-wide"><dt>Typed output</dt><dd>{tool.outputFields.length ? tool.outputFields.join(', ') : 'no declared properties'}</dd></div>
      </dl>

      <form className="pc-receipt-form" onSubmit={lookup}>
        <label htmlFor="pc-receipt-digest">Look up a dispatch receipt by request digest</label>
        <div className="pc-receipt-form-row">
          <input
            id="pc-receipt-digest"
            type="text"
            value={digest}
            onChange={(event) => setDigest(event.target.value)}
            placeholder="sha256:…"
            aria-describedby="pc-receipt-help"
          />
          <button type="submit" disabled={!digest.trim() || receiptState.status === 'loading'}>
            {receiptState.status === 'loading' ? 'Looking up…' : 'Look up receipt'}
          </button>
        </div>
        <small id="pc-receipt-help" className="pc-muted">A request digest is an id, not a command or path. The receipt is served redacted, credentials by reference only.</small>
      </form>

      {receiptState.status === 'error' && (
        <section className="pc-receipt pc-receipt-error" role="alert" aria-label="Receipt unavailable">
          <p className="pc-receipt-summary">{receiptState.message}</p>
        </section>
      )}
      {receiptState.status === 'loaded' && receiptState.receipt && <ReceiptCard receipt={receiptState.receipt} />}
    </section>
  )
}

// A generic category section: label + permission model + availability note +
// optional children. Always rendered (even when unavailable) so the five
// categories stay distinct and labeled rather than a flat list (criterion 1).
function CategorySection({ category, children }) {
  return (
    <section className="pc-category" aria-labelledby={`pc-cat-${category.kind}`}>
      <div className="pc-category-head">
        <h2 id={`pc-cat-${category.kind}`} className="pc-category-title">{category.label}</h2>
        <Pill tone={category.available ? 'green' : 'amber'}>{category.available ? 'available' : 'unavailable'}</Pill>
      </div>
      <p className="pc-category-perm">{category.permissionModel}</p>
      {children}
      {!category.available && category.note && <p className="pc-category-note">{category.note}</p>}
    </section>
  )
}

export default function PluginCatalogView({ data, append }) {
  const [status, setStatus] = useState('loading')
  const [errorMessage, setErrorMessage] = useState(null)
  const [plugins, setPlugins] = useState([])
  const [selectedToolKey, setSelectedToolKey] = useState(null)
  const [announce, setAnnounce] = useState('')
  const mountedRef = useRef(true)
  const loadSeq = useRef(0)
  const detailRef = useRef(null)
  const listRef = useRef(null)

  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])

  const load = async () => {
    const seq = (loadSeq.current += 1)
    setStatus('loading'); setErrorMessage(null); setAnnounce('Loading the plugin catalog…')
    try {
      const value = await fetchPlugins()
      if (seq !== loadSeq.current || !mountedRef.current) return
      const list = Array.isArray(value?.plugins) ? value.plugins : []
      setPlugins(list)
      setStatus('ready')
      setAnnounce(list.length ? `Loaded ${list.length} plugin${list.length === 1 ? '' : 's'}.` : 'No plugins are enabled.')
    } catch (error) {
      if (seq !== loadSeq.current || !mountedRef.current) return
      // Key the unconfigured-degrade branch off the SHARED sentinel value, not a
      // private regex, so an api.js reword can't silently drop us out of the
      // truthful "not configured" state (both sides move with PLUGIN_NOT_CONFIGURED).
      const unconfigured = (error.message || '') === PLUGIN_NOT_CONFIGURED
      setStatus(unconfigured ? 'unavailable' : 'error')
      setErrorMessage(error.message)
      setAnnounce(error.message || 'The plugin catalog is unavailable.')
    }
  }

  useEffect(() => { load() /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [])

  const skills = data?.skills || []
  const routerConfigured = data?.router_configured === true

  // The category model still renders when the catalog itself is 503: plugins and
  // tools degrade to their unavailable note while the bridge-published Skills
  // category (from bootstrap) and the truthful Routes/Delivery notes still show.
  const categories = useMemo(
    () => buildCategoryModel({ plugins: status === 'ready' ? plugins : [], skills, routerConfigured }),
    [plugins, status, skills, routerConfigured],
  )

  const allTools = useMemo(() => categories.find((c) => c.kind === 'tool')?.items || [], [categories])
  const selectedTool = useMemo(() => allTools.find((t) => t.key === selectedToolKey) || null, [allTools, selectedToolKey])

  // Move focus into the opened tool detail so a keyboard user is never dropped to
  // <body> (a11y focus management).
  useEffect(() => { if (selectedTool) detailRef.current?.focus() }, [selectedToolKey])

  const selectTool = (key) => {
    setSelectedToolKey(key)
    const tool = allTools.find((t) => t.key === key)
    if (tool) setAnnounce(`Selected tool ${tool.title}.`)
  }

  const closeDetail = () => {
    const invoked = selectedToolKey
    setSelectedToolKey(null)
    setAnnounce('Closed tool detail.')
    // Restore focus to the invoking tool button, not the top of the list.
    const invoker = invoked && listRef.current?.querySelector(`[data-tool-key="${invoked}"]`)
    const target = invoker || listRef.current?.querySelector('button')
    target?.focus?.()
  }

  // Document-level Escape closes the detail even after focus has moved among its
  // controls, but yields to a native <select> (none here today, kept for parity
  // with the settings/deliver pattern) and a handler that already consumed it.
  useEffect(() => {
    if (!selectedTool) return undefined
    const onKeyDown = (event) => {
      if (event.key !== 'Escape') return
      if (event.defaultPrevented) return
      if (event.target && event.target.tagName === 'SELECT') return
      closeDetail()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedTool, selectedToolKey])

  const pluginCategory = categories.find((c) => c.kind === 'plugin')
  const toolCategory = categories.find((c) => c.kind === 'tool')
  const skillCategory = categories.find((c) => c.kind === 'skill')
  const routeCategory = categories.find((c) => c.kind === 'route')
  const deliveryCategory = categories.find((c) => c.kind === 'delivery_operation')

  return (
    <main className="pc" aria-label="Plugins and tools">
      <div className="pc-inner">
        <header className="pc-header">
          <span className="crumb">Plugins &amp; tools / reviewed capability catalog</span>
          <h1>Plugins &amp; tools</h1>
          <p className="pc-intro">
            The reviewed, capability-enabled catalog. Skills, plugins, tools, routes, and delivery operations are distinct
            categories with their own permission model. Add / upgrade review shows the exact version, digest, publisher,
            access, effect, data policy, and approval state; credentials appear by reference only.
          </p>
        </header>

        {status === 'loading' && <p className="pc-state" role="status">Loading the plugin catalog…</p>}

        {/* Skills is served by the bootstrap projection, so it renders regardless of
            the plugin-catalog status — the surface never blanks out on a 503. */}
        <CategorySection category={skillCategory}>
          {skillCategory.available && (
            <ul className="pc-simple-list">
              {skillCategory.items.map((skill) => (
                <li key={`${skill.bridgeId}:${skill.skillId}`}>
                  <b>{skill.skillId}</b>
                  <span className="pc-tool-perm">{skill.permissionSummary}</span>
                  {skill.description && <small className="pc-muted">{skill.description}</small>}
                </li>
              ))}
            </ul>
          )}
        </CategorySection>

        {status === 'unavailable' && (
          <div className="pc-state pc-unavailable" role="status">
            <b>The plugin catalog is not configured</b>
            <span>{errorMessage || 'The plugin catalog is not configured for this hub.'}</span>
            <button onClick={load}>Retry</button>
          </div>
        )}
        {status === 'error' && (
          <div className="pc-state pc-error" role="alert">
            <b>The plugin catalog could not be loaded</b>
            <span>{errorMessage}</span>
            <button onClick={load}>Retry</button>
          </div>
        )}

        <CategorySection category={pluginCategory}>
          {pluginCategory.available && (
            <div className="pc-plugin-grid">
              {pluginCategory.items.map((plugin) => <PluginCard key={plugin.pluginId} plugin={plugin} />)}
            </div>
          )}
        </CategorySection>

        <CategorySection category={toolCategory}>
          {toolCategory.available && (
            <ul className="pc-tool-list" ref={listRef}>
              {toolCategory.items.map((tool) => (
                <li key={tool.key}>
                  <button
                    type="button"
                    className={`pc-tool-select ${selectedToolKey === tool.key ? 'selected' : ''}`}
                    data-tool-key={tool.key}
                    aria-pressed={selectedToolKey === tool.key}
                    aria-label={`Select tool ${tool.title} from ${tool.pluginTitle || 'plugin'}`}
                    onClick={() => selectTool(tool.key)}
                  >
                    <span className="pc-tool-select-title">{tool.title}</span>
                    <span className="pc-tool-select-plugin">{tool.pluginTitle || tool.pluginId}</span>
                    <span className="pc-tool-perm">{toolPermissionSummary(tool)}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          {selectedTool && (
            <ToolDetail tool={selectedTool} onClose={closeDetail} detailRef={detailRef} announce={setAnnounce} />
          )}
        </CategorySection>

        <CategorySection category={routeCategory} />
        <CategorySection category={deliveryCategory} />
      </div>
      <div className="pc-live" role="status" aria-live="polite">{announce}</div>
    </main>
  )
}

// Re-exported for a focused component test of the add/upgrade projection path.
export { describePlugin }
