import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { ApiError, createJob, getHealth, isActive, type Health } from './api'
import logo from './assets/7pairs-logo.png'
import art from './assets/mahj.png'
import { SafeHarborToggle } from './SafeHarborToggle'
import { StatusPanel } from './StatusPanel'
import { useJob } from './useJob'
import { checkPlaylistUrl } from './validation'

type FormState = { kind: 'idle' } | { kind: 'invalid'; message: string } | { kind: 'submitting' } | { kind: 'error'; message: string }

export default function App() {
  const [url, setUrl] = useState('')
  const [form, setForm] = useState<FormState>({ kind: 'idle' })
  const [health, setHealth] = useState<Health | null>(null)
  const [safeHarbor, setSafeHarbor] = useState(false)
  const { jobId, job, connection, lost, track, clear } = useJob()
  const inputRef = useRef<HTMLInputElement>(null)
  const inFlight = useRef(false)
  const hintId = useId()
  const errorId = useId()

  useEffect(() => {
    const controller = new AbortController()
    void getHealth(controller.signal).then(setHealth)
    return () => controller.abort()
  }, [])

  const jobActive = jobId !== null && (job === null || isActive(job.status))
  const busy = form.kind === 'submitting' || jobActive
  const tracking = jobId !== null

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (busy || inFlight.current) return // duplicate-submission guard
    const check = checkPlaylistUrl(url)
    if (!check.ok) {
      setForm({ kind: 'invalid', message: check.message })
      inputRef.current?.focus()
      return
    }
    inFlight.current = true
    setForm({ kind: 'submitting' })
    try {
      const created = await createJob(check.url, { safeHarbor })
      if (safeHarbor && created.safe_harbor !== true) {
        // An older backend ignores the flag and would deliver unfiltered (possibly explicit) tracks.
        setForm({
          kind: 'error',
          message: "the server didn't confirm safe harbor, so this job may include explicit tracks. restart the backend and try again.",
        })
        return
      }
      track(created)
      setForm({ kind: 'idle' })
    } catch (err) {
      const message = err instanceof ApiError ? err.message : 'something went wrong. try again.'
      const invalid = err instanceof ApiError && err.status === 422
      setForm(invalid ? { kind: 'invalid', message } : { kind: 'error', message })
      inputRef.current?.focus()
    } finally {
      inFlight.current = false
    }
  }

  function reset(keepUrl: boolean) {
    clear()
    if (!keepUrl) setUrl('')
    setForm({ kind: 'idle' })
    requestAnimationFrame(() => inputRef.current?.focus())
  }

  const invalid = form.kind === 'invalid'
  const fieldMessage = form.kind === 'invalid' || form.kind === 'error' ? form.message : null
  const limits = health?.limits

  return (
    <div className="stage">
      <img className="logo" src={logo} alt="7pairs" width={832} height={300} />
      <img className="art" src={art} alt="" aria-hidden="true" width={800} height={1000} />

      <main className="console">
        <h1 className="sr-only">7pairs playlist packager</h1>

        <form className="linkform" onSubmit={submit} noValidate aria-busy={busy}>
          <SafeHarborToggle checked={safeHarbor} onChange={setSafeHarbor} disabled={busy} />

          <label htmlFor="playlist-url" className="sr-only">
            spotify playlist link
          </label>
          <div className={`field${invalid ? ' is-invalid' : ''}${busy ? ' is-busy' : ''}`}>
            <input
              ref={inputRef}
              id="playlist-url"
              name="url"
              type="url"
              inputMode="url"
              autoComplete="off"
              autoCapitalize="off"
              autoCorrect="off"
              spellCheck={false}
              placeholder="paste playlist link here"
              value={url}
              disabled={busy}
              aria-invalid={invalid}
              aria-describedby={fieldMessage ? `${errorId} ${hintId}` : hintId}
              onChange={(e) => {
                setUrl(e.target.value)
                if (form.kind === 'invalid' || form.kind === 'error') setForm({ kind: 'idle' })
              }}
            />
            {(url.trim() || busy) && (
              <button type="submit" className="go" disabled={busy}>
                {form.kind === 'submitting' ? (
                  <>
                    working<span className="caret" aria-hidden="true" />
                  </>
                ) : jobActive ? (
                  'running'
                ) : (
                  <>
                    fetch <span aria-hidden="true">↵</span>
                  </>
                )}
              </button>
            )}
          </div>

          {fieldMessage && (
            <p id={errorId} className="field-msg" role="alert">
              <span className="tag tag-red">
                {form.kind === 'invalid' ? 'err' : 'fail'}
              </span>
              {fieldMessage}
            </p>
          )}
          <p id={hintId} className="specline hint">
            make sure ur playlists are public and i&rsquo;ll do the rest &middot; support your local dj
            {health?.credentials_required && !health.spotify_configured && (
              <>
                {' '}
                &middot; <b className="warn">spotify api not configured</b>
              </>
            )}
            {health && !health.ffmpeg_available && (
              <>
                {' '}
                &middot; <b className="warn">ffmpeg missing</b>
              </>
            )}
          </p>
        </form>

        {lost && !tracking && (
          <p className="field-msg" role="status">
            <span className="tag tag-yellow">lost</span>
            {lost}
          </p>
        )}

        {tracking && (
          <StatusPanel
            jobId={jobId}
            job={job}
            connection={connection}
            expirationHours={limits?.job_expiration_hours}
            onReset={reset}
          />
        )}
      </main>

      <footer className="notice">
        <p>
          all the audio comes from third-party sources and not spotify so we&rsquo;re good there. the metadata comes from
          spotify tho, but i won&rsquo;t tell if you don&rsquo;t.
        </p>
        <p className="stamp">
          format mp3 {limits?.mp3_bitrate ?? 192} kbps &middot; max {limits?.max_playlist_tracks ?? 100} tracks
        </p>
      </footer>
    </div>
  )
}
