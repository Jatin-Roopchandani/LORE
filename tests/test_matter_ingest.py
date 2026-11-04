"""Ingest classifies sections; extraction, beats, loads and the keyword index
read only story sections; a book ingested before keeps its extraction units.

Fixture books are built here: one EPUB wrapped in front and back matter, one
EPUB of chapters only, and a PDF with a contents page.
"""
from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from lore.cli import app
from lore.db.sqlite import open_db
from lore.ingest.matter import BACK, FRONT, STORY, Classified
from lore.ingest.registry import (
    chunks_for_book, forget_book, non_story_chapters, save_sections, section_rows,
    story_chapters, story_place)
from lore.orchestrator.run import book_units, set_unit_state
from lore.orchestrator.workunit import pack_units
from lore.query.keyword import KeywordIndex
from tests.fixtures import make_epub, make_pdf

runner = CliRunner()


def _p(word: str, n: int = 25) -> str:
    return "<p>" + " ".join(f"The {word} crossed the river at dusk and waited." for _ in range(n)) + "</p>"


WRAPPED = [
    ("The Project Gutenberg eBook of The Marrow Road",
     "<p>This ebook is for the use of anyone anywhere in the United States and most other parts"
     " of the world at no cost. You may copy it under the terms of the Project Gutenberg License"
     " included with this ebook or online at www.gutenberg.org.</p>"),
    ("Contents", "<p>Chapter I. The Toll House</p><p>Chapter II. A Letter</p>"
                 "<p>Chapter III. The Ferry</p><p>Chapter IV. The Prophet's Bargain</p>"),
    ("Chapter I. The Toll House", _p("tollkeeper")),
    ("Chapter II. A Letter", _p("courier")),
    ("Chapter III. The Ferry", _p("ferryman")),
    ("Chapter IV. The Prophet's Bargain", _p("prophet")),
    ("The Full Project Gutenberg License",
     "".join(f"<p>1.F.{i}. The Project Gutenberg Literary Archive Foundation, clause {i}.</p>"
             for i in range(30))),
]
PLAIN = [("Chapter 1", _p("girl")), ("Chapter 2", _p("boy")), ("Chapter 3", _p("dog"))]


def _ingest(tmp_path: Path, monkeypatch, name: str, chapters) -> tuple[str, str]:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    path = make_epub(tmp_path / f"{name}.epub", title=name, author="A", chapters=chapters)
    result = runner.invoke(app, ["ingest", str(path), "--yes", "--world", name])
    assert result.exit_code == 0, result.output
    conn = open_db(tmp_path / "data" / "lore.db")
    book_id = conn.execute("SELECT id FROM books WHERE title = ?", (name,)).fetchone()[0]
    conn.close()
    return book_id, result.output


def test_ingest_stores_the_classification_and_skips_matter(tmp_path, monkeypatch):
    book_id, output = _ingest(tmp_path, monkeypatch, "Marrow", WRAPPED)
    assert "3 of the sections are front or back matter" in output
    conn = open_db(tmp_path / "data" / "lore.db")
    rows = section_rows(conn, book_id)
    assert [r.matter for r in rows] == [FRONT, FRONT, STORY, STORY, STORY, STORY, BACK]
    assert non_story_chapters(conn) == {book_id: {1, 2, 7}}
    story = chunks_for_book(conn, book_id)
    assert story and {c.chapter_seq for c in story} == {3, 4, 5, 6}
    everything = chunks_for_book(conn, book_id, story_only=False)
    assert {c.chapter_seq for c in everything} >= {2, 3, 4, 5, 6}
    assert [(c["seq"], c["number"], c["heading"]) for c in story_chapters(conn, book_id)] == [
        (3, 1, "The Toll House"), (4, 2, "A Letter"), (5, 3, "The Ferry"),
        (6, 4, "The Prophet's Bargain")]
    # Extraction packs only story chunks for a new book.
    units = book_units(conn, book_id, 1000)
    assert [c.id for u in units for c in u.chunks] == [c.id for c in story]
    # The keyword index never finds the contents page.
    assert not [h for h in KeywordIndex(conn).search("Prophet Bargain Toll House", 10)
                if h[2] == 2]
    # Forgetting the book forgets its sections.
    forget_book(conn, book_id)
    assert section_rows(conn, book_id) == []
    conn.close()


def test_a_book_of_chapters_only_is_all_story(tmp_path, monkeypatch):
    book_id, output = _ingest(tmp_path, monkeypatch, "Plain", PLAIN)
    assert "front or back matter" not in output
    conn = open_db(tmp_path / "data" / "lore.db")
    assert [r.matter for r in section_rows(conn, book_id)] == [STORY] * 3
    assert [(c["seq"], c["number"]) for c in story_chapters(conn, book_id)] == [
        (1, 1), (2, 2), (3, 3)]
    assert len(chunks_for_book(conn, book_id)) == len(chunks_for_book(conn, book_id, story_only=False))
    conn.close()


def test_a_pdf_contents_page_is_front_matter(tmp_path, monkeypatch):
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    def body(tag):
        return " ".join(f"The {tag} keeper lit lamp {i} and watched the sea." for i in range(30))
    path = make_pdf(tmp_path / "lamp.pdf",
                    pages=["Chapter 1 The Lamp 3\nChapter 2 The Storm 9\nChapter 3 The Wreck 15"
                           "\nChapter 4 Morning 21", body("first"), body("second"), body("third"), body("fourth")],
                    heading_pages={0: "Contents", 1: "Chapter One", 2: "Chapter Two",
                                   3: "Chapter Three", 4: "Chapter Four"})
    result = runner.invoke(app, ["ingest", str(path), "--yes", "--world", "Lamp"])
    assert result.exit_code == 0, result.output
    conn = open_db(tmp_path / "data" / "lore.db")
    book_id = conn.execute("SELECT id FROM books").fetchone()[0]
    assert [r.matter for r in section_rows(conn, book_id)] == [FRONT] + [STORY] * 4
    conn.close()


def test_a_book_extracted_before_classification_keeps_its_units(tmp_path):
    conn = open_db(tmp_path / "lore.db")
    conn.execute("INSERT INTO books (id, title, file_path) VALUES ('b', 'B', 'b.epub')")
    for seq in (1, 2, 3):
        conn.execute("INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text)"
                     " VALUES (?, 'b', ?, 0, 0, 'text')", (f"b:{seq}:0", seq))
    conn.commit()
    everything = chunks_for_book(conn, "b")
    legacy = pack_units("b", everything, 2)
    set_unit_state(conn, "b", legacy[0].unit_id, "completed")
    save_sections(conn, "b", [Classified(1, FRONT, "contents", None),
                              Classified(2, STORY, "", None), Classified(3, STORY, "", None)])
    assert [u.unit_id for u in book_units(conn, "b", 2)] == [u.unit_id for u in legacy]
    conn.close()


def test_story_place_maps_a_stored_chapter_seq_to_the_story_chapter():
    chapters = [{"seq": 5, "number": 1, "heading": "A"}, {"seq": 6, "number": 2, "heading": None},
                {"seq": 7, "number": 3, "heading": "C"}]
    assert story_place(chapters, 0) is None
    assert story_place(chapters, 3) is None  # in the front matter: nothing read yet
    assert story_place(chapters, 7)["number"] == 3
    assert story_place(chapters, 19)["number"] == 3  # past the end: the last chapter


def test_an_unclassified_book_counts_every_chapter(tmp_path):
    conn = open_db(tmp_path / "lore.db")
    conn.execute("INSERT INTO books (id, title, file_path) VALUES ('b', 'B', 'b.epub')")
    for seq in (1, 3, 4):
        conn.execute("INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text)"
                     " VALUES (?, 'b', ?, 0, 0, 'text')", (f"b:{seq}:0", seq))
    conn.commit()
    assert [(c["seq"], c["number"]) for c in story_chapters(conn, "b")] == [(1, 1), (3, 2), (4, 3)]
    assert non_story_chapters(conn) == {}
    conn.close()
