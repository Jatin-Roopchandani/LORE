"""Answers that say a book is still being processed.

A book gets its chunk vectors and its graph only when processing finishes (the
load stage), so until then nothing in it can be found. A question whose scope
holds such a book, and whose answer comes back empty, says which book, how far
its processing got, when it continues, and to ask again when it is ready; the
detail is on the answer as `processing`. No model is asked for any of it.
With every book processed nothing differs (the goldens pin that).

Real SQLite, Kuzu and LanceDB over tests/worlds_fixtures.py; scripted models.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from lore.db.sqlite import open_db
from lore.mcp import tools
from lore.mcp.stores import open_stores
from lore.orchestrator import jobs
from lore.query import agent as agent_module
from lore.query.agent import answer_question
from lore.query.models import Claim, ProcessingBook
from lore.query.synthesize import REFUSAL, Synthesis
from lore.worlds import create_world
from tests.worlds_fixtures import ALPHA, BETA, build_library, scripted_models

NOW = datetime(2026, 11, 17, 20, 0, tzinfo=timezone.utc)
RESUME = datetime(2026, 11, 18, 8, 0, tzinfo=timezone.utc)
BIG = "Les Misérables"


@pytest.fixture
def config(tmp_path):
    return build_library(tmp_path)


def add_book(config, book_id="big", title=BIG, world=None, status="chunked") -> int | None:
    """An uploaded book that has not reached the load stage: rows in SQLite,
    nothing in LanceDB or the graph. `world=None`: a world of its own."""
    conn = open_db(config.sqlite_path)
    try:
        wid = (create_world(conn, title) if world is None else
               conn.execute("SELECT id FROM worlds WHERE name = ?", (world,)).fetchone()[0])
        conn.execute("INSERT INTO books (id, title, file_path, status, world_id)"
                     " VALUES (?, ?, 'x.epub', ?, ?)", (book_id, title, status, wid))
        conn.commit()
        return wid
    finally:
        conn.close()


def waiting(config, book_id="big", done=969, total=1051):
    conn = open_db(config.sqlite_path)
    try:
        jobs.start_job(conn, book_id, "extract", total, now=NOW)
        jobs.advance(conn, book_id, done, now=NOW)
        jobs.stop_quota(conn, book_id, "Today's allowance is used up.", now=NOW, resume_at=RESUME)
    finally:
        conn.close()


def running(config, book_id="big", done=12, total=1051, now=None):
    conn = open_db(config.sqlite_path)
    try:
        jobs.start_job(conn, book_id, "extract", total, now=now or datetime.now(timezone.utc))
        jobs.advance(conn, book_id, done, now=now or datetime.now(timezone.utc))
    finally:
        conn.close()


async def ask(config, world, synthesis=None, question="Who is Zed Quill?"):
    models, calls = scripted_models(synthesis=synthesis)
    with open_stores(config) as s:
        args = {} if world is None else {"world": world}
        answer = await answer_question(s.conn, s.kuzu, s.lance, question, models, **args)
    return answer, calls


# --- a world whose only book is still processing ---------------------------------------

async def test_a_world_of_one_unprocessed_book_says_it_is_still_being_processed(config):
    wid = add_book(config)
    waiting(config)
    answer, calls = await ask(config, wid)
    assert answer.mode == "empty" and answer.claims == ()
    assert answer.text == (
        "Les Misérables is still being processed (969 of 1,051 parts read), so it can't be"
        " searched yet. It continues automatically after 08:00 UTC. Ask again when it's ready.")
    assert answer.processing == (ProcessingBook(
        book_id="big", title=BIG, stage="extract", state="waiting_quota", done=969,
        total=1051, resumable_at="2026-11-18T08:00:00+00:00"),)
    # a data lookup: no model was asked
    assert calls["select"] == [] and calls["synthesize"] == [] and calls["decompose"] == []


async def test_a_running_book_is_processing_now(config):
    wid = add_book(config)
    running(config)
    answer, _ = await ask(config, wid)
    assert answer.text == (
        "Les Misérables is still being processed (12 of 1,051 parts read), so it can't be"
        " searched yet. It is processing now. Ask again when it's ready.")
    assert answer.processing[0].state == "running"


async def test_a_running_job_with_no_heartbeat_is_paused_not_processing(config):
    wid = add_book(config)
    running(config, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    answer, _ = await ask(config, wid)
    assert "It is paused: press Continue in the Library." in answer.text
    assert answer.processing[0].state == "idle"


async def test_a_book_with_no_job_yet_has_not_been_processed(config):
    wid = add_book(config, status="registered")
    answer, _ = await ask(config, wid)
    assert answer.text == ("Les Misérables hasn't been processed yet, so it can't be searched."
                           " Ask again when it's ready.")
    assert answer.processing == (ProcessingBook(
        book_id="big", title=BIG, stage=None, state="not_started", done=None, total=None,
        resumable_at=None),)


@pytest.mark.parametrize("stop, said", [
    (lambda c: jobs.stop_key(c, "big", "bad key", now=NOW),
     "It is paused until your AI provider's key is fixed in Settings."),
    (lambda c: jobs.fail(c, "big", "boom", now=NOW),
     "Its processing stopped with an error: press Retry in the Library."),
])
async def test_a_paused_or_failed_book_says_what_to_do(config, stop, said):
    wid = add_book(config)
    conn = open_db(config.sqlite_path)
    try:
        jobs.start_job(conn, "big", "extract", 1051, now=NOW)
        jobs.advance(conn, "big", 40, now=NOW)
        stop(conn)
    finally:
        conn.close()
    answer, _ = await ask(config, wid)
    assert "(40 of 1,051 parts read)" in answer.text and said in answer.text


async def test_two_unprocessed_books_are_both_named(config):
    wid = add_book(config)
    waiting(config)
    conn = open_db(config.sqlite_path)
    try:
        conn.execute("INSERT INTO books (id, title, file_path, status, world_id)"
                     " VALUES ('big2', 'Ninety-Three', 'y.epub', 'registered', ?)", (wid,))
        conn.commit()
    finally:
        conn.close()
    answer, _ = await ask(config, wid)
    assert answer.text.startswith("Les Misérables is still being processed")
    assert "Ninety-Three hasn't been processed yet, so it can't be searched." in answer.text
    assert answer.text.endswith("Ask again when they're ready.")
    assert [b.book_id for b in answer.processing] == ["big", "big2"]


# --- a world mixing ready and unready books -------------------------------------------

async def test_an_empty_answer_in_a_mixed_world_says_the_ready_books_had_nothing(config):
    add_book(config, world=BETA)
    waiting(config)
    answer, calls = await ask(config, BETA)
    assert calls["synthesize"]  # the ready book was searched as ever
    assert answer.claims == ()
    assert answer.text == (
        "The books that are ready don't contain enough to answer that. Les Misérables is"
        " still being processed (969 of 1,051 parts read), so it can't be searched yet."
        " It continues automatically after 08:00 UTC. Ask again when it's ready.")


async def test_an_answer_from_the_ready_books_keeps_its_text_and_lists_the_unready_one(config):
    found = Synthesis(can_answer=True, claims=[
        Claim(text="Zed Quill walked with Yan Vale.", passage=1, quote="walked on with Yan Vale")])
    plain, _ = await ask(config, BETA, synthesis=found)
    assert plain.claims and plain.processing == ()
    add_book(config, world=BETA)
    waiting(config)
    answer, _ = await ask(config, BETA, synthesis=found)
    assert answer.text == plain.text and answer.claims == plain.claims
    assert [b.book_id for b in answer.processing] == ["big"]


async def test_all_worlds_names_the_unprocessed_book_of_any_world(config):
    add_book(config)
    waiting(config)
    answer, _ = await ask(config, "all")
    assert [b.book_id for b in answer.processing] == ["big"]


async def test_another_worlds_unprocessed_book_is_not_mentioned(config):
    add_book(config)  # its own world
    waiting(config)
    answer, _ = await ask(config, ALPHA)
    assert answer.processing == () and "Les Misérables" not in answer.text


async def test_a_book_marked_unprocessed_but_searchable_is_not_flagged(config):
    # The fixture's books are 'registered' yet have their vectors: they can be searched.
    answer, _ = await ask(config, BETA)
    assert answer.processing == () and answer.text == REFUSAL


async def test_the_switch_off_leaves_the_answer_as_it_was(config, monkeypatch):
    wid = add_book(config)
    waiting(config)
    monkeypatch.setattr(agent_module, "PROCESSING_CHECK", False)
    answer, _ = await ask(config, wid)
    assert answer.text == REFUSAL and answer.processing == ()


# --- lore_search --------------------------------------------------------------------------

def _search(config, monkeypatch, world):
    models, _ = scripted_models()
    monkeypatch.setattr(tools, "_build_models", lambda c: models)
    monkeypatch.setattr(tools, "_check_synthesis_budget", lambda c: None)
    import asyncio
    return asyncio.run(tools.lore_search(config, "Who is Zed Quill?", world=world))


def test_lore_search_carries_processing_only_when_a_book_is_unready(config, monkeypatch):
    assert "processing" not in _search(config, monkeypatch, BETA)
    wid = add_book(config)
    waiting(config)
    out = _search(config, monkeypatch, str(wid))
    assert out["answer"].startswith("Les Misérables is still being processed")
    assert out["processing"] == [{
        "book_id": "big", "title": BIG, "stage": "extract", "state": "waiting_quota",
        "done": 969, "total": 1051, "resumable_at": "2026-11-18T08:00:00+00:00"}]
