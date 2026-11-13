"""Deleting a book from a live library (lore.book_delete): everything of the
book goes, shared entities keep their other books' facts, the codex entries
the removal dropped come back from saved answers, an emptied world goes, and
a delete never overlaps a run, an upload or another delete of the library.

The library is test_sample's three-book source (ONE and TWO in World A,
sharing Maren Vex; THREE alone in World B). Real SQLite, Kuzu and LanceDB in
tmp dirs; no model is asked.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import lancedb
import pytest

from lore import book_delete, library_lock, worlds
from lore.codex import build
from lore.config import LoreConfig
from lore.db.kuzu_store import KuzuStore
from lore.db.sqlite import open_db
from lore.graph.loader import check_world_invariant
from lore.orchestrator import jobs
from lore.orchestrator.pipeline import JobRefused, run_book
from lore.reading_position import Progress, get_position, progress_position, set_position
from tests.test_sample import ONE, THREE, TWO, _built, _refresh, source  # noqa: F401

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _low_threshold(monkeypatch):
    monkeypatch.setattr(build, "MIN_MENTION_CHUNKS", 1)


@pytest.fixture
def config(source: Path) -> LoreConfig:
    return LoreConfig(data_dir=source)


def _delete(config: LoreConfig, book: str, **kw):
    kw.setdefault("codex_refresh", _refresh())
    return book_delete.delete_book(config, book, now=NOW, **kw)


def _graph(config: LoreConfig):
    return KuzuStore(config.kuzu_dir / "graph", read_only=True)


def _entities(config: LoreConfig) -> dict[str, list[str]]:
    kuzu = _graph(config)
    try:
        return {r[0]: sorted(r[1] or ()) for r in kuzu.execute(
            "MATCH (e:Entity) RETURN e.name, e.books")}
    finally:
        kuzu.close()


def test_a_book_and_everything_of_it_goes(config: LoreConfig) -> None:
    conn = open_db(config.sqlite_path)
    one_job = conn.execute("SELECT * FROM book_jobs WHERE book_id = ?", (ONE,)).fetchone()
    conn.close()

    report = _delete(config, TWO)
    assert report.book_id == TWO and report.title == f"title-{TWO}"
    assert report.world == "World A" and report.world_removed is False

    conn = open_db(config.sqlite_path)
    try:
        assert sorted(r[0] for r in conn.execute("SELECT id FROM books")) == sorted([ONE, THREE])
        for table in ("chunks", "sections", "beats", "jobs", "book_jobs"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE book_id = ?",
                                (TWO,)).fetchone()[0] == 0, table
        # the other books' processing rows are exactly as they were
        assert conn.execute("SELECT * FROM book_jobs WHERE book_id = ?",
                            (ONE,)).fetchone() == one_job
        kuzu = _graph(config)
        try:
            check_world_invariant(conn, kuzu)
            assert sorted(r[0] for r in kuzu.execute("MATCH (b:Book) RETURN b.id")) == sorted(
                [ONE, THREE])
            assert not list(kuzu.execute(
                "MATCH ()-[r:REL]->() WHERE r.book_id = $b RETURN r", {"b": TWO}))
        finally:
            kuzu.close()
    finally:
        conn.close()
    names = _entities(config)
    assert "Zed" not in names  # only TWO knew him
    assert names["Maren Vex"] == [ONE]  # shared: kept, with ONE's facts
    db = lancedb.connect(str(config.lancedb_dir))
    assert TWO not in {r["book_id"] for r in db.open_table("chunks").to_arrow().to_pylist()}
    assert TWO not in {r["book_id"] for r in db.open_table("beats").to_arrow().to_pylist()}

    data = config.data_dir
    assert not (data / "extractions" / TWO).exists()
    assert not (data / "beats" / TWO).exists()
    assert (data / "extractions" / ONE).exists() and (data / "beats" / ONE).exists()
    assert sorted(p.name for p in (data / "inbox").iterdir()) == sorted(
        [f"{ONE}-stored.epub", f"{THREE}-stored.epub"])
    assert not (data / "disambiguation" / "pending" / f"{TWO}.json").exists()
    assert list(json.loads((data / "narrators" / "answers.json").read_text())) == [f"{ONE}:abc"]
    assert list(json.loads((data / "speakers" / "answers.json").read_text())) == [
        f"{ONE}:1:0:sha256=1"]


def test_the_codex_entries_the_removal_dropped_come_back_from_saved_answers(
        config: LoreConfig) -> None:
    kuzu = _graph(config)
    touched = [r[0] for r in kuzu.execute(
        "MATCH (e:Entity) WHERE list_contains(e.books, $b) RETURN e.id", {"b": TWO})]
    kuzu.close()
    untouched_before = sorted(p.name for p in config.codex_dir.iterdir()
                              if not any(p.name.startswith(f"{e}-") for e in touched))
    report = _delete(config, TWO)
    conn = open_db(config.sqlite_path)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM codex_summaries")}
        assert "Maren Vex" in names and "Zed" not in names
        assert report.codex_missing == []
        statements = {r[0] for r in conn.execute("SELECT id FROM codex_entries")}
        current = {f"{e}-{h}" for e, h in conn.execute(
            "SELECT entity_id, evidence_hash FROM codex_summaries")}
    finally:
        conn.close()
    db = lancedb.connect(str(config.lancedb_dir))
    assert {r["id"] for r in db.open_table("codex").to_arrow().to_pylist()} == statements
    # Saved answers that quote the deleted book are gone; the current ones stay.
    cached = {p.name.split(".", 1)[0] for p in config.codex_dir.iterdir() if p.is_file()}
    assert current <= cached
    for path in config.codex_dir.iterdir():
        assert b"silver key" not in path.read_bytes(), path.name
        if any(path.name.startswith(f"{e}-") for e in touched):
            assert path.name.split(".", 1)[0] in current, path.name
    # entities the book never touched keep every saved answer
    assert untouched_before == sorted(
        p.name for p in config.codex_dir.iterdir()
        if not any(p.name.startswith(f"{e}-") for e in touched))


def test_entries_with_no_saved_answer_are_reported(config: LoreConfig) -> None:
    # TWO was processed second: Maren Vex's entry for TWO's evidence alone
    # was never asked for, so it cannot come back without the model.
    report = _delete(config, ONE)
    assert "Maren Vex" in report.codex_missing
    assert _entities(config)["Maren Vex"] == [TWO]


def test_the_last_book_of_a_world_takes_the_world_and_its_position(
        config: LoreConfig) -> None:
    conn = open_db(config.sqlite_path)
    world_b = worlds.world_of(conn, THREE).id
    set_position(conn, progress_position(world_b, Progress.of({THREE: 1})))
    conn.close()

    report = _delete(config, THREE)
    assert report.world == "World B" and report.world_removed is True
    conn = open_db(config.sqlite_path)
    try:
        assert [w.name for w in worlds.list_worlds(conn)] == ["World A"]
        assert get_position(conn, world_b) is None
        for table in ("codex_summaries", "codex_entries", "entity_intros"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE world_id = ?",
                                (world_b,)).fetchone()[0] == 0, table
    finally:
        conn.close()
    assert "Ilsa Morrow" not in _entities(config)


def test_the_book_leaves_its_worlds_reading_progress(config: LoreConfig) -> None:
    conn = open_db(config.sqlite_path)
    world_a = worlds.world_of(conn, ONE).id
    set_position(conn, progress_position(world_a, Progress.of({ONE: 1}, unread=[TWO])))
    conn.close()

    _delete(config, TWO)
    conn = open_db(config.sqlite_path)
    try:
        position = get_position(conn, world_a)
        assert position.progress == Progress.of({ONE: 1})
    finally:
        conn.close()

    _delete(config, ONE)  # World A goes with its last book
    conn = open_db(config.sqlite_path)
    try:
        assert get_position(conn, world_a) is None
    finally:
        conn.close()


def test_an_older_position_pointing_at_the_book_is_cleared(config: LoreConfig) -> None:
    # The source holds an older-shape position: World A, reading ONE, chapter 1.
    conn = open_db(config.sqlite_path)
    world_a = worlds.world_of(conn, ONE).id
    conn.execute("UPDATE reading_positions SET progress = NULL, read_books = ?"
                 " WHERE world_id = ?", (json.dumps([TWO]), world_a))
    conn.commit()
    conn.close()
    _delete(config, ONE)
    conn = open_db(config.sqlite_path)
    try:
        position = get_position(conn, world_a)
        assert position.book_id is None and ONE not in position.read_books
    finally:
        conn.close()


def test_entity_intros_of_the_books_entities_go(config: LoreConfig) -> None:
    conn = open_db(config.sqlite_path)
    world_a = worlds.world_of(conn, ONE).id
    kuzu = _graph(config)
    ids = {r[0]: r[1] for r in kuzu.execute("MATCH (e:Entity) RETURN e.name, e.id")}
    kuzu.close()
    with conn:
        for name in ("Maren Vex", "Zed", "Bo"):
            conn.execute("INSERT INTO entity_intros (entity_id, world_id, scope_hash,"
                         " prompt_hash, intro) VALUES (?, ?, 's', 'p', ?)",
                         (ids[name], world_a, f"About {name}."))
    conn.close()
    _delete(config, TWO)
    conn = open_db(config.sqlite_path)
    try:
        left = {r[0] for r in conn.execute("SELECT intro FROM entity_intros")}
    finally:
        conn.close()
    assert left == {"About Bo."}  # Bo is ONE's alone: untouched


def test_an_unknown_book_is_not_found(config: LoreConfig) -> None:
    with pytest.raises(book_delete.DeleteRefused) as err:
        _delete(config, "0000000000000000")
    assert err.value.code == "not_found"


def test_a_book_being_processed_is_refused_and_left_alone(config: LoreConfig) -> None:
    conn = open_db(config.sqlite_path)
    jobs.claim_job(conn, TWO, now=NOW)
    conn.close()
    with pytest.raises(book_delete.DeleteRefused) as err:
        _delete(config, TWO)
    assert err.value.code == "processing"
    assert TWO in _entities(config)["Maren Vex"]


def test_a_stopped_book_can_be_deleted(config: LoreConfig) -> None:
    conn = open_db(config.sqlite_path)
    jobs.claim_job(conn, TWO, now=NOW)
    jobs.fail(conn, TWO, "the parse broke", now=NOW)
    conn.close()
    _delete(config, TWO)
    assert "Zed" not in _entities(config)


def test_a_queued_job_elsewhere_refuses_through_the_check(config: LoreConfig) -> None:
    seen = []

    def queued(book_id):
        seen.append(book_id)
        return book_delete.DeleteRefused("processing", "queued")

    with pytest.raises(book_delete.DeleteRefused) as err:
        _delete(config, TWO, check=queued)
    assert err.value.code == "processing" and seen == [TWO]
    assert TWO in _entities(config)["Maren Vex"]


def test_an_upload_or_a_run_holding_the_library_refuses_a_delete(config: LoreConfig) -> None:
    with library_lock.hold(config.data_dir, exclusive=False):
        with pytest.raises(book_delete.DeleteRefused) as err:
            _delete(config, TWO)
    assert err.value.code == "busy"
    _delete(config, TWO)  # released: goes through


def test_no_run_starts_while_a_delete_holds_the_library(config: LoreConfig) -> None:
    conn = open_db(config.sqlite_path)
    try:
        with library_lock.hold(config.data_dir, exclusive=True):
            with pytest.raises(JobRefused):
                run_book(conn, config, ONE, [], now=NOW)
            with pytest.raises(library_lock.LibraryBusy):
                with library_lock.hold(config.data_dir, exclusive=False):
                    pass
        assert run_book(conn, config, ONE, [], now=NOW).state == jobs.DONE
    finally:
        conn.close()


def test_two_holders_of_the_shared_lock_do_not_block_each_other(tmp_path: Path) -> None:
    with library_lock.hold(tmp_path / "lib", exclusive=False):
        with library_lock.hold(tmp_path / "lib", exclusive=False):
            pass
        with pytest.raises(library_lock.LibraryBusy):
            with library_lock.hold(tmp_path / "lib", exclusive=True):
                pass


def test_a_failed_codex_refresh_does_not_undo_the_delete(config: LoreConfig) -> None:
    def broken(cfg, conn, book_id):
        raise RuntimeError("no embedder")

    report = _delete(config, TWO, codex_refresh=broken)
    assert "Zed" not in _entities(config)
    assert report.codex_missing  # said, not hidden
    conn = open_db(config.sqlite_path)
    try:
        assert jobs.get_job(conn, ONE).state == jobs.DONE
    finally:
        conn.close()


def test_the_lock_file_never_travels_into_a_template(config: LoreConfig, tmp_path: Path) -> None:
    from lore import sample

    with library_lock.hold(config.data_dir, exclusive=False):
        pass
    assert (config.data_dir / library_lock.LOCK_NAME).exists()
    summary = sample.make_template(config.data_dir, tmp_path / "root" / "sample", ONE,
                                   codex_refresh=_refresh())
    assert not (tmp_path / "root" / "sample" / library_lock.LOCK_NAME).exists()
    assert not [w for w in summary.warnings if "unexpected" in w]
