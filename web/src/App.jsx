import { useEffect, useMemo, useState } from 'react'
import { approve, bootstrap } from './api'

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

function Rail({ active, setActive }) {
  return <aside className="rail">
    <div className="brand"><Mark /><span>Anvil<br /><em>Workbench</em></span></div>
    <nav>{nav.map(([label, glyph]) => <button key={label} aria-label={label} className={active === label ? 'nav-item selected' : 'nav-item'} onClick={() => setActive(label)}><b aria-hidden="true">{glyph}</b>{label}</button>)}</nav>
    <div className="rail-footer">
      <button className="new-run"><span>+</span> New delivery</button>
      <div className="profile"><span>SD</span><div><strong>Operator</strong><small>tailnet owner</small></div><b>···</b></div>
    </div>
  </aside>
}

function Delivery({ data, append }) {
  const project = data.projects[0]
  const [input, setInput] = useState('')
  const [messages, setMessages] = useState([
    { who: 'You', type: 'human', text: 'Plan the delivery path for the Responses compatibility slice, then start task task_48.' },
    { who: 'Anvil agent', type: 'agent', text: 'I claimed task_48 from State, loaded its work packet, and mapped the response translator. I will edit and test locally; the final PR action will wait for your approval.' },
  ])
  const submit = (event) => {
    event.preventDefault()
    if (!input.trim()) return
    setMessages([...messages, { who: 'You', type: 'human', text: input.trim() }])
    append(`Queued a delivery note: ${input.trim()}`)
    setInput('')
  }
  return <main className="delivery">
    <header className="project-header"><div><span className="crumb">Delivery / {project?.name || 'No project'}</span><h1>Responses compatibility</h1><p>PRD → State plan → local Codex run → evidence → approved PR</p></div><button className="ghost-button">Task task_48 <span>⌄</span></button></header>
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

function Trace({ data }) {
  const approval = data.approvals.find((entry) => entry.status === 'pending')
  const [expanded, setExpanded] = useState(false)
  const [approved, setApproved] = useState(approval?.status === 'approved')
  const [busy, setBusy] = useState(false)
  const authorize = async () => {
    if (!approval) return
    setBusy(true)
    try { await approve(approval.id) } catch { /* seed UI remains usable when the hub is offline */ }
    setApproved(true); setBusy(false)
  }
  return <aside className="trace">
    <section className="trace-head"><div><span>Live trace</span><Status>router healthy</Status></div><button onClick={() => setExpanded(!expanded)}>{expanded ? '−' : '↗'}</button></section>
    <section className="trace-body">
      <TraceStep label="Intent" value="Implement task_48" detail="State work packet · bridge_dark" done />
      <TraceStep label="Route" value="heavy-local" detail="quality profile: trusted / local" done green />
      <TraceStep label="Tools" value="5 calls" detail="files · tests · State evidence" done />
      <TraceStep label="Verify" value="independent gate" detail="responses contract suite pending" current />
      <TraceStep label="GitHub" value="approval required" detail="commit + PR are bridge-only" />
      {expanded && <div className="raw-trace"><code>workbench_run_id: run_7f2a</code><code>task_id: task_48</code><code>request_id: req_91ce</code></div>}
    </section>
    <section className="evidence-mini"><header><span>Evidence packet</span><button>View all</button></header><div><span className="file-dot">✓</span><p><b>responses-contract.json</b><small>tool continuation · streaming order</small></p><time>09:46</time></div><div><span className="file-dot">✓</span><p><b>route-decision.ndjson</b><small>correlation and routed tier</small></p><time>09:47</time></div></section>
    <section className="approval-card"><div className="approval-title"><span>Approval required</span><Status tone={approved ? 'green' : 'amber'}>{approved ? 'authorized' : 'pending'}</Status></div><h2>{approved ? 'PR action released' : 'Create GitHub PR'}</h2><p>Diff hash <code>{approval?.payload_hash || 'no approval request'}</code></p><p className="muted">The local bridge verifies this exact diff, commits, pushes, and creates the PR once.</p><button disabled={busy || approved || !approval} onClick={authorize}>{approved ? 'Authorized' : busy ? 'Authorizing…' : 'Authorize action'} <span>→</span></button></section>
  </aside>
}

function TraceStep({ label, value, detail, done, current, green }) {
  return <div className={`trace-step ${done ? 'done' : ''} ${current ? 'current' : ''}`}><i>{done ? '✓' : current ? '●' : '○'}</i><div><small>{label}</small><b className={green ? 'green-text' : ''}>{value}</b><span>{detail}</span></div></div>
}

function App() {
  const [active, setActive] = useState('Delivery')
  const [data, setData] = useState(seed)
  const [notice, setNotice] = useState('')
  useEffect(() => { bootstrap().then((value) => value.projects.length ? setData(value) : undefined).catch(() => undefined) }, [])
  const context = useMemo(() => active === 'Delivery' ? 'Delivery cockpit' : `${active} view`, [active])
  return <div className="app-shell"><Rail active={active} setActive={setActive} /><div className="workspace"><header className="topbar"><span>{context}</span><div><Status>{data.router_configured ? 'private route ready' : 'router not configured'}</Status><button className="help">?</button><button className="bell">♢</button></div></header><div className="main-grid">{active === 'Delivery' ? <Delivery data={data} append={setNotice} /> : <section className="empty-view"><h1>{active}</h1><p>{active === 'Sandbox' ? 'Try a stateless model request without changing a delivery run.' : 'This view is connected to the same private delivery record.'}</p></section>}<Trace data={data} /></div>{notice && <div className="toast">{notice}<button onClick={() => setNotice('')}>×</button></div>}</div></div>
}

export default App
