from pathlib import Path

from typer.testing import CliRunner

from lore.cli import app
from tests.fixtures import make_epub

runner = CliRunner()


def _long_html(tag: str) -> str:
    body = " ".join(f"{tag} sentence number {i} with plenty of words." for i in range(40))
    return f"<p>{body}</p>"


def _fixture(tmp_path: Path) -> Path:
    return make_epub(
        tmp_path / "book.epub",
        title="CLI Test Book",
        author="A",
        chapters=[("One", _long_html("one")), ("Two", _long_html("two"))],
    )


def test_ingest_then_noop(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    path = _fixture(tmp_path)

    first = runner.invoke(app, ["ingest", str(path)])
    assert first.exit_code == 0, first.output
    assert "CLI Test Book" in first.output
    assert "2 chapters" in first.output

    second = runner.invoke(app, ["ingest", str(path)])
    assert second.exit_code == 0
    assert "no-op" in second.output


def test_books_lists_library(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["ingest", str(_fixture(tmp_path))])

    result = runner.invoke(app, ["books"])
    assert result.exit_code == 0
    assert "CLI Test Book" in result.output
    assert "chunked" in result.output


def test_unsupported_extension_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    bad = tmp_path / "book.txt"
    bad.write_text("plain text")
    result = runner.invoke(app, ["ingest", str(bad)])
    assert result.exit_code != 0
