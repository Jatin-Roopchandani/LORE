"""Interpretive answers (lore/query/interpret.py): lens parts, the writer's
shape, the guards, the check's tests and the closing lines, the fallbacks, and
what changes (and what does not) with INTERPRETIVE off.

Fake generate callables and invented books and names throughout; no test
reaches a model."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from lore.query import decompose, interpret, synthesize as synth, write as w
from lore.query.decompose import Decomposition, Part, PartSpec, retrieval_parts
from lore.query.interpret import Shape
from lore.query.models import Evidence, ProseSentence
from lore.query.synthesize import (
    Faithfulness, Judgement, Synthesis, _build_prompt, faithfulness_prompt, synthesize_parts,
)
from lore.query.viewpoint import load_viewpoint
from lore.query.write import Prose, Sentence, assemble, compose_text, verify_prose, writer_prompt
from tests.test_query_story import BOOKS, CS, PS, TITLES

VP = load_viewpoint()
TEXTS = [c.text for c in CS]
PLAN = (Part("Was Rin right to leave?", "interpretive", ("What did Rin do?", "How did it end?")),)
SHAPE = Shape(
    viewpoint=VP, parts=("Was Rin right to leave?",), kinds=("interpretive",),
    retrieval=(("What did Rin do?", 0, True), ("How did it end?", 0, True)),
    # claim 1 was found for lens 0, claims 2 and 3 for lens 1
    claim_parts=(frozenset({0}), frozenset({1}), frozenset({1})))


def S(text, claims, kind="fact", new_paragraph=False):
    return Sentence(text=text, claims=claims, kind=kind, new_paragraph=new_paragraph)


def _kinds(sentences):
    return [s.kind for s in sentences]


def _verify(items, shape=SHAPE):
    return verify_prose(Prose(sentences=items), 3, TEXTS, shape)


# ---- the shape --------------------------------------------------------------------

def test_shaped_needs_the_switch_and_an_interpretive_or_beyond_part(monkeypatch):
    assert interpret.shaped(["factual", "interpretive"]) and interpret.shaped(["beyond"])
    assert not interpret.shaped(["factual", "factual"])
    monkeypatch.setattr(decompose, "INTERPRETIVE", False)
    assert not interpret.shaped(["interpretive"])


def test_claims_are_grouped_under_the_first_lens_that_found_them():
    shape = SHAPE.with_claims([frozenset({1}), frozenset({0, 1}), frozenset({1})])
    assert shape.groups() == [("What did Rin do?", [1]), ("How did it end?", [0, 2])]
    assert Shape(VP, ("q",), ("interpretive",), (("l", 0, True),)).groups() == []


def test_a_part_needs_two_lenses_or_all_it_had():
    assert SHAPE.need(0) == 2
    one = Shape(VP, ("q",), ("interpretive",), (("l", 0, True),))
    assert one.need(0) == 1


# ---- the writer -------------------------------------------------------------------

def test_the_writer_prompt_has_the_shape_the_viewpoint_and_the_lens_headings():
    prompt = writer_prompt("Was Rin right to leave?", list(SHAPE.parts), CS, PS, TITLES,
                           books=BOOKS, shape=SHAPE)
    assert interpret.writer_rule(SHAPE) in prompt
    assert VP in prompt  # verbatim
    assert '"reading"' in prompt and '"counter"' in prompt
    assert w._READINGS_RULE not in prompt
    assert "1. Was Rin right to leave? (interpretive" in prompt
    assert prompt.index("Evidence for: What did Rin do?") < prompt.index("Evidence for: How did it end?")
    group_a = prompt.split("Evidence for: What did Rin do?")[1].split("Evidence for:")[0]
    assert "Rin trained with the sword." in group_a and "Rin met the old teacher." not in group_a
    # Within a lens, facts are in story order and the numbering runs on across lenses.
    group_b = prompt.split("Evidence for: How did it end?")[1]
    assert group_b.index("1") >= 0 and "2. Rin met the old teacher." in group_b
    assert "3. Rin learned to read maps." in group_b


def test_a_beyond_part_adds_its_writer_rule():
    shape = Shape(VP, ("Why did the author write it?",), ("beyond",),
                  (("Why did the author write it?", 0, False),), (frozenset({0}),) * 3)
    prompt = writer_prompt("q", list(shape.parts), CS, PS, TITLES, books=BOOKS, shape=shape)
    assert interpret.WRITER_BEYOND_RULE in prompt and interpret.WRITER_SHAPE_RULE.split("{")[0] not in prompt
    assert "(beyond the books)" in prompt and VP in prompt


def test_without_a_shape_the_writer_prompt_is_unchanged():
    prompt = writer_prompt("How did Rin grow?", ["How did Rin grow?"], CS, PS, TITLES, books=BOOKS)
    assert w._READINGS_RULE in prompt and "Evidence for:" not in prompt and VP not in prompt


async def test_the_writers_numbers_are_converted_back_through_the_lens_grouping():
    sent = []

    async def generate(prompt, schema):
        sent.append(schema)
        # shown order: 1 = claim 1 (lens 0), then lens 1 in story order: 2 = claim 2, 3 = claim 3
        return Prose(sentences=[S("A.", [1]), S("B.", [2]), S("C.", [3])])
    shape = SHAPE.with_claims([frozenset({1}), frozenset({0}), frozenset({1})])
    prose = await w.write_answer("q", list(shape.parts), CS, PS, generate, book_titles=TITLES,
                                 books=BOOKS, shape=shape)
    # lens 0 holds claim 2; lens 1 holds claims 1 and 3 (story order: claim 3 comes first)
    assert [s.claims for s in prose.sentences] == [[2], [3], [1]]
    assert sent == [Prose]


async def test_a_counter_kind_is_kept_only_for_an_interpretive_answer():
    async def generate(prompt, schema):
        return Prose(sentences=[S("A.", [1]), S("Against that.", [2], "counter"),
                                S("The books don't settle it.", [2], "limit"),
                                S("Arguably so.", [1, 2], "judgement")])
    on = await w.write_answer("q", list(SHAPE.parts), CS, PS, generate, shape=SHAPE)
    assert _kinds(on.sentences) == ["fact", "counter", "fact", "reading"]  # a writer's limit is a fact
    off = await w.write_answer("q", ["q"], CS, PS, generate)
    assert _kinds(off.sentences) == ["fact", "fact", "fact", "reading"]


# ---- the code guards --------------------------------------------------------------

def test_a_reading_with_wording_and_two_lenses_survives():
    out = _verify([S("Rin trained.", [1]), S("The evidence suggests Rin was committed.", [1, 2], "reading")])
    assert _kinds(out) == ["fact", "reading"]


@pytest.mark.parametrize("text", [
    "On balance, Rin was committed.", "A strong case can be made that Rin was committed.",
    "The evidence is split on Rin.", "Rin arguably stands out."])
def test_interpretive_wording_variants_pass(text):
    assert _kinds(_verify([S("Rin trained.", [1]), S(text, [1, 3], "reading")])) == ["fact", "reading"]


@pytest.mark.parametrize("text, claims", [
    ("Rin was committed.", [1, 2]),                             # plain statement
    ("The evidence suggests it. Rin was committed.", [1, 2]),   # two sentences
    ("The evidence suggests Rin was committed.", [2, 3]),       # both from lens 1
    ("The evidence suggests Rin was committed.", [1]),          # one lens
])
def test_a_reading_that_breaks_a_guard_is_dropped(text, claims):
    assert _kinds(_verify([S("Rin trained.", [1]), S(text, claims, "reading")])) == ["fact"]


def test_readings_and_counters_are_capped_and_follow_the_facts():
    items = [S("Rin trained.", [1]),
             S("Arguably one.", [1, 2], "reading"), S("Against that, a.", [2], "counter"),
             S("Arguably two.", [1, 2], "reading"), S("Arguably three.", [1, 3], "reading"),
             S("Arguably four.", [1, 2], "reading"),
             S("Against that, b.", [3], "counter"), S("Against that, c.", [3], "counter"),
             S("Rin met a teacher.", [2])]
    out = _verify(items)
    assert [s.text for s in out] == [
        "Rin trained.", "Rin met a teacher.", "Arguably one.", "Arguably two.", "Arguably three.",
        "Against that, a.", "Against that, b."]
    assert [s.new_paragraph for s in out if s.kind == "reading"] == [True, False, False]
    assert [s.new_paragraph for s in out if s.kind == "counter"] == [False, False]


def test_a_counter_case_needs_a_citation():
    out = interpret.limit_interpretive(
        [S("Rin trained.", [1]), S("Against that.", [], "counter")], SHAPE)
    assert _kinds(out) == ["fact"]


def test_a_part_with_one_lens_needs_only_that_lens():
    one = Shape(VP, ("q",), ("interpretive",), (("l", 0, True),), (frozenset({0}),) * 3)
    out = verify_prose(Prose(sentences=[S("Rin trained.", [1]),
                                        S("The evidence suggests it.", [1, 2], "reading")]), 3, TEXTS, one)
    assert _kinds(out) == ["fact", "reading"]


def test_a_reading_is_checked_against_one_parts_lenses_not_a_mix():
    shape = Shape(VP, ("p1", "p2"), ("interpretive", "interpretive"),
                  (("a", 0, True), ("b", 0, True), ("c", 1, True), ("d", 1, True)),
                  (frozenset({0}), frozenset({2}), frozenset({1})))
    items = [S("F.", [1]), S("The evidence suggests x.", [1, 2], "reading")]
    assert _kinds(verify_prose(Prose(sentences=items), 3, TEXTS, shape)) == ["fact"]
    items = [S("F.", [1]), S("The evidence suggests x.", [1, 3], "reading")]
    assert _kinds(verify_prose(Prose(sentences=items), 3, TEXTS, shape)) == ["fact", "reading"]


def test_without_a_shape_the_old_reading_guard_applies():
    out = verify_prose(Prose(sentences=[S("Rin trained.", [1]),
                                        S("This suggests commitment.", [2, 3], "reading")]), 3, TEXTS)
    assert _kinds(out) == ["fact", "reading"]


# ---- assemble, fallbacks and the closing lines -------------------------------------

def _prose(*kinds):
    texts = {"fact": "Rin trained.", "reading": "The evidence suggests it.", "counter": "Against that."}
    return tuple(ProseSentence(texts[k], (1,), (1,), False, k) for k in kinds)


def test_a_dropped_reading_says_the_evidence_did_not_support_a_position_and_drops_the_counter():
    out = interpret.close_answer(_prose("fact", "fact", "counter"), SHAPE)
    assert [s.kind for s in out] == ["fact", "fact", "limit"]
    assert out[-1].text == interpret.NO_POSITION_LINE == "The evidence found doesn't support a clear position."
    assert out[-1].claims == () and out[-1].passages == () and out[-1].new_paragraph


def test_a_reading_with_no_counter_says_there_was_no_strong_opposing_reading():
    out = interpret.close_answer(_prose("fact", "reading"), SHAPE)
    assert out[-1].kind == "limit" and out[-1].text == interpret.NO_COUNTER_LINE


def test_a_full_answer_needs_no_closing_line():
    out = interpret.close_answer(_prose("fact", "reading", "counter"), SHAPE)
    assert [s.kind for s in out] == ["fact", "reading", "counter"]
    assert out[1].new_paragraph


def test_limits_come_from_the_beyond_parts_and_the_checked_gaps():
    shape = Shape(VP, ("Was Rin right?", "Why did the author write it?"), ("interpretive", "beyond"),
                  SHAPE.retrieval, SHAPE.claim_parts)
    out = interpret.close_answer(_prose("fact", "reading", "counter"), shape, ["the cause of the quarrel"])
    assert out[-1].text == ("The books don't settle: Why did the author write it?; "
                            "the cause of the quarrel.".replace("it?;", "it?;"))
    assert out[-1].kind == "limit"
    # Gaps alone: a sentence ending in a stop.
    plain = interpret.close_answer(_prose("fact", "reading", "counter"), SHAPE, ["the cause"])
    assert plain[-1].text == "The books don't settle: the cause."
    # Nothing to say, nothing added.
    assert interpret.limit_text(SHAPE) == []


def test_a_beyond_only_answer_adds_only_its_limit_line():
    shape = Shape(VP, ("Why did the author write it?",), ("beyond",), (), ())
    out = interpret.close_answer(_prose("fact", "fact"), shape)
    assert [s.kind for s in out] == ["fact", "fact", "limit"]
    assert out[-1].text.startswith(interpret.LIMITS_LEAD)


def test_prose_of_interpretation_alone_falls_back_and_only_facts_count_toward_the_limit():
    only = [S("The evidence suggests it.", [1, 2], "reading"), S("Against that.", [2], "counter")]
    assert assemble(only, 2, [True] * 3, [True, True], [1, 2, 3]) is None
    # Four facts, one rejected: a reading rejected too does not push it over the fifth.
    items = [S(f"Fact {i}.", [1]) for i in range(1, 6)] + [S("The evidence suggests it.", [1, 2], "reading")]
    out = assemble(items, 6, [True] * 3, [True, True, False, True, True, False], [1, 2, 3])
    assert out is not None and _kinds(out) == ["fact"] * 4
    two_bad = assemble(items, 6, [True] * 3, [True, False, False, True, True, True], [1, 2, 3])
    assert two_bad is None  # two of five facts is past one in five


def test_compose_text_renders_a_limit_without_citations():
    prose = (*_prose("fact", "reading"), ProseSentence("The books don't settle: x.", (), (), True, "limit"))
    text = compose_text(prose)
    assert text.endswith("\n\nThe books don't settle: x.")
    assert "Rin trained. [1]" in text and "The evidence suggests it. [1]" in text


# ---- the check --------------------------------------------------------------------

def _check(sentences, shape=SHAPE):
    return faithfulness_prompt(CS, PS, TITLES, sentences, books=BOOKS, shape=shape)


def test_the_check_labels_a_reading_and_quotes_the_viewpoint_lines_that_apply():
    prompt = _check([S("Rin trained.", [1]), S("The evidence suggests it.", [1, 2], "reading")])
    assert "(reading)" in prompt and "(counter)" not in prompt
    rules = interpret.viewpoint_rules(VP)
    for n in interpret.READING_RULES:
        assert f"{n}. {rules[n]}" in prompt
    assert f"3. {rules[3]}" not in prompt  # the counter-case rule is not in a reading's test
    assert "more certainty than the claims give" in " ".join(prompt.split())


def test_the_check_tests_a_counter_case_with_its_own_rules():
    prompt = _check([S("Rin trained.", [1]), S("Against that.", [2], "counter")])
    rules = interpret.viewpoint_rules(VP)
    assert "(counter)" in prompt and "(reading)" not in prompt
    for n in interpret.COUNTER_RULES:
        assert f"{n}. {rules[n]}" in prompt
    assert "fair reading of the claims it cites" in " ".join(prompt.split())


def test_a_reading_and_a_counter_case_each_get_their_test():
    prompt = _check([S("Rin trained.", [1]), S("The evidence suggests it.", [1, 2], "reading"),
                     S("Against that.", [2], "counter")])
    assert prompt.count("(reading)") >= 2 and prompt.count("(counter)") >= 2


def test_the_check_prompt_of_facts_alone_is_unchanged_by_the_shape():
    facts = [S("Rin trained.", [1])]
    assert _check(facts) == faithfulness_prompt(CS, PS, TITLES, facts, books=BOOKS)


def test_without_a_shape_a_reading_gets_the_old_test_and_no_viewpoint():
    prompt = faithfulness_prompt(CS, PS, TITLES, [S("The evidence suggests it.", [1, 2], "reading")],
                                 books=BOOKS)
    assert "interprets what its cited claims show" in prompt and VP.splitlines()[2][:20] not in prompt


def test_a_viewpoint_without_numbered_rules_is_quoted_whole():
    custom = "Be fair.\nBe brief."
    shape = Shape(custom, SHAPE.parts, SHAPE.kinds, SHAPE.retrieval, SHAPE.claim_parts)
    prompt = _check([S("The evidence suggests it.", [1, 2], "reading")], shape)
    assert "Be fair." in prompt and "Be brief." in prompt


def test_viewpoint_rules_parse_numbered_lines_and_their_continuations():
    text = "Intro.\n\n1. First rule,\n   continued here.\n2. Second.\nTrailer."
    assert interpret.viewpoint_rules(text) == {1: "First rule, continued here.", 2: "Second."}
    assert interpret.viewpoint_rules("no rules") == {}


# ---- the claims step ---------------------------------------------------------------

def test_a_lens_part_is_told_it_gathers_one_side_and_a_beyond_part_what_the_text_says():
    ev = Evidence(edges=(), passages=PS[:3], truncated=False)
    lens = _build_prompt("What did Rin do?", ev, TITLES, part_of="Was Rin right?", books=BOOKS,
                         role="lens")
    assert "gathers evidence for one side of a larger interpretive question: Was Rin right?" in lens
    assert "what characters do, choose, hide and change" in " ".join(lens.split())
    assert "Answer this part fully" not in lens
    beyond = _build_prompt("Why?", ev, TITLES, part_of="Why? and more", books=BOOKS, role="beyond")
    assert "cannot settle" in beyond and "Answer this part fully" not in beyond
    plain = _build_prompt("Who?", ev, TITLES, part_of="Who? and why?", books=BOOKS)
    assert "Answer this part fully" in plain and "interpretive" not in plain


# ---- end to end through synthesize_parts -------------------------------------------

def _j(n, v="supported"):
    return Judgement(claim=n, verdict=v, reason="r")


def _claim(passage, text, quote):
    return {"text": text, "passage": passage, "quote": quote}


class Script:
    """generate, write and check in one: routed by the schema asked for."""

    def __init__(self, lens_claims, prose, sentence_verdicts=None, claim_verdicts=None):
        self.lens_claims, self.prose = lens_claims, prose
        self.sentence_verdicts, self.claim_verdicts = sentence_verdicts, claim_verdicts
        self.prompts = {"claims": [], "write": [], "check": []}

    async def generate(self, prompt, schema):
        if schema is Synthesis:
            self.prompts["claims"].append(prompt)
            for question, claims in self.lens_claims.items():
                if f"Question: {question}" in prompt:
                    return Synthesis(can_answer=True, claims=claims)
            return Synthesis(can_answer=False, claims=[])
        if schema is Prose or schema is w._StoryProse:
            self.prompts["write"].append(prompt)
            if isinstance(self.prose, Exception):
                raise self.prose
            return self.prose
        self.prompts["check"].append(prompt)
        n = self.prompts["check"][-1].count("\n   cites passage")
        verdicts = self.sentence_verdicts or []
        return Faithfulness(
            judgements=[_j(i, (self.claim_verdicts or {}).get(i, "supported")) for i in range(1, n + 1)],
            sentence_judgements=[_j(i, v) for i, v in enumerate(verdicts, start=1)])


LENS_CLAIMS = {
    "What did Rin do?": [_claim(1, "Rin trained with the sword.", "Rin trained with the sword"),
                         _claim(2, "Rin met the old teacher.", "Rin met the old teacher")],
    "How did it end?": [_claim(3, "Rin learned to read maps.", "Rin learned to read maps")]}
# Shown to the writer: lens 1 (claims 1 and 2 of the kept list) in story order, then lens 2.
FULL = Prose(sentences=[
    S("Rin met a teacher.", [1]), S("Rin trained with the sword.", [2]), S("Rin learned maps.", [3]),
    S("The evidence suggests Rin was committed.", [1, 3], "reading"),
    S("Against that, the training came late.", [2], "counter")])
EVIDENCE = Evidence(edges=(), passages=PS[:3], truncated=False)


async def _answer(script, plan=PLAN, *, write=True, check=True, viewpoint=None):
    rparts = retrieval_parts(plan)
    return await synthesize_parts(
        "Was Rin right to leave?", [r.question for r in rparts], EVIDENCE, script.generate,
        mode="graph", book_titles=TITLES, books=BOOKS,
        check=script.generate if check else None, write=script.generate if write else None,
        plan=plan, retrieval=rparts, viewpoint=viewpoint)


async def test_an_interpretive_answer_has_evidence_a_reading_a_counter_case():
    script = Script(LENS_CLAIMS, FULL, ["supported"] * 5)
    a = await _answer(script)
    assert a.answer_style == "prose"
    assert [s.kind for s in a.prose] == ["fact", "fact", "fact", "reading", "counter"]
    assert a.prose[3].new_paragraph and not a.prose[4].new_paragraph
    assert "The evidence suggests Rin was committed." in a.text
    assert len(a.claims) == 3
    # One claims call per lens, over that lens's question, with the lens context.
    assert len(script.prompts["claims"]) == 2
    assert all("one side of a larger interpretive question: Was Rin right to leave?" in p
               for p in script.prompts["claims"])
    # The writer saw the viewpoint, the check quoted its lines.
    assert VP in script.prompts["write"][0]
    assert interpret.viewpoint_rules(VP)[1] in script.prompts["check"][0]
    # The writer answered the plan's part, not the lenses.
    assert "1. Was Rin right to leave? (interpretive" in script.prompts["write"][0]
    assert "2. What did Rin do?" not in script.prompts["write"][0]


async def test_the_reading_dropped_by_the_check_leaves_the_facts_and_a_line_from_code():
    script = Script(LENS_CLAIMS, FULL, ["supported"] * 3 + ["not_supported", "supported"])
    a = await _answer(script)
    assert [s.kind for s in a.prose] == ["fact", "fact", "fact", "limit"]
    assert a.prose[-1].text == interpret.NO_POSITION_LINE
    assert a.text.endswith(interpret.NO_POSITION_LINE)
    assert "Against that" not in a.text  # a counter-case with nothing to oppose is dropped


async def test_a_counter_case_dropped_by_the_check_is_said_to_be_missing():
    script = Script(LENS_CLAIMS, FULL, ["supported"] * 4 + ["not_supported"])
    a = await _answer(script)
    assert [s.kind for s in a.prose] == ["fact", "fact", "fact", "reading", "limit"]
    assert a.prose[-1].text == interpret.NO_COUNTER_LINE


async def test_a_reading_from_one_lens_only_is_dropped_by_code():
    prose = Prose(sentences=[S("Rin met a teacher.", [1]), S("Rin trained.", [2]),
                             S("The evidence suggests Rin was committed.", [1, 2], "reading")])
    script = Script(LENS_CLAIMS, prose, ["supported"] * 2)
    a = await _answer(script)
    assert [s.kind for s in a.prose] == ["fact", "fact", "limit"]
    assert a.prose[-1].text == interpret.NO_POSITION_LINE


async def test_one_failed_fact_does_not_cost_the_prose_but_more_than_a_fifth_does():
    one = await _answer(Script(LENS_CLAIMS, FULL, ["supported", "not_supported", "supported",
                                                   "supported", "supported"]))
    assert one.answer_style == "prose" and [s.kind for s in one.prose].count("fact") == 2
    two = await _answer(Script(LENS_CLAIMS, FULL, ["not_supported", "not_supported", "supported",
                                                   "supported", "supported"]))
    assert two.answer_style == "claims"


async def test_failed_prose_falls_back_to_the_facts_and_says_there_is_no_position():
    a = await _answer(Script(LENS_CLAIMS, FULL, ["not_supported"] * 5))
    assert a.answer_style == "claims" and len(a.claims) == 3
    assert a.note and "did not pass verification" in a.note
    assert a.text.endswith(interpret.NO_POSITION_LINE)
    assert "Rin met the old teacher." in a.text  # the facts are still shown


async def test_a_writer_that_raises_falls_back_to_the_facts_and_the_line():
    a = await _answer(Script(LENS_CLAIMS, RuntimeError("boom")))
    assert a.answer_style == "claims" and a.text.endswith(interpret.NO_POSITION_LINE)


async def test_with_no_writer_configured_the_facts_stand_with_no_claim_about_position():
    a = await _answer(Script(LENS_CLAIMS, FULL), write=False, check=False)
    assert a.answer_style == "claims" and interpret.NO_POSITION_LINE not in a.text


async def test_a_prose_of_only_a_reading_falls_back():
    prose = Prose(sentences=[S("The evidence suggests Rin was committed.", [1, 3], "reading")])
    a = await _answer(Script(LENS_CLAIMS, prose, ["supported"]))
    assert a.answer_style == "claims" and a.text.endswith(interpret.NO_POSITION_LINE)


async def test_the_beyond_part_is_retrieved_once_and_its_limit_is_said_by_code():
    plan = (*PLAN, Part("Why did the author write it?", "beyond"))
    rparts = retrieval_parts(plan)
    assert [(r.question, r.lens) for r in rparts] == [
        ("What did Rin do?", True), ("How did it end?", True), ("Why did the author write it?", False)]
    lens = {**LENS_CLAIMS, "Why did the author write it?": [
        _claim(2, "Rin met the old teacher too.", "Rin met the old teacher")]}
    script = Script(lens, FULL, ["supported"] * 5)
    a = await _answer(script, plan)
    assert len(script.prompts["claims"]) == 3
    beyond_prompt = [p for p in script.prompts["claims"] if "Question: Why did the author" in p][0]
    assert "cannot settle" in beyond_prompt
    assert a.prose[-1].kind == "limit"
    assert a.prose[-1].text == "The books don't settle: Why did the author write it?"
    assert "not found" not in (a.note or "")


async def test_a_beyond_part_with_no_facts_is_not_a_missing_part():
    plan = (*PLAN, Part("Why did the author write it?", "beyond"))
    a = await _answer(Script(LENS_CLAIMS, FULL, ["supported"] * 5), plan)
    assert not a.note or "not found" not in a.note
    assert a.prose[-1].text.startswith(interpret.LIMITS_LEAD)


async def test_a_mixed_question_keeps_its_factual_part_as_facts():
    plan = (Part("Who is Rin?", "factual"), *PLAN)
    lens = {"Who is Rin?": [_claim(1, "Rin trained with the sword.", "Rin trained with the sword")],
            **LENS_CLAIMS}
    script = Script(lens, Prose(sentences=[
        S("Rin trained.", [1]), S("Rin met a teacher.", [2]), S("Rin learned maps.", [3]),
        S("The evidence suggests commitment.", [2, 3], "reading")]), ["supported"] * 4)
    a = await _answer(script, plan)
    assert len(script.prompts["claims"]) == 3
    plain = [p for p in script.prompts["claims"] if "Question: Who is Rin?" in p][0]
    assert "Answer this part fully" in plain
    assert [s.kind for s in a.prose][:3] == ["fact"] * 3 and a.prose[3].kind == "reading"


async def test_a_custom_viewpoint_reaches_the_writer_and_the_check():
    script = Script(LENS_CLAIMS, FULL, ["supported"] * 5)
    await _answer(script, viewpoint="1. Be even-handed.\n2. Be brief.")
    assert "1. Be even-handed." in script.prompts["write"][0]
    assert "1. Be even-handed." in script.prompts["check"][0]
    assert VP not in script.prompts["write"][0]


async def test_with_the_switch_off_nothing_is_shaped(monkeypatch):
    monkeypatch.setattr(decompose, "INTERPRETIVE", False)
    rparts = [decompose.RetrievalPart("What did Rin do?", 0), decompose.RetrievalPart("How did it end?", 0)]
    whole = {"Was Rin right to leave?": [c for cs in LENS_CLAIMS.values() for c in cs]}
    script = Script(whole, Prose(sentences=[S("Rin trained.", [1]), S("Rin met a teacher.", [2])]),
                    ["supported"] * 2)
    off = await synthesize_parts("Was Rin right to leave?", [r.question for r in rparts], EVIDENCE,
                                 script.generate, mode="graph", book_titles=TITLES, books=BOOKS,
                                 check=script.generate, write=script.generate, plan=PLAN, retrieval=rparts)
    assert off.answer_style == "prose" and all(s.kind == "fact" for s in off.prose)
    assert script.prompts["write"] and script.prompts["check"]
    assert all("interpretive" not in p for p in script.prompts["claims"] + script.prompts["write"])
    assert "Evidence for:" not in script.prompts["write"][0]


async def test_a_plan_with_no_interpretive_part_takes_the_old_path():
    plan = (Part("What did Rin do?"), Part("How did it end?"))
    rparts = retrieval_parts(plan)
    script = Script(LENS_CLAIMS, Prose(sentences=[S("Rin trained.", [1]), S("Rin met a teacher.", [2])]),
                    ["supported"] * 2)
    evidence = Evidence(edges=(), passages=PS[:3], truncated=False,
                        shares=((1, 2), (3,)), timelines=((), ()))
    with_plan = await synthesize_parts("q", [r.question for r in rparts], evidence, script.generate,
                                       mode="graph", book_titles=TITLES, books=BOOKS,
                                       check=script.generate, write=script.generate,
                                       plan=plan, retrieval=rparts)
    script2 = Script(LENS_CLAIMS, Prose(sentences=[S("Rin trained.", [1]), S("Rin met a teacher.", [2])]),
                     ["supported"] * 2)
    without = await synthesize_parts("q", [r.question for r in rparts], evidence, script2.generate,
                                     mode="graph", book_titles=TITLES, books=BOOKS,
                                     check=script2.generate, write=script2.generate)
    assert script.prompts == script2.prompts and with_plan == without


# ---- the replaced paths -------------------------------------------------------------

def test_the_old_judgement_machinery_is_gone():
    for name in ("_JUDGEMENT_RULE", "_JUDGEMENTS_BLOCK"):
        assert not hasattr(synth, name) and not hasattr(w, name)
    assert not hasattr(w, "_JUDGE_WORDING")
    # An evaluative question gets no extra claims rule or writer rule on its own wording.
    q = "Who is the most complex character in Alpha Tale?"
    ev = Evidence(edges=(), passages=PS, truncated=False)
    assert _build_prompt(q, ev, TITLES, books=BOOKS) == _build_prompt("Who is Rin?", ev, TITLES, books=BOOKS).replace(
        "Who is Rin?", q)
    assert writer_prompt(q, [q], CS, PS, TITLES, books=BOOKS) == writer_prompt(
        "Who is Rin?", ["Who is Rin?"], CS, PS, TITLES, books=BOOKS).replace("Who is Rin?", q)


def test_the_arm_toggles_interpretive():
    import sys
    sys.path.insert(0, str(Path(__file__).parents[1] / "evals" / "answer_quality"))
    import run_pair
    assert "judgements" not in run_pair.ARM_SETTINGS
    assert run_pair.parse_arm("interpretive=false") == {("lore.query.decompose", "INTERPRETIVE"): False}
    assert run_pair.parse_arm("interpretive=on") == {("lore.query.decompose", "INTERPRETIVE"): True}


# ---- generality ---------------------------------------------------------------------

def test_the_interpretive_code_and_prompts_name_no_character_book_or_genre():
    banned = r"\b(holmes|watson|sherlock|small|hope|drebber|stangerson|sholto|ferrier|tonga|" \
             r"morstan|lestrade|baker street|mormon|agra|crime|crimes|murder|villain|villains|" \
             r"detective|treasure|darcy|bennet|wizard|dragon|magic|kingdom|elf|elves|quest|sword)\b"
    lib = Path(__file__).parents[1] / "lore" / "query"
    for name in ("interpret.py", "judge.py"):
        assert not re.findall(banned, (lib / name).read_text().lower()), name
    texts = "\n".join((interpret.LENS_CONTEXT, interpret.BEYOND_CONTEXT, interpret.WRITER_SHAPE_RULE,
                       interpret.WRITER_BEYOND_RULE, interpret.VIEWPOINT_LEAD,
                       interpret.READING_CHECK_BLOCK, interpret.COUNTER_CHECK_BLOCK,
                       interpret.NO_POSITION_LINE, interpret.NO_COUNTER_LINE, interpret.LIMITS_LEAD))
    assert not re.findall(banned, texts.lower())


def test_the_new_prompt_texts_are_in_the_banned_word_and_leak_tests():
    from tests.test_query_synthesize import _new_prompt_texts
    covered = _new_prompt_texts()
    for text in (interpret.LENS_CONTEXT, interpret.BEYOND_CONTEXT, interpret.WRITER_SHAPE_RULE,
                 interpret.WRITER_BEYOND_RULE, interpret.READING_CHECK_BLOCK, interpret.COUNTER_CHECK_BLOCK):
        assert text in covered
