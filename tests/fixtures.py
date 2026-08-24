"""Programmatic test fixtures — no binary files in git."""
from pathlib import Path

from ebooklib import epub


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
