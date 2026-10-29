"""A library graphed before worlds existed, and the world invariant around it.

Migration 9 puts each series-less book in a world of its own; an entity the
old resolver already fused across two such books then spans two worlds.
`reconcile_worlds_from_graph` merges them. Invented books and names only.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lore import worlds
from lore.cli import app
from lore.config import LoreConfig
from lore.db.kuzu_store import KuzuStore
from lore.db.sqlite import MIGRATIONS, open_db
from lore.graph.loader import WorldInvariantError, check_world_invariant, load_book
from lore.worlds import (
    ensure_reconciled, reconcile_worlds_from_graph, suggest_world, world_of,
)
from tests.test_loader import (
    BOOK, BOOK_B, _add_book, _chunk, _embed, _env, _never, _row,
)

runner = CliRunner()


def _pre_worlds_db(path: Path, rows) -> sqlite3.Connection:
    """A catalog as it was before migration 9; `rows` are (id, title, series)."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY,"
                 " applied_at TEXT NOT NULL DEFAULT (datetime('now')))")
    for version, stmts in MIGRATIONS:
        if version >= 9:
            break
        for s in stmts:
            conn.execute(s)
        conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
    for id_, title, series in rows:
        conn.execute("INSERT INTO books (id, title, file_path, series)"
                     " VALUES (?, ?, '/x', ?)", (id_, title, series))
    conn.commit()
    conn.close()
    return open_db(path)


def _entity(kuzu, node_id, name, books) -> None:
    kuzu.execute("CREATE (:Entity {id: $id, name: $n, kind: 'Character',"
                 " aliases: [], books: $b})", {"id": node_id, "n": name, "b": books})


def _kuzu(tmp_path: Path) -> KuzuStore:
    return KuzuStore(tmp_path / "graph")


# ---- grouping --------------------------------------------------------------

def test_series_differing_only_in_case_or_padding_share_a_world(tmp_path) -> None:
    conn = _pre_worlds_db(tmp_path / "lore.db", [
        ("a", "Part One", "Saga of Tarn"), ("b", "Part Two", "saga of tarn"),
        ("c", "Part Three", "  SAGA OF TARN "), ("d", "Other", "Elsewhere")])
    assert len({world_of(conn, b).id for b in "abc"}) == 1
    assert world_of(conn, "a").name == "Saga of Tarn"  # first book's spelling
    assert world_of(conn, "d").id != world_of(conn, "a").id
    assert len(worlds.list_worlds(conn)) == 2


def test_suggest_world_and_the_migration_group_series_the_same_way(tmp_path) -> None:
    conn = _pre_worlds_db(tmp_path / "lore.db", [("a", "Part One", " Saga of Tarn ")])
    wid = world_of(conn, "a").id
    for spelling in ("saga of tarn", "SAGA OF TARN", "Saga of Tarn  "):
        assert suggest_world(conn, spelling, "Next") == (wid, "Saga of Tarn")


# ---- reconcile -------------------------------------------------------------

def test_two_series_less_books_sharing_an_entity_end_in_one_world(tmp_path) -> None:
    conn = _pre_worlds_db(tmp_path / "lore.db", [
        ("a", "Lone Tale", None), ("b", "Another Tale", None), ("c", "Apart", None)])
    assert world_of(conn, "a").id != world_of(conn, "b").id
    kuzu = _kuzu(tmp_path)
    _entity(kuzu, "e1", "Maren Vex", ["a", "b"])
    _entity(kuzu, "e2", "Zed", ["c"])
    with pytest.raises(WorldInvariantError):
        check_world_invariant(conn, kuzu)

    report = reconcile_worlds_from_graph(conn, kuzu)

    assert world_of(conn, "a").id == world_of(conn, "b").id
    assert world_of(conn, "c").id != world_of(conn, "a").id  # untouched
    assert len(worlds.list_worlds(conn)) == 2  # the emptied world is gone
    assert report.merged == 1 and len(report.merges) == 1
    merge = report.merges[0]
    assert merge.kept in ("Lone Tale", "Another Tale")
    assert len(merge.absorbed) == 1 and len(merge.books) == 1
    check_world_invariant(conn, kuzu)
    kuzu.close()


def test_reconcile_keeps_the_world_with_most_books_then_the_series_world(
    tmp_path,
) -> None:
    conn = _pre_worlds_db(tmp_path / "lore.db", [
        ("a", "A1", "Big Saga"), ("b", "A2", "Big Saga"), ("c", "Solo", None),
        ("d", "D1", "Tie Series"), ("e", "Plain", None)])
    kuzu = _kuzu(tmp_path)
    _entity(kuzu, "e1", "Maren Vex", ["a", "c"])
    _entity(kuzu, "e2", "Zed", ["d", "e"])
    big = world_of(conn, "a").id
    report = reconcile_worlds_from_graph(conn, kuzu)
    assert world_of(conn, "c").id == big  # most books wins
    assert world_of(conn, "e").name == "Tie Series"  # a tie goes to the series
    assert {m.kept for m in report.merges} == {"Big Saga", "Tie Series"}
    kuzu.close()


def test_reconcile_chains_worlds_connected_through_several_entities(tmp_path) -> None:
    conn = _pre_worlds_db(tmp_path / "lore.db", [
        ("a", "One", None), ("b", "Two", None), ("c", "Three", None)])
    kuzu = _kuzu(tmp_path)
    _entity(kuzu, "e1", "Maren Vex", ["a", "b"])
    _entity(kuzu, "e2", "Zed", ["b", "c"])
    reconcile_worlds_from_graph(conn, kuzu)
    assert len({world_of(conn, b).id for b in "abc"}) == 1
    kuzu.close()


def test_reconcile_is_idempotent_and_runs_once_automatically(tmp_path) -> None:
    conn = _pre_worlds_db(tmp_path / "lore.db", [
        ("a", "One", None), ("b", "Two", None)])
    kuzu = _kuzu(tmp_path)
    _entity(kuzu, "e1", "Maren Vex", ["a", "b"])
    first = ensure_reconciled(conn, kuzu)
    assert first is not None and first.merged == 1
    snapshot = conn.execute("SELECT id, world_id FROM books ORDER BY id").fetchall()
    assert ensure_reconciled(conn, kuzu) is None  # the marker stops a rerun
    again = reconcile_worlds_from_graph(conn, kuzu)  # and a forced rerun is empty
    assert again.merges == [] and again.merged == 0
    assert conn.execute(
        "SELECT id, world_id FROM books ORDER BY id").fetchall() == snapshot
    # The marker is per library: a world made later is not merged away by it.
    other = worlds.create_world(conn, "Later")
    assert ensure_reconciled(conn, kuzu) is None
    assert worlds.get_world(conn, other) is not None
    kuzu.close()


@pytest.mark.asyncio
async def test_the_first_load_after_migration_reconciles_and_succeeds(
    tmp_path: Path,
) -> None:
    config, conn, kuzu, lance = _env(tmp_path)
    one = [_chunk(BOOK, 1, 0)]
    _add_book(config, conn, BOOK, one, [
        _row(one[0].id, BOOK, "Maren Vex", "OPPOSES", "Bo",
             ["Maren Vex", "Bo"], ["Character", "Character"])])
    two = [_chunk(BOOK_B, 1, 0)]
    _add_book(config, conn, BOOK_B, two, [
        _row(two[0].id, BOOK_B, "Maren Vex", "LOVES", "Zed",
             ["Maren Vex", "Zed"], ["Character", "Character"])])
    await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    await load_book(config, conn, kuzu, lance, BOOK_B, _embed, _never)
    # The state migration 9 leaves behind: a fused entity, two worlds, no
    # reconcile yet.
    split = worlds.create_world(conn, "Split Off")
    worlds.assign(conn, BOOK_B, split)
    conn.execute("DELETE FROM world_reconcile")
    conn.commit()

    await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)  # no raise

    assert world_of(conn, BOOK).id == world_of(conn, BOOK_B).id
    assert conn.execute("SELECT COUNT(*) FROM world_reconcile").fetchone()[0] == 1
    await load_book(config, conn, kuzu, lance, BOOK_B, _embed, _never)
    check_world_invariant(conn, kuzu)
    conn.close(); kuzu.close()


def test_the_world_reconcile_command_merges_and_reports(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    config = LoreConfig.load()
    config.ensure_dirs()
    conn = _pre_worlds_db(config.sqlite_path, [
        ("a", "One", None), ("b", "Two", None)])
    kuzu = KuzuStore(config.kuzu_dir / "graph")
    _entity(kuzu, "e1", "Maren Vex", ["a", "b"])
    kuzu.close()
    conn.close()

    result = runner.invoke(app, ["world", "reconcile"])
    assert result.exit_code == 0, result.output
    assert "merged" in result.output and "One" in result.output
    conn = open_db(config.sqlite_path)
    assert world_of(conn, "a").id == world_of(conn, "b").id
    conn.close()
    again = runner.invoke(app, ["world", "reconcile"])
    assert again.exit_code == 0 and "nothing to merge" in again.output


# ---- the scoped invariant --------------------------------------------------

def _two_worlds_with_a_fused_entity(tmp_path):
    conn = _pre_worlds_db(tmp_path / "lore.db", [
        ("a", "One", None), ("b", "Two", None),
        ("c", "Three", None), ("d", "Four", None)])
    kuzu = _kuzu(tmp_path)
    return conn, kuzu


def test_a_violation_inside_the_loaded_world_still_raises(tmp_path) -> None:
    conn, kuzu = _two_worlds_with_a_fused_entity(tmp_path)
    worlds.assign(conn, "b", world_of(conn, "a").id)  # a and b: one world
    _entity(kuzu, "e1", "Maren Vex", ["a", "c"])  # c is in another world
    with pytest.raises(WorldInvariantError, match="Maren Vex"):
        check_world_invariant(conn, kuzu, world_id=world_of(conn, "a").id)
    kuzu.close()


def test_a_violation_elsewhere_is_a_warning_naming_reconcile(
    tmp_path, caplog,
) -> None:
    conn, kuzu = _two_worlds_with_a_fused_entity(tmp_path)
    _entity(kuzu, "e1", "Maren Vex", ["c", "d"])  # spans two worlds, not a's
    _entity(kuzu, "e2", "Zed", ["a"])
    with caplog.at_level(logging.WARNING):
        check_world_invariant(conn, kuzu, world_id=world_of(conn, "a").id)
    assert "lore world reconcile" in caplog.text
    assert "Maren Vex" in caplog.text
    with pytest.raises(WorldInvariantError):  # the global check still raises
        check_world_invariant(conn, kuzu)
    kuzu.close()


def test_a_clean_scoped_check_is_silent(tmp_path, caplog) -> None:
    conn, kuzu = _two_worlds_with_a_fused_entity(tmp_path)
    _entity(kuzu, "e1", "Zed", ["a"])
    with caplog.at_level(logging.WARNING):
        check_world_invariant(conn, kuzu, world_id=world_of(conn, "a").id)
    assert caplog.text == ""
    kuzu.close()


# ---- ghosts ----------------------------------------------------------------

def test_a_book_id_missing_from_the_registry_is_not_a_world(tmp_path) -> None:
    conn, kuzu = _two_worlds_with_a_fused_entity(tmp_path)
    _entity(kuzu, "e1", "Maren Vex", ["a", "ghost-book"])
    _entity(kuzu, "e2", "Zed", ["ghost-book"])
    check_world_invariant(conn, kuzu)  # no raise
    check_world_invariant(conn, kuzu, world_id=world_of(conn, "a").id)
    assert reconcile_worlds_from_graph(conn, kuzu).merges == []
    kuzu.close()
