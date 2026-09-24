import { useEffect, useRef } from 'react'
import { downloadUrl, isActive, type Job } from './api'
import { formatBytes, jobRef, pad, trackProgressLabel } from './format'
import type { Connection } from './useJob'

interface Props {
  jobId: string
  job: Job | null
  connection: Connection
  expirationHours?: number
  onReset: (keepUrl: boolean) => void
}

const TAG: Record<Job['status'], { label: string; cls: string }> = {
  queued: { label: 'queued', cls: 'tag-dim' },
  processing: { label: 'running', cls: 'tag-white' },
  complete: { label: 'complete', cls: 'tag-white' },
  partial: { label: 'partial', cls: 'tag-yellow' },
  failed: { label: 'failed', cls: 'tag-red' },
}

export function StatusPanel({ jobId, job, connection, expirationHours, onReset }: Props) {
  const downloadRef = useRef<HTMLAnchorElement>(null)
  const wasActive = useRef(false)

  const status = job?.status ?? 'queued'
  const phase = job?.phase ?? 'connecting'
  const total = job?.total_tracks ?? 0
  const processed = job?.processed_tracks ?? 0
  const progress = Math.round(job?.progress ?? 0)
  const active = isActive(status)
  const width = Math.max(2, `${total}`.length)

  // The live region text depends only on status/phase, so screen readers hear each
  // transition once rather than every progress tick.
  let announcement = ''
  if (job?.status === 'complete') announcement = `complete. ${job.completed_tracks} tracks ready to download.`
  else if (job?.status === 'partial')
    announcement = `finished with ${job.failed_tracks} failed tracks. ${job.completed_tracks} tracks ready to download.`
  else if (job?.status === 'failed') announcement = `failed. ${job.error ?? ''}`
  else if (job) announcement = `${job.status}: ${job.phase}`

  // Move focus to the download link when the archive becomes ready, unless the user is elsewhere.
  const ready = job?.download_ready ?? false
  useEffect(() => {
    if (!ready) return
    if (wasActive.current && document.activeElement === document.body) downloadRef.current?.focus()
  }, [ready])
  useEffect(() => {
    wasActive.current = active
  }, [active])

  const headingId = `job-${jobId}-title`

  return (
    <section className={`panel is-${status}`} aria-labelledby={headingId}>
      <div className="sr-only" role="status" aria-live="polite">
        {announcement}
      </div>

      <header className="panel-head">
        <span className={`tag ${TAG[status].cls}`}>
          {TAG[status].label}
          {active && <span className="caret" aria-hidden="true" />}
        </span>
        <span className="meta">
          {active ? phase : 'job'} &middot; {jobRef(jobId)}
        </span>
        {connection === 'retrying' && <span className="meta warn">signal lost &middot; retrying</span>}
      </header>

      <h2 id={headingId} className="panel-title">
        {job?.playlist_title ??
          (!job ? 'resuming job' : status === 'failed' ? 'playlist unavailable' : 'reading playlist')}
      </h2>

      {status !== 'failed' && (
        <>
          <div
            className="meter"
            role="progressbar"
            aria-label="tracks processed"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={progress}
            aria-valuetext={trackProgressLabel(processed, total)}
          >
            <div className="meter-fill" style={{ width: `${progress}%` }} />
          </div>
          <div className="counts">
            <span>{trackProgressLabel(processed, total)}</span>
            <span className="pct">{pad(progress, 3)}%</span>
          </div>
        </>
      )}

      {active && (
        <p className="now">
          <span className="label">now</span>
          <span className="now-track">
            {job?.current_track ?? (phase === 'packaging' ? 'building archive' : 'waiting for a worker')}
          </span>
        </p>
      )}

      {job && total > 0 && (
        <p className="specline">
          <b>ok</b> {pad(job.completed_tracks, width)} &middot; <b>fail</b> {pad(job.failed_tracks, width)} &middot;{' '}
          <b>skip</b> {pad(job.skipped_tracks, width)} &middot; <b>total</b> {pad(total, width)}
        </p>
      )}

      {status === 'failed' && job?.error && (
        <p className="panel-error" role="alert">
          {job.error}
        </p>
      )}

      {job?.download_ready && (
        <a ref={downloadRef} className="dl" href={downloadUrl(jobId)} download={job.zip_name ?? undefined}>
          <span className="dl-label">download zip</span>
          <span className="dl-meta">
            {job.zip_name} &middot; {formatBytes(job.zip_size_bytes)}
          </span>
          <span className="dl-arrow" aria-hidden="true">
            ↓
          </span>
        </a>
      )}

      {job?.notice && !active && (
        <p className="note">
          <span className="tag tag-yellow">note</span> {job.notice}
        </p>
      )}

      {status === 'partial' && (
        <p className="note">
          {job?.failed_tracks} {job?.failed_tracks === 1 ? 'track was' : 'tracks were'} not matched or failed. see
          failed-tracks.txt inside the archive.
        </p>
      )}

      {job && job.failures.length > 0 && !active && (
        <details className="failures">
          <summary>
            {pad(job.failures.length, width)} not included <span aria-hidden="true">+</span>
          </summary>
          <ol>
            {job.failures.map((f) => (
              <li key={f.position}>
                <span className="pos">{pad(f.position, width)}</span>
                <span className="trk">{f.track}</span>
                <span className="why">{f.reason}</span>
              </li>
            ))}
          </ol>
        </details>
      )}

      {!active && (
        <div className="panel-foot">
          <button type="button" className="btn" onClick={() => onReset(status === 'failed')}>
            {status === 'failed' ? 'try again' : 'new playlist'}
          </button>
          {job?.download_ready && (
            <span className="meta">archive deleted after {expirationHours ?? 2} h</span>
          )}
        </div>
      )}
    </section>
  )
}
