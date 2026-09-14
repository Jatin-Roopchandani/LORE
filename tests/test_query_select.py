import pytest

from lore.query.models import SeedEntity
from lore.query.select import TemplateChoice, entity_count, select_template
from lore.query.templates import TEMPLATES
from lore.resolve.predicates import CURATED_PREDICATES, UNCATEGORIZED

# Ids deliberately look like the real ones: the prompt must never show them.
SEEDS = [
    SeedEntity(id="e1a2b3c4d5e6f", name="Holmes", kind="Character",
               books=("b1", "b2"), score=0.1),
    SeedEntity(id="e0f9e8d7c6b5a", name="Watson", kind="Character",
               books=("b1",), score=0.2),
    SeedEntity(id="e777777777777", name="Baker Street", kind="Place",
               books=("b1", "b2"), score=0.3),
]
HOLMES, WATSON, BAKER = (s.id for s in SEEDS)


def _generate(choice: TemplateChoice):
    async def generate(prompt: str, schema):
        return choice
    return generate


async def _select(choice: TemplateChoice, seeds=SEEDS):
    return await select_template("q", seeds, _generate(choice))


async def _prompt_for(seeds=SEEDS) -> str:
    seen: dict[str, str] = {}

    async def generate(prompt: str, schema):
        seen["prompt"] = prompt
        return TemplateChoice(template="neighbours", entities=[1])

    await select_template("q", seeds, generate)
    return seen["prompt"]


# --- the plan's eight, ported to numbered choices -------------------------

@pytest.mark.asyncio
async def test_a_valid_choice_becomes_a_template_call() -> None:
    call = await _select(TemplateChoice(template="neighbours", entities=[1]))
    assert call.template == "neighbours"
    assert call.slots == {"entity_id": HOLMES, "limit": 40}


@pytest.mark.asyncio
async def test_an_unknown_template_name_is_rejected() -> None:
    # A 3B model will invent names. Rejecting is correct; executing an
    # unknown key would KeyError deep in traversal.
    assert await _select(TemplateChoice(template="magic", entities=[1])) is None


@pytest.mark.asyncio
async def test_a_number_outside_the_seed_list_is_rejected() -> None:
    # The model may only choose among entities we actually surfaced.
    assert await _select(TemplateChoice(template="neighbours", entities=[4])) is None


@pytest.mark.asyncio
async def test_a_template_needing_two_entities_rejects_a_missing_second() -> None:
    assert await _select(TemplateChoice(template="path_between", entities=[1])) is None


@pytest.mark.asyncio
async def test_two_entity_template_fills_both_slots() -> None:
    call = await _select(TemplateChoice(template="path_between", entities=[1, 2]))
    assert call.slots == {"a_id": HOLMES, "b_id": WATSON, "limit": 40}


@pytest.mark.asyncio
async def test_chapter_window_defaults_an_unset_range_to_everything() -> None:
    call = await _select(TemplateChoice(template="chapter_window", entities=[1],
                                        chapter_lo=0, chapter_hi=0))
    assert call.slots["entity_id"] == HOLMES
    assert call.slots["lo"] == 0
    assert call.slots["hi"] >= 10_000


@pytest.mark.asyncio
async def test_chapter_window_keeps_a_real_range() -> None:
    call = await _select(TemplateChoice(template="chapter_window", entities=[1],
                                        chapter_lo=3, chapter_hi=7))
    assert (call.slots["lo"], call.slots["hi"]) == (3, 7)


@pytest.mark.asyncio
async def test_no_seeds_selects_nothing_without_calling_the_model() -> None:
    called = False

    async def generate(prompt: str, schema):
        nonlocal called
        called = True
        raise AssertionError("must not reach the model")

    assert await select_template("q", [], generate) is None
    assert called is False


@pytest.mark.asyncio
async def test_the_prompt_lists_every_template_and_every_seed() -> None:
    prompt = await _prompt_for()
    for name in TEMPLATES:
        assert name in prompt
    for seed in SEEDS:
        assert seed.name in prompt


# --- the prompt's shape ---------------------------------------------------

@pytest.mark.asyncio
async def test_the_prompt_never_shows_a_seed_id() -> None:
    # 40/45 ids were copied exactly, but the other 5 can never be
    # recovered. A number cannot be garbled into another entity's id.
    prompt = await _prompt_for()
    for seed in SEEDS:
        assert seed.id not in prompt


@pytest.mark.asyncio
async def test_the_prompt_numbers_the_seeds_from_one() -> None:
    prompt = await _prompt_for()
    assert "1) Holmes" in prompt
    assert "2) Watson" in prompt
    assert "3) Baker Street" in prompt
    assert "0)" not in prompt


def test_entity_count_is_derived_from_the_template_slots() -> None:
    assert entity_count(TEMPLATES["neighbours"]) == 1
    assert entity_count(TEMPLATES["by_predicate"]) == 1
    assert entity_count(TEMPLATES["chapter_window"]) == 1
    assert entity_count(TEMPLATES["path_between"]) == 2
    assert entity_count(TEMPLATES["shared_neighbours"]) == 2


@pytest.mark.asyncio
async def test_the_prompt_shows_each_templates_entity_count() -> None:
    prompt = await _prompt_for()
    for name, template in TEMPLATES.items():
        n = entity_count(template)
        label = "1 entity" if n == 1 else f"{n} entities"
        assert f"{name} ({label})" in prompt


@pytest.mark.asyncio
async def test_the_prompt_lists_the_curated_predicates_but_not_uncategorized() -> None:
    prompt = await _prompt_for()
    for predicate in CURATED_PREDICATES:
        assert predicate in prompt
    assert UNCATEGORIZED not in prompt


# --- validation -----------------------------------------------------------

@pytest.mark.asyncio
async def test_numbers_are_one_based_so_zero_is_rejected() -> None:
    assert await _select(TemplateChoice(template="neighbours", entities=[0])) is None


@pytest.mark.asyncio
async def test_a_negative_number_is_rejected() -> None:
    assert await _select(TemplateChoice(template="neighbours", entities=[-1])) is None


@pytest.mark.asyncio
async def test_an_empty_entity_list_is_rejected() -> None:
    assert await _select(TemplateChoice(template="neighbours", entities=[])) is None


@pytest.mark.asyncio
async def test_an_out_of_range_number_anywhere_rejects_the_choice() -> None:
    # One bad number means the model misread the list; do not guess
    # which of its other numbers it meant.
    assert await _select(TemplateChoice(template="path_between",
                                        entities=[1, 2, 9])) is None


@pytest.mark.asyncio
async def test_a_repeated_number_does_not_count_as_two_entities() -> None:
    assert await _select(TemplateChoice(template="path_between",
                                        entities=[2, 2])) is None


@pytest.mark.asyncio
async def test_two_entity_template_uses_the_first_two_distinct_numbers() -> None:
    call = await _select(TemplateChoice(template="shared_neighbours",
                                        entities=[2, 2, 1, 3]))
    assert call.slots == {"a_id": WATSON, "b_id": HOLMES, "limit": 40}


@pytest.mark.asyncio
async def test_two_entity_template_ignores_a_third_number() -> None:
    call = await _select(TemplateChoice(template="path_between", entities=[2, 1, 3]))
    assert call.slots == {"a_id": WATSON, "b_id": HOLMES, "limit": 40}


@pytest.mark.asyncio
async def test_one_entity_template_uses_the_first_number_and_ignores_extras() -> None:
    call = await _select(TemplateChoice(template="across_books", entities=[2, 1]))
    assert call.slots == {"entity_id": WATSON, "limit": 40}


@pytest.mark.asyncio
async def test_by_predicate_fills_entity_and_predicate() -> None:
    call = await _select(TemplateChoice(template="by_predicate", entities=[3],
                                        predicate="LOCATED_IN"))
    assert call.slots == {"entity_id": BAKER, "predicate": "LOCATED_IN", "limit": 40}


@pytest.mark.asyncio
async def test_by_predicate_normalises_case_and_whitespace() -> None:
    call = await _select(TemplateChoice(template="by_predicate", entities=[1],
                                        predicate="  kills "))
    assert call.slots["predicate"] == "KILLS"


@pytest.mark.asyncio
async def test_by_predicate_rejects_a_predicate_outside_the_vocabulary() -> None:
    assert await _select(TemplateChoice(template="by_predicate", entities=[1],
                                        predicate="MURDERS")) is None


@pytest.mark.asyncio
async def test_by_predicate_rejects_uncategorized() -> None:
    assert await _select(TemplateChoice(template="by_predicate", entities=[1],
                                        predicate="UNCATEGORIZED")) is None


@pytest.mark.asyncio
async def test_by_predicate_rejects_an_empty_predicate() -> None:
    assert await _select(TemplateChoice(template="by_predicate", entities=[1])) is None


@pytest.mark.asyncio
async def test_a_custom_limit_reaches_the_slots() -> None:
    call = await select_template(
        "q", SEEDS, _generate(TemplateChoice(template="two_hop", entities=[1])),
        limit=7,
    )
    assert call.slots == {"entity_id": HOLMES, "limit": 7}
