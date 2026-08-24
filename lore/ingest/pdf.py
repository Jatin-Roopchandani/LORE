"""PDF → ParsedBook (basic): heading-font chapters, header/footer strip.

PDFs carry no semantic structure, so this is heuristic by design (SPEC:
"EPUB-first, PDF basic"). Scanned PDFs (no text layer) are rejected.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pymupdf

from lore.ingest.models import IngestError, ParsedBook, ParsedChapter

_FALLBACK_PAGES_PER_CHAPTER = 15
_HEADING_SCALE = 1.5  # a heading is >= 1.5x the median font size
_MIN_TEXT_CHARS = 500


def _page_lines(page: pymupdf.Page) -> list[tuple[str, float]]:
    """Lines on the page as (text, max_font_size)."""
    lines: list[tuple[str, float]] = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = "".join(s["text"] for s in spans).strip()
            if text:
                lines.append((text, max(s["size"] for s in spans)))
    return lines


def _strip_repeated_edges(pages: list[list[tuple[str, float]]]) -> None:
    """Remove header/footer lines: first/last line repeated on >=50% of pages."""
    if len(pages) < 3:
        return
    edges = Counter()
    for lines in pages:
        if lines:
            edges[re.sub(r"\d+", "#", lines[0][0])] += 1
            edges[re.sub(r"\d+", "#", lines[-1][0])] += 1
    repeated = {t for t, n in edges.items() if n >= len(pages) * 0.5 * 1}
    for lines in pages:
        while lines and re.sub(r"\d+", "#", lines[0][0]) in repeated:
            lines.pop(0)
        while lines and re.sub(r"\d+", "#", lines[-1][0]) in repeated:
            lines.pop()


def parse_pdf(path: Path) -> ParsedBook:
    try:
        doc = pymupdf.open(str(path))
    except Exception as exc:
        raise IngestError(f"cannot read PDF {path.name}: {exc}") from exc
    try:
        if doc.is_encrypted:
            raise IngestError(f"PDF {path.name} is encrypted")

        pages = [_page_lines(page) for page in doc]
        _strip_repeated_edges(pages)

        all_sizes = [size for lines in pages for _, size in lines]
        if sum(len(t) for lines in pages for t, _ in lines) < _MIN_TEXT_CHARS:
            raise IngestError(
                f"PDF {path.name} has no usable text layer (scanned book?)"
            )
        sizes = sorted(all_sizes)
        median = sizes[len(sizes) // 2]

        # A heading starts a chapter: big font, short line.
        starts: list[tuple[int, int, str]] = []  # (page_idx, line_idx, title)
        for p_idx, lines in enumerate(pages):
            for l_idx, (text, size) in enumerate(lines):
                if size >= median * _HEADING_SCALE and len(text) < 80:
                    starts.append((p_idx, l_idx, text))

        chapters: list[ParsedChapter] = []
        if len(starts) >= 2:
            for c_idx, (p_idx, l_idx, title) in enumerate(starts):
                end_p, end_l = (
                    (starts[c_idx + 1][0], starts[c_idx + 1][1])
                    if c_idx + 1 < len(starts)
                    else (len(pages), 0)
                )
                parts: list[str] = []
                for pi in range(p_idx, min(end_p + 1, len(pages))):
                    for li, (text, _) in enumerate(pages[pi]):
                        after_start = pi > p_idx or li > l_idx
                        before_end = pi < end_p or li < end_l
                        if after_start and before_end:
                            parts.append(text)
                body = "\n".join(parts).strip()
                if body:
                    chapters.append(
                        ParsedChapter(seq=len(chapters) + 1, title=title, text=body)
                    )
        if not chapters:  # fallback: fixed page ranges
            for start in range(0, len(pages), _FALLBACK_PAGES_PER_CHAPTER):
                page_range = pages[start : start + _FALLBACK_PAGES_PER_CHAPTER]
                body = "\n".join(t for lines in page_range for t, _ in lines).strip()
                if body:
                    chapters.append(
                        ParsedChapter(
                            seq=len(chapters) + 1,
                            title=f"Pages {start + 1}-{min(start + len(page_range), len(pages))}",
                            text=body,
                        )
                    )
        if not chapters:
            raise IngestError(f"PDF {path.name} produced no chapters")
        return ParsedBook(
            title=(doc.metadata or {}).get("title") or path.stem,
            author=(doc.metadata or {}).get("author") or None,
            chapters=chapters,
        )
    finally:
        doc.close()
