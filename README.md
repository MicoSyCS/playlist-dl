# 7pairs — playlist packager

Paste a public Spotify playlist link; get back one ZIP of ordered, tagged MP3 files.

**For content you own or are authorized to download.** Spotify is used only as a
*metadata* source (titles, artists, duration, order). **No API key or Spotify account is
needed**: by default the track list is read from Spotify's public embed player. The
official Web API is optional. Audio is located by searching a [yt-dlp](https://github.com/yt-dlp/yt-dlp)-supported
site for each track, then converted with FFmpeg. Nothing is extracted from Spotify's
streams and no DRM is circumvented.

```
browser ──▶ web (React/Vite, nginx) ──/api──▶ api (FastAPI)
                                               ├─ Spotify embed player (keyless) or Web API (optional)
                                               ├─ yt-dlp           (search + best audio)
                                               ├─ FFmpeg           (MP3 + ID3 tags)
                                               └─ DATA_DIR/<job>/  (ZIP, deleted on expiry)
```

---

## Where the track list comes from

`SPOTIFY_METADATA_SOURCE` picks the source. The default is `auto`.

| Source | Needs a key? | Limits |
|---|---|---|
| **Embed player** (`embed`, and `auto` without credentials) | **No.** It reads the JSON that `open.spotify.com/embed/playlist/{id}` serves to anonymous visitors. | **Max 100 tracks.** The true total isn't exposed, so when exactly 100 come back the job shows a "may be truncated" note, which is also written to `failed-tracks.txt`. No album names. **Unofficial:** Spotify can change the page at any time, and automated access may conflict with Spotify's terms of use. |
| **Web API** (`api`, and `auto` with credentials) | Client ID and secret from the [developer dashboard](https://developer.spotify.com/dashboard) | Official and stable, with album names and full pagination. Since **February 2026**, **Development Mode** apps get playlist *metadata* but **not the tracks** of playlists they don't own, and the owner needs Premium. Reading any public playlist requires an **Extended Quota** app ([migration guide](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide)). In `auto` mode that refusal falls back to the embed player automatically. |

Neither source logs into a Spotify account. Private playlists are **not supported**
(that would require user OAuth).

---

## Quick start (Docker Compose)

```bash
cp .env.example .env          # works as-is (keyless); Spotify credentials are optional
docker compose up --build
```

Open <http://localhost:8080>. The images include FFmpeg and Deno, so nothing else is needed.
Downloads live in the `downloads` named volume.

## Local development

Prerequisites: **Python 3.12+**, **Node 20+**, **FFmpeg** on `PATH`, and preferably
**Deno** (see below).

### Installing FFmpeg

| OS | Command |
|---|---|
| Windows | `winget install Gyan.FFmpeg` (or download from <https://www.gyan.dev/ffmpeg/builds/>) |
| macOS | `brew install ffmpeg` |
| Debian/Ubuntu | `sudo apt install ffmpeg` |

Or point `FFMPEG_PATH` at any ffmpeg binary. Check with `ffmpeg -version`.

### yt-dlp and Deno

yt-dlp is installed from `requirements.txt` (`yt-dlp[default]`). Current yt-dlp releases
need an external JavaScript runtime to download from YouTube. Install **Deno**
(<https://docs.deno.com/runtime/getting_started/installation/>: `winget install DenoLand.Deno`,
`brew install deno`, or `curl -fsSL https://deno.land/install.sh | sh`). Search works
without it, but downloads may fail. Sites change often, so keep yt-dlp current:
`pip install -U "yt-dlp[default]"`.

### Backend

```bash
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
cp ../.env.example ../.env              # optional: Spotify credentials
uvicorn app.main:app --reload --port 8000
```

The backend reads `.env` from `backend/` or the repository root.

### Frontend

```bash
cd frontend
npm install
npm run dev                             # http://localhost:5173, proxies /api to :8000
```

`VITE_API_PROXY` overrides the proxy target. The browser only ever talks to `/api`,
and no credentials are compiled into the bundle.

### Checks

```bash
# backend (from backend/)
ruff check . && ruff format --check .
mypy app
pytest

# frontend (from frontend/)
npm run lint
npm run typecheck
npm test
npm run build
```

Tests cover URL validation, filename sanitization and ZIP safety, playlist parsing for both
Web API schemas (including pagination, rate limits, 404s and metadata-only responses) and for
the keyless embed page (artist splitting, the 100-track cap, format changes, refused redirects,
API-to-embed fallback),
candidate matching, FFMETADATA escaping, and job status behavior (complete, partial and
failed states, duplicate submissions, capacity limits, expiry and orphan cleanup, and the
HTTP API).

---

## Configuration

All settings are environment variables. See [.env.example](.env.example).

| Variable | Default | Purpose |
|---|---|---|
| `SPOTIFY_METADATA_SOURCE` | `auto` | `auto`, `embed` (keyless) or `api` |
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` | — | Optional server-side Web API credentials |
| `SPOTIFY_MARKET` | `US` | Market for availability/relinking |
| `MAX_PLAYLIST_TRACKS` | `100` | Web API: larger playlists are rejected before any download. Embed: the first N tracks, capped at 100 by Spotify. |
| `MP3_BITRATE` | `192` | kbps, 64–320 |
| `MAX_CONCURRENT_JOBS` | `2` | Playlists processed at once |
| `MAX_QUEUED_JOBS` | `10` | Waiting jobs before new submissions get HTTP 429 |
| `TRACK_CONCURRENCY` | `2` | Parallel tracks per job |
| `TRACK_TIMEOUT_SECONDS` | `240` | Search + download + convert budget per track |
| `JOB_TIMEOUT_MINUTES` | `90` | Whole-job budget; remaining tracks are marked failed |
| `JOB_EXPIRATION_HOURS` | `2` | ZIP and job record deleted this long after finishing |
| `CLEANUP_INTERVAL_SECONDS` | `300` | Janitor period (also removes orphaned job directories) |
| `MAX_DOWNLOAD_MB` | `80` | Per-track source size cap |
| `SEARCH_PREFIX` | `ytsearch` | Any yt-dlp search prefix, e.g. `scsearch` |
| `SEARCH_RESULTS` | `6` | Candidates scored per track |
| `DATA_DIR` | `./data` | Job working directory |
| `FFMPEG_PATH` | `ffmpeg` | FFmpeg executable |
| `CORS_ORIGINS` | *(empty)* | Only needed when the UI is served from another origin |

## API

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/jobs` | Body `{"url": "https://open.spotify.com/playlist/…"}` → `202` + job. Re-submitting a playlist that is already running returns the same job. |
| `GET` | `/api/jobs/{job_id}` | `status` (`queued`, `processing`, `complete`, `partial`, `failed`), `phase`, `playlist_title`, `total_tracks`, `processed_tracks`, `completed_tracks`, `failed_tracks`, `skipped_tracks`, `current_track`, `progress`, `error`, `notice`, `failures[]`, `download_ready`, `zip_name`, `zip_size_bytes`, `expires_at` |
| `GET` | `/api/jobs/{job_id}/download` | The ZIP. Returns `409` until the job is `complete` or `partial`. |
| `GET` | `/api/health` | Metadata source, whether credentials and FFmpeg are available, and the public limits. No secrets. |

Errors are always `{"error": "<user-friendly message>"}`. Stack traces stay in the server log.
The UI polls every 1.5 s and resumes after a page reload.

### Output

- `01 - Artist - Title.mp3` … in playlist order. Zero-padding grows for 100+ tracks, and name collisions get ` (2)`.
- ID3v2.3 and ID3v1 tags: title, artists, album artist, album (Web API source only), and track `n/total`.
- `failed-tracks.txt` lists every row not included, with a reason: no confident match,
  download or conversion failure, podcast episode, local file, removed track, or duplicate.
- The ZIP is named after the sanitized playlist title.

## How matching works

For each track the query is `Primary Artist - Title`. "Remaster", "feat." and similar
suffixes are stripped, and the album is added when the title is a single generic word.
Every candidate is scored on title-token coverage, artist presence and duration agreement
(tolerance: 12% or 15 s, whichever is larger). Karaoke, cover, nightcore, sped-up/slowed,
8D and reaction uploads are rejected unless Spotify's own title says so. Live, remix and
extended versions are penalized. YouTube "Topic" channels and "official audio" uploads get
a small bonus. If nothing clears the threshold, the track is reported as unmatched rather
than guessed.

---

## Production deployment

- **Needs a long-running host.** Each job runs for minutes, spawns FFmpeg and writes
  hundreds of MB to disk. **Many serverless platforms are unsuitable**, including Vercel/Netlify
  functions, AWS Lambda and Cloud Run without always-on CPU. Their request or execution time
  limits, ephemeral or read-only filesystems, and lack of background execution break the
  pipeline. Use a VM, a container host (ECS, Fly.io Machines, Railway, a VPS with Docker)
  or Kubernetes.
- **Persistent, sized storage.** Mount `DATA_DIR` on a persistent volume with room for about
  `MAX_CONCURRENT_JOBS × MAX_PLAYLIST_TRACKS × 10 MB`, plus finished ZIPs until they expire.
  To scale beyond one machine, upload finished ZIPs to object storage (S3, GCS, R2) and serve
  pre-signed URLs from the download endpoint.
- **Run a single API process** (`--workers 1`, as in the Dockerfile). Job state is in memory.
  Horizontal scaling needs a shared store and queue (Redis with RQ/Celery, for example) plus
  shared or object storage. A restart loses in-flight jobs, and the janitor removes their
  orphaned files on startup.
- **Put TLS and rate limiting in front** (nginx, Caddy or a cloud load balancer). The bundled
  nginx config sets a strict CSP, proxies `/api` and streams ZIPs unbuffered.
- **Keep concurrency conservative.** Audio sources rate-limit aggressive clients and FFmpeg
  is CPU-bound. The defaults (2 jobs × 2 tracks) suit a 2-vCPU box.
- **Update yt-dlp regularly** by rebuilding the image. Extractors break as sites change.
- Logs go to stdout: job lifecycle, per-track match and failure reasons, and Spotify or yt-dlp errors.

## Security notes

- The playlist URL is parsed with a strict pattern (`https`, host `open.spotify.com`, no
  credentials or port, 22-character base62 ID) and **never fetched as given**. The server only
  requests URLs it builds itself: `open.spotify.com/embed/playlist/{id}` (redirects refused,
  5 MB cap) or the fixed Web API hosts, whose pagination links are checked to stay on `api.spotify.com`.
- yt-dlp is used through its Python API. FFmpeg runs with an argument list, `shell=False`,
  a timeout, fixed ASCII filenames inside a per-track work directory, and tags supplied via
  an FFMETADATA file.
- Every generated filename is sanitized: separators, control characters and reserved
  device names are removed. Paths are joined with an escape check, and ZIP members are
  validated as flat names, so there is no path traversal or zip-slip. Job IDs are 128-bit
  random hex and validated before lookup.
- Spotify credentials, if used, stay on the server. `/api/health` reports only whether they are set.

## Limitations

- **Spotify supplies metadata only; matching by search is imperfect.** A track can match a
  different recording or edit, or no upload at all, especially for obscure, regional or
  instrumental music. Check `failed-tracks.txt`.
- Keyless mode reads at most 100 tracks and depends on an undocumented page that Spotify can
  change without notice. If it breaks, jobs fail with "the page format may have changed" until
  the parser is updated, or you can configure Web API credentials.
- Development-mode Spotify apps cannot read other users' playlist tracks through the API (see above).
  Private playlists are unsupported.
- Audio quality depends on the source. Converting a lossy source to MP3 cannot improve it.
- A running track cannot be killed mid-flight. Timeouts are enforced through yt-dlp progress
  hooks, socket timeouts and the FFmpeg subprocess timeout.
- Jobs are in memory and don't survive an API restart.
