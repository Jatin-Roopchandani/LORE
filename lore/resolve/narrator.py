"""First-person endpoints, resolved to the person speaking in each passage.

Task 10c. The extraction model faithfully copies "I", "me", "we" and "the
narrator" into triplet endpoints, and `is_probable_entity` then (correctly)
refuses to mint an entity called "I". The loader drops every edge with such
an endpoint. Measured on the two Holmes novels: 327 of 2,219 extracted
triplets (14.7%) had one, and the capitalised "Narrator", which does pass the
entity check, became a bogus 64-edge node spanning several speakers.

A per-book narrator is NOT the answer, and was measured not to be. Rewriting
every "I" in *A Study in Scarlet* to Watson hung Jefferson Hope's confession
on Watson ("Watson KILLS Drebber"), Holmes's explanation of his method on
Watson, and in *The Sign of the Four* Jonathan Small's story ("Watson
LOCATED_IN Ganges"): at least 80 of 217 recovered edges belonged to someone
else. A wrong edge is a false fact; a dropped one only costs recall.

So the book's narrator is only a HINT (`render_prompt`, one cached question
per book), and the decision is taken per passage:

  * every chunk with a first-person endpoint gets one question
    (`render_speaker_prompt`): for each of its first-person evidence
    sentences, who is speaking? The model sees the chapter's opening chunk,
    the previous chunk and the chunk itself, which is what makes a
    confession or a spoken explanation visible, plus the hint;
  * an answer is used only when it is "certain", correctly aligned with the
    sentences asked, and names someone actually written in the supplied text
    (`validated_speakers`). Anything else leaves the endpoint as extracted,
    so it drops exactly as it did before;
  * `rewrite_first_person` then renames the endpoint, and ordinary resolution
    decides which entity that name merges into. No character is
    special-cased anywhere.

Both questions are cached on disk, so a reload spends zero requests.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ValidationError

from lore.ingest.models import Chunk
from lore.resolve.disambiguation import _atomic_write
from lore.resolve.entityhood import (
    is_generic_name,
    is_gutenberg_boilerplate,
    is_probable_entity,
)

# Whole-endpoint matches only, after trimming and case-folding. "we", "us"
# and "our" are plural, but a plural first-person subject or object always
# INCLUDES the speaker ("we took rooms in Baker Street" is a thing the speaker
# did), so hanging that relationship on the speaker is right rather than a
# guess. The possessive "my"/"our" appears as a whole endpoint only when the
# extractor cut a phrase short, and then it still means the speaker.
_REFERENCES = frozenset({
    "i", "me", "my", "myself",
    "we", "us", "our", "ourselves",
    "narrator", "the narrator",
})

# About 3,000 tokens of opening story text for the book-level hint. Measured
# on the two Holmes novels: the narrator's own name is first written 629
# characters in (A Study in Scarlet) and 2,053 characters in (The Sign of the
# Four). 12,000 characters is roughly the first chapter. A narrator not named
# within it reads as unknown: the per-passage questions still run, without
# a hint.
OPENING_BUDGET_CHARS = 12_000

_UNKNOWN = "unknown"


def is_narrator_reference(name: str) -> bool:
    """True when the WHOLE name is a first-person pronoun or "the narrator".

    Never a substring test: "Irene Adler", "I. Watson" and "the narrator's
    friend" are names or descriptions of someone, not a first-person voice.
    """
    return " ".join(str(name).split()).casefold() in _REFERENCES


def _squash(text: str) -> str:
    return " ".join(str(text).split())


def _is_first_person_row(row: dict) -> bool:
    return is_narrator_reference(str(row.get("subject") or "")) or is_narrator_reference(
        str(row.get("object") or "")
    )


# --------------------------------------------------------------------------
# The book-level hint


class NarratorAnswer(BaseModel):
    first_person: bool
    narrator_name: str = ""


def opening_passages(
    chunks: Sequence[Chunk], budget: int = OPENING_BUDGET_CHARS
) -> list[Chunk]:
    """The book's opening story chunks, whole, within `budget` characters.

    Gutenberg header and licence chunks are skipped (the loader's own test).
    The first story chunk is always offered, even when it alone is too long.
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


def _acceptable_name(name: str) -> bool:
    """A real, specific name: not a pronoun, not "unknown", not a bare role.

    "the Doctor" or "Inspector" would merge into whichever generic node
    exists, so a generic name is as good as no answer.
    """
    return bool(name) and name.casefold() != _UNKNOWN and not is_narrator_reference(
        name
    ) and is_probable_entity(name) and not is_generic_name(name)


def validated_narrator(answer: NarratorAnswer, supplied_text: str) -> str | None:
    """The narrator's name, or None when it is not written in the text shown."""
    if not answer.first_person:
        return None
    name = _squash(answer.narrator_name)
    if not _acceptable_name(name) or name not in _squash(supplied_text):
        return None
    return name


# --------------------------------------------------------------------------
# Per-passage speakers


class SpeakerItem(BaseModel):
    sentence: int
    speaker_name: str
    # Free text, not Literal["certain", "unsure"]: Flash Lite was measured
    # answering "uncertain", and a strict enum rejected the whole answer and
    # aborted the book's load. Only the exact word "certain" ever rewrites
    # (see `validated_speakers`); any other word is simply not certain.
    confidence: str


class SpeakerAnswers(BaseModel):
    speakers: list[SpeakerItem]


def first_person_sentences(rows: Sequence[dict]) -> dict[str, list[str]]:
    """Chunk id -> its distinct first-person evidence sentences, sorted.

    Sorted rather than in row order, so the question (and its cache key) is a
    function of the rows' CONTENT, not of the order artifacts were read in.
    """
    found: dict[str, set[str]] = {}
    for row in rows:
        if _is_first_person_row(row):
            found.setdefault(str(row.get("chunk_id") or ""), set()).add(
                str(row.get("evidence") or "")
            )
    return {chunk_id: sorted(sentences) for chunk_id, sentences in sorted(found.items())}


def speaker_context(
    chunks: Sequence[Chunk], chunk_id: str
) -> list[tuple[str, Chunk]]:
    """(label, chunk) pairs shown with a question: chapter opening, previous
    chunk, the chunk itself, without repeats. Empty for an unknown chunk."""
    target = next((chunk for chunk in chunks if chunk.id == chunk_id), None)
    if target is None:
        return []
    chapter = sorted(
        (c for c in chunks if c.book_id == target.book_id
         and c.chapter_seq == target.chapter_seq),
        key=lambda c: (c.scene_seq, c.seq, c.id),
    )
    index = chapter.index(target)
    context: list[tuple[str, Chunk]] = []
    if index >= 2:
        context.append(("Chapter opening", chapter[0]))
    if index >= 1:
        context.append(("Previous passage", chapter[index - 1]))
    context.append(("Passage", target))
    return context


_SPEAKER_TEMPLATE = """Below are passages from a novel: {shown}.
{hint}

Each numbered sentence below comes from the passage in question and contains a
first-person reference ("I", "me", "my", "we", "us" or "the narrator"). For each
sentence, who is that first person: whose voice is it?

Read carefully. Inside quotation marks the "I" is whoever is talking, not the
narrator. A long confession, a letter or a story told by a character is in that
character's voice even across many paragraphs.

Return one entry per sentence, in the same order, each with its sentence number:
- speaker_name: that person's name copied exactly as it is written in the
  passages or in the note above, or "unknown" if no name for them is written.
- confidence: "certain" only if the passages leave no doubt who is speaking;
  otherwise "unsure".

{passages}

Sentences:
{sentences}"""


def render_speaker_prompt(
    context: Sequence[tuple[str, Chunk]], sentences: Sequence[str], hint: str | None
) -> str:
    labels = [label.lower() for label, _chunk in context]
    shown = {
        ("chapter opening", "previous passage", "passage"):
            "the opening of the chapter, the passage just before, and the passage"
            " in question",
        ("previous passage", "passage"):
            "the passage just before, and the passage in question",
    }.get(tuple(labels), "the passage in question")
    note = (
        f"Note: the book as a whole is narrated in the first person by {hint}, but"
        " quoted speech, a letter or a story told by another character is in"
        " that character's voice."
        if hint else
        "Note: no first-person narrator was identified for the book as a whole;"
        " quoted speech is in the voice of whoever is talking."
    )
    return _SPEAKER_TEMPLATE.format(
        shown=shown,
        hint=note,
        passages="\n\n".join(f"[{label}]\n{chunk.text}" for label, chunk in context),
        sentences="\n".join(
            f"{number}. {_squash(sentence)}"
            for number, sentence in enumerate(sentences, 1)
        ),
    )


def validated_speakers(
    answers: SpeakerAnswers, expected: int, supplied_text: str, hint: str | None
) -> list[str | None] | None:
    """One validated name (or None) per sentence asked, or None if misaligned.

    The mapping must be verifiable, as in the disambiguator: exactly
    `expected` entries numbered 1..expected in order. A count check alone
    would accept a rotated list, which attaches the right name to the wrong
    sentence.

    A name is accepted only when the answer is "certain", the name is a real
    specific name (`_acceptable_name`), and it is written in the supplied
    passages or is the book's validated narrator hint (itself checked
    against the book's text).
    """
    items = answers.speakers
    if [item.sentence for item in items] != list(range(1, expected + 1)):
        return None
    haystack = _squash(supplied_text) + ("\n" + hint if hint else "")
    names: list[str | None] = []
    for item in items:
        name = _squash(item.speaker_name)
        if item.confidence != "certain" or not _acceptable_name(name) or (
            name not in haystack
        ):
            names.append(None)
        else:
            names.append(name)
    return names


def rewrite_first_person(
    rows: Sequence[dict], speaker_of: Callable[[dict], str | None]
) -> tuple[list[dict], int, int]:
    """Rows with each first-person endpoint renamed to that row's speaker.

    Returns (rows, endpoints rewritten, endpoints left unresolved). A row
    whose speaker is None is returned untouched, so it drops at load exactly
    as before. Input rows are never mutated; `predicate`, `predicate_raw` and
    `evidence` are never touched.

    Mentions naming a first-person reference are rewritten too, as a
    Character, and de-duplicated. When a rewritten endpoint was not listed
    among the row's mentions, the speaker is added: the loader hangs an edge
    only on a surface form some mention introduced.
    """
    out: list[dict] = []
    rewritten = unresolved = 0
    for row in rows:
        ends = [
            end for end in ("subject", "object")
            if is_narrator_reference(str(row.get(end) or ""))
        ]
        if not ends:
            out.append(row)
            continue
        speaker = speaker_of(row)
        if not speaker:
            unresolved += len(ends)
            out.append(row)
            continue

        new = dict(row)
        for end in ends:
            new[end] = speaker
        rewritten += len(ends)

        names = [str(name) for name in (row.get("entity_names") or [])]
        kinds = [str(kind) for kind in (row.get("entity_kinds") or [])]
        new_names: list[str] = []
        new_kinds: list[str] = []
        for index, name in enumerate(names):
            kind = kinds[index] if index < len(kinds) else "Concept"
            if is_narrator_reference(name):
                name, kind = speaker, "Character"
            if name in new_names:
                continue
            new_names.append(name)
            new_kinds.append(kind)
        if speaker not in new_names:
            new_names.append(speaker)
            new_kinds.append("Character")
        new["entity_names"] = new_names
        new["entity_kinds"] = new_kinds
        out.append(new)
    return out, rewritten, unresolved


# --------------------------------------------------------------------------
# Caches


class _AnswerFile:
    """`<dir>/answers.json`: the AdjudicationCache pattern.

    Values are RAW model answers; validation runs on every read, so a cached
    answer and a fresh one are judged identically. Writes are atomic and
    merge with what is on disk. A missing, corrupt or wrong-typed file or
    entry reads as "not cached".
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    @property
    def path(self) -> Path:
        return self.directory / "answers.json"

    @staticmethod
    def _digest(provider: str, prompt: str) -> str:
        return hashlib.sha256(f"{provider}\x1f{prompt}".encode("utf-8")).hexdigest()

    def _entries(self) -> dict[str, dict]:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}

    def _put(self, key: str, value: dict) -> None:
        merged = self._entries()
        merged[key] = value
        _atomic_write(self.path, merged)


class NarratorCache(_AnswerFile):
    """Book hints. Key: `<book_id>:<sha256 of provider and rendered prompt>`."""

    @staticmethod
    def key(book_id: str, prompt: str, provider: str = "gemini") -> str:
        return f"{book_id}:{_AnswerFile._digest(provider, prompt)}"

    def get(self, key: str) -> NarratorAnswer | None:
        entry = self._entries().get(key)
        try:
            return None if entry is None else NarratorAnswer.model_validate(
                {"first_person": entry["first_person"],
                 "narrator_name": entry["narrator_name"]}, strict=True)
        except (KeyError, ValidationError):
            return None

    def put(self, key: str, answer: NarratorAnswer, model: str | None) -> None:
        self._put(key, {"first_person": answer.first_person,
                        "narrator_name": answer.narrator_name,
                        "model": model or ""})


class SpeakerCache(_AnswerFile):
    """Per-chunk speakers. Key: `<chunk_id>:sha256=<provider and prompt>`.

    The prompt carries the hint, all three passages and every sentence
    asked, so a re-extraction, a re-ingest or a changed hint is a new key.
    Only a correctly ALIGNED answer is ever stored.
    """

    @staticmethod
    def key(chunk_id: str, prompt: str, provider: str = "gemini") -> str:
        return f"{chunk_id}:sha256={_AnswerFile._digest(provider, prompt)}"

    def get(self, key: str) -> SpeakerAnswers | None:
        entry = self._entries().get(key)
        try:
            return None if entry is None else SpeakerAnswers.model_validate(
                {"speakers": entry["speakers"]})
        except (KeyError, ValidationError):
            return None

    def put(self, key: str, answers: SpeakerAnswers, model: str | None) -> None:
        self._put(key, {"speakers": [item.model_dump() for item in answers.speakers],
                        "model": model or ""})


# --------------------------------------------------------------------------
# The resolver


@dataclass
class SpeakerResolution:
    narrator_hint: str = ""
    # (chunk_id, evidence) -> validated speaker name. Absent means unresolved.
    speakers: dict[tuple[str, str], str] = field(default_factory=dict)
    requests: int = 0            # speaker questions actually sent this load
    mismatched_chunks: int = 0   # answers refused as misaligned (not cached)


ResolveFirstPerson = Callable[[str, list[Chunk], list[dict]], Awaitable[SpeakerResolution]]
AskNarrator = Callable[[str], Awaitable[tuple[NarratorAnswer, str | None]]]
AskSpeakers = Callable[[str], Awaitable[tuple[SpeakerAnswers, str | None]]]


def make_first_person_resolver(
    ask_narrator: AskNarrator,
    ask_speakers: AskSpeakers,
    narrator_cache: NarratorCache,
    speaker_cache: SpeakerCache,
    provider: str = "gemini",
) -> ResolveFirstPerson:
    """A `ResolveFirstPerson` that asks at most once per book and per chunk.

    Errors from either `ask` propagate and fail the load before anything is
    written. A misaligned speaker answer is the one exception: it leaves that
    chunk unresolved for this load (its edges drop, as before) and is not
    cached, so the next load asks again rather than trusting it.
    """

    async def hint_for(book_id: str, chunks: list[Chunk]) -> str | None:
        passages = opening_passages(chunks)
        if not passages:
            return None
        prompt = render_prompt(passages)
        key = NarratorCache.key(book_id, prompt, provider)
        answer = narrator_cache.get(key)
        if answer is None:
            answer, model = await ask_narrator(prompt)
            narrator_cache.put(key, answer, model)
        return validated_narrator(answer, "\n\n".join(p.text for p in passages))

    async def resolve(
        book_id: str, chunks: list[Chunk], rows: list[dict]
    ) -> SpeakerResolution:
        result = SpeakerResolution()
        questions = first_person_sentences(rows)
        if not questions:
            return result
        hint = await hint_for(book_id, chunks)
        result.narrator_hint = hint or ""
        order = {chunk.id: (chunk.chapter_seq, chunk.scene_seq, chunk.seq) for chunk in chunks}
        for chunk_id in sorted(questions, key=lambda cid: (cid not in order,
                                                           order.get(cid, ()), cid)):
            context = speaker_context(chunks, chunk_id)
            if not context:
                continue
            sentences = questions[chunk_id]
            prompt = render_speaker_prompt(context, sentences, hint)
            key = SpeakerCache.key(chunk_id, prompt, provider)
            answers = speaker_cache.get(key)
            fresh = answers is None
            if fresh:
                answers, model = await ask_speakers(prompt)
                result.requests += 1
                if answers is None:
                    # An unusable answer for ONE passage leaves that chunk
                    # unresolved and uncached; it must not abort the book.
                    result.mismatched_chunks += 1
                    continue
            names = validated_speakers(
                answers, len(sentences),
                "\n\n".join(chunk.text for _label, chunk in context), hint,
            )
            if names is None:
                result.mismatched_chunks += 1
                continue
            if fresh:
                speaker_cache.put(key, answers, model)
            for sentence, name in zip(sentences, names):
                if name:
                    result.speakers[(chunk_id, sentence)] = name
        return result

    return resolve
