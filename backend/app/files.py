"""Filename sanitisation and safe ZIP packaging."""

from __future__ import annotations

import re
import unicodedata
import zipfile
from collections.abc import Iterable
from pathlib import Path

_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_FORBIDDEN_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_WHITESPACE = re.compile(r"\s+")
MAX_COMPONENT_LENGTH = 150


def sanitize_filename(name: str, *, fallback: str = "untitled", max_length: int = MAX_COMPONENT_LENGTH) -> str:
    """Return a single safe path component (no directories, no traversal).

    Keeps Unicode letters (artist names are rarely ASCII), strips path separators,
    control and shell-hostile characters, collapses whitespace, removes leading dots
    and trailing dots/spaces, and avoids Windows reserved device names.
    """
    text = unicodedata.normalize("NFC", str(name))
    text = _FORBIDDEN_CHARS.sub(" ", text)
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")  # other control/format chars
    text = _WHITESPACE.sub(" ", text).strip()
    text = text.lstrip(". ").rstrip(". ")
    if len(text) > max_length:
        text = text[:max_length].rstrip(". ")
    if not text or set(text) <= {"."}:
        text = fallback
    if text.split(".")[0].upper() in _WINDOWS_RESERVED:
        text = f"_{text}"
    return text


def track_filename(position: int, total: int, artist: str, title: str) -> str:
    """Build ``01 - Artist - Title.mp3`` with zero-padding sized to the playlist."""
    width = max(2, len(str(total)))
    stem = f"{position:0{width}d} - {artist} - {title}" if artist else f"{position:0{width}d} - {title}"
    return sanitize_filename(stem, fallback=f"{position:0{width}d}", max_length=MAX_COMPONENT_LENGTH - 4) + ".mp3"


def unique_name(name: str, taken: set[str]) -> str:
    """Return ``name`` or ``name (2)``… so it doesn't collide (case-insensitively) with ``taken``.

    Adds the chosen name to ``taken``.
    """
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    candidate = name
    n = 2
    while candidate.lower() in taken:
        candidate = f"{stem} ({n}).{ext}" if ext else f"{stem} ({n})"
        n += 1
    taken.add(candidate.lower())
    return candidate


def zip_name_for(playlist_title: str, *, clean: bool = False) -> str:
    """``Playlist.zip``, or ``Playlist (Clean).zip`` for Safe Harbor jobs.

    The suffix is added after sanitising/truncating so it is never cut off.
    """
    base = sanitize_filename(playlist_title, fallback="playlist", max_length=100)
    return f"{base} (Clean).zip" if clean else f"{base}.zip"


def safe_join(base: Path, name: str) -> Path:
    """Join a single filename onto ``base``, refusing anything that escapes it."""
    if not name or name in (".", "..") or "/" in name or "\\" in name or Path(name).name != name:
        raise ValueError(f"unsafe filename: {name!r}")
    base_resolved = base.resolve()
    target = (base_resolved / name).resolve()
    if target.parent != base_resolved:
        raise ValueError(f"path escapes base directory: {name!r}")
    return target


def build_zip(zip_path: Path, files: Iterable[tuple[Path, str]], extra_text: dict[str, str]) -> None:
    """Write a ZIP of ``(source_path, archive_name)`` pairs plus in-memory text files.

    Archive names must already be flat, sanitised filenames; anything containing a path
    separator or traversal is rejected (zip-slip protection for whoever extracts it).
    MP3s are stored without recompression.
    """
    tmp = zip_path.with_suffix(".zip.part")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for source, arcname in files:
            _check_arcname(arcname)
            zf.write(source, arcname=arcname)
        for arcname, text in extra_text.items():
            _check_arcname(arcname)
            zf.writestr(arcname, text.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
    tmp.replace(zip_path)


def _check_arcname(arcname: str) -> None:
    if (
        not arcname
        or "/" in arcname
        or "\\" in arcname
        or arcname in (".", "..")
        or arcname.startswith(".")
        or ":" in arcname
    ):
        raise ValueError(f"unsafe archive member name: {arcname!r}")
