"""Deterministic structure-aware chunker.

Chapters are hard boundaries; scenes (typographic markers or blank-line
runs) are hard boundaries too; within a scene, sentences are greedily
packed to a character budget with a fixed sentence overlap. Same input,
same output — chunk IDs must be stable across runs forever.

Scene-break contract with parsers: a parser emits a literal "\\n***\\n" for
an explicit, source-marked scene break (e.g. an EPUB <hr/>), and lets any
run of blank lines it produces escalate naturally to "\\n{3,}" wherever
paragraphs were dropped or merged. The chunker is the single consumer of
that contract: it splits scenes on `_SCENE_MARKER` (***, ---, ### lines)
and `_BLANK_RUN` (3+ consecutive newlines), and neither side needs to know
anything about the other's internals beyond this shared marker vocabulary.
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


# A closing-quote-then-space boundary: a straight or curly closing quote
# immediately followed by whitespace — the natural place to cut a run of
# quoted dialogue turns that pysbd failed to split into separate sentences.
_QUOTE_SPACE_BOUNDARY = re.compile(r'["”’]\s')


def _split_oversized(sentence: str, budget_chars: int) -> list[str]:
    """Hard-split a single pseudo-sentence longer than budget_chars.

    pysbd can return an entire run of quoted dialogue turns as one
    "sentence" (observed up to 6267 chars on real prose) because it
    doesn't split between a closing quote and the next quoted turn. Left
    alone, that single oversized sentence would sail straight through
    _pack's budget untouched. This deterministically cuts it into bounded
    fragments: repeatedly cut at the LAST closing-quote-then-space
    boundary at or under budget_chars, falling back to the last plain
    whitespace at or under budget_chars when no such quote boundary
    exists. A fragment with no whitespace under the budget at all (an
    unbroken run of non-space characters, e.g. a URL) is left whole rather
    than butchered mid-word. Fragments are rejoined with a single space by
    the normal join/packing path, so no characters are lost — only the
    whitespace character at each cut point is consumed as the separator,
    exactly as " ".join() would consume it anyway.
    """
    fragments: list[str] = []
    remaining = sentence
    while len(remaining) > budget_chars:
        window = remaining[: budget_chars + 1]
        cut = None
        for match in _QUOTE_SPACE_BOUNDARY.finditer(window):
            cut = match.start() + 1  # keep the quote char, drop the space
        if cut is None:
            space = window.rfind(" ")
            if space > 0:
                cut = space
        if cut is None:
            break  # no whitespace under budget; fragment stays whole
        fragments.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    fragments.append(remaining)
    return [f for f in fragments if f]


def _expand_oversized(sentences: list[str], budget_chars: int) -> list[str]:
    """Expand any sentence longer than budget_chars into bounded fragments.

    Each fragment then flows into _pack as an ordinary sentence, so the
    same greedy packing and overlap semantics apply to fragments as to
    real sentences.
    """
    expanded: list[str] = []
    for sentence in sentences:
        if len(sentence) > budget_chars:
            expanded.extend(_split_oversized(sentence, budget_chars))
        else:
            expanded.append(sentence)
    return expanded


def _pack(
    sentences: list[str], budget_chars: int, overlap_sentences: int
) -> list[list[str]]:
    """Greedily pack sentences into chunks with configurable overlap.

    Tracks overlap explicitly to avoid losing content when sentence sizes vary.

    A sentence bigger than half the budget can't safely take part in
    overlap accumulation: carrying it forward and adding just one more
    sentence anywhere near its own size would already overshoot the
    budget before the normal pre-append check ever gets a chance to flush
    (that check only fires on the *next* sentence). In practice this only
    matters for the bounded fragments _split_oversized() produces from a
    too-long pysbd "sentence" — several of comparable size can arrive back
    to back. Such a sentence is isolated into its own standalone pack
    instead, carrying no overlap in or out, so it can never compound with
    a neighbor past the budget.
    """
    packs: list[list[str]] = []
    current: list[str] = []
    carried = 0  # sentences in `current` that came from the previous pack
    size = 0
    for sentence in sentences:
        if len(sentence) > budget_chars // 2:
            if current and len(current) > carried:
                packs.append(current)
            packs.append([sentence])
            current, carried, size = [], 0, 0
            continue
        if current and size + len(sentence) + 1 > budget_chars:
            packs.append(current)
            current = current[-overlap_sentences:] if overlap_sentences else []
            carried = len(current)
            size = sum(len(s) + 1 for s in current)
        current.append(sentence)
        size += len(sentence) + 1
    # Only append if pack holds at least one new sentence (not pure overlap).
    # Every sentence reaching this function is already <= budget_chars —
    # chunk_book runs them through _expand_oversized() first, which hard-
    # splits any pysbd "sentence" longer than the budget into bounded
    # fragments — so a lone oversized sentence can no longer sail through
    # as an unbounded chunk; it arrives here pre-cut.
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
            sentences = _expand_oversized(_sentences(scene), budget_chars)
            for pack in _pack(sentences, budget_chars, overlap_sentences):
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
