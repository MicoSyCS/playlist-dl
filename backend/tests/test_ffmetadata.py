from app.downloader import ffmetadata


def test_ffmetadata_escapes_reserved_characters() -> None:
    doc = ffmetadata({"title": "a=b; #1 \\ c", "artist": "Line\nBreak\rX", "album": "", "track": "3/12"})
    assert doc.splitlines() == [
        ";FFMETADATA1",
        "title=a\\=b\\; \\#1 \\\\ c",
        "artist=Line Break X",
        "track=3/12",
    ]


def test_ffmetadata_keeps_unicode() -> None:
    assert "title=Beyoncé – Déjà Vu" in ffmetadata({"title": "Beyoncé – Déjà Vu"})
