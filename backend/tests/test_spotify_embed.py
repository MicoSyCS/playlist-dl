"""Keyless embed-player source: parsing (structure mirrors the live page) and HTTP behaviour."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.errors import InvalidPlaylistUrl, PlaylistItemsUnavailable, SpotifyError
from app.models import Playlist
from app.spotify_embed import (
    EMBED_TRACK_CAP,
    TRUNCATION_NOTICE,
    ApiWithEmbedFallback,
    SpotifyEmbedClient,
    parse_embed_html,
)

PID = "37i9dQZF1DXcBWIGoYBM5M"
NBSP_SEP = ", "


def row(i: int, *, title: str | None = None, subtitle: str | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "uri": f"spotify:track:{i:022d}",
        "uid": f"uid{i}",
        "title": title or f"Song {i}",
        "subtitle": subtitle or f"Artist {i}",
        "isExplicit": False,
        "duration": 200_000 + i,
        "isPlayable": True,
        "entityType": "track",
        **extra,
    }


def page(rows: list[dict[str, Any]], name: str = "Road Trip", **entity_extra: Any) -> str:
    data = {
        "props": {
            "pageProps": {
                "state": {
                    "data": {
                        "entity": {
                            "type": "playlist",
                            "name": name,
                            "uri": f"spotify:playlist:{PID}",
                            "id": PID,
                            "subtitle": "someone",
                            "trackList": rows,
                            **entity_extra,
                        }
                    }
                }
            }
        }
    }
    return f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></body></html>'


def test_parses_tracks_in_order() -> None:
    pl = parse_embed_html(page([row(1), row(2), row(3)]), PID, 100)
    assert pl.name == "Road Trip"
    assert pl.total == 3
    assert [e.position for e in pl.entries] == [1, 2, 3]
    first = pl.entries[0].track
    assert first is not None
    assert (first.title, first.artists, first.duration_ms, first.album) == ("Song 1", ("Artist 1",), 200_001, "")
    assert first.spotify_id == f"{1:022d}"
    assert pl.notice is None


def test_artist_split_uses_no_break_space_separator() -> None:
    pl = parse_embed_html(
        page(
            [
                row(1, subtitle=f"Dr. Dre{NBSP_SEP}Snoop Dogg"),
                row(2, subtitle="Earth, Wind & Fire"),
            ]
        ),
        PID,
        100,
    )
    assert pl.entries[0].track is not None and pl.entries[0].track.artists == ("Dr. Dre", "Snoop Dogg")
    assert pl.entries[1].track is not None and pl.entries[1].track.artists == ("Earth, Wind & Fire",)


def test_episodes_and_local_files_are_reported() -> None:
    pl = parse_embed_html(
        page(
            [
                row(1, entityType="episode", uri="spotify:episode:abc"),
                row(2, uri="spotify:local:Artist:Album:Title:180", entityType=None),
            ]
        ),
        PID,
        100,
    )
    assert pl.entries[0].track is None and "podcast" in (pl.entries[0].skip_reason or "")
    assert pl.entries[1].track is None and "local" in (pl.entries[1].skip_reason or "")


def test_cap_sets_truncation_notice_and_respects_limit() -> None:
    rows = [row(i) for i in range(1, EMBED_TRACK_CAP + 1)]
    pl = parse_embed_html(page(rows), PID, 100)
    assert pl.total == 100 and pl.notice == TRUNCATION_NOTICE
    smaller = parse_embed_html(page(rows), PID, 25)
    assert smaller.total == 25 and smaller.notice is None


def test_not_found_page() -> None:
    html = page([])
    html = html.replace('"state"', '"status": 404, "title": "Page not found", "unused"')
    with pytest.raises(SpotifyError) as info:
        parse_embed_html(html, PID, 100)
    assert info.value.status_code == 404 and "private" in info.value.message


@pytest.mark.parametrize(
    "html",
    [
        "<html>no data here</html>",
        '<script id="__NEXT_DATA__" type="application/json">{not json</script>',
        '<script id="__NEXT_DATA__" type="application/json">{"props": {"pageProps": {}}}</script>',
    ],
)
def test_changed_page_format_gives_clear_error(html: str) -> None:
    with pytest.raises(SpotifyError) as info:
        parse_embed_html(html, PID, 100)
    assert "page format may have changed" in info.value.message


def test_non_playlist_entity_rejected() -> None:
    with pytest.raises(SpotifyError):
        parse_embed_html(page([row(1)], type="album"), PID, 100)


# --------------------------------------------------------------------------- client


def client(handler: Any) -> SpotifyEmbedClient:
    return SpotifyEmbedClient(http=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False))


def test_client_requests_only_the_fixed_embed_url() -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(200, text=page([row(1)]))

    pl = client(handler).fetch_playlist(PID, 100)
    assert pl.total == 1
    assert seen == [f"https://open.spotify.com/embed/playlist/{PID}"]


def test_client_rejects_bad_ids_without_requesting() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("should not be called")

    with pytest.raises(InvalidPlaylistUrl):
        client(handler).fetch_playlist("../../evil", 100)


def test_client_does_not_follow_redirects() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://evil.example/"})

    with pytest.raises(SpotifyError):
        client(handler).fetch_playlist(PID, 100)


def test_client_retries_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.spotify_embed.time.sleep", lambda s: None)
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, text=page([row(1)]))

    assert client(handler).fetch_playlist(PID, 100).total == 1
    assert calls["n"] == 2


def test_client_404() -> None:
    with pytest.raises(SpotifyError) as info:
        client(lambda req: httpx.Response(404)).fetch_playlist(PID, 100)
    assert info.value.status_code == 404


# --------------------------------------------------------------------------- fallback


class Stub:
    def __init__(self, result: Playlist | Exception) -> None:
        self.result = result
        self.calls = 0

    def fetch_playlist(self, playlist_id: str, max_tracks: int) -> Playlist:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_fallback_only_on_withheld_items() -> None:
    embed_pl = Playlist(PID, "from embed", "", 0)
    api = Stub(PlaylistItemsUnavailable("withheld"))
    embed = Stub(embed_pl)
    assert ApiWithEmbedFallback(api, embed).fetch_playlist(PID, 100) is embed_pl

    api_404 = Stub(SpotifyError("not found", status_code=404))
    embed2 = Stub(embed_pl)
    with pytest.raises(SpotifyError):
        ApiWithEmbedFallback(api_404, embed2).fetch_playlist(PID, 100)
    assert embed2.calls == 0
