"""The one LLM decision before synthesis: which template, filled how.

Picking among seven named options and a few numbered entities is a
constrained classification -- the kind of thing a 3B model does reliably,
and the reason this phase does not ask it to write Cypher.

The model never sees an entity id. Measured against the live 3B with ids
in the prompt, id copying was mostly fine (40/45 exact) but the choice's
SHAPE was not: it packed two ids into one field, or picked by_predicate
without knowing any predicate. So the seeds are a numbered list, the
entities are one list of numbers, each template says how many it takes,
and by_predicate names its vocabulary. The question's shape does the work.

Determinism is the caller's job, not this module's: whoever builds
`generate` for selection must pass temperature 0 and seed 0 to the model.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from pydantic import BaseModel

from lore.query.models import SeedEntity, Template, TemplateCall
from lore.query.templates import TEMPLATES
from lore.resolve.predicates import CURATED_PREDICATES

Generate = Callable[[str, type[BaseModel]], Awaitable[BaseModel]]

MAX_CHAPTER = 1_000_000

_PROMPT = """You are choosing how to search a knowledge graph built from books.

Question: {question}

Entities found in passages related to the question, numbered:
{seeds}

Available searches, each with how many entities it takes:
{templates}

Relationship types for by_predicate (use exactly one of these):
{predicates}

Choose exactly one search. In "entities", give the NUMBERS of the
entities it should use, from the numbered list above: one number for a
1-entity search, two different numbers for a 2-entity search. For
by_predicate, also give "predicate" from the relationship types above.
If the question asks about a period of the story, give the chapter
range; otherwise leave the chapter numbers at 0.
"""


class TemplateChoice(BaseModel):
    template: str
    entities: list[int]
    predicate: str = ""
    chapter_lo: int = 0
    chapter_hi: int = 0


def _entity_slots(template: Template) -> tuple[str, ...]:
    # Every entity slot is named *_id (entity_id; a_id, b_id), so the
    # count cannot drift from the Cypher it binds into.
    return tuple(slot for slot in template.slots if slot.endswith("_id"))


def entity_count(template: Template) -> int:
    return len(_entity_slots(template))


def _render_templates() -> str:
    lines = []
    for t in TEMPLATES.values():
        n = entity_count(t)
        label = "1 entity" if n == 1 else f"{n} entities"
        lines.append(f"- {t.name} ({label}): {t.description}")
    return "\n".join(lines)


def _chosen_ids(numbers: list[int], seeds: list[SeedEntity]) -> list[str] | None:
    """Map 1-based numbers to seed ids, distinct, in first-seen order.

    Any number outside the list means the model misread it: refuse
    rather than guess which of its other numbers it meant.
    """
    ids: list[str] = []
    for n in numbers:
        if not 1 <= n <= len(seeds):
            return None
        seed_id = seeds[n - 1].id
        if seed_id not in ids:
            ids.append(seed_id)
    return ids


def _slots_for(choice: TemplateChoice, seeds: list[SeedEntity], limit: int) -> dict | None:
    template = TEMPLATES.get(choice.template)
    if template is None:
        return None
    ids = _chosen_ids(choice.entities, seeds)
    wanted = _entity_slots(template)
    # Extra numbers are ignored; too few distinct ones cannot be honoured.
    if ids is None or len(ids) < len(wanted):
        return None
    slots: dict = dict(zip(wanted, ids))
    if "predicate" in template.slots:
        predicate = choice.predicate.strip().upper()
        if predicate not in CURATED_PREDICATES:
            return None
        slots["predicate"] = predicate
    if "lo" in template.slots:
        lo, hi = choice.chapter_lo, choice.chapter_hi
        # An unset range means "no constraint", not "chapter 0 to 0" --
        # which would match nothing and look like an empty graph.
        if hi <= lo:
            lo, hi = 0, MAX_CHAPTER
        slots["lo"], slots["hi"] = lo, hi
    slots["limit"] = limit
    return slots


async def select_template(
    question: str,
    seeds: list[SeedEntity],
    generate: Generate,
    *,
    limit: int = 40,
) -> TemplateCall | None:
    """Pick a template, or None if the model's choice cannot be honoured.

    None is not an error: the caller treats it exactly like a template
    that returned no rows, and falls back to answering from passages.
    """
    if not seeds:
        return None

    rendered_seeds = "\n".join(
        f"{i}) {s.name} ({s.kind}, in {len(s.books)} "
        f"{'book' if len(s.books) == 1 else 'books'})"
        for i, s in enumerate(seeds, start=1)
    )
    choice = await generate(
        _PROMPT.format(
            question=question,
            seeds=rendered_seeds,
            templates=_render_templates(),
            predicates=", ".join(CURATED_PREDICATES),
        ),
        TemplateChoice,
    )
    slots = _slots_for(choice, seeds, limit)
    if slots is None:
        return None
    return TemplateCall(template=choice.template, slots=slots)
