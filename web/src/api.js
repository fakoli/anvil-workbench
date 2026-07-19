export async function bootstrap() {
  const response = await fetch('/api/bootstrap')
  if (!response.ok) throw new Error('Workbench hub is not available')
  return response.json()
}

export async function createProject({ name, state_root }) {
  const response = await fetch('/api/projects', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, state_root }),
  })
  if (!response.ok) throw new Error('Project could not be created')
  return response.json()
}

export async function approve(approvalId) {
  const response = await fetch(`/api/approvals/${approvalId}/approve`, {
    method: 'POST',
  })
  if (!response.ok) throw new Error('Approval could not be recorded')
  return response.json()
}

export async function createSession({ project_id, title, worktree_id, workflow_definition, skills }) {
  const response = await fetch('/api/sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_id, title, worktree_id, workflow_definition, skills }),
  })
  if (!response.ok) throw new Error('Session could not be created')
  return response.json()
}

export async function startWorkflow(workflowId, { task_id, model }) {
  const response = await fetch(`/api/workflows/${workflowId}/start`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ task_id, model }),
  })
  if (!response.ok) throw new Error('Workflow could not be started')
  return response.json()
}

export async function addDirective(sessionId, content) {
  const response = await fetch(`/api/sessions/${sessionId}/directives`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content }),
  })
  if (!response.ok) throw new Error('Delivery direction could not be recorded')
  return response.json()
}

export async function fetchRoutes() {
  const response = await fetch('/api/routes')
  if (!response.ok) throw new Error('Route decisions are unavailable')
  return response.json()
}

export async function searchEvidence(projectId, query) {
  const response = await fetch(`/api/evidence/search?project_id=${encodeURIComponent(projectId)}&query=${encodeURIComponent(query)}`)
  if (!response.ok) throw new Error('Evidence search is unavailable')
  return response.json()
}

export async function taskLineage(taskId) {
  const response = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/lineage`)
  if (!response.ok) throw new Error('Task lineage is unavailable')
  return response.json()
}

export async function runSandbox({ model, input }) {
  const response = await fetch('/api/sandbox', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model, input }),
  })
  if (!response.ok) throw new Error('Sandbox request was not accepted by Anvil Serving')
  return response.json()
}

export async function probeSkills(projectId) {
  const response = await fetch(`/api/projects/${projectId}/skills/probe`, { method: 'POST' })
  if (!response.ok) throw new Error('Bridge skills could not be checked')
  return response.json()
}

export function voiceSocketUrl(sessionId) {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/api/sessions/${sessionId}/voice/realtime`
}
