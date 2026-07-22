import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ModelHealthIndicator from './model-health-indicator'
import { fetchModelHealth } from './api'

vi.mock('./api', () => ({ fetchModelHealth: vi.fn() }))

const SNAPSHOT = {
  schema_version: 'workbench-model-health/v1',
  checked_at: '2026-07-22T10:00:00Z',
  source_note: 'Per-tier and OCR status is derived from recent Anvil Serving routing decisions, not a live per-tier probe. Voice reflects audio-gateway registration.',
  components: [
    { id: 'router', label: 'Router', status: 'ok', detail: 'Router healthy; audio routes registered.' },
    { id: 'heavy', label: 'Heavy', status: 'ok', detail: 'Served on the most recent attempt. (from recent routing)', last_seen: '2026-07-22T10:00:00Z' },
    { id: 'fast', label: 'Fast', status: 'down', detail: 'Unavailable on the most recent attempt. (from recent routing)' },
    { id: 'voice', label: 'Voice', status: 'ok', detail: 'Audio gateway registered (voice path liveness).' },
    { id: 'ocr', label: 'OCR', status: 'idle', detail: 'No recent OCR routing.' },
  ],
}

beforeEach(() => { fetchModelHealth.mockReset() })
afterEach(() => { vi.useRealTimers() })

describe('ModelHealthIndicator', () => {
  it('renders five status dots from the endpoint', async () => {
    fetchModelHealth.mockResolvedValue(SNAPSHOT)
    render(<ModelHealthIndicator />)
    await waitFor(() => expect(fetchModelHealth).toHaveBeenCalled())
    // The cluster button's aria-label carries every component's status (a11y).
    await waitFor(() => {
      const cluster = screen.getByRole('button', { name: /Backend health:/ })
      expect(cluster.getAttribute('aria-label')).toMatch(/Router ok.*Heavy ok.*Fast down.*Voice ok.*OCR idle/)
    })
  })

  it('opens the popover and reflects each status with a non-color glyph cue', async () => {
    fetchModelHealth.mockResolvedValue(SNAPSHOT)
    render(<ModelHealthIndicator />)
    await waitFor(() => expect(fetchModelHealth).toHaveBeenCalled())

    fireEvent.click(screen.getByRole('button', { name: /Backend health:/ }))
    const dialog = await screen.findByRole('dialog', { name: 'Backend health detail' })

    // Each component's status WORD is shown (redundant with color, readable text).
    expect(within(dialog).getByText('down')).toBeTruthy()
    expect(within(dialog).getByText('idle')).toBeTruthy()
    // The colorblind-safe glyph cue is present (× for down, · for idle, ✓ for ok).
    expect(within(dialog).getAllByText('×').length).toBeGreaterThan(0)
    expect(within(dialog).getAllByText('✓').length).toBeGreaterThan(0)
    // The honesty note ("from recent routing", not a live probe) is surfaced.
    expect(within(dialog).getByText(/not a live per-tier probe/)).toBeTruthy()
    expect(within(dialog).getByText(/last seen 2026-07-22T10:00:00Z/)).toBeTruthy()
  })

  it('refreshes when the popover opens and on the manual Refresh control', async () => {
    fetchModelHealth.mockResolvedValue(SNAPSHOT)
    render(<ModelHealthIndicator />)
    await waitFor(() => expect(fetchModelHealth).toHaveBeenCalledTimes(1)) // initial poll

    fireEvent.click(screen.getByRole('button', { name: /Backend health:/ })) // open -> refresh
    await waitFor(() => expect(fetchModelHealth).toHaveBeenCalledTimes(2))

    fireEvent.click(await screen.findByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(fetchModelHealth).toHaveBeenCalledTimes(3))
  })

  it('polls on an interval', async () => {
    vi.useFakeTimers()
    fetchModelHealth.mockResolvedValue(SNAPSHOT)
    render(<ModelHealthIndicator />)
    await vi.waitFor(() => expect(fetchModelHealth).toHaveBeenCalledTimes(1))
    await vi.advanceTimersByTimeAsync(20000)
    expect(fetchModelHealth).toHaveBeenCalledTimes(2)
  })

  it('degrades quietly to all-unknown when the endpoint is unavailable', async () => {
    fetchModelHealth.mockRejectedValue(new Error('Model health is unavailable'))
    render(<ModelHealthIndicator />)
    await waitFor(() => expect(fetchModelHealth).toHaveBeenCalled())
    // Still renders the cluster (on every view) — all dots read "unknown", no toast.
    await waitFor(() => {
      const cluster = screen.getByRole('button', { name: /Backend health:/ })
      expect(cluster.getAttribute('aria-label')).toMatch(/Router unknown.*Heavy unknown.*Fast unknown.*Voice unknown.*OCR unknown/)
    })
    fireEvent.click(screen.getByRole('button', { name: /Backend health:/ }))
    const dialog = await screen.findByRole('dialog', { name: 'Backend health detail' })
    expect(within(dialog).getAllByText('unknown').length).toBe(5)
  })
})
