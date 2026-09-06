from pathlib import Path

from typer.testing import CliRunner

from lore.cli import app
from lore.db.sqlite import open_db
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


def _rows(tmp_path: Path) -> tuple[list, list]:
    conn = open_db(tmp_path / "data" / "lore.db")
    try:
        books = conn.execute("SELECT id, title FROM books").fetchall()
        chunks = conn.execute("SELECT id FROM chunks").fetchall()
    finally:
        conn.close()
    return books, chunks


def test_force_reingests_without_duplicating_rows(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    path = _fixture(tmp_path)

    first = runner.invoke(app, ["ingest", str(path)])
    assert first.exit_code == 0, first.output
    books_before, chunks_before = _rows(tmp_path)
    assert len(books_before) == 1
    assert len(chunks_before) > 0

    # Without --force it stays a no-op and leaves the rows alone.
    second = runner.invoke(app, ["ingest", str(path)])
    assert second.exit_code == 0, second.output
    assert "no-op" in second.output
    assert _rows(tmp_path) == (books_before, chunks_before)

    # With --force it really re-ingests: same counts, no duplicates.
    third = runner.invoke(app, ["ingest", str(path), "--force"])
    assert third.exit_code == 0, third.output
    assert "no-op" not in third.output
    assert "cleared previous ingest" in third.output
    books_after, chunks_after = _rows(tmp_path)
    assert books_after == books_before
    assert sorted(chunks_after) == sorted(chunks_before)
    assert len(chunks_after) == len(set(chunks_after))


def test_force_on_an_unknown_book_is_a_plain_ingest(tmp_path: Path, monkeypatch) -> None:
    # --force must not require a prior ingest to exist.
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["ingest", str(_fixture(tmp_path)), "--force"])
    assert result.exit_code == 0, result.output
    assert "cleared previous ingest" not in result.output
    assert "2 chapters" in result.output


def test_force_clears_jobs_rows_that_reference_the_book(
    tmp_path: Path, monkeypatch
) -> None:
    # jobs.book_id is a FK to books(id) and open_db turns FK enforcement
    # on, so a book that reached an extraction run would make the parent
    # DELETE fail unless its jobs go first. Re-ingesting is exactly what
    # you do after an extraction went wrong, so this is the normal case.
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    path = _fixture(tmp_path)
    assert runner.invoke(app, ["ingest", str(path)]).exit_code == 0

    db = tmp_path / "data" / "lore.db"
    conn = open_db(db)
    try:
        book_id = conn.execute("SELECT id FROM books").fetchone()[0]
        conn.execute(
            "INSERT INTO jobs (kind, book_id, state) VALUES ('extract', ?, 'done')",
            (book_id,),
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["ingest", str(path), "--force"])
    assert result.exit_code == 0, result.output
    conn = open_db(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 1
    finally:
        conn.close()
