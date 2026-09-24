"""Keyless playlist metadata from Spotify's public embed player.

``https://open.spotify.com/embed/playlist/{id}`` is served to anonymous visitors and
carries the track list (title, artists, duration, order) as JSON in its
``__NEXT_DATA__`` script. No account, token or API key is involved.

Trade-offs versus the Web API (see README):
  * at most 100 tracks are listed, and the true total is not exposed;
  * no album names;
  * unofficial: the page structure can change without notice.

Only the fixed embed URL is requested, built from an already validated playlist ID.
Redirects are not followed.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Protocol

import httpx

from .errors import InvalidPlaylistUrl, PlaylistItemsUnavailable, SpotifyError
from .models import Playlist, PlaylistEntry, TrackInfo
from .spotify import INVALID_URL_MESSAGE

log = logging.getLogger(__name__)

EMBED_BASE = "https://open.spotify.com/embed/playlist/"
EMBED_TRACK_CAP = 100
MAX_PAGE_BYTES = 5 * 1024 * 1024
# Multiple artists are joined with "," + no-break space; "Earth, Wind & Fire" uses a normal space.
ARTIST_SEPARATOR = ",\u00a0"

_PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)

TRUNCATION_NOTICE = (
    "spotify's public embed lists at most 100 tracks. if this playlist is longer, later tracks "
    "were not included. configure spotify api credentials to read full playlists."
)
_CHANGED = "could not read this playlist from spotify's public player. the page format may have changed."


def parse_embed_html(html: str, playlist_id: str, max_tracks: int) -> Playlist:
    """Extract a Playlist from an embed page. Raises SpotifyError with a user-safe message."""
    match = _NEXT_DATA_RE.search(html)
    if not match:
        log.warning("embed page for %s has no __NEXT_DATA__ block", playlist_id)
        raise SpotifyError(_CHANGED)
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        log.warning("embed page for %s has invalid JSON: %s", playlist_id, exc)
        raise SpotifyError(_CHANGED) from exc

    page_props = _dig(data, "props", "pageProps")
    if not isinstance(page_props, dict):
        raise SpotifyError(_CHANGED)
    if page_props.get("status") == 404:
        raise SpotifyError(
            "playlist not found. it may be private or deleted. private playlists are not supported.",
            status_code=404,
        )

    entity = _dig(page_props, "state", "data", "entity")
    if not isinstance(entity, dict) or not isinstance(entity.get("trackList"), list):
        log.warning("embed page for %s lacks entity.trackList; keys=%s", playlist_id, list(page_props)[:10])
        raise SpotifyError(_CHANGED)
    if entity.get("type") not in (None, "playlist"):
        raise SpotifyError("that spotify link is not a playlist.")

    rows: list[Any] = entity["trackList"]
    name = str(entity.get("name") or entity.get("title") or "spotify playlist")
    owner = str(entity.get("subtitle") or "")

    entries = [parse_embed_row(row if isinstance(row, dict) else {}, pos) for pos, row in enumerate(rows, 1)]
    entries = entries[:max_tracks]
    playlist = Playlist(playlist_id=playlist_id, name=name, owner=owner, total=len(entries), entries=entries)
    if len(rows) >= EMBED_TRACK_CAP and max_tracks >= EMBED_TRACK_CAP:
        playlist.notice = TRUNCATION_NOTICE
    return playlist


def parse_embed_row(row: dict[str, Any], position: int) -> PlaylistEntry:
    title = str(row.get("title") or "").strip()
    subtitle = str(row.get("subtitle") or "")
    artists = tuple(a.strip() for a in subtitle.split(ARTIST_SEPARATOR) if a.strip())
    label = f"{artists[0]} - {title}" if artists and title else (title or "unknown track")
    uri = str(row.get("uri") or "")
    kind = row.get("entityType") or (uri.split(":")[1] if uri.count(":") >= 2 else "track")

    if kind == "episode":
        return PlaylistEntry(position, None, "podcast episode (not supported)", label)
    if uri.startswith("spotify:local:"):
        return PlaylistEntry(position, None, "local file on the owner's device (not on spotify)", label)
    if kind != "track":
        return PlaylistEntry(position, None, f"unsupported item type: {kind}", label)
    if not title:
        return PlaylistEntry(position, None, "track has no title in spotify metadata", label)

    spotify_id = uri.rsplit(":", 1)[-1] if uri.startswith("spotify:track:") else None
    duration = row.get("duration")
    track = TrackInfo(
        spotify_id=spotify_id,
        title=title,
        artists=artists,
        album="",  # not present in the embed data
        duration_ms=int(duration) if isinstance(duration, (int, float)) and duration > 0 else 0,
        explicit=row["isExplicit"] if isinstance(row.get("isExplicit"), bool) else None,
    )
    return PlaylistEntry(position, track, None, label)


def _dig(obj: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


class SpotifyEmbedClient:
    def __init__(self, *, http: httpx.Client | None = None) -> None:
        self._http = http or httpx.Client(
            timeout=httpx.Timeout(15.0, connect=10.0),
            follow_redirects=False,
            headers={"User-Agent": _USER_AGENT, "Accept": "text/html", "Accept-Language": "en"},
        )

    def fetch_playlist(self, playlist_id: str, max_tracks: int) -> Playlist:
        if not _PLAYLIST_ID_RE.match(playlist_id):
            raise InvalidPlaylistUrl(INVALID_URL_MESSAGE)
        url = EMBED_BASE + playlist_id
        for attempt in range(4):
            try:
                with self._http.stream("GET", url) as resp:
                    if resp.status_code == 200:
                        body = bytearray()
                        for chunk in resp.iter_bytes():
                            body.extend(chunk)
                            if len(body) > MAX_PAGE_BYTES:
                                raise SpotifyError(_CHANGED)
                        html = body.decode(resp.encoding or "utf-8", errors="replace")
                        return parse_embed_html(html, playlist_id, max_tracks)
                    status = resp.status_code
                    retry_after = resp.headers.get("Retry-After")
            except httpx.HTTPError as exc:
                log.warning("embed request error for %s: %s", playlist_id, exc)
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise SpotifyError("could not reach spotify. try again shortly.") from exc

            if status == 404:
                raise SpotifyError(
                    "playlist not found. it may be private or deleted. private playlists are not supported.",
                    status_code=404,
                )
            if status == 429 or status >= 500:
                wait = _seconds(retry_after, default=2.0 * (attempt + 1))
                log.warning("embed returned %s for %s; retry in %.0fs", status, playlist_id, wait)
                if wait <= 30 and attempt < 3:
                    time.sleep(wait)
                    continue
                raise SpotifyError(
                    "spotify is busy or rate limiting requests. wait a minute and try again.", status_code=429
                )
            log.warning("embed returned unexpected status %s for %s", status, playlist_id)
            raise SpotifyError("spotify returned an unexpected response. try again shortly.")
        raise SpotifyError("spotify did not respond successfully. try again shortly.")


def _seconds(value: str | None, *, default: float) -> float:
    try:
        return max(1.0, float(value)) if value else default
    except ValueError:
        return default


class PlaylistSourceProtocol(Protocol):
    def fetch_playlist(self, playlist_id: str, max_tracks: int) -> Playlist: ...


class ApiWithEmbedFallback:
    """Use the Web API; if it returns metadata without tracks (development-mode app),
    read the same public playlist from the embed player instead."""

    def __init__(self, api: PlaylistSourceProtocol, embed: PlaylistSourceProtocol) -> None:
        self.api = api
        self.embed = embed

    def fetch_playlist(self, playlist_id: str, max_tracks: int) -> Playlist:
        try:
            return self.api.fetch_playlist(playlist_id, max_tracks)
        except PlaylistItemsUnavailable:
            log.info("web api withheld tracks for %s; falling back to the embed player", playlist_id)
            return self.embed.fetch_playlist(playlist_id, max_tracks)
