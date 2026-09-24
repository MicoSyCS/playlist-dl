import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, getJob, isActive, type Job } from './api'

const STORAGE_KEY = '7p.job'
const POLL_MS = 1500
const MAX_BACKOFF_MS = 10_000

function readStoredJobId(): string | null {
  try {
    const v = window.localStorage.getItem(STORAGE_KEY)
    return v && /^[a-f0-9]{32}$/.test(v) ? v : null
  } catch {
    return null
  }
}

function storeJobId(id: string | null): void {
  try {
    if (id) window.localStorage.setItem(STORAGE_KEY, id)
    else window.localStorage.removeItem(STORAGE_KEY)
  } catch {
    // storage unavailable (private mode); resuming after reload just won't work
  }
}

export type Connection = 'ok' | 'retrying'

/**
 * Tracks one job by polling GET /api/jobs/{id} until it reaches a terminal state.
 * The job id is remembered locally so a reload resumes progress.
 */
export function useJob() {
  const [jobId, setJobId] = useState<string | null>(readStoredJobId)
  const [job, setJob] = useState<Job | null>(null)
  const [connection, setConnection] = useState<Connection>('ok')
  const [lost, setLost] = useState<string | null>(null)
  const failures = useRef(0)

  useEffect(() => {
    if (!jobId) return
    const controller = new AbortController()
    let timer: number | undefined

    const tick = async () => {
      try {
        const next = await getJob(jobId, controller.signal)
        failures.current = 0
        setConnection('ok')
        setJob(next)
        if (isActive(next.status)) timer = window.setTimeout(tick, POLL_MS)
      } catch (err) {
        if (controller.signal.aborted) return
        if (err instanceof ApiError && err.status === 404) {
          storeJobId(null)
          setJob(null)
          setJobId(null)
          setLost('that job has expired or the server restarted. submit the playlist again.')
          return
        }
        failures.current += 1
        setConnection('retrying')
        const delay = Math.min(MAX_BACKOFF_MS, POLL_MS * 2 ** Math.min(failures.current, 4))
        timer = window.setTimeout(tick, delay)
      }
    }
    void tick()
    return () => {
      controller.abort()
      window.clearTimeout(timer)
    }
  }, [jobId])

  const track = useCallback((initial: Job) => {
    storeJobId(initial.job_id)
    setLost(null)
    setJob(initial)
    setJobId(initial.job_id)
  }, [])

  const clear = useCallback(() => {
    storeJobId(null)
    setJob(null)
    setJobId(null)
    setLost(null)
    setConnection('ok')
  }, [])

  return { jobId, job, connection, lost, track, clear }
}
