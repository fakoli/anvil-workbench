import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import PluginCatalogView from './plugin-catalog-view'
import { fetchPlugins, fetchPluginReceipt } from './api'

vi.mock('./api', () => ({
  fetchPlugins: vi.fn(),
  fetchPluginReceipt: vi.fn(),
  // The view now keys its unconfigured-degrade branch off this SHARED sentinel by
  // value equality; the mock must export the exact same string the real api.js
  // throws on 503 (the 503-degrade test rejects with this verbatim).
  PLUGIN_NOT_CONFIGURED: 'The plugin catalog is not configured for this hub',
}))

// The served /api/plugins shape, traceable to `_published_plugin`
// (workbench/plugin_host.py): the EXACT projected field set, credentials by
// reference only. Two plugins (one host-owned credential, one none) with distinct
// tools so tools flatten across plugins.
const PLUGIN_DIGEST = 'sha256:' + 'a'.repeat(64)
const served = {
  plugins: [
    {
      plugin_id: 'deploy-notify',
      title: 'Deploy Notifier',
      version: '1.2.0',
      plugin_digest: PLUGIN_DIGEST,
      publisher: { name: 'Anvil Labs', kind: 'first_party' },
      description: 'Posts deployment notes to a reviewed channel.',
      support_status: 'supported',
      credential: { requirement: 'host_owned', owner_host: 'notify-connector', credential_refs: ['deploy-channel-ref'] },
      tools: [
        {
          tool_id: 'post_note', title: 'Post note', summary: 'Post a redacted deploy note.',
          effect: 'external_effect',
          gates: { preview: 'required', confirmation: 'required', human_approval: 'required', approval_action: 'invoke_effect_tool' },
          data_access: ['read_task_context'],
          input_schema: { type: 'object', properties: { message: { type: 'string' } } },
          output_schema: { type: 'object', properties: { posted: { type: 'boolean' } } },
        },
      ],
    },
    {
      plugin_id: 'issue-reader',
      title: 'Issue Reader',
      version: '0.4.1',
      plugin_digest: 'sha256:' + 'e'.repeat(64),
      publisher: { name: 'Community Reviewed', kind: 'reviewed_third_party' },
      description: 'Reads issue metadata.',
      support_status: 'supported',
      credential: { requirement: 'none' },
      tools: [
        {
          tool_id: 'read_issue', title: 'Read issue', summary: 'Read one issue.',
          effect: 'read',
          gates: { preview: 'optional', confirmation: 'not_required', human_approval: 'not_required', approval_action: null },
          data_access: ['read_project_metadata'],
          input_schema: { type: 'object', properties: { id: { type: 'string' } } },
          output_schema: { type: 'object', properties: { title: { type: 'string' } } },
        },
      ],
    },
  ],
}

const data = {
  projects: [{ id: 'project_1', name: 'Qualification' }],
  router_configured: true,
  // content_sha256 is served BARE 64-hex (no `sha256:` prefix; api.py/models.py).
  skills: [{ skill_id: 'lint', description: 'Run repository linters', content_sha256: 'b'.repeat(64), bridge_id: 'bridge_1' }],
}

const acceptedReceipt = {
  schema_version: 'workbench-plugin-receipt/v1',
  receipt_id: 'plugrcpt_' + 'a'.repeat(12),
  request_digest: 'sha256:' + 'd'.repeat(64),
  kind: 'tool_call',
  plugin: { plugin_id: 'deploy-notify', plugin_digest: PLUGIN_DIGEST },
  tool_id: 'post_note',
  status: 'accepted',
  effect: 'external_effect',
  credential_use: { requirement: 'host_owned', owner_host: 'notify-connector', credential_refs: ['deploy-channel-ref'] },
  result: { output_digest: 'sha256:' + 'c'.repeat(64), output_summary: 'Note posted to the reviewed channel.' },
  redaction: { status: 'redacted' },
  completed_at: '2026-07-21T09:15:00Z',
}

const deniedReceipt = {
  schema_version: 'workbench-plugin-receipt/v1',
  receipt_id: 'plugrcpt_' + 'f'.repeat(12),
  request_digest: 'sha256:' + '9'.repeat(64),
  kind: 'install',
  plugin: { plugin_id: 'deploy-notify', plugin_digest: PLUGIN_DIGEST },
  status: 'denied',
  effect: 'plugin_lifecycle',
  error: { code: 'digest_drift', safe_summary: 'The pinned plugin digest does not match the reviewed catalog.', retryable: false },
  redaction: { status: 'redacted' },
}

function renderView(props = {}) {
  return render(<PluginCatalogView data={data} append={() => {}} {...props} />)
}

beforeEach(() => {
  fetchPlugins.mockResolvedValue(served)
  fetchPluginReceipt.mockResolvedValue({ receipt: acceptedReceipt })
})

afterEach(() => vi.clearAllMocks())

describe('criterion 1 — distinguishable categories', () => {
  it('renders skills, plugins, tools, routes, and delivery operations as five distinct labeled categories', async () => {
    renderView()
    await screen.findByRole('article', { name: 'Plugin Deploy Notifier' })
    const headings = screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)
    for (const label of ['Bridge skills', 'Reviewed plugins', 'Plugin tools', 'Serving routes', 'Delivery operations']) {
      expect(headings).toContain(label)
    }
    // Each category exposes its own permission model text — not a flat list.
    expect(screen.getByText(/Digest-pinned local body/)).toBeTruthy() // skills
    expect(screen.getByText(/operator-signed/)).toBeTruthy() // plugins
    expect(screen.getByText(/mandatory effect class and gate set/)).toBeTruthy() // tools
    expect(screen.getByText(/read-only Anvil Serving routing decision/i)).toBeTruthy() // routes
    expect(screen.getByText(/execution \/ operation_digest/)).toBeTruthy() // delivery operations
  })

  it('a bridge skill is shown with its own permission summary (digest, no path)', async () => {
    renderView()
    const skillItem = (await screen.findByText('lint')).closest('li')
    expect(within(skillItem).getByText(/Local body · digest/)).toBeTruthy()
    // The served skill has no path; nothing path-like is rendered.
    expect(document.body.textContent).not.toMatch(/\/home\/|C:\\/)
  })
})

describe('criterion 2 — add/upgrade shows exact fields + credentials by reference', () => {
  it('the plugin card shows exact version, digest (verbatim), publisher, support, effect, data policy, and approval state', async () => {
    renderView()
    const card = await screen.findByRole('article', { name: 'Plugin Deploy Notifier' })
    expect(within(card).getByText('1.2.0')).toBeTruthy() // version
    expect(within(card).getByText(PLUGIN_DIGEST)).toBeTruthy() // digest, verbatim (full 64-hex)
    expect(within(card).getByText(/Anvil Labs/)).toBeTruthy() // publisher name
    expect(within(card).getByText(/First-party/)).toBeTruthy() // publisher kind
    expect(within(card).getByText('supported')).toBeTruthy() // support status
    expect(within(card).getAllByText('Reviewed & enabled').length).toBeGreaterThan(0) // approval state (pill + field)
    // Effect + data policy + approval are in the tool permission summary (text, not colour).
    const perm = within(card).getByText(/External effect/)
    expect(perm.textContent).toMatch(/Read task context/)
    expect(perm.textContent).toMatch(/Human approval required/)
  })

  it('credentials appear BY REFERENCE only — owning host + reference id, never a secret/token/path', async () => {
    renderView()
    const card = await screen.findByRole('article', { name: 'Plugin Deploy Notifier' })
    const cred = within(card).getByText(/Host-owned by notify-connector/)
    expect(cred.textContent).toMatch(/deploy-channel-ref/)
    // No secret material is representable in the closed served shape.
    expect(document.body.textContent).not.toMatch(/secret|token|password|bearer|ghp_|sk-/i)
    expect(document.body.textContent).not.toMatch(/:\d{2,5}\//) // no host:port/path
  })

  it('a none-requirement plugin truthfully shows no credential', async () => {
    renderView()
    const card = await screen.findByRole('article', { name: 'Plugin Issue Reader' })
    expect(within(card).getByText('No credential required')).toBeTruthy()
  })
})

describe('criterion 3 — tool selection + result/error cards (keyboard + SR)', () => {
  it('selecting a tool is keyboard-reachable, moves focus into the detail, and looks up an accessible RESULT card', async () => {
    const user = userEvent.setup()
    renderView()
    await screen.findByRole('article', { name: 'Plugin Deploy Notifier' })
    const select = screen.getByRole('button', { name: /Select tool Post note from Deploy Notifier/i })
    await user.click(select)
    const heading = await screen.findByRole('heading', { level: 3, name: 'Post note' })
    await waitFor(() => expect(document.activeElement).toBe(heading))

    const input = screen.getByLabelText(/Look up a dispatch receipt by request digest/i)
    await user.type(input, 'sha256:' + 'd'.repeat(64))
    await user.click(screen.getByRole('button', { name: /Look up receipt/i }))

    const result = await screen.findByRole('status', { name: /dispatch result/i })
    expect(within(result).getAllByText('Approved & applied').length).toBeGreaterThan(0)
    expect(within(result).getByText(/Note posted to the reviewed channel/)).toBeTruthy()
    // Credential use in the receipt is by reference only.
    expect(within(result).getByText(/Host-owned by notify-connector/)).toBeTruthy()
    expect(fetchPluginReceipt).toHaveBeenCalledWith('sha256:' + 'd'.repeat(64))
  })

  it('a denied dispatch renders an accessible ERROR card (role=alert) with a stable code', async () => {
    const user = userEvent.setup()
    fetchPluginReceipt.mockResolvedValueOnce({ receipt: deniedReceipt })
    renderView()
    await screen.findByRole('article', { name: 'Plugin Deploy Notifier' })
    await user.click(screen.getByRole('button', { name: /Select tool Post note/i }))
    await screen.findByRole('heading', { level: 3, name: 'Post note' })
    const input = screen.getByLabelText(/Look up a dispatch receipt by request digest/i)
    await user.type(input, 'sha256:' + '9'.repeat(64))
    await user.click(screen.getByRole('button', { name: /Look up receipt/i }))

    const alert = await screen.findByRole('alert', { name: /dispatch denied/i })
    expect(within(alert).getAllByText('Denied').length).toBeGreaterThan(0)
    expect(within(alert).getByText('digest_drift')).toBeTruthy()
    expect(within(alert).getByText(/does not match the reviewed catalog/)).toBeTruthy()
  })

  it('a missing receipt (404) renders an accessible error, not a crash', async () => {
    const user = userEvent.setup()
    fetchPluginReceipt.mockRejectedValueOnce(new Error('No receipt is stored for that request digest'))
    renderView()
    await screen.findByRole('article', { name: 'Plugin Deploy Notifier' })
    await user.click(screen.getByRole('button', { name: /Select tool Post note/i }))
    await screen.findByRole('heading', { level: 3, name: 'Post note' })
    await user.type(screen.getByLabelText(/Look up a dispatch receipt/i), 'sha256:' + '1'.repeat(64))
    await user.click(screen.getByRole('button', { name: /Look up receipt/i }))
    expect(await screen.findByRole('alert', { name: /receipt unavailable/i })).toBeTruthy()
  })

  it('Escape closes the tool detail and restores focus to the selecting button', async () => {
    const user = userEvent.setup()
    renderView()
    await screen.findByRole('article', { name: 'Plugin Deploy Notifier' })
    const select = screen.getByRole('button', { name: /Select tool Post note/i })
    await user.click(select)
    await screen.findByRole('heading', { level: 3, name: 'Post note' })
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('heading', { level: 3, name: 'Post note' })).toBeNull())
    await waitFor(() => expect(document.activeElement).toBe(select)) // focus restored
  })
})

describe('a11y — the live region announces load then selection (announce wiring)', () => {
  it('the .pc-live status region carries the loaded count, then the selected tool', async () => {
    const user = userEvent.setup()
    renderView()
    await screen.findByRole('article', { name: 'Plugin Deploy Notifier' })
    // Target the dedicated announce region by class (several transient role=status
    // nodes exist); assert it is a real live region, not a silent div.
    const live = document.querySelector('.pc-live')
    expect(live).toBeTruthy()
    expect(live.getAttribute('role')).toBe('status')
    expect(live.getAttribute('aria-live')).toBe('polite')
    await waitFor(() => expect(live.textContent).toMatch(/Loaded 2 plugins\./))

    await user.click(screen.getByRole('button', { name: /Select tool Post note from Deploy Notifier/i }))
    await waitFor(() => expect(live.textContent).toMatch(/Selected tool Post note\./))
  })
})

describe('503 degrade (NOT-WIRED-LIVE)', () => {
  it('renders a truthful unavailable state when the catalog is 503, while Skills still render — no crash', async () => {
    fetchPlugins.mockRejectedValueOnce(new Error('The plugin catalog is not configured for this hub'))
    renderView()
    expect(await screen.findByText('The plugin catalog is not configured')).toBeTruthy()
    expect(screen.getByRole('button', { name: /retry/i })).toBeTruthy()
    // The bridge-published Skills category (from bootstrap) is unaffected by the 503.
    expect(screen.getByText('lint')).toBeTruthy()
    // The five categories are all still labeled and distinct.
    const headings = screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)
    expect(headings).toContain('Reviewed plugins')
    expect(headings).toContain('Delivery operations')
  })

  it('a load error (non-503) is announced as a distinct error state', async () => {
    fetchPlugins.mockRejectedValueOnce(new Error('The plugin catalog is unavailable'))
    renderView()
    expect(await screen.findByText('The plugin catalog could not be loaded')).toBeTruthy()
  })
})
