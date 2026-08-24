"""EPUB → ParsedBook via ebooklib + stdlib HTML text extraction."""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import ebooklib
from ebooklib import epub

from lore.ingest.models import IngestError, ParsedBook, ParsedChapter

_BLOCK_TAGS = {
    "p", "div", "section", "article", "blockquote", "li", "tr",
    "h1", "h2", "h3", "h4", "h5", "h6", "br",
}
_HEADING_TAGS = {"h1", "h2", "h3"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0
        self._in_heading = False
        self.first_heading: str | None = None
        self._heading_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            self._skip_depth += 1
        elif tag == "hr":
            self._parts.append("\n***\n")
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n\n")
        if tag in _HEADING_TAGS and self.first_heading is None:
            self._in_heading = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n\n")
        if tag in _HEADING_TAGS and self._in_heading:
            self._in_heading = False
            heading = " ".join(self._heading_parts).strip()
            if heading:
                self.first_heading = heading

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_heading:
            self._heading_parts.append(data)
        else:
            self._parts.append(data)

    @property
    def text(self) -> str:
        raw = "".join(self._parts)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r" ?\n ?", "\n", raw)
        return re.sub(r"\n{4,}", "\n\n\n", raw).strip()


def _metadata(book: epub.EpubBook, field: str) -> str | None:
    values = book.get_metadata("DC", field)
    return values[0][0] if values else None


def _is_chapter(item) -> bool:
    """Check if item is a chapter. Fallback for older ebooklib versions."""
    # Try using is_chapter() method if available
    if hasattr(item, 'is_chapter') and callable(item.is_chapter):
        try:
            return item.is_chapter()
        except Exception:
            pass
    # Fallback: check if filename contains "nav"
    file_name = getattr(item, 'file_name', '')
    return 'nav' not in file_name.lower()


def parse_epub(path: Path) -> ParsedBook:
    try:
        book = epub.read_epub(str(path), options={"ignore_ncx": True})
    except Exception as exc:
        raise IngestError(f"cannot read EPUB {path.name}: {exc}") from exc

    chapters: list[ParsedChapter] = []
    seq = 0
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        if not _is_chapter(item):  # nav documents etc.
            continue
        extractor = _TextExtractor()
        extractor.feed(item.get_content().decode("utf-8", errors="replace"))
        text = extractor.text
        if not text:
            continue
        seq += 1
        chapters.append(
            ParsedChapter(
                seq=seq,
                title=extractor.first_heading or f"Chapter {seq}",
                text=text,
            )
        )
    if not chapters:
        raise IngestError(f"EPUB {path.name} contains no readable chapters")
    return ParsedBook(
        title=_metadata(book, "title") or path.stem,
        author=_metadata(book, "creator"),
        chapters=chapters,
    )
