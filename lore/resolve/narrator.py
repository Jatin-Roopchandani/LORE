"""First-person narration, resolved to the book's narrator at load time.

Task 10c. The extraction model faithfully copies a first-person narrative's
"I", "me", "we" and "the narrator" into triplet endpoints, and
`is_probable_entity` then (correctly) refuses to mint an entity called "I".
The loader drops every edge with such an endpoint, so everything a narrator
does is lost. Measured on the two Holmes novels before this module: 327 of
2,219 extracted triplets (14.7%) had a first-person or narrator endpoint,
and the capitalised "Narrator", which does pass the entity check, became a
bogus 64-edge node spanning several speakers.

The fix has two halves, both general and per book:

  * ask once, per book, who narrates it (`render_prompt`, answered by
    `lore.graph.adjudicate.make_gemini_narrator` and cached on disk by
    `NarratorCache`), and accept the answer only if the name is actually
    written in the text the model was shown (`validated_narrator`);
  * before any entity filtering, rewrite each whole endpoint that is a
    narrator reference to that name (`rewrite_narrator`). The ordinary
    resolution rules then decide which entity the name merges into. No
    character is special-cased anywhere.

A book with no named first-person narrator gets no rewriting at all, so
its load is exactly what it was before.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from pydantic import BaseModel

from lore.ingest.models import Chunk
from lore.resolve.disambiguation import _atomic_write
from lore.resolve.entityhood import is_gutenberg_boilerplate, is_probable_entity

# Whole-endpoint matches only, after trimming and case-folding. "we", "us"
# and "our" are plural, but a plural first-person subject or object always
# INCLUDES the narrator ("we took rooms in Baker Street" is a thing the
# narrator did), so hanging that relationship on the narrator is right
# rather than a guess. The possessive "my"/"our" appears as a whole endpoint
# only when the extractor cut a phrase short, and then it still means the
# narrator.
_REFERENCES = frozenset({
    "i", "me", "my", "myself",
    "we", "us", "our", "ourselves",
    "narrator", "the narrator",
})

# About 3,000 tokens of opening story text. Measured on the two Holmes
# novels: the first person is unmistakable within the first chunk, and the
# narrator's own name is first written 629 characters in (A Study in
# Scarlet, the title-page "Reminiscences of John H. Watson, M.D.") and 2,053
# characters in (The Sign of the Four, "Perhaps you are right, Watson").
# 12,000 characters is roughly the first chapter: several times that
# margin, for a book whose narrator is addressed by name a little later,
# while still one small request on the Lite tier. A narrator who is not
# named within it reads as unknown, which costs recall but never
# rewrites an edge onto the wrong character.
OPENING_BUDGET_CHARS = 12_000

DetectNarrator = Callable[[str, list[Chunk]], Awaitable[str | None]]


def is_narrator_reference(name: str) -> bool:
    """True when the WHOLE name is a first-person pronoun or "the narrator".

    Never a substring test: "Irene Adler", "I. Watson" and "the narrator's
    friend" are names or descriptions of someone, not the narrator.
    """
    return " ".join(str(name).split()).casefold() in _REFERENCES


class NarratorAnswer(BaseModel):
    """The model's structured answer, before validation."""

    first_person: bool
    narrator_name: str = ""


def opening_passages(
    chunks: Sequence[Chunk], budget: int = OPENING_BUDGET_CHARS
) -> list[Chunk]:
    """The book's opening story chunks, whole, within `budget` characters.

    Gutenberg header and licence chunks are skipped (the same test the
    loader uses to drop their triplets). Chunks are taken in reading order
    and never cut, so the model sees real sentences; the first story chunk
    is always offered, even when it alone exceeds the budget.
    """
    story = sorted(
        (chunk for chunk in chunks if not is_gutenberg_boilerplate(chunk.text)),
        key=lambda chunk: (chunk.chapter_seq, chunk.scene_seq, chunk.seq, chunk.id),
    )
    chosen: list[Chunk] = []
    used = 0
    for chunk in story:
        if chosen and used + len(chunk.text) > budget:
            break
        chosen.append(chunk)
        used += len(chunk.text)
    return chosen


_TEMPLATE = """Below are the opening passages of a novel, in reading order.

Is the story narrated in the first person by a character in it (the narrative
voice itself says "I", not just a character speaking in dialogue)? If so, who
is that narrator?

- first_person: true only for a first-person narrator who is a character.
- narrator_name: the narrator's name copied exactly as it is written in the
  passages below. Use "" if the story is not narrated in the first person, or
  if the narrator's name is never written in these passages. Do not guess a
  name from outside knowledge.

Passages:
{passages}"""


def render_prompt(passages: Sequence[Chunk]) -> str:
    return _TEMPLATE.format(
        passages="\n\n".join(
            f"[{index}] {chunk.text}" for index, chunk in enumerate(passages, 1)
        )
    )


def _squash(text: str) -> str:
    return " ".join(text.split())


def validated_narrator(answer: NarratorAnswer, supplied_text: str) -> str | None:
    """The narrator's name, or None when the answer cannot be trusted.

    A name must be written in the text the model was shown: an answer drawn
    from outside knowledge, or a paraphrase ("Dr. John Watson" where the
    text says "John H. Watson"), is treated as unknown rather than accepted.
    A pronoun or "the narrator" is not a name, and neither is anything
    `is_probable_entity` would refuse as an entity.
    """
    if not answer.first_person:
        return None
    name = _squash(answer.narrator_name)
    if not name or is_narrator_reference(name) or not is_probable_entity(name):
        return None
    if name not in _squash(supplied_text):
        return None
    return name


def rewrite_narrator(rows: Sequence[dict], narrator: str) -> tuple[list[dict], int]:
    """Rows with every narrator-reference endpoint renamed to `narrator`.

    Returns the new rows and how many subject/object endpoints changed. The
    input rows are not mutated; `predicate`, `predicate_raw` and `evidence`
    are never touched, only endpoint names and their mentions.

    Mentions (`entity_names`) naming the narrator are rewritten too, as a
    Character, and de-duplicated. When a rewritten endpoint was not listed
    among the row's mentions at all, the narrator is added: the loader hangs
    an edge only on a surface form some mention introduced, and the name
    the narrator is rewritten to need not be written anywhere else in the
    book's extraction under exactly that spelling.
    """
    out: list[dict] = []
    endpoints = 0
    for row in rows:
        subject = str(row.get("subject") or "")
        obj = str(row.get("object") or "")
        rewritten_ends = [
            end for end, value in (("subject", subject), ("object", obj))
            if is_narrator_reference(value)
        ]
        names = [str(name) for name in (row.get("entity_names") or [])]
        kinds = [str(kind) for kind in (row.get("entity_kinds") or [])]
        touches_mentions = any(is_narrator_reference(name) for name in names)
        if not rewritten_ends and not touches_mentions:
            out.append(row)
            continue

        new = dict(row)
        for end in rewritten_ends:
            new[end] = narrator
        endpoints += len(rewritten_ends)

        new_names: list[str] = []
        new_kinds: list[str] = []
        for index, name in enumerate(names):
            kind = kinds[index] if index < len(kinds) else "Concept"
            if is_narrator_reference(name):
                name, kind = narrator, "Character"
            if name in new_names:
                continue
            new_names.append(name)
            new_kinds.append(kind)
        if rewritten_ends and narrator not in new_names:
            new_names.append(narrator)
            new_kinds.append("Character")
        new["entity_names"] = new_names
        new["entity_kinds"] = new_kinds
        out.append(new)
    return out, endpoints


class NarratorCache:
    """Narrator answers already paid for: `<dir>/answers.json`.

    The same store pattern as `lore.graph.adjudicate.AdjudicationCache`:

      * Key: `<book_id>:<sha256 of provider and rendered prompt>`. The prompt
        carries every passage the model was shown, so a re-ingest that
        changes the opening text is a different key, and a stale answer can
        never be applied to changed text. The book id prefix keeps two books
        with identical openings apart, and makes the file readable.
      * Value: the RAW answer, `{"first_person", "narrator_name", "model"}`.
        Validation runs on every read, so a cached answer and a fresh one
        are judged identically and a load behaves the same run after run.
      * Atomic writes that merge with what is on disk. A missing, corrupt
        or wrong-typed file or entry reads as "not cached".
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    @property
    def path(self) -> Path:
        return self.directory / "answers.json"

    @staticmethod
    def key(book_id: str, prompt: str, provider: str = "gemini") -> str:
        digest = hashlib.sha256(f"{provider}\x1f{prompt}".encode("utf-8")).hexdigest()
        return f"{book_id}:{digest}"

    def _read(self) -> dict[str, dict]:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            str(key): value for key, value in raw.items()
            if isinstance(value, dict)
            and isinstance(value.get("first_person"), bool)
            and isinstance(value.get("narrator_name"), str)
        }

    def get(self, key: str) -> NarratorAnswer | None:
        entry = self._read().get(key)
        if entry is None:
            return None
        return NarratorAnswer(first_person=entry["first_person"],
                              narrator_name=entry["narrator_name"])

    def put(self, key: str, answer: NarratorAnswer, model: str | None) -> None:
        merged = self._read()
        merged[key] = {"first_person": answer.first_person,
                       "narrator_name": answer.narrator_name,
                       "model": model or ""}
        _atomic_write(self.path, merged)


def make_narrator_detector(
    ask: Callable[[str], Awaitable[tuple[NarratorAnswer, str | None]]],
    cache: NarratorCache,
    provider: str = "gemini",
) -> DetectNarrator:
    """A `DetectNarrator` that asks `ask` at most once per book and text.

    `ask(prompt)` returns the answer and the id of the model that gave it.
    Any error it raises propagates: an unanswered question must fail the
    load, not silently load the book without its narrator.
    """

    async def detect(book_id: str, chunks: list[Chunk]) -> str | None:
        passages = opening_passages(chunks)
        if not passages:
            return None
        prompt = render_prompt(passages)
        key = NarratorCache.key(book_id, prompt, provider)
        answer = cache.get(key)
        if answer is None:
            answer, model = await ask(prompt)
            cache.put(key, answer, model)
        return validated_narrator(answer, "\n\n".join(p.text for p in passages))

    return detect
