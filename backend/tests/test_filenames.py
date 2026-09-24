import zipfile
from pathlib import Path

import pytest

from app.files import build_zip, safe_join, sanitize_filename, track_filename, unique_name, zip_name_for


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("AC/DC", "AC DC"),
        ("What?  Is: This*", "What Is This"),
        ("../../etc/passwd", "etc passwd"),
        ("..\\..\\windows", "windows"),
        ("...", "untitled"),
        ("", "untitled"),
        ("   ", "untitled"),
        ("trailing dots...", "trailing dots"),
        (".hidden", "hidden"),
        ("tab\tnew\nline", "tab new line"),
        ("null\x00byte", "null byte"),
        ("Beyoncé – Déjà Vu", "Beyoncé – Déjà Vu"),
        ("坂本龍一", "坂本龍一"),
        ("CON", "_CON"),
        ("nul.txt", "_nul.txt"),
        ('<a href="x">|pipe|</a>', "a href= x pipe a"),
    ],
)
def test_sanitize_filename(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitize_truncates() -> None:
    out = sanitize_filename("x" * 500)
    assert len(out) <= 150


def test_sanitized_names_never_contain_separators() -> None:
    for raw in ["a/b", "a\\b", "/", "\\", "../", "C:\\x", "a:b"]:
        out = sanitize_filename(raw)
        assert "/" not in out and "\\" not in out and ":" not in out and out not in ("", ".", "..")


def test_track_filename_padding_and_format() -> None:
    assert track_filename(1, 35, "Daft Punk", "One More Time") == "01 - Daft Punk - One More Time.mp3"
    assert track_filename(7, 150, "A", "B") == "007 - A - B.mp3"
    assert track_filename(3, 9, "", "Untitled/Track") == "03 - Untitled Track.mp3"


def test_unique_name_avoids_case_insensitive_collisions() -> None:
    taken: set[str] = set()
    assert unique_name("01 - A - B.mp3", taken) == "01 - A - B.mp3"
    assert unique_name("01 - a - b.MP3", taken) == "01 - a - b (2).MP3"
    assert unique_name("01 - A - B.mp3", taken) == "01 - A - B (3).mp3"


def test_zip_name_for() -> None:
    assert zip_name_for("Road Trip / 2024: Best?") == "Road Trip 2024 Best.zip"
    assert zip_name_for("..") == "playlist.zip"


def test_zip_name_clean_suffix_survives_truncation() -> None:
    assert zip_name_for("Road Trip / 2024", clean=True) == "Road Trip 2024 (Clean).zip"
    assert zip_name_for("..", clean=True) == "playlist (Clean).zip"
    long = zip_name_for("x" * 300, clean=True)
    assert long.endswith(" (Clean).zip") and len(long) <= 100 + len(" (Clean).zip")


def test_safe_join_blocks_traversal(tmp_path: Path) -> None:
    assert safe_join(tmp_path, "ok.mp3") == (tmp_path / "ok.mp3").resolve()
    for bad in ["../x", "..", ".", "a/b", "a\\b", ""]:
        with pytest.raises(ValueError):
            safe_join(tmp_path, bad)


def test_build_zip_is_flat_and_rejects_slip(tmp_path: Path) -> None:
    src = tmp_path / "a.mp3"
    src.write_bytes(b"ID3data")
    out = tmp_path / "out.zip"
    build_zip(out, [(src, "01 - A - B.mp3")], {"failed-tracks.txt": "none"})
    with zipfile.ZipFile(out) as zf:
        assert sorted(zf.namelist()) == ["01 - A - B.mp3", "failed-tracks.txt"]
        assert zf.read("01 - A - B.mp3") == b"ID3data"

    for bad in ["../evil.mp3", "sub/evil.mp3", "..\\evil.mp3", "C:evil.mp3", ".."]:
        with pytest.raises(ValueError):
            build_zip(tmp_path / "bad.zip", [(src, bad)], {})
