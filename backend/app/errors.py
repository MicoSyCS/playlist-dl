"""Exceptions whose message is safe to show to the browser.

Anything that is not a ``UserFacingError`` is logged server-side and replaced
with a generic message before it reaches a client.
"""

from __future__ import annotations


class UserFacingError(Exception):
    status_code = 400

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code


class InvalidPlaylistUrl(UserFacingError):
    status_code = 422


class SpotifyNotConfigured(UserFacingError):
    status_code = 503


class SpotifyError(UserFacingError):
    status_code = 502


class PlaylistItemsUnavailable(SpotifyError):
    """The Web API returned playlist metadata without its tracks (development-mode app)."""

    status_code = 403


class PlaylistTooLarge(UserFacingError):
    status_code = 422


class CapacityExceeded(UserFacingError):
    status_code = 429


class TrackError(Exception):
    """A single track failed; the reason is short and safe to write to failed.txt."""
