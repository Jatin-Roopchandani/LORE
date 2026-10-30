"""Question decomposition: one structured call, and a safe fallback."""
import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from lore.inference.base import ProviderOutputError
from lore.inference.gemini import GeminiOutputError
from lore.query import decompose as decompose_module
from lore.query.decompose import (
    MAX_LENSES,
    MAX_PARTS,
    MAX_SUB_QUESTIONS,
    Decomposition,
    Part,
    PartSpec,
    SubQuestions,
    decompose,
    decomposition_prompt,
    plan,
    resolve_depth,
    retrieval_parts,
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
    assert schema is Decomposition  # SubQuestions only with both switches off


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
    assert "each novel" in prompt and "both books" in prompt
    assert "who" in prompt and "why" in prompt
    assert "A Study in Scarlet" in prompt and "The Sign of the Four" in prompt
    assert "1 to 4" in prompt


# --- Task 10e: split per book only when the question asks about each book ---

def test_the_prompt_splits_per_book_only_when_asked_about_each_or_both() -> None:
    prompt = decomposition_prompt("How is A connected to B?",
                                  ["A Study in Scarlet", "The Sign of the Four"])
    lowered = " ".join(prompt.lower().split())
    assert "only when the question explicitly asks about each book" in lowered
    for phrase in ("each book", "both books", "each novel", "both novels"):
        assert phrase in lowered
    assert "otherwise keep the question's own parts" in lowered
    assert "do not split a question by book" in lowered
    # The book list stays, but only as a reference for names.
    assert "reference for names only" in lowered
    assert "A Study in Scarlet" in prompt and "The Sign of the Four" in prompt


def test_no_book_list_means_no_reference_line() -> None:
    assert "reference for names" not in decomposition_prompt("q").lower()


async def test_split_reports_whether_the_model_output_was_usable() -> None:
    from lore.query.decompose import split

    assert await split("q", _returning(SubQuestions(sub_questions=["a?", "b?"]))) == ["a?", "b?"]
    assert await split("q", _returning(SubQuestions(sub_questions=["q"]))) == ["q"]
    for bad in (SubQuestions(sub_questions=[" "]), GeminiOutputError("x", "{"),
                ProviderOutputError("x", raw_output="{")):
        assert await split("q", _returning(bad)) is None
    with pytest.raises(RuntimeError):
        await split("q", _returning(RuntimeError("quota")))


# --- deep and interpretive answers, Task 1 -----------------------------------

GOLDEN_OFF = json.loads((Path(__file__).parent / "golden" / "decompose_off.json").read_text())


@pytest.fixture
def switches_off(monkeypatch):
    monkeypatch.setattr(decompose_module, "INTERPRETIVE", False)
    monkeypatch.setattr(decompose_module, "DEEP", False)


def _decomposition(*parts, depth="focused"):
    return Decomposition(sub_questions=list(parts), depth=depth)


def test_switches_default_on() -> None:
    assert decompose_module.INTERPRETIVE is True and decompose_module.DEEP is True
    assert MAX_PARTS == 6 and MAX_LENSES == 4


def test_golden_off_prompt_and_schema_are_what_they_were(switches_off) -> None:
    """tests/golden/decompose_off.json was dumped from 689ea77, before kinds,
    lenses and depth existed."""
    assert decomposition_prompt("Who did it, and why?") == GOLDEN_OFF["prompt_plain"]
    assert decomposition_prompt("How is A connected to B?", ["Alpha Tale", "Beta Tale"]) \
        == GOLDEN_OFF["prompt_books"]
    assert decompose_module.active_schema() is SubQuestions
    assert SubQuestions.model_json_schema() == GOLDEN_OFF["schema"]


async def test_off_ignores_kinds_lenses_and_depth(switches_off) -> None:
    sink: list = []
    got = await plan("q", _returning(_decomposition(
        PartSpec(question="Why?", kind="interpretive", lenses=["a?"]), depth="deep"), sink))
    assert sink[0][1] is SubQuestions
    assert got == decompose_module.Plan((Part("Why?"),), "focused")


def test_each_switch_adds_only_its_own_rules(monkeypatch) -> None:
    monkeypatch.setattr(decompose_module, "DEEP", False)
    only_kinds = decomposition_prompt("q?")
    assert '"lenses"' in only_kinds and '"depth"' not in only_kinds
    monkeypatch.setattr(decompose_module, "INTERPRETIVE", False)
    monkeypatch.setattr(decompose_module, "DEEP", True)
    only_depth = decomposition_prompt("q?")
    assert '"depth"' in only_depth and '"lenses"' not in only_depth
    assert decompose_module.active_schema() is Decomposition


def test_the_prompt_states_the_kinds_lens_rules_and_depth() -> None:
    prompt = " ".join(decomposition_prompt("q?").split())
    for kind in ("factual", "interpretive", "beyond"):
        assert f'"{kind}"' in prompt
    assert "When unsure, use \"factual\"" in prompt
    assert "up to 4 aspects" in prompt
    assert "cover opposing sides" in prompt
    assert "No lens may assume or hint at the answer" in prompt
    assert '"deep"' in prompt and '"focused"' in prompt
    assert "many entities or much of the story" in prompt


async def test_kinds_lenses_and_depth_are_read() -> None:
    got = await plan("q", _returning(_decomposition(
        PartSpec(question="Who did it?"),
        PartSpec(question="Was it right?", kind="interpretive",
                 lenses=["What harm came of it?", "What reasons were given?"]),
        PartSpec(question="What did the author mean?", kind="beyond"),
        depth="deep")))
    assert got.depth == "deep"
    assert got.parts == (
        Part("Who did it?", "factual", ()),
        Part("Was it right?", "interpretive", ("What harm came of it?", "What reasons were given?")),
        Part("What did the author mean?", "beyond", ()))
    assert got.questions == ["Who did it?", "Was it right?", "What did the author mean?"]


async def test_a_legacy_string_list_is_all_factual_and_focused() -> None:
    got = await plan("q", _returning(SubQuestions(sub_questions=["a?", "b?"])))
    assert got == decompose_module.Plan((Part("a?"), Part("b?")), "focused")


@pytest.mark.parametrize("kind", ["", "Opinion", "INTERPRETIVE ", None, 3])
async def test_a_bad_kind_falls_back_to_factual(kind) -> None:
    raw = {"sub_questions": [{"question": "q?", "kind": kind, "lenses": ["l?"]}], "depth": "deep"}
    got = await plan("q", _returning(Decomposition.model_validate(raw)))
    expected = "interpretive" if kind == "INTERPRETIVE " else "factual"
    assert got.parts[0].kind == expected
    if expected == "factual":
        assert got.parts[0].lenses == ()


@pytest.mark.parametrize("depth", ["", "very deep", None, 7])
async def test_a_bad_depth_falls_back_to_focused(depth) -> None:
    raw = {"sub_questions": [{"question": "q?"}], "depth": depth}
    assert (await plan("q", _returning(Decomposition.model_validate(raw)))).depth == "focused"


async def test_malformed_fields_do_not_fail_the_decomposition() -> None:
    raw = {"sub_questions": ["bare?", {"question": "x?", "kind": "interpretive",
                                       "lenses": "not a list"},
                             {"question": "y?", "kind": "interpretive", "lenses": ["l?", 4, None]}]}
    got = await plan("q", _returning(Decomposition.model_validate(raw)))
    assert got.parts == (Part("bare?"), Part("x?"), Part("y?", "interpretive", ("l?",)))


async def test_an_interpretive_part_without_lenses_is_answered_as_factual() -> None:
    got = await plan("q", _returning(_decomposition(
        PartSpec(question="Why?", kind="interpretive", lenses=[" ", "Why?"]))))
    assert got.parts == (Part("Why?", "factual", ()),)


async def test_lenses_are_capped_cleaned_and_deduplicated() -> None:
    lenses = ["L0?", "  L0?  ", "", *[f"L{i}?" for i in range(1, 8)]]
    got = await plan("q", _returning(_decomposition(
        PartSpec(question="Why?", kind="interpretive", lenses=lenses))))
    assert got.parts[0].lenses == ("L0?", "L1?", "L2?", "L3?")


async def test_a_beyond_or_factual_part_keeps_no_lenses() -> None:
    got = await plan("q", _returning(_decomposition(
        PartSpec(question="a?", kind="beyond", lenses=["l?"]),
        PartSpec(question="b?", kind="factual", lenses=["l?"]))))
    assert [p.lenses for p in got.parts] == [(), ()]


async def test_parts_still_capped_at_four_and_a_blank_part_is_dropped() -> None:
    got = await plan("q", _returning(_decomposition(
        PartSpec(question=" "), *[PartSpec(question=f"P{i}?") for i in range(6)])))
    assert got.questions == ["P0?", "P1?", "P2?", "P3?"]
    assert await plan("q", _returning(_decomposition(PartSpec(question=" ")))) is None


def test_the_schema_reaches_the_model_as_plain_strings() -> None:
    """Gemini sees the JSON schema: kind and depth are strings (a bad value is
    ours to repair, not a validation failure), the lenses a list of strings."""
    props = Decomposition.model_json_schema()["$defs"]["PartSpec"]["properties"]
    assert props["kind"]["type"] == "string" and props["question"]["type"] == "string"
    assert props["lenses"]["items"] == {"type": "string"}
    assert Decomposition.model_json_schema()["properties"]["depth"]["type"] == "string"


# --- depth ---------------------------------------------------------------------

def test_depth_override_beats_the_plan_and_auto_uses_it() -> None:
    assert resolve_depth("auto", "deep") == "deep"
    assert resolve_depth(None, "focused") == "focused"
    assert resolve_depth("deep", "focused") == "deep"
    assert resolve_depth("focused", "deep") == "focused"


def test_depth_is_always_focused_with_deep_off(monkeypatch) -> None:
    monkeypatch.setattr(decompose_module, "DEEP", False)
    assert resolve_depth("deep", "deep") == "focused"


# --- the part budget -------------------------------------------------------------

def _interp(name, n):
    return Part(name, "interpretive", tuple(f"{name} lens {i}?" for i in range(n)))


def test_an_interpretive_part_retrieves_by_its_lenses_not_itself() -> None:
    got = retrieval_parts([Part("Who?"), _interp("Why", 2), Part("Beyond?", "beyond")])
    assert [(r.question, r.part, r.lens) for r in got] == [
        ("Who?", 0, False), ("Why lens 0?", 1, True), ("Why lens 1?", 1, True),
        ("Beyond?", 2, False)]


def test_over_budget_trims_lenses_from_the_widest_part_first() -> None:
    got = retrieval_parts([_interp("A", 4), _interp("B", 3), Part("C?")])
    assert len(got) == MAX_PARTS
    counts = {i: sum(r.part == i for r in got) for i in (0, 1, 2)}
    assert counts == {0: 3, 1: 2, 2: 1}
    # the dropped lenses are the later ones
    assert [r.question for r in got if r.part == 0] == ["A lens 0?", "A lens 1?", "A lens 2?"]


def test_trimming_keeps_at_least_one_lens_per_part_and_never_drops_parts() -> None:
    parts = [_interp(n, 4) for n in "ABCD"]
    got = retrieval_parts(parts)
    assert len(got) == 6 and {r.part for r in got} == {0, 1, 2, 3}
    assert retrieval_parts(parts, max_parts=2) and \
        len(retrieval_parts(parts, max_parts=2)) == 4  # one lens each, parts kept


def test_a_repeated_retrieval_question_is_kept_once() -> None:
    got = retrieval_parts([Part("Same?"), Part("Same?")])
    assert [r.question for r in got] == ["Same?"]


def test_under_budget_nothing_is_trimmed() -> None:
    assert len(retrieval_parts([_interp("A", 4), Part("B?")])) == 5
