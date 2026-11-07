"""resolve_mentions over names split by kind or number, the text's own namings,
and the false aliases measured on a novel's graph (lore/resolve/unify.py)."""
import pytest

from lore.resolve.normalize import entity_id
from lore.resolve.resolver import Mention, resolve_mentions
from lore.resolve.unify import Equivalence


async def _same(texts):
    return [[1.0, 0.0, 0.0] for _ in texts]  # every pair looks identical


async def _never(canonical, mention) -> bool:
    return False


def _ids(result) -> dict[str, str]:
    return {name: target for (name, _kind, _book), target in result.mention_map.items()}


@pytest.mark.asyncio
async def test_one_name_under_several_kinds_is_one_entity_of_the_majority_kind() -> None:
    mentions = [Mention("The Dark One", "Character", "b", "the Dark One stirs", weight=161),
                Mention("Dark One", "Concept", "b", "the Dark One's touch", weight=39),
                Mention("Myrddraal", "Faction", "b", "a Myrddraal came", weight=41),
                Mention("Myrddraal", "Character", "b", "the Myrddraal spoke", weight=30),
                Mention("Myrddraal", "Object", "b", "Myrddraal blade", weight=2)]
    result = await resolve_mentions(mentions, _same, _never)
    kinds = {e.name: e.kind for e in result.entities}
    assert kinds == {"The Dark One": "Character", "Myrddraal": "Faction"}
    # every original (name, kind) answers, so every extracted edge finds its entity
    for m in mentions:
        assert (m.name, m.kind, m.book_id) in result.mention_map
    dark = result.mention_map[("Dark One", "Concept", "b")]
    assert dark == result.mention_map[("The Dark One", "Character", "b")]
    # the old (name, kind) id points at the entity
    assert any(a.alias_id == entity_id("dark one", "Concept") and a.canonical_id == dark
               for a in result.aliases)


@pytest.mark.asyncio
async def test_plurals_join_their_singular() -> None:
    mentions = [Mention("the Fades", "Faction", "b", "the Fades rode", weight=24),
                Mention("Fades", "Character", "b", "Fades hunt", weight=4),
                Mention("The Fade", "Character", "b", "the Fade watched", weight=15),
                Mention("The Fade", "Faction", "b", "a Fade came", weight=14),
                Mention("the Fade", "Concept", "b", "fear of the Fade", weight=6)]
    result = await resolve_mentions(mentions, _same, _never)
    assert len(result.entities) == 1
    entity = result.entities[0]
    assert entity.kind == "Faction"  # 24 + 14 Faction against 4 + 15 Character
    assert {"fade", "fades"} <= set(entity.aliases)
    assert len(set(result.mention_map.values())) == 1


@pytest.mark.asyncio
async def test_different_books_and_different_names_stay_apart() -> None:
    result = await resolve_mentions(
        [Mention("Aurora", "Object", "b1", "the Aurora sailed"),
         Mention("Aurora", "Character", "b2", "Aurora smiled"),
         Mention("Rand", "Character", "b1", "Rand ran")], _same, _never)
    assert len(result.entities) == 3


@pytest.mark.asyncio
async def test_a_naming_in_the_text_is_asked_and_merged_only_on_yes() -> None:
    mentions = [Mention("Myrddraal", "Faction", "b", "a Myrddraal came", weight=41),
                Mention("Halfmen", "Faction", "b", "the Halfmen hunt", weight=6)]
    eq = Equivalence("Myrddraal", "Halfmen", "Myrddraal, also called Halfmen, hunt.", "b:1:3",
                     False)
    asked = []
    no = await resolve_mentions(mentions, _same, _never, record_ambiguous=asked.append,
                                decide_ambiguous=lambda q: None, equivalences=[eq])
    assert len(set(no.mention_map.values())) == 2
    question = asked[0]
    assert question.mention == "Halfmen" and [c.name for c in question.candidates] == ["Myrddraal"]
    yes = await resolve_mentions(mentions, _same, _never,
                                 decide_ambiguous=lambda q: q.candidates[0].id, equivalences=[eq])
    assert len(set(yes.mention_map.values())) == 1
    assert "halfmen" in yes.entities[0].aliases


@pytest.mark.asyncio
async def test_a_believed_identity_is_never_merged_here() -> None:
    mentions = [Mention("Ba'alzamon", "Character", "b", "Ba'alzamon laughed"),
                Mention("The Dark One", "Character", "b", "the Dark One")]
    eq = Equivalence("Dark One", "Ba'alzamon", "“Ba'alzamon is the Dark One,” he said.", "b:1:2",
                     True)
    result = await resolve_mentions(mentions, _same, _never,
                                    decide_ambiguous=lambda q: q.candidates[0].id,
                                    equivalences=[eq])
    assert len(set(result.mention_map.values())) == 2


def test_a_believed_identity_becomes_an_identified_as_row() -> None:
    from lore.graph.loader import IDENTIFIED_AS, _identity_rows

    triplets = [{"subject": "Ba'alzamon", "predicate": "OPPOSES", "object": "Rand",
                 "evidence": "x", "chunk_id": "b:1:1",
                 "entity_names": ["Ba'alzamon", "Rand", "The Dark One"],
                 "entity_kinds": ["Character", "Character", "Character"]}]
    eqs = [Equivalence("Dark One", "Ba'alzamon", "“Ba'alzamon is the Dark One,” he said.",
                       "b:1:2", True),
           Equivalence("Myrddraal", "Halfmen", "naming", "b:1:3", False)]
    rows = _identity_rows(eqs, triplets)
    assert [(r["subject"], r["predicate"], r["object"], r["chunk_id"]) for r in rows] == [
        ("Ba'alzamon", IDENTIFIED_AS, "The Dark One", "b:1:2")]


def test_a_named_seed_brings_the_entities_it_is_identified_as() -> None:
    from lore.query.seed import identified_with

    class Kuzu:
        def execute(self, cypher, params=None):
            assert "IDENTIFIED_AS" in cypher and params == {"ids": ["dark"]}
            return [["baal"]]

    assert identified_with(Kuzu(), ["dark"]) == ["baal"]
    assert identified_with(Kuzu(), []) == []


# --- false aliases measured on the graph -----------------------------------

@pytest.mark.asyncio
async def test_a_part_of_a_coordinated_name_is_not_absorbed() -> None:
    result = await resolve_mentions(
        [Mention("Air and Water", "Concept", "b", "she wove Air and Water"),
         Mention("Water", "Concept", "b", "a weave of Water")], _same, _never)
    assert len(set(result.mention_map.values())) == 2


@pytest.mark.asyncio
async def test_a_bare_title_is_not_a_name() -> None:
    result = await resolve_mentions(
        [Mention("Artur the High King", "Character", "b", "Artur Hawkwing ruled"),
         Mention("King", "Character", "b", "the King rode out")], _same, _never)
    assert len(set(result.mention_map.values())) == 2


@pytest.mark.asyncio
async def test_names_listed_together_are_different_things() -> None:
    sentence = "the Blue Ajah, the White Ajah and the Yellow Ajah sat"
    result = await resolve_mentions(
        [Mention(f"{c} Ajah", "Faction", "b", sentence)
         for c in ("Blue", "White", "Yellow", "Green")], _same, _never)
    assert len(set(result.mention_map.values())) == 4


@pytest.mark.asyncio
async def test_one_head_word_with_different_modifiers_is_two_things() -> None:
    # each in its own sentence, so only the names can tell them apart
    result = await resolve_mentions(
        [Mention("Bond Street", "Place", "b", "a shop on it"),
         Mention("Miles Street", "Place", "b", "a lodging there"),
         Mention("Black Ajah", "Faction", "b", "hidden among the sisters"),
         Mention("Red Ajah", "Faction", "b", "they gentle men")], _same, _never)
    assert len(set(result.mention_map.values())) == 4
    # people are left to similarity: a nickname under one surname is one person
    people = await resolve_mentions(
        [Mention("Elizabeth Bennet", "Character", "b", "she laughed"),
         Mention("Lizzy Bennet", "Character", "b", "she walked")], _same, _never)
    assert len(set(people.mention_map.values())) == 1


@pytest.mark.asyncio
async def test_similar_names_with_different_heads_still_merge_by_similarity() -> None:
    result = await resolve_mentions(
        [Mention("Sherlock Holmes", "Character", "b", "Holmes lit his pipe"),
         Mention("Sherlock Holme", "Character", "b", "the detective frowned")], _same, _never)
    assert len(set(result.mention_map.values())) == 1
