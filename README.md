# 7pairs playlist downloader

Paste a public Spotify playlist link and get back one ZIP of ordered, tagged MP3 files.

Only download content you own or are authorized to download.

## How it works

1. You paste a public Spotify playlist URL.
2. The app reads the playlist's track list from Spotify. Spotify is used **only for metadata**: titles, artists, durations and order.
3. For each track, it searches for the audio with [yt-dlp](https://github.com/yt-dlp/yt-dlp).
4. It converts each match to MP3 with FFmpeg and adds tags (title, artist, track number).
5. You download a ZIP named after the playlist, with the files in playlist order.

The app never downloads audio from Spotify and does not bypass DRM.

You don't need a Spotify account or API key. Without API credentials, playlists are limited to their first 100 tracks.

Tracks that can't be matched are listed in `failed-tracks.txt` inside the ZIP.

## Quick start with Docker

You need [Docker](https://docs.docker.com/get-docker/). The images include FFmpeg and everything else.

```bash
cp .env.example .env
docker compose up --build
```

Open **http://localhost:8080**.

The first build takes a few minutes. Press `Ctrl+C` to stop.

## How to use Safe Harbor

Safe Harbor looks for clean or radio-edit versions of each song.

1. Turn on the **safe harbor** switch above the playlist field.
2. Paste your playlist link and press Enter.
3. Download the ZIP. It's named `Playlist Name (Clean).zip`.

Songs without a confident clean match are skipped, never replaced with the explicit version. They're listed under "no clean version found" and in `failed-tracks.txt`.

Safe Harbor is best effort. It relies on Spotify's explicit flag and on labels like "clean" or "radio edit". It does not analyze lyrics.

## Optional Spotify API credentials

The app works without credentials. Adding them lets you:

- read playlists longer than 100 tracks
- add album names to the MP3 tags
- let Safe Harbor check Spotify's catalog for official clean versions

To add them:

1. Create an app at the [Spotify developer dashboard](https://developer.spotify.com/dashboard).
2. Copy its client ID and secret into `.env`:

   ```
   SPOTIFY_CLIENT_ID=your-client-id
   SPOTIFY_CLIENT_SECRET=your-client-secret
   ```

3. Restart the app.

New Spotify apps may be blocked from reading other people's playlists. When that happens, the app falls back to keyless mode automatically.

Other useful settings in `.env`:

- `MAX_PLAYLIST_TRACKS` sets the largest playlist accepted. Default: `100`.
- `MP3_BITRATE` sets the MP3 quality in kbps. Default: `192`.
- `MAX_CONCURRENT_JOBS` sets how many playlists are processed at once. Default: `2`.
- `JOB_EXPIRATION_HOURS` sets how long finished ZIPs are kept on the server. Default: `2`.

## Local development

Requirements:

- Python 3.12+
- Node 22.12+
- FFmpeg (on your `PATH`)
- Deno 2.3+ (yt-dlp uses it for YouTube downloads)

Copy the settings file once, from the project root:

```bash
cp .env.example .env
```

**Backend** (terminal 1):

```bash
cd backend
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
```

**Frontend** (terminal 2):

```bash
cd frontend
npm install
npm run dev
```

Open **http://localhost:5173**. The frontend forwards API requests to the backend on port 8000.

To run the tests:

```bash
cd backend && pytest
```

```bash
cd frontend && npm test
```

If the grey line under the playlist field says "ffmpeg missing", the backend can't find FFmpeg. Restart your terminal after installing it, or set `FFMPEG_PATH` in `.env`.

## Important limitations

- Search matching is imperfect. A track can match the wrong version or not be found at all.
- Audio quality depends on the source. Converting to MP3 can't improve it.
- Private Spotify playlists are not supported.
- Jobs are kept in memory, so a running or finished job is lost when the backend restarts.
- Safe Harbor cannot guarantee that every result has clean lyrics.
