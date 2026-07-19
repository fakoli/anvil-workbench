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
