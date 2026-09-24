export type JobStatus = 'queued' | 'processing' | 'complete' | 'partial' | 'failed'

export interface TrackFailure {
  position: number
  track: string
  reason: string
}

export interface Job {
  job_id: string
  status: JobStatus
  phase: string
  playlist_title: string | null
  total_tracks: number
  processed_tracks: number
  completed_tracks: number
  failed_tracks: number
  skipped_tracks: number
  current_track: string | null
  progress: number
  error: string | null
  /** Non-fatal caveat, e.g. the keyless source may have truncated the playlist. */
  notice: string | null
  failures: TrackFailure[]
  download_ready: boolean
  zip_name: string | null
  zip_size_bytes: number | null
  created_at: string
  expires_at: string | null
}

export interface Health {
  spotify_configured: boolean
  /** 'embed' = keyless public player, 'api' = Spotify Web API with credentials. */
  metadata_source: 'embed' | 'api'
  credentials_required: boolean
  ffmpeg_available: boolean
  limits: {
    max_playlist_tracks: number
    mp3_bitrate: number
    max_concurrent_jobs: number
    job_expiration_hours: number
  }
}

export class ApiError extends Error {
  readonly status: number
  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

async function parse<T>(res: Response): Promise<T> {
  let body: unknown = null
  try {
    body = await res.json()
  } catch {
    // non-JSON (e.g. proxy error page)
  }
  if (!res.ok) {
    const message =
      body && typeof body === 'object' && 'error' in body && typeof body.error === 'string'
        ? body.error
        : res.status >= 500
          ? 'the server is unavailable. try again shortly.'
          : `request failed (${res.status}).`
    throw new ApiError(message, res.status)
  }
  return body as T
}

function networkError(): ApiError {
  return new ApiError('could not reach the server. check your connection.', 0)
}

export async function createJob(url: string, signal?: AbortSignal): Promise<Job> {
  let res: Response
  try {
    res = await fetch('/api/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
      signal,
    })
  } catch (err) {
    if ((err as Error).name === 'AbortError') throw err
    throw networkError()
  }
  return parse<Job>(res)
}

export async function getJob(jobId: string, signal?: AbortSignal): Promise<Job> {
  let res: Response
  try {
    res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, { signal, cache: 'no-store' })
  } catch (err) {
    if ((err as Error).name === 'AbortError') throw err
    throw networkError()
  }
  return parse<Job>(res)
}

export async function getHealth(signal?: AbortSignal): Promise<Health | null> {
  try {
    const res = await fetch('/api/health', { signal })
    return res.ok ? ((await res.json()) as Health) : null
  } catch {
    return null
  }
}

export function downloadUrl(jobId: string): string {
  return `/api/jobs/${encodeURIComponent(jobId)}/download`
}

export const isActive = (s: JobStatus) => s === 'queued' || s === 'processing'
