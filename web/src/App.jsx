import { useEffect, useMemo, useState } from 'react'
import { approve, bootstrap, createProject } from './api'

const seed = {
  projects: [{ id: 'project_anvil', name: 'Anvil Workbench', state_root: '.anvil', bridge_id: 'bridge_dark' }],
  runs: [{ id: 'run_7f2a', task_id: 'task_48', model: 'heavy-local', status: 'running' }],
  approvals: [{
    id: 'approval_7c4e', project_id: 'project_anvil', action_type: 'commit_pr', status: 'pending',
    payload_hash: '83d94b4a…9a1f', expires_at: '14 minutes',
  }],
  router_configured: true,
}

const nav = [
  ['Delivery', '⌘'], ['Runs', '↗'], ['Routes', '⌁'], ['Approvals', '✓'], ['Evidence', '◈'], ['Sandbox', '□'],
]

function Mark() {
  return <span className="brand-mark" aria-hidden="true"><i /><i /><i /></span>
}

function Status({ tone = 'green', children }) {
  return <span className={`status status-${tone}`}><span className="dot" />{children}</span>
}

function Rail({ active, setActive, onNewDelivery, onProfile }) {
  return <aside className="rail">
    <div className="brand"><Mark /><span>Anvil<br /><em>Workbench</em></span></div>
    <nav>{nav.map(([label, glyph]) => <button key={label} aria-label={label} className={active === label ? 'nav-item selected' : 'nav-item'} onClick={() => setActive(label)}><b aria-hidden="true">{glyph}</b>{label}</button>)}</nav>
    <div className="rail-footer">
      <button className="new-run" onClick={onNewDelivery}><span aria-hidden="true">+</span> New delivery</button>
      <button className="profile" aria-label="Operator menu" onClick={onProfile}><span>SD</span><div><strong>Operator</strong><small>tailnet owner</small></div><b aria-hidden="true">···</b></button>
    </div>
  </aside>
}

function Delivery({ data, append }) {
  const project = data.projects[0]
  const [input, setInput] = useState('')
  const [taskOpen, setTaskOpen] = useState(false)
  const [messages, setMessages] = useState([
    { who: 'You', type: 'human', text: 'Plan the delivery path for the Responses compatibility slice, then start task task_48.' },
    { who: 'Anvil agent', type: 'agent', text: 'I claimed task_48 from State, loaded its work packet, and mapped the response translator. I will edit and test locally; the final PR action will wait for your approval.' },
  ])
  const submit = (event) => {
    event.preventDefault()
    if (!input.trim()) return
    const note = input.trim()
    setMessages([...messages, { who: 'You', type: 'human', text: note }])
    append(`Queued a delivery note: ${note}`)
    setInput('')
  }
  return <main className="delivery">
    <header className="project-header"><div><span className="crumb">Delivery / {project?.name || 'No project'}</span><h1>Responses compatibility</h1><p>PRD → State plan → local Codex run → evidence → approved PR</p></div><button className="ghost-button" aria-expanded={taskOpen} onClick={() => setTaskOpen(!taskOpen)}>Task task_48 <span aria-hidden="true">⌄</span></button></header>
    {taskOpen && <section className="task-details" aria-label="Task task_48 details"><b>Task task_48</b><span>Claimed through Anvil State; its packet requires independent verification and evidence before review.</span></section>}
    <section className="flow-card">
      <div className="flow-top"><span className="thread-avatar">A</span><div><strong>Delivery agent</strong><small>Codex via Anvil Serving</small></div><Status>running</Status></div>
      <ol className="steps">
        <li className="complete"><span>✓</span><div><b>State work packet loaded</b><small>task_48 · acceptance and evidence requirements attached</small></div><time>09:41</time></li>
        <li className="complete"><span>✓</span><div><b>Implementation plan accepted</b><small>stateless Responses subset, tool continuation, route correlation</small></div><time>09:43</time></li>
        <li className="current"><span>3</span><div><b>Editing and independent verification</b><small>Bridge streams redacted activity and test evidence here</small></div><time>now</time></li>
        <li><span>4</span><div><b>Review diff and authorize PR</b><small>Hash-bound approval required before GitHub action</small></div></li>
      </ol>
    </section>
    <section className="conversation" aria-label="Delivery conversation">
      {messages.map((message, index) => <article className={`message ${message.type}`} key={`${message.who}-${index}`}><div className="message-head"><span>{message.type === 'human' ? 'SD' : 'A'}</span><b>{message.who}</b><small>{index === 0 ? '09:40' : index === 1 ? '09:42' : 'now'}</small></div><p>{message.text}</p></article>)}
    </section>
    <form className="composer" onSubmit={submit}><textarea aria-label="Add direction to this delivery" value={input} onChange={(event) => setInput(event.target.value)} rows="2" placeholder="Add direction to this delivery…" /><div><small>Routes through Anvil Serving · stateful actions stay local to the bridge</small><button type="submit" aria-label="Send delivery direction">Send <span aria-hidden="true">↵</span></button></div></form>
  </main>
}

function Trace({ data, onViewEvidence, append }) {
  const approval = data.approvals.find((entry) => entry.status === 'pending')
  const run = data.runs[0]
  const taskId = run?.task_id || 'no task selected'
  const runId = run?.id || 'not assigned'
  const hasEvidence = run?.status === 'evidenced'
  const verification = hasEvidence ? 'evidence submitted' : run?.status === 'reconciliation' ? 'reconciliation required' : 'independent gate pending'
  const [expanded, setExpanded] = useState(false)
  const [approved, setApproved] = useState(approval?.status === 'approved')
  const [busy, setBusy] = useState(false)
  const authorize = async () => {
    if (!approval) return
    setBusy(true)
    try {
      await approve(approval.id)
      setApproved(true)
      append('Approval recorded. The local bridge may now consume this exact action once.')
    } catch {
      append('Approval was not recorded. The bridge remains unable to create a PR.')
    } finally {
      setBusy(false)
    }
  }
  return <aside className="trace">
    <section className="trace-head"><div><span>Live trace</span><Status>router healthy</Status></div><button aria-label="Show correlation trace" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? '−' : '↗'}</button></section>
    <section className="trace-body">
      <TraceStep label="Intent" value={run ? `Implement ${taskId}` : 'No active task'} detail={run ? `run ${runId}` : 'Start a bridge-supervised delivery to load a State work packet.'} done={Boolean(run)} />
      <TraceStep label="Route" value={run?.model || 'no route selected'} detail={data.router_configured ? 'router correlation enabled' : 'router configuration required'} done={Boolean(run && data.router_configured)} green />
      <TraceStep label="Tools" value={hasEvidence ? 'evidence captured' : 'bridge events pending'} detail="files · tests · State evidence" done={hasEvidence} />
      <TraceStep label="Verify" value={verification} detail={hasEvidence ? 'independent evidence is available for review' : 'the bridge must submit independent evidence'} current={!hasEvidence} />
      <TraceStep label="GitHub" value="approval required" detail="commit + PR are bridge-only" />
      {expanded && <div className="raw-trace"><code>workbench_run_id: {runId}</code><code>task_id: {taskId}</code><code>request_id: recorded by Serving when the run calls a model</code></div>}
    </section>
    <section className="evidence-mini"><header><span>Evidence packet</span><button onClick={onViewEvidence}>View all</button></header>{hasEvidence ? <><div><span className="file-dot">✓</span><p><b>verification evidence</b><small>redacted bridge artifact</small></p><time>captured</time></div><div><span className="file-dot">✓</span><p><b>route decision</b><small>correlation and routed tier</small></p><time>captured</time></div></> : <p className="evidence-empty">Awaiting redacted evidence from an evidenced bridge run.</p>}</section>
    <section className="approval-card"><div className="approval-title"><span>Approval required</span><Status tone={approved ? 'green' : 'amber'}>{approved ? 'authorized' : 'pending'}</Status></div><h2>{approved ? 'PR action released' : 'Create GitHub PR'}</h2><p>Diff hash <code>{approval?.payload_hash || 'no approval request'}</code></p><p className="muted">The local bridge verifies this exact diff, commits, pushes, and creates the PR once.</p><button disabled={busy || approved || !approval} onClick={authorize}>{approved ? 'Authorized' : busy ? 'Authorizing…' : 'Authorize action'} <span aria-hidden="true">→</span></button></section>
  </aside>
}

function TraceStep({ label, value, detail, done, current, green }) {
  return <div className={`trace-step ${done ? 'done' : ''} ${current ? 'current' : ''}`}><i>{done ? '✓' : current ? '●' : '○'}</i><div><small>{label}</small><b className={green ? 'green-text' : ''}>{value}</b><span>{detail}</span></div></div>
}

function WorkspaceView({ active, data }) {
  if (active === 'Runs') return <section className="workspace-view"><span className="crumb">Runs / bridge-supervised</span><h1>Runs</h1><p>Every delivery run is linked to its State task and routed model.</p><div className="data-list">{data.runs.map((run) => <article key={run.id}><b>{run.task_id || 'Unassigned task'}</b><span>{run.model}</span><Status tone={run.status === 'reconciliation' ? 'amber' : 'green'}>{run.status}</Status></article>)}</div></section>
  if (active === 'Routes') return <section className="workspace-view"><span className="crumb">Routes / Anvil Serving</span><h1>Routes</h1><p>Run, task, and request correlation stay visible without exposing router credentials.</p><div className="data-list"><article><b>{data.runs[0]?.model || 'No routed run yet'}</b><span>{data.runs[0] ? 'workbench_run_id · task_id · request_id' : 'Start a run to record model-route correlation.'}</span><Status tone={data.router_configured ? 'green' : 'amber'}>{data.router_configured ? 'configured' : 'not configured'}</Status></article></div></section>
  if (active === 'Approvals') return <section className="workspace-view"><span className="crumb">Approvals / hash-bound</span><h1>Approvals</h1><p>Approval alone does not execute a GitHub action; only the matching bridge can consume it once.</p><div className="data-list">{data.approvals.map((approval) => <article key={approval.id}><b>{approval.action_type.replaceAll('_', ' ')}</b><span>{approval.payload_hash}</span><Status tone={approval.status === 'pending' ? 'amber' : 'green'}>{approval.status}</Status></article>)}</div></section>
  if (active === 'Evidence') return <section className="workspace-view"><span className="crumb">Evidence / redacted</span><h1>Evidence</h1><p>These redacted artifacts support review; Anvil State remains canonical for acceptance.</p><div className="data-list">{data.runs.some((run) => run.status === 'evidenced') ? data.runs.filter((run) => run.status === 'evidenced').map((run) => <article key={run.id}><b>Evidence for {run.task_id || run.id}</b><span>Redacted bridge artifacts and route correlation</span><Status>captured</Status></article>) : <article><b>No evidence packet yet</b><span>The bridge will surface redacted artifacts after independent verification succeeds.</span><Status tone="amber">awaiting bridge</Status></article>}</div></section>
  return <section className="workspace-view"><span className="crumb">Sandbox / read-only</span><h1>Model sandbox</h1><p>The browser never receives router credentials. A server-side sandbox request is intentionally not wired until its response redaction and audit contract is qualified.</p><div className="sandbox-note"><b>Current boundary</b><span>Use the bridge for delivery runs; this panel is a visible policy boundary rather than an untracked provider bypass.</span></div></section>
}

function Modal({ title, children, onClose }) {
  return <div className="modal-backdrop" role="presentation"><section className="modal" role="dialog" aria-modal="true" aria-label={title}><header><h2>{title}</h2><button aria-label={`Close ${title}`} onClick={onClose}>×</button></header>{children}</section></div>
}

function NewDelivery({ onClose, onCreate }) {
  const [name, setName] = useState('')
  const [stateRoot, setStateRoot] = useState('.anvil')
  const [busy, setBusy] = useState(false)
  const submit = async (event) => {
    event.preventDefault()
    if (!name.trim()) return
    setBusy(true)
    try { await onCreate({ name: name.trim(), state_root: stateRoot.trim() || '.anvil' }) } finally { setBusy(false) }
  }
  return <Modal title="New delivery" onClose={onClose}><p>Create the Workbench project record first. A project bridge remains local and must be registered outside the browser credential boundary.</p><form className="modal-form" onSubmit={submit}><label>Project name<input value={name} onChange={(event) => setName(event.target.value)} /></label><label>State root<input value={stateRoot} onChange={(event) => setStateRoot(event.target.value)} /></label><div><button type="button" className="secondary-button" onClick={onClose}>Cancel</button><button type="submit" disabled={busy || !name.trim()}>{busy ? 'Creating…' : 'Create project'}</button></div></form></Modal>
}

function Help({ onClose }) {
  return <Modal title="Delivery cockpit help" onClose={onClose}><p>State is canonical. Workbench supervises redacted runs and approvals. The project-local bridge is the only component that may use GitHub credentials.</p><ul><li>Use Delivery to provide direction and inspect the current task.</li><li>Use Runs, Routes, Approvals, and Evidence to review execution context.</li><li>Only authorize a hash after reviewing the diff and evidence.</li></ul></Modal>
}

function Notifications({ read, onRead }) {
  return <section className="notifications" aria-label="Notifications"><header><b>Notifications</b><button onClick={onRead}>Mark all read</button></header>{read ? <p>All caught up.</p> : <p>Independent verification is still pending for task_48.</p>}</section>
}

function ProfileMenu({ onClose }) {
  return <section className="profile-menu" aria-label="Operator menu"><b>Operator</b><span>Tailnet owner</span><small>Approval and project-creation permissions are checked by the hub.</small><button onClick={onClose}>Close menu</button></section>
}

function App() {
  const [active, setActive] = useState('Delivery')
  const [data, setData] = useState(seed)
  const [notice, setNotice] = useState('')
  const [newDeliveryOpen, setNewDeliveryOpen] = useState(false)
  const [helpOpen, setHelpOpen] = useState(false)
  const [profileOpen, setProfileOpen] = useState(false)
  const [notificationsOpen, setNotificationsOpen] = useState(false)
  const [notificationsRead, setNotificationsRead] = useState(false)
  const load = async () => {
    const value = await bootstrap()
    if (value.projects.length) setData(value)
    return value
  }
  useEffect(() => { load().catch(() => undefined) }, [])
  const createDelivery = async (payload) => {
    try {
      const project = await createProject(payload)
      setData((current) => ({ ...current, projects: [project, ...current.projects] }))
      setActive('Delivery')
      setNewDeliveryOpen(false)
      setNotice(`Created ${project.name}. Register its bridge locally before starting a run.`)
      load().catch(() => undefined)
    } catch {
      setNotice('Project could not be created. No bridge or run was started.')
    }
  }
  const context = useMemo(() => active === 'Delivery' ? 'Delivery cockpit' : `${active} view`, [active])
  return <div className="app-shell"><Rail active={active} setActive={setActive} onNewDelivery={() => setNewDeliveryOpen(true)} onProfile={() => setProfileOpen(!profileOpen)} />{profileOpen && <ProfileMenu onClose={() => setProfileOpen(false)} />}<div className="workspace"><header className="topbar"><span>{context}</span><div><Status>{data.router_configured ? 'private route ready' : 'router not configured'}</Status><button className="help" aria-label="Help" onClick={() => setHelpOpen(true)}>?</button><button className="bell" aria-label="Notifications" aria-expanded={notificationsOpen} onClick={() => setNotificationsOpen(!notificationsOpen)}>♢</button></div></header>{notificationsOpen && <Notifications read={notificationsRead} onRead={() => setNotificationsRead(true)} />}<div className="main-grid">{active === 'Delivery' ? <Delivery data={data} append={setNotice} /> : <WorkspaceView active={active} data={data} />}<Trace data={data} onViewEvidence={() => setActive('Evidence')} append={setNotice} /></div>{notice && <div className="toast" role="status">{notice}<button aria-label="Dismiss notification" onClick={() => setNotice('')}>×</button></div>}</div>{newDeliveryOpen && <NewDelivery onClose={() => setNewDeliveryOpen(false)} onCreate={createDelivery} />}{helpOpen && <Help onClose={() => setHelpOpen(false)} />}</div>
}

export default App
