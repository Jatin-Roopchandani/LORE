"""The reading progress list: each book finished (the default), in progress up
to a chapter, or not started; the series order fills itself in; migration 18
turns every older position into the list that shows exactly what it showed."""
from __future__ import annotations


import pytest
from fastapi.testclient import TestClient

from lore.db.sqlite import open_db, schema_version
from lore.query import visible
from lore.query.models import BookInfo
from lore.query.scope import resolve_scopes
from lore.query.visible import FINISHED, NOT_STARTED, READING, sees
from lore.reading_position import (
    Progress, ReadingPosition, get_position, progress_position, set_position,
)
from lore.web.app import create_app
from lore.worlds import create_world
from tests.position_fixtures import HOLLOW, build_position_library

INFOS = {"b1": BookInfo("One", "Saga", 1.0), "b2": BookInfo("Two", "Saga", 2.0),
         "b3": BookInfo("Three", "Saga", 3.0), "x": BookInfo("Standalone"),
         "y": BookInfo("Other series", "Elsewhere", 1.0), "d1": BookInfo("Dup A", "Dup", 1.0),
         "d2": BookInfo("Dup B", "Dup", 1.0)}
WORLD = frozenset(INFOS)


def states(progress: Progress) -> dict[str, str]:
    return {b: s.state for b, s in visible.book_states(WORLD, progress, INFOS).items()}


def made(progress: Progress):
    return visible.visible_books(WORLD, progress_position(1, progress), INFOS)


# --- the state mapping ---------------------------------------------------------------

def test_every_book_is_finished_by_default_and_nothing_is_limited():
    assert set(states(Progress()).values()) == {FINISHED}
    assert made(Progress()) is None
    assert progress_position(1, Progress()).enabled is False


def test_a_book_in_progress_is_capped_and_a_book_not_started_is_hidden():
    books, view = made(Progress.of({"x": 4}, ["y"]))
    assert sees(books, "x", 4) and not sees(books, "x", 5)
    assert not sees(books, "y", 1)
    assert sees(books, "b3", 99) and sees(books, "d2", 99)  # finished
    assert (view.book_id, view.chapter) == ("x", 4)


def test_a_book_in_progress_keeps_the_later_books_of_its_series_not_started():
    got = visible.book_states(WORLD, Progress.of({"b2": 3}), INFOS)
    assert (got["b1"].state, got["b2"].state, got["b3"].state) == (FINISHED, READING, NOT_STARTED)
    assert got["b3"].forced and not got["b1"].forced
    books, _ = made(Progress.of({"b2": 3}))
    assert not sees(books, "b3", 1) and sees(books, "b1", 99)
    # books of unknown order (another series, a shared index, none) are independent
    assert {b: states(Progress.of({"d1": 1}))[b] for b in ("d2", "x", "y")} == \
        {"d2": FINISHED, "x": FINISHED, "y": FINISHED}


def test_several_books_of_unknown_order_may_be_in_progress_at_once():
    books, view = made(Progress.of({"x": 2, "y": 5}))
    assert books.caps == {"x": 2, "y": 5}
    assert {view.book_id, *(o.book_id for o in view.others)} == {"x", "y"}
    assert view.note().count("up to chapter") == 2


def test_with_no_book_in_progress_the_note_names_the_finished_books():
    books, view = made(Progress.of({}, ["b1", "b2", "b3", "d1", "d2"]))
    assert set(books) == {"x", "y"} and not books.capped
    assert view.book_id is None
    assert view.note() == "Answered from what you've read: Other series and Standalone"
    _, nothing = made(Progress.of({}, list(WORLD)))
    assert nothing.note() == "Answered from what you've read: no books yet"


# --- series order fills itself in -----------------------------------------------------

def test_a_later_book_set_reading_or_finished_marks_the_earlier_ones_finished():
    start = Progress.of({}, ["b1", "b2", "b3"])
    after, filled = visible.change_progress(WORLD, start, INFOS, "b3", READING, 2)
    assert [states(after)[b] for b in ("b1", "b2", "b3")] == [FINISHED, FINISHED, READING]
    assert filled == {"b1": FINISHED, "b2": FINISHED}
    after, filled = visible.change_progress(WORLD, start, INFOS, "b2", FINISHED)
    assert (states(after)["b1"], states(after)["b3"]) == (FINISHED, NOT_STARTED)
    assert filled == {"b1": FINISHED}


def test_an_earlier_book_set_not_started_or_reading_marks_the_later_ones_not_started():
    after, filled = visible.change_progress(WORLD, Progress(), INFOS, "b2", NOT_STARTED)
    assert (states(after)["b1"], states(after)["b2"], states(after)["b3"]) == \
        (FINISHED, NOT_STARTED, NOT_STARTED)
    assert filled == {"b3": NOT_STARTED}
    after, filled = visible.change_progress(WORLD, Progress(), INFOS, "b1", READING, 1)
    assert states(after)["b2"] == states(after)["b3"] == NOT_STARTED
    assert filled == {"b2": NOT_STARTED, "b3": NOT_STARTED}
    assert after.chapters == {"b1": 1}


def test_books_of_unknown_order_are_never_filled_in():
    after, filled = visible.change_progress(WORLD, Progress(), INFOS, "x", NOT_STARTED)
    assert filled == {} and states(after)["x"] == NOT_STARTED
    after, filled = visible.change_progress(WORLD, Progress(), INFOS, "d2", READING, 1)
    assert filled == {} and states(after)["d1"] == FINISHED


def test_finishing_the_book_in_progress_leaves_the_later_ones_not_started():
    reading, _ = visible.change_progress(WORLD, Progress(), INFOS, "b1", READING, 3)
    done, filled = visible.change_progress(WORLD, reading, INFOS, "b1", FINISHED)
    assert states(done)["b2"] == NOT_STARTED and filled == {}
    assert made(done)[0].caps == {}


# --- the older shape, converted faithfully --------------------------------------------

def _same(a, b) -> bool:
    if a is None or b is None:
        return a is b
    return set(a[0]) == set(b[0]) and a[0].caps == b[0].caps


@pytest.mark.parametrize("book,chapter,read,enabled", [
    (book, chapter, read, enabled)
    for book in [None, "b1", "b2", "b3", "x", "y", "d1", "gone"]
    for chapter in (0, 3)
    for read in [(), ("x",), ("y", "d2", "b3"), tuple(WORLD)]
    for enabled in (True, False)])
def test_the_older_position_and_its_progress_list_show_exactly_the_same(book, chapter, read, enabled):
    old = ReadingPosition(1, book, chapter, read, enabled)
    progress = visible.legacy_progress(old, WORLD, INFOS)
    assert _same(visible.visible_books(WORLD, old, INFOS),
                 visible.visible_books(WORLD, progress_position(1, progress), INFOS))


def _old_library(tmp_path, rows):
    """A library at migration 17 holding positions of the older shape."""
    path = tmp_path / "lore.db"
    conn = open_db(path)
    w = create_world(conn, "Saga")
    for book_id, info in INFOS.items():
        conn.execute("INSERT INTO books (id, title, file_path, series, series_index, world_id)"
                     " VALUES (?, ?, 'x', ?, ?, ?)",
                     (book_id, info.title, info.series, info.series_index, w))
    conn.commit()
    worlds = [w] + [create_world(conn, f"W{i}") for i in range(len(rows) - 1)]
    for wid, (book, chapter, read, enabled) in zip(worlds, rows):
        set_position(conn, ReadingPosition(wid, book, chapter, read, enabled))
    with conn:
        conn.execute("DELETE FROM schema_migrations WHERE version = 18")
        conn.execute("ALTER TABLE reading_positions DROP COLUMN progress")
    conn.close()
    return path, worlds


def test_migration_18_turns_each_older_position_into_its_progress_list(tmp_path):
    path, (w, off, gone) = _old_library(tmp_path, [("b2", 4, ("x",), True),
                                                   ("b1", 2, (), False),
                                                   (None, 0, (), True)])
    conn = open_db(path)
    try:
        assert schema_version(conn) >= 18
        on = get_position(conn, w)
        assert on.progress == Progress.of({"b2": 4}, ["b3", "d1", "d2", "y"])
        assert on.enabled and (on.book_id, on.chapter) == ("b2", 4)
        # "limit off" showed everything: all finished, nothing limited
        for wid in (off, gone):
            stored = get_position(conn, wid)
            assert stored.progress == Progress() and not stored.enabled
        scope = resolve_scopes(conn, "Saga")[0]
        assert set(scope.books) == {"b1", "b2", "x"} and scope.books.caps == {"b2": 4}
    finally:
        conn.close()


def test_a_migrated_library_opens_again_unchanged(tmp_path):
    path, (w,) = _old_library(tmp_path, [("b1", 1, (), True)])
    open_db(path).close()
    conn = open_db(path)
    try:
        assert get_position(conn, w).progress == Progress.of({"b1": 1}, ["b2", "b3", "x", "y", "d1", "d2"])
    finally:
        conn.close()


# --- a new book --------------------------------------------------------------------------

def test_a_new_book_is_finished_unless_its_series_is_being_read(tmp_path):
    conn = open_db(tmp_path / "lore.db")
    try:
        w = create_world(conn, "Saga")
        for book_id in ("b1", "b2"):
            info = INFOS[book_id]
            conn.execute("INSERT INTO books (id, title, file_path, series, series_index, world_id)"
                         " VALUES (?, ?, 'x', ?, ?, ?)", (book_id, info.title, info.series,
                                                         info.series_index, w))
        conn.commit()
        set_position(conn, progress_position(w, Progress.of({"b1": 2})))
        for book_id in ("x", "b3"):  # uploaded later
            info = INFOS[book_id]
            conn.execute("INSERT INTO books (id, title, file_path, series, series_index, world_id)"
                         " VALUES (?, ?, 'x', ?, ?, ?)", (book_id, info.title, info.series,
                                                         info.series_index, w))
        conn.commit()
        books = resolve_scopes(conn, "Saga")[0].books
        assert sees(books, "x", 99)        # a new book: finished
        assert not sees(books, "b3", 1)    # but never past the book being read
    finally:
        conn.close()


# --- the endpoint ------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(build_position_library(tmp_path)), base_url="http://127.0.0.1:8766")


def change(client, book, state, chapter=None):
    body = {"change": {"book_id": book, "state": state, **({} if chapter is None else {"chapter": chapter})}}
    r = client.put("/api/reading-position", params={"world": HOLLOW}, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_the_endpoint_lists_every_book_finished_until_told_otherwise(client):
    got = client.get("/api/reading-position", params={"world": HOLLOW}).json()
    assert [(b["id"], b["state"]) for b in got["books"]] == \
        [("hb1", FINISHED), ("hb2", FINISHED), ("hb3", FINISHED)]
    assert got["enabled"] is False and got["filled"] == []


def test_a_change_fills_in_the_series_and_says_so(client):
    got = change(client, "hb1", READING, 2)
    assert [(b["id"], b["state"], b["progress_chapter"]) for b in got["books"]] == \
        [("hb1", READING, 2), ("hb2", NOT_STARTED, 0), ("hb3", FINISHED, 0)]
    assert got["filled"] == [{"id": "hb2", "state": NOT_STARTED}]
    assert (got["enabled"], got["book_id"], got["chapter"], got["read_books"]) == (True, "hb1", 2, ["hb3"])
    # and the reads apply it
    text = client.get("/api/overview", params={"world": HOLLOW}).text
    assert "Dorn Vale" not in text
    got = change(client, "hb2", FINISHED)
    assert [b["state"] for b in got["books"]] == [FINISHED, FINISHED, FINISHED]
    assert got["enabled"] is False


def test_reading_with_no_chapter_starts_at_the_first_story_chapter(client):
    got = change(client, "hb3", READING)
    assert got["books"][2]["progress_chapter"] == 1


def test_a_bad_change_is_refused(client):
    for body in ({"change": {"book_id": "nope", "state": READING}},
                 {"change": {"book_id": "hb1", "state": READING, "chapter": 99}},
                 {"change": {"book_id": "hb1", "state": "halfway"}}):
        r = client.put("/api/reading-position", params={"world": HOLLOW}, json=body)
        assert r.status_code in (400, 422), body
