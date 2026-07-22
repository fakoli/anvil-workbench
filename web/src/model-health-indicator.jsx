import { useEffect, useMemo, useRef, useState } from 'react'
import { fetchModelHealth } from './api'

// --- Top-right backend-health indicator (debug at a glance) -------------------
//
// A small, unobtrusive cluster of five status dots in the shared top bar — Router,
// Heavy, Fast, Voice, OCR — so an operator can see "how the planes are doing"
// without opening a view. It reads ONLY the hub's /api/system/model-health
// projection, which itself reads ONLY the Anvil Serving router surface; this
// component never talks to a model serve and never sees a token or endpoint.
//
// Honesty (matches the endpoint's own source_note): the per-tier and OCR dots
// reflect RECENT ROUTING health derived from the router's decision log, not a live
// per-tier probe; the Voice dot reflects audio-gateway registration. The popover
// says so, and each dot's detail comes straight from the server.
//
// Accessibility: color is never the only cue — each status also has a distinct
// glyph that survives grayscale (✓ ok, ! degraded, · idle, × down, ? unknown). The
// cluster is a single focusable button with a full-text aria-label; a polite
// aria-live region announces status changes; the popover is keyboard-openable and
// Escape-dismissable. It degrades quietly (all dots grey "unknown") if the endpoint
// is unavailable — it never toasts, blocks, or clutters the main UI.

const POLL_MS = 20000

// Each status carries a colorblind-safe glyph AND a tone class. The glyph alone
// distinguishes all five statuses in grayscale; the tone adds the redundant color.
const STATUS_META = {
  ok: { glyph: '✓', tone: 'ok', word: 'ok' },
  degraded: { glyph: '!', tone: 'warn', word: 'degraded' },
  idle: { glyph: '·', tone: 'warn', word: 'idle' },
  down: { glyph: '×', tone: 'down', word: 'down' },
  unknown: { glyph: '?', tone: 'unknown', word: 'unknown' },
}

function meta(status) { return STATUS_META[status] || STATUS_META.unknown }

// The five components in a fixed order, used as the honest fallback when the
// endpoint is unavailable (so the dots always render on every view).
const FALLBACK = [
  { id: 'router', label: 'Router' }, { id: 'heavy', label: 'Heavy' },
  { id: 'fast', label: 'Fast' }, { id: 'voice', label: 'Voice' }, { id: 'ocr', label: 'OCR' },
].map((component) => ({ ...component, status: 'unknown', detail: 'Backend health is unavailable.' }))

// A short, screen-reader-friendly summary of the whole cluster.
function summarize(components) {
  return components.map((component) => `${component.label} ${meta(component.status).word}`).join(', ')
}

export default function ModelHealthIndicator() {
  const [components, setComponents] = useState(FALLBACK)
  const [sourceNote, setSourceNote] = useState('')
  const [checkedAt, setCheckedAt] = useState(null)
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [announce, setAnnounce] = useState('')
  const buttonRef = useRef(null)
  const popoverRef = useRef(null)
  const lastSummaryRef = useRef('')
  const mountedRef = useRef(true)

  const refresh = async () => {
    setLoading(true)
    try {
      const body = await fetchModelHealth()
      if (!mountedRef.current) return
      const next = Array.isArray(body?.components) && body.components.length ? body.components : FALLBACK
      setComponents(next)
      setSourceNote(typeof body?.source_note === 'string' ? body.source_note : '')
      setCheckedAt(typeof body?.checked_at === 'string' ? body.checked_at : null)
    } catch {
      // Degrade quietly: keep the cluster present as all-unknown, never toast.
      if (mountedRef.current) { setComponents(FALLBACK); setSourceNote(''); setCheckedAt(null) }
    } finally {
      if (mountedRef.current) setLoading(false)
    }
  }

  // Poll on mount and every POLL_MS, but pause while the tab is hidden so a
  // backgrounded tab never hammers the router; refresh immediately on re-show.
  useEffect(() => {
    mountedRef.current = true
    let timer = null
    const tick = () => { if (!document.hidden) refresh() }
    tick()
    const start = () => { if (timer == null) timer = window.setInterval(tick, POLL_MS) }
    const stop = () => { if (timer != null) { window.clearInterval(timer); timer = null } }
    const onVisibility = () => { if (document.hidden) { stop() } else { refresh(); start() } }
    start()
    document.addEventListener('visibilitychange', onVisibility)
    return () => { mountedRef.current = false; stop(); document.removeEventListener('visibilitychange', onVisibility) }
  }, [])

  // Announce (politely) only when the overall status picture actually changes.
  const summary = useMemo(() => summarize(components), [components])
  useEffect(() => {
    if (lastSummaryRef.current && lastSummaryRef.current !== summary) setAnnounce(`Backend health: ${summary}`)
    lastSummaryRef.current = summary
  }, [summary])

  // Refresh on open (freshest picture when the operator looks) and move focus in.
  const openPopover = () => { setOpen(true); refresh() }
  useEffect(() => {
    if (!open) return undefined
    const onKey = (event) => { if (event.key === 'Escape') { setOpen(false); buttonRef.current?.focus() } }
    document.addEventListener('keydown', onKey)
    popoverRef.current?.focus()
    return () => document.removeEventListener('keydown', onKey)
  }, [open])

  return (
    <div className="mh" onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false) }}>
      <button
        ref={buttonRef}
        type="button"
        className="mh-cluster"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={`Backend health: ${summary}. Activate for details.`}
        onClick={() => (open ? setOpen(false) : openPopover())}
      >
        {components.map((component) => {
          const info = meta(component.status)
          return (
            <span key={component.id} className={`mh-pip mh-${info.tone}`} title={`${component.label}: ${info.word}`}>
              <span className="mh-dot" aria-hidden="true">{info.glyph}</span>
              <span className="mh-tag" aria-hidden="true">{component.label.charAt(0)}</span>
            </span>
          )
        })}
      </button>
      {/* aria-live only (no role="status") so this private announcer never
          collides with a view's own status region. */}
      <span className="mh-live" aria-live="polite">{announce}</span>
      {open && (
        <div ref={popoverRef} className="mh-popover" role="dialog" aria-label="Backend health detail" tabIndex={-1}>
          <header className="mh-pop-head">
            <b>Backend health</b>
            <button type="button" className="mh-refresh" onClick={refresh} disabled={loading}>
              {loading ? 'Refreshing…' : 'Refresh'}
            </button>
          </header>
          <ul className="mh-list">
            {components.map((component) => {
              const info = meta(component.status)
              return (
                <li key={component.id} className="mh-row">
                  <span className={`mh-dot mh-${info.tone}`} aria-hidden="true">{info.glyph}</span>
                  <div className="mh-row-body">
                    <div className="mh-row-head">
                      <b>{component.label}</b>
                      <span className={`mh-word mh-${info.tone}`}>{info.word}</span>
                    </div>
                    {component.detail && <p className="mh-detail">{component.detail}</p>}
                    {component.last_seen && <small className="mh-seen">last seen {component.last_seen}</small>}
                  </div>
                </li>
              )
            })}
          </ul>
          {sourceNote && <p className="mh-note">{sourceNote}</p>}
          {checkedAt && <small className="mh-checked">checked {checkedAt}</small>}
        </div>
      )}
    </div>
  )
}
