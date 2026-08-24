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
