"""Search-query construction and candidate scoring.

Spotify is only the metadata source. We look for the same recording on a
yt-dlp-supported site and score results on title overlap, artist presence and
duration agreement, rejecting obvious mismatches (covers, karaoke, speed edits…).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from .models import TrackInfo

# Variants that are almost never the requested recording unless Spotify's title says so.
_REJECT_WORDS = (
    "karaoke",
    "cover",
    "nightcore",
    "sped up",
    "speed up",
    "slowed",
    "8d audio",
    "reaction",
    "tutorial",
    "lesson",
    "how to play",
    "piano version",
    "guitar version",
    "drum cover",
)
# Variants that are plausible but should lose to a clean match.
_PENALTY_WORDS = (
    "live",
    "remix",
    "instrumental",
    "acoustic",
    "extended",
    "edit",
    "mashup",
    "bass boosted",
    "loop",
    "1 hour",
    "10 hours",
    "full album",
)
_BONUS_WORDS = ("official audio", "provided to youtube", "audio")

_BRACKETS = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_NON_WORD = re.compile(r"[^\w\s]+", re.UNICODE)
_SPOTIFY_SUFFIX = re.compile(
    r"\s+-\s+(\d{4}\s+)?(remaster(ed)?|mono|stereo|single version|radio edit|from .*|.*remaster.*)(\s+\d{4})?$",
    re.IGNORECASE,
)
_FEAT = re.compile(r"\s*[\(\[]?\b(feat\.?|ft\.?|featuring|with)\b.*$", re.IGNORECASE)

MIN_SCORE = 0.58
MIN_TITLE_COVERAGE = 0.5
MAX_CANDIDATE_SECONDS = 20 * 60


@dataclass(slots=True, frozen=True)
class Candidate:
    url: str
    title: str
    duration: float | None
    channel: str

    @classmethod
    def from_entry(cls, entry: dict[str, Any]) -> Candidate | None:
        url = entry.get("webpage_url") or entry.get("url")
        title = entry.get("title")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")) or not isinstance(title, str):
            return None
        duration = entry.get("duration")
        channel = entry.get("channel") or entry.get("uploader") or ""
        return cls(
            url=url,
            title=title,
            duration=float(duration) if isinstance(duration, (int, float)) and duration > 0 else None,
            channel=str(channel),
        )


@dataclass(slots=True, frozen=True)
class Scored:
    candidate: Candidate
    score: float
    reason: str | None  # set when rejected


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ")
    text = _NON_WORD.sub(" ", text)
    return " ".join(text.split())


def core_title(title: str) -> str:
    """Spotify title without remaster/version suffixes and featured-artist credits."""
    t = _SPOTIFY_SUFFIX.sub("", title)
    t = _FEAT.sub("", t)
    return t.strip() or title


def build_search_query(track: TrackInfo, include_album: bool | None = None) -> str:
    """``Artist - Title`` plus the album when the title alone is too generic."""
    title = core_title(track.title)
    parts = [track.primary_artist, title] if track.primary_artist else [title]
    query = " - ".join(p for p in parts if p)
    if include_album is None:
        # A single short word ("Intro", "Home") is too generic to search on its own.
        words = normalize(title).split()
        include_album = len(words) <= 1 and len(title) <= 6
    if include_album and track.album and normalize(track.album) != normalize(title):
        query = f"{query} {track.album}"
    # yt-dlp's search prefix treats ':' specially; keep the query plain text.
    return " ".join(query.replace(":", " ").split())[:200]


def _tokens(text: str) -> list[str]:
    return normalize(text).split()


def _coverage(needle_tokens: list[str], haystack: str) -> float:
    if not needle_tokens:
        return 0.0
    hay = set(_tokens(haystack))
    return sum(1 for t in needle_tokens if t in hay) / len(needle_tokens)


def _contains_phrase(text_norm: str, phrase: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text_norm) is not None


def score_candidate(track: TrackInfo, cand: Candidate) -> Scored:
    title_tokens = _tokens(core_title(track.title))
    cand_title_norm = normalize(cand.title)
    spotify_norm = normalize(track.title + " " + track.album)
    combined = f"{cand.title} {cand.channel}"

    if cand.duration is not None and cand.duration > MAX_CANDIDATE_SECONDS:
        return Scored(cand, 0.0, "result too long")

    for word in _REJECT_WORDS:
        if _contains_phrase(cand_title_norm, word) and not _contains_phrase(spotify_norm, word):
            return Scored(cand, 0.0, f"looks like a {word} version")

    title_cov = _coverage(title_tokens, cand.title)
    if title_cov < MIN_TITLE_COVERAGE:
        return Scored(cand, title_cov * 0.4, "title does not match")

    artist_score = 0.0
    if track.artists:
        artist_score = max(_coverage(_tokens(a), combined) for a in track.artists)

    if track.duration_ms > 0 and cand.duration is not None:
        spotify_s = track.duration_ms / 1000
        tolerance = max(15.0, spotify_s * 0.12)
        diff = abs(cand.duration - spotify_s)
        if diff > tolerance * 2:
            return Scored(cand, 0.0, "duration differs too much")
        duration_score = max(0.0, 1 - diff / (tolerance * 2))
    else:
        duration_score = 0.35

    score = 0.40 * title_cov + 0.25 * artist_score + 0.35 * duration_score

    for word in _PENALTY_WORDS:
        if _contains_phrase(cand_title_norm, word) and not _contains_phrase(spotify_norm, word):
            score -= 0.15
    channel_norm = normalize(cand.channel)
    if channel_norm.endswith(" topic"):
        score += 0.08
    if any(_contains_phrase(cand_title_norm, w) for w in _BONUS_WORDS):
        score += 0.04
    # Extra words beyond artist + title suggest a different upload (compilations, mixes).
    stray = (
        set(_tokens(_BRACKETS.sub(" ", cand.title)))
        - set(title_tokens)
        - {t for a in track.artists for t in _tokens(a)}
    )
    score -= min(0.1, 0.015 * max(0, len(stray) - 3))

    score = max(0.0, min(1.0, score))
    return Scored(cand, score, None if score >= MIN_SCORE else "low match confidence")


def pick_best(track: TrackInfo, entries: list[dict[str, Any]]) -> tuple[Candidate | None, str]:
    """Return the best acceptable candidate, or None with a short reason."""
    scored: list[Scored] = []
    for entry in entries:
        cand = Candidate.from_entry(entry)
        if cand is not None:
            scored.append(score_candidate(track, cand))
    if not scored:
        return None, "no search results"
    accepted = [s for s in scored if s.reason is None]
    if accepted:
        best = max(accepted, key=lambda s: s.score)
        return best.candidate, f"score {best.score:.2f}"
    top = max(scored, key=lambda s: s.score)
    return None, f"no confident match ({top.reason})"
