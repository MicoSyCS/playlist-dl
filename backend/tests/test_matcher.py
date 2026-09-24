from app.matcher import Candidate, build_search_query, core_title, pick_best, score_candidate
from app.models import TrackInfo

TRACK = TrackInfo("id1", "One More Time", ("Daft Punk",), "Discovery", 320_000)


def entry(
    title: str, duration: float | None, channel: str = "", url: str = "https://www.youtube.com/watch?v=x"
) -> dict:
    return {"title": title, "duration": duration, "channel": channel, "url": url}


def test_query_uses_artist_and_title() -> None:
    assert build_search_query(TRACK) == "Daft Punk - One More Time"


def test_query_strips_remaster_suffix_and_feat() -> None:
    t = TrackInfo(None, "Something - 2009 Remaster", ("The Beatles",), "Abbey Road", 180_000)
    assert build_search_query(t) == "The Beatles - Something"
    assert core_title("Stay (feat. Justin Bieber)") == "Stay"


def test_query_adds_album_for_generic_titles() -> None:
    t = TrackInfo(None, "Intro", ("The xx",), "xx", 128_000)
    assert build_search_query(t) == "The xx - Intro xx"


def test_query_removes_colons() -> None:
    t = TrackInfo(None, "Part 2: The Return", ("X",), "", 1)
    assert ":" not in build_search_query(t)


def test_prefers_close_duration_and_topic_channel() -> None:
    entries = [
        entry("Daft Punk - One More Time (Official Video)", 380, "Daft Punk", "https://y/video"),
        entry("One More Time", 321, "Daft Punk - Topic", "https://y/topic"),
        entry("Daft Punk - One More Time (Live)", 330, "fan", "https://y/live"),
    ]
    best, _ = pick_best(TRACK, entries)
    assert best is not None and best.url == "https://y/topic"


def test_rejects_covers_karaoke_and_speed_edits() -> None:
    for title in [
        "One More Time (Karaoke Version)",
        "One More Time - Piano Cover",
        "One More Time sped up",
        "One More Time (Nightcore)",
    ]:
        s = score_candidate(TRACK, Candidate("https://y/1", title, 320, "someone"))
        assert s.reason is not None, title


def test_cover_allowed_when_spotify_title_says_so() -> None:
    t = TrackInfo(None, "Hurt - Cover", ("Johnny Cash",), "American IV", 218_000)
    s = score_candidate(t, Candidate("https://y/1", "Johnny Cash - Hurt (Cover)", 218, "Johnny Cash"))
    # "cover" appears in the Spotify title, so it is not auto-rejected
    assert s.reason is None or "cover" not in s.reason


def test_rejects_large_duration_mismatch() -> None:
    s = score_candidate(TRACK, Candidate("https://y/1", "Daft Punk - One More Time", 600, "Daft Punk"))
    assert s.reason == "duration differs too much"


def test_rejects_unrelated_title() -> None:
    best, why = pick_best(TRACK, [entry("Harder Better Faster Stronger", 320, "Daft Punk")])
    assert best is None and "no confident match" in why


def test_no_results() -> None:
    assert pick_best(TRACK, []) == (None, "no search results")


def test_ignores_entries_without_http_url() -> None:
    assert pick_best(TRACK, [{"title": "One More Time", "url": "file:///etc/passwd", "duration": 320}])[0] is None


def test_accented_titles_match() -> None:
    t = TrackInfo(None, "Déjà Vu", ("Beyoncé",), "B'Day", 240_000)
    best, _ = pick_best(t, [entry("Beyonce - Deja Vu (Audio)", 241, "Beyoncé - Topic")])
    assert best is not None
