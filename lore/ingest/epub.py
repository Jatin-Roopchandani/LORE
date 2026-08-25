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
        # Per-open-block bookkeeping: (parts_list, position_at_open), pushed
        # on a block tag's start tag and popped on its end tag. At close
        # time, parts_list[position_at_open:] is exactly the segment this
        # block (and anything nested inside it) produced, so we can look at
        # its *content* rather than assume a raw length delta means "this
        # block itself emitted text" — the naive length check double-counts
        # a nested block's own separator as the parent's content too.
        self._block_stack: list[tuple[list[str], int]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            self._skip_depth += 1
        elif tag == "hr":
            self._sections[-1]["parts"].append("\n***\n")
        elif tag == "br":
            self._sections[-1]["parts"].append("\n")
        elif tag in _BLOCK_TAGS:
            parts = self._sections[-1]["parts"]
            self._block_stack.append((parts, len(parts)))
        if tag in _HEADING_TAGS:
            self._sections.append({"heading": None, "parts": []})
            # A heading swaps the active section mid-stream. Any block still
            # open (including the heading tag's own just-pushed entry) now
            # continues writing into the NEW section, so its segment must be
            # re-anchored to start at position 0 of the new parts list —
            # otherwise its start position would point into the old list.
            new_parts = self._sections[-1]["parts"]
            self._block_stack = [(new_parts, 0) for _ in self._block_stack]
            self._in_heading = True
            self._heading_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            if self._skip_depth:
                self._skip_depth -= 1
        elif tag in ("hr", "br"):
            pass  # already emitted on the start tag
        elif tag in _BLOCK_TAGS:
            if self._block_stack:
                parts, pos = self._block_stack.pop()
            else:
                parts, pos = self._sections[-1]["parts"], len(self._sections[-1]["parts"])
            seg = "".join(parts[pos:])
            target = self._sections[-1]["parts"]
            if seg.strip():
                # This block (or something nested in it) produced real text.
                # Append exactly enough newlines to end the block with
                # "\n\n": a nested block may already have closed with "\n\n"
                # (add nothing), and a trailing <br> leaves a bare "\n" (top
                # up with one more, not a full "\n\n" — that would make
                # "\n\n\n", a spurious scene break).
                if not seg.endswith("\n\n"):
                    target.append("\n" if seg.endswith("\n") else "\n\n")
            elif seg:
                pass  # nested separators only (e.g. blockquote around an
                # empty <p>) — nothing of our own to add
            else:
                target.append("\n")  # a genuinely empty leaf block
        if tag in _HEADING_TAGS and self._in_heading:
            self._in_heading = False
            heading = " ".join(self._heading_parts).strip()
            self._sections[-1]["heading"] = heading or None

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_heading:
            self._heading_parts.append(data)
        elif data.strip():
            # Whitespace-only text nodes are formatting/indentation between
            # sibling tags (ebooklib pretty-prints its XHTML output, and
            # real-world EPUBs are often indented too) — not real content.
            # Ignoring them keeps paragraph-boundary newline counts exact,
            # so the block-open/close bookkeeping isn't thrown off by an
            # incidental "\n    " between two <p> tags.
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
            # A headingless section is either true front matter (no chapter
            # seen yet -> its own fallback chapter) or the tail of a chapter
            # whose spine document was split mid-chapter (e.g. at an
            # illustration) -> append it to the chapter still in progress.
            if heading is None and chapters:
                prev = chapters[-1]
                chapters[-1] = ParsedChapter(
                    seq=prev.seq,
                    title=prev.title,
                    text=f"{prev.text}\n\n{text}",
                )
                continue
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
