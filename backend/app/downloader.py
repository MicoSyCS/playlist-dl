"""yt-dlp search/download and FFmpeg conversion.

Uses yt-dlp's Python API (no shell). FFmpeg runs as a subprocess with an argument
list and ``shell=False``; Spotify metadata is passed as discrete ``-metadata`` args.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import yt_dlp
from yt_dlp.utils import DownloadError, match_filter_func

from .errors import TrackError
from .models import TrackInfo

log = logging.getLogger(__name__)


class _Quiet:
    """Route yt-dlp's own logging into ours at debug level."""

    def debug(self, msg: str) -> None:
        if not msg.startswith("[download]"):
            log.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        log.info("yt-dlp warning: %s", msg)

    def error(self, msg: str) -> None:
        log.info("yt-dlp error: %s", msg)


class Aborted(Exception):
    """Raised from a progress hook to stop a download (timeout or job cancellation)."""


def resolve_ffmpeg(ffmpeg_path: str) -> str | None:
    return shutil.which(ffmpeg_path)


def ffmetadata(tags: dict[str, str]) -> str:
    """Render an FFMETADATA1 document, escaping the characters the format reserves."""

    def esc(value: str) -> str:
        out = []
        for ch in value:
            if ch in "=;#\\":
                out.append("\\" + ch)
            elif ord(ch) < 32 or ch == "\x7f":
                out.append(" ")  # newlines/control characters would break the line format
            else:
                out.append(ch)
        return "".join(out).strip()

    lines = [";FFMETADATA1"]
    lines += [f"{key}={esc(value)}" for key, value in tags.items() if value]
    return "\n".join(lines) + "\n"


class AudioEngine:
    def __init__(
        self,
        *,
        ffmpeg: str,
        search_prefix: str,
        search_results: int,
        max_download_mb: int,
        bitrate_kbps: int,
    ) -> None:
        self.ffmpeg = ffmpeg
        self.search_prefix = search_prefix
        self.search_results = search_results
        self.max_download_bytes = max_download_mb * 1024 * 1024
        self.bitrate_kbps = bitrate_kbps

    # -- search ---------------------------------------------------------------

    def search(self, query: str) -> list[dict[str, Any]]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": "in_playlist",
            "noplaylist": True,
            "socket_timeout": 15,
            "logger": _Quiet(),
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"{self.search_prefix}{self.search_results}:{query}", download=False)
        except DownloadError as exc:
            log.info("search failed for %r: %s", query, exc)
            raise TrackError("search failed on the audio source") from exc
        entries = (info or {}).get("entries") or []
        return [e for e in entries if isinstance(e, dict)]

    # -- download -------------------------------------------------------------

    def download_audio(self, url: str, work_dir: Path, *, deadline: float, cancel: threading.Event) -> Path:
        """Download best available audio for ``url`` into ``work_dir``; return the file path."""

        def hook(status: dict[str, Any]) -> None:
            if cancel.is_set():
                raise Aborted("job cancelled")
            if time.monotonic() > deadline:
                raise Aborted("track timed out")

        opts: dict[str, Any] = {
            "format": "bestaudio/best",
            "outtmpl": {"default": str(work_dir / "source.%(ext)s")},
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "overwrites": True,
            "socket_timeout": 20,
            "retries": 2,
            "fragment_retries": 2,
            "max_filesize": self.max_download_bytes,
            "match_filter": match_filter_func("!is_live & !was_live"),
            "progress_hooks": [hook],
            "logger": _Quiet(),
            "cachedir": False,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Aborted as exc:
            raise TrackError(str(exc)) from exc
        except DownloadError as exc:
            if isinstance(getattr(exc, "exc_info", (None, None))[1], Aborted):
                raise TrackError(str(exc.exc_info[1])) from exc
            log.info("download failed for %s: %s", url, exc)
            raise TrackError("download failed on the audio source") from exc

        path: Path | None = None
        for item in (info or {}).get("requested_downloads") or []:
            fp = item.get("filepath")
            if fp and Path(fp).is_file():
                path = Path(fp)
                break
        if path is None:
            files = [p for p in work_dir.glob("source.*") if p.is_file() and not p.name.endswith(".part")]
            if not files:
                raise TrackError("download produced no audio (file too large, live stream or unavailable)")
            path = files[0]
        if path.resolve().parent != work_dir.resolve():
            raise TrackError("download wrote outside the working directory")
        return path

    # -- convert --------------------------------------------------------------

    def convert_to_mp3(
        self,
        source: Path,
        dest: Path,
        track: TrackInfo,
        *,
        position: int,
        total: int,
        timeout: float,
    ) -> None:
        if timeout <= 1:
            raise TrackError("track timed out before conversion")
        # FFmpeg runs inside the per-track work directory and only sees fixed ASCII names.
        # Tags go through a UTF-8 FFMETADATA file rather than argv, which Windows mangles
        # for non-ASCII text; the Unicode output name is applied by Python afterwards.
        work = source.parent
        meta = work / "meta.txt"
        tmp = work / "out.mp3"
        meta.write_text(
            ffmetadata(
                {
                    "title": track.title,
                    "artist": ", ".join(track.artists),
                    "album_artist": track.primary_artist,
                    "album": track.album,
                    "track": f"{position}/{total}",
                }
            ),
            encoding="utf-8",
            newline="\n",
        )
        args = [
            self.ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            source.name,
            "-f",
            "ffmetadata",
            "-i",
            meta.name,
            "-map",
            "0:a:0",
            "-map_metadata",
            "1",
            "-vn",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            f"{self.bitrate_kbps}k",
            "-id3v2_version",
            "3",
            "-write_id3v1",
            "1",
            tmp.name,
        ]
        try:
            proc = subprocess.run(  # noqa: S603 - argument list, shell=False, fixed executable
                args,
                shell=False,
                cwd=work,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            tmp.unlink(missing_ok=True)
            raise TrackError("mp3 conversion timed out") from exc
        except OSError as exc:
            log.error("could not start ffmpeg (%s): %s", self.ffmpeg, exc)
            raise TrackError("ffmpeg is not available on the server") from exc

        if proc.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
            log.info("ffmpeg failed (%s): %s", proc.returncode, proc.stderr.decode("utf-8", "replace")[-500:])
            tmp.unlink(missing_ok=True)
            raise TrackError("mp3 conversion failed")
        shutil.move(tmp, dest)
