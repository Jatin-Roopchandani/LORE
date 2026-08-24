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
    """Split text into scenes by markers (***,---,###) and blank-line runs."""
    scenes: list[str] = []
    for part in _SCENE_MARKER.split("\n" + text + "\n"):
        scenes.extend(_BLANK_RUN.split(part))
    return [s.strip() for s in scenes if s.strip()]


def _sentences(scene_text: str) -> list[str]:
    """Segment scene text into sentences using pysbd."""
    flat = re.sub(r"\s+", " ", scene_text).strip()
    return [s.strip() for s in _SEGMENTER.segment(flat) if s.strip()]


def _pack(
    sentences: list[str], budget_chars: int, overlap_sentences: int
) -> list[list[str]]:
    """Greedily pack sentences into chunks with configurable overlap.

    Tracks overlap explicitly to avoid losing content when sentence sizes vary.
    """
    packs: list[list[str]] = []
    current: list[str] = []
    carried = 0  # sentences in `current` that came from the previous pack
    size = 0
    for sentence in sentences:
        if current and size + len(sentence) + 1 > budget_chars:
            packs.append(current)
            current = current[-overlap_sentences:] if overlap_sentences else []
            carried = len(current)
            size = sum(len(s) + 1 for s in current)
        current.append(sentence)
        size += len(sentence) + 1
    # Only append if pack holds at least one new sentence (not pure overlap).
    # A single sentence longer than budget intentionally becomes its own chunk.
    if current and len(current) > carried:
        packs.append(current)
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
