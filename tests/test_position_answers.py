"""Answers under a reading position: every step sees only what has been read.

Real SQLite, Kuzu and LanceDB over tests/position_fixtures.py; the models are
scripted and no test reaches one. The beyond-position facts carry markers
(MARK-hb1-3, SECRETFACT, LATEBEAT, ...) so a test can see exactly what reached
a prompt. With the position off, tests/test_position_golden.py pins every
output to what it was before the feature.
"""
from __future__ import annotations

import pytest

from lore.db.sqlite import open_db
from lore.mcp import tools
from lore.mcp.stores import open_stores
from lore.query import agent as agent_module
from lore.query.agent import answer_question
from lore.query.decompose import Decomposition, PartSpec
from lore.query.deep import DeepPlan, PlanSection
from lore.query.models import Claim
from lore.query.synthesize import Synthesis
from lore.reading_position import ReadingPosition, set_position
from tests.deep_fixtures import Scripted
from tests.position_fixtures import HOLLOW, OTHER, build_position_library
from tests.worlds_fixtures import scripted_models

QUESTION = "Who is Old Tam?"
LATE = ("MARK-hb1-3", "SECRETFACT", "LATEBEAT", "LATESTATE", "Dorn Vale", "BETRAYED",
        "MARK-hb2", "SEQUELBEAT", "SEQUELSTATE", "MARK-hb3")


@pytest.fixture
def config(tmp_path):
    return build_position_library(tmp_path)


def position(config, world=HOLLOW, book="hb1", chapter=2, read=(), enabled=True):
    conn = open_db(config.sqlite_path)
    try:
        wid = conn.execute("SELECT id FROM worlds WHERE name = ?", (world,)).fetchone()[0]
        set_position(conn, ReadingPosition(wid, book, chapter, tuple(read), enabled))
    finally:
        conn.close()


async def ask(config, world=HOLLOW, question=QUESTION, sub_questions=None, synthesis=None, **kw):
    models, calls = scripted_models(sub_questions, synthesis=synthesis)
    with open_stores(config) as s:
        args = {} if world is None else {"world": world}
        answer = await answer_question(s.conn, s.kuzu, s.lance, question, models, **args, **kw)
    return answer, calls


def prompt_of(calls) -> str:
    return "\n".join(calls["synthesize"] + calls["select"] + calls["decompose"])


# --- evidence ---------------------------------------------------------------------------

async def test_evidence_comes_only_from_chunks_the_reader_has_read(config):
    position(config, chapter=2)
    answer, calls = await ask(config)
    prompt = prompt_of(calls)
    assert "MARK-hb1-1" in prompt and "MARK-hb1-2" in prompt
    for late in LATE:
        assert late not in prompt, late
    assert {(p.book_id, p.chapter_seq) for p in answer.passages} == {("hb1", 1), ("hb1", 2)}
    assert answer.edges and all((e.book_id, e.chapter_seq) in {("hb1", 1)} for e in answer.edges)
    # the seeds are who has been met, under the name met
    assert {s.id: s.name for s in answer.seeds} == {"h_ren": "Old Tam", "h_mira": "Mira Cole"}
    assert "Ren Ashby" not in prompt  # the merged name stays out of every prompt
    assert "Old Tam" in prompt
    assert answer.position_note == "Answered from what you've read: up to chapter 2 of Hollow Gate"


async def test_a_fact_beyond_the_position_cannot_appear_in_the_answer(config):
    # Without a position, passage n of hb1 chapter 3 holds the secret.
    free, _calls = await ask(config, spoilers=True)
    secret = next(p for p in free.passages if p.chunk_id == "hb1:3:0")
    quote = "Old Tam was truly Ren Ashby"
    assert quote in secret.text
    leak = Synthesis(can_answer=True, claims=[
        Claim(text="Old Tam is Ren Ashby.", passage=secret.n, quote=quote)])
    got, _ = await ask(config, spoilers=True, synthesis=leak)
    assert any("Ren Ashby" in c.text for c in got.claims)  # the control: it would appear
    position(config, chapter=2)
    # Even a model that names a visible passage and quotes the hidden text is dropped:
    # the quote is checked against the visible passage it cites.
    for n in (1, 2):
        sealed = Synthesis(can_answer=True, claims=[
            Claim(text="Old Tam is Ren Ashby.", passage=n, quote=quote)])
        answer, calls = await ask(config, synthesis=sealed)
        assert answer.claims == () and "Ren Ashby" not in answer.text
        assert quote not in prompt_of(calls)


async def test_the_earlier_book_is_read_in_full_and_the_later_one_not_at_all(config):
    position(config, book="hb2", chapter=1)
    answer, calls = await ask(config)
    prompt = prompt_of(calls)
    assert "SECRETFACT" in prompt and "MARK-hb1-3" in prompt  # the first book is read
    assert "MARK-hb2-1" in prompt
    assert "MARK-hb2-2" not in prompt and "MARK-hb2-3" not in prompt
    assert "MARK-hb3" not in prompt
    assert answer.position_note.endswith("up to chapter 1 of Hollow Keep")


async def test_a_book_of_unknown_order_joins_only_when_marked_read(config):
    question = "Who are Pip Marsh and Lan Dray?"
    position(config, book="hb2", chapter=3)
    _answer, calls = await ask(config, question=question)
    assert "MARK-hb3" not in prompt_of(calls)
    position(config, book="hb2", chapter=3, read=["hb3"])
    answer, calls = await ask(config, question=question)
    assert "MARK-hb3" in prompt_of(calls) and {"w_pip", "w_lan"} <= {s.id for s in answer.seeds}


async def test_the_timeline_and_graph_evidence_are_cut_at_the_position(config):
    position(config, chapter=2)
    _answer, calls = await ask(config)
    prompt = calls["synthesize"][0]
    assert "EARLYBEAT" in prompt and "EARLYSTATE" in prompt
    position(config, chapter=3)
    _answer, calls = await ask(config)
    prompt = calls["synthesize"][0]
    assert "LATEBEAT" in prompt and "LATESTATE" in prompt and "SEQUELBEAT" not in prompt


async def test_the_decomposition_lists_only_the_books_in_reach(config):
    position(config, chapter=2)
    _answer, calls = await ask(config, sub_questions=[QUESTION])
    assert "Hollow Gate" in calls["decompose"][0]
    assert "Hollow Keep" not in calls["decompose"][0] and "Wanderer Road" not in calls["decompose"][0]


async def test_vector_and_keyword_candidates_stop_at_the_position(config, monkeypatch):
    monkeypatch.setattr(agent_module, "KEYWORD_K", 6)
    position(config, chapter=2)
    _answer, calls = await ask(config, question="the long road with the traveller")
    for late in LATE:
        assert late not in prompt_of(calls), late


async def test_a_part_naming_a_book_stays_inside_its_chapters(config):
    position(config, book="hb2", chapter=1)
    question = "What happens in Hollow Keep?"
    _answer, calls = await ask(config, question=question, sub_questions=[question])
    assert "MARK-hb2-1" in prompt_of(calls)
    assert "MARK-hb2-2" not in prompt_of(calls) and "MARK-hb2-3" not in prompt_of(calls)


async def test_the_baseline_answer_respects_the_position_too(config):
    position(config, chapter=2)
    answer, calls = await ask(config, baseline=True)
    assert {p.chunk_id for p in answer.passages} <= {"hb1:1:0", "hb1:2:0"}
    assert "SECRETFACT" not in prompt_of(calls)


# --- the note, other worlds and the bypass ------------------------------------------------------

async def test_other_worlds_are_answered_in_full_and_the_note_names_the_limited_one(config):
    position(config, chapter=2)
    answer, calls = await ask(config, world=None, question="Who is Zed Quill?")
    assert "MARK-ob1-1" in prompt_of(calls)  # Other Realm has no position
    assert answer.position_note == "Answered from what you've read: up to chapter 2 of Hollow Gate"
    both = await ask(config, world=None, question=QUESTION)
    assert "SECRETFACT" not in prompt_of(both[1])


async def test_a_world_without_a_position_carries_no_note(config):
    position(config, chapter=2)
    answer, _ = await ask(config, world=OTHER, question="Who is Zed Quill?")
    assert answer.position_note is None


async def test_spoilers_bypasses_the_position_for_one_question(config):
    position(config, chapter=2)
    answer, calls = await ask(config, spoilers=True)
    assert "SECRETFACT" in prompt_of(calls) and answer.position_note is None
    again, calls = await ask(config)
    assert "SECRETFACT" not in prompt_of(calls) and again.position_note


async def test_a_disabled_position_is_no_position(config):
    position(config, chapter=1, enabled=False)
    answer, calls = await ask(config)
    assert "SECRETFACT" in prompt_of(calls) and answer.position_note is None


async def test_the_result_carries_the_note_only_when_a_position_applies(config):
    models, _ = scripted_models()
    plain = await tools.lore_search(config, QUESTION, models=models, world=HOLLOW)
    assert "position_note" not in plain
    position(config, chapter=2)
    models, _ = scripted_models()
    limited = await tools.lore_search(config, QUESTION, models=models, world=HOLLOW)
    assert limited["position_note"].endswith("up to chapter 2 of Hollow Gate")
    models, _ = scripted_models()
    bypass = await tools.lore_search(config, QUESTION, models=models, world=HOLLOW, spoilers=True)
    assert "position_note" not in bypass and "SECRETFACT" in str(bypass["passages"])


# --- deep answers ----------------------------------------------------------------------------------

async def run_deep(config, question="Tell me everything about this world."):
    scripted = Scripted(Decomposition(sub_questions=[PartSpec(question=question)], depth="deep"),
                        planner=DeepPlan(overview="All of it.", sections=[
                            PlanSection(heading="The people", focus="Who the people are",
                                        entities=["h_ren", "h_mira", "h_dorn", "w_pip"])]))
    events: list[dict] = []
    with open_stores(config) as s:
        answer = await answer_question(s.conn, s.kuzu, s.lance, question, scripted.models(),
                                       world=HOLLOW, on_event=events.append, depth="deep")
    return answer, events, scripted


async def test_the_deep_planner_draws_candidates_only_from_what_has_been_read(config):
    position(config, chapter=2)
    answer, events, scripted = await run_deep(config)
    plan = "\n".join(scripted.prompts("plan"))
    assert scripted.prompts("plan")
    assert "Old Tam" in plan and "Mira Cole" in plan
    for late in ("Ren Ashby", "Dorn Vale", "Pip Marsh", "WHOLESTORYSUMMARY"):
        assert late not in plan, late
    for prompt in scripted.prompts("claims") + scripted.prompts("write"):
        for late in LATE:
            assert late not in prompt, late
    assert answer.position_note.endswith("up to chapter 2 of Hollow Gate")
    shown = str([e for e in events if e["type"] in ("plan", "seeds", "edges", "section")])
    for late in ("Ren Ashby", "Dorn Vale", "Pip Marsh", "SECRETFACT"):
        assert late not in shown, late


async def test_the_deep_planner_sees_everything_without_a_position(config):
    _answer, _events, scripted = await run_deep(config)
    plan = "\n".join(scripted.prompts("plan"))
    assert "Ren Ashby" in plan and "Dorn Vale" in plan and "WHOLESTORYSUMMARY" in plan
    assert "Pip Marsh" in plan


async def test_a_deep_question_about_one_entity_plans_from_its_visible_facets(config):
    position(config, chapter=2)
    question = "Tell me everything about Old Tam."
    answer, _events, scripted = await run_deep(config, question)
    plan = "\n".join(scripted.prompts("plan"))
    assert "Old Tam" in plan and "EARLYSTATE" in plan
    for late in ("Ren Ashby", "LATESTATE", "SEQUELSTATE", "WHOLESTORYSUMMARY", "Dorn Vale"):
        assert late not in plan, late
