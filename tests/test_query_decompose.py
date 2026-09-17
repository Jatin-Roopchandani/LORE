"""Question decomposition: one structured call, and a safe fallback."""
import pytest
from pydantic import BaseModel

from lore.inference.base import ProviderOutputError
from lore.inference.gemini import GeminiOutputError
from lore.query.decompose import (
    MAX_SUB_QUESTIONS,
    SubQuestions,
    decompose,
    decomposition_prompt,
)


def _returning(result, sink: list | None = None):
    async def generate(prompt: str, schema: type[BaseModel]):
        if sink is not None:
            sink.append((prompt, schema))
        if isinstance(result, Exception):
            raise result
        return result
    return generate


async def test_parts_come_back_in_order() -> None:
    sink: list = []
    subs = await decompose(
        "Who did it, and why?",
        _returning(SubQuestions(sub_questions=["Who did it?", "Why did they do it?"]), sink))
    assert subs == ["Who did it?", "Why did they do it?"]
    (_, schema), = sink
    assert schema is SubQuestions


@pytest.mark.parametrize("bad", [
    SubQuestions(sub_questions=[]),
    SubQuestions(sub_questions=["  ", ""]),
    ProviderOutputError("not json", raw_output="{"),
    GeminiOutputError("not a SubQuestions object", "{"),
], ids=["empty", "blank", "ollama-malformed", "gemini-malformed"])
async def test_malformed_or_empty_output_falls_back_to_the_question(bad) -> None:
    assert await decompose("Who is Holmes?", _returning(bad)) == ["Who is Holmes?"]


async def test_a_result_of_the_wrong_type_falls_back() -> None:
    class Other(BaseModel):
        claims: list[str] = []

    assert await decompose("q?", _returning(Other())) == ["q?"]


async def test_capped_at_four_and_deduplicated() -> None:
    many = [f"Part {i}?" for i in range(7)]
    subs = await decompose("q", _returning(SubQuestions(sub_questions=["Part 0?", *many])))
    assert MAX_SUB_QUESTIONS == 4
    assert subs == ["Part 0?", "Part 1?", "Part 2?", "Part 3?"]


async def test_capacity_errors_are_not_swallowed() -> None:
    # Only malformed OUTPUT falls back; a transport or quota failure is real.
    with pytest.raises(RuntimeError):
        await decompose("q", _returning(RuntimeError("quota")))


def test_the_prompt_states_the_rules_and_names_the_books() -> None:
    prompt = decomposition_prompt("Who is it in each novel?",
                                  ["A Study in Scarlet", "The Sign of the Four"])
    assert "Who is it in each novel?" in prompt
    assert "unchanged" in prompt
    assert "in each novel" in prompt and "in both books" in prompt
    assert "who" in prompt and "why" in prompt
    assert "A Study in Scarlet" in prompt and "The Sign of the Four" in prompt
    assert "1 to 4" in prompt
