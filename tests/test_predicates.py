import pytest

from lore.resolve.predicates import (
    CURATED_PREDICATES,
    UNCATEGORIZED,
    cosine,
    normalize_predicates,
)


def _vec(*values: float) -> list[float]:
    return list(values)


def test_cosine_basics() -> None:
    assert cosine(_vec(1, 0), _vec(1, 0)) == pytest.approx(1.0)
    assert cosine(_vec(1, 0), _vec(0, 1)) == pytest.approx(0.0)
    assert cosine(_vec(0, 0), _vec(1, 0)) == 0.0  # zero vector, no crash


@pytest.mark.asyncio
async def test_exact_curated_match_skips_the_embedder() -> None:
    calls: list[list[str]] = []

    async def embed(texts):
        calls.append(texts)
        return [[1.0, 0.0] for _ in texts]

    mapping = await normalize_predicates(["OPPOSES", "LOVES"], embed)
    assert mapping == {"OPPOSES": "OPPOSES", "LOVES": "LOVES"}
    assert calls == []  # nothing needed embedding


@pytest.mark.asyncio
async def test_near_predicate_maps_to_curated() -> None:
    # DISTRUSTS is made identical to OPPOSES; everything else orthogonal.
    async def embed(texts):
        out = []
        for text in texts:
            if text in ("OPPOSES", "DISTRUSTS"):
                out.append([1.0, 0.0])
            else:
                out.append([0.0, 1.0])
        return out

    mapping = await normalize_predicates(["DISTRUSTS"], embed)
    assert mapping["DISTRUSTS"] == "OPPOSES"


@pytest.mark.asyncio
async def test_unrelated_predicate_becomes_uncategorized() -> None:
    async def embed(texts):
        return [[1.0, 0.0] if t != "SMELLS_LIKE" else [0.0, 1.0] for t in texts]

    mapping = await normalize_predicates(["SMELLS_LIKE"], embed)
    assert mapping["SMELLS_LIKE"] == UNCATEGORIZED


@pytest.mark.asyncio
async def test_duplicate_raws_are_embedded_once() -> None:
    seen: list[list[str]] = []

    async def embed(texts):
        seen.append(texts)
        return [[0.0, 1.0] for _ in texts]

    await normalize_predicates(["FOO", "FOO", "FOO"], embed)
    embedded_raws = [t for batch in seen for t in batch if t == "FOO"]
    assert len(embedded_raws) == 1


def test_curated_list_is_the_documented_ten() -> None:
    assert CURATED_PREDICATES == (
        "ALLEGIANCE_TO", "MEMBER_OF", "LOCATED_IN", "POSSESSES",
        "INTERACTS_WITH", "OPPOSES", "LOVES", "KILLS", "BECOMES", "EXPERIENCES",
    )
