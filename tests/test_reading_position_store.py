"""The stored reading position: one row per world."""
import sqlite3

import pytest

from lore.db.sqlite import open_db
from lore.reading_position import (
    ReadingPosition, delete_position, enabled_positions, get_position, set_position,
)
from lore.worlds import create_world, delete_world


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "lore.db")
    yield c
    c.close()


def add_book(conn, book_id, world_id):
    conn.execute("INSERT INTO books (id, title, file_path, world_id) VALUES (?, ?, 'x', ?)",
                 (book_id, book_id.upper(), world_id))
    conn.commit()


def test_no_position_until_one_is_set(conn):
    wid = create_world(conn, "W")
    assert get_position(conn, wid) is None
    assert enabled_positions(conn) == {}


def test_a_position_round_trips_and_is_replaced(conn):
    wid = create_world(conn, "W")
    add_book(conn, "b1", wid)
    add_book(conn, "b2", wid)
    first = set_position(conn, ReadingPosition(wid, "b1", 4, ("b2", "b2"), True))
    assert (first.book_id, first.chapter, first.read_books, first.enabled) == ("b1", 4, ("b2",), True)
    assert first.updated_at
    second = set_position(conn, ReadingPosition(wid, "b2", 0, (), False))
    assert get_position(conn, wid) == second
    assert (second.book_id, second.chapter, second.enabled) == ("b2", 0, False)
    assert conn.execute("SELECT COUNT(*) FROM reading_positions").fetchone()[0] == 1


def test_only_enabled_positions_are_listed(conn):
    a, b = create_world(conn, "A"), create_world(conn, "B")
    add_book(conn, "a1", a)
    add_book(conn, "b1", b)
    set_position(conn, ReadingPosition(a, "a1", 2, (), True))
    set_position(conn, ReadingPosition(b, "b1", 2, (), False))
    assert set(enabled_positions(conn)) == {a}


def test_deleting_a_world_or_a_book_leaves_no_dangling_position(conn):
    wid = create_world(conn, "W")
    add_book(conn, "b1", wid)
    set_position(conn, ReadingPosition(wid, "b1", 3, (), True))
    conn.execute("DELETE FROM books WHERE id = 'b1'")
    conn.commit()
    after = get_position(conn, wid)
    assert after is not None and after.book_id is None
    assert enabled_positions(conn) == {}  # no book: nothing to apply
    delete_world(conn, wid)
    assert get_position(conn, wid) is None


def test_delete_position_and_an_unmigrated_library(conn):
    wid = create_world(conn, "W")
    add_book(conn, "b1", wid)
    set_position(conn, ReadingPosition(wid, "b1", 1, (), True))
    delete_position(conn, wid)
    assert get_position(conn, wid) is None
    conn.execute("DROP TABLE reading_positions")
    assert get_position(conn, wid) is None and enabled_positions(conn) == {}
