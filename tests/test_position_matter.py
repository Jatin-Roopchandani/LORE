"""The reading position counts story chapters only, and names them.

Over tests/position_fixtures.py, with Hollow Gate's first section classified
as a contents page: the book has two story chapters, "The Road" (chapter_seq
2) and "The Secret" (3). A stored position keeps its chapter_seq, so a
position stored before the classification still means the same place.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lore.db.sqlite import open_db
from lore.ingest.matter import FRONT, STORY, Classified
from lore.ingest.registry import save_sections
from lore.query.scope import resolve_scopes
from lore.query.visible import PositionView, position_note
from lore.reading_position import ReadingPosition, set_position
from lore.web.app import create_app
from tests.position_fixtures import HOLLOW, build_position_library


@pytest.fixture
def config(tmp_path):
    config = build_position_library(tmp_path)
    conn = open_db(config.sqlite_path)
    save_sections(conn, "hb1", [Classified(1, FRONT, "contents", "Contents"),
                                Classified(2, STORY, "", "The Road"),
                                Classified(3, STORY, "", "The Secret")])
    conn.close()
    return config


def _store(config, chapter: int, book: str = "hb1") -> None:
    conn = open_db(config.sqlite_path)
    world = conn.execute("SELECT id FROM worlds WHERE name = ?", (HOLLOW,)).fetchone()[0]
    set_position(conn, ReadingPosition(world, book, chapter, (), True))
    conn.close()


def test_the_api_lists_story_chapters_with_their_headings(config):
    client = TestClient(create_app(config), base_url="http://127.0.0.1:8766")
    got = client.get("/api/reading-position", params={"world": HOLLOW}).json()
    hb1 = next(b for b in got["books"] if b["id"] == "hb1")
    assert hb1["chapters"] == 2
    assert hb1["sections"] == [{"seq": 2, "number": 1, "heading": "The Road"},
                               {"seq": 3, "number": 2, "heading": "The Secret"}]
    # An unclassified book counts its chapters, unnamed.
    hb2 = next(b for b in got["books"] if b["id"] == "hb2")
    assert hb2["chapters"] == 3 and hb2["sections"][0] == {"seq": 1, "number": 1, "heading": None}
    # The stored value is a chapter_seq, as before.
    r = client.put("/api/reading-position", params={"world": HOLLOW},
                   json={"enabled": True, "book_id": "hb1", "chapter": 2, "read_books": []})
    assert r.status_code == 200 and r.json()["chapter"] == 2


def test_the_answer_note_counts_and_names_the_story_chapter(config):
    _store(config, 2)
    conn = open_db(config.sqlite_path)
    scope = resolve_scopes(conn, HOLLOW)[0]
    conn.close()
    assert scope.position.number == 1 and scope.position.heading == "The Road"
    assert scope.position.note() == \
        "Answered from what you've read: up to chapter 1 (The Road) of Hollow Gate"
    # Visibility still caps at the stored chapter_seq.
    assert scope.sees("hb1", 2) and not scope.sees("hb1", 3)


def test_a_position_in_the_front_matter_is_the_start(config):
    _store(config, 1)
    conn = open_db(config.sqlite_path)
    scope = resolve_scopes(conn, HOLLOW)[0]
    conn.close()
    assert scope.position.number == 0 and scope.position.heading is None
    assert scope.position.note().endswith("up to chapter 0 of Hollow Gate")


def test_notes_without_a_count_read_as_before():
    view = PositionView("b", "Book", 4)
    assert view.note() == "Answered from what you've read: up to chapter 4 of Book"
    assert position_note([view, PositionView("c", "Other", 9, 2, "Dawn")]) == (
        "Answered from what you've read: up to chapter 4 of Book;"
        " up to chapter 2 (Dawn) of Other")
