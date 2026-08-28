"""Map free-text predicates onto the curated vocabulary.

The extraction model prefers literal verbs (COMMANDS, DISTRUSTS) over the
curated set. Traversal queries need a small closed vocabulary, so each raw
predicate is mapped to its nearest curated type by embedding similarity —
but the raw string is always preserved alongside it, because DISTRUSTS and
KILLS both collapsing to OPPOSES would destroy real meaning.
"""
from __future__ import annotations

import math
from collections.abc import Awaitable, Callable

CURATED_PREDICATES: tuple[str, ...] = (
    "ALLEGIANCE_TO", "MEMBER_OF", "LOCATED_IN", "POSSESSES",
    "INTERACTS_WITH", "OPPOSES", "LOVES", "KILLS", "BECOMES", "EXPERIENCES",
)
UNCATEGORIZED = "UNCATEGORIZED"

Embed = Callable[[list[str]], Awaitable[list[list[float]]]]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


async def normalize_predicates(
    raws: list[str], embed: Embed, threshold: float = 0.75
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    unknown: list[str] = []
    for raw in raws:
        if raw in mapping:
            continue
        if raw in CURATED_PREDICATES:
            mapping[raw] = raw
        elif raw not in unknown:
            unknown.append(raw)
    if not unknown:
        return mapping

    vectors = await embed(list(CURATED_PREDICATES) + unknown)
    curated_vectors = vectors[: len(CURATED_PREDICATES)]
    unknown_vectors = vectors[len(CURATED_PREDICATES) :]
    for raw, vector in zip(unknown, unknown_vectors):
        scores = [cosine(vector, cv) for cv in curated_vectors]
        best = max(range(len(scores)), key=lambda i: scores[i])
        mapping[raw] = (
            CURATED_PREDICATES[best] if scores[best] >= threshold else UNCATEGORIZED
        )
    return mapping
