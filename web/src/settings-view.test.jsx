import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import SettingsView from './settings-view'
import {
  fetchPreferences, fetchPreference, writePreference, resetPreference, previewPolicyOperation,
} from './api'

vi.mock('./api', () => ({
  fetchPreferences: vi.fn(), fetchPreference: vi.fn(), writePreference: vi.fn(),
  resetPreference: vi.fn(), previewPolicyOperation: vi.fn(), policyApprovalBinding: vi.fn(),
}))

// The served /api/preferences shape, traceable to settings_actor_view: personal
// (common) + project (advanced) descriptors carrying only actor-view fields, plus
// resolved EffectiveValue.as_dict entries. Includes a mutable, an int-bounded, an
// approval-gated, and an env_only (read-only) project descriptor — every shape the
// backend can actually emit in the actor view.
const served = {
  catalog: {
    schema_version: 'workbench-settings-descriptor/v1',
    catalog_id: 'workbench.settings.initial',
    revision: '1.0.0',
    settings: [
      { id: 'personal.voice_autoplay', title: 'Voice auto-play', description: 'Play synthesized replies aloud automatically.', type: 'bool', scope: 'personal', sensitivity: 'public', mutability: 'mutable', application_timing: 'next_session', default: false },
      { id: 'personal.chat_transcript_retention_days', title: 'Chat transcript retention (days)', description: 'Actor-chosen retention, capped by the operator ceiling.', type: 'int', scope: 'personal', sensitivity: 'public', mutability: 'mutable', application_timing: 'next_run', bounds: { min: 1, max: 90 }, default: 30, policy_ceiling: { ceiling_setting: 'policy.transcript_retention_max_days' } },
      { id: 'project.delivery_route', title: 'Default delivery route', type: 'id_ref', scope: 'project', sensitivity: 'public', mutability: 'approval_gated', application_timing: 'next_run', ref_kind: 'route', default: 'route.delivery-heavy', policy_ceiling: { ceiling_setting: 'policy.route_allowlist_profile' } },
      { id: 'project.managed_identity', title: 'Managed identity binding', description: 'Owner-managed; configured outside the browser.', type: 'string', scope: 'project', sensitivity: 'public', mutability: 'env_only', application_timing: 'next_session' },
    ],
  },
  effective: [
    { setting_id: 'personal.voice_autoplay', scope: 'personal', value: false, source: 'default' },
    { setting_id: 'personal.chat_transcript_retention_days', scope: 'personal', value: 30, source: 'clamped' },
    { setting_id: 'project.delivery_route', scope: 'project', value: 'route.delivery-heavy', source: 'default' },
    { setting_id: 'project.managed_identity', scope: 'project', value: 'identity.default', source: 'stored' },
  ],
}

const data = { actor: 'operator', projects: [{ id: 'project_1', name: 'Qualification' }] }

function renderView() {
  return render(<SettingsView data={data} append={() => {}} />)
}

beforeEach(() => {
  fetchPreferences.mockResolvedValue(served)
  fetchPreference.mockResolvedValue({ preference: { setting_id: 'x', value: 30, write_version: 2, scope: 'personal' } })
  writePreference.mockResolvedValue({ status: 'saved', preference: { setting_id: 'x', value: 30, write_version: 3, scope: 'personal' } })
  resetPreference.mockResolvedValue({ status: 'reset', effective: { setting_id: 'x', scope: 'personal', value: 30, source: 'default' } })
  // The client returns the FULL server envelope under `preview` (the server's
  // preview_operation returns {preview:{…}, target, hub_local, requires_approval});
  // mirror that exact nesting so the test drives the real formatting path.
  previewPolicyOperation.mockResolvedValue({ status: 'previewed', preview: { preview: { digest: 'sha256:' + 'e'.repeat(64), operation: { operation: 'preference.set', setting_id: 'project.delivery_route', scope: 'project', value: 'route.delivery-heavy' }, effect_summary: 'set project.delivery_route (project) via hub-local' }, target: 'anvil-preferences', hub_local: true, requires_approval: true } })
})

afterEach(() => vi.clearAllMocks())

describe('SettingsView shell (T005.2)', () => {
  it('loads and groups COMMON actor preferences ahead of ADVANCED project controls', async () => {
    renderView()
    await screen.findByText('Voice auto-play')
    const groupTitles = screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)
    const commonIdx = groupTitles.findIndex((t) => /Your preferences/.test(t))
    const advancedIdx = groupTitles.findIndex((t) => /Project & system/.test(t))
    expect(commonIdx).toBeGreaterThanOrEqual(0)
    expect(advancedIdx).toBeGreaterThan(commonIdx)
  })

  it('shows the effective value and WHY it is effective, naming the policy ceiling (T005.4)', async () => {
    renderView()
    const retention = (await screen.findByText('Chat transcript retention (days)')).closest('.setting-row')
    expect(within(retention).getByText(/policy.transcript_retention_max_days/)).toBeTruthy()
  })

  it('searches by safe keyword and announces the result count, with a distinct empty state', async () => {
    const user = userEvent.setup()
    renderView()
    await screen.findByText('Voice auto-play')
    const search = screen.getByRole('searchbox', { name: /search settings/i })
    await user.type(search, 'voice')
    await waitFor(() => expect(screen.queryByText('Default delivery route')).toBeNull())
    expect(screen.getByText('Voice auto-play')).toBeTruthy()
    await user.clear(search)
    await user.type(search, 'zzzz')
    expect(await screen.findByText(/No settings match/, { selector: 'p.settings-empty' })).toBeTruthy()
  })

  it('renders a distinct unavailable state when the settings service is not configured', async () => {
    fetchPreferences.mockRejectedValueOnce(new Error('The settings service is not configured for this hub'))
    renderView()
    expect(await screen.findByText('Settings are unavailable')).toBeTruthy()
    expect(screen.getByRole('button', { name: /retry/i })).toBeTruthy()
  })
})

describe('scoped controls + validation (T005.3)', () => {
  it('renders a control FROM its descriptor (bounded number) and manages focus on open', async () => {
    const user = userEvent.setup()
    renderView()
    const toggle = await screen.findByRole('button', { name: /Chat transcript retention/i })
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    await user.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    const heading = await screen.findByRole('heading', { level: 4, name: /Change Chat transcript retention/i })
    await waitFor(() => expect(document.activeElement).toBe(heading))
    const control = screen.getByLabelText(/Chat transcript retention/i, { selector: 'input' })
    expect(control.getAttribute('type')).toBe('number')
    expect(control.getAttribute('min')).toBe('1')
    expect(control.getAttribute('max')).toBe('90')
  })

  it('blocks an invalid value with an accessible repair message and does NOT submit it', async () => {
    const user = userEvent.setup()
    renderView()
    await user.click(await screen.findByRole('button', { name: /Chat transcript retention/i }))
    const control = await screen.findByLabelText(/Chat transcript retention/i, { selector: 'input' })
    await waitFor(() => expect(screen.getByRole('button', { name: /Save change/i }).disabled).toBe(false))
    await user.clear(control)
    await user.type(control, '9999')
    await user.click(screen.getByRole('button', { name: /Save change/i }))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/at most 90/)
    expect(control.getAttribute('aria-describedby')).toContain(alert.id)
    expect(writePreference).not.toHaveBeenCalled()
  })

  it('successful save updates the version', async () => {
    const user = userEvent.setup()
    writePreference.mockResolvedValueOnce({ status: 'saved', preference: { setting_id: 'personal.chat_transcript_retention_days', value: 45, write_version: 3, scope: 'personal' } })
    renderView()
    await user.click(await screen.findByRole('button', { name: /Chat transcript retention/i }))
    const control = await screen.findByLabelText(/Chat transcript retention/i, { selector: 'input' })
    await waitFor(() => expect(screen.getByRole('button', { name: /Save change/i }).disabled).toBe(false))
    await user.clear(control)
    await user.type(control, '45')
    await user.click(screen.getByRole('button', { name: /Save change/i }))
    expect(await screen.findByText(/Now at version 3/, { selector: '.setting-notice' })).toBeTruthy()
    expect(writePreference).toHaveBeenCalledWith('personal.chat_transcript_retention_days', expect.objectContaining({ scope: 'personal', value: 45, expectedVersion: 2 }))
  })

  it('a SECOND save sends the UPDATED expectedVersion, not the original (S2 — revert-detecting)', async () => {
    const user = userEvent.setup()
    writePreference
      .mockResolvedValueOnce({ status: 'saved', preference: { setting_id: 'personal.chat_transcript_retention_days', value: 45, write_version: 3, scope: 'personal' } })
      .mockResolvedValueOnce({ status: 'saved', preference: { setting_id: 'personal.chat_transcript_retention_days', value: 50, write_version: 4, scope: 'personal' } })
    renderView()
    await user.click(await screen.findByRole('button', { name: /Chat transcript retention/i }))
    const control = await screen.findByLabelText(/Chat transcript retention/i, { selector: 'input' })
    await waitFor(() => expect(screen.getByRole('button', { name: /Save change/i }).disabled).toBe(false))
    await user.clear(control); await user.type(control, '45')
    await user.click(screen.getByRole('button', { name: /Save change/i }))
    await screen.findByText(/Now at version 3/, { selector: '.setting-notice' })
    // The first save started from the loaded version 2.
    expect(writePreference).toHaveBeenLastCalledWith('personal.chat_transcript_retention_days', expect.objectContaining({ expectedVersion: 2 }))

    // A second save MUST advance to the version the first save returned (3). If the
    // post-save `setExpectedVersion` is deleted, this re-sends 2 and the test fails.
    await user.clear(control); await user.type(control, '50')
    await user.click(screen.getByRole('button', { name: /Save change/i }))
    await waitFor(() => expect(writePreference).toHaveBeenLastCalledWith('personal.chat_transcript_retention_days', expect.objectContaining({ expectedVersion: 3, value: 50 })))
  })

  it('a STALE save (409) preserves the local draft and offers reload/compare', async () => {
    const user = userEvent.setup()
    writePreference.mockResolvedValueOnce({ status: 'stale', reloadRequired: true, currentVersion: 9, message: 'This setting changed elsewhere. Reload to compare before saving.' })
    renderView()
    await user.click(await screen.findByRole('button', { name: /Chat transcript retention/i }))
    const control = await screen.findByLabelText(/Chat transcript retention/i, { selector: 'input' })
    await waitFor(() => expect(screen.getByRole('button', { name: /Save change/i }).disabled).toBe(false))
    await user.clear(control)
    await user.type(control, '60')
    await user.click(screen.getByRole('button', { name: /Save change/i }))
    expect(await screen.findByText(/changed elsewhere/, { selector: '.setting-stale p' })).toBeTruthy()
    expect(control.value).toBe('60') // draft preserved, not discarded
    const reload = screen.getByRole('button', { name: /Reload & compare/i })
    await user.click(reload)
    await waitFor(() => expect(fetchPreference).toHaveBeenCalled())
  })

  it('reset PREVIEWS the scope-only target, then applies ONLY the selected scope on confirm (T005.3 c4)', async () => {
    const user = userEvent.setup()
    renderView()
    await user.click(await screen.findByRole('button', { name: /Chat transcript retention/i }))
    const resetBtn = await screen.findByRole('button', { name: /Reset personal scope to its inherited default/i })
    await waitFor(() => expect(resetBtn.disabled).toBe(false))

    // First click PREVIEWS — it must not immediately mutate.
    await user.click(resetBtn)
    const confirmPanel = await screen.findByRole('group', { name: /confirm reset/i })
    expect(within(confirmPanel).getByText(/only the/i).textContent).toMatch(/personal/) // scope-only, named
    expect(within(confirmPanel).getByText(/inherit the reviewed default/i)).toBeTruthy()
    expect(within(confirmPanel).getByText('30')).toBeTruthy() // the shown inherited target value
    expect(resetPreference).not.toHaveBeenCalled()

    // Confirm applies exactly the selected scope.
    await user.click(within(confirmPanel).getByRole('button', { name: /confirm reset/i }))
    await waitFor(() => expect(resetPreference).toHaveBeenCalledWith('personal.chat_transcript_retention_days', expect.objectContaining({ scope: 'personal' })))
  })

  it('after a reset the expectedVersion refreshes to 0 so an immediate re-save is not a guaranteed stale 409 (S2)', async () => {
    const user = userEvent.setup()
    // Seed a stored record at version 5; after reset the override is removed → next write expects 0.
    fetchPreference.mockResolvedValue({ preference: { setting_id: 'personal.chat_transcript_retention_days', value: 30, write_version: 5, scope: 'personal' } })
    renderView()
    await user.click(await screen.findByRole('button', { name: /Chat transcript retention/i }))
    const resetBtn = await screen.findByRole('button', { name: /Reset personal scope to its inherited default/i })
    await waitFor(() => expect(resetBtn.disabled).toBe(false))
    await user.click(resetBtn)
    await user.click(await screen.findByRole('button', { name: /confirm reset/i }))
    await waitFor(() => expect(resetPreference).toHaveBeenCalled())

    // Now re-save: the write must carry the REFRESHED expectedVersion 0, not the stale 5.
    const control = screen.getByLabelText(/Chat transcript retention/i, { selector: 'input' })
    await user.clear(control)
    await user.type(control, '45')
    await user.click(screen.getByRole('button', { name: /Save change/i }))
    await waitFor(() => expect(writePreference).toHaveBeenCalledWith('personal.chat_transcript_retention_days', expect.objectContaining({ expectedVersion: 0, value: 45 })))
  })
})

describe('affordances + approval preview (T005.4)', () => {
  it('an approval-gated control cannot masquerade as a save — it previews, never PUTs', async () => {
    const user = userEvent.setup()
    renderView()
    const row = (await screen.findByText('Default delivery route')).closest('.setting-row')
    expect(within(row).getByText(/Approval required/i)).toBeTruthy()
    await user.click(within(row).getByRole('button', { name: /Default delivery route/i }))
    await waitFor(() => expect(screen.getByRole('button', { name: /Preview approval-gated change/i }).disabled).toBe(false))
    expect(screen.queryByRole('button', { name: /Save change/i })).toBeNull()
    await user.click(screen.getByRole('button', { name: /Preview approval-gated change/i }))
    const preview = await screen.findByRole('region', { name: /approval preview/i })
    expect(within(preview).getByText('preference.set')).toBeTruthy()
    expect(within(preview).getByText(/^sha256:[0-9a-f]{64}$/)).toBeTruthy() // payload fingerprint, no secret
    expect(writePreference).not.toHaveBeenCalled()
  })

  it('an owner-managed (env_only) control is shown read-only and offers no ordinary save', async () => {
    const user = userEvent.setup()
    renderView()
    const row = (await screen.findByText('Managed identity binding')).closest('.setting-row')
    expect(within(row).getByText(/read only/i)).toBeTruthy()
    await user.click(within(row).getByRole('button', { name: /Managed identity binding/i }))
    expect(await screen.findByText(/owner-managed and configured outside the browser/i)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Save change/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /Preview approval-gated change/i })).toBeNull()
  })

  it('an approval-gated Reset routes through PREVIEW — it never fires a plain mutating reset that dead-ends as stale (MUST#1)', async () => {
    const user = userEvent.setup()
    // The reset preview envelope carries the reset operation, not a set.
    previewPolicyOperation.mockResolvedValueOnce({ status: 'previewed', preview: { preview: { digest: 'sha256:' + 'e'.repeat(64), operation: { operation: 'preference.reset', setting_id: 'project.delivery_route', scope: 'project' }, effect_summary: 'reset project.delivery_route (project)' }, target: 'anvil-preferences', hub_local: true, requires_approval: true } })
    renderView()
    const row = (await screen.findByText('Default delivery route')).closest('.setting-row')
    await user.click(within(row).getByRole('button', { name: /Default delivery route/i }))
    await waitFor(() => expect(screen.getByRole('button', { name: /Preview approval-gated change/i }).disabled).toBe(false))
    // There is NO plain "Reset {scope} scope" button on an approval-gated editor…
    expect(screen.queryByRole('button', { name: /^Reset project scope to its inherited default/i })).toBeNull()
    // …only a preview-routed reset, which must NOT call the mutating resetPreference.
    await user.click(screen.getByRole('button', { name: /Preview reset of Default delivery route/i }))
    const preview = await screen.findByRole('region', { name: /approval preview/i })
    expect(within(preview).getByText('preference.reset')).toBeTruthy()
    expect(previewPolicyOperation).toHaveBeenLastCalledWith(expect.objectContaining({ operation: 'preference.reset', scope: 'project' }))
    expect(resetPreference).not.toHaveBeenCalled()
    expect(writePreference).not.toHaveBeenCalled()
  })

  it('suppresses a NON-conforming digest — a leaked non-hash value is never rendered as a Payload fingerprint (S1)', async () => {
    const user = userEvent.setup()
    previewPolicyOperation.mockResolvedValueOnce({ status: 'previewed', preview: { preview: { digest: 'https://provider.example:8443/leaked', operation: { operation: 'preference.set', setting_id: 'project.delivery_route', scope: 'project', value: 'route.delivery-heavy' }, effect_summary: 'set project.delivery_route' }, target: 'anvil-preferences', hub_local: true, requires_approval: true } })
    renderView()
    const row = (await screen.findByText('Default delivery route')).closest('.setting-row')
    await user.click(within(row).getByRole('button', { name: /Default delivery route/i }))
    await waitFor(() => expect(screen.getByRole('button', { name: /Preview approval-gated change/i }).disabled).toBe(false))
    await user.click(screen.getByRole('button', { name: /Preview approval-gated change/i }))
    const preview = await screen.findByRole('region', { name: /approval preview/i })
    // The leaked non-hash digest must NOT appear anywhere in the rendered preview…
    expect(within(preview).queryByText(/provider\.example/)).toBeNull()
    expect(within(preview).queryByText(/8443/)).toBeNull()
    // …the fingerprint cell shows the suppressed placeholder under the trusted label.
    expect(within(preview).getByText('fingerprint unavailable')).toBeTruthy()
  })
})

describe('a11y status announcements (T005.5)', () => {
  it('exposes a polite live region and announces the load', async () => {
    renderView()
    await screen.findByText('Voice auto-play')
    const live = document.querySelector('.settings-live[role="status"]')
    expect(live).toBeTruthy()
    await waitFor(() => expect(live.textContent).toMatch(/Loaded 4 settings/))
  })

  it('Escape closes the editor and restores focus to the row toggle (T005.5)', async () => {
    const user = userEvent.setup()
    renderView()
    const toggle = await screen.findByRole('button', { name: /Chat transcript retention/i })
    await user.click(toggle)
    const heading = await screen.findByRole('heading', { level: 4, name: /Change Chat transcript retention/i })
    await waitFor(() => expect(document.activeElement).toBe(heading))
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('heading', { level: 4, name: /Change Chat transcript retention/i })).toBeNull())
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    await waitFor(() => expect(document.activeElement).toBe(toggle)) // focus restored to the opener
  })

  it('Escape while a native <select> is focused does NOT close the editor — the dropdown owns that Escape (T005.5)', async () => {
    const user = userEvent.setup()
    // A catalog with an enum (select) setting so we can focus a real <select>.
    const enumServed = {
      catalog: {
        schema_version: 'workbench-settings-descriptor/v1', catalog_id: 'c', revision: '1.0.0',
        settings: [
          { id: 'personal.landing_surface', title: 'Default landing surface', description: 'The surface an actor opens on.', type: 'enum', scope: 'personal', sensitivity: 'public', mutability: 'mutable', application_timing: 'immediate', allowed_values: ['chat', 'delivery', 'dashboard'], default: 'chat' },
        ],
      },
      effective: [{ setting_id: 'personal.landing_surface', scope: 'personal', value: 'chat', source: 'default' }],
    }
    fetchPreferences.mockResolvedValueOnce(enumServed)
    renderView()
    const toggle = await screen.findByRole('button', { name: /Default landing surface/i })
    await user.click(toggle)
    const select = await screen.findByLabelText(/Default landing surface/i, { selector: 'select' })
    select.focus()
    expect(document.activeElement).toBe(select)
    await user.keyboard('{Escape}')
    // The editor stays open; the toggle remains expanded.
    expect(screen.getByRole('heading', { level: 4, name: /Change Default landing surface/i })).toBeTruthy()
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
  })
})
