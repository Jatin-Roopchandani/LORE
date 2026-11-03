"""A deep question with an interpretive part is organised by its own parts and
lenses (lore/query/deep.py, answer_lenses), not by a topical outline: routing,
one section per lens with a clean heading, the short answer first, the group of
a part, failed and empty sections, the claim cap, the opening rule, and the
events the graph view stages from.

Models are scripted over the synthetic worlds library; nothing reaches a real
one."""
from __future__ import annotations

import re

import pytest

from lore.query import decompose as decompose_module, deep, interpret
from lore.query.decompose import Decomposition, PartSpec
from lore.query.deep import DeepPlan, PlanSection
from lore.query.synthesize import Synthesis
from lore.query.write import LENS_LENGTH_RULE, Prose, Sentence
from tests.deep_fixtures import Scripted, build_deep_library, run_scenario
from tests.test_query_interpret_agent import (
    LENS_A, LENS_B, QUESTION, Models, plan_of,
)
from tests.worlds_fixtures import ALPHA, QUESTION_VECTOR

QUOTES = ["walked on with Yan Vale", "the traveller Zed Quill", "Zed Quill walked on with",
          "Yan Vale through the"]


class LensModels(Models):
    """Per-lens claims (each lens quotes its own stretch), a reading phase that
    answers with `reading`, and a section writer that states each fact once."""

    def __init__(self, decomposition, reading, *, quotes=None, fail_section=None, empty=()):
        super().__init__(decomposition, write=None)
        self.reading = reading
        self.quotes = quotes or {}
        self.fail_section = fail_section
        self.empty = tuple(empty)
        self.calls: list[tuple[str, str]] = []

    async def generate(self, prompt, schema):
        if schema is Synthesis:
            self.claims_prompts.append(prompt)
            self.calls.append(("claims", prompt))
            question = re.search(r"^Question: (.*)$", prompt, re.M).group(1)
            if question in self.empty:
                return Synthesis(can_answer=False, claims=[])
            n = int(re.search(r"^\[(\d+)\]", prompt, re.M).group(1))
            quote = self.quotes.get(question, QUOTES[2])
            return Synthesis(can_answer=True, claims=[
                {"text": f"Zed Quill: {quote}.", "passage": n, "quote": quote}])
        if "sentences" in schema.model_fields:
            self.writer_prompts.append(prompt)
            self.calls.append(("write", prompt))
            if "Evidence for:" in prompt:
                return self.reading
            if self.fail_section and self.fail_section in prompt:
                raise RuntimeError("writer failed")
            count = len(re.findall(r"^\d+\. ", prompt.split("Facts:")[-1], re.M))
            return Prose(sentences=[Sentence(text=f"Section fact {i}.", claims=[i])
                                    for i in range(1, count + 1)])
        self.calls.append(("check", prompt))
        return await super().generate(prompt, schema)

    def prompts(self, kind):
        return [p for k, p in self.calls if k == kind]


def S(text, claims, kind="fact"):
    return Sentence(text=text, claims=claims, kind=kind)


READING = Prose(sentences=[
    S("Fact one.", [1]), S("Fact two.", [2]),
    S("The evidence suggests Zed was right to go on.", [1, 2], "reading"),
    S("Against that, the road was long.", [2], "counter")])
QUOTE_OF = {LENS_A: QUOTES[0], LENS_B: QUOTES[1]}
ONE_PART = Decomposition(sub_questions=[PartSpec(
    question=QUESTION, kind="interpretive", stance="verdict", lenses=[LENS_A, LENS_B])], depth="deep")


@pytest.fixture
def config(tmp_path):
    return build_deep_library(tmp_path)


async def ask(config, scripted, question=QUESTION, **kw):
    return await run_scenario(config, scripted, question, depth="deep", **kw)


def of(events, kind):
    return [e for e in events if e["type"] == kind]


# ---- routing --------------------------------------------------------------------------------

async def test_a_deep_interpretive_question_is_organised_by_its_lenses_not_a_topical_outline(config):
    scripted = LensModels(ONE_PART, READING, quotes=QUOTE_OF)
    scripted.planner_called = False
    answer, events = await ask(config, scripted)
    # The topical planner never ran: no planner step, no planner prompt.
    assert not [e for e in of(events, "step") if e["name"] == "plan"]
    assert not any("table of contents" in p for _, p in scripted.calls)
    assert answer.answer_style == "sectioned" and answer.template != "deep"
    assert [s.heading for s in answer.sections[:2]] == [
        "What did Zed Quill do on the road", "How did the road end for Zed Quill"]
    assert [s.kind for s in answer.sections] == ["section", "section", "close"]


async def test_a_deep_question_with_no_interpretive_part_still_gets_the_topical_planner(config):
    plan = Decomposition(sub_questions=[PartSpec(question="Tell me about the world.")], depth="deep")
    scripted = Scripted(plan, planner=DeepPlan(overview="x", sections=[
        PlanSection(heading="The traveller", focus="Who the traveller is", entities=["a_zed"]),
        PlanSection(heading="The road", focus="Where the road leads", entities=["a_road"])]), tagged=True)
    answer, events = await run_scenario(config, scripted, "Tell me about the world.", depth="deep")
    assert [e["name"] for e in of(events, "step") if e["name"] == "plan"]
    # No interpretive part: the topical sections, opened by a short answer of fact sentences only.
    assert answer.template == "deep" and answer.short_answer
    assert all(s.kind == "fact" for s in answer.short_answer)


async def test_a_focused_interpretive_question_is_not_sectioned(config):
    scripted = LensModels(plan_of(PartSpec(question=QUESTION, kind="interpretive",
                                           lenses=[LENS_A, LENS_B])), READING, quotes=QUOTE_OF)
    answer, _ = await run_scenario(config, scripted, QUESTION, depth="focused")
    assert answer.answer_style == "prose" and answer.sections == ()
    assert answer.short_answer and answer.short_answer[0].kind == "reading"


# ---- sections: one per lens, grouped under a part --------------------------------------------

async def test_each_lens_is_a_section_with_its_own_claims_and_prose(config):
    scripted = LensModels(ONE_PART, READING, quotes=QUOTE_OF)
    answer, events = await ask(config, scripted)
    one, two, close = answer.sections
    assert (one.claims_start, one.claims_end) == (1, 1) and (two.claims_start, two.claims_end) == (2, 2)
    assert [s.text for s in one.prose] == ["Section fact 1."]
    assert one.prose[0].claims == (1,) and one.prose[0].passages
    assert two.prose[0].claims == (2,)
    assert len(answer.claims) == 2
    # Each section's writer saw only its own lens's fact.
    sections = [p for p in scripted.prompts("write") if "Evidence for:" not in p]
    assert len(sections) == 2
    assert "walked on with Yan Vale" in sections[0] and "the traveller Zed Quill" not in sections[0]
    assert "the traveller Zed Quill" in sections[1] and "walked on with Yan Vale" not in sections[1]
    # Section events are indexed by retrieval part: the index the parts event uses.
    (parts,) = of(events, "parts")
    assert parts["sub_questions"] == [LENS_A, LENS_B]
    assert [e["index"] for e in of(events, "section") if e["state"] == "start"] == [0, 1, 2]
    assert answer.requests == scripted_requests(scripted)


def scripted_requests(scripted):
    """Model requests after decomposition and selection: claims, writer, check."""
    return len(scripted.calls)


async def test_the_heading_is_the_lens_question_cleaned_and_a_part_is_named_when_there_are_several(config):
    plan = Decomposition(sub_questions=[
        PartSpec(question="In the first volume, who walked best?", kind="interpretive",
                 lenses=[LENS_A, "**How** did it end [3]?"]),
        PartSpec(question="In the second volume, who walked best?", kind="interpretive",
                 lenses=["Who guided the walker?", "What did the guide ask in return?"])], depth="deep")
    quotes = {LENS_A: QUOTES[0], "**How** did it end [3]?": QUOTES[1],
              "Who guided the walker?": QUOTES[2], "What did the guide ask in return?": QUOTES[3]}
    reading = Prose(sentences=[
        S("F1.", [1]), S("F2.", [2]), S("F3.", [3]), S("F4.", [4]),
        S("In the first volume, the evidence suggests Zed walked best.", [1, 2], "reading"),
        S("In the second volume, on balance the guide walked best.", [3, 4], "reading")])
    answer, events = await ask(config, LensModels(plan, reading, quotes=quotes))
    sections = [s for s in answer.sections if s.kind == "section"]
    assert [s.heading for s in sections] == [
        "What did Zed Quill do on the road", "How did it end", "Who guided the walker",
        "What did the guide ask in return"]
    assert [s.group for s in sections] == ["In the first volume, who walked best?"] * 2 + [
        "In the second volume, who walked best?"] * 2
    # One verdict per part, in the short answer.
    assert [s.text for s in answer.short_answer] == [
        "In the first volume, the evidence suggests Zed walked best.",
        "In the second volume, on balance the guide walked best."]
    (plan_event,) = of(events, "plan")
    assert [(x["index"], x["group"]) for x in plan_event["sections"]][-1] == (3, "In the second volume, who walked best?")


async def test_a_lens_label_heads_the_section_and_the_full_question_rides_along_as_its_title(config):
    plan = Decomposition(sub_questions=[PartSpec(
        question=QUESTION, kind="interpretive",
        lenses=[{"question": LENS_A, "label": "What Zed did"}, {"question": LENS_B}])], depth="deep")
    answer, events = await ask(config, LensModels(plan, READING, quotes=QUOTE_OF))
    one, two, close = answer.sections
    assert (one.heading, one.title) == ("What Zed did", LENS_A)
    assert (two.heading, two.title) == ("How did the road end for Zed Quill", LENS_B)  # no label: the question
    assert close.title == ""
    (plan_event,) = of(events, "plan")
    assert [(x["heading"], x["title"]) for x in plan_event["sections"]] == [
        ("What Zed did", LENS_A), ("How did the road end for Zed Quill", LENS_B)]
    done = [e for e in of(events, "section") if e["state"] == "done" and e["kind"] == "section"]
    assert [e["title"] for e in done] == [LENS_A, LENS_B]


async def test_a_single_part_names_no_group(config):
    answer, _ = await ask(config, LensModels(ONE_PART, READING, quotes=QUOTE_OF))
    assert [s.group for s in answer.sections] == ["", "", ""]


# ---- the short answer comes first -------------------------------------------------------------

async def test_the_short_answer_is_sent_before_any_section_and_opens_the_text(config):
    answer, events = await ask(config, LensModels(ONE_PART, READING, quotes=QUOTE_OF))
    types = [e["type"] for e in events]
    assert types.index("short_answer") < types.index("section")
    assert types.index("plan") < types.index("short_answer")
    event = of(events, "short_answer")[0]
    assert [s["text"] for s in event["prose"]] == ["The evidence suggests Zed was right to go on."]
    assert event["prose"][0]["kind"] == "reading" and event["prose"][0]["claims"] == [1, 2]
    # It carries the claims and passages its citations need, in the answer's numbering.
    assert [c["text"] for c in event["claims"]] == [c.text for c in answer.claims]
    assert {p["n"] for p in event["passages"]} >= {c.passage for c in answer.claims}
    assert answer.text.startswith("Short answer\nThe evidence suggests Zed was right to go on. [")
    assert [s.kind for s in answer.short_answer] == ["reading"]


async def test_no_reading_means_code_says_there_is_no_clear_position_and_the_sections_still_come(config):
    reading = Prose(sentences=[S("Fact one.", [1]), S("Fact two.", [2])])
    answer, events = await ask(config, LensModels(ONE_PART, reading, quotes=QUOTE_OF))
    assert [(s.kind, s.text) for s in answer.short_answer] == [("limit", interpret.NO_VERDICT_LINE)]
    assert len(answer.sections) == 2 and answer.sections[0].prose
    assert of(events, "short_answer")[0]["prose"][0]["text"] == interpret.NO_VERDICT_LINE


async def test_the_close_holds_the_counter_case_and_the_limits_and_no_reading(config):
    plan = Decomposition(sub_questions=[
        PartSpec(question=QUESTION, kind="interpretive", lenses=[LENS_A, LENS_B]),
        PartSpec(question="Why did the author write the road?", kind="beyond")], depth="deep")
    answer, events = await ask(config, LensModels(plan, READING, quotes=QUOTE_OF))
    close = answer.sections[-1]
    assert close.kind == "close" and close.heading == deep.CLOSE_HEADING
    assert close.claims_end < close.claims_start
    assert [s.kind for s in close.prose] == ["counter", "limit"]
    assert close.prose[-1].text.startswith(interpret.LIMITS_LEAD)
    assert all(s.kind != "reading" for sec in answer.sections for s in sec.prose)
    assert of(events, "section")[-1]["kind"] == "close"


# ---- the writer of a lens section -------------------------------------------------------------

async def test_a_lens_section_opens_by_saying_how_its_facts_bear_on_the_question(config):
    scripted = LensModels(ONE_PART, READING, quotes=QUOTE_OF)
    await ask(config, scripted)
    section = [p for p in scripted.prompts("write") if "Evidence for:" not in p][0]
    assert " ".join(LENS_LENGTH_RULE.split()) in " ".join(section.split())
    assert f"Question: {LENS_A} (one aspect of the larger question: {QUESTION})" in section
    flat = " ".join(LENS_LENGTH_RULE.split())
    assert "Open with one sentence that says how these facts bear on the question" in flat
    assert "never open with a remark that no fact supports" in flat
    assert "there is no word limit" in flat
    # The opening is a fact sentence citing claims, or left out: the writer's reply
    # is verified and checked like any other sentence, and a section writer has no
    # reading kind to open with.
    assert "reading" not in section.split("Facts:")[0]


async def test_deep_mode_lifts_the_claim_cap_of_the_lens_parts_only(config):
    deep_models = LensModels(ONE_PART, READING, quotes=QUOTE_OF)
    await ask(config, deep_models)
    focused = LensModels(Decomposition(sub_questions=ONE_PART.sub_questions, depth="focused"),
                         READING, quotes=QUOTE_OF)
    await run_scenario(config, focused, QUESTION, depth="focused")
    assert all(f"Give at most {deep.SECTION_CLAIMS} claims" in p for p in deep_models.claims_prompts)
    assert all("Give at most 8 claims" in p for p in focused.claims_prompts)


# ---- failed and empty sections ----------------------------------------------------------------

async def test_a_section_whose_writer_fails_falls_back_to_its_checked_facts(config):
    scripted = LensModels(ONE_PART, READING, quotes=QUOTE_OF, fail_section=QUOTES[1])
    answer, _ = await ask(config, scripted)
    two = answer.sections[1]
    assert two.style == "claims" and two.prose == () and "Zed Quill: the traveller Zed Quill." in two.text
    assert answer.unwritten == 0 and answer.sections[0].style == "prose"


async def test_a_section_that_fails_is_left_out_and_counted(config, monkeypatch):
    real = deep.cited_prose

    async def flaky(question, *a, **kw):
        if question.startswith(LENS_B):
            raise RuntimeError("boom")
        return await real(question, *a, **kw)
    monkeypatch.setattr(deep, "cited_prose", flaky)
    scripted = LensModels(ONE_PART, READING, quotes=QUOTE_OF)
    answer, events = await ask(config, scripted)
    assert [s.heading for s in answer.sections if s.kind == "section"] == [
        "What did Zed Quill do on the road"]
    assert answer.unwritten == 1
    failed = [e for e in of(events, "section") if e["state"] == "done" and e.get("style") == "failed"]
    assert [e["index"] for e in failed] == [1]
    assert "How did the road end" not in answer.text


async def test_a_lens_that_found_nothing_is_not_a_section_and_is_counted(config):
    scripted = LensModels(ONE_PART, READING, quotes=QUOTE_OF, empty=[LENS_B])
    answer, events = await ask(config, scripted)
    assert [s.heading for s in answer.sections if s.kind == "section"] == [
        "What did Zed Quill do on the road"]
    assert answer.unwritten == 1
    assert [e["index"] for e in of(events, "plan")[0]["sections"]] == [0]


async def test_the_topical_path_counts_its_unwritten_sections_too(config):
    plan = Decomposition(sub_questions=[PartSpec(question="Tell me about the world.")], depth="deep")
    planner = DeepPlan(overview="x", sections=[
        PlanSection(heading="The traveller", focus="Who the traveller is", entities=["a_zed"]),
        PlanSection(heading="The road", focus="Where the road leads", entities=["a_road"])])
    scripted = Scripted(plan, planner=planner, tagged=True, fail=["Where the road leads"])
    answer, _ = await run_scenario(config, scripted, "Tell me about the world.", depth="deep")
    assert [s.heading for s in answer.sections] == ["The traveller"]
    assert answer.unwritten == 1


# ---- the graph stages from the events ---------------------------------------------------------

async def test_the_lens_path_sends_seeds_and_edges_per_part_before_any_answer_text(config):
    answer, events = await ask(config, LensModels(ONE_PART, READING, quotes=QUOTE_OF))
    types = [e["type"] for e in events]
    assert {e["part"] for e in of(events, "seeds")} == {0, 1}
    assert {e["part"] for e in of(events, "edges")} == {0, 1}
    last_graph = max(i for i, t in enumerate(types) if t in ("seeds", "edges"))
    assert last_graph < types.index("short_answer") < types.index("section")
    # `evidence` is sent once, before any writing.
    assert types.count("evidence") == 1 and types.index("evidence") < types.index("short_answer")
    # Part indices are the parts event's: seeds of part k belong to sub_questions[k].
    assert max(e["part"] for e in of(events, "seeds")) < len(of(events, "parts")[0]["sub_questions"])


async def test_the_topical_path_sends_seeds_and_edges_per_section_and_names_the_sections_as_parts(config):
    plan = Decomposition(sub_questions=[PartSpec(question="Tell me about the world.")], depth="deep")
    planner = DeepPlan(overview="x", sections=[
        PlanSection(heading="The traveller", focus="Who the traveller is", entities=["a_zed", "a_yan"]),
        PlanSection(heading="The road", focus="Where the road leads", entities=["a_gate", "a_guild", "a_road"])])
    answer, events = await run_scenario(config, Scripted(plan, planner=planner, tagged=True),
                                        "Tell me about the world.", depth="deep")
    types = [e["type"] for e in events]
    seeds, edges = of(events, "seeds"), of(events, "edges")
    assert [e["part"] for e in seeds] == [0, 1] and [e["part"] for e in edges] == [0, 1]
    assert {e["name"] for e in seeds[0]["entities"]} >= {"Zed Quill"}
    assert edges[0]["total"] > 0 and edges[0]["edges"]
    # Each section's graph arrives as that section starts, not at the end.
    starts = [i for i, e in enumerate(events) if e["type"] == "section" and e["state"] == "start"]
    assert starts[0] < types.index("seeds") < types.index("edges") < starts[1]
    # The parts event after the plan names the sections, so part k is section k.
    parts = of(events, "parts")
    assert parts[-1]["sub_questions"] == ["The traveller", "The road"]
    assert types.index("plan") < len(types) - 1 - types[::-1].index("parts") < types.index("seeds")
    assert types.count("evidence") == 1 and types.index("evidence") > types.index("edges")
