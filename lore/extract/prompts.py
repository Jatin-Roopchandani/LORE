"""Extraction prompt. Imported by both tiers — never duplicated in the kernel."""
from __future__ import annotations

PREFERRED_PREDICATES: tuple[str, ...] = (
    "ALLEGIANCE_TO",
    "MEMBER_OF",
    "LOCATED_IN",
    "POSSESSES",
    "INTERACTS_WITH",
    "OPPOSES",
    "LOVES",
    "KILLS",
    "BECOMES",
    "EXPERIENCES",
)

_TEMPLATE = """You extract a knowledge graph from a passage of a novel.

Read the PASSAGE and return every entity it names and every relationship it \
states or clearly implies.

Rules:
- Use the entity's fullest name as written in the passage.
- Prefer these predicates when one fits: {predicates}. Otherwise use a short \
SCREAMING_SNAKE_CASE verb phrase.
- `evidence` must be a verbatim span copied from the passage that supports the \
relationship. Never invent or paraphrase it.
- `confidence` is 0.0-1.0: use 0.9+ when the passage states it outright, 0.5-0.7 \
when it is implied, below 0.5 when it is a guess.
- Extract only what this passage supports. Return empty lists rather than \
speculating.

PASSAGE:
{chunk_text}
"""


def build_prompt(chunk_text: str) -> str:
    return _TEMPLATE.format(
        predicates=", ".join(PREFERRED_PREDICATES), chunk_text=chunk_text
    )
