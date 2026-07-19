export async function bootstrap() {
  const response = await fetch('/api/bootstrap')
  if (!response.ok) throw new Error('Workbench hub is not available')
  return response.json()
}

export async function approve(approvalId) {
  const response = await fetch(`/api/approvals/${approvalId}/approve`, {
    method: 'POST',
  })
  if (!response.ok) throw new Error('Approval could not be recorded')
  return response.json()
}
