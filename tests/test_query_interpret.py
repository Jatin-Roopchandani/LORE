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
PLAN = (Part("Was Rin right to leave?", "interpretive", ("What did Rin do?", "How did it end?"),
             stance="verdict"),)
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


@pytest.mark.parametrize("text", [
    "Rin kept the letters, as shown in the third chapter.",     # a participle states, it does not interpret
    "Rin trained with both of these.", "Rin spoke of them."])   # a pronoun phrase is no hedge
def test_wording_that_only_states_or_points_is_not_interpretive(text):
    assert not interpret.INTERPRETIVE_WORDING.search(text)
    assert _kinds(_verify([S("Rin trained.", [1]), S(text, [1, 3], "reading")])) == ["fact"]


@pytest.mark.parametrize("text", ["The record shows Rin was committed.",
                                  "Rin's habits suggest a quiet pride.", "It seems Rin was ready."])
def test_real_hedges_still_count(text):
    assert interpret.INTERPRETIVE_WORDING.search(text)


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
             S("Against that, b.", [3], "counter"), S("Against that, c.", [3], "counter"),
             S("Against that, d.", [3], "counter"),
             S("Rin met a teacher.", [2])]
    out = _verify(items)
    # One interpretive part: a reading and one spare (the check may drop the first),
    # two counter-cases.
    assert [s.text for s in out] == [
        "Rin trained.", "Rin met a teacher.", "Arguably one.", "Arguably two.",
        "Against that, a.", "Against that, b."]


def test_each_interpretive_part_has_room_for_its_own_readings():
    shape = Shape(VP, ("p1", "p2"), ("interpretive", "interpretive"),
                  (("a", 0, True), ("b", 0, True), ("c", 1, True), ("d", 1, True)),
                  (frozenset({0}), frozenset({1}), frozenset({2}), frozenset({3})))
    items = [S("F.", [1]), S("Arguably a.", [1, 2], "reading"), S("Arguably b.", [1, 2], "reading"),
             S("Arguably c.", [1, 2], "reading"),                      # part 0 is full
             S("Arguably d.", [3, 4], "reading")]
    out = verify_prose(Prose(sentences=items), 4, TEXTS + ["x"], shape)
    assert [s.text for s in out if s.kind == "reading"] == ["Arguably a.", "Arguably b.", "Arguably d."]


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
    # A reading cites facts from both lenses (claims 1 and 2).
    return tuple(ProseSentence(texts[k], (1, 2) if k == "reading" else (1,), (1,), False, k)
                 for k in kinds)


def _split(prose):
    return [s for s in prose if s.short], [s for s in prose if not s.short]


def test_a_dropped_reading_is_a_short_answer_line_from_code_and_the_counter_goes():
    out = interpret.close_answer(_prose("fact", "fact", "counter"), SHAPE)
    short, body = _split(out)
    assert [s.kind for s in short] == ["limit"]
    assert short[0].text == interpret.NO_VERDICT_LINE == "The evidence found isn't strong enough for a clear verdict on this."
    assert short[0].claims == () and short[0].passages == () and short[0].new_paragraph
    assert [s.kind for s in body] == ["fact", "fact"]  # a counter-case with nothing to oppose is dropped
    assert out[0].short  # the short answer comes first


def test_the_reading_is_the_short_answer_and_a_missing_counter_is_said_in_the_argument():
    out = interpret.close_answer(_prose("fact", "reading"), SHAPE)
    short, body = _split(out)
    assert [(s.kind, s.text) for s in short] == [("reading", "The evidence suggests it.")]
    assert short[0].new_paragraph
    assert [s.kind for s in body] == ["fact", "limit"] and body[-1].text == interpret.NO_COUNTER_LINE


def test_a_full_answer_needs_no_closing_line():
    out = interpret.close_answer(_prose("fact", "reading", "counter"), SHAPE)
    assert [(s.kind, s.short) for s in out] == [("reading", True), ("fact", False), ("counter", False)]
    assert out[2].new_paragraph


def test_limits_come_from_the_beyond_parts_and_the_checked_gaps():
    shape = Shape(VP, ("Was Rin right?", "Why did the author write it?"), ("interpretive", "beyond"),
                  SHAPE.retrieval, SHAPE.claim_parts)
    out = interpret.close_answer(_prose("fact", "reading", "counter"), shape, ["the cause of the quarrel"])
    assert out[-1].text == "The books don't settle: Why did the author write it?; the cause of the quarrel."
    assert out[-1].kind == "limit" and not out[-1].short
    # Gaps alone: a sentence ending in a stop.
    plain = interpret.close_answer(_prose("fact", "reading", "counter"), SHAPE, ["the cause"])
    assert plain[-1].text == "The books don't settle: the cause."
    # Nothing to say, nothing added.
    assert interpret.limit_text(SHAPE) == []


def test_a_beyond_only_answer_adds_only_its_limit_line_and_no_short_answer():
    shape = Shape(VP, ("Why did the author write it?",), ("beyond",), (), ())
    out = interpret.close_answer(_prose("fact", "fact"), shape)
    assert [s.kind for s in out] == ["fact", "fact", "limit"] and not any(s.short for s in out)
    assert out[-1].text.startswith(interpret.LIMITS_LEAD)


TWO = Shape(VP, ("In book A, who is bravest?", "In book B, who is bravest?"),
            ("interpretive", "interpretive"),
            (("a1", 0, True), ("a2", 0, True), ("b1", 1, True), ("b2", 1, True)),
            (frozenset({0}), frozenset({1}), frozenset({2}), frozenset({3})))


def _reading(text, claims):
    return ProseSentence(text, tuple(claims), (1,), False, "reading")


def test_each_interpretive_part_gets_its_own_reading_in_the_short_answer():
    prose = (ProseSentence("A fact.", (1,), (1,)),
             _reading("On balance, B's Tam is bravest.", [3, 4]),
             _reading("On balance, A's Rin is bravest.", [1, 2]))
    short, _ = _split(interpret.close_answer(prose, TWO))
    # In the order of the parts, whatever order the writer used.
    assert [s.text for s in short] == ["On balance, A's Rin is bravest.", "On balance, B's Tam is bravest."]
    assert all(s.kind == "reading" for s in short)


def test_a_part_whose_reading_failed_gets_the_line_naming_it_when_there_are_several():
    prose = (ProseSentence("A fact.", (1,), (1,)), _reading("On balance, A's Rin is bravest.", [1, 2]))
    short, _ = _split(interpret.close_answer(prose, TWO))
    assert short[0].kind == "reading"
    assert short[1].kind == "limit"
    assert short[1].text == interpret.NO_VERDICT_LINE and short[1].label == "In book B, who is bravest"


def test_readings_are_placed_by_the_survivors_numbering():
    # The check dropped claim 1: survivors 1..3 are the original claims 2, 3, 4.
    prose = (ProseSentence("A fact.", (1,), (1,)), _reading("On balance, B is bravest.", [2, 3]))
    short, _ = _split(interpret.close_answer(prose, TWO, originals=[2, 3, 4]))
    assert short[0].kind == "limit"  # originals 3 and 4 are part 1's lenses
    assert short[1].text == "On balance, B is bravest."


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
    # Only facts that bear on the lens and on the larger question stay.
    flat = " ".join(lens.split())
    assert "Keep only facts that bear on this aspect and on the larger question" in flat
    assert "motives, choices, contradictions, change" in flat and "Skip plot detail" in flat
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


async def test_an_interpretive_answer_opens_with_its_short_answer_then_the_argument():
    script = Script(LENS_CLAIMS, FULL, ["supported"] * 5)
    a = await _answer(script)
    assert a.answer_style == "prose"
    # The reading is the short answer; the argument below is the facts and the counter-case.
    assert [(s.kind, s.short) for s in a.short_answer] == [("reading", True)]
    assert a.short_answer[0].text == "The evidence suggests Rin was committed."
    assert [s.kind for s in a.prose] == ["fact", "fact", "fact", "counter"]
    assert not any(s.short for s in a.prose)
    assert a.prose[3].new_paragraph
    # The text puts the short answer first, under its heading, with its citations.
    assert a.text.startswith(f"{w.SHORT_ANSWER_HEADING}\nThe evidence suggests Rin was committed. [")
    assert a.text.index("The evidence suggests") < a.text.index("Rin met a teacher.")
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


async def test_the_reading_dropped_by_the_check_leaves_the_facts_and_a_short_answer_line_from_code():
    script = Script(LENS_CLAIMS, FULL, ["supported"] * 3 + ["not_supported", "supported"])
    a = await _answer(script)
    assert [s.kind for s in a.prose] == ["fact", "fact", "fact"]
    assert [(s.kind, s.text) for s in a.short_answer] == [("limit", interpret.NO_VERDICT_LINE)]
    assert a.text.startswith(f"{w.SHORT_ANSWER_HEADING}\n{interpret.NO_VERDICT_LINE}")
    assert "Against that" not in a.text  # a counter-case with nothing to oppose is dropped


async def test_a_counter_case_dropped_by_the_check_is_said_to_be_missing():
    script = Script(LENS_CLAIMS, FULL, ["supported"] * 4 + ["not_supported"])
    a = await _answer(script)
    assert [s.kind for s in a.short_answer] == ["reading"]
    assert [s.kind for s in a.prose] == ["fact", "fact", "fact", "limit"]
    assert a.prose[-1].text == interpret.NO_COUNTER_LINE


async def test_a_reading_from_one_lens_only_is_dropped_by_code():
    prose = Prose(sentences=[S("Rin met a teacher.", [1]), S("Rin trained.", [2]),
                             S("The evidence suggests Rin was committed.", [1, 2], "reading")])
    script = Script(LENS_CLAIMS, prose, ["supported"] * 2)
    a = await _answer(script)
    assert [s.kind for s in a.prose] == ["fact", "fact"]
    assert [s.text for s in a.short_answer] == [interpret.NO_VERDICT_LINE]


async def test_one_failed_fact_does_not_cost_the_prose_but_more_than_a_fifth_does():
    one = await _answer(Script(LENS_CLAIMS, FULL, ["supported", "not_supported", "supported",
                                                   "supported", "supported"]))
    assert one.answer_style == "prose" and [s.kind for s in one.prose].count("fact") == 2
    two = await _answer(Script(LENS_CLAIMS, FULL, ["not_supported", "not_supported", "supported",
                                                   "supported", "supported"]))
    assert two.answer_style == "claims"


async def test_failed_prose_falls_back_to_the_facts_and_a_short_answer_says_there_is_no_position():
    a = await _answer(Script(LENS_CLAIMS, FULL, ["not_supported"] * 5))
    assert a.answer_style == "claims" and len(a.claims) == 3
    assert a.note and "did not pass verification" in a.note
    assert [s.text for s in a.short_answer] == [interpret.NO_VERDICT_LINE]
    assert a.text.startswith(f"{w.SHORT_ANSWER_HEADING}\n{interpret.NO_VERDICT_LINE}")
    assert "Rin met the old teacher." in a.text  # the facts are still shown


async def test_a_writer_that_raises_falls_back_to_the_facts_and_the_short_line():
    a = await _answer(Script(LENS_CLAIMS, RuntimeError("boom")))
    assert a.answer_style == "claims"
    assert [s.text for s in a.short_answer] == [interpret.NO_VERDICT_LINE]
    assert a.text.startswith(w.SHORT_ANSWER_HEADING)


async def test_with_no_writer_configured_the_facts_stand_with_no_claim_about_position():
    a = await _answer(Script(LENS_CLAIMS, FULL), write=False, check=False)
    assert a.answer_style == "claims" and interpret.NO_VERDICT_LINE not in a.text
    assert a.short_answer == ()


async def test_a_prose_of_only_a_reading_falls_back():
    prose = Prose(sentences=[S("The evidence suggests Rin was committed.", [1, 3], "reading")])
    a = await _answer(Script(LENS_CLAIMS, prose, ["supported"]))
    assert a.answer_style == "claims"
    assert [s.text for s in a.short_answer] == [interpret.NO_VERDICT_LINE]


async def test_every_interpretive_part_gets_a_verdict_when_the_parts_are_per_book():
    plan = (Part("In book A, who is boldest?", "interpretive", ("What did Rin do?", "How did it end?")),
            Part("In book B, who is boldest?", "interpretive", ("What did Tam do?", "How did Tam end?")))
    lens = {**LENS_CLAIMS,
            "What did Tam do?": [_claim(1, "Tam sailed.", "Rin trained with the sword")],
            "How did Tam end?": [_claim(2, "Tam returned.", "Rin met the old teacher")]}
    prose = Prose(sentences=[
        S("Rin met a teacher.", [1]), S("Rin trained with the sword.", [2]), S("Rin learned maps.", [3]),
        S("In book A, the evidence suggests Rin is boldest.", [1, 3], "reading"),
        S("In book B, on balance Tam is boldest.", [4, 5], "reading")])
    script = Script(lens, prose, ["supported"] * 5)
    a = await _answer(script, plan)
    assert [s.kind for s in a.short_answer] == ["reading", "reading"]
    assert [s.text for s in a.short_answer] == [
        "In book A, the evidence suggests Rin is boldest.", "In book B, on balance Tam is boldest."]
    # The writer is told to write one reading for each such part.
    assert "EACH part marked (interpretive)" in script.prompts["write"][0]


async def test_a_part_without_a_reading_gets_the_line_that_names_it_beside_a_part_that_has_one():
    plan = (Part("In book A, who is boldest?", "interpretive", ("What did Rin do?", "How did it end?"), stance="verdict"),
            Part("In book B, who is boldest?", "interpretive", ("What did Tam do?", "How did Tam end?"), stance="verdict"))
    lens = {**LENS_CLAIMS,
            "What did Tam do?": [_claim(1, "Tam sailed.", "Rin trained with the sword")],
            "How did Tam end?": [_claim(2, "Tam returned.", "Rin met the old teacher")]}
    prose = Prose(sentences=[
        S("Rin met a teacher.", [1]), S("Rin learned maps.", [3]), S("Tam returned.", [4]),
        S("In book A, the evidence suggests Rin is boldest.", [1, 3], "reading")])
    a = await _answer(Script(lens, prose, ["supported"] * 4), plan)
    assert [s.kind for s in a.short_answer] == ["reading", "limit"]
    assert a.short_answer[1].text == interpret.NO_VERDICT_LINE
    assert [s.label for s in a.short_answer] == ["In book A, who is boldest", "In book B, who is boldest"]


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
    assert [s.kind for s in a.short_answer] == ["reading"]
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
    assert [s.kind for s in a.prose][:3] == ["fact"] * 3
    assert [s.kind for s in a.short_answer] == ["reading"]


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
                       interpret.NO_VERDICT_LINE, interpret.NO_COUNTER_LINE, interpret.LIMITS_LEAD))
    assert not re.findall(banned, texts.lower())


def test_the_new_prompt_texts_are_in_the_banned_word_and_leak_tests():
    from tests.test_query_synthesize import _new_prompt_texts
    covered = _new_prompt_texts()
    for text in (interpret.LENS_CONTEXT, interpret.BEYOND_CONTEXT, interpret.WRITER_SHAPE_RULE,
                 interpret.WRITER_BEYOND_RULE, interpret.READING_CHECK_BLOCK, interpret.COUNTER_CHECK_BLOCK):
        assert text in covered


def test_the_writer_names_the_chosen_candidate_for_a_judgement_and_rests_it_on_the_quality_asked_about():
    flat = " ".join(interpret.writer_rule(SHAPE).split())
    assert "name the candidate you choose and say why" in flat
    assert "not on plot events that happen around them" in flat
    assert "for EACH part marked (interpretive)" in flat and "every one needs its own reading" in flat


def test_compose_answer_puts_the_short_answer_first_under_its_heading():
    short = (ProseSentence("On balance, A.", (1,), (1,), True, "reading", True),
             ProseSentence("B? " + interpret.NO_VERDICT_LINE, (), (), True, "limit", True))
    body = (ProseSentence("A fact.", (2,), (2,), False),)
    text = w.compose_answer(short, body)
    assert text == (f"{w.SHORT_ANSWER_HEADING}\nOn balance, A. [1]\n\nB? {interpret.NO_VERDICT_LINE}"
                    "\n\nA fact. [2]")
    assert w.compose_answer((), body) == w.compose_text(body)


# ---- the short answer: labels, fact summaries, stance (2026-11-02) ---------------------

def _two(stances=("verdict", "verdict"), labels=("", "")):
    return Shape(TWO.viewpoint, TWO.parts, TWO.kinds, TWO.retrieval, TWO.claim_parts, stances, labels)


def _summary(text, claims):
    return ProseSentence(text, tuple(claims), (1,), False, "summary")


def test_stance_parsing_is_tolerant_and_defaults_to_analysis():
    for raw, want in (("verdict", "verdict"), (" Verdict ", "verdict"), ("analysis", "analysis"),
                      ("junk", "analysis"), (None, "analysis"), (3, "analysis")):
        part = decompose._part(PartSpec(question="q", kind="interpretive", stance=raw,
                                        lenses=[{"question": "l"}]))
        assert part.stance == want
    bare = decompose._part(PartSpec.model_validate({"question": "q", "kind": "interpretive",
                                                    "lenses": ["l"]}))
    assert bare.stance == "analysis"
    assert decompose._part(PartSpec(question="q", kind="interpretive", label="Short name",
                                    lenses=[{"question": "l"}])).label == "Short name"


def test_the_decomposer_prompt_gives_a_general_stance_rule():
    flat = " ".join(decompose.decomposition_prompt("q?").split())
    assert '"stance"' in flat and '"verdict"' in flat and '"analysis"' in flat


def test_a_multi_part_short_answer_carries_each_parts_label_and_never_its_question():
    shape = _two(labels=("Book A", ""))
    prose = (ProseSentence("A fact.", (1,), (1,)),
             _reading("On balance, A's Rin is bravest.", [1, 2]),
             _reading("On balance, B's Tam is bravest.", [3, 4]))
    short, _ = _split(interpret.close_answer(prose, shape))
    assert [s.label for s in short] == ["Book A", "In book B, who is bravest"]  # derived with lens_heading
    for s in short:
        assert "who is bravest?" not in s.text
    assert w.compose_text(short).startswith("Book A: On balance, A's Rin")


def test_a_single_part_short_answer_has_no_label():
    short, _ = _split(interpret.close_answer(_prose("fact", "reading"), SHAPE))
    assert [s.label for s in short] == [""]


def test_the_no_verdict_line_names_no_question_and_is_for_verdict_parts_only():
    prose = (ProseSentence("A fact.", (1,), (1,)), _reading("On balance, A's Rin is bravest.", [1, 2]))
    short, _ = _split(interpret.close_answer(prose, _two(("verdict", "verdict"))))
    assert short[1].kind == "limit" and short[1].text == interpret.NO_VERDICT_LINE
    assert short[1].text == "The evidence found isn't strong enough for a clear verdict on this."
    assert short[1].label == "In book B, who is bravest"
    short, _ = _split(interpret.close_answer(prose, _two(("verdict", "analysis"))))
    assert [s.label for s in short] == ["In book A, who is bravest"]
    assert not any(s.kind == "limit" for s in short)  # analysis part: no line, nothing to say


def test_a_fact_summary_is_the_short_answer_when_the_reading_is_missing():
    prose = (ProseSentence("A fact.", (1,), (1,)),
             _summary("Rin trained and learned maps.", [1, 2]),
             _summary("Tam sailed home.", [3, 4]))
    out = interpret.close_answer(prose, _two(("verdict", "analysis")))
    short, body = _split(out)
    assert [(s.kind, s.text, s.label) for s in short] == [
        ("fact", "Rin trained and learned maps.", "In book A, who is bravest"),
        ("fact", "Tam sailed home.", "In book B, who is bravest")]
    assert all(s.short and s.claims for s in short) and all(s.new_paragraph for s in short)
    assert [s.kind for s in body] == ["fact"]  # summaries are not repeated in the argument
    assert not any(s.text == interpret.NO_VERDICT_LINE for s in out)


def test_a_reading_wins_over_a_summary_and_at_most_two_summaries_stand():
    prose = (_summary("One.", [1]), _summary("Two.", [1, 2]), _summary("Three.", [2]),
             _reading("The evidence suggests it.", [1, 3]))
    short, _ = _split(interpret.close_answer((ProseSentence("F.", (1,), (1,)), *prose), SHAPE))
    assert [s.kind for s in short] == ["reading"]
    short, _ = _split(interpret.close_answer(prose[:3], SHAPE))
    assert [s.text for s in short] == ["One.", "Two."]


def test_analysis_parts_with_nothing_surviving_get_no_line_at_all():
    shape = Shape(VP, SHAPE.parts, SHAPE.kinds, SHAPE.retrieval, SHAPE.claim_parts, ("analysis",), ("",))
    out = interpret.close_answer(_prose("fact", "fact"), shape)
    assert not any(s.short for s in out)
    assert not any(s.text == interpret.NO_VERDICT_LINE for s in out)


def test_summaries_are_limited_by_the_guards():
    nothing = _verify([S("Rin met a teacher.", [1]), S("Nothing cited.", [], "summary")])
    assert _kinds(nothing) == ["fact"]
    many = _verify([S("Rin met a teacher.", [1]), S("A.", [1], "summary"), S("B.", [2], "summary"),
                    S("C.", [3], "summary")])
    assert _kinds(many) == ["fact", "summary", "summary"]


def test_the_writer_is_asked_for_a_summary_per_part_and_a_stray_summary_is_a_fact_without_a_shape():
    assert '"summary"' in interpret.writer_rule(SHAPE)
    assert w._kind(S("x", [1], "summary"), SHAPE) == "summary"
    assert w._kind(S("x", [1], "summary"), None) == "fact"


async def test_the_dropped_reading_leaves_a_checked_summary_as_the_short_answer():
    prose = Prose(sentences=[
        S("Rin met a teacher.", [1]), S("Rin trained with the sword.", [2]), S("Rin learned maps.", [3]),
        S("Rin trained, met a teacher and learned maps.", [1, 2, 3], "summary"),
        S("The evidence suggests Rin was committed.", [1, 3], "reading")])
    a = await _answer(Script(LENS_CLAIMS, prose, ["supported"] * 4 + ["not_supported"]))
    assert [(s.kind, s.short) for s in a.short_answer] == [("fact", True)]
    assert a.short_answer[0].text == "Rin trained, met a teacher and learned maps."
    assert a.short_answer[0].claims and a.short_answer[0].passages
    assert a.text.startswith(f"{w.SHORT_ANSWER_HEADING}\nRin trained, met a teacher")
    assert interpret.NO_VERDICT_LINE not in a.text
    assert "summary" not in [s.kind for s in a.prose]


async def test_a_summary_the_check_rejects_falls_back_to_the_verdict_line():
    prose = Prose(sentences=[
        S("Rin met a teacher.", [1]), S("Rin trained with the sword.", [2]), S("Rin learned maps.", [3]),
        S("Rin always won.", [1, 2, 3], "summary")])
    shape_plan = (Part("Was Rin right to leave?", "interpretive", ("What did Rin do?", "How did it end?"),
                       stance="verdict"),)
    a = await _answer(Script(LENS_CLAIMS, prose, ["supported"] * 3 + ["not_supported"]), shape_plan)
    assert [(s.kind, s.text) for s in a.short_answer] == [("limit", interpret.NO_VERDICT_LINE)]
    analysis = (Part("Was Rin right to leave?", "interpretive", ("What did Rin do?", "How did it end?"),
                     stance="analysis"),)
    a = await _answer(Script(LENS_CLAIMS, prose, ["supported"] * 3 + ["not_supported"]), analysis)
    assert a.short_answer == ()
    assert interpret.NO_VERDICT_LINE not in a.text and "no clear" not in a.text.lower()
