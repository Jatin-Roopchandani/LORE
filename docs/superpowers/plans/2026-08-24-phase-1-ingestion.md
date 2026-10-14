# LORE Phase 1 — Ingestion & Chunking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `lore ingest book.epub` turns an EPUB or PDF into deterministic, structure-aware chunks registered in SQLite — idempotently (same book twice = no-op).

**Architecture:** Both parsers produce a shared `ParsedBook` intermediate; the chunker consumes only that. Chunking is deterministic: pysbd sentence segmentation, scene splitting on typographic markers, greedy packing to a ~1600-char budget with 2-sentence overlap, hard chapter boundaries, IDs derived from the file's SHA-256. A v2 SQLite migration adds the `chunks` table; the registry writes book + chunks in one transaction. No embeddings in this phase (they happen in the Phase 2 Kaggle notebook).

**Tech Stack:** Python 3.12, uv, ebooklib (EPUB), pymupdf (PDF), pysbd (sentences), stdlib `html.parser`, existing SQLite migration runner, typer CLI.

**Spec:** `SPEC.md` §5.1, §6, §9 Phase 1; design decisions approved in chat: ~400-token (1600-char) chunks with 2-sentence overlap; chapters + typographic scene markers (no ML); EPUB-first with basic PDF (font-size heading heuristic, header/footer stripping, scanned PDFs rejected).

## Global Constraints

- Python **3.12** via **uv** (`uv run`, `uv add` — never bare pip).
- **Git identity (HARD GATE):** before EVERY commit, `git config user.email` must print exactly `jatinroopchandani3007@gmail.com`; anything else → STOP. Commit messages end with the two trailer lines (exactly as in Task 1 Step 5).
- TDD per task: failing test → implement → pass. Full suite (`uv run pytest -m 'not live' -v`) green before each commit; output pristine (no warnings).
- Tests use `tmp_path` and programmatically built fixtures — no binary files committed, nothing under `data/`.
- Chunk budget: **1600 chars** (~400 tokens at 4 chars/token); overlap: **2 sentences**; `book_id` = **sha256(file bytes) hexdigest[:16]**; chunk id = `{book_id}:{chapter_seq}:{seq}` (`seq` counts per chapter).
- Chunks NEVER span chapters. Scenes are split on marker lines (`***`-style, `---`-style) and on runs of 3+ consecutive newlines (a visibly blank gap between paragraphs); packing runs per scene.
- New runtime deps for this phase: `ebooklib`, `pymupdf`, `pysbd` (add with `uv add` in the task that first needs each).

---

### Task 1: Ingest Models + Book Identity

**Files:**
- Create: `lore/ingest/__init__.py`, `lore/ingest/models.py`
- Test: `tests/test_ingest_models.py`

**Interfaces:**
- Consumes: nothing.
- Produces (all later tasks import these):
  - `@dataclass(frozen=True) ParsedChapter(seq: int, title: str, text: str)`
  - `@dataclass(frozen=True) ParsedBook(title: str, author: str | None, chapters: list[ParsedChapter])`
  - `@dataclass(frozen=True) Chunk(id: str, book_id: str, chapter_seq: int, scene_seq: int, seq: int, text: str)`
  - `book_id_for_file(path: Path) -> str` — sha256 of file bytes, first 16 hex chars
  - `class IngestError(Exception)` — the one error type all ingest failures raise

- [ ] **Step 1: Write the failing tests**

`tests/test_ingest_models.py`:
```python
from pathlib import Path

from lore.ingest.models import (
    Chunk,
    IngestError,
    ParsedBook,
    ParsedChapter,
    book_id_for_file,
)


def test_book_id_is_deterministic_and_content_addressed(tmp_path: Path) -> None:
    a = tmp_path / "a.epub"
    b = tmp_path / "b.epub"
    a.write_bytes(b"same content")
    b.write_bytes(b"same content")
    c = tmp_path / "c.epub"
    c.write_bytes(b"different content")
    assert book_id_for_file(a) == book_id_for_file(b)
    assert book_id_for_file(a) != book_id_for_file(c)
    assert len(book_id_for_file(a)) == 16
    assert all(ch in "0123456789abcdef" for ch in book_id_for_file(a))


def test_models_are_frozen() -> None:
    chapter = ParsedChapter(seq=1, title="One", text="Hello.")
    book = ParsedBook(title="T", author=None, chapters=[chapter])
    chunk = Chunk(id="ab:1:0", book_id="ab", chapter_seq=1, scene_seq=0, seq=0, text="Hello.")
    for obj, attr in ((chapter, "title"), (book, "title"), (chunk, "text")):
        try:
            setattr(obj, attr, "changed")
            raise AssertionError("expected FrozenInstanceError")
        except AttributeError:
            pass


def test_ingest_error_is_exception() -> None:
    assert issubclass(IngestError, Exception)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ingest_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lore.ingest'`.

- [ ] **Step 3: Implement**

`lore/ingest/__init__.py`: empty file.

`lore/ingest/models.py`:
```python
"""Parser-agnostic intermediate types for the ingestion pipeline."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path


class IngestError(Exception):
    """Any failure while parsing or chunking a book."""


@dataclass(frozen=True)
class ParsedChapter:
    seq: int
    title: str
    text: str


@dataclass(frozen=True)
class ParsedBook:
    title: str
    author: str | None
    chapters: list[ParsedChapter] = field(default_factory=list)


@dataclass(frozen=True)
class Chunk:
    id: str
    book_id: str
    chapter_seq: int
    scene_seq: int
    seq: int
    text: str


def book_id_for_file(path: Path) -> str:
    """Content-addressed book identity: same bytes, same id — forever."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ingest_models.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git config user.email   # HARD GATE: must print jatinroopchandani3007@gmail.com
git add lore/ingest/ tests/test_ingest_models.py
git commit -m "feat: ingest models and content-addressed book identity"
```

---

### Task 2: Deterministic Chunker

**Files:**
- Create: `lore/ingest/chunker.py`
- Test: `tests/test_chunker.py`

**Interfaces:**
- Consumes: `ParsedBook`, `ParsedChapter`, `Chunk` from Task 1.
- Produces: `chunk_book(book: ParsedBook, book_id: str, budget_chars: int = 1600, overlap_sentences: int = 2) -> list[Chunk]`. Guarantees: deterministic; chunks never span chapters or scenes; `seq` counts 0.. per chapter; `scene_seq` counts 0.. per chapter; consecutive chunks within one scene share `overlap_sentences` sentences.

- [ ] **Step 1: Add the dependency**

Run: `uv add pysbd`
Expected: resolves cleanly (pure-Python package).

- [ ] **Step 2: Write the failing tests**

`tests/test_chunker.py`:
```python
from lore.ingest.chunker import chunk_book
from lore.ingest.models import ParsedBook, ParsedChapter

BOOK_ID = "abcdef0123456789"


def _book(chapters: list[tuple[str, str]]) -> ParsedBook:
    return ParsedBook(
        title="T",
        author="A",
        chapters=[
            ParsedChapter(seq=i + 1, title=t, text=x)
            for i, (t, x) in enumerate(chapters)
        ],
    )


def _long_text(n_sentences: int) -> str:
    return " ".join(
        f"This is sentence number {i} padding the scene with enough words to matter."
        for i in range(n_sentences)
    )


def test_deterministic() -> None:
    book = _book([("One", _long_text(60)), ("Two", _long_text(40))])
    first = chunk_book(book, BOOK_ID)
    second = chunk_book(book, BOOK_ID)
    assert first == second
    assert len(first) > 2


def test_ids_and_chapter_boundaries() -> None:
    book = _book([("One", _long_text(60)), ("Two", _long_text(60))])
    chunks = chunk_book(book, BOOK_ID)
    for chunk in chunks:
        assert chunk.id == f"{BOOK_ID}:{chunk.chapter_seq}:{chunk.seq}"
        assert chunk.book_id == BOOK_ID
    seqs_ch1 = [c.seq for c in chunks if c.chapter_seq == 1]
    seqs_ch2 = [c.seq for c in chunks if c.chapter_seq == 2]
    assert seqs_ch1 == list(range(len(seqs_ch1)))  # per-chapter numbering
    assert seqs_ch2 == list(range(len(seqs_ch2)))  # restarts at 0


def test_budget_respected() -> None:
    book = _book([("One", _long_text(120))])
    for chunk in chunk_book(book, BOOK_ID):
        # budget + slack for the sentence that tipped it over
        assert len(chunk.text) < 1600 + 200


def test_overlap_between_consecutive_chunks() -> None:
    book = _book([("One", _long_text(120))])
    chunks = chunk_book(book, BOOK_ID)
    assert len(chunks) >= 2
    # the first two sentences of chunk n+1 are the last two of chunk n
    prev, nxt = chunks[0].text, chunks[1].text
    assert prev[-80:] in nxt  # the overlap makes chunk n's ending appear in n+1


def test_scene_markers_split_scenes() -> None:
    text = _long_text(10) + "\n\n***\n\n" + _long_text(10)
    book = _book([("One", text)])
    chunks = chunk_book(book, BOOK_ID)
    scene_seqs = {c.scene_seq for c in chunks}
    assert scene_seqs == {0, 1}
    # no chunk mixes text from both scenes
    for chunk in chunks:
        assert "***" not in chunk.text


def test_blank_line_runs_split_scenes() -> None:
    text = _long_text(10) + "\n\n\n" + _long_text(10)
    book = _book([("One", text)])
    chunks = chunk_book(book, BOOK_ID)
    assert {c.scene_seq for c in chunks} == {0, 1}


def test_empty_chapter_produces_no_chunks() -> None:
    book = _book([("One", "   \n\n  "), ("Two", _long_text(5))])
    chunks = chunk_book(book, BOOK_ID)
    assert all(c.chapter_seq == 2 for c in chunks)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_chunker.py -v`
Expected: FAIL with `ModuleNotFoundError` for `lore.ingest.chunker`.

- [ ] **Step 4: Implement `lore/ingest/chunker.py`**

```python
"""Deterministic structure-aware chunker.

Chapters are hard boundaries; scenes (typographic markers or blank-line
runs) are hard boundaries too; within a scene, sentences are greedily
packed to a character budget with a fixed sentence overlap. Same input,
same output — chunk IDs must be stable across runs forever.
"""
from __future__ import annotations

import re

import pysbd

from lore.ingest.models import Chunk, ParsedBook

# A scene marker is a line of ***, ---, ### (3+, spaces allowed between).
_SCENE_MARKER = re.compile(
    r"\n[ \t]*(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:#[ \t]*){3,})[ \t]*\n"
)
# Two or more consecutive blank lines also end a scene.
_BLANK_RUN = re.compile(r"\n{3,}")

_SEGMENTER = pysbd.Segmenter(language="en", clean=False)


def _split_scenes(text: str) -> list[str]:
    scenes: list[str] = []
    for part in _SCENE_MARKER.split("\n" + text + "\n"):
        scenes.extend(_BLANK_RUN.split(part))
    return [s.strip() for s in scenes if s.strip()]


def _sentences(scene_text: str) -> list[str]:
    flat = re.sub(r"\s+", " ", scene_text).strip()
    return [s.strip() for s in _SEGMENTER.segment(flat) if s.strip()]


def _pack(
    sentences: list[str], budget_chars: int, overlap_sentences: int
) -> list[list[str]]:
    packs: list[list[str]] = []
    current: list[str] = []
    size = 0
    for sentence in sentences:
        if current and size + len(sentence) + 1 > budget_chars:
            packs.append(current)
            current = current[-overlap_sentences:] if overlap_sentences else []
            size = sum(len(s) + 1 for s in current)
        current.append(sentence)
        size += len(sentence) + 1
    if current:
        packs.append(current)
    # A trailing pack that is pure overlap adds nothing — drop it.
    if len(packs) > 1 and len(packs[-1]) <= overlap_sentences:
        packs.pop()
    return packs


def chunk_book(
    book: ParsedBook,
    book_id: str,
    budget_chars: int = 1600,
    overlap_sentences: int = 2,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    for chapter in book.chapters:
        seq = 0
        for scene_seq, scene in enumerate(_split_scenes(chapter.text)):
            for pack in _pack(_sentences(scene), budget_chars, overlap_sentences):
                chunks.append(
                    Chunk(
                        id=f"{book_id}:{chapter.seq}:{seq}",
                        book_id=book_id,
                        chapter_seq=chapter.seq,
                        scene_seq=scene_seq,
                        seq=seq,
                        text=" ".join(pack),
                    )
                )
                seq += 1
    return chunks
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_chunker.py -v`
Expected: 7 passed. If `test_overlap_between_consecutive_chunks`'s crude suffix assertion proves brittle against pysbd's exact segmentation, tighten the TEST to compare actual sentence lists (split both chunks with the same segmenter and assert `sentences(prev)[-2:] == sentences(next)[:2]`) — never weaken the overlap guarantee itself.

- [ ] **Step 6: Commit**

```bash
git config user.email   # HARD GATE: must print jatinroopchandani3007@gmail.com
git add lore/ingest/chunker.py tests/test_chunker.py pyproject.toml uv.lock
git commit -m "feat: deterministic scene-aware sentence-packing chunker"
```

---

### Task 3: EPUB Parser

**Files:**
- Create: `lore/ingest/epub.py`, `tests/fixtures.py`
- Test: `tests/test_epub.py`

**Interfaces:**
- Consumes: `ParsedBook`, `ParsedChapter`, `IngestError` from Task 1.
- Produces: `parse_epub(path: Path) -> ParsedBook` (raises `IngestError` on unreadable/empty EPUB). Also the shared test helper `tests/fixtures.py::make_epub(path: Path, title: str, author: str, chapters: list[tuple[str, str]]) -> Path` used again by Task 5's CLI tests.

- [ ] **Step 1: Add the dependency**

Run: `uv add ebooklib`
Expected: resolves cleanly.

- [ ] **Step 2: Write the fixture helper and failing tests**

`tests/fixtures.py`:
```python
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
```

`tests/test_epub.py`:
```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_epub.py -v`
Expected: FAIL with `ModuleNotFoundError` for `lore.ingest.epub`.

- [ ] **Step 4: Implement `lore/ingest/epub.py`**

```python
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


def parse_epub(path: Path) -> ParsedBook:
    try:
        book = epub.read_epub(str(path))
    except Exception as exc:
        raise IngestError(f"cannot read EPUB {path.name}: {exc}") from exc

    chapters: list[ParsedChapter] = []
    seq = 0
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        if item.is_chapter() is False:  # nav documents etc.
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
```

Implementer note: ebooklib API details vary slightly by version (`is_chapter()` may not exist on older versions — fall back to skipping items whose file name contains "nav"). Adapt the implementation, never the tests. ebooklib may emit `UserWarning`s about ignore options; if test output shows warnings, silence them at the source with the documented `read_epub(..., options={"ignore_ncx": True})` or a targeted `filterwarnings` entry in `pyproject.toml` for ebooklib only — document which in your report.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_epub.py -v`
Expected: 5 passed, no warnings.

- [ ] **Step 6: Commit**

```bash
git config user.email   # HARD GATE: must print jatinroopchandani3007@gmail.com
git add lore/ingest/epub.py tests/test_epub.py tests/fixtures.py pyproject.toml uv.lock
git commit -m "feat: epub parser with stdlib html text extraction"
```

---

### Task 4: PDF Parser (basic)

**Files:**
- Create: `lore/ingest/pdf.py`
- Modify: `tests/fixtures.py` (add `make_pdf`)
- Test: `tests/test_pdf.py`

**Interfaces:**
- Consumes: `ParsedBook`, `ParsedChapter`, `IngestError` from Task 1.
- Produces: `parse_pdf(path: Path) -> ParsedBook` — raises `IngestError` for encrypted or text-less (scanned) PDFs; chapters from font-size heading heuristic, else 15-page fallback ranges; repeated per-page header/footer lines stripped.

- [ ] **Step 1: Add the dependency**

Run: `uv add pymupdf`
Expected: resolves cleanly.

- [ ] **Step 2: Add the PDF fixture helper and failing tests**

Append to `tests/fixtures.py`:
```python
import pymupdf


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
```

`tests/test_pdf.py`:
```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_pdf.py -v`
Expected: FAIL with `ModuleNotFoundError` for `lore.ingest.pdf`.

- [ ] **Step 4: Implement `lore/ingest/pdf.py`**

```python
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
```

Implementer note: pymupdf geometry/format details (e.g. `get_text("dict")` structure) are stable, but if a fixture assertion fails, print the actual extracted structure and adjust the IMPLEMENTATION (e.g. the chapter body slicing) — the tests define the contract. Empty metadata titles come back as "" — the `or path.stem` handles that.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_pdf.py -v`
Expected: 4 passed, no warnings.

- [ ] **Step 6: Commit**

```bash
git config user.email   # HARD GATE: must print jatinroopchandani3007@gmail.com
git add lore/ingest/pdf.py tests/test_pdf.py tests/fixtures.py pyproject.toml uv.lock
git commit -m "feat: basic pdf parser with heading heuristic and edge stripping"
```

---

### Task 5: Registry + SQLite Migration v2

**Files:**
- Create: `lore/ingest/registry.py`
- Modify: `lore/db/sqlite.py` (append migration 2 to `MIGRATIONS` — do not touch migration 1)
- Test: `tests/test_registry.py`

**Interfaces:**
- Consumes: `Chunk` from Task 1; `open_db`/`schema_version` from `lore.db.sqlite` (migration runner applies new versions automatically and atomically).
- Produces:
  - Migration 2: table `chunks (id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id), chapter_seq INTEGER NOT NULL, scene_seq INTEGER NOT NULL, seq INTEGER NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')))` plus index `idx_chunks_book ON chunks(book_id)`.
  - `ingest_book(conn, *, book_id: str, title: str, author: str | None, source: str, file_path: str, chunks: list[Chunk]) -> bool` — returns False (and writes nothing) if the book already has status `'chunked'`; otherwise inserts the book row (status `'chunked'`) and all chunks in ONE transaction and returns True.
  - `list_books(conn) -> list[dict]` — dicts with keys `id, title, author, status, chunk_count`.

- [ ] **Step 1: Write the failing tests**

`tests/test_registry.py`:
```python
from pathlib import Path

from lore.db.sqlite import open_db, schema_version
from lore.ingest.models import Chunk
from lore.ingest.registry import ingest_book, list_books

BOOK_ID = "abcdef0123456789"


def _chunks(n: int) -> list[Chunk]:
    return [
        Chunk(
            id=f"{BOOK_ID}:1:{i}",
            book_id=BOOK_ID,
            chapter_seq=1,
            scene_seq=0,
            seq=i,
            text=f"chunk text {i}",
        )
        for i in range(n)
    ]


def test_migration_v2_applies(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    assert schema_version(conn) == 2
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "chunks" in tables
    conn.close()


def test_ingest_book_inserts_and_is_idempotent(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    created = ingest_book(
        conn, book_id=BOOK_ID, title="T", author="A",
        source="upload", file_path="/x/b.epub", chunks=_chunks(3),
    )
    assert created is True
    again = ingest_book(
        conn, book_id=BOOK_ID, title="T", author="A",
        source="upload", file_path="/x/b.epub", chunks=_chunks(3),
    )
    assert again is False
    count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert count == 3
    conn.close()


def test_ingest_book_is_transactional(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    bad = _chunks(3)
    bad[2] = Chunk(id=bad[0].id, book_id=BOOK_ID, chapter_seq=1,
                   scene_seq=0, seq=2, text="dup id")  # PK collision
    try:
        ingest_book(
            conn, book_id=BOOK_ID, title="T", author=None,
            source="upload", file_path="/x/b.epub", chunks=bad,
        )
        raise AssertionError("expected IntegrityError")
    except Exception:
        pass
    # nothing committed: neither the book nor any chunk
    assert conn.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    conn.close()


def test_list_books_reports_counts(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    ingest_book(
        conn, book_id=BOOK_ID, title="T", author="A",
        source="upload", file_path="/x/b.epub", chunks=_chunks(5),
    )
    books = list_books(conn)
    assert len(books) == 1
    assert books[0]["id"] == BOOK_ID
    assert books[0]["title"] == "T"
    assert books[0]["status"] == "chunked"
    assert books[0]["chunk_count"] == 5
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_registry.py -v`
Expected: `test_migration_v2_applies` fails on `schema_version == 2`; others fail on `ModuleNotFoundError` for `lore.ingest.registry`.

- [ ] **Step 3: Implement**

Append to `MIGRATIONS` in `lore/db/sqlite.py` (after the version-1 tuple, same `list[tuple[int, list[str]]]` shape):
```python
    (
        2,
        [
            """
            CREATE TABLE chunks (
                id          TEXT PRIMARY KEY,
                book_id     TEXT NOT NULL REFERENCES books(id),
                chapter_seq INTEGER NOT NULL,
                scene_seq   INTEGER NOT NULL,
                seq         INTEGER NOT NULL,
                text        TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """,
            "CREATE INDEX idx_chunks_book ON chunks(book_id)",
        ],
    ),
```

`lore/ingest/registry.py`:
```python
"""Book + chunk registration: one transaction, idempotent by book_id."""
from __future__ import annotations

import sqlite3

from lore.ingest.models import Chunk


def ingest_book(
    conn: sqlite3.Connection,
    *,
    book_id: str,
    title: str,
    author: str | None,
    source: str,
    file_path: str,
    chunks: list[Chunk],
) -> bool:
    row = conn.execute(
        "SELECT status FROM books WHERE id = ?", (book_id,)
    ).fetchone()
    if row is not None and row[0] == "chunked":
        return False
    try:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT OR REPLACE INTO books (id, title, author, source, file_path, status)"
            " VALUES (?, ?, ?, ?, ?, 'chunked')",
            (book_id, title, author, source, file_path),
        )
        conn.executemany(
            "INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (c.id, c.book_id, c.chapter_seq, c.scene_seq, c.seq, c.text)
                for c in chunks
            ],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


def list_books(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT b.id, b.title, b.author, b.status, COUNT(c.id)"
        " FROM books b LEFT JOIN chunks c ON c.book_id = b.id"
        " GROUP BY b.id ORDER BY b.created_at"
    ).fetchall()
    return [
        {"id": r[0], "title": r[1], "author": r[2], "status": r[3], "chunk_count": r[4]}
        for r in rows
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_registry.py tests/test_sqlite.py -v`
Expected: all pass — including the existing Phase 0 sqlite tests, EXCEPT `tests/test_sqlite.py` asserts `schema_version(conn) == 1` in two places; update those two assertions to `== 2` (the migration count grew — that is the correct new expectation, not a weakening). The atomicity test appends its broken migration after the real ones and still passes unchanged.

- [ ] **Step 5: Commit**

```bash
git config user.email   # HARD GATE: must print jatinroopchandani3007@gmail.com
git add lore/ingest/registry.py lore/db/sqlite.py tests/test_registry.py tests/test_sqlite.py
git commit -m "feat: chunks table migration and transactional book registry"
```

---

### Task 6: CLI — `lore ingest` and `lore books`

**Files:**
- Modify: `lore/cli.py` (add two commands after `doctor`; do not change `doctor`)
- Test: `tests/test_cli_ingest.py`

**Interfaces:**
- Consumes: everything — `book_id_for_file`, `IngestError` (Task 1), `chunk_book` (Task 2), `parse_epub` (Task 3), `parse_pdf` (Task 4), `ingest_book`/`list_books` (Task 5), `LoreConfig` + `open_db` (Phase 0).
- Produces: `lore ingest <path>` — parses by extension (`.epub`/`.pdf`, anything else → error exit 2), chunks, registers; prints `book_id`, chapter/chunk counts; re-run prints `already ingested — no-op` and exits 0. `lore books` — one line per book: id, title, chunk_count, status.

- [ ] **Step 1: Write the failing tests**

`tests/test_cli_ingest.py`:
```python
from pathlib import Path

from typer.testing import CliRunner

from lore.cli import app
from tests.fixtures import make_epub

runner = CliRunner()


def _long_html(tag: str) -> str:
    body = " ".join(f"{tag} sentence number {i} with plenty of words." for i in range(40))
    return f"<p>{body}</p>"


def _fixture(tmp_path: Path) -> Path:
    return make_epub(
        tmp_path / "book.epub",
        title="CLI Test Book",
        author="A",
        chapters=[("One", _long_html("one")), ("Two", _long_html("two"))],
    )


def test_ingest_then_noop(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    path = _fixture(tmp_path)

    first = runner.invoke(app, ["ingest", str(path)])
    assert first.exit_code == 0, first.output
    assert "CLI Test Book" in first.output
    assert "2 chapters" in first.output

    second = runner.invoke(app, ["ingest", str(path)])
    assert second.exit_code == 0
    assert "no-op" in second.output


def test_books_lists_library(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["ingest", str(_fixture(tmp_path))])

    result = runner.invoke(app, ["books"])
    assert result.exit_code == 0
    assert "CLI Test Book" in result.output
    assert "chunked" in result.output


def test_unsupported_extension_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    bad = tmp_path / "book.txt"
    bad.write_text("plain text")
    result = runner.invoke(app, ["ingest", str(bad)])
    assert result.exit_code != 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli_ingest.py -v`
Expected: FAIL — `ingest` is not a registered command (usage error output).

- [ ] **Step 3: Implement — add to `lore/cli.py`**

Add imports at the top:
```python
from lore.db.sqlite import open_db
from lore.ingest.chunker import chunk_book
from lore.ingest.epub import parse_epub
from lore.ingest.models import IngestError, book_id_for_file
from lore.ingest.pdf import parse_pdf
from lore.ingest.registry import ingest_book, list_books
```
(`open_db` and `schema_version` are already imported for doctor — merge, don't duplicate.)

Add after the `doctor` command:
```python
@app.command()
def ingest(path: Path) -> None:
    """Parse, chunk, and register an EPUB or PDF."""
    config = LoreConfig.load()
    config.ensure_dirs()
    parsers = {".epub": parse_epub, ".pdf": parse_pdf}
    parser = parsers.get(path.suffix.lower())
    if parser is None:
        typer.echo(f"unsupported file type: {path.suffix} (need .epub or .pdf)")
        raise typer.Exit(2)
    try:
        book_id = book_id_for_file(path)
        parsed = parser(path)
        chunks = chunk_book(parsed, book_id)
    except IngestError as exc:
        typer.echo(f"ingest failed: {exc}")
        raise typer.Exit(1)
    conn = open_db(config.sqlite_path)
    try:
        created = ingest_book(
            conn, book_id=book_id, title=parsed.title, author=parsed.author,
            source="upload", file_path=str(path), chunks=chunks,
        )
    finally:
        conn.close()
    if not created:
        typer.echo(f"{parsed.title} ({book_id}) already ingested — no-op")
        return
    typer.echo(
        f"{parsed.title} ({book_id}): "
        f"{len(parsed.chapters)} chapters, {len(chunks)} chunks"
    )


@app.command()
def books() -> None:
    """List the ingested library."""
    config = LoreConfig.load()
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    try:
        rows = list_books(conn)
    finally:
        conn.close()
    if not rows:
        typer.echo("library is empty")
        return
    for row in rows:
        typer.echo(
            f"{row['id']}  {row['title']}  [{row['status']}]  "
            f"{row['chunk_count']} chunks"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_ingest.py -v`, then the full suite `uv run pytest -m 'not live' -v`
Expected: all green, no warnings, no regressions.

- [ ] **Step 5: Commit**

```bash
git config user.email   # HARD GATE: must print jatinroopchandani3007@gmail.com
git add lore/cli.py tests/test_cli_ingest.py
git commit -m "feat: lore ingest and lore books commands"
```

---

### Task 7: Real-Novel Acceptance + README

**Files:**
- Modify: `README.md` (Quickstart gains ingest usage)
- No new code — this is the SPEC "done when" verification on a real novel.

**Interfaces:**
- Consumes: the finished CLI from Task 6.
- Produces: evidence (in the task report) that a real public-domain novel ingests with sane chapter/scene boundaries, and that re-ingesting is a no-op.

- [ ] **Step 1: Download a real public-domain EPUB**

```bash
mkdir -p data/inbox
curl -fsSL -o data/inbox/pride-and-prejudice.epub \
  "https://www.gutenberg.org/ebooks/1342.epub.noimages"
ls -la data/inbox/
```
(If that URL 404s, try `https://www.gutenberg.org/ebooks/1342.epub3.images` — Gutenberg occasionally reshuffles; any of their EPUB variants of book 1342 works. `data/` is gitignored — the file must never be committed.)

- [ ] **Step 2: Ingest it and inspect the output**

```bash
uv run lore ingest data/inbox/pride-and-prejudice.epub
uv run lore books
```
Expected: exit 0; on the order of 50–65 chapters (Pride and Prejudice has 61; front/back matter may add or drop a few) and roughly 300–900 chunks. Record the exact numbers in the report.

- [ ] **Step 3: Verify idempotency on the real book**

```bash
uv run lore ingest data/inbox/pride-and-prejudice.epub
```
Expected: `... already ingested — no-op`, exit 0.

- [ ] **Step 4: Spot-check boundaries**

```bash
uv run python - <<'EOF'
import sqlite3
conn = sqlite3.connect("data/lore.db")
rows = conn.execute(
    "SELECT chapter_seq, COUNT(*), MIN(LENGTH(text)), MAX(LENGTH(text))"
    " FROM chunks GROUP BY chapter_seq ORDER BY chapter_seq LIMIT 10"
).fetchall()
for r in rows:
    print(f"chapter {r[0]}: {r[1]} chunks, len {r[2]}..{r[3]}")
print(conn.execute("SELECT text FROM chunks WHERE seq=0 AND chapter_seq=3").fetchone()[0][:200])
EOF
```
Expected: max chunk length < ~1800; chapter 3's first chunk starts with narrative text from that chapter (not the end of chapter 2). Paste the output into the report. If a boundary looks wrong (e.g. a chunk containing two chapters' text), STOP and report DONE_WITH_CONCERNS with the evidence — do not paper over it.

- [ ] **Step 5: Update README quickstart**

In `README.md`, replace the Quickstart section with:
```markdown
## Quickstart

    uv sync
    uv run lore doctor            # checks config, stores, ollama, kaggle auth
    uv run lore doctor --kaggle   # + full free-tier GPU round trip (slow)
    uv run lore ingest book.epub  # parse + chunk + register a book (.epub/.pdf)
    uv run lore books             # list the ingested library
```

- [ ] **Step 6: Full suite, then commit**

```bash
uv run pytest -m 'not live' -v
git config user.email   # HARD GATE: must print jatinroopchandani3007@gmail.com
git status --short      # data/ must NOT appear
git add README.md
git commit -m "docs: quickstart covers ingest and books commands"
```

---

## Phase 1 Exit Criteria (mirror of SPEC §9 Phase 1 "Done when")

1. `uv run pytest -m 'not live'` — entire suite green, no warnings.
2. `uv run lore ingest <real novel>.epub` produces structure-respecting chunks; running it twice is a no-op.
3. Chunk IDs are stable: re-chunking the same file yields byte-identical chunk rows.
4. `lore books` lists the library with counts and status.
5. All commits authored by `jatinroopchandani3007@gmail.com`, pushed to `origin/main`.
