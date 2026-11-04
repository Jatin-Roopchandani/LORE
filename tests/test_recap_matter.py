"""A recap counts chapters as the book does: front matter is not a chapter.

Over tests/position_fixtures.py with Hollow Gate's first section classified as
a contents page, its story chapters are 1 (chapter_seq 2) and 2 (seq 3).
"""
from __future__ import annotations

import asyncio

from lore.db.sqlite import open_db
from lore.ingest.matter import FRONT, STORY, Classified
from lore.ingest.registry import book_rows, save_sections
from lore.query.decompose import Recap
from lore.query.recap import Chapter, Chunk, gather_span, plan_stretches
from lore.query.scope import Scope
from tests.position_fixtures import build_position_library


def _classified_library(tmp_path):
    config = build_position_library(tmp_path)
    conn = open_db(config.sqlite_path)
    save_sections(conn, "hb1", [Classified(1, FRONT, "contents", "Contents"),
                                Classified(2, STORY, "", "The Road"),
                                Classified(3, STORY, "", "The Secret")])
    return conn


def test_recap_chapters_x_to_y_are_the_books_own_numbers(tmp_path):
    conn = _classified_library(tmp_path)
    infos = book_rows(conn)
    span = asyncio.run(gather_span(
        conn, None, Recap(span="chapters", book="Hollow Gate", first=1, last=1),
        scope=Scope(), question="recap chapter 1 of Hollow Gate",
        titles={b: i.title for b, i in infos.items()}, books=infos))
    conn.close()
    # Chapter 1 is raw section 2; the contents page (section 1) is never read.
    assert [c.key for c in span.chapters] == [("hb1", 2)]
    assert span.numbers == {("hb1", 2): 1}


def test_recap_section_headings_use_the_books_own_numbers():
    chapters = [Chapter("b", seq, (Chunk(f"b:{seq}:0", "b", seq, "x"),)) for seq in (5, 6, 7)]
    numbers = {("b", 5): 1, ("b", 6): 2, ("b", 7): 3}
    got = plan_stretches(chapters, {"b": "Scarlet"}, max_sections=1, numbers=numbers)
    assert [s.heading for s in got] == ["Chapters 1–3"]
    assert got[0].focus == "chapters 1–3 of Scarlet"
    # Without numbers (an unclassified book) the raw chapter_seq is shown, as before.
    assert plan_stretches(chapters, {"b": "Scarlet"}, max_sections=1)[0].heading == "Chapters 5–7"
