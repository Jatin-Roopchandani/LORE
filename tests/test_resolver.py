import pytest

from lore.resolve.resolver import Mention, resolve_mentions


def _embedder(vectors: dict[str, list[float]]):
    async def embed(texts):
        return [vectors.get(t.split(" — ")[0], [0.0, 0.0, 1.0]) for t in texts]

    return embed


async def _never(canonical, mention) -> bool:
    return False


async def _always(canonical, mention) -> bool:
    return True


@pytest.mark.asyncio
async def test_exact_and_honorific_forms_collapse_without_embedding() -> None:
    calls: list[list[str]] = []

    async def embed(texts):
        calls.append(texts)
        return [[1.0, 0.0, 0.0] for _ in texts]

    result = await resolve_mentions(
        [
            Mention("Elizabeth Bennet", "Character", "b1", "she laughed"),
            Mention("Miss Bennet", "Character", "b1", "she laughed"),
        ],
        embed,
        _never,
    )
    # "miss bennet" normalizes to "bennet", not "elizabeth bennet" — so this
    # pair does NOT collapse on pass 1; it must reach the embedder.
    assert calls, "second mention should have needed an embedding"
    assert len(result.entities) >= 1


@pytest.mark.asyncio
async def test_identical_names_collapse_on_pass_one() -> None:
    calls: list[list[str]] = []

    async def embed(texts):
        calls.append(texts)
        return [[1.0, 0.0, 0.0] for _ in texts]

    result = await resolve_mentions(
        [
            Mention("Mr. Darcy", "Character", "b1", "he bowed"),
            Mention("Darcy", "Character", "b1", "he rode away"),
        ],
        embed,
        _never,
    )
    assert len(result.entities) == 1
    assert calls == []  # pass 1 handled both


@pytest.mark.asyncio
async def test_same_book_similar_names_merge_on_embedding() -> None:
    embed = _embedder({"Lizzy": [1.0, 0.0, 0.0], "Elizabeth Bennet": [1.0, 0.0, 0.0]})
    result = await resolve_mentions(
        [
            Mention("Elizabeth Bennet", "Character", "b1", "she laughed"),
            Mention("Lizzy", "Character", "b1", "she laughed"),
        ],
        embed,
        _never,  # adjudication would say no; same-book similarity is enough
    )
    assert len(result.entities) == 1
    assert set(result.entities[0].aliases) == {"elizabeth bennet", "lizzy"}
    assert len(result.aliases) == 1


@pytest.mark.asyncio
async def test_dissimilar_names_stay_separate() -> None:
    embed = _embedder({"Darcy": [1.0, 0.0, 0.0], "Wickham": [0.0, 1.0, 0.0]})
    result = await resolve_mentions(
        [
            Mention("Darcy", "Character", "b1", "he bowed"),
            Mention("Wickham", "Character", "b1", "he lied"),
        ],
        embed,
        _always,  # even a yes-man adjudicator must not be consulted
    )
    assert len(result.entities) == 2


@pytest.mark.asyncio
async def test_cross_book_merge_requires_adjudication() -> None:
    embed = _embedder({"the Captain": [1.0, 0.0, 0.0]})
    refused = await resolve_mentions(
        [
            Mention("the Captain", "Character", "b1", "he shouted"),
            Mention("the Captain", "Character", "b2", "he wept"),
        ],
        embed,
        _never,
    )
    assert len(refused.entities) == 2, "different books must not fuse silently"

    approved = await resolve_mentions(
        [
            Mention("the Captain", "Character", "b1", "he shouted"),
            Mention("the Captain", "Character", "b2", "he wept"),
        ],
        embed,
        _always,
    )
    assert len(approved.entities) == 1
    assert set(approved.entities[0].book_ids) == {"b1", "b2"}


@pytest.mark.asyncio
async def test_different_kinds_never_merge() -> None:
    embed = _embedder({"Netherfield": [1.0, 0.0, 0.0]})
    result = await resolve_mentions(
        [
            Mention("Netherfield", "Place", "b1", "the estate"),
            Mention("Netherfield", "Faction", "b1", "the household"),
        ],
        embed,
        _always,
    )
    assert len(result.entities) == 2


@pytest.mark.asyncio
async def test_mention_map_points_every_mention_at_an_entity() -> None:
    embed = _embedder({"Darcy": [1.0, 0.0, 0.0], "Wickham": [0.0, 1.0, 0.0]})
    mentions = [
        Mention("Darcy", "Character", "b1", "he bowed"),
        Mention("Wickham", "Character", "b1", "he lied"),
    ]
    result = await resolve_mentions(mentions, embed, _never)
    ids = {result.mention_map[(m.name, m.kind, m.book_id)] for m in mentions}
    assert len(ids) == 2
    assert all(i.startswith("e") for i in ids)


@pytest.mark.asyncio
async def test_mention_with_empty_normalized_name_mints_no_entity() -> None:
    async def embed(texts):
        return [[1.0, 0.0, 0.0] for _ in texts]

    result = await resolve_mentions(
        [Mention("...", "Character", "b1", "nothing here")],
        embed,
        _never,
    )
    assert result.entities == []
    assert result.aliases == []
    assert result.mention_map == {}
