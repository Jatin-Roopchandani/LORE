"""The ambiguous residue: pending questions, one batched ask, a cache.

Entity resolution decides almost everything with deterministic rules. One
class genuinely needs judgement and no rule can settle it: a bare surname
that is a subset of two or more candidates. `normalize_name` strips
honorifics, so `Major Sholto` becomes the bare `sholto`, which nests
inside both `thaddeus sholto` and `bartholomew sholto` — a murdered father
and his two sons. Task 1i makes that refuse, which is safe but lossy: 36
refused pairs over 17 distinct bare names on the real corpus (`sholto`,
`ferrier`, `smith`, `drebber`, `morstan`, `young`, `lucy`, `agra`, …).

A question is about a BARE NAME and all of its rivals at once — never a
pair. `resolve_mentions` groups its refusals before it asks, because the
pairwise form is a different and wrong question: measured live, the model
answers Yes to `Sholto` vs `Major Sholto` AND Yes to `Sholto` vs
`Thaddeus Sholto`, each individually reasonable, together fusing the
father into his son. See `resolve_mentions.grouped_question`.

Three things shape this module, all of them the free tier's daily request
ceiling — 20/day on Flash, 500/day on Lite (see `lore.inference.gemini`):

1. **The API is not in the load path.** `lore process` only RECORDS the
   questions it could not answer, to `pending/<book_id>.json`. It stays
   local, fast and offline. `lore disambiguate` is a separate, deliberate
   command that spends the quota.

2. **One request, all questions.** 17 questions is ~2k tokens against a
   250,000 TPM ceiling, so token budget is a non-issue and request count
   is everything: a whole corpus is 1 request, not 17 — which on the
   Flash tier is the difference between usable and unusable.

3. **Every answer is cached**, keyed by a stable hash of the question.
   A reload spends zero requests. Reloading is the common operation on
   this project, so an uncached design would be unusable however well it
   batched.

The dangerous part of (2) is the mapping. A numbered batch returns a list
that must land back on the right questions, and an off-by-one there
silently attaches the wrong merge decision to the wrong entity — a graph
that is quietly wrong while every test passes. So the answer format is
redundant on purpose: each line carries the question NUMBER and an echo
of the MENTION before its verdict, and both must agree with the question
at that number. A rotated, short, long or duplicated answer set is
rejected outright and nothing is written — never zipped against the
questions and truncated.

Within a verified line, the verdict is read DEFENSIVELY. A verdict that
is not cleanly an offered option number, an offered option name, or
"none" — prose, an out-of-range number, a name nobody offered — is read
as a REFUSAL. Measured: `gemini-3.5-flash-lite` got 11 of 12 eval cases,
and its single miss was answering an unresolvable case with prose
("Based on the context of Ar...") instead of "none". Guessing at the
intended candidate there would attach the wrong merge to the wrong
entity and be unrecoverable; refusing leaves two nodes a later pass can
still join.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

# One request carries at most this many questions. Far below the token
# ceiling (17 questions is ~2k tokens against 250,000 TPM); the cap exists
# so a pathological corpus produces a readable prompt and a parseable
# answer set, not to save tokens.
MAX_BATCH = 25

_FIELD_SEPARATOR = "\x1f"
_ANSWER_LINE = re.compile(r"^\s*(?:[-*•]\s*)?\**\s*(\d+)\s*\**\s*[.):]\s*(.+)$")
_ARROW = re.compile(r"\s*(?:->|→|=>)\s*")
_SQUASH = re.compile(r"[^a-z0-9]+")
# A bare option number, with or without the decoration models add: "2",
# "2)", "(2)", "2.", "option 2", "#2".
_OPTION_NUMBER = re.compile(r"^(?:option\s*|#|\()?(\d+)\)?\.?$", re.IGNORECASE)


class AnswerMismatchError(ValueError):
    """An answer set that cannot be trusted to map onto its questions.

    Raised instead of returning a partial or best-effort mapping. The
    caller writes nothing when this is raised, so a mis-mapped batch costs
    one request and no wrong data.
    """


@dataclass(frozen=True, order=True)
class Candidate:
    """One entity a refused mention might belong to."""

    id: str
    name: str


@dataclass(frozen=True)
class Question:
    """A same-book merge the deterministic rules refused to decide."""

    mention: str
    kind: str
    book_id: str
    evidence: str
    candidates: tuple[Candidate, ...]

    def __post_init__(self) -> None:
        # Canonical candidate order, so the prompt, the answer labels and
        # the cache key are all functions of the question's CONTENT and
        # never of the order the resolver happened to collect them in.
        object.__setattr__(self, "candidates", tuple(sorted(self.candidates)))

    @property
    def key(self) -> str:
        return question_key(self)

    def as_dict(self) -> dict:
        return {
            "mention": self.mention, "kind": self.kind,
            "book_id": self.book_id, "evidence": self.evidence,
            "candidates": [{"id": c.id, "name": c.name} for c in self.candidates],
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Question":
        return cls(
            mention=str(raw["mention"]), kind=str(raw["kind"]),
            book_id=str(raw["book_id"]), evidence=str(raw["evidence"]),
            candidates=tuple(
                Candidate(id=str(c["id"]), name=str(c["name"]))
                for c in raw["candidates"]
            ),
        )


def question_key(question: Question) -> str:
    """A stable hash of mention name, kind, evidence and candidate ids.

    Sorted ids, so supplying the candidates in a different order hits the
    same cache entry. Candidate NAMES are deliberately excluded: the id is
    a hash of (normalized name, kind), so it already fixes the entity, and
    a display name can legitimately change (`display_name` picks the
    longest surface form seen) without changing the question being asked.

    `book_id` is excluded for the same reason it does not appear in
    `entity_id`: two books that produce a byte-identical question — same
    mention, same kind, same evidence sentence, same candidate entities —
    are asking the same thing and deserve the same answer for free.
    """
    parts = [question.mention, question.kind, question.evidence]
    parts.extend(sorted(c.id for c in question.candidates))
    return hashlib.sha256(
        _FIELD_SEPARATOR.join(parts).encode("utf-8")
    ).hexdigest()


# --------------------------------------------------------------------------
# The batch protocol


def _squash(text: str) -> str:
    """Case- and punctuation-insensitive identity for one short label."""
    return _SQUASH.sub("", text.casefold())


def _labels(question: Question) -> list[tuple[str, str]]:
    """(label, entity id) for each candidate, labels unique within a question.

    Two distinct nodes can carry the same display name. An answer names its
    choice by label, so a duplicate label would be unresolvable — number
    the repeats rather than let the mapping become ambiguous.
    """
    seen: dict[str, int] = {}
    out: list[tuple[str, str]] = []
    for candidate in question.candidates:
        seen[candidate.name] = seen.get(candidate.name, 0) + 1
        count = seen[candidate.name]
        label = candidate.name if count == 1 else f"{candidate.name} ({count})"
        out.append((label, candidate.id))
    return out


_PREAMBLE = """\
You are disambiguating character and place names in classic novels.

Each numbered question gives a name exactly as it appeared in the text,
the sentence it appeared in, and the entities already known in that same
book whose full names contain that name. Decide which ONE of the listed
options the name refers to in that sentence, or answer none if it refers
to someone else entirely (a father and his sons share a surname, so the
bare surname often belongs to a person who is not on the list).

Answer with one line per question and nothing else, in exactly this form:

  <question number>. <name as given> -> <option number, or none>

The verdict must be a bare option number or the word none. Do not explain.
If you are not sure, answer none.

Answer every question. Give exactly {count} line(s), numbered 1 to {count},
in order.
"""


def batch_prompt(questions: Sequence[Question]) -> str:
    """Every pending question in one request."""
    blocks = [_PREAMBLE.format(count=len(questions))]
    for index, question in enumerate(questions, start=1):
        options = "\n".join(
            f"     {position}) {label}"
            for position, (label, _cid) in enumerate(_labels(question), start=1)
        )
        blocks.append(
            f"{index}. name: {question.mention} ({question.kind})\n"
            f"   sentence: {question.evidence or '(none recorded)'}\n"
            f"   options:\n{options}\n"
            f"     none) a different person, not listed above"
        )
    return "\n\n".join(blocks)


def _verdict(question: Question, choice: str) -> str:
    """One line's verdict: an offered entity id, or "" for a refusal.

    Never a guess. An option number outside the offered range, a name
    nobody offered, or free prose all read as a refusal — the measured
    failure of `gemini-3.5-flash-lite` was exactly prose where "none"
    belonged, and inferring an intended candidate from prose would attach
    the wrong merge to the wrong entity, which nothing downstream can
    undo.
    """
    labels = _labels(question)
    cleaned = choice.strip().strip("*").strip()
    number = _OPTION_NUMBER.match(cleaned)
    if number is not None:
        position = int(number.group(1))
        return labels[position - 1][1] if 1 <= position <= len(labels) else ""
    squashed = _squash(cleaned)
    for label, cid in labels:
        if _squash(label) == squashed:
            return cid
    return ""


def parse_batch_answers(
    questions: Sequence[Question], text: str
) -> dict[str, str]:
    """Map a numbered answer set back onto its questions, or refuse.

    Returns {question key: chosen entity id}, with "" meaning "none of
    them — stay separate".

    Two different kinds of wrong get two different treatments, and the
    distinction is the whole safety argument:

      * The MAPPING must be verifiable. A missing, extra or duplicated
        question number, a line that carries no verdict at all, or a line
        whose echoed mention belongs to a different question means the
        answer set does not line up with the questions — `AnswerMismatch
        Error`, and the caller writes nothing. A count check alone would
        accept a rotated answer set, which is exactly how a numbered
        batch silently attaches the wrong decision to the wrong entity.

      * A VERDICT inside a verified line may be unreadable, and that is
        just a refusal (see `_verdict`). Under-merging leaves two nodes a
        later pass can join; a guessed merge is unrecoverable.
    """
    parsed: dict[int, tuple[str, str]] = {}
    for line in text.splitlines():
        match = _ANSWER_LINE.match(line)
        if not match:
            continue
        number = int(match.group(1))
        body = match.group(2).strip().strip("*").strip()
        pieces = _ARROW.split(body)
        if len(pieces) != 2:
            raise AnswerMismatchError(
                f"answer {number} is not in the required"
                f" `<number>. <name> -> <verdict>` form, so it cannot be"
                f" checked against question {number}: {line.strip()!r}"
            )
        if number in parsed:
            raise AnswerMismatchError(
                f"answer number {number} appears more than once; refusing to"
                " guess which one belongs to the question"
            )
        parsed[number] = (pieces[0].strip(), pieces[1].strip())

    expected = set(range(1, len(questions) + 1))
    if set(parsed) != expected:
        missing = sorted(expected - set(parsed))
        extra = sorted(set(parsed) - expected)
        raise AnswerMismatchError(
            f"expected exactly {len(questions)} answer(s) numbered 1-"
            f"{len(questions)}, got {len(parsed)}"
            + (f"; missing {missing}" if missing else "")
            + (f"; unexpected {extra}" if extra else "")
            + ". Refusing to map a partial answer set onto the questions."
        )

    answers: dict[str, str] = {}
    for number, (echoed, choice) in sorted(parsed.items()):
        question = questions[number - 1]
        if _squash(echoed) != _squash(question.mention):
            raise AnswerMismatchError(
                f"answer {number} echoes {echoed!r} but question {number} asked"
                f" about {question.mention!r}; the answer set does not line up"
                " with the questions"
            )
        answers[question.key] = _verdict(question, choice)
    return answers


async def ask(client, questions: Sequence[Question], *, max_batch: int = MAX_BATCH):
    """Ask every question, in as few requests as the batch cap allows.

    Returns {question key: entity id or ""}. Raises on any failure, having
    written nothing anywhere — the caller commits to the cache only after
    a complete, verified answer set for every batch.

    Splitting is spacing-safe: `GeminiClient` enforces the 5 RPM ceiling
    itself, so a second batch waits 12s rather than earning a 429.
    """
    answers: dict[str, str] = {}
    for start in range(0, len(questions), max_batch):
        batch = list(questions[start:start + max_batch])
        raw = await client.generate(batch_prompt(batch))
        answers.update(parse_batch_answers(batch, raw))
    return answers


# --------------------------------------------------------------------------
# The store


def _atomic_write(path: Path, payload: object) -> None:
    """Write-temp-then-rename, so a reader never sees a partial file.

    The temp name carries the pid and a random suffix. A shared
    `<name>.tmp` looked fine and was not: two processes writing at once
    each created the SAME temp path, and whichever renamed second got
    FileNotFoundError because the first had already moved it away —
    measured, not theorised.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex[:8]}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _safe_name(book_id: str) -> str:
    """A filesystem-safe stem for a book id (a 16-char content hash)."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", book_id) or "unknown"


class DisambiguationStore:
    """Pending questions on disk, and the answers already paid for.

    Both halves must survive a corrupt file: a load that crashed because
    the answer cache was half-written would make the optional feature able
    to break the mandatory one. Every read is best-effort and every write
    is atomic (write-temp, `os.replace`), so a truncated file reads as
    "nothing cached" and is repaired by the next successful save.

    The API key is never written here, and neither is any prompt or raw
    model output — only question keys and chosen entity ids.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self._recorded: dict[str, dict[str, Question]] = {}
        self._answers: dict[str, str] | None = None

    # -- paths ------------------------------------------------------------
    @property
    def answers_path(self) -> Path:
        return self.directory / "answers.json"

    @property
    def pending_dir(self) -> Path:
        return self.directory / "pending"

    def pending_path(self, book_id: str) -> Path:
        return self.pending_dir / f"{_safe_name(book_id)}.json"

    # -- recording (called from the load path) ----------------------------
    def record(self, question: Question) -> None:
        """Note a question the resolver refused. Writes nothing yet."""
        self._recorded.setdefault(question.book_id, {})[question.key] = question

    def flush(self, book_id: str) -> None:
        """Persist this book's questions, REPLACING whatever it had.

        A re-load is the whole point: if a question is no longer ambiguous
        (or the book no longer produces it), its stale entry must not
        linger and spend a request. A book with nothing to ask loses its
        file entirely.
        """
        recorded = self._recorded.get(book_id, {})
        path = self.pending_path(book_id)
        if not recorded:
            path.unlink(missing_ok=True)
            return
        _atomic_write(
            path,
            [recorded[key].as_dict() for key in sorted(recorded)],
        )

    # -- reading ----------------------------------------------------------
    def pending(self) -> list[Question]:
        """Every recorded question across every book, deduplicated by key.

        A corrupt or half-written file is skipped, not fatal: `lore
        disambiguate` should ask what it can read rather than refuse to
        run.
        """
        found: dict[str, Question] = {}
        if not self.pending_dir.is_dir():
            return []
        for path in sorted(self.pending_dir.glob("*.json")):
            try:
                rows = json.loads(path.read_text())
                if not isinstance(rows, list):
                    continue
                for raw in rows:
                    question = Question.from_dict(raw)
                    found[question.key] = question
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return [found[key] for key in sorted(found)]

    def answers(self) -> dict[str, str]:
        """The answer cache, or {} if it is missing or unreadable."""
        if self._answers is None:
            self._answers = self._read_answers()
        return self._answers

    def _read_answers(self) -> dict[str, str]:
        try:
            raw = json.loads(self.answers_path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            str(key): value
            for key, value in raw.items()
            if isinstance(value, str)
        }

    def answer_for(self, question: Question) -> str | None:
        """The cached entity id to merge into, or None.

        None covers three different things on purpose, because they all
        mean "do not merge": no cached answer, a cached refusal (""), and
        a cached answer naming an entity this question does not offer —
        a stale entry from before a re-extraction, which must never be
        allowed to fuse an arbitrary pair of entities.
        """
        answer = self.answers().get(question.key)
        if not answer:
            return None
        return answer if answer in {c.id for c in question.candidates} else None

    def unanswered(self, questions: Iterable[Question]) -> list[Question]:
        """The questions with no cache entry at all — what a run must ask.

        A cached refusal counts as answered: it was paid for and it stands
        until the question itself changes.
        """
        cached = self.answers()
        return [q for q in questions if q.key not in cached]

    # -- writing ----------------------------------------------------------
    def save_answers(self, answers: dict[str, str]) -> None:
        """Merge new answers into the cache. Atomic; never partial.

        Re-reads from disk rather than merging into the memoized copy: an
        answer another run paid for while this one was talking to the API
        must not be thrown away by a stale in-memory snapshot.
        """
        merged = self._read_answers()
        merged.update(answers)
        _atomic_write(self.answers_path, merged)
        self._answers = merged
