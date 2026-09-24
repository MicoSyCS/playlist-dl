"""Safe Harbor clean-version logic: normalization, catalog ranking, candidate checks, selection."""

from __future__ import annotations

from typing import Any

import pytest

from app.errors import SpotifyError, TrackError
from app.matcher import Candidate
from app.models import TrackInfo
from app.safe_harbor import (
    CleanStatus,
    clean_queries,
    comparable_title,
    evaluate_candidate,
    is_clean_edit_title,
    rank_clean_catalog,
    score_catalog_candidate,
    select_clean_source,
    version_free_title,
)

EXPLICIT = TrackInfo("orig1", "Bad Words", ("Rapper",), "Album One", 200_000, explicit=True)
CLEAN_CAT = TrackInfo("clean1", "Bad Words - Clean", ("Rapper",), "Album One (Clean)", 199_000, explicit=False)
NON_EXPLICIT = TrackInfo("orig2", "Sunny Day", ("Singer",), "Weather", 180_000, explicit=False)


def cand(title: str, duration: float | None = 200, channel: str = "Rapper", url: str = "https://y/1") -> Candidate:
    return Candidate(url=url, title=title, duration=duration, channel=channel)


def entry(title: str, duration: float = 200, channel: str = "Rapper", url: str | None = None) -> dict[str, Any]:
    return {"title": title, "duration": duration, "channel": channel, "url": url or f"https://y/{abs(hash(title))}"}


# --------------------------------------------------------------------------- normalization


@pytest.mark.parametrize(
    "title",
    [
        "Bad Words (Clean)",
        "Bad Words - Clean Version",
        "Bad Words - Radio Edit",
        "Bad Words [Censored]",
        "Bad Words (Explicit)",
        "Bad Words - Dirty",
        "Bad Words (Uncensored)",
        "Bad Words - 2011 Remastered",
        "Bad Words - Remaster",
        "Bad Words (feat. Guest) - Radio Edit",
    ],
)
def test_version_labels_normalize_away(title: str) -> None:
    assert comparable_title(title) == "bad words"


def test_real_title_words_are_not_stripped() -> None:
    assert version_free_title("Clean") == "Clean"
    assert version_free_title("Dirty Diana") == "Dirty Diana"
    assert version_free_title("Radio (Live Session)") == "Radio (Live Session)"


def test_clean_edit_title_detection() -> None:
    assert is_clean_edit_title("Song - Radio Edit")
    assert is_clean_edit_title("Song (Clean)")
    assert not is_clean_edit_title("Clean")  # a song called "Clean"
    assert not is_clean_edit_title("Song - 2011 Remaster")


def test_clean_queries() -> None:
    assert clean_queries(CLEAN_CAT) == [
        "Rapper Bad Words clean",
        "Rapper Bad Words clean version",
        "Rapper Bad Words radio edit",
    ]


# --------------------------------------------------------------------------- Spotify catalog ranking


def test_catalog_accepts_strong_clean_version() -> None:
    best, _ = rank_clean_catalog(EXPLICIT, [EXPLICIT, CLEAN_CAT])
    assert best is CLEAN_CAT


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        (TrackInfo("x", "Bad Words", ("Rapper",), "Album One", 200_000, explicit=True), "explicit or unrated"),
        (TrackInfo("x", "Bad Words", ("Rapper",), "Album One", 200_000, explicit=None), "explicit or unrated"),
        (
            TrackInfo("x", "Bad Words - Clean", ("Someone Else",), "A", 200_000, explicit=False),
            "different primary artist",
        ),
        (TrackInfo("x", "Other Song - Clean", ("Rapper",), "Album One", 200_000, explicit=False), "different title"),
        (
            TrackInfo("x", "Bad Words - Clean", ("Rapper",), "Album One", 320_000, explicit=False),
            "duration differs too much",
        ),
        (TrackInfo("x", "Bad Words - Live", ("Rapper",), "Live", 200_000, explicit=False), "live version"),
        (
            TrackInfo("orig1", "Bad Words", ("Rapper",), "Album One", 200_000, explicit=False),
            "same track as the original",
        ),
    ],
)
def test_catalog_rejections(candidate: TrackInfo, reason: str) -> None:
    assert score_catalog_candidate(EXPLICIT, candidate).reason == reason


def test_catalog_prefers_same_album() -> None:
    other_album = TrackInfo("c2", "Bad Words (Clean)", ("Rapper",), "Greatest Hits", 200_000, explicit=False)
    best, _ = rank_clean_catalog(EXPLICIT, [other_album, CLEAN_CAT])
    assert best is CLEAN_CAT


def test_catalog_none_found_reports_reason() -> None:
    best, why = rank_clean_catalog(EXPLICIT, [EXPLICIT])
    assert best is None and "no non-explicit version" in why


# --------------------------------------------------------------------------- audio-source candidates


def test_strong_clean_candidate_accepted() -> None:
    v = evaluate_candidate(EXPLICIT, CLEAN_CAT, cand("Rapper - Bad Words (Clean)", 199), require_clean_label=True)
    assert v.reason is None and v.clean_labeled and not v.radio_edit


def test_radio_edit_candidate_flagged() -> None:
    v = evaluate_candidate(EXPLICIT, EXPLICIT, cand("Rapper - Bad Words (Radio Edit)"), require_clean_label=True)
    assert v.reason is None and v.radio_edit


@pytest.mark.parametrize("label", ["Explicit", "Dirty", "Uncensored", "Dirty Version", "Explicit Version"])
def test_explicit_labels_rejected_even_when_clean_not_required(label: str) -> None:
    v = evaluate_candidate(NON_EXPLICIT, NON_EXPLICIT, cand(f"Singer - Sunny Day ({label})", 180, "Singer"),
                           require_clean_label=False)  # fmt: skip
    assert v.reason is not None and v.reason.startswith("labelled")


@pytest.mark.parametrize(
    "variant",
    ["Karaoke", "Instrumental", "Live", "Cover", "Remix", "Sped Up", "Slowed", "Nightcore", "Tribute"],
)
def test_variant_false_positives_rejected(variant: str) -> None:
    v = evaluate_candidate(EXPLICIT, EXPLICIT, cand(f"Rapper - Bad Words (Clean {variant})"), require_clean_label=True)
    assert v.reason is not None


def test_variant_allowed_when_original_is_that_variant() -> None:
    live = TrackInfo("l", "Bad Words - Live", ("Rapper",), "Live Album", 200_000, explicit=False)
    v = evaluate_candidate(live, live, cand("Rapper - Bad Words (Live)"), require_clean_label=False)
    assert v.reason is None


def test_duration_and_title_mismatch_rejected() -> None:
    long = evaluate_candidate(EXPLICIT, EXPLICIT, cand("Rapper - Bad Words (Clean)", 400), require_clean_label=True)
    other = evaluate_candidate(EXPLICIT, EXPLICIT, cand("Rapper - Good Vibes (Clean)"), require_clean_label=True)
    assert long.reason == "duration differs too much"
    assert other.reason == "title does not match"


def test_unlabelled_result_rejected_for_explicit_track() -> None:
    v = evaluate_candidate(EXPLICIT, EXPLICIT, cand("Rapper - Bad Words (Official Audio)"), require_clean_label=True)
    assert v.reason == "not labelled clean"


def test_song_names_containing_label_words_are_protected() -> None:
    diana = TrackInfo("d", "Dirty Diana", ("Michael Jackson",), "Bad", 280_000, explicit=False)
    v = evaluate_candidate(diana, diana, cand("Michael Jackson - Dirty Diana", 281, "Michael Jackson"),
                           require_clean_label=False)  # fmt: skip
    assert v.reason is None


# --------------------------------------------------------------------------- selection


class Catalog:
    def __init__(self, result: list[TrackInfo] | Exception) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def search_tracks(self, artist: str, title: str) -> list[TrackInfo]:
        self.calls.append((artist, title))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Search:
    def __init__(self, table: dict[str, list[dict[str, Any]]], default: list[dict[str, Any]] | None = None) -> None:
        self.table = table
        self.default = default or []
        self.queries: list[str] = []

    def __call__(self, query: str) -> list[dict[str, Any]]:
        self.queries.append(query)
        for key, results in self.table.items():
            if query.endswith(key):
                return results
        return self.default


def test_non_explicit_track_uses_normal_query_and_stays_eligible() -> None:
    search = Search({}, default=[entry("Singer - Sunny Day (Official Audio)", 180, "Singer")])
    catalog = Catalog([])
    d = select_clean_source(NON_EXPLICIT, search, catalog)
    assert d.status is CleanStatus.ORIGINAL_CLEAN and d.candidate is not None
    assert search.queries == ["Singer - Sunny Day"]
    assert catalog.calls == []  # no catalog lookup needed


def test_non_explicit_track_still_rejects_explicit_labelled_result() -> None:
    search = Search({}, default=[entry("Singer - Sunny Day (Explicit)", 180, "Singer")])
    d = select_clean_source(NON_EXPLICIT, search, None)
    assert d.candidate is None and d.status is CleanStatus.LOW_CONFIDENCE


def test_explicit_track_searches_catalog_for_non_explicit_version() -> None:
    catalog = Catalog([EXPLICIT, CLEAN_CAT])
    search = Search({" clean": [entry("Rapper - Bad Words (Clean)", 199)]})
    d = select_clean_source(EXPLICIT, search, catalog)
    assert catalog.calls == [("Rapper", "Bad Words")]
    assert d.status is CleanStatus.CLEAN_MATCHED and d.candidate is not None
    assert search.queries[0] == "Rapper Bad Words clean"


def test_radio_edit_match_status() -> None:
    search = Search({" radio edit": [entry("Rapper - Bad Words (Radio Edit)")]})
    d = select_clean_source(EXPLICIT, search, Catalog([CLEAN_CAT]))
    assert d.status is CleanStatus.RADIO_EDIT_MATCHED
    assert search.queries == ["Rapper Bad Words clean", "Rapper Bad Words clean version", "Rapper Bad Words radio edit"]


def test_missing_catalog_clean_version_is_unavailable_without_audio_search() -> None:
    search = Search({}, default=[entry("Rapper - Bad Words (Clean)")])
    d = select_clean_source(EXPLICIT, search, Catalog([EXPLICIT]))
    assert d.candidate is None and d.status is CleanStatus.UNAVAILABLE
    assert d.reason.startswith("clean version unavailable")
    assert search.queries == []


def test_never_falls_back_to_known_explicit_result() -> None:
    perfect_explicit = [
        entry("Rapper - Bad Words (Official Audio)", 200, "Rapper - Topic"),
        entry("Rapper - Bad Words (Explicit)", 200),
        entry("Rapper - Bad Words (Uncensored)", 200),
    ]
    search = Search({}, default=perfect_explicit)
    d = select_clean_source(EXPLICIT, search, None)
    assert d.candidate is None and d.status is CleanStatus.UNAVAILABLE


def test_keyless_mode_requires_clean_label_for_explicit_tracks() -> None:
    search = Search({" clean version": [entry("Rapper - Bad Words (Clean Version)")]})
    d = select_clean_source(EXPLICIT, search, None)
    assert d.status is CleanStatus.CLEAN_MATCHED


def test_unknown_explicit_flag_is_treated_as_explicit() -> None:
    unknown = TrackInfo("u", "Bad Words", ("Rapper",), "", 200_000, explicit=None)
    search = Search({}, default=[entry("Rapper - Bad Words (Official Audio)")])
    d = select_clean_source(unknown, search, None)
    assert d.candidate is None and d.status is CleanStatus.UNAVAILABLE


def test_catalog_error_falls_back_to_label_only_path() -> None:
    search = Search({" clean": [entry("Rapper - Bad Words (Clean)")]})
    d = select_clean_source(EXPLICIT, search, Catalog(SpotifyError("rate limited", status_code=429)))
    assert d.status is CleanStatus.CLEAN_MATCHED


def test_all_searches_failing_raises_track_error() -> None:
    def broken(query: str) -> list[dict[str, Any]]:
        raise TrackError("search failed on the audio source")

    with pytest.raises(TrackError):
        select_clean_source(EXPLICIT, broken, None)
