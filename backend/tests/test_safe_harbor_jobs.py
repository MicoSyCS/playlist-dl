"""Safe Harbor through the job pipeline and HTTP API, plus explicit-flag parsing."""

from __future__ import annotations

import threading
import zipfile
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from app.jobs import JobManager
from app.main import create_app
from app.models import TrackInfo
from app.spotify import SpotifyClient, parse_entry, parse_search_tracks
from app.spotify_embed import parse_embed_row
from tests.test_jobs import URL, FakeEngine, FakeSpotify, entries_for, playlist, settings, wait_done


def t(i: int, title: str, *, explicit: bool | None) -> TrackInfo:
    return TrackInfo(f"s{i}", title, (f"Artist {i}",), "Album", 200_000, explicit=explicit)


class CleanAwareEngine(FakeEngine):
    """Normal queries return the regular upload. "clean"/"radio edit" queries return a
    clean-labelled upload unless the title contains "noclean". Records every query."""

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.lock = threading.Lock()

    def search(self, query: str) -> list[dict[str, Any]]:
        with self.lock:
            self.queries.append(query)
        if query.endswith((" clean", " clean version", " radio edit")):
            if "noclean" in query:
                return [{"title": query.rsplit(" ", 1)[0] + " (Explicit)", "duration": 200, "url": "https://x/dirty"}]
            base = query.removesuffix(" clean version").removesuffix(" clean").removesuffix(" radio edit")
            artist = " ".join(base.split()[:2])  # "Artist N"
            title = base[len(artist) + 1 :]
            return [{"title": f"{artist} - {title} (Clean)", "duration": 200, "channel": artist,
                     "url": f"https://x/clean/{title.replace(' ', '_')}"}]  # fmt: skip
        return super().search(query)


def test_safe_harbor_defaults_to_false_and_off_path_is_unchanged(tmp_path: Path) -> None:
    engine = CleanAwareEngine()
    mgr = JobManager(settings(tmp_path), FakeSpotify(playlist(entries_for(t(1, "Song 1", explicit=True)))), engine)
    job = mgr.create(URL)
    snap = wait_done(mgr, job.id)
    assert job.safe_harbor is False and snap["safe_harbor"] is False
    assert snap["status"] == "complete"
    # Off: explicit tracks are matched normally with the single standard query.
    assert engine.queries == ["Artist 1 - Song 1"]
    assert snap["failures"] == [] and snap["clean_summary"] == {}
    assert snap["zip_name"] == "Road Trip 2024.zip"  # no suffix when Safe Harbor is off
    assert job.results[0].clean_status is None
    mgr.shutdown()


def test_flag_reaches_worker_and_partial_zip_reports_clean_status(tmp_path: Path) -> None:
    pl = playlist(
        entries_for(
            t(1, "Sunny", explicit=False),  # original already clean
            t(2, "Bad Words", explicit=True),  # clean version found by label
            t(3, "noclean", explicit=True),  # only explicit uploads exist
        )
    )
    engine = CleanAwareEngine()
    mgr = JobManager(settings(tmp_path), FakeSpotify(pl), engine)
    job = mgr.create(URL, safe_harbor=True)
    snap = wait_done(mgr, job.id)

    assert snap["safe_harbor"] is True
    assert snap["status"] == "partial"
    assert snap["completed_tracks"] == 2 and snap["failed_tracks"] == 1
    assert snap["clean_unavailable_tracks"] == 1
    assert snap["clean_summary"] == {"original_clean": 1, "clean_matched": 1, "clean_unavailable": 1}
    [failure] = snap["failures"]
    assert failure["position"] == 3 and failure["clean_status"] == "clean_unavailable"
    assert failure["reason"].startswith("clean version unavailable")

    # The worker really used the clean pipeline.
    assert "Artist 2 Bad Words clean" in engine.queries
    assert "Artist 1 - Sunny Album" in engine.queries  # standard query (album added for one-word titles)

    assert snap["zip_name"] == "Road Trip 2024 (Clean).zip"
    assert job.zip_path is not None and job.zip_path.name == "Road Trip 2024 (Clean).zip"
    with zipfile.ZipFile(job.zip_path) as zf:
        names = zf.namelist()
        assert names == ["01 - Artist 1 - Sunny.mp3", "02 - Artist 2 - Bad Words.mp3", "failed-tracks.txt"]
        report = zf.read("failed-tracks.txt").decode()
    assert "003  Artist 3 - noclean  --  clean version unavailable" in report
    assert "001  Artist 1 - Sunny  --  original version already non-explicit" in report
    assert "002  Artist 2 - Bad Words  --  clean version matched" in report
    assert job.results[2].source_url is None  # nothing explicit was downloaded as a fallback
    mgr.shutdown()


def test_no_clean_tracks_gives_clear_error_not_empty_zip(tmp_path: Path) -> None:
    pl = playlist(entries_for(t(1, "noclean one", explicit=True), t(2, "noclean two", explicit=True)))
    mgr = JobManager(settings(tmp_path), FakeSpotify(pl), CleanAwareEngine())
    job = mgr.create(URL, safe_harbor=True)
    snap = wait_done(mgr, job.id)
    assert snap["status"] == "failed" and snap["download_ready"] is False
    assert "safe harbor could not find a verified clean version" in snap["error"]
    assert job.zip_path is None
    mgr.shutdown()


def test_same_playlist_with_and_without_safe_harbor_are_separate_jobs(tmp_path: Path) -> None:
    gate = threading.Event()
    mgr = JobManager(
        settings(tmp_path, max_queued_jobs=5), FakeSpotify(playlist(entries_for(t(1, "S", explicit=False))), gate),
        CleanAwareEngine(),
    )  # fmt: skip
    a = mgr.create(URL)
    b = mgr.create(URL, safe_harbor=True)
    assert a.id != b.id
    assert mgr.create(URL, safe_harbor=True).id == b.id
    gate.set()
    wait_done(mgr, a.id)
    wait_done(mgr, b.id)
    mgr.shutdown()


def test_api_accepts_flag_validates_boolean_and_reports_it(tmp_path: Path) -> None:
    s = settings(tmp_path)
    mgr = JobManager(s, FakeSpotify(playlist(entries_for(t(1, "Sunny", explicit=False)))), CleanAwareEngine())
    with TestClient(create_app(s, mgr)) as client:
        for bad in ("yes", "true", 1, None):
            r = client.post("/api/jobs", json={"url": URL, "safe_harbor": bad})
            assert r.status_code == 422 and r.json()["error"] == "safe_harbor must be true or false.", bad

        omitted = client.post("/api/jobs", json={"url": URL})
        assert omitted.status_code == 202 and omitted.json()["safe_harbor"] is False
        wait_done(mgr, omitted.json()["job_id"])

        on = client.post("/api/jobs", json={"url": URL, "safe_harbor": True})
        assert on.status_code == 202 and on.json()["safe_harbor"] is True
        job_id = on.json()["job_id"]
        wait_done(mgr, job_id)
        body = client.get(f"/api/jobs/{job_id}").json()
        assert body["safe_harbor"] is True and body["clean_summary"] == {"original_clean": 1}

        health = client.get("/api/health").json()
        assert health["safe_harbor_catalog"] is True  # test settings include credentials


# --------------------------------------------------------------------------- explicit-flag parsing


def test_api_and_embed_parsers_read_explicit_flag() -> None:
    api = parse_entry({"track": {"type": "track", "name": "X", "artists": [{"name": "A"}], "explicit": True}}, 1)
    assert api.track is not None and api.track.explicit is True
    missing = parse_entry({"track": {"type": "track", "name": "X", "artists": [{"name": "A"}]}}, 1)
    assert missing.track is not None and missing.track.explicit is None
    embed = parse_embed_row({"uri": "spotify:track:1", "title": "X", "subtitle": "A", "isExplicit": False,
                             "entityType": "track"}, 1)  # fmt: skip
    assert embed.track is not None and embed.track.explicit is False


def test_catalog_search_request_and_parsing() -> None:
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "accounts.spotify.com":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        seen.update(dict(req.url.params))
        item = {"id": "c1", "type": "track", "name": "Song - Clean", "artists": [{"name": "Artist"}],
                "album": {"name": "Album"}, "duration_ms": 200_000, "explicit": False}  # fmt: skip
        return httpx.Response(200, json={"tracks": {"items": [item, None]}})

    client = SpotifyClient("id", "secret", http=httpx.Client(transport=httpx.MockTransport(handler)))
    results = client.search_tracks('Artist "Quoted"', "Song: Part 2")
    assert seen["type"] == "track" and seen["limit"] == "10"
    assert seen["q"] == 'track:"Song Part 2" artist:"Artist Quoted"'
    assert [(r.spotify_id, r.explicit) for r in results] == [("c1", False)]
    assert parse_search_tracks({}) == []
