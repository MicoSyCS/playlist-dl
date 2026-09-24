"""Spotify Web API access (metadata only) and playlist URL validation.

The user-supplied URL is never fetched. We extract the playlist ID with a strict
pattern and only ever talk to the fixed ``api.spotify.com`` / ``accounts.spotify.com``
hosts using server-side client credentials.

Handles both the current schema (``items`` / ``item``, February 2026 onwards) and the
legacy schema (``tracks`` / ``track``).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

import httpx

from .errors import (
    InvalidPlaylistUrl,
    PlaylistItemsUnavailable,
    PlaylistTooLarge,
    SpotifyError,
    SpotifyNotConfigured,
)
from .models import Playlist, PlaylistEntry, TrackInfo

log = logging.getLogger(__name__)

API_BASE = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"  # noqa: S105 - endpoint URL, not a secret
MAX_URL_LENGTH = 512

_PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
_PLAYLIST_PATH_RE = re.compile(r"^/(?:intl-[a-z]{2}(?:-[a-z]{2,4})?/)?playlist/(?P<id>[A-Za-z0-9]{22})/?$")

INVALID_URL_MESSAGE = "invalid link. expected https://open.spotify.com/playlist/ followed by a playlist id."


def extract_playlist_id(raw_url: str) -> str:
    """Return the 22-character playlist ID from an open.spotify.com playlist URL.

    Raises ``InvalidPlaylistUrl`` for anything else (albums, tracks, other hosts,
    credentials in the URL, custom ports, non-https schemes).
    """
    if not isinstance(raw_url, str):
        raise InvalidPlaylistUrl(INVALID_URL_MESSAGE)
    url = raw_url.strip()
    if not url or len(url) > MAX_URL_LENGTH or any(c.isspace() for c in url):
        raise InvalidPlaylistUrl(INVALID_URL_MESSAGE)
    if url.lower().startswith("open.spotify.com/"):
        url = "https://" + url

    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise InvalidPlaylistUrl(INVALID_URL_MESSAGE) from exc

    if (
        parts.scheme.lower() != "https"
        or (parts.hostname or "").lower() != "open.spotify.com"
        or parts.username is not None
        or parts.password is not None
        or port is not None
    ):
        raise InvalidPlaylistUrl(INVALID_URL_MESSAGE)

    match = _PLAYLIST_PATH_RE.match(parts.path)
    if not match:
        if re.match(r"^/(?:intl-[^/]+/)?(album|track|artist|show|episode|user)/", parts.path):
            raise InvalidPlaylistUrl("that is a spotify link, but not a playlist. only playlist links are supported.")
        raise InvalidPlaylistUrl(INVALID_URL_MESSAGE)
    return match.group("id")


# --------------------------------------------------------------------------- parsing


def _paging_container(playlist_json: dict[str, Any]) -> dict[str, Any] | None:
    """Return the paging object holding playlist rows, for either schema."""
    for key in ("items", "tracks"):
        value = playlist_json.get(key)
        if isinstance(value, dict):
            return value
    return None


def parse_entry(row: dict[str, Any], position: int) -> PlaylistEntry:
    """Convert one playlist row into a PlaylistEntry."""
    obj = row.get("item")
    if obj is None:
        obj = row.get("track")

    if not isinstance(obj, dict):
        return PlaylistEntry(position, None, "track unavailable or removed from spotify", "unknown track")

    title = str(obj.get("name") or "").strip()
    artists = tuple(
        str(a.get("name")).strip() for a in obj.get("artists") or [] if isinstance(a, dict) and a.get("name")
    )
    album_obj = obj.get("album")
    album = str(album_obj.get("name") or "").strip() if isinstance(album_obj, dict) else ""
    label = f"{artists[0]} - {title}" if artists and title else (title or "unknown track")

    kind = obj.get("type", "track")
    if kind == "episode" or obj.get("episode") is True:
        return PlaylistEntry(position, None, "podcast episode (not supported)", label)
    if kind != "track":
        return PlaylistEntry(position, None, f"unsupported item type: {kind}", label)
    if row.get("is_local") or obj.get("is_local"):
        return PlaylistEntry(position, None, "local file on the owner's device (not on spotify)", label)
    if not title:
        return PlaylistEntry(position, None, "track has no title in spotify metadata", label)

    track = TrackInfo(
        spotify_id=obj.get("id") if isinstance(obj.get("id"), str) else None,
        title=title,
        artists=artists,
        album=album,
        duration_ms=int(obj.get("duration_ms") or 0),
        explicit=obj["explicit"] if isinstance(obj.get("explicit"), bool) else None,
    )
    return PlaylistEntry(position, track, None, label)


SEARCH_LIMIT = 10  # current Search API maximum per request


def parse_search_tracks(data: dict[str, Any]) -> list[TrackInfo]:
    """Track objects from a ``GET /search?type=track`` response (episodes/local files dropped)."""
    tracks_obj = data.get("tracks")
    items = tracks_obj.get("items") if isinstance(tracks_obj, dict) else None
    out: list[TrackInfo] = []
    for pos, obj in enumerate(items or [], start=1):
        if not isinstance(obj, dict):
            continue
        entry = parse_entry({"track": obj}, pos)
        if entry.track is not None:
            out.append(entry.track)
    return out


def _search_term(text: str) -> str:
    """Make free text safe inside a quoted Search API field filter."""
    return " ".join(text.replace('"', " ").replace(":", " ").split())[:100]


def parse_rows(rows: Iterable[Any], start_position: int) -> list[PlaylistEntry]:
    entries: list[PlaylistEntry] = []
    pos = start_position
    for row in rows:
        entries.append(parse_entry(row if isinstance(row, dict) else {}, pos))
        pos += 1
    return entries


# --------------------------------------------------------------------------- client


class SpotifyClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        market: str = "US",
        http: httpx.Client | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._market = market
        self._http = http or httpx.Client(timeout=httpx.Timeout(15.0, connect=10.0))
        self._token: str | None = None
        self._token_expiry = 0.0
        self._lock = threading.Lock()

    # -- auth ---------------------------------------------------------------

    def _get_token(self, force: bool = False) -> str:
        if not (self._client_id and self._client_secret):
            raise SpotifyNotConfigured(
                "the server is missing spotify api credentials. set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET."
            )
        with self._lock:
            if not force and self._token and time.monotonic() < self._token_expiry - 60:
                return self._token
            try:
                resp = self._http.post(
                    TOKEN_URL,
                    data={"grant_type": "client_credentials"},
                    auth=(self._client_id, self._client_secret),
                )
            except httpx.HTTPError as exc:
                log.warning("spotify token request failed: %s", exc)
                raise SpotifyError("could not reach spotify. try again shortly.") from exc
            if resp.status_code in (400, 401):
                log.error("spotify rejected client credentials: %s %s", resp.status_code, resp.text[:200])
                raise SpotifyNotConfigured("the server's spotify credentials were rejected.")
            if resp.status_code != 200:
                log.warning("spotify token endpoint returned %s", resp.status_code)
                raise SpotifyError("spotify authentication is unavailable. try again shortly.")
            body = resp.json()
            self._token = str(body["access_token"])
            self._token_expiry = time.monotonic() + float(body.get("expires_in", 3600))
            return self._token

    # -- requests -----------------------------------------------------------

    def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET a Spotify API URL with retry for 401 (token refresh), 429 and 5xx."""
        if not url.startswith(API_BASE + "/"):
            # Pagination "next" links come from Spotify; refuse anything off-host.
            raise SpotifyError("spotify returned an unexpected pagination link.")

        forced_refresh = False
        for attempt in range(5):
            token = self._get_token(force=forced_refresh)
            try:
                resp = self._http.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as exc:
                log.warning("spotify request error (%s): %s", url, exc)
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise SpotifyError("could not reach spotify. try again shortly.") from exc

            if resp.status_code == 200:
                data = resp.json()
                if not isinstance(data, dict):
                    raise SpotifyError("spotify returned an unexpected response.")
                return data
            if resp.status_code == 401 and not forced_refresh:
                forced_refresh = True
                continue
            if resp.status_code == 429:
                retry_after = _retry_after_seconds(resp)
                log.warning("spotify rate limited; retry-after=%ss", retry_after)
                if retry_after <= 30 and attempt < 4:
                    time.sleep(retry_after)
                    continue
                raise SpotifyError("spotify is rate limiting requests. wait a minute and try again.", status_code=429)
            if resp.status_code >= 500 and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code == 404:
                raise SpotifyError(
                    "playlist not found. it may be private, deleted, or a spotify-generated playlist "
                    "that the api does not expose. private playlists are not supported.",
                    status_code=404,
                )
            if resp.status_code == 403:
                log.warning("spotify 403 for %s: %s", url, resp.text[:300])
                raise SpotifyError(
                    "spotify denied access to this playlist. private and some editorial playlists are not supported.",
                    status_code=403,
                )
            log.warning("spotify returned %s for %s: %s", resp.status_code, url, resp.text[:300])
            raise SpotifyError("spotify returned an error. try again shortly.")
        raise SpotifyError("spotify did not respond successfully. try again shortly.")

    # -- public -------------------------------------------------------------

    def fetch_playlist(self, playlist_id: str, max_tracks: int) -> Playlist:
        if not _PLAYLIST_ID_RE.match(playlist_id):
            raise InvalidPlaylistUrl(INVALID_URL_MESSAGE)

        data = self._get(
            f"{API_BASE}/playlists/{playlist_id}",
            params={"market": self._market, "additional_types": "track,episode"},
        )
        name = str(data.get("name") or "spotify playlist")
        owner_obj = data.get("owner")
        owner = str(owner_obj.get("display_name") or owner_obj.get("id") or "") if isinstance(owner_obj, dict) else ""

        container = _paging_container(data)
        if container is None:
            # Development-mode apps receive metadata only for playlists they don't own.
            log.warning("playlist %s returned metadata without items (spotify app access level)", playlist_id)
            raise PlaylistItemsUnavailable(
                "spotify returned this playlist's name but not its tracks. apps in spotify's "
                "development mode can only read tracks of playlists owned by the app's user; "
                "reading other public playlists requires an extended quota spotify app.",
                status_code=403,
            )

        total = int(container.get("total") or 0)
        if total > max_tracks:
            raise PlaylistTooLarge(f"playlist has {total} tracks; this server's limit is {max_tracks}.")
        playlist = Playlist(playlist_id=playlist_id, name=name, owner=owner, total=total)
        if total == 0:
            return playlist

        first_rows = container.get("items")
        next_url = container.get("next")
        if isinstance(first_rows, list) and first_rows:
            playlist.entries.extend(parse_rows(first_rows, 1))
        else:
            # Paging object without rows: request pages explicitly.
            next_url = None
            playlist.entries.extend(self._fetch_rows_explicit(playlist_id, total))

        while isinstance(next_url, str) and next_url and len(playlist.entries) < total:
            page = self._get(next_url)
            rows = page.get("items") or []
            if not rows:
                break
            playlist.entries.extend(parse_rows(rows, len(playlist.entries) + 1))
            next_url = page.get("next")

        playlist.entries = playlist.entries[:max_tracks]
        return playlist

    def search_tracks(self, artist: str, title: str) -> list[TrackInfo]:
        """Catalog search used by Safe Harbor to find other (e.g. non-explicit) versions of a track."""
        query = f'track:"{_search_term(title)}" artist:"{_search_term(artist)}"'
        data = self._get(
            f"{API_BASE}/search",
            params={"q": query, "type": "track", "limit": SEARCH_LIMIT, "market": self._market},
        )
        return parse_search_tracks(data)

    def _fetch_rows_explicit(self, playlist_id: str, total: int) -> list[PlaylistEntry]:
        entries: list[PlaylistEntry] = []
        endpoints = [
            (f"{API_BASE}/playlists/{playlist_id}/items", 50),
            (f"{API_BASE}/playlists/{playlist_id}/tracks", 100),
        ]
        for url, limit in endpoints:
            try:
                while len(entries) < total:
                    page = self._get(
                        url,
                        params={
                            "offset": len(entries),
                            "limit": limit,
                            "market": self._market,
                            "additional_types": "track,episode",
                        },
                    )
                    rows = page.get("items") or []
                    if not rows:
                        break
                    entries.extend(parse_rows(rows, len(entries) + 1))
                return entries
            except SpotifyError as exc:
                if exc.status_code == 404 and not entries:
                    continue  # try the other (legacy/new) endpoint
                raise
        return entries


def _retry_after_seconds(resp: httpx.Response) -> float:
    try:
        return max(1.0, float(resp.headers.get("Retry-After", "5")))
    except ValueError:
        return 5.0
