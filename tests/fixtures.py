"""Programmatic test fixtures — no binary files in git."""
from pathlib import Path

from ebooklib import epub
import pymupdf


def make_epub(
    path: Path, title: str, author: str, chapters: list[tuple[str, str]]
) -> Path:
    """chapters: list of (chapter_title, html_body) pairs."""
    book = epub.EpubBook()
    book.set_identifier("fixture-id")
    book.set_title(title)
    book.set_language("en")
    book.add_author(author)
    items = []
    for i, (chapter_title, body_html) in enumerate(chapters, start=1):
        item = epub.EpubHtml(
            title=chapter_title, file_name=f"ch{i}.xhtml", lang="en"
        )
        item.content = f"<h1>{chapter_title}</h1>{body_html}"
        book.add_item(item)
        items.append(item)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.toc = items
    book.spine = ["nav", *items]
    epub.write_epub(str(path), book)
    return path


def make_pdf(
    path: Path,
    pages: list[str],
    heading_pages: dict[int, str] | None = None,
    header: str | None = None,
) -> Path:
    """pages: body text per page. heading_pages: {page_index: heading} drawn
    in a large font. header: line repeated at the top of every page."""
    doc = pymupdf.open()
    for i, body in enumerate(pages):
        page = doc.new_page()
        y = 60.0
        if header is not None:
            page.insert_text((72, 40), header, fontsize=9)
        if heading_pages and i in heading_pages:
            page.insert_text((72, 80), heading_pages[i], fontsize=24)
            y = 120.0
        page.insert_textbox(
            pymupdf.Rect(72, y, 520, 780), body, fontsize=11
        )
    doc.save(str(path))
    doc.close()
    return path
