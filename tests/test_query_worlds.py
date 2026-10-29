"""Questions are answered inside a world; "all worlds" searches each on its own.

Real SQLite, Kuzu and LanceDB over a synthetic two-world library
(tests/worlds_fixtures.py); the models are scripted and no test reaches one.
"""
from __future__ import annotations

import pytest

from lore.mcp.stores import open_stores
from lore.query import agent as agent_module
from lore.query.agent import answer_question
from lore.query.scope import UnknownWorld, resolve_scopes
from tests.worlds_fixtures import ALPHA, BETA, build_library, scripted_models

QUESTION = "Who is Zed Quill?"


async def ask(config, world="unset", *, question=QUESTION, sub_questions=None, **kw):
    models, calls = scripted_models(sub_questions)
    with open_stores(config) as s:
        args = {} if world == "unset" else {"world": world}
        answer = await answer_question(s.conn, s.kuzu, s.lance, question, models,
                                       **args, **kw)
    return answer, calls


def passage_books(answer):
    return {p.book_id for p in answer.passages}


# --- a question in one world ---------------------------------------------------

@pytest.mark.parametrize("world, mine, other, seed, marker, foreign", [
    (ALPHA, {"ba1", "ba2"}, {"bb1"}, "a_zed", "ALPHAMARK", "BETAMARK"),
    (BETA, {"bb1"}, {"ba1", "ba2"}, "b_zed", "BETAMARK", "ALPHAMARK"),
])
async def test_a_world_never_returns_the_other_worlds_evidence(
        tmp_path, world, mine, other, seed, marker, foreign) -> None:
    config = build_library(tmp_path)
    answer, calls = await ask(config, world)
    # Seeds and select candidates: only this world's Zed Quill.
    prefix = seed[0]  # entity ids start with their world's letter
    assert seed in {s.id for s in answer.seeds}
    assert {s.id[0] for s in answer.seeds} == {prefix}
    assert len(calls["select"]) == 1
    assert calls["select"][0].count("Zed Quill (") == 1
    # Graph evidence and passages.
    assert answer.edges and {e.book_id for e in answer.edges} <= mine
    assert {e.subject_id[0] for e in answer.edges} | {e.object_id[0] for e in answer.edges} == {prefix}
    prompt = calls["synthesize"][0]
    assert marker in prompt and foreign not in prompt
    # The timeline's beats are this world's.
    assert ("ALPHABEAT" in prompt) == (seed == "a_zed")
    assert ("BETABEAT" in prompt) == (seed == "b_zed")


async def test_vector_and_keyword_candidates_stay_in_the_world(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(agent_module, "KEYWORD_K", 6)
    config = build_library(tmp_path)
    for world, foreign in ((ALPHA, "BETAMARK"), (BETA, "ALPHAMARK")):
        _answer, calls = await ask(config, world, question="the traveller walked on hills")
        assert foreign not in calls["synthesize"][0]


async def test_the_decomposition_only_lists_the_worlds_books(tmp_path) -> None:
    config = build_library(tmp_path)
    _a, calls = await ask(config, ALPHA, sub_questions=[QUESTION])
    assert "Alpha One" in calls["decompose"][0] and "Alpha Two" in calls["decompose"][0]
    assert "Beta Solo" not in calls["decompose"][0]
    _a, calls = await ask(config, BETA, sub_questions=[QUESTION])
    assert "Beta Solo" in calls["decompose"][0] and "Alpha" not in calls["decompose"][0]


async def test_a_book_of_another_world_in_the_question_is_not_searched(tmp_path) -> None:
    config = build_library(tmp_path)
    _a, calls = await ask(config, ALPHA, question="Who is Zed Quill in Beta Solo?")
    assert "BETAMARK" not in calls["synthesize"][0]


async def test_world_by_id_or_name_in_any_case(tmp_path) -> None:
    config = build_library(tmp_path)
    with open_stores(config, kuzu=False, lance=False) as s:
        world_id = resolve_scopes(s.conn, BETA)[0].world_id
    for ref in (world_id, str(world_id), BETA.upper()):
        answer, _c = await ask(config, ref)
        assert {s.id[0] for s in answer.seeds} == {"b"}


async def test_an_unknown_world_is_refused(tmp_path) -> None:
    config = build_library(tmp_path)
    with pytest.raises(UnknownWorld, match="Gamma"):
        await ask(config, "Gamma")


# --- all worlds -----------------------------------------------------------------

async def test_all_worlds_keeps_same_named_entities_distinct(tmp_path) -> None:
    config = build_library(tmp_path)
    answer, calls = await ask(config)  # no world, two worlds: all of them
    assert {"a_zed", "b_zed"} <= {s.id for s in answer.seeds}
    # Each world selects on its own: never two "Zed Quill" in one choice.
    assert len(calls["select"]) == 2
    assert all(p.count("Zed Quill (") == 1 for p in calls["select"])
    # No edge joins one world's entity to the other's.
    assert answer.edges
    assert {(e.subject_id[0], e.object_id[0]) for e in answer.edges} <= {("a", "a"), ("b", "b")}
    assert {"a", "b"} == {e.subject_id[0] for e in answer.edges}


async def test_all_worlds_pools_evidence_and_names_the_books(tmp_path) -> None:
    config = build_library(tmp_path)
    answer, calls = await ask(config)
    prompt = calls["synthesize"][0]
    assert "ALPHAMARK" in prompt and "BETAMARK" in prompt
    # Each fact is labelled with its book, which is how the writer names it.
    assert "(Alpha One," in prompt and "(Beta Solo," in prompt


async def test_all_worlds_and_an_explicit_all_are_the_same(tmp_path) -> None:
    config = build_library(tmp_path)
    implicit, _ = await ask(config)
    explicit, _ = await ask(config, "all")
    assert implicit == explicit


async def test_a_part_naming_a_book_runs_only_in_its_world(tmp_path) -> None:
    config = build_library(tmp_path)
    answer, calls = await ask(config, question="Who is Zed Quill in Beta Solo?",
                              sub_questions=["Who is Zed Quill in Beta Solo?"])
    assert len(calls["select"]) == 1
    assert {s.id[0] for s in answer.seeds} == {"b"}
    assert "ALPHAMARK" not in calls["synthesize"][0]


async def test_a_world_with_nothing_near_the_question_is_left_out(tmp_path) -> None:
    config = build_library(tmp_path, beta_axis=9)  # Beta's chunks point elsewhere
    answer, calls = await ask(config)
    assert {s.id[0] for s in answer.seeds} == {"a"}
    assert len(calls["select"]) == 1
    assert "BETAMARK" not in calls["synthesize"][0]


async def test_per_part_events_are_one_set_across_worlds(tmp_path) -> None:
    config = build_library(tmp_path)
    events = []
    await ask(config, on_event=events.append)
    kinds = [(e["type"], e.get("name"), e.get("state")) for e in events]
    assert kinds.count(("step", "seeds", "start")) == 1
    assert kinds.count(("step", "search", "start")) == 1
    seeds = [e for e in events if e["type"] == "seeds"]
    assert len(seeds) == 1 and {"a_zed", "b_zed"} <= {s["id"] for s in seeds[0]["entities"]}


# --- one world behaves as before --------------------------------------------------

async def test_with_one_world_nothing_changes(tmp_path) -> None:
    pre_worlds = build_library(tmp_path / "pre", worlds=False)
    one = build_library(tmp_path / "one", one_world=True)
    before, calls_before = await ask(pre_worlds)
    for world in ("unset", ALPHA, "all"):
        after, calls_after = await ask(one, world)
        assert after == before
        assert calls_after == calls_before


async def test_with_one_world_the_scope_filters_nothing(tmp_path) -> None:
    config = build_library(tmp_path, one_world=True)
    with open_stores(config, kuzu=False, lance=False) as s:
        scopes = resolve_scopes(s.conn, None)
    assert len(scopes) == 1 and scopes[0].books is None and scopes[0].world == ALPHA


async def test_a_library_without_worlds_is_unscoped(tmp_path) -> None:
    config = build_library(tmp_path, worlds=False)
    with open_stores(config, kuzu=False, lance=False) as s:
        assert [x.books for x in resolve_scopes(s.conn, None)] == [None]
        with pytest.raises(UnknownWorld):
            resolve_scopes(s.conn, "Anything")


async def test_the_baseline_stays_in_the_world(tmp_path) -> None:
    config = build_library(tmp_path)
    answer, calls = await ask(config, ALPHA, baseline=True)
    assert passage_books(answer) <= {"ba1", "ba2"} and passage_books(answer)
    assert "BETAMARK" not in calls["synthesize"][0]
