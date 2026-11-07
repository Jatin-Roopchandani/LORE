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
    # Story position from the file's metadata, when it has any (EPUB only).
    series: str | None = None
    series_index: float | None = None


def chunk_seq(chunk_id: str) -> int:
    """A chunk's position in its book, from its id `<book>:<chapter>:<seq>`
    (0 for an id of another shape)."""
    try:
        return int(str(chunk_id).rsplit(":", 1)[-1])
    except ValueError:
        return 0


def chunk_story_key(chunk_id: str) -> tuple[int, int, str]:
    """Sort key putting chunks of one book in story order: the numeric seq,
    never the id as text (as text ":1000" sorts before ":2"). In Cypher and
    SQL the same order within a (book, chapter) is `size(id), id`: ids there
    share the `<book>:<chapter>:` prefix, so the shorter seq is the smaller."""
    return (chunk_seq(chunk_id), len(str(chunk_id)), str(chunk_id))


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
