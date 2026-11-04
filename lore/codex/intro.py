"""A spoiler-safe introduction to one entity, for a reader partway through.

The codex summary is written over the whole story, so a profile hides it while
a reading position is on. In its place the profile shows a short intro of who
or what the entity is so far: 2 or 3 sentences written by one cheap model call
from ONLY what the reader may see, the entity's codex statements and story
beats that the visibility gate (lore/query/visible.py) lets through, with every
name as the reader has met it. The model is shown nothing past the position and
told to add no fact beyond its input, so the intro is spoiler-safe by
construction.

An intro is cached in the library's SQLite (`entity_intros`), keyed by
(entity, world, the hash of what the model was shown, the prompt hash): moving
the position changes what is visible, so it gets its own intro, and moving back
finds the old one.

Whenever no model intro can be had (INTRO off, no Gemini key or provider, the
daily allowance spent, or the call fails), `fallback_intro` gives a plain one
from the same visible statements: the identity lines, then the first role line.
Nothing here ever raises to the reader.

Nothing here names a book, a person or a genre. The entity and the facts arrive
as data.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel

from lore.codex.store import DISPLAY_ORDER

# Module switch: off, every intro is the deterministic `fallback_intro`.
INTRO = True

# What the model is shown: every identity line, then the others in display
# order, at most MAX_INTRO_STATEMENTS in all; at most MAX_INTRO_BEATS beats,
# spread evenly over what has been read.
MAX_INTRO_STATEMENTS = 24
MAX_INTRO_BEATS = 12
MAX_INTRO_CHARS = 600

INTRO_PROMPT = """You are introducing one {kind} of a story to a reader who is partway through it. Say who or what the subject is so far, from the notes below and nothing else.

Subject: {name} ({kind})

Notes, each established by the part of the text the reader has read:
{statements}

Moments so far, in story order:
{moments}

Rules:
- "intro" is 2 or 3 plain sentences, at most 70 words in all. The first says who or what the subject is: for a person, their occupation, status or position and how they stand to the others named; for anything else, what kind of thing, place or group it is. Then say what part the subject has played so far.
- Use only facts stated in the notes and moments. Add nothing from memory or general knowledge, even if you recognise the story: the reader must not learn anything they have not yet read.
- Call the subject "{name}" and every other figure by the name the notes use.
- Neutral words: no opinion, no guess at a motive, no theme, no verdict.
"""


class IntroAnswer(BaseModel):
    intro: str


@dataclass(frozen=True)
class IntroInput:
    """What a reader may see of one entity: `statements` as (aspect, text) in
    display order then story order, `beats` texts in story order."""
    name: str
    kind: str
    statements: tuple[tuple[str, str], ...]
    beats: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.statements and not self.beats


Generate = Callable[[str, type[BaseModel]], Awaitable[BaseModel]]


def prompt_hash() -> str:
    return hashlib.sha256(INTRO_PROMPT.encode()).hexdigest()[:16]


def _rank(aspect: str) -> int:
    return DISPLAY_ORDER.index(aspect) if aspect in DISPLAY_ORDER else len(DISPLAY_ORDER)


def make_input(name: str, kind: str, statements: Sequence[tuple[str, str]],
               beats: Sequence[str]) -> IntroInput:
    """The model's input from visible `statements` ((aspect, text), story
    order) and visible `beats` (texts, story order): statements regrouped in
    display order (a stable sort keeps story order within an aspect), then
    trimmed; beats sampled evenly over the span read."""
    from lore.query.timeline import coverage_order

    ordered = sorted(statements, key=lambda s: _rank(s[0]))
    beats = list(beats)
    if len(beats) > MAX_INTRO_BEATS:
        beats = [beats[i] for i in sorted(coverage_order(len(beats))[:MAX_INTRO_BEATS])]
    return IntroInput(name=name, kind=kind,
                      statements=tuple(ordered[:MAX_INTRO_STATEMENTS]),
                      beats=tuple(beats))


def scope_hash(inp: IntroInput) -> str:
    """Identifies exactly what the model is shown."""
    payload = json.dumps([inp.name, inp.kind, [list(s) for s in inp.statements],
                          list(inp.beats)])
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def render_prompt(inp: IntroInput) -> str:
    statements = "\n".join(f"- ({a}) {t}" for a, t in inp.statements) or "(none)"
    moments = "\n".join(f"- {b}" for b in inp.beats) or "(none)"
    return INTRO_PROMPT.format(kind=inp.kind or "entity", name=inp.name,
                               statements=statements, moments=moments)


def _sentence(text: str) -> str:
    text = " ".join(text.split())
    return text if not text or text[-1] in ".!?\"'”" else text + "."


def fallback_intro(inp: IntroInput) -> str | None:
    """The intro without a model: the visible identity lines, then the first
    visible role line; with neither, the first visible statement. None when
    nothing is visible."""
    identity = [t for a, t in inp.statements if a == "identity"]
    role = [t for a, t in inp.statements if a == "role"][:1]
    lines = identity + role or [t for _a, t in inp.statements[:1]]
    return " ".join(_sentence(t) for t in lines) or None


def clean(text: str) -> str | None:
    """A model intro on one line, at most MAX_INTRO_CHARS (cut at a sentence
    end when it runs over); None when empty."""
    text = " ".join((text or "").split())
    if len(text) > MAX_INTRO_CHARS:
        cut = text[:MAX_INTRO_CHARS]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        text = cut[:end + 1] if end > 0 else cut.rstrip() + "…"
    return text or None


# --- the cache ---------------------------------------------------------------------


def cached(conn: sqlite3.Connection, entity_id: str, world_id: int,
           scope: str) -> str | None:
    """The saved intro, or None (also when the library predates the table)."""
    try:
        row = conn.execute(
            "SELECT intro FROM entity_intros WHERE entity_id=? AND world_id=?"
            " AND scope_hash=? AND prompt_hash=?",
            (entity_id, world_id, scope, prompt_hash())).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def save(conn: sqlite3.Connection, entity_id: str, world_id: int, scope: str,
         intro: str, model: str | None = None) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO entity_intros (entity_id, world_id, scope_hash,"
            " prompt_hash, intro, model) VALUES (?,?,?,?,?,?)",
            (entity_id, world_id, scope, prompt_hash(), intro, model))


async def generate(inp: IntroInput, generate_json: Generate) -> str | None:
    """One model call; None when it fails or says nothing. Never raises."""
    try:
        answer = await generate_json(render_prompt(inp), IntroAnswer)
    except Exception:  # noqa: BLE001 -- the fallback intro covers every failure
        return None
    return clean(getattr(answer, "intro", "") or "")
