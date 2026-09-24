"""Job lifecycle and API behaviour, using in-test Spotify/engine doubles (no network)."""

from __future__ import annotations

import threading
import time
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.errors import CapacityExceeded, InvalidPlaylistUrl, SpotifyError, SpotifyNotConfigured
from app.jobs import JobManager
from app.main import create_app
from app.models import JobStatus, Playlist, PlaylistEntry, TrackInfo

PID = "37i9dQZF1DXcBWIGoYBM5M"
URL = f"https://open.spotify.com/playlist/{PID}"


def ti(i: int, title: str | None = None, sid: str | None = None) -> TrackInfo:
    return TrackInfo(sid or f"s{i}", title or f"Song {i}", (f"Artist {i}",), "Album", 200_000)


def playlist(entries: list[PlaylistEntry], name: str = "Road Trip: 2024") -> Playlist:
    return Playlist(PID, name, "owner", len(entries), entries)


def entries_for(*tracks: TrackInfo | None) -> list[PlaylistEntry]:
    out = []
    for pos, t in enumerate(tracks, start=1):
        label = t.display if t else "unknown track"
        out.append(PlaylistEntry(pos, t, None if t else "track unavailable", label))
    return out


class FakeSpotify:
    def __init__(self, result: Playlist | Exception, gate: threading.Event | None = None) -> None:
        self.result = result
        self.gate = gate

    def fetch_playlist(self, playlist_id: str, max_tracks: int) -> Playlist:
        if self.gate:
            self.gate.wait(5)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeEngine:
    """Search returns a matching candidate unless the title contains 'nomatch'; download
    fails for titles containing 'broken'. Conversion writes a small file."""

    def search(self, query: str) -> list[dict[str, Any]]:
        if "nomatch" in query:
            return [{"title": "Completely Different Thing", "duration": 999, "url": "https://x/other"}]
        title = query.split(" - ", 1)[-1]
        artist = query.split(" - ", 1)[0]
        return [
            {
                "title": f"{artist} - {title}",
                "duration": 200,
                "channel": f"{artist} - Topic",
                "url": f"https://x/{title.replace(' ', '_')}",
            }
        ]

    def download_audio(self, url: str, work_dir: Path, *, deadline: float, cancel: threading.Event) -> Path:
        if "broken" in url:
            from app.errors import TrackError

            raise TrackError("download failed on the audio source")
        p = work_dir / "source.webm"
        p.write_bytes(b"audio")
        return p

    def convert_to_mp3(
        self, source: Path, dest: Path, track: TrackInfo, *, position: int, total: int, timeout: float
    ) -> None:
        dest.write_bytes(f"ID3 {position}/{total} {track.title}".encode())


def settings(tmp_path: Path, **kw: Any) -> Settings:
    base = dict(
        spotify_client_id="id",
        spotify_client_secret="secret",
        data_dir=tmp_path,
        max_concurrent_jobs=1,
        max_queued_jobs=1,
        track_concurrency=2,
        cleanup_interval_seconds=3600,
    )
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


def wait_done(mgr: JobManager, job_id: str, timeout: float = 10) -> dict[str, Any]:
    end = time.time() + timeout
    while time.time() < end:
        job = mgr.get(job_id)
        assert job is not None
        snap = mgr.snapshot(job)
        if snap["status"] not in ("queued", "processing"):
            return snap
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_complete_job_produces_ordered_zip(tmp_path: Path) -> None:
    pl = playlist(entries_for(ti(1), ti(2, "Same/Name"), ti(3)))
    mgr = JobManager(settings(tmp_path), FakeSpotify(pl), FakeEngine())
    job = mgr.create(URL)
    snap = wait_done(mgr, job.id)
    assert snap["status"] == "complete"
    assert snap["completed_tracks"] == 3 and snap["failed_tracks"] == 0 and snap["progress"] == 100
    assert snap["zip_name"] == "Road Trip 2024.zip"
    assert snap["download_ready"] is True
    assert job.zip_path is not None
    with zipfile.ZipFile(job.zip_path) as zf:
        assert zf.namelist() == [
            "01 - Artist 1 - Song 1.mp3",
            "02 - Artist 2 - Same Name.mp3",
            "03 - Artist 3 - Song 3.mp3",
        ]
    # Intermediate files are removed; only the ZIP remains.
    assert sorted(p.name for p in (tmp_path / job.id).iterdir()) == ["Road Trip 2024.zip"]
    mgr.shutdown()


def test_partial_job_lists_failures_and_skips_duplicates(tmp_path: Path) -> None:
    pl = playlist(entries_for(ti(1), ti(2, "nomatch"), ti(3, "broken"), None, ti(5, sid="s1")))
    mgr = JobManager(settings(tmp_path), FakeSpotify(pl), FakeEngine())
    job = mgr.create(URL)
    snap = wait_done(mgr, job.id)
    assert snap["status"] == "partial"
    assert snap["completed_tracks"] == 1
    assert snap["failed_tracks"] == 3
    assert snap["skipped_tracks"] == 1
    reasons = {f["position"]: f["reason"] for f in snap["failures"]}
    assert "no confident match" in reasons[2]
    assert "download failed" in reasons[3]
    assert reasons[4] == "track unavailable"
    assert reasons[5] == "duplicate of track 01"
    assert job.zip_path is not None
    with zipfile.ZipFile(job.zip_path) as zf:
        assert zf.namelist() == ["01 - Artist 1 - Song 1.mp3", "failed-tracks.txt"]
        report = zf.read("failed-tracks.txt").decode()
        assert "002  Artist 2 - nomatch" in report and "duplicate of track 01" in report
    mgr.shutdown()


def test_job_fails_when_nothing_downloads(tmp_path: Path) -> None:
    pl = playlist(entries_for(ti(1, "nomatch"), ti(2, "broken")))
    mgr = JobManager(settings(tmp_path), FakeSpotify(pl), FakeEngine())
    snap = wait_done(mgr, mgr.create(URL).id)
    assert snap["status"] == "failed"
    assert snap["download_ready"] is False
    assert "none of the tracks" in snap["error"]
    mgr.shutdown()


def test_spotify_error_is_reported_without_internals(tmp_path: Path) -> None:
    err = SpotifyError("playlist not found.", status_code=404)
    mgr = JobManager(settings(tmp_path), FakeSpotify(err), FakeEngine())
    snap = wait_done(mgr, mgr.create(URL).id)
    assert snap["status"] == "failed" and snap["error"] == "playlist not found."
    mgr.shutdown()


def test_unexpected_crash_is_generic(tmp_path: Path) -> None:
    mgr = JobManager(settings(tmp_path), FakeSpotify(RuntimeError("secret internal detail")), FakeEngine())
    snap = wait_done(mgr, mgr.create(URL).id)
    assert snap["status"] == "failed" and "secret" not in snap["error"]
    mgr.shutdown()


def test_invalid_url_and_missing_credentials(tmp_path: Path) -> None:
    mgr = JobManager(settings(tmp_path), FakeSpotify(playlist([])), FakeEngine())
    with pytest.raises(InvalidPlaylistUrl):
        mgr.create("https://open.spotify.com/album/37i9dQZF1DXcBWIGoYBM5M")
    mgr.shutdown()
    # Credentials are only mandatory when the Web API is forced.
    mgr2 = JobManager(
        settings(tmp_path, spotify_client_id="", spotify_metadata_source="api"), FakeSpotify(playlist([])), FakeEngine()
    )
    with pytest.raises(SpotifyNotConfigured):
        mgr2.create(URL)
    mgr2.shutdown()


def test_keyless_mode_needs_no_credentials_and_reports_notice(tmp_path: Path) -> None:
    s = settings(tmp_path, spotify_client_id="", spotify_client_secret="")
    assert s.spotify_metadata_source == "auto" and not s.uses_spotify_api
    pl = playlist(entries_for(ti(1), ti(2)))
    pl.notice = "embed lists at most 100 tracks"
    mgr = JobManager(s, FakeSpotify(pl), FakeEngine())
    job = mgr.create(URL)
    snap = wait_done(mgr, job.id)
    assert snap["status"] == "complete"
    assert snap["notice"] == "embed lists at most 100 tracks"
    assert job.zip_path is not None
    with zipfile.ZipFile(job.zip_path) as zf:
        assert "note: embed lists at most 100 tracks" in zf.read("failed-tracks.txt").decode()
    mgr.shutdown()


def test_same_playlist_reuses_active_job_and_capacity_limit(tmp_path: Path) -> None:
    gate = threading.Event()
    pl = playlist(entries_for(ti(1)))
    mgr = JobManager(settings(tmp_path, max_concurrent_jobs=1, max_queued_jobs=1), FakeSpotify(pl, gate), FakeEngine())
    first = mgr.create(URL)
    assert mgr.create(URL + "?si=abc").id == first.id  # duplicate submission -> same job
    second = mgr.create("https://open.spotify.com/playlist/0000000000000000000002")
    assert mgr.snapshot(second)["status"] == "queued"
    with pytest.raises(CapacityExceeded):
        mgr.create("https://open.spotify.com/playlist/0000000000000000000003")
    gate.set()
    wait_done(mgr, first.id)
    wait_done(mgr, second.id)
    mgr.shutdown()


def test_expired_jobs_and_orphans_are_cleaned(tmp_path: Path) -> None:
    mgr = JobManager(settings(tmp_path), FakeSpotify(playlist(entries_for(ti(1)))), FakeEngine())
    job = mgr.create(URL)
    wait_done(mgr, job.id)
    orphan = tmp_path / ("f" * 32)
    orphan.mkdir()
    unrelated = tmp_path / "keep-me"
    unrelated.mkdir()
    assert mgr.cleanup(remove_orphans=True) == 0
    assert not orphan.exists() and unrelated.exists() and (tmp_path / job.id).exists()

    assert job.finished_at is not None
    job.finished_at -= timedelta(hours=3)
    assert mgr.cleanup() == 1
    assert mgr.get(job.id) is None
    assert not (tmp_path / job.id).exists()
    mgr.shutdown()


# --------------------------------------------------------------------------- HTTP API


def test_api_flow(tmp_path: Path) -> None:
    s = settings(tmp_path)
    mgr = JobManager(s, FakeSpotify(playlist(entries_for(ti(1), ti(2, "nomatch")))), FakeEngine())
    with TestClient(create_app(s, mgr)) as client:
        bad = client.post("/api/jobs", json={"url": "https://example.com/x"})
        assert bad.status_code == 422 and "invalid link" in bad.json()["error"]

        missing = client.post("/api/jobs", json={})
        assert missing.status_code == 422 and "error" in missing.json()

        r = client.post("/api/jobs", json={"url": URL})
        assert r.status_code == 202
        job_id = r.json()["job_id"]
        assert r.json()["status"] in ("queued", "processing")

        snap = wait_done(mgr, job_id)
        assert snap["status"] == "partial"

        status = client.get(f"/api/jobs/{job_id}")
        assert status.status_code == 200
        body = status.json()
        for key in (
            "status",
            "playlist_title",
            "total_tracks",
            "completed_tracks",
            "failed_tracks",
            "current_track",
            "progress",
            "error",
        ):
            assert key in body
        assert body["total_tracks"] == 2 and body["processed_tracks"] == 2

        dl = client.get(f"/api/jobs/{job_id}/download")
        assert dl.status_code == 200
        assert dl.headers["content-type"] == "application/zip"
        assert "Road%20Trip%202024.zip" in dl.headers["content-disposition"]

        assert client.get("/api/jobs/../../etc/passwd").status_code == 404
        assert client.get("/api/jobs/" + "a" * 32).status_code == 404
        assert client.get("/api/jobs/not-a-job/download").status_code == 404


def test_download_refused_while_processing(tmp_path: Path) -> None:
    gate = threading.Event()
    s = settings(tmp_path)
    mgr = JobManager(s, FakeSpotify(playlist(entries_for(ti(1))), gate), FakeEngine())
    with TestClient(create_app(s, mgr)) as client:
        job_id = client.post("/api/jobs", json={"url": URL}).json()["job_id"]
        r = client.get(f"/api/jobs/{job_id}/download")
        assert r.status_code == 409 and "not ready" in r.json()["error"]
        gate.set()
        wait_done(mgr, job_id)
        assert mgr.get(job_id) is not None
        assert JobStatus(mgr.snapshot(mgr.get(job_id))["status"]).is_downloadable  # type: ignore[arg-type]
