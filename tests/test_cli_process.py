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


def test_graph_reports_empty_store(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    assert "0" in result.output
