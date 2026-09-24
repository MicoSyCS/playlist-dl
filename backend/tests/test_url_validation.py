import pytest

from app.errors import InvalidPlaylistUrl
from app.spotify import extract_playlist_id

PID = "37i9dQZF1DXcBWIGoYBM5M"


@pytest.mark.parametrize(
    "url",
    [
        f"https://open.spotify.com/playlist/{PID}",
        f"https://open.spotify.com/playlist/{PID}/",
        f"https://open.spotify.com/playlist/{PID}?si=abc123def456",
        f"https://open.spotify.com/playlist/{PID}?si=abc#top",
        f"  https://open.spotify.com/playlist/{PID}  ",
        f"https://OPEN.SPOTIFY.COM/playlist/{PID}",
        f"https://open.spotify.com/intl-de/playlist/{PID}",
        f"https://open.spotify.com/intl-pt-br/playlist/{PID}",
        f"open.spotify.com/playlist/{PID}",
    ],
)
def test_accepts_playlist_urls(url: str) -> None:
    assert extract_playlist_id(url) == PID


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "not a url",
        f"http://open.spotify.com/playlist/{PID}",
        f"ftp://open.spotify.com/playlist/{PID}",
        f"https://evil.com/playlist/{PID}",
        f"https://open.spotify.com.evil.com/playlist/{PID}",
        f"https://evil.com#open.spotify.com/playlist/{PID}",
        f"https://user:pass@open.spotify.com/playlist/{PID}",
        f"https://open.spotify.com:8443/playlist/{PID}",
        f"https://open.spotify.com/playlist/{PID}/../../etc/passwd",
        "https://open.spotify.com/playlist/short",
        f"https://open.spotify.com/playlist/{PID}extra",
        f"https://open.spotify.com/playlist/{PID[:-1]}!",
        f"spotify:playlist:{PID}",
        f"https://open.spotify.com/playlists/{PID}",
        f"https://open.spotify.com/playlist/{PID} ; rm -rf /",
        "https://open.spotify.com/" + "a" * 600,
    ],
)
def test_rejects_invalid_urls(url: str) -> None:
    with pytest.raises(InvalidPlaylistUrl):
        extract_playlist_id(url)


@pytest.mark.parametrize("kind", ["album", "track", "artist", "show", "episode"])
def test_other_spotify_links_get_specific_message(kind: str) -> None:
    with pytest.raises(InvalidPlaylistUrl) as info:
        extract_playlist_id(f"https://open.spotify.com/{kind}/{PID}")
    assert "not a playlist" in info.value.message


def test_non_string_rejected() -> None:
    with pytest.raises(InvalidPlaylistUrl):
        extract_playlist_id(None)  # type: ignore[arg-type]
