// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import type { Health, Job } from './api'
import { createJob } from './api'
import { StatusPanel } from './StatusPanel'
import { SAFE_HARBOR_HELP } from './SafeHarborToggle'

const URL = 'https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M'
const JOB_ID = '0123456789abcdef0123456789abcdef'

const health: Health = {
  spotify_configured: false,
  metadata_source: 'embed',
  credentials_required: false,
  safe_harbor_catalog: false,
  ffmpeg_available: true,
  limits: { max_playlist_tracks: 100, mp3_bitrate: 192, max_concurrent_jobs: 2, job_expiration_hours: 2 },
}

function job(overrides: Partial<Job> = {}): Job {
  return {
    job_id: JOB_ID,
    status: 'processing',
    phase: 'downloading',
    safe_harbor: false,
    playlist_title: 'Road Trip',
    total_tracks: 3,
    processed_tracks: 1,
    completed_tracks: 1,
    failed_tracks: 0,
    skipped_tracks: 0,
    clean_unavailable_tracks: 0,
    clean_summary: {},
    current_track: 'Artist - Song',
    progress: 33,
    error: null,
    notice: null,
    failures: [],
    download_ready: false,
    zip_name: null,
    zip_size_bytes: null,
    created_at: '2026-09-24T00:00:00Z',
    expires_at: null,
    ...overrides,
  }
}

type Call = { url: string; body: unknown }

function mockServer(jobForBody: (body: { safe_harbor?: boolean }) => Job) {
  const calls: Call[] = []
  let current: Job | null = null
  const json = (data: unknown, status = 200) =>
    new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } })
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const body = init?.body ? JSON.parse(String(init.body)) : undefined
      calls.push({ url, body })
      if (url === '/api/health') return json(health)
      if (url === '/api/jobs' && init?.method === 'POST') {
        current = jobForBody(body)
        return json(current, 202)
      }
      if (url.startsWith('/api/jobs/') && current) return json(current)
      return json({ error: 'not found' }, 404)
    }),
  )
  return calls
}

beforeEach(() => {
  window.localStorage.clear()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('Safe Harbor toggle', () => {
  it('is an accessible switch, off by default, with helper text', async () => {
    mockServer(() => job())
    render(<App />)
    const toggle = screen.getByRole<HTMLInputElement>('switch', { name: 'safe harbor' })
    expect(toggle.checked).toBe(false)
    const describedBy = toggle.getAttribute('aria-describedby') ?? ''
    expect(document.getElementById(describedBy)?.textContent).toBe(SAFE_HARBOR_HELP)
  })

  it('shows the help as a tooltip that Escape dismisses', async () => {
    mockServer(() => job())
    const user = userEvent.setup()
    render(<App />)
    const tip = screen.getByRole('tooltip', { hidden: true })
    expect(tip.textContent).toBe(SAFE_HARBOR_HELP)
    const wrapper = tip.closest('.safe-harbor') as HTMLElement
    screen.getByRole('switch', { name: 'safe harbor' }).focus()
    await user.keyboard('{Escape}')
    expect(wrapper.classList.contains('tip-dismissed')).toBe(true)
    await user.hover(wrapper)
    await user.unhover(wrapper)
    expect(wrapper.classList.contains('tip-dismissed')).toBe(false)
  })

  it('toggles with a click and with the keyboard', async () => {
    mockServer(() => job())
    const user = userEvent.setup()
    render(<App />)
    const toggle = screen.getByRole<HTMLInputElement>('switch', { name: 'safe harbor' })
    await user.click(toggle)
    expect(toggle.checked).toBe(true)
    toggle.focus()
    await user.keyboard(' ')
    expect(toggle.checked).toBe(false)
  })

  it('sends safe_harbor: true and shows the indicator while processing', async () => {
    const calls = mockServer((body) => job({ safe_harbor: body.safe_harbor === true }))
    const user = userEvent.setup()
    render(<App />)
    await user.click(screen.getByRole('switch', { name: 'safe harbor' }))
    await user.type(screen.getByRole('textbox'), URL)
    await user.keyboard('{Enter}')

    const post = calls.find((c) => c.url === '/api/jobs')
    expect(post?.body).toEqual({ url: URL, safe_harbor: true })
    expect(await screen.findByText('safe harbor enabled')).toBeTruthy()
    // the toggle cannot change while the job is active
    expect(screen.getByRole<HTMLInputElement>('switch', { name: 'safe harbor' }).disabled).toBe(true)
  })

  it('refuses to track a job when the server does not confirm safe harbor', async () => {
    // An older backend drops the unknown field and returns no safe_harbor at all.
    mockServer(() => {
      const { safe_harbor: _dropped, ...legacy } = job()
      void _dropped
      return legacy as Job
    })
    const user = userEvent.setup()
    render(<App />)
    await user.click(screen.getByRole('switch', { name: 'safe harbor' }))
    await user.type(screen.getByRole('textbox'), URL)
    await user.keyboard('{Enter}')
    expect((await screen.findByRole('alert')).textContent).toContain("didn't confirm safe harbor")
    expect(screen.queryByText('road trip', { exact: false })).toBeNull()
  })

  it('sends safe_harbor: false when left off and shows no indicator', async () => {
    const calls = mockServer((body) => job({ safe_harbor: body.safe_harbor === true }))
    const user = userEvent.setup()
    render(<App />)
    await user.type(screen.getByRole('textbox'), URL)
    await user.keyboard('{Enter}')
    await waitFor(() => expect(calls.some((c) => c.url === '/api/jobs')).toBe(true))
    expect(calls.find((c) => c.url === '/api/jobs')?.body).toEqual({ url: URL, safe_harbor: false })
    expect(await screen.findByText('road trip', { exact: false })).toBeTruthy()
    expect(screen.queryByText('safe harbor enabled')).toBeNull()
  })
})

describe('Safe Harbor results', () => {
  it('lists tracks with no clean version separately from other failures', () => {
    const result = job({
      status: 'partial',
      phase: 'partial',
      safe_harbor: true,
      processed_tracks: 3,
      completed_tracks: 1,
      failed_tracks: 2,
      clean_unavailable_tracks: 1,
      clean_summary: { clean_matched: 1, clean_unavailable: 1, download_failed: 1 },
      current_track: null,
      progress: 100,
      download_ready: true,
      zip_name: 'Road Trip.zip',
      zip_size_bytes: 1024,
      failures: [
        { position: 2, track: 'Rapper - Bad Words', reason: 'clean version unavailable', clean_status: 'clean_unavailable' },
        { position: 3, track: 'Band - Broken', reason: 'mp3 conversion failed', clean_status: 'download_failed' },
      ],
    })
    render(<StatusPanel jobId={JOB_ID} job={result} connection="ok" onReset={() => {}} />)

    const cleanList = screen.getByText(/no clean version found/).closest('details')
    const otherList = screen.getByText(/not included/).closest('details')
    expect(cleanList).not.toBeNull()
    expect(otherList).not.toBeNull()
    expect(within(cleanList as HTMLElement).getByText('Rapper - Bad Words')).toBeTruthy()
    expect(within(cleanList as HTMLElement).queryByText('Band - Broken')).toBeNull()
    expect(within(otherList as HTMLElement).getByText('Band - Broken')).toBeTruthy()
    expect(screen.getByText('safe harbor enabled')).toBeTruthy()
    expect(screen.getByLabelText('safe harbor results').textContent).toContain('no clean')
    expect(screen.getByText(/1 skipped: no verified clean version/)).toBeTruthy()
  })

  it('shows no clean-version list for normal jobs', () => {
    const result = job({
      status: 'partial',
      phase: 'partial',
      failed_tracks: 1,
      current_track: null,
      download_ready: true,
      failures: [{ position: 2, track: 'A - B', reason: 'no confident match', clean_status: null }],
    })
    render(<StatusPanel jobId={JOB_ID} job={result} connection="ok" onReset={() => {}} />)
    expect(screen.queryByText(/no clean version found/)).toBeNull()
    expect(screen.queryByLabelText('safe harbor results')).toBeNull()
    expect(screen.getByText(/not included/)).toBeTruthy()
  })
})

describe('createJob', () => {
  it('defaults safe_harbor to false in the request body', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(job()), { status: 202 }))
    vi.stubGlobal('fetch', fetchMock)
    await createJob(URL)
    const init = (fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1]
    expect(JSON.parse(String(init.body))).toEqual({ url: URL, safe_harbor: false })
  })
})
