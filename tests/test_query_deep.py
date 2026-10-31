"""Deep answers (lore/query/deep.py) and the codex block of focused parts, over
the synthetic worlds library with a place, a faction, an idea and codex rows
(tests/deep_fixtures.py): real SQLite, Kuzu and LanceDB; scripted models.

Covers the planner and its validation, a section's isolation, the events and
their order, the per-section fallback, the codex block (only with entries), the
request budget, the interpretive close, the output shapes, and DEEP off."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from lore.mcp.shapes import to_search_result
from lore.mcp.stores import open_stores
from lore.query import codexslice, decompose as decompose_module, deep, interpret
from lore.query.decompose import Decomposition, PartSpec
from lore.query.deep import Candidate, DeepPlan, PlanSection
from lore.query.models import CodexLine, Evidence, Passage, SectionSpec
from lore.query.synthesize import CHECK_SKIPPED_NOTE, _build_prompt
from lore.query.write import (
    LENGTH_RULES, STORY_WRITER_PROMPT, compose_sections, writer_prompt,
)
from tests.deep_fixtures import (
    QUESTION, Scripted, add_codex, build_deep_library, run_scenario, snapshot,
)
from tests.worlds_fixtures import ALPHA

GENERAL = "Tell me everything about this world."  # names no entity: planned from the whole scope


async def ask(config, scripted, question=GENERAL, **kw):
    return await run_scenario(config, scripted, question, **kw)


DEEP_PLAN = Decomposition(sub_questions=[PartSpec(question=QUESTION)], depth="deep")
TWO = DeepPlan(overview="Everything about the traveller and the road.", sections=[
    PlanSection(heading="The traveller", entities=["a_zed", "a_yan"]),
    PlanSection(heading="The road", entities=["a_gate", "a_guild", "a_road"])])


@pytest.fixture
def config(tmp_path):
    return build_deep_library(tmp_path)


def types(events, kind="type"):
    return [e["type"] for e in events]


def of(events, kind):
    return [e for e in events if e["type"] == kind]


def deep_models(planner=TWO, **kw):
    return Scripted(DEEP_PLAN, planner=planner, tagged=True, **kw)


# ---- the planner: candidates and validation ----------------------------------------

def test_candidates_are_grouped_by_kind_and_ranked_by_span_and_richness(config):
    with open_stores(config) as s:
        found = deep.gather_candidates(s.conn, s.kuzu, book_ids=frozenset({"ba1", "ba2"}))
    kinds = [c.kind for c in found]
    assert kinds == sorted(kinds, key=deep.KINDS.index)  # grouped, in the planner's order
    assert {c.id for c in found} == {"a_zed", "a_yan", "a_gate", "a_guild", "a_road"}
    chars = [c.id for c in found if c.kind == "Character"]
    assert chars == ["a_zed", "a_yan"]  # the traveller spans more of the story and has a codex entry
    zed = next(c for c in found if c.id == "a_zed")
    assert zed.summary == "A traveller who walks the roads." and zed.score > 0
    # The other world's entities, and the book-title nodes, are not candidates.
    assert not any(c.id.startswith("b_") for c in found)


def test_candidates_stay_inside_the_scope_and_exclude_book_titles(config):
    with open_stores(config) as s:
        only_one = deep.gather_candidates(s.conn, s.kuzu, book_ids=frozenset({"ba2"}))
        named = deep.gather_candidates(s.conn, s.kuzu, book_ids=frozenset({"ba1", "ba2"}),
                                       book_names=frozenset({"zed quill"}))
    assert "a_gate" in {c.id for c in only_one}
    assert "a_zed" not in {c.id for c in named}


def test_each_kind_is_capped_but_a_named_entity_stays(config, monkeypatch):
    monkeypatch.setattr(deep, "PER_KIND", 1)
    with open_stores(config) as s:
        plain = deep.gather_candidates(s.conn, s.kuzu, book_ids=None)
        focused = deep.gather_candidates(s.conn, s.kuzu, book_ids=None, focus=["a_yan"])
    assert [c.id for c in plain if c.kind == "Character"] == ["a_zed"]
    # Both worlds' characters are here (no scope), and one per kind each.
    assert [c.id for c in focused if c.kind == "Character"] == ["a_yan"]


def test_a_question_about_one_entity_gets_its_facets(config, monkeypatch):
    monkeypatch.setattr(deep, "MIN_PERIOD_BEATS", 1)
    with open_stores(config) as s:
        from lore.query.models import SeedEntity
        from lore.query.seed import named_seeds
        import asyncio
        (focal,) = asyncio.run(named_seeds(s.kuzu, ["a_zed"]))
        facets = deep.facet_candidates(s.conn, s.kuzu, focal, book_ids=frozenset({"ba1", "ba2"}),
                                       books=None)
    by_kind = {}
    for c in facets:
        by_kind.setdefault(c.kind, []).append(c)
    assert facets[0].id == "a_zed" and facets[0].summary
    # Its codex entry's aspects, each narrowing the codex to that aspect.
    assert {c.aspect for c in by_kind["Aspect"]} == {"nature", "history", "role", "change"}
    assert all(c.entity_ids == ("a_zed",) for c in by_kind["Aspect"])
    # A stretch of its story: the beats of that stretch only.
    assert by_kind["Period"] and by_kind["Period"][0].beat_ids
    # The entities it is most involved with are candidates standing for both.
    related = {c.id: c for c in facets if c.kind not in ("Aspect", "Period")}
    assert {"a_yan", "a_gate", "a_guild", "a_road"} <= set(related)
    assert related["a_gate"].entity_ids == ("a_gate", "a_zed")


def candidates():
    return [Candidate("a", "Alpha", "Concept", ("a",)), Candidate("b", "Beta", "Place", ("b",)),
            Candidate("c", "Gamma", "Character", ("c",))]


def test_validation_drops_unknown_entities_and_empty_sections():
    raw = DeepPlan(sections=[
        PlanSection(heading="One", entities=["a", "ghost", "a", "b"]),
        PlanSection(heading="Nothing real", entities=["ghost", "phantom"]),
        PlanSection(heading="Empty", entities=[]),
        PlanSection(heading="Two", entities=["c"])])
    got = deep.validate_plan(raw, candidates())
    assert [(s.heading, [c.id for c in s.candidates]) for s in got] == [
        ("One", ["a", "b"]), ("Two", ["c"])]


def test_validation_caps_the_sections(monkeypatch):
    many = DeepPlan(sections=[PlanSection(heading=f"S{i}", entities=["a"])
                              for i in range(deep.DEEP_MAX_SECTIONS + 5)])
    assert len(deep.validate_plan(many, candidates())) == deep.DEEP_MAX_SECTIONS == 12
    assert [s.heading for s in deep.validate_plan(many, candidates(), 2)] == ["S0", "S1"]
    monkeypatch.setattr(deep, "DEEP_MAX_SECTIONS", 3)
    assert len(deep.validate_plan(many, candidates())) == 3


def test_validation_merges_repeated_headings_and_names_blank_ones():
    raw = DeepPlan(sections=[PlanSection(heading="Same", entities=["a"]),
                             PlanSection(heading="other", entities=["b"]),
                             PlanSection(heading=" same ", entities=["c", "a"]),
                             PlanSection(heading="  ##  ", entities=["b"])])
    got = deep.validate_plan(raw, candidates())
    assert [(s.heading, [c.id for c in s.candidates]) for s in got] == [
        ("Same", ["a", "c"]), ("other", ["b"]), ("Beta", ["b"])]


def test_validation_survives_a_garbled_plan():
    assert deep.validate_plan(DeepPlan(), candidates()) == []
    assert deep.validate_plan(None, candidates()) == []


def test_the_planner_prompt_lists_candidates_by_kind_with_their_summaries():
    prompt = deep.planner_prompt("Tell me everything", [
        Candidate("a", "Alpha", "Concept", ("a",), summary="An idea."),
        Candidate("c", "Gamma", "Character", ("c",))])
    assert prompt.index("Concept:") < prompt.index("Character:")
    assert "- a | Alpha | An idea." in prompt and "- c | Gamma" in prompt
    assert "between 3 and 12 sections" in prompt and "Tell me everything" in prompt


# ---- a deep answer: shape, events, numbering ---------------------------------------------

async def test_a_deep_answer_has_an_overview_a_contents_list_and_sections(config):
    scripted = deep_models()
    answer, events = await ask(config, scripted)
    assert answer.answer_style == "sectioned" and answer.template == "deep"
    assert [s.heading for s in answer.sections] == ["The traveller", "The road"]
    assert answer.overview and all(s.kind == "fact" for s in answer.overview)
    text = answer.text
    assert text.index("Contents\n1. The traveller\n2. The road") > 0
    assert text.index("## The traveller") < text.index("## The road")
    assert text.startswith(answer.overview[0].text)
    # One planner call, a claims call, a writer and a check per section, then the
    # overview's writer and check.
    assert [k for k, _ in scripted.calls] == [
        "plan", "claims", "write", "check", "claims", "write", "check", "write", "check"]
    assert answer.requests == 9


async def test_sections_cite_the_answers_own_claims_and_passages(config):
    answer, _ = await ask(config, deep_models())
    one, two = answer.sections
    assert (one.claims_start, one.claims_end) == (1, len(one.prose))
    assert two.claims_start == one.claims_end + 1 and two.claims_end == len(answer.claims)
    for section in answer.sections:
        for sentence in section.prose:
            assert all(section.claims_start <= k <= section.claims_end for k in sentence.claims)
            assert set(sentence.passages) == {answer.claims[k - 1].passage for k in sentence.claims}
    numbers = [p.n for p in answer.passages]
    assert numbers == list(range(1, len(numbers) + 1))
    assert all(c.passage in numbers for c in answer.claims)
    # The overview cites claims from several sections.
    cited = {k for s in answer.overview for k in s.claims}
    assert any(k <= one.claims_end for k in cited) and any(k >= two.claims_start for k in cited)


async def test_events_stream_in_order_and_each_section_carries_its_prose(config):
    answer, events = await ask(config, deep_models())
    kinds = types(events)
    assert kinds.index("parts") < kinds.index("plan") < kinds.index("overview")
    flow = [(e["type"], e.get("index"), e.get("state")) for e in events
            if e["type"] in ("plan", "section", "overview")]
    assert flow == [("plan", None, None), ("section", 0, "start"), ("section", 0, "done"),
                    ("section", 1, "start"), ("section", 1, "done"), ("overview", None, None)]
    (plan,) = of(events, "plan")
    assert plan["sections"][0]["heading"] == "The traveller"
    assert [e["id"] for e in plan["sections"][1]["entities"]] == ["a_gate", "a_guild", "a_road"]
    assert plan["scope"] == TWO.overview
    first = [e for e in of(events, "section") if e["state"] == "done"][0]
    assert first["heading"] == "The traveller" and first["style"] == "prose"
    assert first["claims_range"] == [1, 3] and len(first["claims"]) == 3
    assert [p["text"] for p in first["prose"]] == [s.text for s in answer.sections[0].prose]
    assert first["text"] == answer.sections[0].text
    # The passages a section added, with their text, so the page can show them at once.
    assert first["passages"] and all(p["text"] for p in first["passages"])
    second = [e for e in of(events, "section") if e["state"] == "done"][1]
    assert not ({p["n"] for p in first["passages"]} & {p["n"] for p in second["passages"]})
    # The sections come out before the answer is complete: every event is JSON-safe.
    json.dumps(events, allow_nan=False)
    steps = [(e["name"], e["state"], e["part"]) for e in of(events, "step")
             if e["name"] in ("plan", "section")]
    assert steps == [("plan", "start", None), ("plan", "done", None), ("section", "start", 0),
                     ("section", "done", 0), ("section", "start", 1), ("section", "done", 1)]


async def test_each_section_reads_its_own_entities_and_nothing_of_the_others(config):
    scripted = deep_models()
    await ask(config, scripted)
    one, two = scripted.prompts("claims")
    assert "Question: The traveller (Zed Quill, Yan Vale)" in one
    assert "Question: The road (Iron Gate, Ledger Guild, Long Road)" in two
    assert "The traveller" not in two and "The road" not in one
    # Its own context: a section of a longer answer to the whole question.
    for prompt in (one, two):
        assert f"one section of a long, detailed answer to the larger question: {GENERAL}" in prompt
        assert "at most 16 claims" in prompt
    # Each writer is told its section has no word limit; neither sees the other's facts.
    w1, w2 = scripted.prompts("write")[:2]
    assert "there is no word limit" in w1 and "400 words" not in w1
    assert "The road" not in w1 and "The traveller" not in w2


async def test_a_section_failing_leaves_the_others(config):
    scripted = deep_models(fail=("Question: The road",))
    answer, events = await ask(config, scripted)
    assert [s.heading for s in answer.sections] == ["The traveller"]
    done = [e for e in of(events, "section") if e["state"] == "done"]
    assert [(e["heading"], e["style"]) for e in done] == [("The traveller", "prose"),
                                                           ("The road", "failed")]
    assert answer.answer_style == "sectioned" and answer.overview


async def test_every_section_failing_is_an_error_not_an_empty_answer(config):
    with pytest.raises(RuntimeError):
        await ask(config, deep_models(fail=("Question: The",)))


async def test_a_section_whose_prose_fails_falls_back_to_its_claims_alone(config):
    scripted = deep_models(reject=("The road (",))
    answer, events = await ask(config, scripted)
    one, two = answer.sections
    assert one.style == "prose" and one.prose
    assert two.style == "claims" and two.prose == ()
    # The fallback is the claim list, cited by the answer's own passage numbers.
    claims = answer.claims[two.claims_start - 1:two.claims_end]
    assert all(c.text.rstrip(".") in two.text for c in claims)
    done = [e for e in of(events, "section") if e["state"] == "done"]
    assert [e["style"] for e in done] == ["prose", "claims"]
    assert done[1]["claims_range"] == [two.claims_start, two.claims_end] and not done[1]["prose"]
    assert "## The road" in answer.text


async def test_a_section_with_no_evidence_is_dropped(config):
    plan = DeepPlan(sections=[PlanSection(heading="The traveller", entities=["a_zed"]),
                              PlanSection(heading="A thing", entities=["a_road"])])
    scripted = deep_models(plan)
    # Nothing in the second section's claims reply can be quoted.
    real = scripted.generate

    async def generate(prompt, schema):
        from lore.query.synthesize import Synthesis
        if schema is Synthesis and "Question: A thing" in prompt:
            scripted.calls.append(("claims", prompt))
            return Synthesis(can_answer=False, claims=[])
        return await real(prompt, schema)
    scripted.generate = generate
    answer, events = await ask(config, scripted)
    assert [s.heading for s in answer.sections] == ["The traveller"]
    assert [e["style"] for e in of(events, "section") if e["state"] == "done"] == ["prose", "empty"]


async def test_no_usable_plan_falls_back_to_the_ordinary_answer(config):
    for planner in (DeepPlan(), DeepPlan(sections=[PlanSection(heading="X", entities=["ghost"])])):
        scripted = deep_models(planner)
        answer, events = await ask(config, scripted)
        assert answer.answer_style != "sectioned" and answer.sections == ()
        assert "plan" not in types(events) and "section" not in types(events)
        assert answer.claims  # the focused path answered


async def test_a_failing_planner_falls_back_too(config):
    def boom(prompt):
        raise RuntimeError("planner down")
    answer, events = await ask(config, deep_models(boom))
    assert answer.answer_style != "sectioned" and answer.claims


async def test_a_question_about_one_entity_is_planned_from_its_facets(config):
    scripted = deep_models(DeepPlan(sections=[
        PlanSection(heading="What it is", entities=["a_zed#nature"]),
        PlanSection(heading="Its history", entities=["a_zed#history", "a_gate"])]))
    answer, events = await ask(config, scripted, "Tell me about Zed Quill.")
    (prompt,) = scripted.prompts("plan")
    assert "Aspect:" in prompt and "a_zed#history" in prompt
    assert [s.heading for s in answer.sections] == ["What it is", "Its history"]
    # The aspect section reads the codex lines of that aspect only.
    nature = scripted.prompts("claims")[0]
    assert "Codex:" in nature and "Zed Quill, nature:" in nature
    assert "Zed Quill, history:" not in nature and "Zed Quill, change:" not in nature
    # A section standing for the entity and a neighbour keeps that entity's whole codex.
    history = scripted.prompts("claims")[1]
    assert "Zed Quill, history:" in history


async def test_the_overview_and_close_are_not_asked_for_without_claims_to_cite(config):
    answer, _ = await ask(config, deep_models(write_kinds=False))
    assert answer.sections[-1].kind == "section"  # no interpretive part: no close


# ---- depth ---------------------------------------------------------------------------------

async def test_depth_picks_the_path(config):
    plain = Decomposition(sub_questions=[PartSpec(question=QUESTION)], depth="focused")
    # auto + a focused plan: ordinary
    a, ev = await ask(config, Scripted(plain, planner=TWO, tagged=True))
    assert a.answer_style != "sectioned" and "plan" not in types(ev)
    # auto + a deep plan: deep
    a, ev = await ask(config, deep_models())
    assert a.answer_style == "sectioned"
    # deep forces it over a focused plan
    a, ev = await ask(config, Scripted(plain, planner=TWO, tagged=True), depth="deep")
    assert a.answer_style == "sectioned"
    # focused forces the ordinary path over a deep plan
    a, ev = await ask(config, deep_models(), depth="focused")
    assert a.answer_style != "sectioned" and "plan" not in types(ev)


async def test_a_question_across_worlds_is_not_answered_deep(config):
    scripted = deep_models()
    events = []
    with open_stores(config) as s:
        from lore.query.agent import answer_question
        answer = await answer_question(s.conn, s.kuzu, s.lance, QUESTION, scripted.models(),
                                       on_event=events.append)  # no world: both worlds match
    assert answer.answer_style != "sectioned" and "plan" not in types(events)


# ---- the budget -------------------------------------------------------------------------------

async def test_the_request_budget_stops_further_sections(config, monkeypatch):
    # Room for the planner-free first section and the tail, not for the second.
    monkeypatch.setattr(deep, "DEEP_MAX_REQUESTS", deep.SECTION_REQUESTS + deep.TAIL_REQUESTS + 1 + 2)
    scripted = deep_models()
    answer, events = await ask(config, scripted)
    assert [s.heading for s in answer.sections] == ["The traveller"]
    skipped = [e for e in of(events, "section") if e["state"] == "skipped"]
    assert [(e["index"], e["heading"], e["reason"]) for e in skipped] == [(1, "The road", "budget")]
    assert "stopped at the request limit; not written: The road" in answer.note
    assert answer.requests <= deep.DEEP_MAX_REQUESTS
    assert len(scripted.prompts("claims")) == 1


async def test_the_answer_reserve_stops_sections_too(config):
    calls = []

    def spend_ok():
        calls.append(1)
        return len(calls) <= 1  # the first section may start, the second may not
    scripted = deep_models()
    events = []
    with open_stores(config) as s:
        from lore.query.agent import answer_question
        answer = await answer_question(s.conn, s.kuzu, s.lance, QUESTION,
                                       scripted.models(spend_ok=spend_ok), world=ALPHA,
                                       on_event=events.append)
    assert [s.heading for s in answer.sections] == ["The traveller"]
    assert [e["reason"] for e in of(events, "section") if e["state"] == "skipped"] == ["reserve"]


async def test_the_budget_always_leaves_room_for_the_overview(config, monkeypatch):
    monkeypatch.setattr(deep, "DEEP_MAX_REQUESTS", 1 + deep.SECTION_REQUESTS * 2 + deep.TAIL_REQUESTS)
    scripted = deep_models()
    answer, _ = await ask(config, scripted)
    assert len(answer.sections) == 2 and answer.overview
    assert answer.requests == 1 + 2 * deep.SECTION_REQUESTS + deep.TAIL_REQUESTS


# ---- the interpretive close ---------------------------------------------------------------------

async def test_an_interpretive_deep_question_closes_with_a_reading_and_the_limits(config):
    plan = Decomposition(sub_questions=[
        PartSpec(question="Was the road right for Zed Quill?", kind="interpretive",
                 lenses=["What did the road cost Zed Quill?", "What did the road give Zed Quill?"]),
        PartSpec(question="Why does the road exist?", kind="beyond")], depth="deep")
    scripted = Scripted(plan, planner=TWO, tagged=True, write_kinds=True)
    answer, events = await ask(config, scripted, "Was the road right?")
    close = answer.sections[-1]
    assert close.kind == "close" and close.heading == deep.CLOSE_HEADING
    assert close.claims_end < close.claims_start
    kinds = [s.kind for s in close.prose]
    assert "reading" in kinds and kinds[-1] == "limit"
    assert interpret.LIMITS_LEAD in close.prose[-1].text
    # The reading rests on facts of at least two sections.
    reading = next(s for s in close.prose if s.kind == "reading")
    one, two = answer.sections[:2]
    assert any(k <= one.claims_end for k in reading.claims)
    assert any(k >= two.claims_start for k in reading.claims)
    assert "## " + deep.CLOSE_HEADING in answer.text
    last = of(events, "section")[-1]
    assert last["kind"] == "close" and last["index"] == 2 and last["claims_range"] is None
    # Facts are never repeated in the close.
    assert all(s.kind != "fact" for s in close.prose)


async def test_an_interpretive_close_never_goes_silent(config):
    plan = Decomposition(sub_questions=[
        PartSpec(question="Was the road right?", kind="interpretive",
                 lenses=["What did the road cost?", "What did the road give?"])], depth="deep")
    # The writer gives no reading: code says the evidence did not support one.
    scripted = Scripted(plan, planner=TWO, tagged=True, write_kinds=False)
    answer, _ = await ask(config, scripted, "Was the road right?")
    close = answer.sections[-1]
    assert [s.text for s in close.prose] == [interpret.NO_POSITION_LINE]


# ---- the codex block of focused parts -------------------------------------------------------------

async def test_a_focused_part_gets_a_codex_block_when_its_entities_have_entries(config):
    scripted = Scripted(Decomposition(sub_questions=[PartSpec(question=QUESTION)], depth="focused"))
    answer, _ = await run_scenario(config, scripted)
    (claims,) = scripted.prompts("claims")
    assert "\nCodex:" in claims
    assert "Zed Quill, nature: Zed is a traveller." in claims
    assert "never quote a codex line" in claims
    block = claims.split("Codex:")[1].split("Passages")[0]
    assert re.search(r"- \[\d+\] \(.*\) Zed Quill, history:", block)
    # Its passages joined the evidence, so every line cites a passage that is shown.
    for n in re.findall(r"- \[(\d+)\] \(", block):
        assert re.search(rf"^\[{n}\] ", claims, re.M)


async def test_no_codex_block_without_entries(tmp_path):
    config = build_deep_library(tmp_path, codex=False)
    scripted = Scripted(Decomposition(sub_questions=[PartSpec(question=QUESTION)], depth="focused"))
    await run_scenario(config, scripted)
    assert "Codex:" not in scripted.prompts("claims")[0]


async def test_no_codex_block_for_entities_without_entries(tmp_path):
    config = build_deep_library(tmp_path, codex=False)
    add_codex(config, ("a_gate",))  # an entry, but not for the seeds of this question
    scripted = Scripted(Decomposition(sub_questions=[PartSpec(question="Who is Yan Vale?")],
                                      depth="focused"))
    await run_scenario(config, scripted, question="Who is Yan Vale?")
    assert "Codex:" not in scripted.prompts("claims")[0]


def test_each_part_takes_the_codex_of_its_own_seeds(config):
    from lore.query.models import SeedEntity
    zed = SeedEntity("a_zed", "Zed Quill", "Character", ("ba1",), 0.0)
    gate = SeedEntity("a_gate", "Iron Gate", "Place", ("ba1",), 0.0)
    road = SeedEntity("a_road", "Long Road", "Concept", ("ba1",), 0.0)  # no entry
    vector = [1.0] + [0.0] * 767
    with open_stores(config) as s:
        of_zed = codexslice.part_codex(s.conn, s.lance, [zed], vector, None)
        of_gate = codexslice.part_codex(s.conn, s.lance, [road, gate], vector, None)
        none = codexslice.part_codex(s.conn, s.lance, [road], vector, None)
    assert {l.subject for l in of_zed} == {"Zed Quill"}
    assert {l.subject for l in of_gate} == {"Iron Gate"}
    assert none == []


async def test_the_codex_is_gated_by_deep(config, monkeypatch):
    monkeypatch.setattr(decompose_module, "DEEP", False)
    scripted = Scripted(Decomposition(sub_questions=[PartSpec(question=QUESTION)], depth="focused"))
    await run_scenario(config, scripted)
    assert "Codex:" not in scripted.prompts("claims")[0]


# ---- the codex slice and the evidence ------------------------------------------------------------------

def test_codex_lines_are_filtered_by_book_and_aspect(config):
    with open_stores(config) as s:
        everything = codexslice.lines_for(s.conn, ["a_zed"])
        assert [l.aspect for l in everything] == ["nature", "history", "role", "change"]
        assert {l.book_id for l in everything} == {"ba1", "ba2"}
        one_book = codexslice.lines_for(s.conn, ["a_zed"], book_ids=["ba2"])
        assert {l.book_id for l in one_book} == {"ba2"}
        narrowed = codexslice.lines_for(s.conn, ["a_zed", "a_gate"], aspects={"a_zed": {"role"}})
        assert [(l.subject, l.aspect) for l in narrowed] == [
            ("Zed Quill", "role"), ("Iron Gate", "nature"), ("Iron Gate", "history")]
        assert codexslice.lines_for(s.conn, ["nobody"]) == []


def test_the_slice_is_capped_with_the_earliest_and_the_latest(config):
    with open_stores(config) as s:
        lines = codexslice.lines_for(s.conn, ["a_zed"])
        vector = [1.0] + [0.0] * 767
        picked = codexslice.select_lines(s.conn, lines, ["a_zed"], vector, s.lance, None,
                                         spread=2, relevant=1)
    ordered = codexslice.order_lines(lines, None)
    assert len(picked) == 3
    assert picked[0] == ordered[0] and picked[-1] == ordered[-1]


def test_codex_lines_widen_the_evidence_through_their_own_slice(config):
    from lore.query.subgraph import assemble_ranked
    with open_stores(config) as s:
        line = CodexLine("ba2:2:0", "ba2", 2, "Zed Quill", "history", "Zed walked on.")
        without = assemble_ranked(s.conn, [], extra_chunks=[("ba1:1:0", "ba1", 1)],
                                  score_lists=[{"ba1:1:0": 1.0}], max_chars=10_000)
        with_codex = assemble_ranked(s.conn, [], extra_chunks=[("ba1:1:0", "ba1", 1)],
                                     score_lists=[{"ba1:1:0": 1.0}], max_chars=10_000,
                                     codexes=[[line]], codex_share=0.5)
    assert [p.chunk_id for p in without.passages] == ["ba1:1:0"]
    assert {p.chunk_id for p in with_codex.passages} == {"ba1:1:0", "ba2:2:0"}
    assert with_codex.codex == (line,) and with_codex.codexes == ((line,),)
    assert without.codex == () and without.codexes == ()


def test_the_codex_block_leaves_out_a_line_whose_passage_was_cut():
    passages = (Passage(n=1, chunk_id="c1", book_id="b", chapter_seq=1, text="Some text here."),)
    lines = (CodexLine("c1", "b", 1, "Ash", "nature", "Ash is a hill."),
             CodexLine("gone", "b", 2, "Ash", "history", "Ash fell."))
    prompt = _build_prompt("q", Evidence(edges=(), passages=passages, truncated=False, codex=lines))
    assert "Ash, nature: Ash is a hill." in prompt and "Ash fell" not in prompt
    bare = _build_prompt("q", Evidence(edges=(), passages=passages, truncated=False))
    assert "Codex:" not in bare


def test_the_default_prompts_do_not_change():
    passages = (Passage(n=1, chunk_id="c1", book_id="b", chapter_seq=1, text="Some text here."),)
    evidence = Evidence(edges=(), passages=passages, truncated=False)
    assert "at most 8 claims" in _build_prompt("q", evidence)
    assert _build_prompt("q", evidence) == _build_prompt("q", evidence, section=None)
    assert "at most 5 claims" in _build_prompt("q", evidence,
                                               section=SectionSpec("whole", 5))
    default = writer_prompt("q", ["q"], [], passages, None)
    assert "about 400 words at most" in default
    assert LENGTH_RULES[None] in default and "{length}" not in default
    assert "{length}" in STORY_WRITER_PROMPT


# ---- output shapes ---------------------------------------------------------------------------------------

async def test_the_mcp_shape_carries_the_sections_and_overview(config):
    answer, _ = await ask(config, deep_models())
    out = to_search_result(answer, {"ba1": "Alpha One"}).model_dump()
    assert out["answer_style"] == "sectioned" and out["requests"] == answer.requests
    assert [s["heading"] for s in out["sections"]] == ["The traveller", "The road"]
    first = out["sections"][0]
    assert first["claims_range"] == [1, 3] and first["style"] == "prose" and first["kind"] == "section"
    assert first["prose"][0].keys() >= {"text", "claims", "passages", "new_paragraph", "kind"}
    assert first["entities"] == ["Zed Quill", "Yan Vale"]
    assert out["overview"] and out["text"] if "text" in out else out["answer"] == answer.text
    assert len(out["claims"]) == len(answer.claims)
    json.dumps(out, allow_nan=False)


async def test_an_ordinary_answer_has_no_sections_in_its_shape(config):
    scripted = Scripted(Decomposition(sub_questions=[PartSpec(question=QUESTION)], depth="focused"))
    answer, _ = await run_scenario(config, scripted)
    out = to_search_result(answer, {}).model_dump()
    assert out["sections"] == [] and out["overview"] == [] and out["requests"] == 0


def test_compose_sections_renders_markdown_headings_and_a_contents_list():
    from lore.query.models import AnswerSection, ProseSentence
    overview = (ProseSentence("It covers two things.", (1,), (1,)),)
    sections = [AnswerSection("Alpha", (), "Alpha text. [1]", 1, 1),
                AnswerSection("Beta", (), "Beta text. [2]", 2, 2)]
    assert compose_sections(overview, sections) == (
        "It covers two things. [1]\n\nContents\n1. Alpha\n2. Beta\n\n"
        "## Alpha\n\nAlpha text. [1]\n\n## Beta\n\nBeta text. [2]")
    assert compose_sections((), sections[:1]) == "## Alpha\n\nAlpha text. [1]"


# ---- DEEP off ------------------------------------------------------------------------------------------------

GOLDEN = Path(__file__).parent / "golden" / "deep_off.json"


@pytest.mark.parametrize("name,plan", [
    ("one_part", Decomposition(sub_questions=[PartSpec(question=QUESTION)], depth="deep")),
    ("two_parts", Decomposition(sub_questions=[PartSpec(question="Who is Zed Quill?"),
                                               PartSpec(question="What is the Long Road?")],
                                depth="deep"))])
async def test_with_deep_off_everything_is_what_it_was(tmp_path, monkeypatch, name, plan):
    """Events, prompts and the answer, with codex rows in the library and depth
    asked for, are byte-identical to the code before deep answers (pinned from
    6d931a7)."""
    monkeypatch.setattr(decompose_module, "DEEP", False)
    config = build_deep_library(tmp_path)
    scripted = Scripted(plan)
    answer, events = await run_scenario(config, scripted, depth="deep")
    got = json.loads(json.dumps(snapshot(answer, events, scripted), sort_keys=True))
    assert got == json.loads(GOLDEN.read_text())[name]
    assert answer.sections == () and answer.overview == () and answer.requests == 0


def test_deep_is_on_by_default():
    assert decompose_module.DEEP is True
    assert deep.DEEP_MAX_SECTIONS == 12


# ---- the eval arm, and generality -------------------------------------------------------------------------

def test_the_arm_toggles_deep_and_asks_at_depth_deep():
    import sys
    sys.path.insert(0, str(Path(__file__).parents[1] / "evals" / "answer_quality"))
    import run_pair
    assert run_pair.parse_arm("deep=true") == {("lore.query.decompose", "DEEP"): True}
    assert run_pair.parse_arm("deep=off") == {("lore.query.decompose", "DEEP"): False}


async def test_the_deep_arm_asks_every_question_at_depth_deep(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(Path(__file__).parents[1] / "evals" / "answer_quality"))
    import run_pair
    seen = []

    async def lore_search(config, question, models=None, **kw):
        seen.append(kw)
        return {}
    monkeypatch.setattr(run_pair.tools, "lore_search", lore_search)
    monkeypatch.setattr(run_pair, "_real_models", lambda config: None)
    await run_pair.ask(None, "q", run_pair.parse_arm("deep=true"), None)
    await run_pair.ask(None, "q", run_pair.parse_arm("deep=false"), None)
    await run_pair.ask(None, "q", run_pair.parse_arm("control"), None)
    assert seen == [{"depth": "deep"}, {}, {}]


def test_the_deep_code_and_prompts_name_no_character_book_or_genre():
    from lore.query import synthesize as synthesize_module
    from lore.query import write as write_module
    banned = r"\b(holmes|watson|sherlock|small|hope|drebber|stangerson|sholto|ferrier|tonga|" \
             r"morstan|lestrade|baker street|mormon|agra|crime|crimes|murder|villain|villains|" \
             r"detective|treasure|darcy|bennet|wizard|dragon|magic|kingdom|elf|elves|quest|sword)\b"
    lib = Path(__file__).parents[1] / "lore" / "query"
    for name in ("deep.py", "codexslice.py"):
        assert not re.findall(banned, (lib / name).read_text().lower()), name
    texts = "\n".join((deep.PLANNER_PROMPT, deep.CLOSE_HEADING, synthesize_module._CODEX_BLOCK,
                       synthesize_module._SECTION_CONTEXT, write_module.SECTION_LENGTH_RULE,
                       write_module.OVERVIEW_LENGTH_RULE, write_module.CONTENTS_HEADING))
    assert not re.findall(banned, texts.lower())


async def test_the_real_search_stops_sections_when_the_ledger_is_spent(config, monkeypatch):
    from lore.mcp import tools
    scripted = deep_models()
    checks = []

    def budget(cfg):
        checks.append(1)
        return None if len(checks) <= 2 else "every model is at its cap"  # guard, section 1, then spent
    monkeypatch.setattr(tools, "_check_synthesis_budget", budget)
    monkeypatch.setattr(tools, "_build_models", lambda cfg: scripted.models())
    result = await tools.lore_search(config, GENERAL, world=ALPHA)
    assert [s["heading"] for s in result["sections"]] == ["The traveller"]
    assert "not written: The road" in result["note"]
