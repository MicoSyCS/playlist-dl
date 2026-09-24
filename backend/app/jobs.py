"""In-process job queue: playlist -> matched MP3s -> ZIP.

Jobs live in memory, so the API must run as a single process (one uvicorn worker).
Files live under ``DATA_DIR/<job_id>/`` and are removed when the job expires.
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .config import Settings
from .errors import CapacityExceeded, SpotifyNotConfigured, TrackError, UserFacingError
from .files import build_zip, safe_join, track_filename, unique_name, zip_name_for
from .matcher import build_search_query, pick_best
from .models import JobStatus, Playlist, PlaylistEntry, TrackInfo, TrackResult, TrackState
from .safe_harbor import CleanCatalog, CleanStatus, select_clean_source
from .spotify import extract_playlist_id

log = logging.getLogger(__name__)

JOB_ID_RE = re.compile(r"^[a-f0-9]{32}$")
GENERIC_ERROR = "something went wrong while processing this playlist. try again later."
FAILED_LIST_NAME = "failed-tracks.txt"


class PlaylistSource(Protocol):
    def fetch_playlist(self, playlist_id: str, max_tracks: int) -> Playlist: ...


class Engine(Protocol):
    def search(self, query: str) -> list[dict[str, Any]]: ...

    def download_audio(self, url: str, work_dir: Path, *, deadline: float, cancel: threading.Event) -> Path: ...

    def convert_to_mp3(
        self, source: Path, dest: Path, track: TrackInfo, *, position: int, total: int, timeout: float
    ) -> None: ...


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Job:
    id: str
    playlist_id: str
    created_at: datetime = field(default_factory=_now)
    safe_harbor: bool = False  # prefer clean versions; skip tracks without a verified clean match
    status: JobStatus = JobStatus.QUEUED
    phase: str = "queued"
    playlist_title: str | None = None
    total_tracks: int = 0
    results: list[TrackResult] = field(default_factory=list)
    in_progress: dict[int, str] = field(default_factory=dict)
    error: str | None = None
    notice: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    zip_path: Path | None = None
    zip_name: str | None = None
    zip_size: int | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def expires_at(self, hours: float) -> datetime:
        base = self.finished_at or self.created_at
        return base + timedelta(hours=hours)

    def snapshot(self, expiration_hours: float) -> dict[str, Any]:
        with self.lock:
            ok = sum(1 for r in self.results if r.state is TrackState.OK)
            failed = sum(1 for r in self.results if r.state is TrackState.FAILED)
            skipped = sum(1 for r in self.results if r.state is TrackState.SKIPPED)
            processed = ok + failed + skipped
            clean_unavailable = sum(1 for r in self.results if r.clean_status == CleanStatus.UNAVAILABLE)
            clean_summary: dict[str, int] = {}
            if self.safe_harbor:
                for r in self.results:
                    if r.clean_status:
                        clean_summary[r.clean_status] = clean_summary.get(r.clean_status, 0) + 1
            total = self.total_tracks
            if self.status.is_downloadable:
                progress = 100.0
            elif total:
                progress = round(min(99.0, processed * 100 / total), 1)
            else:
                progress = 0.0
            current = None
            if self.in_progress:
                current = self.in_progress[max(self.in_progress)]
            return {
                "job_id": self.id,
                "status": self.status.value,
                "phase": self.phase,
                "safe_harbor": self.safe_harbor,
                "playlist_title": self.playlist_title,
                "total_tracks": total,
                "processed_tracks": processed,
                "completed_tracks": ok,
                "failed_tracks": failed,
                "skipped_tracks": skipped,
                "clean_unavailable_tracks": clean_unavailable,
                "clean_summary": clean_summary,
                "current_track": current,
                "progress": progress,
                "error": self.error,
                "notice": self.notice,
                "failures": [
                    {
                        "position": r.position,
                        "track": r.label,
                        "reason": r.reason or "failed",
                        "clean_status": r.clean_status,
                    }
                    for r in self.results
                    if r.state in (TrackState.FAILED, TrackState.SKIPPED)
                ][:200],
                "download_ready": self.status.is_downloadable and self.zip_path is not None,
                "zip_name": self.zip_name,
                "zip_size_bytes": self.zip_size,
                "created_at": self.created_at.isoformat(),
                "expires_at": self.expires_at(expiration_hours).isoformat() if self.finished_at else None,
            }


class JobManager:
    def __init__(
        self,
        settings: Settings,
        spotify: PlaylistSource,
        engine: Engine,
        catalog: CleanCatalog | None = None,
    ) -> None:
        self.settings = settings
        self.spotify = spotify
        self.engine = engine
        # Spotify catalog search for Safe Harbor (None without API credentials).
        self.catalog = catalog
        self.data_dir = settings.data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=settings.max_concurrent_jobs, thread_name_prefix="job")
        self._stop = threading.Event()
        self._janitor: threading.Thread | None = None

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        self.cleanup(remove_orphans=True)
        self._janitor = threading.Thread(target=self._janitor_loop, name="janitor", daemon=True)
        self._janitor.start()

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            for job in self._jobs.values():
                job.cancel.set()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _janitor_loop(self) -> None:
        while not self._stop.wait(self.settings.cleanup_interval_seconds):
            try:
                self.cleanup(remove_orphans=True)
            except Exception:
                log.exception("cleanup pass failed")

    # -- public API -------------------------------------------------------------

    def create(self, url: str, *, safe_harbor: bool = False) -> Job:
        playlist_id = extract_playlist_id(url)
        if self.settings.credentials_required and not self.settings.spotify_configured:
            raise SpotifyNotConfigured(
                "the server is missing spotify api credentials. set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET."
            )
        with self._lock:
            active = [j for j in self._jobs.values() if j.status.is_active]
            for existing in active:
                if existing.playlist_id == playlist_id and existing.safe_harbor == safe_harbor:
                    log.info("reusing active job %s for playlist %s", existing.id, playlist_id)
                    return existing
            limit = self.settings.max_concurrent_jobs + self.settings.max_queued_jobs
            if len(active) >= limit:
                raise CapacityExceeded("the server is busy with other playlists. try again in a few minutes.")
            job = Job(id=uuid.uuid4().hex, playlist_id=playlist_id, safe_harbor=safe_harbor)
            self._jobs[job.id] = job
        log.info("job %s created for playlist %s safe_harbor=%s", job.id, playlist_id, safe_harbor)
        self._executor.submit(self._run, job)
        return job

    def get(self, job_id: str) -> Job | None:
        if not JOB_ID_RE.match(job_id):
            return None
        with self._lock:
            return self._jobs.get(job_id)

    def snapshot(self, job: Job) -> dict[str, Any]:
        return job.snapshot(self.settings.job_expiration_hours)

    # -- worker -----------------------------------------------------------------

    def _job_dir(self, job: Job) -> Path:
        return safe_join(self.data_dir, job.id)

    def _run(self, job: Job) -> None:
        started = time.monotonic()
        deadline = started + self.settings.job_timeout_minutes * 60
        job_dir = self._job_dir(job)
        with job.lock:
            job.status = JobStatus.PROCESSING
            job.phase = "fetching playlist"
            job.started_at = _now()
        try:
            job_dir.mkdir(parents=True, exist_ok=True)
            playlist = self.spotify.fetch_playlist(job.playlist_id, self.settings.max_playlist_tracks)
            plan = self._plan(job, playlist)
            if not plan:
                raise UserFacingError("this playlist has no downloadable tracks.")
            with job.lock:
                job.phase = "downloading"
            self._process_all(job, plan, job_dir / "mp3", deadline)
            self._package(job, playlist, plan, job_dir)
        except UserFacingError as exc:
            log.info("job %s failed: %s", job.id, exc.message)
            self._finish(job, JobStatus.FAILED, exc.message)
        except Exception:
            log.exception("job %s crashed", job.id)
            self._finish(job, JobStatus.FAILED, GENERIC_ERROR)
        finally:
            for sub in ("mp3", "work"):
                shutil.rmtree(job_dir / sub, ignore_errors=True)
            log.info("job %s finished as %s in %.1fs", job.id, job.status.value, time.monotonic() - started)

    def _plan(self, job: Job, playlist: Playlist) -> list[tuple[PlaylistEntry, TrackResult]]:
        """Create one result per playlist row; return the rows that need downloading."""
        total = len(playlist.entries)
        taken: set[str] = {FAILED_LIST_NAME.lower()}
        seen: dict[str, int] = {}
        results: list[TrackResult] = []
        plan: list[tuple[PlaylistEntry, TrackResult]] = []
        for entry in playlist.entries:
            result = TrackResult(position=entry.position, label=entry.label)
            results.append(result)
            track = entry.track
            if track is None:
                result.state, result.reason = TrackState.FAILED, entry.skip_reason or "unavailable"
                continue
            if track.spotify_id and track.spotify_id in seen:
                result.state = TrackState.SKIPPED
                result.reason = f"duplicate of track {seen[track.spotify_id]:02d}"
                continue
            if track.spotify_id:
                seen[track.spotify_id] = entry.position
            result.filename = unique_name(
                track_filename(entry.position, total, track.primary_artist, track.title), taken
            )
            plan.append((entry, result))
        with job.lock:
            job.playlist_title = playlist.name
            job.notice = playlist.notice
            job.total_tracks = total
            job.results = results
        log.info("job %s: %r, %d rows, %d to download", job.id, playlist.name, total, len(plan))
        return plan

    def _process_all(
        self, job: Job, plan: list[tuple[PlaylistEntry, TrackResult]], mp3_dir: Path, deadline: float
    ) -> None:
        mp3_dir.mkdir(parents=True, exist_ok=True)
        total = job.total_tracks
        with ThreadPoolExecutor(
            max_workers=self.settings.track_concurrency, thread_name_prefix=f"trk-{job.id[:6]}"
        ) as pool:
            futures = [
                pool.submit(self._process_track, job, entry, result, mp3_dir, total, deadline) for entry, result in plan
            ]
            for fut in futures:
                fut.result()

    def _process_track(
        self,
        job: Job,
        entry: PlaylistEntry,
        result: TrackResult,
        mp3_dir: Path,
        total: int,
        job_deadline: float,
    ) -> None:
        track = entry.track
        assert track is not None and result.filename is not None
        failed_status = CleanStatus.FAILED if job.safe_harbor else None
        if job.cancel.is_set() or time.monotonic() >= job_deadline:
            self._set_result(job, result, TrackState.FAILED, "job time limit reached before this track", failed_status)
            return

        with job.lock:
            job.in_progress[entry.position] = entry.label
        work = safe_join(mp3_dir.parent, "work") / f"t{entry.position:05d}"
        deadline = min(time.monotonic() + self.settings.track_timeout_seconds, job_deadline)
        try:
            work.mkdir(parents=True, exist_ok=True)
            clean_status: CleanStatus | None = None
            if job.safe_harbor:
                decision = select_clean_source(
                    track, self.engine.search, self.catalog, context=f"job={job.id[:8]} pos={entry.position}"
                )
                if decision.candidate is None:
                    # Never fall back to a possibly explicit version: skip and report.
                    self._set_result(job, result, TrackState.FAILED, decision.reason, decision.status)
                    return
                best, clean_status = decision.candidate, decision.status
            else:
                query = build_search_query(track)
                candidates = self.engine.search(query)
                picked, why = pick_best(track, candidates)
                if picked is None:
                    raise TrackError(why)
                best = picked
                log.debug("job %s #%d %r -> %s (%s)", job.id, entry.position, query, best.url, why)
            if time.monotonic() >= deadline:
                raise TrackError("track timed out")
            source = self.engine.download_audio(best.url, work, deadline=deadline, cancel=job.cancel)
            dest = safe_join(mp3_dir, result.filename)
            self.engine.convert_to_mp3(
                source, dest, track, position=entry.position, total=total, timeout=deadline - time.monotonic()
            )
            result.source_url = best.url
            self._set_result(job, result, TrackState.OK, None, clean_status)
        except TrackError as exc:
            log.info("job %s #%d %s: %s", job.id, entry.position, entry.label, exc)
            self._set_result(job, result, TrackState.FAILED, str(exc) or "failed", failed_status)
        except Exception:
            log.exception("job %s #%d unexpected error", job.id, entry.position)
            self._set_result(job, result, TrackState.FAILED, "unexpected error", failed_status)
        finally:
            with job.lock:
                job.in_progress.pop(entry.position, None)
            shutil.rmtree(work, ignore_errors=True)

    @staticmethod
    def _set_result(
        job: Job,
        result: TrackResult,
        state: TrackState,
        reason: str | None,
        clean_status: CleanStatus | None = None,
    ) -> None:
        with job.lock:
            result.state = state
            result.reason = reason
            result.clean_status = clean_status.value if clean_status else None

    def _package(
        self, job: Job, playlist: Playlist, plan: list[tuple[PlaylistEntry, TrackResult]], job_dir: Path
    ) -> None:
        with job.lock:
            job.phase = "packaging"
        mp3_dir = job_dir / "mp3"
        files: list[tuple[Path, str]] = []
        for _entry, result in plan:
            if result.state is TrackState.OK and result.filename:
                path = safe_join(mp3_dir, result.filename)
                if path.is_file():
                    files.append((path, result.filename))
                else:
                    self._set_result(job, result, TrackState.FAILED, "converted file missing")
        if not files:
            if job.safe_harbor:
                raise UserFacingError(
                    "safe harbor could not find a verified clean version of any track in this playlist."
                )
            raise UserFacingError(
                "none of the tracks could be matched or downloaded. the audio source may be unavailable."
            )
        failed = [r for r in job.results if r.state is not TrackState.OK]
        extra: dict[str, str] = {}
        if failed or playlist.notice or job.safe_harbor:
            extra[FAILED_LIST_NAME] = _failed_report(playlist, job.results, safe_harbor=job.safe_harbor)
        zip_name = zip_name_for(playlist.name, clean=job.safe_harbor)
        zip_path = safe_join(job_dir, zip_name)
        build_zip(zip_path, files, extra)
        with job.lock:
            job.zip_path = zip_path
            job.zip_name = zip_name
            job.zip_size = zip_path.stat().st_size
        has_failures = any(r.state is TrackState.FAILED for r in job.results)
        self._finish(job, JobStatus.PARTIAL if has_failures else JobStatus.COMPLETE, None)

    def _finish(self, job: Job, status: JobStatus, error: str | None) -> None:
        with job.lock:
            job.status = status
            job.phase = status.value
            job.error = error
            job.finished_at = _now()
            job.in_progress.clear()

    # -- cleanup ----------------------------------------------------------------

    def cleanup(self, *, remove_orphans: bool = False) -> int:
        """Delete expired jobs and their files. Returns the number of jobs removed."""
        now = _now()
        hours = self.settings.job_expiration_hours
        # Active jobs older than timeout + expiration are considered abandoned/stuck.
        stuck_after = timedelta(minutes=self.settings.job_timeout_minutes, hours=hours)
        removed: list[Job] = []
        with self._lock:
            for job_id, job in list(self._jobs.items()):
                expired = job.finished_at is not None and job.expires_at(hours) <= now
                stuck = job.status.is_active and now - job.created_at > stuck_after
                if expired or stuck:
                    job.cancel.set()
                    removed.append(self._jobs.pop(job_id))
            known = set(self._jobs)
        for job in removed:
            shutil.rmtree(self._job_dir(job), ignore_errors=True)
            log.info("job %s expired and was removed", job.id)
        if remove_orphans:
            for child in self.data_dir.iterdir():
                if child.name not in known and child.is_dir() and JOB_ID_RE.match(child.name):
                    shutil.rmtree(child, ignore_errors=True)
                    log.info("removed orphaned job directory %s", child.name)
        return len(removed)


def _failed_report(playlist: Playlist, results: list[TrackResult], *, safe_harbor: bool = False) -> str:
    lines = [
        f"playlist: {playlist.name}",
        f"spotify: https://open.spotify.com/playlist/{playlist.playlist_id}",
        f"generated: {_now().strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        *([f"note: {playlist.notice}", ""] if playlist.notice else []),
        "tracks not included in this archive:",
        "",
    ]
    for r in results:
        if r.state is not TrackState.OK:
            lines.append(f"{r.position:03d}  {r.label}  --  {r.reason or 'failed'}")
    if safe_harbor:
        lines += [
            "",
            "safe harbor: clean-version status of every track",
            "(best effort, from spotify's explicit flag and source labels; audio is not analysed)",
            "",
        ]
        for r in results:
            status = CleanStatus(r.clean_status).label if r.clean_status else (r.reason or "not processed")
            lines.append(f"{r.position:03d}  {r.label}  --  {status}")
    lines += [
        "",
        "spotify supplies metadata only. audio was located by search and matching may be imperfect.",
        "",
    ]
    return "\n".join(lines)
