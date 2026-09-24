"""Playlist parsing for both Spotify schemas, and the client's pagination via a mock transport."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.errors import PlaylistTooLarge, SpotifyError
from app.spotify import API_BASE, SpotifyClient, parse_entry

PID = "37i9dQZF1DXcBWIGoYBM5M"


def track(i: int, *, name: str | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "id": f"t{i:03d}",
        "type": "track",
        "name": name or f"Song {i}",
        "artists": [{"name": f"Artist {i}"}, {"name": "Guest"}],
        "album": {"name": f"Album {i}"},
        "duration_ms": 180_000 + i,
        **extra,
    }


# --------------------------------------------------------------------------- parse_entry


def test_parse_new_schema_item() -> None:
    e = parse_entry({"item": track(1)}, 1)
    assert e.track is not None
    assert e.track.title == "Song 1"
    assert e.track.artists == ("Artist 1", "Guest")
    assert e.track.album == "Album 1"
    assert e.track.duration_ms == 180_001
    assert e.label == "Artist 1 - Song 1"


def test_parse_legacy_schema_track() -> None:
    e = parse_entry({"track": track(2)}, 2)
    assert e.track is not None and e.track.spotify_id == "t002"


def test_parse_episode_is_skipped() -> None:
    e = parse_entry({"item": {"type": "episode", "name": "Pod 1", "id": "e1"}}, 1)
    assert e.track is None and "podcast" in (e.skip_reason or "")


def test_parse_local_file_is_skipped() -> None:
    e = parse_entry({"is_local": True, "track": {**track(3), "id": None, "is_local": True}}, 3)
    assert e.track is None and "local" in (e.skip_reason or "")


def test_parse_removed_track() -> None:
    e = parse_entry({"track": None}, 4)
    assert e.track is None and e.label == "unknown track"


# --------------------------------------------------------------------------- client


def make_client(handler: Any) -> SpotifyClient:
    def wrapped(request: httpx.Request) -> httpx.Response:
        if request.url.host == "accounts.spotify.com":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer tok"
        return handler(request)

    return SpotifyClient("id", "secret", http=httpx.Client(transport=httpx.MockTransport(wrapped)))


def page(rows: list[dict[str, Any]], total: int, next_url: str | None, key: str = "item") -> dict[str, Any]:
    return {"items": [{key: r} for r in rows], "total": total, "next": next_url}


@pytest.mark.parametrize("schema", ["new", "legacy"])
def test_fetch_all_pages_in_order(schema: str) -> None:
    total = 230
    key = "item" if schema == "new" else "track"
    container = "items" if schema == "new" else "tracks"
    sub = "items" if schema == "new" else "tracks"
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        if req.url.path == f"/v1/playlists/{PID}":
            nxt = f"{API_BASE}/playlists/{PID}/{sub}?offset=100&limit=100"
            first = page([track(i) for i in range(1, 101)], total, nxt, key)
            return httpx.Response(200, json={"name": "Big List", "owner": {"display_name": "me"}, container: first})
        offset = int(req.url.params["offset"])
        end = min(offset + 100, total)
        nxt = f"{API_BASE}/playlists/{PID}/{sub}?offset={end}&limit=100" if end < total else None
        return httpx.Response(200, json=page([track(i) for i in range(offset + 1, end + 1)], total, nxt, key))

    pl = make_client(handler).fetch_playlist(PID, max_tracks=500)
    assert pl.name == "Big List"
    assert pl.total == total
    assert len(pl.entries) == total
    assert [e.position for e in pl.entries] == list(range(1, total + 1))
    assert pl.entries[-1].track is not None and pl.entries[-1].track.title == f"Song {total}"
    assert len(calls) == 3


def test_metadata_only_response_gives_clear_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"name": "Someone else's", "owner": {"id": "x"}})

    with pytest.raises(SpotifyError) as info:
        make_client(handler).fetch_playlist(PID, max_tracks=100)
    assert "extended quota" in info.value.message


def test_too_large_is_rejected_before_paging() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"name": "x", "items": page([track(1)], 5000, "ignored")})

    with pytest.raises(PlaylistTooLarge):
        make_client(handler).fetch_playlist(PID, max_tracks=100)


def test_not_found_maps_to_friendly_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"status": 404, "message": "Not found."}})

    with pytest.raises(SpotifyError) as info:
        make_client(handler).fetch_playlist(PID, max_tracks=100)
    assert info.value.status_code == 404
    assert "private" in info.value.message


def test_rate_limit_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.spotify.time.sleep", lambda s: None)
    attempts = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"name": "x", "items": page([track(1)], 1, None)})

    pl = make_client(handler).fetch_playlist(PID, max_tracks=100)
    assert len(pl.entries) == 1 and attempts["n"] == 2


def test_off_host_pagination_link_is_refused() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"name": "x", "items": page([track(1)], 2, "https://evil.example/next")})

    with pytest.raises(SpotifyError):
        make_client(handler).fetch_playlist(PID, max_tracks=100)


def test_secret_not_in_error_messages() -> None:
    def wrapped(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=json.dumps({"error": "invalid_client"}))

    client = SpotifyClient("id", "super-secret-value", http=httpx.Client(transport=httpx.MockTransport(wrapped)))
    with pytest.raises(Exception) as info:
        client.fetch_playlist(PID, max_tracks=10)
    assert "super-secret-value" not in str(info.value)
