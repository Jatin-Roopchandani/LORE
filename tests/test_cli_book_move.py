"""`lore book move` and `lore book remove-from-graph`: prompts, messages and
what each runs. The stores and the load are doubles here; the graph itself is
tested in test_book_remove.py."""
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from lore import worlds
from lore.cli import app
from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.graph.loader import LoadReport
from lore.orchestrator import jobs
from tests.fixtures import seed_book

runner = CliRunner()
BOOK = "abcdef0123456789"


@pytest.fixture
def lib(tmp_path: Path, monkeypatch):
    """One book in "Old Realm"; "New Realm" exists and is empty."""
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    seed_book(BOOK)
    config = LoreConfig.load()
    conn = open_db(config.sqlite_path)
    old = worlds.create_world(conn, "Old Realm")
    new = worlds.create_world(conn, "New Realm")
    worlds.assign(conn, BOOK, old)
    conn.close()
    return config, old, new


def _db(config):
    return open_db(config.sqlite_path)


class _Doubles:
    """Records the order of remove and load, and the book's world at load."""

    def __init__(self, config) -> None:
        self.config = config
        self.calls: list[tuple] = []

    def remove(self, config, conn, book_id):
        self.calls.append(("remove", worlds.world_of(conn, book_id).name))

    def load(self, config, conn, book_id, api_key=None):
        self.calls.append(("load", worlds.world_of(conn, book_id).name))
        return LoadReport(book_id=book_id, entities=1, chunks=1)

    def patches(self):
        return (
            patch("lore.orchestrator.stages._remove_from_stores", self.remove),
            patch("lore.cli._load_book_sync", self.load),
        )


def _run(config, args, input=None):
    doubles = _Doubles(config)
    p1, p2 = doubles.patches()
    with p1, p2:
        result = runner.invoke(app, args, input=input)
    return result, doubles


def test_move_runs_remove_then_load_in_the_new_world(lib) -> None:
    config, _old, _new = lib
    result, doubles = _run(config, ["book", "move", BOOK, "--world", "New Realm"])
    assert result.exit_code == 0, result.output
    assert doubles.calls == [("remove", "Old Realm"), ("load", "New Realm")]
    assert "moved to world 'New Realm'" in result.output
    conn = _db(config)
    assert worlds.world_of(conn, BOOK).name == "New Realm"
    assert jobs.get_job(conn, BOOK).state == jobs.DONE
    assert conn.execute("SELECT status FROM books WHERE id = ?",
                        (BOOK,)).fetchone()[0] == "graphed"
    conn.close()


def test_move_finds_the_book_by_title_and_world_by_name_ignoring_case(lib) -> None:
    config, _old, _new = lib
    result, doubles = _run(config, ["book", "move", "T", "--world", "new realm"])
    assert result.exit_code == 0, result.output
    assert doubles.calls[-1] == ("load", "New Realm")


def test_move_to_a_missing_world_asks_first_and_declining_changes_nothing(lib) -> None:
    config, _old, _new = lib
    result, doubles = _run(config, ["book", "move", BOOK, "--world", "Brand New"],
                           input="n\n")
    assert result.exit_code == 1
    assert "create world 'Brand New'?" in result.output
    assert doubles.calls == []
    conn = _db(config)
    assert worlds.get_world(conn, "Brand New") is None
    assert worlds.world_of(conn, BOOK).name == "Old Realm"
    assert jobs.get_job(conn, BOOK) is None
    conn.close()


def test_move_to_a_missing_world_creates_it_after_confirmation(lib) -> None:
    config, _old, _new = lib
    result, doubles = _run(config, ["book", "move", BOOK, "--world", "Brand New"],
                           input="y\n")
    assert result.exit_code == 0, result.output
    assert doubles.calls == [("remove", "Old Realm"), ("load", "Brand New")]
    conn = _db(config)
    assert worlds.world_of(conn, BOOK).name == "Brand New"
    conn.close()


def test_move_yes_creates_a_missing_world_without_asking(lib) -> None:
    config, _old, _new = lib
    result, doubles = _run(
        config, ["book", "move", BOOK, "--world", "Brand New", "--yes"])
    assert result.exit_code == 0, result.output
    assert "create world" not in result.output
    assert doubles.calls[-1] == ("load", "Brand New")


def test_move_to_the_books_own_world_is_a_no_op(lib) -> None:
    config, _old, _new = lib
    result, doubles = _run(config, ["book", "move", BOOK, "--world", "Old Realm"])
    assert result.exit_code == 0, result.output
    assert "already in world 'Old Realm'" in result.output
    assert doubles.calls == []
    conn = _db(config)
    assert jobs.get_job(conn, BOOK) is None  # nothing was started
    conn.close()


def test_move_failure_exits_non_zero_and_a_second_run_continues(lib) -> None:
    config, _old, _new = lib
    doubles = _Doubles(config)

    def dying_load(config, conn, book_id, api_key=None):
        raise RuntimeError("disk full")

    with patch("lore.orchestrator.stages._remove_from_stores", doubles.remove), \
            patch("lore.cli._load_book_sync", dying_load):
        failed = runner.invoke(app, ["book", "move", BOOK, "--world", "New Realm"])
    assert failed.exit_code == 1
    assert "disk full" in failed.output and "again to continue" in failed.output

    result, doubles2 = _run(config, ["book", "move", BOOK, "--world", "New Realm"])
    assert result.exit_code == 0, result.output
    assert doubles2.calls == [("load", "New Realm")]  # remove had already run


def test_move_unknown_book_fails(lib) -> None:
    config, _old, _new = lib
    result, doubles = _run(config, ["book", "move", "nope", "--world", "New Realm"])
    assert result.exit_code == 1 and "no book matches" in result.output
    assert doubles.calls == []


def test_move_refuses_while_the_book_is_running(lib) -> None:
    config, _old, _new = lib
    conn = _db(config)
    jobs.start_job(conn, BOOK, "load", 1)
    conn.close()
    result, doubles = _run(
        config, ["book", "move", BOOK, "--world", "Brand New", "--yes"])
    assert result.exit_code == 1 and "already running" in result.output
    assert doubles.calls == []
    conn = _db(config)
    assert worlds.get_world(conn, "Brand New") is None  # not left behind
    conn.close()


def test_remove_from_graph_asks_and_declining_removes_nothing(lib) -> None:
    config, _old, _new = lib
    removed: list[str] = []
    with patch("lore.cli.remove_book_from_graph",
               lambda *a: removed.append(a[3])):
        result = runner.invoke(app, ["book", "remove-from-graph", BOOK],
                               input="n\n")
    assert result.exit_code == 1
    assert "from the graph?" in result.output and "caches stay" in result.output
    assert removed == []


def test_remove_from_graph_yes_removes_and_reports(lib) -> None:
    config, _old, _new = lib
    result = runner.invoke(app, ["book", "remove-from-graph", BOOK, "--yes"])
    assert result.exit_code == 0, result.output  # real (empty) stores
    assert "removed 0 chunks" in result.output
    conn = _db(config)
    assert conn.execute("SELECT COUNT(*) FROM chunks WHERE book_id = ?",
                        (BOOK,)).fetchone()[0] == 1  # the registry stays
    conn.close()


def test_remove_from_graph_confirmed_at_the_prompt(lib) -> None:
    config, _old, _new = lib
    result = runner.invoke(app, ["book", "remove-from-graph", BOOK], input="y\n")
    assert result.exit_code == 0, result.output


def test_remove_from_graph_leaves_a_finished_job_ready_to_load_again(lib) -> None:
    config, _old, _new = lib
    conn = _db(config)
    jobs.start_job(conn, BOOK, "load", 1)
    jobs.done(conn, BOOK)
    conn.close()
    result = runner.invoke(app, ["book", "remove-from-graph", BOOK, "--yes"])
    assert result.exit_code == 0, result.output
    conn = _db(config)
    job = jobs.get_job(conn, BOOK)
    assert (job.state, job.stage) == (jobs.IDLE, "load")
    conn.close()


def test_remove_from_graph_refuses_a_running_book(lib) -> None:
    config, _old, _new = lib
    conn = _db(config)
    jobs.start_job(conn, BOOK, "load", 1)
    conn.close()
    result = runner.invoke(app, ["book", "remove-from-graph", BOOK, "--yes"])
    assert result.exit_code == 1 and "running" in result.output


def test_move_refused_at_the_claim_leaves_no_target_and_no_stray_world(
    lib, monkeypatch,
) -> None:
    config, _old, _new = lib

    def refuse(*args, **kwargs):
        raise jobs.JobRefused("a job is already running for this book")

    monkeypatch.setattr(jobs, "claim_job", refuse)
    result, doubles = _run(
        config, ["book", "move", BOOK, "--world", "Brand New", "--yes"])
    assert result.exit_code == 1 and "move refused" in result.output
    assert doubles.calls == []
    conn = _db(config)
    assert jobs.move_target(conn, BOOK) is None
    assert worlds.get_world(conn, "Brand New") is None
    conn.close()


def test_move_to_the_own_world_with_an_unfinished_ordinary_job_is_not_a_move(
    lib,
) -> None:
    config, _old, _new = lib
    conn = _db(config)
    jobs.start_job(conn, BOOK, "load", 1)
    jobs.fail(conn, BOOK, "boom")
    before = jobs.get_job(conn, BOOK)
    conn.close()
    result, doubles = _run(config, ["book", "move", BOOK, "--world", "Old Realm"])
    assert result.exit_code == 0, result.output
    assert "not a move" in result.output and "continue it instead" in result.output
    assert doubles.calls == []  # no move stage ran over the stopped load
    conn = _db(config)
    assert jobs.get_job(conn, BOOK) == before
    assert jobs.move_target(conn, BOOK) is None
    conn.close()
