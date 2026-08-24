from pathlib import Path

import pytest

from lore.ingest.models import IngestError
from lore.ingest.pdf import parse_pdf
from tests.fixtures import make_pdf


def _body(tag: str) -> str:
    return " ".join(f"{tag} body sentence number {i} with several words." for i in range(30))


def test_headings_become_chapters(tmp_path: Path) -> None:
    path = make_pdf(
        tmp_path / "b.pdf",
        pages=[_body("one"), _body("one-cont"), _body("two"), _body("two-cont")],
        heading_pages={0: "Chapter One", 2: "Chapter Two"},
    )
    book = parse_pdf(path)
    assert [c.title for c in book.chapters] == ["Chapter One", "Chapter Two"]
    assert "one body sentence" in book.chapters[0].text
    assert "one-cont body sentence" in book.chapters[0].text
    assert "two body sentence" in book.chapters[1].text


def test_no_headings_falls_back_to_page_ranges(tmp_path: Path) -> None:
    path = make_pdf(tmp_path / "b.pdf", pages=[_body(f"p{i}") for i in range(20)])
    book = parse_pdf(path)
    assert len(book.chapters) == 2  # 20 pages / 15-page fallback
    assert book.chapters[0].title.startswith("Pages ")


def test_repeated_headers_are_stripped(tmp_path: Path) -> None:
    path = make_pdf(
        tmp_path / "b.pdf",
        pages=[_body(f"p{i}") for i in range(4)],
        header="MY BOOK TITLE",
    )
    book = parse_pdf(path)
    combined = " ".join(c.text for c in book.chapters)
    assert "MY BOOK TITLE" not in combined


def test_scanned_pdf_rejected(tmp_path: Path) -> None:
    import pymupdf

    doc = pymupdf.open()
    for _ in range(3):
        doc.new_page()  # image-less, text-less pages
    empty = tmp_path / "scan.pdf"
    doc.save(str(empty))
    doc.close()
    with pytest.raises(IngestError, match="text"):
        parse_pdf(empty)


def test_encrypted_pdf_rejected(tmp_path: Path) -> None:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "secret content " * 50, fontsize=11)
    locked = tmp_path / "locked.pdf"
    doc.save(
        str(locked),
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw="owner-secret",
        user_pw="user-secret",
    )
    doc.close()
    with pytest.raises(IngestError, match="encrypted"):
        parse_pdf(locked)
