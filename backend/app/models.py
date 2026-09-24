"""Domain types shared across the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"

    @property
    def is_active(self) -> bool:
        return self in (JobStatus.QUEUED, JobStatus.PROCESSING)

    @property
    def is_downloadable(self) -> bool:
        return self in (JobStatus.COMPLETE, JobStatus.PARTIAL)


class TrackState(StrEnum):
    PENDING = "pending"
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(slots=True)
class PlaylistEntry:
    """One row of a Spotify playlist, in playlist order (1-based position).

    ``track`` is None when the row cannot be downloaded at all (podcast episode,
    local file, removed track). ``skip_reason`` explains why.
    """

    position: int
    track: TrackInfo | None
    skip_reason: str | None = None
    label: str = ""


@dataclass(slots=True, frozen=True)
class TrackInfo:
    spotify_id: str | None
    title: str
    artists: tuple[str, ...]
    album: str
    duration_ms: int

    @property
    def primary_artist(self) -> str:
        return self.artists[0] if self.artists else ""

    @property
    def display(self) -> str:
        return f"{self.primary_artist} - {self.title}" if self.primary_artist else self.title


@dataclass(slots=True)
class Playlist:
    playlist_id: str
    name: str
    owner: str
    total: int
    entries: list[PlaylistEntry] = field(default_factory=list)
    notice: str | None = None  # e.g. possible truncation by the keyless source


@dataclass(slots=True)
class TrackResult:
    position: int
    label: str
    state: TrackState = TrackState.PENDING
    reason: str | None = None
    filename: str | None = None
    source_url: str | None = None
