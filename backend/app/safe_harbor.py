"""Safe Harbor: prefer clean / radio-edit versions and never knowingly pick an explicit one.

Everything here is best effort and label based. Spotify's ``explicit`` flag and catalog
tell us whether a clean version *exists*; the downloaded audio is only trusted as clean
when the source result is itself labelled clean (for tracks Spotify marks explicit).
No audio or lyric analysis is performed. When confidence is low the track is skipped.

Flow for one playlist track (``select_clean_source``):

* Spotify says non-explicit: normal query; accept the usual match unless the result is
  labelled explicit/dirty/uncensored or is a different variant (live, remix, ...).
  If the Spotify title itself is a clean/radio edit, a clean label is required downstream.
* Spotify says explicit (or doesn't say): with API credentials, search Spotify's catalog
  for an ``explicit=false`` version (``rank_clean_catalog``); none found -> "clean version
  unavailable". Then search the audio source with "clean" / "clean version" / "radio edit"
  queries and accept only results labelled clean that also match title, artist and
  duration (``evaluate_candidate``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol

from .errors import TrackError, UserFacingError
from .matcher import Candidate, build_search_query, core_title, duration_tolerance, normalize, score_candidate
from .models import TrackInfo

log = logging.getLogger(__name__)


class CleanStatus(StrEnum):
    ORIGINAL_CLEAN = "original_clean"
    CLEAN_MATCHED = "clean_matched"
    RADIO_EDIT_MATCHED = "radio_edit_matched"
    UNAVAILABLE = "clean_unavailable"
    LOW_CONFIDENCE = "low_confidence"
    FAILED = "download_failed"

    @property
    def label(self) -> str:
        return _STATUS_LABELS[self]


_STATUS_LABELS = {
    CleanStatus.ORIGINAL_CLEAN: "original version already non-explicit",
    CleanStatus.CLEAN_MATCHED: "clean version matched",
    CleanStatus.RADIO_EDIT_MATCHED: "radio edit matched",
    CleanStatus.UNAVAILABLE: "clean version unavailable",
    CleanStatus.LOW_CONFIDENCE: "match rejected due to low confidence",
    CleanStatus.FAILED: "download or conversion failed",
}

# Phrases that mark a result as a clean edit.
CLEAN_LABELS = ("clean version", "clean edit", "clean", "radio edit", "radio version", "censored", "edited version")
RADIO_LABELS = ("radio edit", "radio version")
# Phrases that mark a result as explicit. Never accepted in Safe Harbor mode.
DIRTY_LABELS = ("explicit", "uncensored", "dirty", "unedited", "uncut", "nsfw", "parental advisory")
# Different recordings/edits that must not be substituted unless the original is one.
VARIANT_WORDS = (
    "karaoke",
    "instrumental",
    "live",
    "cover",
    "remix",
    "sped up",
    "speed up",
    "slowed",
    "reverb",
    "nightcore",
    "tribute",
    "acoustic",
    "mashup",
    "8d",
    "bass boosted",
    "acapella",
    "a cappella",
)
# Tokens that only describe a version; removed before comparing titles.
_VERSION_TOKENS = frozenset(
    {"clean", "censored", "explicit", "dirty", "uncensored", "radio", "edit", "edited", "version", "remaster",
     "remastered", "album", "single", "unedited"}
)  # fmt: skip
_LABEL_CORE = frozenset({"clean", "censored", "explicit", "dirty", "uncensored", "radio", "edited", "remaster",
                         "remastered", "unedited"})  # fmt: skip

_BRACKET = re.compile(r"[\(\[\{]([^\)\]\}]*)[\)\]\}]")

SAFE_MIN_SCORE = 0.70  # stricter than the normal matcher's 0.58
CATALOG_MIN_SCORE = 0.70
CLEAN_LABEL_BONUS = 0.05


class CleanCatalog(Protocol):
    def search_tracks(self, artist: str, title: str) -> list[TrackInfo]: ...


# --------------------------------------------------------------------------- text helpers


def _has_phrase(text_norm: str, phrase: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text_norm) is not None


def _found(text_norm: str, phrases: tuple[str, ...], protected_norm: str) -> str | None:
    """First phrase present in ``text_norm`` that is not part of the (protected) original title."""
    for phrase in phrases:
        if _has_phrase(text_norm, phrase) and not _has_phrase(protected_norm, phrase):
            return phrase
    return None


def _is_version_label(segment: str) -> bool:
    tokens = normalize(segment).split()
    return (
        bool(tokens)
        and all(t in _VERSION_TOKENS or t.isdigit() for t in tokens)
        and any(t in _LABEL_CORE for t in tokens)
    )


def version_free_title(title: str) -> str:
    """Strip version labels from a title: ``Song (Clean)``, ``Song - Radio Edit``,
    ``Song [Explicit]``, ``Song - 2011 Remastered`` all become ``Song``."""
    text = _BRACKET.sub(lambda m: " " if _is_version_label(m.group(1)) else m.group(0), title)
    parts = re.split(r"\s+-\s+", text)
    while len(parts) > 1 and _is_version_label(parts[-1]):
        parts.pop()
    return " ".join(" - ".join(parts).split()) or title


def comparable_title(title: str) -> str:
    """Normalized title without version labels or featured-artist credits."""
    return normalize(core_title(version_free_title(title)))


def is_clean_edit_title(title: str) -> bool:
    """True when a Spotify title carries a clean/radio-edit *version label*, e.g.
    "Song - Radio Edit" or "Song (Clean)". A song simply named "Clean" is not one."""
    segments = [m.group(1) for m in _BRACKET.finditer(title)] + re.split(r"\s+-\s+", title)[1:]
    return any(_is_version_label(s) and _found(normalize(s), CLEAN_LABELS, "") for s in segments)


# --------------------------------------------------------------------------- Spotify catalog


@dataclass(slots=True, frozen=True)
class CatalogVerdict:
    track: TrackInfo
    score: float
    reason: str | None  # None when acceptable


def score_catalog_candidate(original: TrackInfo, cand: TrackInfo) -> CatalogVerdict:
    """Is ``cand`` (from Spotify search) a non-explicit version of ``original``?"""
    if cand.explicit is not False:
        return CatalogVerdict(cand, 0.0, "explicit or unrated")
    if original.spotify_id and cand.spotify_id == original.spotify_id:
        return CatalogVerdict(cand, 0.0, "same track as the original")
    orig_artists = {normalize(a) for a in original.artists}
    if not orig_artists & {normalize(a) for a in cand.artists} or normalize(cand.primary_artist) not in orig_artists:
        return CatalogVerdict(cand, 0.0, "different primary artist")
    variant = _found(normalize(cand.title), VARIANT_WORDS, normalize(original.title))
    if variant:
        return CatalogVerdict(cand, 0.0, f"{variant} version")
    if comparable_title(cand.title) != comparable_title(original.title):
        return CatalogVerdict(cand, 0.0, "different title")

    closeness = 0.5
    if original.duration_ms > 0 and cand.duration_ms > 0:
        orig_s = original.duration_ms / 1000
        diff = abs(cand.duration_ms / 1000 - orig_s)
        tolerance = duration_tolerance(orig_s)
        if diff > tolerance * 2:
            return CatalogVerdict(cand, 0.0, "duration differs too much")
        closeness = 1 - diff / (tolerance * 2)

    if original.album and cand.album:
        a, b = (
            set(normalize(version_free_title(original.album)).split()),
            set(normalize(version_free_title(cand.album)).split()),
        )
        album_score = 1.0 if a == b else (len(a & b) / max(len(a | b), 1))
    else:
        album_score = 0.5  # unknown (keyless source has no album names)

    score = 0.5 + 0.3 * closeness + 0.2 * album_score
    return CatalogVerdict(cand, round(score, 3), None if score >= CATALOG_MIN_SCORE else "low confidence")


def rank_clean_catalog(original: TrackInfo, candidates: list[TrackInfo]) -> tuple[TrackInfo | None, str]:
    verdicts = [score_catalog_candidate(original, c) for c in candidates]
    for v in verdicts:
        log.debug("safe_harbor catalog candidate id=%s score=%.2f verdict=%s", v.track.spotify_id, v.score,
                  v.reason or "accept")  # fmt: skip
    accepted = [v for v in verdicts if v.reason is None]
    if not accepted:
        reasons = sorted({v.reason for v in verdicts if v.reason})
        return None, "no non-explicit version in spotify's catalog" + (f" ({', '.join(reasons)})" if reasons else "")
    best = max(accepted, key=lambda v: v.score)
    return best.track, f"catalog score {best.score:.2f}"


# --------------------------------------------------------------------------- audio-source candidates


@dataclass(slots=True, frozen=True)
class Verdict:
    candidate: Candidate
    score: float
    reason: str | None  # None when accepted
    clean_labeled: bool = False
    radio_edit: bool = False


def _scrub_labels(title: str, protected_tokens: set[str]) -> str:
    """Drop version-label words (unless the real title uses them) so "Song (Clean Radio Edit)"
    scores like "Song" in the shared matcher instead of being penalised as an "edit"."""
    kept = [t for t in normalize(title).split() if t not in _VERSION_TOKENS or t in protected_tokens]
    return " ".join(kept)


def evaluate_candidate(
    original: TrackInfo, target: TrackInfo, cand: Candidate, *, require_clean_label: bool
) -> Verdict:
    """Decide whether an audio-source result is an acceptable clean match.

    ``original`` is the playlist track (its title protects words like "Live" or "Dirty" that
    are genuinely part of the song name); ``target`` is what we match against (a verified
    clean catalog version, or the original).
    """
    cand_norm = normalize(cand.title)
    # Words that are really part of the song name (not version labels) are protected,
    # e.g. "Dirty Diana", "Clean", "Live Your Life".
    protected = normalize(f"{version_free_title(original.title)} {version_free_title(target.title)}")

    dirty = _found(cand_norm, DIRTY_LABELS, protected)
    if dirty:
        return Verdict(cand, 0.0, f"labelled {dirty}")
    variant = _found(cand_norm, VARIANT_WORDS, f"{protected} {normalize(original.album)}")
    if variant:
        return Verdict(cand, 0.0, f"{variant} version")

    clean_phrase = _found(cand_norm, CLEAN_LABELS, protected)
    radio = _found(cand_norm, RADIO_LABELS, protected) is not None
    if require_clean_label and clean_phrase is None:
        return Verdict(cand, 0.0, "not labelled clean")

    scrubbed = replace(cand, title=_scrub_labels(cand.title, set(protected.split())))
    match_track = replace(target, title=version_free_title(target.title))
    base = score_candidate(match_track, scrubbed)
    if base.reason is not None:  # title/artist/duration mismatch or matcher-level rejection
        return Verdict(cand, base.score, base.reason, clean_phrase is not None, radio)

    score = min(1.0, base.score + (CLEAN_LABEL_BONUS if clean_phrase else 0.0))
    if score < SAFE_MIN_SCORE:
        return Verdict(cand, score, "low match confidence", clean_phrase is not None, radio)
    return Verdict(cand, score, None, clean_phrase is not None, radio)


def clean_queries(target: TrackInfo) -> list[str]:
    """Focused searches: "Artist Track clean", "... clean version", "... radio edit"."""
    base = build_search_query(replace(target, title=version_free_title(target.title))).replace(" - ", " ", 1)
    return [f"{base} clean", f"{base} clean version", f"{base} radio edit"]


# --------------------------------------------------------------------------- orchestration


@dataclass(slots=True, frozen=True)
class CleanDecision:
    candidate: Candidate | None
    status: CleanStatus
    detail: str
    score: float = 0.0

    @property
    def reason(self) -> str:
        """User-facing reason for skipped tracks."""
        return f"{self.status.label} ({self.detail})" if self.detail else self.status.label


SearchFn = Callable[[str], list[dict[str, Any]]]


def select_clean_source(
    original: TrackInfo,
    search: SearchFn,
    catalog: CleanCatalog | None,
    *,
    context: str = "",
) -> CleanDecision:
    """Pick a clean audio-source result for ``original`` or explain why none is safe."""
    explicit = original.explicit is not False  # unknown counts as explicit
    target = original

    if explicit and catalog is not None:
        try:
            found = catalog.search_tracks(original.primary_artist, core_title(version_free_title(original.title)))
        except UserFacingError as exc:
            # Catalog unavailable: continue with the stricter label-only path (still requires "clean").
            log.info("safe_harbor %s catalog=unavailable reason=%r", context, exc.message)
        else:
            clean_track, why = rank_clean_catalog(original, found)
            log.info("safe_harbor %s catalog_candidates=%d result=%s", context, len(found),
                     "accept" if clean_track else "none")  # fmt: skip
            if clean_track is None:
                return CleanDecision(None, CleanStatus.UNAVAILABLE, why)
            target = clean_track

    title_is_clean_edit = is_clean_edit_title(original.title)
    require_label = explicit or title_is_clean_edit
    queries = clean_queries(target) if require_label else [build_search_query(original)]

    seen: set[str] = set()
    rejected: dict[str, int] = {}
    search_errors = 0
    for query in queries:
        try:
            entries = search(query)
        except TrackError:
            search_errors += 1
            continue
        verdicts: list[Verdict] = []
        for entry in entries:
            cand = Candidate.from_entry(entry)
            if cand is None or cand.url in seen:
                continue
            seen.add(cand.url)
            v = evaluate_candidate(original, target, cand, require_clean_label=require_label)
            verdicts.append(v)
            log.debug("safe_harbor %s candidate verdict=%s score=%.2f url=%s", context, v.reason or "accept",
                      v.score, cand.url)  # fmt: skip
            if v.reason:
                key = v.reason.split(" (")[0]
                rejected[key] = rejected.get(key, 0) + 1
        accepted = [v for v in verdicts if v.reason is None]
        if accepted:
            best = max(accepted, key=lambda v: v.score)
            if not require_label:
                status = CleanStatus.ORIGINAL_CLEAN
            elif best.radio_edit:
                status = CleanStatus.RADIO_EDIT_MATCHED
            else:
                status = CleanStatus.CLEAN_MATCHED
            log.info("safe_harbor %s decision=accept status=%s score=%.2f explicit=%s catalog=%s", context,
                     status.value, best.score, explicit, target is not original)  # fmt: skip
            return CleanDecision(best.candidate, status, "", best.score)

    if search_errors == len(queries):
        raise TrackError("search failed on the audio source")
    summary = ", ".join(f"{k} x{n}" for k, n in sorted(rejected.items(), key=lambda kv: -kv[1])[:3]) or "no results"
    status = CleanStatus.UNAVAILABLE if require_label else CleanStatus.LOW_CONFIDENCE
    log.info("safe_harbor %s decision=skip status=%s explicit=%s rejected=%s", context, status.value, explicit,
             summary)  # fmt: skip
    detail = "no result labelled clean passed matching" if require_label else "no safe match"
    return CleanDecision(None, status, f"{detail}: {summary}")
