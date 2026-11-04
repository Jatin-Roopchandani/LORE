"""The decomposer's recap: one general rule, the span it records, and nothing
changed with RECAP off. No test reaches a model."""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from lore.query import decompose as decompose_module
from lore.query.decompose import (
    Decomposition, PartSpec, Plan, Recap, RecapDecomposition, active_schema,
    decomposition_prompt, plan,
)


def _returning(result, sink: list | None = None):
    async def generate(prompt: str, schema: type[BaseModel]):
        if sink is not None:
            sink.append((prompt, schema))
        return result
    return generate


def _reply(recap, question="Summarise the story so far."):
    return RecapDecomposition(sub_questions=[PartSpec(question=question)], depth="focused",
                              recap=recap)


def test_recap_is_on_by_default_and_asked_for_in_one_general_rule() -> None:
    assert decompose_module.RECAP is True
    assert active_schema() is RecapDecomposition
    prompt = " ".join(decomposition_prompt("q?").split())
    assert 'Also return "recap"' in prompt
    assert "an account of what happens over a stretch of the story, told in order" in prompt
    for span in ('"story"', '"book"', '"chapters"', '"since"', '"none"'):
        assert span in prompt
    assert 'When unsure, use "none"' in prompt
    # One rule, not a list of example phrasings.
    assert "so far" in prompt and "sumarrize" not in prompt.lower()


def test_the_schema_keeps_its_name_and_adds_only_the_recap() -> None:
    schema = RecapDecomposition.model_json_schema()
    assert schema["title"] == "Decomposition"
    assert set(schema["properties"]) == set(Decomposition.model_json_schema()["properties"]) | {"recap"}


def test_recap_off_gives_the_prompt_and_schema_of_before(monkeypatch) -> None:
    with_recap = decomposition_prompt("Who did it?", ["Alpha Tale"])
    monkeypatch.setattr(decompose_module, "RECAP", False)
    without = decomposition_prompt("Who did it?", ["Alpha Tale"])
    assert active_schema() is Decomposition
    assert '"recap"' not in without
    assert with_recap.startswith(without.rstrip("\n"))  # the rule is only appended


async def test_recap_off_ignores_a_recap_in_the_reply(monkeypatch) -> None:
    monkeypatch.setattr(decompose_module, "RECAP", False)
    got = await plan("q", _returning(_reply({"span": "story"})))
    assert got.recap is None


@pytest.mark.parametrize("raw, want", [
    ({"span": "story"}, Recap("story")),
    ({"span": "Book", "book": "Alpha Two"}, Recap("book", book="Alpha Two")),
    ({"span": "book", "book": 2}, Recap("book", book="2")),
    ({"span": "chapters", "from_chapter": 3, "to_chapter": 6}, Recap("chapters", first=3, last=6)),
    ({"span": "chapters", "from_chapter": "6", "to_chapter": 3, "book": "Alpha One"},
     Recap("chapters", book="Alpha One", first=3, last=6)),
    ({"span": "chapters", "from_chapter": 4}, Recap("chapters", first=4, last=4)),
    ({"span": "since", "since": "the stranger left the inn"},
     Recap("since", since="the stranger left the inn")),
], ids=["story", "book", "book-number", "chapters", "chapters-reversed", "one-chapter", "since"])
async def test_the_span_is_recorded(raw, want) -> None:
    got = await plan("q", _returning(_reply(raw)))
    assert got.recap == want
    assert [p.kind for p in got.parts] == ["factual"]


@pytest.mark.parametrize("raw, want", [
    (None, None),
    ({"span": "none"}, None),
    ({"span": "everything"}, None),
    ("story", None),
    ({"span": "chapters", "book": "Alpha One"}, Recap("book", book="Alpha One")),
    ({"span": "chapters"}, Recap("story")),
    ({"span": "book"}, Recap("story")),
    ({"span": "since", "since": " "}, Recap("story")),
    ({"span": "story", "from_chapter": "x", "book": ["?"]}, Recap("story")),
], ids=["absent", "none", "unknown", "not-an-object", "chapters-no-numbers", "chapters-nothing",
        "book-unnamed", "since-blank", "junk-fields"])
async def test_an_unusable_span_is_none_or_the_nearest_usable_one(raw, want) -> None:
    got = await plan("q", _returning(_reply(raw)))
    assert got.recap == want


async def test_a_question_that_is_no_recap_plans_as_before() -> None:
    reply = RecapDecomposition(sub_questions=[PartSpec(question="Who is Ana?")], depth="focused")
    got = await plan("Who is Ana?", _returning(reply))
    assert got == Plan(got.parts, "focused") and got.recap is None
