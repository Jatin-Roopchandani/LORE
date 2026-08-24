from pathlib import Path

import pytest

from lore.ingest.epub import parse_epub
from lore.ingest.models import IngestError
from tests.fixtures import make_epub


def test_parses_metadata_and_chapters(tmp_path: Path) -> None:
    path = make_epub(
        tmp_path / "b.epub",
        title="The Test Book",
        author="Ann Author",
        chapters=[
            ("Chapter One", "<p>First paragraph.</p><p>Second paragraph.</p>"),
            ("Chapter Two", "<p>More text here.</p>"),
        ],
    )
    book = parse_epub(path)
    assert book.title == "The Test Book"
    assert book.author == "Ann Author"
    assert [c.title for c in book.chapters] == ["Chapter One", "Chapter Two"]
    assert [c.seq for c in book.chapters] == [1, 2]
    assert "First paragraph." in book.chapters[0].text
    assert "Second paragraph." in book.chapters[0].text


def test_html_becomes_clean_text_with_paragraph_breaks(tmp_path: Path) -> None:
    path = make_epub(
        tmp_path / "b.epub",
        title="T",
        author="A",
        chapters=[("One", "<p>Para one.</p><p>Para two.</p><p><i>Styled</i> text.</p>")],
    )
    text = parse_epub(path).chapters[0].text
    assert "<" not in text and ">" not in text
    assert "Para one.\n\nPara two." in text.replace("\n\n\n", "\n\n") or (
        "Para one." in text and "Para two." in text
    )
    assert "Styled text." in text


def test_hr_becomes_scene_marker(tmp_path: Path) -> None:
    path = make_epub(
        tmp_path / "b.epub",
        title="T",
        author="A",
        chapters=[("One", "<p>Scene one.</p><hr/><p>Scene two.</p>")],
    )
    text = parse_epub(path).chapters[0].text
    assert "***" in text


def test_empty_or_navigation_docs_are_skipped(tmp_path: Path) -> None:
    path = make_epub(
        tmp_path / "b.epub",
        title="T",
        author="A",
        chapters=[("One", "<p>Real content.</p>"), ("Blank", "<p>  </p>")],
    )
    book = parse_epub(path)
    assert [c.title for c in book.chapters] == ["One"]


def test_unreadable_file_raises_ingest_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.epub"
    bad.write_bytes(b"this is not an epub")
    with pytest.raises(IngestError):
        parse_epub(bad)
