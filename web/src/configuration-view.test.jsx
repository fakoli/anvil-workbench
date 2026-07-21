import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ConfigurationView from './configuration-view'
import {
  fetchConfigurationExport, previewConfigurationImport, applyConfigurationImport,
  previewConfigurationReset, applyConfigurationReset,
} from './api'

vi.mock('./api', () => ({
  fetchConfigurationExport: vi.fn(),
  previewConfigurationImport: vi.fn(),
  applyConfigurationImport: vi.fn(),
  previewConfigurationReset: vi.fn(),
  applyConfigurationReset: vi.fn(),
}))

const ENVELOPE_JSON = '{"schema_version":"workbench-configuration-export/v1","settings":[]}'
const data = { actor: 'operator', projects: [{ id: 'project_1', name: 'Qualification' }] }

function renderView() {
  return render(<ConfigurationView data={data} append={() => {}} />)
}

// Set the paste-in textarea directly (userEvent.type parses `{`/`}`/`[` as key
// descriptors, which JSON braces trip over).
function pasteImport(json) {
  fireEvent.change(screen.getByLabelText('Exported configuration'), { target: { value: json } })
}

beforeEach(() => {
  fetchConfigurationExport.mockResolvedValue({
    schema_version: 'workbench-configuration-export/v1',
    source: { scope: 'personal', actor_ref: 'actorref:0123456789abcdef', catalog_id: 'workbench.settings.initial' },
    settings: [{ setting_id: 'personal.time_format', scope: 'personal', value: 'format_12h' }],
  })
  previewConfigurationImport.mockResolvedValue({ status: 'previewed', preview: { valid: true, creates: [], changes: [], resets: [], skipped_read_only: [], unavailable_references: [], repairable: [], no_ops: [], base_versions: {} } })
  applyConfigurationImport.mockResolvedValue({ status: 'applied', result: { applied: [] }, applied: [], appliedCount: 0 })
  previewConfigurationReset.mockResolvedValue({ status: 'previewed', preview: { scope: 'personal', changes: [], base_versions: {} } })
  applyConfigurationReset.mockResolvedValue({ status: 'reset', result: { applied: [] }, applied: [], appliedCount: 0, scope: 'personal' })
})

afterEach(() => vi.clearAllMocks())

describe('ConfigurationView backup & transfer workflows (T006.4)', () => {
  it('states exclusions before any download control exists', () => {
    renderView()
    const note = screen.getByRole('note', { name: 'What an export excludes' })
    expect(within(note).getByText(/Secrets, credentials, and API tokens/)).toBeTruthy()
    expect(within(note).getByText(/Local filesystem paths/)).toBeTruthy()
    expect(within(note).getByText(/Chat history and raw prompts/)).toBeTruthy()
    // The download control only appears after Prepare export — exclusions first.
    expect(screen.queryByRole('link', { name: 'Download export' })).toBeNull()
  })

  it('exposes the download only after preparing, showing the opaque actor ref', async () => {
    const user = userEvent.setup()
    renderView()
    await user.click(screen.getByRole('button', { name: 'Prepare export' }))
    const download = await screen.findByRole('link', { name: 'Download export' })
    expect(download.getAttribute('download')).toBe('workbench-configuration.json')
    expect(screen.getByText('actorref:0123456789abcdef')).toBeTruthy()
  })

  it('withholds an export that fails the redaction guard (defence-in-depth)', async () => {
    fetchConfigurationExport.mockResolvedValue({
      schema_version: 'workbench-configuration-export/v1',
      source: { scope: 'personal', actor_ref: 'alice@example.com' }, // not opaque
      settings: [],
    })
    const user = userEvent.setup()
    renderView()
    await user.click(screen.getByRole('button', { name: 'Prepare export' }))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/withheld/i)
    expect(screen.queryByRole('link', { name: 'Download export' })).toBeNull()
  })

  it('does not apply an import before an explicit preview then apply step', async () => {
    previewConfigurationImport.mockResolvedValue({ status: 'previewed', preview: {
      valid: true,
      creates: [{ setting_id: 'personal.time_format', scope: 'personal', value: 'format_12h' }],
      changes: [], resets: [], skipped_read_only: [], unavailable_references: [], repairable: [], no_ops: [],
      base_versions: { 'personal.time_format': 0 },
    } })
    const user = userEvent.setup()
    renderView()
    // Apply is disabled until a valid preview exists (no early apply).
    expect(screen.getByRole('button', { name: 'Apply import' }).disabled).toBe(true)
    pasteImport(ENVELOPE_JSON)
    await user.click(screen.getByRole('button', { name: 'Preview import' }))
    await screen.findByRole('group', { name: 'Import preview' })
    expect(applyConfigurationImport).not.toHaveBeenCalled()
    const apply = screen.getByRole('button', { name: 'Apply import' })
    expect(apply.disabled).toBe(false)
    await user.click(apply)
    expect(applyConfigurationImport).toHaveBeenCalledWith(
      { schema_version: 'workbench-configuration-export/v1', settings: [] },
      expect.objectContaining({ projectId: 'project_1', baseVersions: { 'personal.time_format': 0 } }),
    )
  })

  it('cannot apply an invalid import and names every repairable field', async () => {
    previewConfigurationImport.mockResolvedValue({ status: 'previewed', preview: {
      valid: false, creates: [], changes: [], resets: [], skipped_read_only: [], unavailable_references: [],
      repairable: [
        { setting_id: 'personal.time_format', reason: 'not one of its allowed values' },
        { setting_id: 'personal.chat_transcript_retention_days', reason: 'out of bounds' },
      ],
      no_ops: [], base_versions: {},
    } })
    const user = userEvent.setup()
    renderView()
    pasteImport(ENVELOPE_JSON)
    await user.click(screen.getByRole('button', { name: 'Preview import' }))
    await screen.findByRole('group', { name: 'Import preview' })
    expect(screen.getByText(/not one of its allowed values/)).toBeTruthy()
    expect(screen.getByText(/out of bounds/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Apply import' }).disabled).toBe(true)
    // An invalid import can never be applied.
    await user.click(screen.getByRole('button', { name: 'Apply import' }))
    expect(applyConfigurationImport).not.toHaveBeenCalled()
  })

  it('previews a scoped reset then reports scope + result on apply', async () => {
    previewConfigurationReset.mockResolvedValue({ status: 'previewed', preview: {
      scope: 'personal',
      changes: [{ setting_id: 'personal.landing_surface', scope: 'personal', from: 'dashboard', to_default: 'chat', expected_version: 1 }],
      base_versions: { 'personal.landing_surface': 1 },
    } })
    applyConfigurationReset.mockResolvedValue({ status: 'reset', applied: [{ setting_id: 'personal.landing_surface' }], appliedCount: 1, scope: 'personal' })
    const user = userEvent.setup()
    renderView()
    const panel = screen.getByRole('region', { name: 'Reset preferences' })
    await user.click(within(panel).getByRole('button', { name: 'Preview reset' }))
    const preview = await screen.findByRole('group', { name: 'Reset preview' })
    expect(within(preview).getByText('personal.landing_surface')).toBeTruthy()
    await user.click(within(panel).getByRole('button', { name: 'Apply reset' }))
    expect((await within(panel).findByText(/personal preferences were reset/i))).toBeTruthy()
  })

  it('every workflow is reachable by keyboard with no min-width lockout', () => {
    renderView()
    const buttons = screen.getAllByRole('button')
    expect(buttons.length).toBeGreaterThanOrEqual(3)
    buttons.forEach((button) => expect(button.style.minWidth).toBe(''))
    expect(screen.getByLabelText('Exported configuration').tagName).toBe('TEXTAREA')
    expect(screen.getByLabelText('Scope to reset').tagName).toBe('SELECT')
  })
})
