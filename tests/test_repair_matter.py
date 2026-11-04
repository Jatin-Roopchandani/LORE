"""`lore repair matter`: classify an existing book and take what its front and
back matter produced out of every store, asking no model.

A library on real SQLite, Kuzu and LanceDB, ingested "before classification":
book "mr1" has a licence header (chapter 1), a contents page (2), three story
chapters (3-5) and a licence (6). The contents page minted "Prophet Vane",
whom no story chapter mentions, an edge to him, a beat and a codex statement.
Book "pl1" (another world) is chapters only.
"""
from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from lore.beats.store import Beat, save_beats
from lore.cli import app
from lore.codex.store import CodexEntry, Statement, save_entry
from lore.config import LoreConfig
from lore.db.kuzu_store import KuzuStore
from lore.db.lance_store import LanceStore
from lore.db.sqlite import open_db
from lore.graph.repair_matter import apply_repair, plan_repair
from lore.ingest.matter import BACK, FRONT, STORY
from lore.ingest.registry import chunks_for_book, section_rows, story_chapters
from lore.worlds import create_world
from tests.worlds_fixtures import unit

runner = CliRunner()


def story(word: str) -> str:
    return " ".join(f"{word} rode along the ridge and spoke of the salt flats." for _ in range(20))


LICENCE = ("This ebook is for the use of anyone anywhere in the United States and most other"
           " parts of the world at no cost. It is under the Project Gutenberg License at"
           " www.gutenberg.org.")
TOC = ("THE MARROW ROAD. PART I. CHAPTER I. THE TOLL HOUSE. CHAPTER II. FERRIN AT THE FORD."
       " PART II. CHAPTER I. FERRIN TALKS WITH THE PROPHET VANE. CHAPTER II. THE BARGAIN.")
TAIL = " ".join(f"1.F.{i}. The Project Gutenberg Literary Archive Foundation, paper edition {i}."
                for i in range(20))
CHAPTERS = {
    "mr1": {1: LICENCE, 2: TOC, 3: story("Ferrin"), 4: story("Ferrin and Kel"),
            5: story("Kel"), 6: TAIL},
    "pl1": {1: story("Ona"), 2: story("Ona and Bram")},
}
# entity -> (name, books)
ENTITIES = {"ferrin": ("Ferrin", ["mr1"]), "kel": ("Kel", ["mr1"]),
            "vane": ("Prophet Vane", ["mr1"]), "ona": ("Ona", ["pl1"]),
            "bram": ("Bram", ["pl1"])}
MENTIONS = {"mr1:2:0": ["ferrin", "vane"], "mr1:3:0": ["ferrin"], "mr1:4:0": ["ferrin", "kel"],
            "mr1:5:0": ["kel"], "pl1:1:0": ["ona"], "pl1:2:0": ["ona", "bram"]}
EDGES = [("ferrin", "vane", "mr1:2:0", "TALKS_WITH"), ("ferrin", "kel", "mr1:4:0", "RIDES_WITH"),
         ("ona", "bram", "pl1:2:0", "MEETS")]


def build(tmp_path: Path) -> LoreConfig:
    config = LoreConfig(data_dir=tmp_path / "data")
    conn = open_db(config.sqlite_path)
    for book_id, title in (("mr1", "The Marrow Road"), ("pl1", "Plain Tales")):
        world = create_world(conn, title)
        conn.execute("INSERT INTO books (id, title, file_path, world_id, status)"
                     " VALUES (?, ?, 'gone.epub', ?, 'graphed')", (book_id, title, world))
    lance = LanceStore(config.lancedb_dir)
    rows = []
    n = 0
    for book_id, chapters in CHAPTERS.items():
        for seq, text in chapters.items():
            chunk_id = f"{book_id}:{seq}:0"
            conn.execute("INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text)"
                         " VALUES (?, ?, ?, 0, 0, ?)", (chunk_id, book_id, seq, text))
            if chunk_id in MENTIONS or seq in (1, 6):
                rows.append({"vector": unit(n % 700, 701, 0.1), "chunk_id": chunk_id,
                             "book_id": book_id, "chapter_seq": seq, "text": text,
                             "entity_ids": MENTIONS.get(chunk_id, [])})
            n += 1
    lance.add_chunks(rows)
    lance.add_entities([{"vector": unit(i + 710), "entity_id": e, "name": name, "kind": "Character"}
                        for i, (e, (name, _b)) in enumerate(ENTITIES.items())])
    for chapter, text in ((2, "Ferrin engages in a conversation with the Prophet Vane"),
                          (4, "Ferrin rides with Kel")):
        save_beats(conn, "mr1", chapter, 0, [Beat(
            id=f"beat-{chapter}", book_id="mr1", chapter_seq=chapter, chunk_id=f"mr1:{chapter}:0",
            entities=("Ferrin",), kind="action", text=text, quote="Ferrin")])
    world = conn.execute("SELECT world_id FROM books WHERE id = 'mr1'").fetchone()[0]
    save_entry(conn, CodexEntry(
        entity_id="ferrin", world_id=world, name="Ferrin", kind="Character", summary="S",
        statements=(Statement("history", "TOCSTATEMENT talks with the prophet", "FERRIN", "mr1:2:0"),
                    Statement("history", "STORYSTATEMENT rides the ridge", "Ferrin", "mr1:3:0"))))
    conn.commit()
    conn.close()

    graph = KuzuStore(config.kuzu_dir / "graph")
    for book_id, chapters in CHAPTERS.items():
        graph.execute("MERGE (b:Book {id: $id})", {"id": book_id})
        for seq in chapters:
            graph.execute("CREATE (:Chapter {id: $id, book_id: $b, seq: $s, title: ''})",
                          {"id": f"{book_id}:{seq}", "b": book_id, "s": seq})
            graph.execute("CREATE (:Chunk {id: $id, book_id: $b, chapter_seq: $s})",
                          {"id": f"{book_id}:{seq}:0", "b": book_id, "s": seq})
    for entity_id, (name, books) in ENTITIES.items():
        graph.execute("CREATE (:Entity {id: $id, name: $n, kind: 'Character', aliases: [],"
                      " books: $b})", {"id": entity_id, "n": name, "b": books})
    for chunk_id, entities in MENTIONS.items():
        for e in entities:
            graph.execute("MATCH (c:Chunk {id: $c}), (e:Entity {id: $e}) CREATE (c)-[:MENTIONS]->(e)",
                          {"c": chunk_id, "e": e})
    for s, o, chunk_id, predicate in EDGES:
        book_id, seq, _ = chunk_id.split(":")
        graph.execute("MATCH (s:Entity {id: $s}), (o:Entity {id: $o}) CREATE (s)-[:REL {predicate:"
                      " $p, predicate_raw: $p, evidence: 'e', confidence: 0.9, chunk_id: $c,"
                      " book_id: $b, chapter_seq: $q, run_id: 'r'}]->(o)",
                      {"s": s, "o": o, "p": predicate, "c": chunk_id, "b": book_id, "q": int(seq)})
    graph.close()
    return config


def stores(config):
    return (open_db(config.sqlite_path), KuzuStore(config.kuzu_dir / "graph"),
            LanceStore(config.lancedb_dir))


def test_the_plan_names_exactly_what_front_and_back_matter_produced(tmp_path):
    config = build(tmp_path)
    conn, kuzu, lance = stores(config)
    plan = plan_repair(conn, kuzu, lance, "mr1", [tmp_path])
    assert plan.source == "chunks"
    assert [c.matter for c in plan.sections] == [FRONT, FRONT, STORY, STORY, STORY, BACK]
    assert plan.chunk_ids == ["mr1:1:0", "mr1:2:0", "mr1:6:0"]
    assert [b[0] for b in plan.beats] == ["beat-2"]
    assert [c[2] for c in plan.codex] == ["TOCSTATEMENT talks with the prophet"]
    assert plan.rel_edges == [("Ferrin", "TALKS_WITH", "Prophet Vane", "mr1:2:0")]
    assert plan.chunk_nodes == 3 and plan.chapter_nodes == 3
    assert plan.vector_chunks == ["mr1:1:0", "mr1:2:0", "mr1:6:0"]
    assert plan.entities_deleted == [("vane", "Prophet Vane")]
    assert plan.books_released == {} and plan.recentred == ["ferrin"]
    # Planning changed nothing.
    assert section_rows(conn, "mr1") == []
    assert conn.execute("SELECT COUNT(*) FROM beats").fetchone() == (2,)
    kuzu.close()
    conn.close()


def test_apply_removes_it_everywhere_and_a_second_run_finds_nothing(tmp_path):
    config = build(tmp_path)
    conn, kuzu, lance = stores(config)
    apply_repair(conn, kuzu, lance, plan_repair(conn, kuzu, lance, "mr1", [tmp_path]))

    assert [r.matter for r in section_rows(conn, "mr1")] == [FRONT, FRONT, STORY, STORY, STORY, BACK]
    assert [c["seq"] for c in story_chapters(conn, "mr1")] == [3, 4, 5]
    assert {c.chapter_seq for c in chunks_for_book(conn, "mr1")} == {3, 4, 5}
    # Chunk rows stay: the faithful record of the file.
    assert conn.execute("SELECT COUNT(*) FROM chunks WHERE book_id = 'mr1'").fetchone() == (6,)
    assert [r[0] for r in conn.execute("SELECT id FROM beats")] == ["beat-4"]
    assert [r[0] for r in conn.execute("SELECT statement FROM codex_entries")] == [
        "STORYSTATEMENT rides the ridge"]
    assert {r[0] for r in kuzu.execute("MATCH (e:Entity) RETURN e.id")} == {"ferrin", "kel", "ona", "bram"}
    assert [r[0] for r in kuzu.execute("MATCH ()-[r:REL]->() RETURN r.chunk_id ORDER BY r.chunk_id")] == [
        "mr1:4:0", "pl1:2:0"]
    assert sorted(r[0] for r in kuzu.execute("MATCH (c:Chunk) RETURN c.id")) == [
        "mr1:3:0", "mr1:4:0", "mr1:5:0", "pl1:1:0", "pl1:2:0"]
    assert lance.present_chunks(["mr1:1:0", "mr1:2:0", "mr1:6:0", "mr1:3:0"]) == {"mr1:3:0"}
    left = {r["entity_id"] for r in lance._db.open_table("entities").search().limit(None).to_list()}
    assert "vane" not in left and "ferrin" in left

    again = plan_repair(conn, kuzu, lance, "mr1", [tmp_path])
    assert not again.changed and not again.deletes_anything
    plain = plan_repair(conn, kuzu, lance, "pl1", [tmp_path])
    assert [c.matter for c in plain.sections] == [STORY, STORY] and not plain.deletes_anything
    kuzu.close()
    conn.close()


def test_the_cli_dry_run_changes_nothing_and_the_run_is_idempotent(tmp_path, monkeypatch):
    build(tmp_path)
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    dry = runner.invoke(app, ["repair", "matter", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "would delete: 1 beats, 1 codex statements, 1 relationships" in dry.output
    assert "beat (section 2): Ferrin engages in a conversation with the Prophet Vane" in dry.output
    assert "entity: Prophet Vane (vane)" in dry.output
    assert "section 2: front matter (contents)" in dry.output
    conn = open_db(tmp_path / "data" / "lore.db")
    assert conn.execute("SELECT COUNT(*) FROM sections").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM beats").fetchone() == (2,)
    conn.close()

    run = runner.invoke(app, ["repair", "matter", "--book", "Marrow"])
    assert run.exit_code == 0, run.output
    assert "deleted: 1 beats" in run.output
    second = runner.invoke(app, ["repair", "matter"])
    assert second.exit_code == 0, second.output
    assert "deleted" not in second.output and second.output.count("nothing to delete") == 2


def test_sections_come_from_the_source_file_when_it_still_matches(tmp_path, monkeypatch):
    """The file's headings classify a preface that chunk text alone cannot."""
    from lore.ingest.chunker import chunk_book
    from lore.ingest.epub import parse_epub
    from lore.ingest.registry import ingest_book
    from tests.fixtures import make_epub

    path = make_epub(tmp_path / "pref.epub", title="Pref", author="A", chapters=[
        ("Preface", "<p>" + story("The editor") + "</p>"),
        ("Chapter I. The Ridge", "<p>" + story("Ferrin") + "</p>"),
        ("Chapter II. The Ford", "<p>" + story("Kel") + "</p>")])
    conn = open_db(tmp_path / "lore.db")
    parsed = parse_epub(path)
    ingest_book(conn, book_id="pf", title="Pref", author=None, source="upload",
                file_path="pref.epub", chunks=chunk_book(parsed, "pf"))  # no sections: old ingest
    plan = plan_repair(conn, None, None, "pf", [tmp_path])
    assert plan.source == "file"
    assert [(c.matter, c.heading) for c in plan.sections] == [
        (FRONT, "Preface"), (STORY, "The Ridge"), (STORY, "The Ford")]
    apply_repair(conn, None, None, plan)
    assert [c["heading"] for c in story_chapters(conn, "pf")] == ["The Ridge", "The Ford"]
    # Without the file, the chunk text cannot tell the preface from a chapter.
    assert [c.matter for c in plan_repair(conn, None, None, "pf", [tmp_path / "x"]).sections] == [
        STORY, STORY, STORY]
    conn.close()
