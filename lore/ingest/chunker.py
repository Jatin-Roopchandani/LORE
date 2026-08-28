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

from lore.ingest.frontmatter import is_front_matter
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
    whitespace at or under budget_chars, or hard-cutting at budget_chars
    when no whitespace boundary exists (e.g. a long URL or unbroken token).
    Fragments are rejoined with a single space by the normal join/packing
    path, so no characters are lost — only the whitespace character at each
    cut point is consumed as the separator, exactly as " ".join() would
    consume it anyway.
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
            # No whitespace or quote boundary under budget; hard-cut at budget_chars
            # as the last resort to prevent unbounded chunks. A mid-token cut is
            # preferred over violating the budget guarantee.
            cut = budget_chars
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

    Every emitted pack is <= budget_chars. Two mechanisms keep that true
    without sacrificing overlap:

    * a sentence is only appended when it still fits, otherwise the pack
      is flushed first;
    * on flush, the carried overlap is trimmed from the front until the
      incoming sentence fits alongside it. Large sentences therefore
      carry less overlap (or none) rather than being isolated, so real
      long-dialogue sentences keep their context while bounded fragments
      from _split_oversized() can never compound past the budget.

    Sentences arrive pre-cut: chunk_book runs them through
    _expand_oversized() first, so none exceeds budget_chars on its own.
    """
    packs: list[list[str]] = []
    current: list[str] = []
    carried = 0  # sentences in `current` that came from the previous pack
    size = 0
    for sentence in sentences:
        if current and size + len(sentence) + 1 > budget_chars:
            packs.append(current)
            carry = current[-overlap_sentences:] if overlap_sentences else []
            while carry and sum(len(s) + 1 for s in carry) + len(sentence) + 1 > budget_chars:
                carry.pop(0)
            current = carry
            carried = len(current)
            size = sum(len(s) + 1 for s in current)
        current.append(sentence)
        size += len(sentence) + 1
    # Only append if the pack holds at least one new sentence (not pure overlap).
    if current and len(current) > carried:
        packs.append(current)
    return packs


def chunk_book(
    book: ParsedBook,
    book_id: str,
    budget_chars: int = 1600,
    overlap_sentences: int = 2,
    *,
    drop_front_matter: bool = True,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    for chapter in book.chapters:
        seq = 0
        for scene_seq, scene in enumerate(_split_scenes(chapter.text)):
            sentences = _sentences(scene)
            # Filter at sentence granularity, before packing: a front-matter
            # sentence sitting beside narrative in the same pack must only
            # remove itself, not the surrounding narrative it happens to
            # share a pack with. A whole front-matter scene still vanishes
            # naturally once every one of its sentences is dropped, leaving
            # nothing left to pack.
            if drop_front_matter:
                sentences = [s for s in sentences if not is_front_matter(s)]
            sentences = _expand_oversized(sentences, budget_chars)
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
