import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lore.cli import app
from lore.db.sqlite import MIGRATIONS, open_db, schema_version
from lore.ingest.models import Chunk
from lore.ingest.registry import ingest_book
from lore.worlds import (
    WorldError, assign, books_in, create_world, delete_world, get_world,
    list_worlds, rename_world, suggest_world, world_of,
)
from tests.fixtures import make_epub

runner = CliRunner()


def _add(conn, book_id, title, series=None, idx=None, world_id=None):
    ingest_book(conn, book_id=book_id, title=title, author=None, source="upload",
                file_path="/x", chunks=[Chunk(id=f"{book_id}:1:0", book_id=book_id,
                chapter_seq=1, scene_seq=0, seq=0, text="t")],
                series=series, series_index=idx, world_id=world_id)


def test_migration_version_and_backfill(tmp_path: Path) -> None:
    path = tmp_path / "lore.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY,"
                 " applied_at TEXT NOT NULL DEFAULT (datetime('now')))")
    for version, stmts in MIGRATIONS:
        if version >= 9:
            break
        for s in stmts:
            conn.execute(s)
        conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
    rows = [("a", "Alpha One", "Saga", 1), ("b", "Alpha Two", "Saga", 2),
            ("c", "Solo", None, None), ("d", "Solo", None, None),
            ("e", "Saga", None, None)]
    for id_, title, series, idx in rows:
        conn.execute("INSERT INTO books (id, title, file_path, series, series_index)"
                     " VALUES (?, ?, '/x', ?, ?)", (id_, title, series, idx))
    conn.commit()
    conn.close()

    conn = open_db(path)
    assert schema_version(conn) == 12
    w = {b: world_of(conn, b) for b in "abcde"}
    assert all(v is not None for v in w.values())
    assert w["a"].id == w["b"].id and w["a"].name == "Saga"
    assert len({w[b].id for b in "acde"}) == 4  # same-title books get their own worlds
    assert w["c"].name == "Solo"
    assert len(list_worlds(conn)) == 4


def test_crud_and_delete_refuses_non_empty(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    wid = create_world(conn, "Alpha")
    assert get_world(conn, wid).name == "Alpha"
    assert get_world(conn, "alpha").id == wid  # by name, ignoring case
    with pytest.raises(WorldError):
        create_world(conn, "Alpha")
    with pytest.raises(WorldError):
        create_world(conn, "  ")
    rename_world(conn, wid, "Beta")
    assert get_world(conn, "Beta").id == wid
    other = create_world(conn, "Gamma")
    with pytest.raises(WorldError):
        rename_world(conn, wid, "Gamma")
    _add(conn, "b1", "Book", world_id=wid)
    assert books_in(conn, wid) == ["b1"]
    assert world_of(conn, "b1").id == wid
    with pytest.raises(WorldError):
        delete_world(conn, wid)
    assign(conn, "b1", other)
    assert books_in(conn, wid) == [] and books_in(conn, other) == ["b1"]
    delete_world(conn, wid)
    assert get_world(conn, wid) is None


def test_ingest_book_without_world_gets_one(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    _add(conn, "b1", "Lonely Book")
    assert world_of(conn, "b1").name == "Lonely Book"
    _add(conn, "b2", "Part", series="Saga", idx=1)
    _add(conn, "b3", "Part Two", series="Saga", idx=2)
    assert world_of(conn, "b2").id == world_of(conn, "b3").id
    assert world_of(conn, "b2").name == "Saga"


def test_suggest_world_order(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    wid = create_world(conn, "Realm")
    _add(conn, "b1", "First", series="Saga", idx=1, world_id=wid)
    # 1. series already in a world -> that world
    assert suggest_world(conn, "Saga", "Second") == (wid, "Realm")
    # 2. new series -> new world named after the series
    assert suggest_world(conn, "Other", "Title") == (None, "Other")
    # 3. no series -> new world named after the title
    assert suggest_world(conn, None, "Title") == (None, "Title")
    assert suggest_world(conn, "  ", "Title") == (None, "Title")


def _epub(tmp_path, title="Prompt Book", series=None):
    body = " ".join(f"sentence number {i} with plenty of words." for i in range(40))
    kw = {"series": (series, 1.0)} if series else {}
    return make_epub(tmp_path / f"{title}.epub", title=title, author="A",
                     chapters=[("One", f"<p>{body}</p>")], **kw)


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)


def _world_names(tmp_path):
    conn = open_db(tmp_path / "data" / "lore.db")
    try:
        return [w.name for w in list_worlds(conn)]
    finally:
        conn.close()


def test_cli_ingest_with_world_confirms_creation(tmp_path, monkeypatch) -> None:
    _env(tmp_path, monkeypatch)
    path = _epub(tmp_path)
    declined = runner.invoke(app, ["ingest", str(path), "--world", "Newland"], input="n\n")
    assert declined.exit_code != 0
    assert "Newland" in declined.output
    assert _world_names(tmp_path) == []
    ok = runner.invoke(app, ["ingest", str(path), "--world", "Newland"], input="y\n")
    assert ok.exit_code == 0, ok.output
    assert _world_names(tmp_path) == ["Newland"]
    result = runner.invoke(app, ["books"])
    assert "world: Newland" in result.output


def test_cli_ingest_yes_skips_prompt_and_existing_world_needs_none(tmp_path, monkeypatch) -> None:
    _env(tmp_path, monkeypatch)
    first = runner.invoke(app, ["ingest", str(_epub(tmp_path)), "--world", "W", "--yes"])
    assert first.exit_code == 0, first.output
    other = _epub(tmp_path, title="Second Book")
    second = runner.invoke(app, ["ingest", str(other), "--world", "W"])  # no input
    assert second.exit_code == 0, second.output
    conn = open_db(tmp_path / "data" / "lore.db")
    assert len(books_in(conn, get_world(conn, "W").id)) == 2


def test_cli_ingest_offers_suggestion(tmp_path, monkeypatch) -> None:
    _env(tmp_path, monkeypatch)
    path = _epub(tmp_path, title="Prompt Book", series="Saga")
    declined = runner.invoke(app, ["ingest", str(path)], input="n\n")
    assert declined.exit_code != 0
    assert "Saga" in declined.output and "new world" in declined.output
    accepted = runner.invoke(app, ["ingest", str(path)], input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert _world_names(tmp_path) == ["Saga"]
    nxt = _epub(tmp_path, title="Sequel", series="Saga")
    again = runner.invoke(app, ["ingest", str(nxt), "--yes"])
    assert again.exit_code == 0
    assert "existing world" in again.output
    assert _world_names(tmp_path) == ["Saga"]


def test_cli_world_list_rename_delete(tmp_path, monkeypatch) -> None:
    _env(tmp_path, monkeypatch)
    runner.invoke(app, ["ingest", str(_epub(tmp_path)), "--world", "Old", "--yes"])
    conn = open_db(tmp_path / "data" / "lore.db")
    create_world(conn, "Empty")
    conn.close()
    listed = runner.invoke(app, ["world", "list"])
    assert "Old" in listed.output and "Empty" in listed.output
    assert runner.invoke(app, ["world", "rename", "Old", "New"]).exit_code == 0
    refused = runner.invoke(app, ["world", "delete", "New"])
    assert refused.exit_code != 0 and "not empty" in refused.output
    assert runner.invoke(app, ["world", "delete", "Empty"]).exit_code == 0
    assert _world_names(tmp_path) == ["New"]
    assert runner.invoke(app, ["world", "delete", "Nope"]).exit_code != 0
