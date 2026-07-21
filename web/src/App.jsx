import { useEffect, useMemo, useRef, useState } from 'react'
import {
  addDirective, approve, bootstrap, createProject, createSession, fetchRoutes, probeSkills,
  runSandbox, searchEvidence, startWorkflow, taskLineage, voiceSocketUrl,
  archiveConversation, branchTurn, createConversation, deleteConversation, fetchChatRoutes,
  getConversation, listConversations, renameConversation, retryTurn, searchConversations,
  sendMessage, unarchiveConversation,
  fetchPrdContent, fetchPrdTasks, fetchTaskEligibility,
} from './api'
import { describeConversation, selectChatRoute, successorTurnBody, terminalToStatus } from './chat-api'
import {
  deliverBlockReason, describeEligibility, describePrdContent, describeTaskReference,
  filterDescribedTasks, freshnessLabel, nextDeliverCandidate, progressSummaryLabel,
  workflowEntryModel,
} from './delivery-explorer'

const emptyData = {
  projects: [], runs: [], sessions: [], workflows: [], approvals: [], skills: [], directives: [], audit: [],
  router_configured: false, sandbox: { available: false, models: [] },
  voice: { available: false, transport: 'not_configured', retains_transcripts: false },
}

// Chat is first and selected by default; Delivery stays reachable directly below
// it (chat-first-voice T004.4). The order here IS the rendered nav order.
const nav = [
  ['Chat', '◇'], ['Delivery', '⌘'], ['Explorer', '▦'], ['Sessions', '◫'], ['Runs', '↗'], ['Routes', '⌁'], ['Approvals', '✓'],
  ['Evidence', '◈'], ['Skills', '✦'], ['Sandbox', '□'],
]

function Mark() { return <span className="brand-mark" aria-hidden="true"><i /><i /><i /></span> }
function Status({ tone = 'green', children }) { return <span className={`status status-${tone}`}><span className="dot" />{children}</span> }
function tone(status) { return ['reconciliation', 'pending', 'not_configured', 'unavailable'].includes(status) ? 'amber' : 'green' }

function Rail({ active, setActive, onNewDelivery, onProfile }) {
  return <aside className="rail">
    <div className="brand"><Mark /><span>Anvil<br /><em>Workbench</em></span></div>
    <nav>{nav.map(([label, glyph]) => <button key={label} aria-label={label} aria-current={active === label ? 'page' : undefined} className={active === label ? 'nav-item selected' : 'nav-item'} onClick={() => setActive(label)}><b aria-hidden="true">{glyph}</b>{label}</button>)}</nav>
    <div className="rail-footer"><button className="new-run" onClick={onNewDelivery}><span aria-hidden="true">+</span> New delivery</button><button className="profile" aria-label="Operator menu" onClick={onProfile}><span>AW</span><div><strong>Operator</strong><small>tailnet owner</small></div><b aria-hidden="true">···</b></button></div>
  </aside>
}

function encodePcm16(samples) {
  const bytes = new Uint8Array(samples.length * 2)
  const view = new DataView(bytes.buffer)
  samples.forEach((sample, index) => view.setInt16(index * 2, Math.max(-1, Math.min(1, sample)) * 0x7fff, true))
  let binary = ''
  for (let index = 0; index < bytes.length; index += 0x8000) binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000))
  return window.btoa(binary)
}

function VoiceDock({ data, append }) {
  const session = data.sessions?.[0]
  const configured = Boolean(data.voice?.available && session)
  const [state, setState] = useState('disconnected')
  const socketRef = useRef(null)
  const captureRef = useRef(null)
  const releaseCapture = () => {
    const capture = captureRef.current
    if (!capture) return
    capture.processor.disconnect(); capture.source.disconnect(); capture.stream.getTracks().forEach((track) => track.stop())
    capture.context.close().catch(() => undefined); captureRef.current = null
  }
  const playAudio = (encoded) => {
    if (typeof encoded !== 'string' || !encoded || !window.AudioContext) return
    try {
      const binary = window.atob(encoded); const context = new window.AudioContext({ sampleRate: 24000 })
      const buffer = context.createBuffer(1, binary.length / 2, 24000); const channel = buffer.getChannelData(0)
      const bytes = new Uint8Array(binary.length); for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index)
      const view = new DataView(bytes.buffer); for (let index = 0; index < channel.length; index += 1) channel[index] = view.getInt16(index * 2, true) / 0x8000
      const source = context.createBufferSource(); source.buffer = buffer; source.connect(context.destination); source.start(); source.onended = () => context.close().catch(() => undefined)
    } catch { append('Voice output could not be played in this browser.') }
  }
  const connect = () => {
    if (!configured) return
    setState('connecting')
    const socket = new WebSocket(voiceSocketUrl(session.id)); socketRef.current = socket
    socket.onopen = () => { socket.send(JSON.stringify({ type: 'session.update', session: { modalities: ['audio', 'text'] } })); setState('ready') }
    socket.onmessage = (event) => { try { const message = JSON.parse(event.data); if (message.type === 'response.output_audio.delta') playAudio(message.delta); if (message.type === 'error') append('Voice relay rejected an event; no delivery action was started.') } catch { append('Voice relay returned an unreadable event; no delivery action was started.') } }
    socket.onclose = () => { releaseCapture(); setState('disconnected') }
    socket.onerror = () => append('Voice relay is unavailable. The session and workflow remain unchanged.')
  }
  const startCapture = async () => {
    if (state !== 'ready' || socketRef.current?.readyState !== WebSocket.OPEN) return
    if (!navigator.mediaDevices?.getUserMedia || !window.AudioContext) { append('This browser cannot capture microphone audio for the Workbench voice relay.'); return }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true }); const context = new window.AudioContext({ sampleRate: 24000 })
      const source = context.createMediaStreamSource(stream); const processor = context.createScriptProcessor(4096, 1, 1)
      processor.onaudioprocess = (event) => { if (socketRef.current?.readyState === WebSocket.OPEN) socketRef.current.send(JSON.stringify({ type: 'input_audio_buffer.append', audio: encodePcm16(event.inputBuffer.getChannelData(0)) })) }
      source.connect(processor); processor.connect(context.destination); captureRef.current = { stream, context, source, processor }; setState('listening')
    } catch { append('Microphone access was not granted. No audio left this browser.') }
  }
  const finishCapture = () => { if (state !== 'listening') return; releaseCapture(); if (socketRef.current?.readyState === WebSocket.OPEN) { socketRef.current.send(JSON.stringify({ type: 'input_audio_buffer.commit' })); socketRef.current.send(JSON.stringify({ type: 'response.create' })) }; setState('ready') }
  const disconnect = () => { releaseCapture(); socketRef.current?.close(); socketRef.current = null; setState('disconnected') }
  return <section className="voice-dock" aria-label="Voice controls"><div><b>Voice, session-bound</b><small>{configured ? 'Push to talk through the private relay; delivery actions still require the bridge.' : 'Configure a private Anvil Voice Realtime endpoint to enable push to talk.'}</small></div>{!configured ? <button disabled aria-label="Voice not configured">Voice unavailable</button> : state === 'disconnected' || state === 'connecting' ? <button onClick={connect} disabled={state === 'connecting'}>{state === 'connecting' ? 'Connecting…' : 'Connect voice'}</button> : <><button className={state === 'listening' ? 'speaking' : ''} onPointerDown={startCapture} onPointerUp={finishCapture} onPointerCancel={finishCapture} aria-label="Hold to talk">{state === 'listening' ? 'Listening… release to send' : 'Hold to talk'}</button><button className="voice-close" onClick={disconnect}>Disconnect</button></>}</section>
}

function Delivery({ data, append, onDirective, onGuide, onDeliverNext }) {
  const project = data.projects[0]
  const run = data.runs.find((item) => ['queued', 'running'].includes(item.status)) || data.runs[0]
  const session = data.sessions.find((item) => item.id === run?.session_id) || data.sessions[0]
  const messages = (data.directives || []).filter((event) => !session || event.session_id === session.id)
  const [input, setInput] = useState('')
  const submit = async (event) => { event.preventDefault(); if (!session || !input.trim()) return; const text = input.trim(); try { await onDirective(session.id, text); setInput('') } catch { append('Direction was not recorded. No future work packet was changed.') } }
  if (!project) return <main className="delivery empty-delivery"><span className="crumb">Delivery / setup required</span><h1>Start a private delivery</h1><p>Workbench has no synthetic delivery. Create a project, register its local bridge, publish reviewed skills, then create a session.</p><button className="session-action" onClick={onGuide}>Open setup guide</button></main>
  return <main className="delivery"><header className="project-header"><div><span className="crumb">Delivery / {project.name}</span><h1>{run?.task_id ? `Task ${run.task_id}` : 'No active task'}</h1><p>PRD → State plan → local Codex run → evidence → approved PR</p></div><div className="project-header-actions"><button className="session-action" onClick={onDeliverNext}>Deliver next task</button><Status tone={run ? tone(run.status) : 'amber'}>{run?.status || 'ready for session'}</Status></div></header>
    <section className="flow-card"><div className="flow-top"><span className="thread-avatar">A</span><div><strong>Delivery operator</strong><small>{run ? `${run.model} through Anvil Serving` : 'Waiting for a bridge-supervised run'}</small></div><Status tone={run ? tone(run.status) : 'amber'}>{run?.status || 'idle'}</Status></div><ol className="steps"><li className={run ? 'complete' : 'current'}><span>{run ? '✓' : '1'}</span><div><b>{run ? 'State work packet requested' : 'Create a session'}</b><small>{run ? `${run.id} is bound to the bridge and its configured worktree.` : 'A session creates a durable workflow and lease boundary.'}</small></div></li><li className={run?.status === 'evidenced' ? 'complete' : run ? 'current' : ''}><span>{run?.status === 'evidenced' ? '✓' : '2'}</span><div><b>Bridge edits and verifies locally</b><small>Redacted transcripts and State evidence return through the bridge.</small></div></li><li className={run?.status === 'evidenced' ? 'current' : ''}><span>3</span><div><b>Review evidence and authorize a hash-bound action</b><small>GitHub remains local to the bridge and requires approval.</small></div></li></ol></section>
    <section className="conversation" aria-label="Delivery directions">{messages.length ? messages.map((message) => <article className="message human" key={message.id}><div className="message-head"><span>OP</span><b>Recorded direction</b><small>event {message.sequence}</small></div><p>{message.data?.content}</p></article>) : <p className="evidence-empty">No recorded delivery directions for this session yet.</p>}</section>
    <form className="composer" onSubmit={submit}><textarea aria-label="Add direction to this delivery" value={input} disabled={!session} onChange={(event) => setInput(event.target.value)} rows="2" placeholder={session ? 'Add a direction for the next work packet…' : 'Create a session before adding delivery direction…'} /><div><small>Saved into the next bridge work packet; it does not interrupt a running Codex process.</small><button type="submit" disabled={!session || !input.trim()} aria-label="Send delivery direction">Send <span aria-hidden="true">↵</span></button></div></form><VoiceDock data={data} append={append} />
  </main>
}

function Trace({ data, setActive, append, refresh, selectedApprovalId, clearApproval }) {
  const run = data.runs[0]; const approval = data.approvals.find((item) => item.id === selectedApprovalId && item.status === 'pending'); const [expanded, setExpanded] = useState(false); const [busy, setBusy] = useState(false)
  const authorize = async () => { if (!approval) return; setBusy(true); try { await approve(approval.id); clearApproval(); append('Approval recorded. The local bridge may consume this exact action once.'); await refresh() } catch { append('Approval was not recorded. The bridge remains unable to create a PR.') } finally { setBusy(false) } }
  return <aside className="trace"><section className="trace-head"><div><span>Live trace</span><Status tone={data.router_configured ? 'green' : 'amber'}>{data.router_configured ? 'router configured' : 'router unavailable'}</Status></div><button aria-label="Show correlation trace" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? '−' : '↗'}</button></section><section className="trace-body"><TraceStep label="Intent" value={run?.task_id || 'No task selected'} detail={run ? `run ${run.id}` : 'Start a session to request a State work packet.'} done={Boolean(run)} /><TraceStep label="Route" value={run?.model || 'No route selected'} detail={data.router_configured ? 'decision lookup is available in Routes' : 'router token or URL is missing'} done={Boolean(run && data.router_configured)} /><TraceStep label="Skills" value={data.skills?.length ? `${data.skills.length} bridge-published` : 'No skills published'} detail="Only selected local skills enter a work packet." done={Boolean(data.skills?.length)} /><TraceStep label="Verify" value={run?.status === 'evidenced' ? 'evidence submitted' : 'independent gate pending'} detail="The bridge submits evidence to State before approval." current={run?.status !== 'evidenced'} />{expanded && <div className="raw-trace"><code>workbench_run_id: {run?.id || 'not assigned'}</code><code>task_id: {run?.task_id || 'not assigned'}</code><code>request_id: written by Serving if the model request succeeds</code></div>}</section><section className="evidence-mini"><header><span>Evidence packet</span><button onClick={() => setActive('Evidence')}>View all</button></header><p className="evidence-empty">{run?.status === 'evidenced' ? 'Use Evidence search to inspect cited, redacted artifacts.' : 'Awaiting independent bridge evidence.'}</p></section><section className="approval-card"><div className="approval-title"><span>Approval</span><Status tone={approval ? 'amber' : 'green'}>{approval ? 'selected' : 'selection required'}</Status></div><h2>{approval ? approval.action_type.replaceAll('_', ' ') : 'Select an approval to review'}</h2>{approval ? <><dl className="approval-binding"><div><dt>Approval id</dt><dd>{approval.id}</dd></div><div><dt>Payload hash</dt><dd>{approval.payload_hash}</dd></div><div><dt>Run</dt><dd>{approval.payload?.run_id || 'not bound'}</dd></div><div><dt>Worktree</dt><dd>{approval.payload?.worktree_id || 'not bound'}</dd></div></dl><pre aria-label="Selected approval payload">{JSON.stringify(approval.payload, null, 2)}</pre></> : <p>Open Approvals and choose one pending action. Workbench never guesses which grant you intended.</p>}<p className="muted">Approval is a one-time release; the bridge checks this exact safe payload and binding before it can change GitHub.</p><button disabled={busy || !approval} onClick={authorize}>{busy ? 'Authorizing…' : 'Authorize selected action'} <span aria-hidden="true">→</span></button></section></aside>
}

function TraceStep({ label, value, detail, done, current }) { return <div className={`trace-step ${done ? 'done' : ''} ${current ? 'current' : ''}`}><i>{done ? '✓' : current ? '●' : '○'}</i><div><small>{label}</small><b>{value}</b><span>{detail}</span></div></div> }

function SessionsView({ data, onNewSession, onStartSession }) { const sessions = data.sessions || []; const workflows = data.workflows || []; return <section className="workspace-view"><span className="crumb">Sessions / durable harness contexts</span><div className="view-heading"><div><h1>Concurrent sessions</h1><p>Each session has its own workflow cursor. Worktree leases prevent concurrent sessions from editing the same configured worktree.</p></div><button className="session-action" onClick={onNewSession} disabled={!data.projects[0]}>New concurrent session</button></div><div className="session-list">{sessions.length ? sessions.map((session) => { const workflow = workflows.find((item) => item.session_id === session.id); const activeRun = data.runs?.some((run) => run.session_id === session.id && ['queued', 'running'].includes(run.status)); return <article key={session.id}><div><b>{session.title}</b><small>{session.worktree_id} · {session.id}</small></div><span>{workflow ? `workflow v${workflow.version} · ${workflow.status}` : 'workflow pending'}</span><Status tone={activeRun ? 'green' : tone(workflow?.status)}>{activeRun ? 'active run' : session.status}</Status><button aria-label={`Start delivery ${session.title}`} disabled={!workflow || activeRun || workflow.status !== 'draft'} onClick={() => onStartSession(session, workflow)}>Start delivery</button></article> }) : <article className="session-empty"><b>No harness session yet</b><span>Create a project and bridge, then create a session.</span></article>}</div></section> }

function RunsView({ data, refresh }) { return <section className="workspace-view"><span className="crumb">Runs / bridge-supervised</span><div className="view-heading"><div><h1>Runs</h1><p>Every row is a durable Workbench run, correlated to a State task and selected model.</p></div><button className="session-action" onClick={refresh}>Refresh runs</button></div><div className="data-list">{data.runs.length ? data.runs.map((run) => <article key={run.id}><b>{run.task_id || 'Unassigned task'}</b><span>{run.model} · {run.id}</span><Status tone={tone(run.status)}>{run.status}</Status></article>) : <article><b>No delivery runs yet</b><span>Start a session workflow to create a bridge command.</span><Status tone="amber">idle</Status></article>}</div></section> }

function RoutesView({ data, append }) { const [routes, setRoutes] = useState([]); const [loaded, setLoaded] = useState(false); const [busy, setBusy] = useState(false); const refresh = async () => { setBusy(true); try { const value = await fetchRoutes(); setRoutes(value.routes || []); setLoaded(true) } catch { append('Route decisions are unavailable. Check the server-held Anvil Serving URL and token.') } finally { setBusy(false) } }; return <section className="workspace-view"><span className="crumb">Routes / Anvil Serving</span><div className="view-heading"><div><h1>Routes</h1><p>Read-only router decision metadata, filtered to Workbench run correlations.</p></div><button className="session-action" onClick={refresh} disabled={!data.router_configured || busy}>{busy ? 'Refreshing…' : 'Refresh decisions'}</button></div>{!data.router_configured ? <div className="sandbox-note"><b>Routes unavailable</b><span>Configure the hub’s Anvil Serving URL and token. The browser never sees either credential.</span></div> : <div className="data-list">{loaded && !routes.length ? <article><b>No correlated decisions yet</b><span>Run a bridge delivery, then refresh after Serving records the request.</span><Status tone="amber">waiting</Status></article> : routes.map((route, index) => <article key={`${route.request_id || index}`}><b>{route.intent || route.model || route.served_model || route.served_tier || 'selected route'}</b><span>{route.workbench_run_id} · {route.task_id || 'no task id'} · {route.request_id || 'no request id'}</span><Status>{route.status || route.tier || route.served_tier || 'recorded'}</Status></article>)}</div>}</section> }

function ApprovalsView({ data, selectApproval }) { const pending = data.approvals.filter((approval) => approval.status === 'pending'); return <section className="workspace-view"><span className="crumb">Approvals / hash-bound</span><h1>Approvals</h1><p>Approval only releases a matching, one-time bridge command; it does not expose a GitHub credential to this browser.</p><div className="data-list">{pending.length ? pending.map((approval) => <article key={approval.id}><b>{approval.action_type.replaceAll('_', ' ')}</b><span>{approval.id} · {approval.payload_hash}</span><button className="inline-action" aria-label={`Review action ${approval.id}`} onClick={() => selectApproval(approval.id)}>Review action</button></article>) : <article><b>No pending approval</b><span>A bridge must submit evidence before a PR action can be requested.</span><Status tone="amber">waiting</Status></article>}</div></section> }

function EvidenceView({ data, append }) { const [query, setQuery] = useState(''); const [results, setResults] = useState([]); const [lineage, setLineage] = useState(null); const project = data.projects[0]; const search = async (event) => { event.preventDefault(); if (!project || !query.trim()) return; try { const value = await searchEvidence(project.id, query.trim()); setResults(value.results || []); setLineage(null) } catch { append('Evidence search is unavailable. The graph remains read-only and never approves actions.') } }; const loadLineage = async (taskId) => { try { setLineage(await taskLineage(taskId)) } catch { append('Task lineage is unavailable for that task.') } }; return <section className="workspace-view"><span className="crumb">Evidence / redacted projection</span><h1>Evidence</h1><p>Searches the read-optimized evidence projection. Anvil State remains canonical for task acceptance.</p><form className="query-form" onSubmit={search}><input aria-label="Evidence query" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search evidence and cited artifacts" disabled={!project} /><button disabled={!project || !query.trim()}>Search evidence</button></form><div className="data-list">{results.map((result, index) => <article key={`${result.citation || index}`}><b>{result.title || result.source_id || 'Evidence artifact'}</b><span>{result.citation || result.summary || 'redacted projection result'}</span><Status>cited</Status></article>)}{!results.length && <article><b>No evidence query yet</b><span>Search only returns redacted, cited projection records.</span><Status tone="amber">ready</Status></article>}</div>{data.runs.filter((run) => run.task_id).slice(0, 4).map((run) => <button className="lineage-button" key={run.id} onClick={() => loadLineage(run.task_id)}>Show lineage for {run.task_id}</button>)}{lineage && <pre className="lineage-result">{JSON.stringify(lineage, null, 2)}</pre>}</section> }

function SkillsView({ data, append, refresh }) { const project = data.projects[0]; const [busy, setBusy] = useState(false); const verify = async () => { if (!project) return; setBusy(true); try { await probeSkills(project.id); append('Bridge skill check queued. The bridge verifies local files and matching digests before it reports evidence.'); await refresh() } catch { append('Skills could not be checked. A project bridge must publish configured skill metadata first.') } finally { setBusy(false) } }; return <section className="workspace-view"><span className="crumb">Skills / bridge-local</span><div className="view-heading"><div><h1>Reviewed skills</h1><p>Skills are discovered from explicit local bridge roots. The hub receives only names, descriptions, and digests; paths and bodies stay local.</p></div><button className="session-action" onClick={verify} disabled={!project || !data.skills.length || busy}>{busy ? 'Queueing check…' : 'Verify bridge skills'}</button></div><div className="data-list">{data.skills.length ? data.skills.map((skill) => <article key={`${skill.bridge_id}:${skill.skill_id}`}><b>{skill.skill_id}</b><span>{skill.description} · {skill.content_sha256.slice(0, 12)}…</span><Status>published</Status></article>) : <article><b>No skills published</b><span>Start the local bridge with one or more explicit <code>--skills-root</code> paths.</span><Status tone="amber">bridge setup</Status></article>}</div></section> }

function SandboxView({ data, append }) { const [model, setModel] = useState(data.sandbox?.models?.[0] || ''); const [input, setInput] = useState(''); const [result, setResult] = useState(null); const [busy, setBusy] = useState(false); useEffect(() => { setModel(data.sandbox?.models?.[0] || '') }, [data.sandbox]); const submit = async (event) => { event.preventDefault(); setBusy(true); try { setResult(await runSandbox({ model, input })) } catch { append('Sandbox request was not accepted by Anvil Serving. No provider fallback was used.') } finally { setBusy(false) } }; return <section className="workspace-view"><span className="crumb">Sandbox / Serving only</span><h1>Model sandbox</h1><p>A bounded, audited Responses request through Anvil Serving. It is separate from bridge delivery and cannot create a PR, merge, or change policy.</p>{!data.sandbox?.available ? <div className="sandbox-note"><b>Sandbox unavailable</b><span>Set an allowlisted <code>WORKBENCH_SANDBOX_MODELS</code> value plus the hub’s Serving URL and token.</span></div> : <form className="sandbox-form" onSubmit={submit}><label>Allowed route<select aria-label="Sandbox model" value={model} onChange={(event) => setModel(event.target.value)}>{data.sandbox.models.map((item) => <option key={item}>{item}</option>)}</select></label><label>Prompt<textarea aria-label="Sandbox prompt" value={input} onChange={(event) => setInput(event.target.value)} rows="5" placeholder="Ask a bounded, non-mutating model question" /></label><button disabled={busy || !input.trim()}>{busy ? 'Routing…' : 'Run through Anvil Serving'}</button></form>}{result && <section className="sandbox-result"><b>{result.model} · {result.status}</b><pre>{result.output_text || 'The routed response contained no output_text.'}</pre></section>}</section> }

function WorkspaceView({ active, data, onNewSession, onStartSession, append, refresh, selectApproval }) { if (active === 'Sessions') return <SessionsView data={data} onNewSession={onNewSession} onStartSession={onStartSession} />; if (active === 'Runs') return <RunsView data={data} refresh={refresh} />; if (active === 'Routes') return <RoutesView data={data} append={append} />; if (active === 'Approvals') return <ApprovalsView data={data} selectApproval={selectApproval} />; if (active === 'Evidence') return <EvidenceView data={data} append={append} />; if (active === 'Skills') return <SkillsView data={data} append={append} refresh={refresh} />; return <SandboxView data={data} append={append} /> }

function Modal({ title, children, onClose }) { return <div className="modal-backdrop" role="presentation"><section className="modal" role="dialog" aria-modal="true" aria-label={title}><header><h2>{title}</h2><button aria-label={`Close ${title}`} onClick={onClose}>×</button></header>{children}</section></div> }

function NewDelivery({ onClose, onCreate }) { const [name, setName] = useState(''); const [stateRoot, setStateRoot] = useState('.anvil'); const [busy, setBusy] = useState(false); const submit = async (event) => { event.preventDefault(); if (!name.trim()) return; setBusy(true); try { await onCreate({ name: name.trim(), state_root: stateRoot.trim() || '.anvil' }) } finally { setBusy(false) } }; return <Modal title="New delivery" onClose={onClose}><p>This creates a Workbench project record only. Register a project-local bridge separately; its secret never enters this browser.</p><form className="modal-form" onSubmit={submit}><label>Project name<input aria-label="Project name" value={name} onChange={(event) => setName(event.target.value)} /></label><label>State root<input aria-label="State root" value={stateRoot} onChange={(event) => setStateRoot(event.target.value)} /></label><div><button type="button" className="secondary-button" onClick={onClose}>Cancel</button><button type="submit" disabled={busy || !name.trim()}>{busy ? 'Creating…' : 'Create project'}</button></div></form></Modal> }

function NewSession({ project, skills, onClose, onCreate }) { const [title, setTitle] = useState(''); const [worktree, setWorktree] = useState('default'); const [selected, setSelected] = useState([]); const [busy, setBusy] = useState(false); const toggle = (skill) => setSelected((current) => current.includes(skill) ? current.filter((item) => item !== skill) : [...current, skill]); const submit = async (event) => { event.preventDefault(); if (!project || !title.trim() || !worktree.trim()) return; setBusy(true); try { await onCreate({ project_id: project.id, title: title.trim(), worktree_id: worktree.trim(), skills: selected }) } finally { setBusy(false) } }; return <Modal title="New concurrent session" onClose={onClose}><p>A session is a durable harness context. Its worktree id must match a local bridge configuration before delivery can start.</p><form className="modal-form" onSubmit={submit}><label>Session title<input aria-label="Session title" value={title} onChange={(event) => setTitle(event.target.value)} /></label><label>Configured worktree id<input aria-label="Configured worktree id" value={worktree} onChange={(event) => setWorktree(event.target.value)} /></label>{skills.length ? <fieldset className="skill-selector"><legend>Bridge-published skills for this session</legend>{skills.map((skill) => <label key={skill.skill_id}><input type="checkbox" checked={selected.includes(skill.skill_id)} onChange={() => toggle(skill.skill_id)} />{skill.skill_id}</label>)}</fieldset> : null}<div><button type="button" className="secondary-button" onClick={onClose}>Cancel</button><button type="submit" disabled={busy || !project || !title.trim() || !worktree.trim()}>{busy ? 'Creating…' : 'Create session'}</button></div></form></Modal> }

function StartSession({ session, workflow, onClose, onStart }) { const [taskId, setTaskId] = useState(''); const [model, setModel] = useState('planning'); const [busy, setBusy] = useState(false); const submit = async (event) => { event.preventDefault(); if (!taskId.trim()) return; setBusy(true); try { await onStart(workflow.id, { task_id: taskId.trim(), model: model.trim() || 'planning' }) } finally { setBusy(false) } }; return <Modal title={`Start ${session.title}`} onClose={onClose}><p>This queues the workflow’s pinned agent step through the configured local bridge. State claims the task; the browser never selects a filesystem path.</p><form className="modal-form" onSubmit={submit}><label>State task id<input aria-label="State task id" value={taskId} onChange={(event) => setTaskId(event.target.value)} /></label><label>Requested route<input aria-label="Requested route" value={model} onChange={(event) => setModel(event.target.value)} /></label><div><button type="button" className="secondary-button" onClick={onClose}>Cancel</button><button type="submit" disabled={busy || !taskId.trim()}>{busy ? 'Starting…' : 'Start bridge delivery'}</button></div></form></Modal> }

// --- Deliver controls (plan-task-delivery T006) ------------------------------
//
// A focus-managed setup sheet that turns a ready State task into a started run
// in ONE activation. The candidate and its blocked/eligibility truth come from
// the merged read-only delivery-projection GET surface (fetchPrdTasks →
// {tasks}, fetchTaskEligibility → {eligibility}); the start itself is the REAL
// wired POST /api/workflows/{id}/start (startWorkflow → {workflow, run}) — there
// is no separate Deliver route (workbench/deliver.py is deliberately NOT wired),
// so this speaks only the shapes the hub actually serves.
//
// The sheet previews EXACTLY ONE ranked candidate (State's plan head), never a
// batch and never skipping past a blocked head. Every choice is an approved id
// (a startable session by id, a PRD by id) — never a filesystem path or a raw
// command. A blocked candidate disables Deliver with its reason IN TEXT (not
// colour alone) and an aria-describedby binding, and a live region announces the
// candidate / blocked / delivering / error states.
function DeliverSheet({ project, workflows, sessions, runs, onClose, onDeliver }) {
  const [prdInput, setPrdInput] = useState('')
  const [selectedWorkflowId, setSelectedWorkflowId] = useState('')
  const [candidate, setCandidate] = useState(null)
  const [loadedPrdId, setLoadedPrdId] = useState('')
  const [loadError, setLoadError] = useState(null)
  const [eligibility, setEligibility] = useState({ status: 'idle', value: null, message: null })
  const [announce, setAnnounce] = useState('')
  const [busy, setBusy] = useState(false)
  const headingRef = useRef(null)
  const loadSeq = useRef(0)
  const sheetRef = useRef(null)
  const startAbortRef = useRef(null)
  // Guards a state update after the sheet unmounts: a dismissal during busy
  // tears the sheet down while the start promise may still settle, and neither
  // its success nor its abort must poke React state on the gone component.
  const mountedRef = useRef(true)
  // Set-on-mount as well as clear-on-unmount so a StrictMode dev remount (mount →
  // cleanup → remount) does not leave the ref stuck false, which would otherwise
  // swallow setBusy(false)/announce on a genuine start failure in development.
  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])

  // Only a draft workflow whose session has no active run is startable — the same
  // gate the Sessions view uses. Options carry the session TITLE (human) with the
  // worktree + id as secondary text; the option VALUE is the approved workflow id
  // (never free text). This is the "approved id/title only" choice (criterion 3).
  const startable = (workflows || [])
    .filter((workflow) => workflow.status === 'draft'
      && !(runs || []).some((run) => run.session_id === workflow.session_id && ['queued', 'running'].includes(run.status)))
    .map((workflow) => ({ workflow, session: (sessions || []).find((item) => item.id === workflow.session_id) }))
  const selectedWorkflow = startable.find((item) => item.workflow.id === selectedWorkflowId)?.workflow || null
  // The real route the hub will pin comes from the workflow's entry agent step
  // (SHOULD #2), not a non-existent `workflow.model`. When the definition does
  // not pin one, fall back to the hub's own default ('planning') and label the
  // displayed route as the default so the text never claims a derived route it
  // did not derive.
  const derivedModel = workflowEntryModel(selectedWorkflow)
  const model = derivedModel || 'planning'

  const blockReason = deliverBlockReason({ candidate, eligibility, hasSession: Boolean(selectedWorkflow) })

  // A single dismissal path (Close, Cancel, Escape). While busy it aborts the
  // in-flight start so a hung bridge POST cannot trap the user (a11y #4), then
  // closes. When idle it just closes.
  const dismiss = () => {
    if (busy) startAbortRef.current?.abort()
    onClose()
  }

  // Focus into the sheet on open and restore focus to the opener on close, so a
  // keyboard user is never dropped to <body> (a11y focus management).
  useEffect(() => {
    const opener = document.activeElement
    headingRef.current?.focus()
    return () => { opener?.focus?.() }
  }, [])
  // Document-level Escape closes the sheet even after focus has moved among its
  // controls (a11y): the visible Close button is the discoverable path, Escape
  // the keyboard one. It dismisses even while busy so a hung Deliver is never a
  // trap (#4). But it honours an inner control that already handled Escape
  // (#5): if the event was defaultPrevented, or it targets an open native
  // <select> (whose own Escape dismisses its listbox), the sheet stays open so
  // Escape does not discard the loaded candidate/eligibility out from under a
  // dropdown dismissal.
  useEffect(() => {
    const onKeyDown = (event) => {
      if (event.key !== 'Escape') return
      if (event.defaultPrevented) return
      if (event.target && event.target.tagName === 'SELECT') return
      dismiss()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [busy, onClose])

  // Keep Tab focus inside the sheet (a11y #7): with aria-modal the background is
  // occluded, so Tab/Shift+Tab must cycle within the dialog rather than reach an
  // aria-hidden background control. Wrap at the first/last enabled focusable.
  const onTrapKeyDown = (event) => {
    if (event.key !== 'Tab') return
    const root = sheetRef.current
    if (!root) return
    const focusables = Array.from(
      root.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'),
    ).filter((el) => !el.disabled && el.getAttribute('aria-hidden') !== 'true')
    if (!focusables.length) return
    const first = focusables[0]
    const last = focusables[focusables.length - 1]
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
  }

  const loadCandidate = async () => {
    const prdId = prdInput.trim()
    if (!prdId) return
    const seq = (loadSeq.current += 1)
    setCandidate(null); setLoadError(null); setLoadedPrdId('')
    setEligibility({ status: 'idle', value: null, message: null })
    setAnnounce(`Loading the ranked candidate for PRD ${prdId}…`)
    let tasks
    try {
      const value = await fetchPrdTasks(project.id, prdId)
      if (seq !== loadSeq.current) return // a newer load superseded this one
      tasks = value.tasks || []
    } catch (error) {
      if (seq !== loadSeq.current) return
      setLoadError(error.message)
      setAnnounce(error.message || `PRD ${prdId} tasks could not be loaded.`)
      return
    }
    const cand = nextDeliverCandidate(tasks)
    setCandidate(cand); setLoadedPrdId(prdId)
    if (!cand) { setAnnounce(`PRD ${prdId} has no ranked task to deliver.`); return }
    setAnnounce(`Ranked candidate: ${cand.title}. Checking delivery eligibility…`)
    setEligibility({ status: 'loading', value: null, message: null })
    try {
      const value = await fetchTaskEligibility(project.id, prdId, cand.taskId)
      if (seq !== loadSeq.current) return
      const verdict = describeEligibility(value.eligibility)
      setEligibility({ status: 'loaded', value: verdict, message: null })
      if (verdict && !verdict.eligible) {
        const primary = verdict.reasons[0]
        setAnnounce(`Delivery blocked: ${primary?.explanation || verdict.state}`)
      } else {
        setAnnounce(`Ranked candidate ready to deliver: ${cand.title}`)
      }
    } catch (error) {
      if (seq !== loadSeq.current) return
      setEligibility({ status: 'error', value: null, message: error.message })
      setAnnounce(error.message || 'Delivery eligibility is unavailable for this candidate.')
    }
  }

  // One activation starts EXACTLY ONE Deliver: the busy guard plus the disabled
  // button mean a second click/Enter while the start is in flight cannot fire a
  // second startWorkflow — the idempotent backend is not asked to dedupe a UI
  // double-submit (criterion 1 / no double-fire).
  const deliver = async () => {
    if (busy || blockReason || !candidate || !selectedWorkflow) return
    const controller = new AbortController()
    startAbortRef.current = controller
    setBusy(true)
    setAnnounce(`Delivering ${candidate.title}…`)
    try {
      await onDeliver(selectedWorkflow.id, { task_id: candidate.taskId, model }, controller.signal)
      // On success the app closes the sheet and routes to the resulting run.
    } catch {
      // A dismissal during busy unmounts the sheet (and may abort this start);
      // do not poke state on the gone component.
      if (!mountedRef.current || controller.signal.aborted) return
      setBusy(false)
      setAnnounce('Delivery could not be started. No run was launched.')
    }
  }

  return <div className="modal-backdrop" role="presentation">
    <section className="modal deliver-sheet" role="dialog" aria-modal="true" aria-labelledby="deliver-sheet-title" ref={sheetRef} onKeyDown={onTrapKeyDown}>
      <header>
        <h2 id="deliver-sheet-title" tabIndex={-1} ref={headingRef}>Deliver next task</h2>
        {/* Close stays enabled while busy so a hung Deliver is never a trap (#4). */}
        <button aria-label="Close Deliver next task" onClick={dismiss}>×</button>
      </header>
      <p>Preview State’s next ranked candidate for one PRD and start it through the local bridge in a single activation. The browser never sends a filesystem path or a raw command.</p>

      <div className="deliver-choices">
        <label>Deliver into session
          {startable.length
            ? <select aria-label="Deliver into session" value={selectedWorkflowId} onChange={(event) => setSelectedWorkflowId(event.target.value)}>
                <option value="">Choose a startable session…</option>
                {startable.map(({ workflow, session }) => <option key={workflow.id} value={workflow.id}>{session ? `${session.title} · ${session.worktree_id}` : workflow.id}</option>)}
              </select>
            : <p className="deliver-muted">No startable session. Create a session with a configured worktree and start it here — no path or command is entered in this browser.</p>}
        </label>
        <form className="deliver-open" onSubmit={(event) => { event.preventDefault(); loadCandidate() }}>
          <label>PRD id
            <input aria-label="PRD id" value={prdInput} onChange={(event) => setPrdInput(event.target.value)} placeholder="e.g. release-alpha" />
          </label>
          <button className="explorer-open-prd" type="submit" disabled={!prdInput.trim()}>Load ranked candidate</button>
          <small className="deliver-muted">PRD enumeration is not served; open a PRD by its approved id.</small>
        </form>
      </div>

      <section className="deliver-candidate" aria-label="Ranked candidate">
        {loadError
          ? <p className="explorer-degraded">{loadError}</p>
          : candidate
          ? <>
              <div className="deliver-candidate-head">
                <div>
                  <span className="deliver-candidate-eyebrow">Next ranked candidate{loadedPrdId ? ` · ${loadedPrdId}` : ''}</span>
                  <b className="deliver-candidate-title">{candidate.title}</b>
                  <small className="deliver-candidate-scoped">{candidate.scopedId}</small>
                </div>
                <Status tone={tone(candidate.status)}>{candidate.status}</Status>
              </div>
              <p className="deliver-candidate-meta">Delivery: {candidate.latestDeliveryStatus} · {candidate.dependsOn.length} {candidate.dependsOn.length === 1 ? 'dependency' : 'dependencies'} · route {model}{derivedModel ? '' : ' (default)'}</p>
              <DeliverEligibility eligibility={eligibility} />
            </>
          : <p className="deliver-muted">Load a PRD to preview its single next ranked candidate. Exactly one candidate is shown; blocked dependencies are never silently skipped.</p>}
      </section>

      {/* The disabled Deliver always states WHY in bound text — in EVERY disabled
          state, including the pre-load no-session / no-candidate ones, not only
          when a candidate is loaded (#6). */}
      {blockReason && <p id="deliver-block-reason" className="deliver-block-reason">{blockReason.text} <code>{blockReason.code}</code></p>}
      <div className="deliver-actions">
        {/* Cancel stays enabled while busy so a hung Deliver is never a trap (#4). */}
        <button type="button" className="secondary-button" onClick={dismiss}>Cancel</button>
        <button
          type="button"
          className="deliver-start"
          onClick={deliver}
          disabled={busy || Boolean(blockReason)}
          aria-disabled={busy || Boolean(blockReason) ? 'true' : undefined}
          aria-describedby={blockReason ? 'deliver-block-reason' : undefined}
        >{busy ? 'Delivering…' : candidate ? `Deliver ${candidate.title}` : 'Deliver'} <span aria-hidden="true">→</span></button>
      </div>
      <div className="deliver-live" role="status" aria-live="polite" aria-label="Deliver status">{announce}</div>
    </section>
  </div>
}

function DeliverEligibility({ eligibility }) {
  if (!eligibility || eligibility.status === 'idle') return <p className="deliver-muted">Delivery eligibility has not been checked yet.</p>
  if (eligibility.status === 'loading') return <p className="deliver-muted">Checking delivery eligibility…</p>
  if (eligibility.status === 'error') return <p className="explorer-degraded">{eligibility.message || 'Delivery eligibility is unavailable for this candidate.'}</p>
  const verdict = eligibility.value
  if (!verdict) return <p className="deliver-muted">No delivery eligibility verdict for this candidate.</p>
  return <div className="deliver-eligibility">
    <Status tone={verdict.eligible ? 'green' : 'amber'}>{verdict.state}</Status>
    <ul>{verdict.reasons.map((reason) => <li key={reason.code}><b>{reason.code}</b> — {reason.explanation}</li>)}</ul>
  </div>
}

function Onboarding({ data, onClose, setActive, onNewDelivery, onNewSession }) { const project = data.projects[0]; const steps = [{ label: 'Create a Workbench project', complete: Boolean(project), action: () => project ? setActive('Delivery') : onNewDelivery() }, { label: 'Register a project-local bridge', complete: Boolean(project?.bridge_id), action: () => setActive('Skills') }, { label: 'Publish and verify bridge skills', complete: Boolean(data.skills?.length), action: () => setActive('Skills') }, { label: 'Create a harness session', complete: Boolean(data.sessions?.length), action: () => onNewSession() }, { label: 'Run a State task through the bridge', complete: Boolean(data.runs?.length), action: () => setActive('Sessions') }, { label: 'Review evidence before an approval', complete: Boolean(data.runs?.some((run) => run.status === 'evidenced')), action: () => setActive('Evidence') }]; const current = steps.find((step) => !step.complete) || steps.at(-1); const go = () => { current.action(); onClose() }; return <Modal title="Workbench setup guide" onClose={onClose}><p>This guide reflects live hub state. It never marks a bridge, skill, run, or evidence step complete on its own.</p><ol className="onboarding-steps">{steps.map((step, index) => <li key={step.label} className={step.complete ? 'done' : ''}><span>{step.complete ? '✓' : index + 1}</span>{step.label}</li>)}</ol><button className="session-action" onClick={go}>{current.complete ? 'Review completed setup' : `Continue: ${current.label}`}</button></Modal> }

function Notifications({ audit, read, onRead }) { return <section className="notifications" aria-label="Notifications"><header><b>Recent hub activity</b><button onClick={onRead}>Mark viewed</button></header>{read ? <p>All current activity is marked viewed in this browser.</p> : audit.length ? audit.slice(0, 4).map((event) => <p key={event.id}><b>{event.kind}</b><br />{event.actor}</p>) : <p>No Workbench audit events yet.</p>}</section> }
function ProfileMenu({ data, onClose }) { return <section className="profile-menu" aria-label="Operator menu"><b>{data.actor || 'Allowlisted operator'}</b><span>Tailnet identity verified by the hub</span><small>Project creation and approvals are server-checked. The browser has no bridge, GitHub, or model credential.</small><button onClick={onClose}>Close menu</button></section> }

// --- Chat surface (chat-first-voice T004.2 / T004.3 / T004.4) ----------------
//
// The default surface. A conversation rail (management + search + active/archived
// distinction), a transcript with distinct empty/streaming/interrupted/error
// states, a multiline composer with documented keyboard submission, incremental
// cancellable streaming, retry/branch as visible successors, an Advanced mode
// that opens within Chat, and an allowlisted route selector.

const LIFECYCLE = {
  streaming: 'Streaming response…',
  complete: 'Response complete',
  cancelled: 'Response cancelled',
  interrupted: 'Response interrupted',
  failed: 'Response failed',
}

function turnText(turn) {
  return (turn.content || []).map((block) => block.text || '').join('')
}

function ConversationRow({ record, selected, onSelect, onRename, onArchive, onUnarchive, onDelete, renaming, onStartRename, onCancelRename, confirmingDelete, onRequestDelete, onCancelDelete }) {
  const info = describeConversation(record)
  const [draft, setDraft] = useState(info.title)
  const renameRef = useRef(null)
  useEffect(() => { setDraft(info.title) }, [renaming, info.title])
  // Move focus into the rename field the moment editing opens, and support
  // Escape-to-cancel so keyboard users are never trapped mid-edit (a11y #6).
  useEffect(() => { if (renaming) renameRef.current?.focus() }, [renaming])
  if (renaming) {
    return <li className={`conv-row ${info.archived ? 'is-archived' : 'is-active'}`}>
      <form className="conv-rename" onSubmit={(event) => { event.preventDefault(); if (draft.trim()) onRename(record.id, draft.trim()) }}>
        <input ref={renameRef} aria-label={`Rename ${info.title}`} value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => { if (event.key === 'Escape') { event.preventDefault(); onCancelRename() } }} />
        <button type="submit" disabled={!draft.trim()}>Save</button>
        <button type="button" onClick={onCancelRename}>Cancel</button>
      </form>
    </li>
  }
  return <li className={`conv-row ${info.archived ? 'is-archived' : 'is-active'} ${selected ? 'selected' : ''}`}>
    <button className="conv-open" aria-current={selected ? 'true' : undefined} aria-label={`Open ${info.title}`} onClick={() => onSelect(record.id)}>
      <span className="conv-title">{info.title}</span>
      <span className="conv-meta">
        <span className={`conv-state conv-state-${info.state}`}>{info.state}</span>
        {info.ephemeral && <span className="conv-badge">ephemeral</span>}
        {info.tags.map((tag) => <span key={tag} className="conv-tag">{tag}</span>)}
      </span>
      <small className="conv-id">{record.id}</small>
    </button>
    <div className="conv-actions">
      <button aria-label={`Rename ${info.title}`} onClick={() => onStartRename(record.id)}>Rename</button>
      {info.archived
        ? <button aria-label={`Unarchive ${info.title}`} onClick={() => onUnarchive(record.id)}>Unarchive</button>
        : <button aria-label={`Archive ${info.title}`} onClick={() => onArchive(record.id)}>Archive</button>}
      {/* Delete is a two-step confirm so a single stray keypress cannot destroy a
          conversation (a11y #7): the first press arms an explicit Confirm/Keep. */}
      {confirmingDelete
        ? <>
            <button className="conv-danger" aria-label={`Confirm delete ${info.title}`} onClick={() => onDelete(record.id)}>Confirm delete</button>
            <button aria-label={`Keep ${info.title}`} onClick={onCancelDelete}>Keep</button>
          </>
        : <button aria-label={`Delete ${info.title}`} onClick={() => onRequestDelete(record.id)}>Delete</button>}
    </div>
  </li>
}

function ConversationRail({ conversations, selectedId, includeArchived, query, renamingId, confirmingDeleteId, railRef, onSelect, onNew, onQueryChange, onToggleArchived, onRename, onArchive, onUnarchive, onDelete, onStartRename, onCancelRename, onRequestDelete, onCancelDelete }) {
  const active = conversations.filter((record) => record.status !== 'archived')
  const archived = conversations.filter((record) => record.status === 'archived')
  const rowProps = { selected: false, onSelect, onRename, onArchive, onUnarchive, onDelete, onStartRename, onCancelRename, onRequestDelete, onCancelDelete }
  const row = (record) => <ConversationRow key={record.id} record={record} {...rowProps} selected={selectedId === record.id} renaming={renamingId === record.id} confirmingDelete={confirmingDeleteId === record.id} />
  // Heading order (a11y #11): the rail is first in document order, so its label
  // is a non-heading element. The page <h1> in chat-main is the document's first
  // heading, keeping the rank sequence h1 → … without a rail <h2> preceding it.
  return <nav className="conv-rail" aria-label="Conversations" ref={railRef}>
    <div className="conv-rail-head"><p className="conv-rail-title">Conversations</p><button className="conv-new" aria-label="Start a new conversation" onClick={onNew}>+ New</button></div>
    <form className="conv-search" role="search" onSubmit={(event) => event.preventDefault()}>
      <input type="search" aria-label="Search conversations" placeholder="Search titles and messages" value={query} onChange={(event) => onQueryChange(event.target.value)} />
    </form>
    <label className="conv-filter"><input type="checkbox" checked={includeArchived} onChange={onToggleArchived} aria-label="Show archived conversations" /> Show archived</label>
    {conversations.length === 0
      ? <p className="conv-empty">{query.trim() ? 'No conversations match that search.' : 'No conversations yet. Start one to begin.'}</p>
      : <>
          <section aria-label="Active conversations"><p className="conv-section-title">Active</p><ul className="conv-list">{active.length ? active.map(row) : <li className="conv-none">No active conversations.</li>}</ul></section>
          {includeArchived && <section aria-label="Archived conversations"><p className="conv-section-title">Archived</p><ul className="conv-list">{archived.length ? archived.map(row) : <li className="conv-none">No archived conversations.</li>}</ul></section>}
        </>}
  </nav>
}

// Project / PRD / task titles as readable context when the conversation is bound
// to a delivery, with the canonical ids available only as secondary disclosure.
// The merged conversation projection does not yet emit this binding, so the panel
// renders defensively from an optional `context` block and shows a truthful
// unlinked state otherwise.
function DeliveryContext({ context }) {
  const rows = context ? [['Project', context.project], ['PRD', context.prd], ['Task', context.task]].filter(([, value]) => value) : []
  if (!rows.length) return <p className="chat-context none">No linked delivery context.</p>
  return <section className="chat-context" aria-label="Linked delivery context">
    {rows.map(([label, value]) => <div key={label} className="context-row">
      <span className="context-label">{label}</span>
      <b className="context-title">{value.title}</b>
      {value.id && <details className="context-id"><summary aria-label={`${label} id`}>id</summary><code>{value.id}</code></details>}
    </div>)}
  </section>
}

function AdvancedPanel() {
  return <section className="advanced-panel" aria-label="Advanced controls">
    <p>Advanced controls are not configured in this build. The transcript and its route are unchanged.</p>
    <label>Reasoning effort<select aria-label="Reasoning effort" disabled><option>not configured</option></select></label>
    <label>Temperature<input aria-label="Temperature" type="range" min="0" max="2" step="0.1" disabled /></label>
  </section>
}

function TurnView({ turn, onRetry, onBranch, streamActive }) {
  const text = turnText(turn)
  const streaming = turn.status === 'streaming'
  const distinct = !streaming && turn.status !== 'complete'
  const lineage = turn.lineage?.kind && turn.lineage.kind !== 'initial' ? turn.lineage.kind : null
  return <li className={`turn turn-${turn.role} turn-${turn.status}`}>
    <div className="turn-head">
      <span className="turn-role">{turn.role === 'user' ? 'You' : 'Assistant'}</span>
      {lineage && <span className="turn-lineage">{lineage}</span>}
      {streaming && <span className="turn-status streaming">streaming…</span>}
      {distinct && <span className={`turn-status turn-status-${turn.status}`}>{turn.status}</span>}
    </div>
    <p className="turn-text">{text || (streaming ? '…' : '')}</p>
    {/* Successor actions are disabled while a stream is in flight (a11y #12): a
        retry/branch cannot be issued against history that is still settling. */}
    {turn.role === 'assistant' && !streaming && <div className="turn-actions">
      <button aria-label="Retry this response" disabled={streamActive} onClick={() => onRetry(turn)}>Retry</button>
      <button aria-label="Branch from this response" disabled={streamActive} onClick={() => onBranch(turn)}>Branch</button>
    </div>}
  </li>
}

function Transcript({ selected, turns, streamingTurn, onRetry, onBranch }) {
  if (!selected) return <div className="chat-empty" role="region" aria-label="No conversation selected"><h2>Select or start a conversation</h2><p>Your conversations are private to you and stay on the tailnet.</p></div>
  const rendered = streamingTurn ? [...turns, streamingTurn] : turns
  if (rendered.length === 0) return <div className="chat-empty" role="region" aria-label="Empty conversation"><h2>No messages yet</h2><p>Send the first message to start this conversation.</p></div>
  const streamActive = Boolean(streamingTurn)
  return <ol className="transcript" aria-label="Transcript">{rendered.map((turn) => <TurnView key={turn.id} turn={turn} onRetry={onRetry} onBranch={onBranch} streamActive={streamActive} />)}</ol>
}

function Composer({ draft, setDraft, onSend, onCancel, streaming, disabled, canSend }) {
  const textareaRef = useRef(null)
  const cancelRef = useRef(null)
  const wasStreamingRef = useRef(false)
  const onKeyDown = (event) => {
    // Do not submit on an IME commit Enter (a11y #8): while composing CJK/other
    // input, the terminal Enter that accepts a candidate reports isComposing
    // (keyCode 229 on older engines) and must insert/commit, never send.
    if (event.isComposing || event.keyCode === 229) return
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); if (canSend) onSend() }
  }
  // Focus follows the Send↔Cancel swap (a11y #6): when a stream starts, move
  // focus to Cancel so it is never dropped to <body>; when the stream settles,
  // return focus to the composer so the next message can be typed immediately.
  useEffect(() => {
    if (streaming) cancelRef.current?.focus()
    else if (wasStreamingRef.current) textareaRef.current?.focus()
    wasStreamingRef.current = streaming
  }, [streaming])
  return <form className="chat-composer" onSubmit={(event) => { event.preventDefault(); if (canSend) onSend() }}>
    <textarea ref={textareaRef} aria-label="Message composer" aria-describedby="composer-hint" rows="3" value={draft} disabled={disabled}
      onChange={(event) => setDraft(event.target.value)} onKeyDown={onKeyDown}
      placeholder={disabled ? 'Select or start a conversation to send a message…' : 'Message…  (Enter to send, Shift+Enter for a new line)'} />
    <div className="composer-bar">
      <small id="composer-hint">Enter sends. Shift+Enter inserts a new line.</small>
      {streaming
        ? <button ref={cancelRef} type="button" className="composer-cancel" aria-label="Cancel streaming response" onClick={onCancel}>Cancel</button>
        : <button type="submit" aria-label="Send message" disabled={!canSend}>Send <span aria-hidden="true">↵</span></button>}
    </div>
  </form>
}

function RouteSelect({ routes, routeId, onChange }) {
  return <label className="chat-route"><span>Route</span>
    <select aria-label="Chat route" value={routeId || ''} onChange={(event) => onChange(event.target.value)} disabled={routes.length === 0}>
      {routes.length === 0 && <option value="">No routes configured</option>}
      {routes.map((route) => <option key={route.route_id} value={route.route_id}>{route.display_name || route.route_id}</option>)}
    </select>
  </label>
}

function ChatView({ append }) {
  const [conversations, setConversations] = useState([])
  const [includeArchived, setIncludeArchived] = useState(false)
  const [query, setQuery] = useState('')
  const [selectedId, setSelectedId] = useState(null)
  const [turns, setTurns] = useState([])
  const [routes, setRoutes] = useState([])
  const [routeId, setRouteId] = useState('')
  const [advanced, setAdvanced] = useState(false)
  const [draft, setDraft] = useState('')
  const [streamingTurn, setStreamingTurn] = useState(null)
  const [lifecycle, setLifecycle] = useState('')
  const [renamingId, setRenamingId] = useState(null)
  const [confirmingDeleteId, setConfirmingDeleteId] = useState(null)
  const abortRef = useRef(null)
  const seqRef = useRef(0)
  // Latest-wins guard for the conversation list/search (a11y #9): every fetch
  // claims a monotonic ticket; a resolved fetch applies its result only if it is
  // still the newest, so a slow earlier query can never overwrite a newer one.
  const listSeqRef = useRef(0)
  const listAbortRef = useRef(null)
  const railRef = useRef(null)
  // Mirror of the selected id readable from an async settle without a stale
  // closure (a11y #2): an in-flight send captured its conversation id and
  // compares it against this on settle to drop a result for a switched-away
  // conversation instead of mutating the now-current one.
  const selectedIdRef = useRef(null)

  const selected = conversations.find((record) => record.id === selectedId) || null

  const refreshList = async (nextQuery, nextArchived, signal) => {
    const seq = (listSeqRef.current += 1)
    try {
      const value = nextQuery && nextQuery.trim()
        ? await searchConversations(nextQuery.trim(), { includeArchived: nextArchived, signal })
        : await listConversations({ includeArchived: nextArchived, signal })
      if (seq !== listSeqRef.current) return // a newer list/search superseded this one
      setConversations(value.conversations || [])
    } catch (error) {
      if (signal?.aborted || error?.name === 'AbortError') return // superseded fetch aborted
      if (seq === listSeqRef.current) append('Conversations are unavailable. No conversation content left the tailnet.')
    }
  }
  // List/search fetch with debounce + abort (a11y #9): each run aborts the prior
  // in-flight request, then a non-empty search waits ~150ms so a fast typist
  // makes one request per pause; the plain list and the archived toggle refresh
  // immediately. The latest-wins guard in refreshList is the final backstop.
  useEffect(() => {
    const controller = new AbortController()
    listAbortRef.current?.abort()
    listAbortRef.current = controller
    if (!query.trim()) { refreshList(query, includeArchived, controller.signal); return undefined }
    const handle = setTimeout(() => { refreshList(query, includeArchived, controller.signal) }, 150)
    return () => { clearTimeout(handle) }
  }, [query, includeArchived])
  useEffect(() => {
    fetchChatRoutes()
      .then((value) => { const list = value.routes || []; setRoutes(list); setRouteId((current) => current || list[0]?.route_id || '') })
      .catch(() => setRoutes([]))
  }, [])

  // Focus a sensible target after a row leaves the rail (a11y #6): the first
  // remaining conversation, else the "New" affordance — never <body>.
  const focusRail = () => {
    const rail = railRef.current
    if (!rail) return
    const target = rail.querySelector('.conv-open') || rail.querySelector('.conv-new')
    target?.focus()
  }

  const select = async (id) => {
    // Switching conversations aborts any in-flight stream so its settled answer
    // cannot land in — or announce for — the newly selected conversation (#2).
    abortRef.current?.abort()
    selectedIdRef.current = id
    setSelectedId(id); setStreamingTurn(null); setLifecycle(''); setRenamingId(null); setConfirmingDeleteId(null)
    try { const value = await getConversation(id); if (selectedIdRef.current === id) setTurns(value.turns || []) }
    catch { if (selectedIdRef.current === id) { setTurns([]); append('That conversation could not be opened.') } }
  }
  const newConversation = async () => {
    try { const record = await createConversation({}); setConversations((current) => [record, ...current]); await select(record.id) }
    catch { append('A new conversation could not be created.') }
  }
  const rename = async (id, title) => {
    setRenamingId(null)
    try { const record = await renameConversation(id, title); setConversations((current) => current.map((item) => (item.id === id ? record : item))) }
    catch { append('The conversation could not be renamed.') }
  }
  const archive = async (id) => { try { await archiveConversation(id); await refreshList(query, includeArchived); focusRail() } catch { append('The conversation could not be archived.') } }
  const unarchive = async (id) => { try { await unarchiveConversation(id); await refreshList(query, includeArchived); focusRail() } catch { append('The conversation could not be unarchived.') } }
  const remove = async (id) => {
    setConfirmingDeleteId(null)
    try {
      await deleteConversation(id)
      if (selectedId === id) { abortRef.current?.abort(); selectedIdRef.current = null; setSelectedId(null); setTurns([]); setStreamingTurn(null); setLifecycle('') }
      await refreshList(query, includeArchived); focusRail()
    }
    catch { append('The conversation could not be deleted.') }
  }

  const send = async () => {
    const prompt = draft.trim()
    if (!prompt || !selectedId || !routeId || streamingTurn) return
    try { selectChatRoute(routes, routeId) } catch { append('That route is not in the reviewed allowlist.'); return }
    // Bind this send to the conversation it started in; a settle for a
    // switched-away conversation is dropped rather than mutating the current one.
    const conversationId = selectedId
    const isCurrent = () => selectedIdRef.current === conversationId
    const ordinal = (seqRef.current += 1)
    const userTurn = { id: `local-user-${ordinal}`, role: 'user', status: 'complete', content: [{ text: prompt }], lineage: { kind: 'initial' } }
    setTurns((current) => [...current, userTurn]); setDraft('')
    const assistant = { id: `local-assistant-${ordinal}`, role: 'assistant', status: 'streaming', content: [{ text: '' }], lineage: { kind: 'initial' } }
    setStreamingTurn(assistant); setLifecycle(LIFECYCLE.streaming)
    const controller = new AbortController(); abortRef.current = controller
    try {
      const state = await sendMessage({
        conversationId, routeId, prompt, signal: controller.signal,
        onState: (streamState) => { if (!isCurrent()) return; setStreamingTurn((current) => (current ? {
          ...current, content: [{ text: streamState.text }],
          status: streamState.terminal ? terminalToStatus(streamState.terminal) : 'streaming',
        } : current)) },
      })
      if (!isCurrent()) return // switched away mid-stream: drop this settle entirely
      const status = terminalToStatus(state.terminal)
      setTurns((current) => [...current, { ...assistant, content: [{ text: state.text }], status }])
      setStreamingTurn(null)
      setLifecycle(LIFECYCLE[status] || LIFECYCLE.complete)
      if (state.needsRefresh) {
        try { const value = await getConversation(conversationId); if (isCurrent()) setTurns(value.turns || []) }
        catch { if (isCurrent()) append('The reconnected transcript could not be refreshed.') }
      }
    } catch {
      if (!isCurrent()) return
      setStreamingTurn(null)
      setTurns((current) => [...current, { ...assistant, status: 'failed', content: [{ text: '' }] }])
      setLifecycle(LIFECYCLE.failed)
      append('The response failed. No partial answer was recorded as complete.')
    } finally {
      // Only clear the ref if this send still owns it — a newer stream may have
      // taken it while this one was settling (#2), and must not be wiped.
      if (abortRef.current === controller) abortRef.current = null
    }
  }
  const cancel = () => { abortRef.current?.abort() }

  // Retry/branch post ONLY the `{kind:'text', text}` slice the server accepts and
  // pick the role the server's turn tree expects (#1): retry appends a sibling
  // ASSISTANT regeneration; branch opens a follow-up USER turn. Reposting a
  // server-loaded block verbatim carried `content_trust` and was rejected 422.
  const addSuccessor = async (call, turn, role, fallbackKind, failure) => {
    try {
      const created = await call(selectedId, turn.id, successorTurnBody(turn, { role, mode: advanced ? 'advanced' : 'ordinary' }))
      setTurns((current) => [...current, { ...created, lineage: created.lineage || { kind: fallbackKind } }])
      setLifecycle(fallbackKind === 'retry' ? 'Added a retry response' : 'Added a branch response')
    } catch { append(failure) }
  }
  const retry = (turn) => addSuccessor(retryTurn, turn, 'assistant', 'retry', 'Retry could not be recorded.')
  const branch = (turn) => addSuccessor(branchTurn, turn, 'user', 'branch', 'Branch could not be recorded.')

  const streaming = Boolean(streamingTurn)
  const canSend = Boolean(!streaming && draft.trim() && selectedId && routeId)
  return <main className="chat">
    <ConversationRail
      conversations={conversations} selectedId={selectedId} includeArchived={includeArchived} query={query} renamingId={renamingId}
      confirmingDeleteId={confirmingDeleteId} railRef={railRef}
      onSelect={select} onNew={newConversation} onQueryChange={setQuery} onToggleArchived={() => setIncludeArchived((value) => !value)}
      onRename={rename} onArchive={archive} onUnarchive={unarchive} onDelete={remove}
      onStartRename={(id) => { setConfirmingDeleteId(null); setRenamingId(id) }} onCancelRename={() => setRenamingId(null)}
      onRequestDelete={(id) => { setRenamingId(null); setConfirmingDeleteId(id) }} onCancelDelete={() => setConfirmingDeleteId(null)} />
    <section className="chat-main" aria-label="Conversation">
      <header className="chat-header">
        <div><span className="crumb">Chat / private</span><h1>{selected ? describeConversation(selected).title : 'Chat'}</h1></div>
        <div className="chat-controls">
          <RouteSelect routes={routes} routeId={routeId} onChange={setRouteId} />
          <button className={`advanced-toggle ${advanced ? 'on' : ''}`} aria-pressed={advanced} aria-label="Toggle Advanced mode" onClick={() => setAdvanced((value) => !value)}>Advanced</button>
        </div>
      </header>
      <DeliveryContext context={selected?.context} />
      {advanced && <AdvancedPanel />}
      <div className="transcript-scroll"><Transcript selected={selected} turns={turns} streamingTurn={streamingTurn} onRetry={retry} onBranch={branch} /></div>
      <div className="chat-live" role="status" aria-live="polite">{lifecycle}</div>
      <Composer draft={draft} setDraft={setDraft} onSend={send} onCancel={cancel} streaming={streaming} disabled={!selectedId} canSend={canSend} />
    </section>
  </main>
}

// --- Delivery explorer (plan-task-delivery T003) -----------------------------
//
// A read-only Project → PRD → plan → task explorer over the merged
// delivery-projection GET surface (workbench/api.py build_delivery_projection_router).
// Titles and lineage are the primary visible hierarchy; the scoped id
// `<prd_id>:<task_id>` disambiguates two PRDs' `T001` in navigation AND in the
// URL hash / selection state (R004 / criterion 2). The merged router serves
// per-(project, prd) reads ONLY — there is no served PRD/project enumeration —
// so the explorer lists Projects from bootstrap and opens a PRD by its id,
// rendering a truthful note about the missing enumeration rather than
// fabricating a PRD list. Every load failure (including a 503 when the
// projection is not configured) renders a truthful degraded state.

function ExplorerEligibility({ eligibility }) {
  if (!eligibility || eligibility.status === 'idle' || eligibility.status === 'loading') {
    return <p className="explorer-muted">{eligibility?.status === 'loading' ? 'Checking eligibility…' : 'No eligibility loaded.'}</p>
  }
  if (eligibility.status === 'error') {
    return <p className="explorer-degraded">{eligibility.message || 'Eligibility is unavailable for this task.'}</p>
  }
  const verdict = eligibility.value
  if (!verdict) return <p className="explorer-muted">No eligibility verdict for this task.</p>
  return <div className="explorer-eligibility">
    <Status tone={verdict.eligible ? 'green' : 'amber'}>{verdict.state}</Status>
    <ul>{verdict.reasons.map((reason) => <li key={reason.code}><b>{reason.code}</b> — {reason.explanation}</li>)}</ul>
  </div>
}

function ExplorerTaskRow({ task, selected, onOpen }) {
  // No aria-label here (a11y #1): a button role makes its children presentational
  // for name computation, so an `aria-label` of just the scoped id would REPLACE
  // the visible content and a screen-reader user would hear only the bare id —
  // inverting criterion 1 (title/lineage lead, scoped id is secondary). Letting
  // the accessible name come from content keeps title, State status, delivery
  // status, and scoped id all in the announced name, title-first.
  return <li className={`explorer-task ${selected ? 'selected' : ''}`}>
    <button data-explorer-task={task.scopedId} aria-current={selected ? 'true' : undefined} onClick={() => onOpen(task)}>
      <span className="explorer-task-title">{task.title}</span>
      <span className="explorer-task-meta">
        <Status tone={tone(task.status)}>{task.status}</Status>
        <span className="explorer-pill delivery">{task.latestDeliveryStatus}</span>
      </span>
      <small className="explorer-scoped">{task.scopedId}</small>
    </button>
  </li>
}

function ExplorerPrdCard({ entry, filter, selectedScopedId, onReadPrd, onOpenTask }) {
  const described = (entry.tasks || []).map(describeTaskReference)
  const filtered = filterDescribedTasks(described, filter)
  const heading = entry.prd ? entry.prd.title : entry.prdId
  return <article className="explorer-prd" aria-label={`PRD ${heading}`}>
    <header className="explorer-prd-head">
      <div>
        <h2 className="explorer-prd-title">{heading}</h2>
        <p className="explorer-prd-lineage">{entry.projectName} / {heading}</p>
      </div>
      {entry.prd ? <Status tone={tone(entry.prd.status)}>{entry.prd.status}</Status> : entry.prdError ? <Status tone="amber">unavailable</Status> : null}
    </header>
    {entry.prd && <>
      <dl className="explorer-prd-meta">
        <div><dt>Release</dt><dd>{entry.prd.release || '—'}</dd></div>
        <div><dt>Revision</dt><dd>r{entry.prd.revision ?? '—'}</dd></div>
        <div><dt>Freshness</dt><dd>{freshnessLabel(entry.prd.generatedAt)}</dd></div>
        <div><dt>Progress</dt><dd>{progressSummaryLabel(entry.tasks)}</dd></div>
      </dl>
      <button className="explorer-read" onClick={() => onReadPrd(entry)}>Read PRD content</button>
    </>}
    {entry.prdError && <p className="explorer-degraded">{entry.prdError}</p>}
    {entry.tasksError
      ? <p className="explorer-degraded">{entry.tasksError}</p>
      : <ul className="explorer-task-list" aria-label={`Tasks in ${heading}`}>
          {filtered.length
            ? filtered.map((task) => <ExplorerTaskRow key={task.scopedId} task={task} selected={selectedScopedId === task.scopedId} onOpen={(chosen) => onOpenTask(entry, chosen)} />)
            : <li className="explorer-none">{described.length ? 'No tasks match that filter.' : 'No tasks in this PRD projection.'}</li>}
        </ul>}
  </article>
}

function ExplorerDetail({ selection, reading, eligibility, detailRef, onClose }) {
  if (selection) {
    const task = selection.task
    const lineage = [task.prdTitle, task.featureId, task.scopedId].filter(Boolean).join(' / ')
    // Region name is title-first (a11y #10): `<title> (<scoped id>)` keeps the
    // human title as the primary label and the scoped id as disambiguating
    // secondary disclosure, matching criterion 1.
    return <section className="explorer-detail-pane" aria-label={`${task.title} (${task.scopedId})`} data-scoped-id={task.scopedId}>
      <div className="explorer-detail-topbar"><span className="crumb">{lineage}</span><button className="explorer-close" aria-label="Close" onClick={onClose}>Close</button></div>
      <h2 className="explorer-detail-title" tabIndex={-1} ref={detailRef}>{task.title}</h2>
      <div className="explorer-detail-badges">
        <Status tone={tone(task.status)}>{task.status}</Status>
        {task.priority && <span className="explorer-pill">{task.priority}</span>}
        <span className="explorer-pill delivery">delivery: {task.latestDeliveryStatus}</span>
      </div>
      <details className="explorer-ids"><summary>scoped identity</summary>
        <dl>
          <div><dt>Scoped id</dt><dd>{task.scopedId}</dd></div>
          <div><dt>Run label</dt><dd>{task.runLabel || '—'}</dd></div>
          <div><dt>PRD revision</dt><dd>r{task.prdRevision ?? '—'}</dd></div>
        </dl>
      </details>
      <section aria-label="Dependencies" className="explorer-detail-block">
        <h3>Dependencies</h3>
        {task.dependsOn.length
          ? <ul className="explorer-deps">{task.dependsOn.map((dep) => <li key={dep.scopedId}>{dep.scopedId}{dep.prdRevision != null ? ` @r${dep.prdRevision}` : ''}</li>)}</ul>
          : <p className="explorer-muted">No dependencies.</p>}
      </section>
      <section aria-label="Acceptance criteria" className="explorer-detail-block">
        <h3>Acceptance criteria</h3>
        <p>{task.acceptanceCriteriaCount} acceptance {task.acceptanceCriteriaCount === 1 ? 'criterion' : 'criteria'}</p>
      </section>
      <section aria-label="Verification" className="explorer-detail-block">
        <h3>Verification</h3>
        <p>{task.verificationSummary || 'No verification summary in this projection.'}</p>
      </section>
      <section aria-label="Delivery eligibility" className="explorer-detail-block">
        <h3>Delivery eligibility</h3>
        <ExplorerEligibility eligibility={eligibility} />
      </section>
    </section>
  }
  if (reading) {
    const prd = reading.prd
    return <section className="explorer-detail-pane" aria-label={`PRD ${prd ? prd.title : reading.prdId}`}>
      <div className="explorer-detail-topbar"><span className="crumb">PRD content / {prd?.redactionStatus || 'redacted'}</span><button className="explorer-close" aria-label="Close" onClick={onClose}>Close</button></div>
      <h2 className="explorer-detail-title" tabIndex={-1} ref={detailRef}>{prd ? prd.title : reading.prdId}</h2>
      {prd ? <>
        <dl className="explorer-prd-meta">
          <div><dt>Release</dt><dd>{prd.release || '—'}</dd></div>
          <div><dt>Revision</dt><dd>r{prd.revision ?? '—'}</dd></div>
          <div><dt>State status</dt><dd>{prd.status}</dd></div>
          <div><dt>Freshness</dt><dd>{freshnessLabel(prd.generatedAt)}</dd></div>
        </dl>
        {prd.truncated && <p className="explorer-muted">Showing a redacted, truncated projection{prd.totalBytes != null ? ` (${prd.totalBytes} bytes total)` : ''}.</p>}
        <pre className="explorer-prd-body">{prd.body || 'No PRD body in this projection.'}</pre>
      </> : <p className="explorer-degraded">{reading.prdError || 'PRD content is unavailable.'}</p>}
    </section>
  }
  return <section className="explorer-detail-pane empty" aria-label="Nothing selected">
    <h2 className="explorer-detail-title">Open a PRD, then a task</h2>
    <p className="explorer-muted">Select a project and open a PRD by its id to read its content and browse its plan and tasks. Titles and lineage lead; the scoped id keeps two PRDs' tasks distinct.</p>
  </section>
}

function ExplorerView({ data, append }) {
  const projects = data.projects || []
  const [selectedProjectId, setSelectedProjectId] = useState(projects[0]?.id || '')
  const [prdInput, setPrdInput] = useState('')
  const [entries, setEntries] = useState([])
  const [filter, setFilter] = useState('')
  const [selection, setSelection] = useState(null)
  const [reading, setReading] = useState(null)
  const [eligibility, setEligibility] = useState({ status: 'idle', value: null, message: null })
  const [announce, setAnnounce] = useState('')
  const detailRef = useRef(null)
  const railRef = useRef(null)
  // Latest-wins guard for the per-task eligibility fetch (a11y + suite #2),
  // mirroring the chat rail's listSeqRef: every fetch claims a monotonic ticket
  // and applies its result only if it is still the newest. Without it, opening
  // alpha:T001 (slow) then beta:T001 (fast) lets alpha's late resolve overwrite
  // beta's verdict — showing the WRONG eligibility under beta, the exact
  // T001-vs-T001 confusion this explorer exists to prevent. closeDetail also
  // bumps it so a stale in-flight fetch cannot repaint a closed pane.
  const eligibilitySeqRef = useRef(0)

  useEffect(() => { if (!selectedProjectId && projects[0]) setSelectedProjectId(projects[0].id) }, [projects, selectedProjectId])
  // Move focus to the opened detail heading so a view switch / detail open never
  // drops focus to <body> (a11y focus management). tabIndex=-1 makes the h2 a
  // programmatic focus target without adding it to the tab order.
  useEffect(() => { if (selection || reading) detailRef.current?.focus() }, [selection?.scopedId, reading?.key])

  const loadEligibility = async (entry, task) => {
    const seq = (eligibilitySeqRef.current += 1)
    setEligibility({ status: 'loading', value: null, message: null })
    try {
      const value = await fetchTaskEligibility(entry.projectId, entry.prdId, task.taskId)
      if (seq !== eligibilitySeqRef.current) return // a newer task open superseded this fetch
      setEligibility({ status: 'loaded', value: describeEligibility(value.eligibility), message: null })
    } catch (error) {
      if (seq !== eligibilitySeqRef.current) return // superseded fetch failed late; do not repaint
      setEligibility({ status: 'error', value: null, message: error.message })
    }
  }

  const openPrd = async () => {
    const project = projects.find((item) => item.id === selectedProjectId)
    const prdId = prdInput.trim()
    if (!project || !prdId) return
    const key = `${project.id}::${prdId}`
    setAnnounce(`Loading PRD ${prdId}…`)
    const [contentResult, tasksResult] = await Promise.allSettled([
      fetchPrdContent(project.id, prdId),
      fetchPrdTasks(project.id, prdId),
    ])
    const entry = {
      key, projectId: project.id, projectName: project.name, prdId,
      prd: contentResult.status === 'fulfilled' ? describePrdContent(contentResult.value.content) : null,
      prdError: contentResult.status === 'rejected' ? contentResult.reason.message : null,
      tasks: tasksResult.status === 'fulfilled' ? (tasksResult.value.tasks || []) : [],
      tasksError: tasksResult.status === 'rejected' ? tasksResult.reason.message : null,
    }
    setEntries((current) => [entry, ...current.filter((item) => item.key !== key)])
    setPrdInput('')
    // Announce the truthful outcome (a11y #6): when the PRD content loads but the
    // task projection fails, say so — never "…with 0 tasks", which reads as an
    // empty-but-healthy PRD and hides the failure (which is otherwise visual-only).
    if (entry.prd) {
      setAnnounce(entry.tasksError
        ? `Loaded PRD ${entry.prd.title}; tasks failed to load`
        : `Loaded PRD ${entry.prd.title} with ${entry.tasks.length} tasks`)
    } else { setAnnounce(entry.prdError || `PRD ${prdId} could not be loaded`); append?.(entry.prdError || `PRD ${prdId} could not be loaded`) }
  }

  const readPrd = (entry) => {
    setSelection(null)
    setReading(entry)
    setAnnounce(`Reading PRD ${entry.prd ? entry.prd.title : entry.prdId}`)
  }

  const openTask = (entry, task) => {
    setReading(null)
    setSelection({ scopedId: task.scopedId, entry, task })
    // Reflect the scoped identity in the URL so two PRDs' T001 are distinct in
    // state AND in the address bar (criterion 2), not merely on screen.
    if (task.scopedId) window.location.hash = `explorer/${task.scopedId}`
    setAnnounce(`Opened task ${task.scopedId}: ${task.title}`)
    loadEligibility(entry, task)
  }

  const closeDetail = () => {
    const invokedScopedId = selection?.scopedId || null
    const had = selection || reading
    setSelection(null)
    setReading(null)
    setEligibility({ status: 'idle', value: null, message: null })
    eligibilitySeqRef.current += 1 // drop any in-flight eligibility fetch (a11y #2)
    // Clear the write-only URL hash so it stops lying once the pane is closed
    // (a11y + suite #4): the hash reflects an OPEN task, so a closed detail must
    // not leave a stale `#explorer/<scoped id>` in the address bar.
    if (window.location.hash.startsWith('#explorer/')) window.location.hash = ''
    if (had) {
      setAnnounce('Closed detail')
      // Return focus to the invoking task row (a11y #9), not the first project
      // button in document order — a keyboard user deep in a second PRD is not
      // teleported to the top. Fall back to the first focusable rail control.
      const invoker = invokedScopedId && railRef.current?.querySelector(`[data-explorer-task="${invokedScopedId}"]`)
      const target = invoker || railRef.current?.querySelector('.explorer-open-prd, input, button')
      target?.focus()
    }
  }

  // Document-level Escape closes the detail while it is open (a11y #3), so it
  // works even after focus has left the pane (in PRD-read mode the pane has no
  // tabbable elements, so one Tab exits and a pane-scoped handler would go dead).
  useEffect(() => {
    if (!selection && !reading) return undefined
    const onDocKeyDown = (event) => { if (event.key === 'Escape') closeDetail() }
    document.addEventListener('keydown', onDocKeyDown)
    return () => document.removeEventListener('keydown', onDocKeyDown)
  }, [selection, reading])

  // Clear the URL hash when the Explorer view unmounts (a11y + suite #4): leaving
  // the Explorer route must not leave a stale `#explorer/<scoped id>` behind.
  useEffect(() => () => { if (window.location.hash.startsWith('#explorer/')) window.location.hash = '' }, [])

  // Announce the filter result in the live region on filter change (a11y #7):
  // filtering is named in the criterion-4 SR smoke but otherwise gives a
  // screen-reader user no feedback. An empty filter makes no announcement (it is
  // the resting state, and openPrd already owns the load announcement).
  useEffect(() => {
    const needle = filter.trim()
    if (!needle) return
    const total = entries.reduce(
      (sum, entry) => sum + filterDescribedTasks((entry.tasks || []).map(describeTaskReference), filter).length,
      0,
    )
    setAnnounce(total ? `Filter shows ${total} task${total === 1 ? '' : 's'}` : `No tasks match "${needle}"`)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- fire on filter edits only
  }, [filter])

  return <main className="explorer" aria-label="Delivery explorer">
    <aside className="explorer-rail" ref={railRef}>
      <span className="crumb">Explorer / PRD → plan → task</span>
      <h1>Delivery explorer</h1>
      <p className="explorer-intro">Read-only Project → PRD → plan → task lineage from the redacted delivery projection.</p>
      <section aria-label="Projects" className="explorer-projects">
        <p className="explorer-section-title">Projects</p>
        {projects.length
          ? <div className="explorer-project-list">{projects.map((project) => <button key={project.id} className={`explorer-project ${selectedProjectId === project.id ? 'selected' : ''}`} aria-pressed={selectedProjectId === project.id} aria-label={`Select project ${project.name}`} onClick={() => setSelectedProjectId(project.id)}><b>{project.name}</b><small>{project.id}</small></button>)}</div>
          : <p className="explorer-muted">No projects yet. Create one from Delivery.</p>}
      </section>
      <form className="explorer-open" onSubmit={(event) => { event.preventDefault(); openPrd() }}>
        <label>Open a PRD by id<input value={prdInput} onChange={(event) => setPrdInput(event.target.value)} placeholder="e.g. release-alpha" disabled={!selectedProjectId} /></label>
        <button className="explorer-open-prd" type="submit" disabled={!selectedProjectId || !prdInput.trim()}>Open PRD</button>
        <small className="explorer-muted">PRD enumeration is not served by the projection; open a PRD by its id.</small>
      </form>
      <label className="explorer-filter">Filter tasks<input type="search" aria-label="Filter tasks" value={filter} onChange={(event) => setFilter(event.target.value)} placeholder="title, scoped id, or status" /></label>
      <section aria-label="Loaded PRDs" className="explorer-prds">
        {entries.length
          ? entries.map((entry) => <ExplorerPrdCard key={entry.key} entry={entry} filter={filter} selectedScopedId={selection?.scopedId || null} onReadPrd={readPrd} onOpenTask={openTask} />)
          : <p className="explorer-empty">No PRD opened yet. Choose a project and open a PRD by id.</p>}
      </section>
    </aside>
    <ExplorerDetail selection={selection} reading={reading} eligibility={eligibility} detailRef={detailRef} onClose={closeDetail} />
    <div className="explorer-live" role="status" aria-live="polite">{announce}</div>
  </main>
}

function App() {
  const [active, setActive] = useState('Chat'); const [data, setData] = useState(emptyData); const [notice, setNotice] = useState(''); const [selectedApprovalId, setSelectedApprovalId] = useState(null); const [newDeliveryOpen, setNewDeliveryOpen] = useState(false); const [newSessionOpen, setNewSessionOpen] = useState(false); const [startSession, setStartSession] = useState(null); const [guideOpen, setGuideOpen] = useState(false); const [profileOpen, setProfileOpen] = useState(false); const [notificationsOpen, setNotificationsOpen] = useState(false); const [notificationsRead, setNotificationsRead] = useState(false); const [deliverOpen, setDeliverOpen] = useState(false)
  const load = async () => { const value = await bootstrap(); setData({ ...emptyData, ...value, sandbox: { ...emptyData.sandbox, ...(value.sandbox || {}) }, voice: { ...emptyData.voice, ...(value.voice || {}) } }); return value }
  useEffect(() => { load().catch(() => setNotice('Workbench hub is unavailable; no local mock delivery is shown.')) }, [])
  const createDelivery = async (payload) => { try { const project = await createProject(payload); setData((current) => ({ ...current, projects: [project, ...current.projects] })); setNewDeliveryOpen(false); setNotice(`Created ${project.name}. Register its bridge locally before starting a run.`); await load() } catch { setNotice('Project could not be created. No bridge or run was started.') } }
  const createConcurrentSession = async (payload) => { try { const created = await createSession(payload); setData((current) => ({ ...current, sessions: [created.session, ...current.sessions], workflows: [created.workflow, ...current.workflows] })); setNewSessionOpen(false); setActive('Sessions'); setNotice(`Created ${created.session.title}. Start it only after its named worktree is configured on the bridge.`); await load() } catch { setNotice('Session could not be created. No bridge run was started.') } }
  const startConcurrentSession = async (workflowId, payload) => { try { const result = await startWorkflow(workflowId, payload); setStartSession(null); setActive('Delivery'); setNotice(`Started ${payload.task_id} through the local bridge. The workflow is traceable in this session.`); setData((current) => ({ ...current, runs: [result.run, ...current.runs] })); await load() } catch { setNotice('Workflow did not start. Check the project bridge, published skills, and named worktree configuration.') } }
  // The one-activation Deliver: start the ranked candidate through the REAL wired
  // POST /api/workflows/{id}/start, then route to the resulting run. Throws on
  // failure so the sheet keeps itself open and announces a truthful error (it
  // does NOT close or fabricate a started run).
  const deliverCandidate = async (workflowId, payload, signal) => {
    const result = await startWorkflow(workflowId, payload, { signal })
    setDeliverOpen(false); setActive('Delivery')
    setData((current) => ({ ...current, runs: [result.run, ...current.runs] }))
    setNotice(`Delivering ${payload.task_id} through the local bridge. Its run is ${result.run.id}.`)
    await load()
    return result.run
  }
  const addDirection = async (sessionId, text) => { const result = await addDirective(sessionId, text); if (result.recorded && result.event) { setData((current) => ({ ...current, directives: [...current.directives, result.event] })); setNotice('Direction recorded. It will be included only in the next bridge work packet for this session.') } else { setNotice(`Direction was not recorded (${result.outcome}). No future work packet was changed.`) } await load() }
  const context = useMemo(() => active === 'Delivery' ? 'Delivery cockpit' : `${active} view`, [active])
  const selectApproval = (approvalId) => { setSelectedApprovalId(approvalId); setActive('Delivery') }
  return <div className={`app-shell${active === 'Chat' ? ' chat-active' : ''}${active === 'Explorer' ? ' explorer-active' : ''}`}>
    <Rail active={active} setActive={setActive} onNewDelivery={() => setNewDeliveryOpen(true)} onProfile={() => setProfileOpen(!profileOpen)} />
    {profileOpen && <ProfileMenu data={data} onClose={() => setProfileOpen(false)} />}
    <div className="workspace"><header className="topbar"><span>{context}</span><div><Status tone={data.router_configured ? 'green' : 'amber'}>{data.router_configured ? 'router configured' : 'router not configured'}</Status><button className="help" aria-label="Help" onClick={() => setGuideOpen(true)}>?</button><button className="bell" aria-label="Notifications" aria-expanded={notificationsOpen} onClick={() => setNotificationsOpen(!notificationsOpen)}>♢</button></div></header>
      {notificationsOpen && <Notifications audit={data.audit || []} read={notificationsRead} onRead={() => setNotificationsRead(true)} />}
      {active === 'Chat'
        ? <div className="chat-grid"><ChatView append={setNotice} /></div>
        : active === 'Explorer'
        ? <div className="explorer-grid"><ExplorerView data={data} append={setNotice} /></div>
        : <div className="main-grid">
            {active === 'Delivery' ? <Delivery data={data} append={setNotice} onDirective={addDirection} onGuide={() => setGuideOpen(true)} onDeliverNext={() => setDeliverOpen(true)} /> : <WorkspaceView active={active} data={data} onNewSession={() => setNewSessionOpen(true)} onStartSession={(session, workflow) => setStartSession({ session, workflow })} append={setNotice} refresh={load} selectApproval={selectApproval} />}
            <Trace data={data} setActive={setActive} append={setNotice} refresh={load} selectedApprovalId={selectedApprovalId} clearApproval={() => setSelectedApprovalId(null)} />
          </div>}
      {notice && <div className="toast" role="status">{notice}<button aria-label="Dismiss notification" onClick={() => setNotice('')}>×</button></div>}
    </div>
    {newDeliveryOpen && <NewDelivery onClose={() => setNewDeliveryOpen(false)} onCreate={createDelivery} />}
    {newSessionOpen && <NewSession project={data.projects[0]} skills={data.skills.filter((skill) => skill.bridge_id === data.projects[0]?.bridge_id)} onClose={() => setNewSessionOpen(false)} onCreate={createConcurrentSession} />}
    {startSession && <StartSession session={startSession.session} workflow={startSession.workflow} onClose={() => setStartSession(null)} onStart={startConcurrentSession} />}
    {guideOpen && <Onboarding data={data} onClose={() => setGuideOpen(false)} setActive={setActive} onNewDelivery={() => setNewDeliveryOpen(true)} onNewSession={() => setNewSessionOpen(true)} />}
    {deliverOpen && <DeliverSheet project={data.projects[0]} workflows={data.workflows} sessions={data.sessions} runs={data.runs} onClose={() => setDeliverOpen(false)} onDeliver={deliverCandidate} />}
  </div>
}

export default App
