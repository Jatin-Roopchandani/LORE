from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from lore.cli import app
from lore.graph.loader import LoadReport, PartialLoadError
from lore.orchestrator.run import RunReport

runner = CliRunner()
BOOK = "abcdef0123456789"


def test_process_runs_extraction_then_load(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    report = LoadReport(book_id=BOOK, entities=4, alias_links=1, rel_edges=7,
                        chunks=2, mentions=6)
    with (
        patch("lore.cli.run_extraction",
              return_value=RunReport(book_id=BOOK, units_total=1, units_completed=1)) as extract,
        patch("lore.cli._load_book_sync", return_value=report) as load,
    ):
        result = runner.invoke(app, ["process", BOOK])
    assert result.exit_code == 0, result.output
    assert extract.called and load.called
    assert "7 relationships" in result.output


def test_process_skip_extract_only_loads(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    report = LoadReport(book_id=BOOK, entities=1, alias_links=0, rel_edges=1,
                        chunks=1, mentions=1)
    with (
        patch("lore.cli.run_extraction") as extract,
        patch("lore.cli._load_book_sync", return_value=report),
    ):
        result = runner.invoke(app, ["process", BOOK, "--skip-extract"])
    assert result.exit_code == 0
    assert not extract.called


def test_process_reports_dropped_mentions(tmp_path: Path, monkeypatch) -> None:
    """A non-zero dropped_mentions count must be visible, not silent."""
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    report = LoadReport(book_id=BOOK, entities=1, alias_links=0, rel_edges=1,
                        chunks=1, mentions=1, dropped_mentions=3)
    with (
        patch("lore.cli.run_extraction",
              return_value=RunReport(book_id=BOOK, units_total=1, units_completed=1)),
        patch("lore.cli._load_book_sync", return_value=report),
    ):
        result = runner.invoke(app, ["process", BOOK])
    assert result.exit_code == 0, result.output
    assert "3 mention(s) dropped" in result.output


def test_process_says_nothing_when_no_mentions_were_dropped(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    report = LoadReport(book_id=BOOK, entities=1, alias_links=0, rel_edges=1,
                        chunks=1, mentions=1, dropped_mentions=0)
    with (
        patch("lore.cli.run_extraction",
              return_value=RunReport(book_id=BOOK, units_total=1, units_completed=1)),
        patch("lore.cli._load_book_sync", return_value=report),
    ):
        result = runner.invoke(app, ["process", BOOK])
    assert result.exit_code == 0, result.output
    assert "mention(s) dropped" not in result.output


def test_process_reports_a_load_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    with (
        patch("lore.cli.run_extraction",
              return_value=RunReport(book_id=BOOK, units_total=1, units_completed=1)),
        patch("lore.cli._load_book_sync", side_effect=ValueError("no completed units")),
    ):
        result = runner.invoke(app, ["process", BOOK])
    assert result.exit_code == 1
    assert "no completed units" in result.output


def test_process_reports_partial_load_and_does_not_claim_success(
    tmp_path: Path, monkeypatch
) -> None:
    """PartialLoadError means the graph committed but vectors did not.

    The book is genuinely half-loaded: `process` must fail loudly (exit
    non-zero), never print the success summary, and tell the user to
    re-run rather than silently treating the book as ready.
    """
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    with (
        patch("lore.cli.run_extraction",
              return_value=RunReport(book_id=BOOK, units_total=1, units_completed=1)),
        patch(
            "lore.cli._load_book_sync",
            side_effect=PartialLoadError(
                f"book {BOOK}: the graph was written but the vector store was"
                " left incomplete. Re-run the load to repair it."
            ),
        ),
    ):
        result = runner.invoke(app, ["process", BOOK])
    assert result.exit_code == 1
    assert "vector store was" in result.output
    assert "re-run" in result.output.lower()
    # The success summary line (entity/edge counts) must never appear.
    assert "entities" not in result.output.lower()


def test_process_with_failed_units_does_not_claim_success(
    tmp_path: Path, monkeypatch
) -> None:
    """1 of 2 units failed: the load still runs (partial graph is useful),
    but `process` must not print a bare success line at exit 0 — that
    would tell the user their book is fully in the graph when it isn't.
    """
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    report = LoadReport(book_id=BOOK, entities=4, alias_links=1, rel_edges=7,
                        chunks=2, mentions=6)
    extraction = RunReport(book_id=BOOK, units_total=2, units_completed=1,
                           units_failed=1)
    with (
        patch("lore.cli.run_extraction", return_value=extraction) as extract,
        patch("lore.cli._load_book_sync", return_value=report) as load,
    ):
        result = runner.invoke(app, ["process", BOOK])
    assert extract.called and load.called
    assert result.exit_code != 0, result.output
    assert "--resume" in result.output
    assert "incomplete" in result.output.lower()


def test_process_with_quota_truncated_extraction_does_not_claim_success(
    tmp_path: Path, monkeypatch
) -> None:
    """Quota exhausted after 1 of 5 units: same non-zero-exit requirement,
    driven by `stopped_reason` rather than `units_failed`.
    """
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    report = LoadReport(book_id=BOOK, entities=1, alias_links=0, rel_edges=1,
                        chunks=1, mentions=1)
    extraction = RunReport(book_id=BOOK, units_total=5, units_completed=1,
                           stopped_reason="weekly GPU-hour quota exhausted")
    with (
        patch("lore.cli.run_extraction", return_value=extraction),
        patch("lore.cli._load_book_sync", return_value=report),
    ):
        result = runner.invoke(app, ["process", BOOK])
    assert result.exit_code != 0, result.output
    assert "--resume" in result.output
    assert "incomplete" in result.output.lower()
    assert "quota exhausted" in result.output.lower()


def test_graph_reports_empty_store(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    assert "0" in result.output


def test_load_book_sync_closes_the_client_on_the_loop_it_used(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression: the provider close must share the load's event loop.

    Every other test in this file patches `_load_book_sync` away, so a
    second `asyncio.run()` around the close went unnoticed until a real
    run — httpx binds its sockets to the running loop, and closing them
    from a fresh loop raises "Event loop is closed" *after* the graph is
    already committed, reporting `process failed` on a successful load.
    """
    import asyncio

    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    seen: dict[str, object] = {}
    report = LoadReport(book_id=BOOK, entities=1, alias_links=0, rel_edges=1,
                        chunks=1, mentions=1)

    async def fake_load(*args, **kwargs):
        seen["load"] = asyncio.get_running_loop()
        return report

    async def fake_aclose() -> None:
        seen["close"] = asyncio.get_running_loop()

    from lore.config import LoreConfig

    config = LoreConfig.load()
    config.ensure_dirs()
    with (
        patch("lore.cli.load_book", fake_load),
        patch("lore.cli.make_ollama_pair", return_value=(None, None, fake_aclose)),
    ):
        from lore.cli import _load_book_sync

        result = _load_book_sync(config, None, BOOK)
    assert result is report
    assert seen["close"] is seen["load"]


# ---------------------------------------------------------------------------
# Final fix round, item 3: a unit that completes with every chunk
# dead-lettered has no failed units and no stopped_reason, so the Task 7 fix
# never saw it — `process` printed a success line over an empty graph and
# exited 0. `chunks_dead` is the missing signal, and it must always be shown.
# ---------------------------------------------------------------------------


def _dead_letter_run(monkeypatch, tmp_path: Path, extraction: RunReport,
                     report: LoadReport):
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    with (
        patch("lore.cli.run_extraction", return_value=extraction),
        patch("lore.cli._load_book_sync", return_value=report),
    ):
        return runner.invoke(app, ["process", BOOK])


def test_process_fails_when_every_chunk_dead_letters(
    tmp_path: Path, monkeypatch
) -> None:
    """The unit "completed" — with nothing in it. Never exit 0 for that."""
    result = _dead_letter_run(
        monkeypatch, tmp_path,
        RunReport(book_id=BOOK, units_total=1, units_completed=1,
                  chunks_ok=0, chunks_dead=1),
        LoadReport(book_id=BOOK, entities=0, alias_links=0, rel_edges=0,
                   chunks=1, mentions=0),
    )
    assert result.exit_code != 0, result.output
    assert "dead-lettered" in result.output
    assert "incomplete" in result.output.lower()
    assert "--resume" in result.output


def test_process_reports_dead_chunks_among_good_ones(
    tmp_path: Path, monkeypatch
) -> None:
    """The untested middle: 5 chunks landed, 3 were lost. Say so."""
    result = _dead_letter_run(
        monkeypatch, tmp_path,
        RunReport(book_id=BOOK, units_total=1, units_completed=1,
                  chunks_ok=5, chunks_dead=3),
        LoadReport(book_id=BOOK, entities=4, alias_links=1, rel_edges=7,
                   chunks=8, mentions=6),
    )
    assert result.exit_code != 0, result.output
    assert "5 chunks extracted" in result.output
    assert "3 dead-lettered" in result.output


def test_process_echoes_chunk_counts_on_a_clean_run(
    tmp_path: Path, monkeypatch
) -> None:
    """`extract` always prints them; `process` printed nothing at all."""
    result = _dead_letter_run(
        monkeypatch, tmp_path,
        RunReport(book_id=BOOK, units_total=2, units_completed=2,
                  chunks_ok=9, chunks_dead=0),
        LoadReport(book_id=BOOK, entities=4, alias_links=1, rel_edges=7,
                   chunks=9, mentions=6),
    )
    assert result.exit_code == 0, result.output
    assert "9 chunks extracted" in result.output
    assert "0 dead-lettered" in result.output


def test_process_resume_with_nothing_pending_still_succeeds(
    tmp_path: Path, monkeypatch
) -> None:
    """A --resume over an already-finished book reports 0/3 units run and is
    a complete success. `units_completed < units_total` would false-fire."""
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    with (
        patch("lore.cli.run_extraction",
              return_value=RunReport(book_id=BOOK, units_total=3,
                                     units_completed=0)),
        patch("lore.cli._load_book_sync",
              return_value=LoadReport(book_id=BOOK, entities=4, alias_links=1,
                                      rel_edges=7, chunks=2, mentions=6)),
    ):
        result = runner.invoke(app, ["process", BOOK, "--resume"])
    assert result.exit_code == 0, result.output
    assert "incomplete" not in result.output.lower()


# ---------------------------------------------------------------------------
# Final fix round, item 5: `process` says "4 entities" where `graph` says
# "6 Entity", because each merged alias is persisted as its own node linked
# by ALIAS_OF. Name what is being counted.
# ---------------------------------------------------------------------------


def test_process_labels_its_entity_count_as_canonical(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    with (
        patch("lore.cli.run_extraction",
              return_value=RunReport(book_id=BOOK, units_total=1,
                                     units_completed=1, chunks_ok=2)),
        patch("lore.cli._load_book_sync",
              return_value=LoadReport(book_id=BOOK, entities=4, alias_links=2,
                                      rel_edges=7, chunks=2, mentions=6)),
    ):
        result = runner.invoke(app, ["process", BOOK])
    assert result.exit_code == 0, result.output
    assert "4 canonical entities" in result.output
