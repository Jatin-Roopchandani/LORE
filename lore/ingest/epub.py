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
    """Splits a document's text into sections at every heading tag.

    Each heading (h1/h2/h3) starts a new section: the heading's own text
    becomes that section's title (kept out of the body), and everything
    until the next heading (or end of document) becomes the section's body.
    Text before the first heading forms a leading section with title None.
    """

    def __init__(self) -> None:
        super().__init__()
        self._sections: list[dict] = [{"heading": None, "parts": []}]
        self._skip_depth = 0
        self._in_heading = False
        self._heading_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            self._skip_depth += 1
        elif tag == "hr":
            self._sections[-1]["parts"].append("\n***\n")
        elif tag in _BLOCK_TAGS:
            self._sections[-1]["parts"].append("\n\n")
        if tag in _HEADING_TAGS:
            self._sections.append({"heading": None, "parts": []})
            self._in_heading = True
            self._heading_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._sections[-1]["parts"].append("\n\n")
        if tag in _HEADING_TAGS and self._in_heading:
            self._in_heading = False
            heading = " ".join(self._heading_parts).strip()
            self._sections[-1]["heading"] = heading or None

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_heading:
            self._heading_parts.append(data)
        else:
            self._sections[-1]["parts"].append(data)

    @staticmethod
    def _clean(raw: str) -> str:
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r" ?\n ?", "\n", raw)
        return re.sub(r"\n{4,}", "\n\n\n", raw).strip()

    @property
    def sections(self) -> list[tuple[str | None, str]]:
        """(heading title or None, body text) per section; empty ones dropped."""
        result = []
        for section in self._sections:
            text = self._clean("".join(section["parts"]))
            if text:
                result.append((section["heading"], text))
        return result


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
        for heading, text in extractor.sections:
            seq += 1
            chapters.append(
                ParsedChapter(
                    seq=seq,
                    title=heading or f"Chapter {seq}",
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
