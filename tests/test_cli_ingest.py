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


def test_ingest_records_series_from_the_epub(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    path = make_epub(tmp_path / "s.epub", title="Book Two", author="A",
                     chapters=[("One", _long_html("one"))], series=("The Saga", 2))
    assert runner.invoke(app, ["ingest", str(path)]).exit_code == 0
    conn = open_db(tmp_path / "data" / "lore.db")
    assert conn.execute("SELECT series, series_index FROM books").fetchall() == [("The Saga", 2.0)]
    conn.close()


def _library(tmp_path: Path, monkeypatch) -> list[str]:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    ids = []
    for title in ("First Tale", "Second Tale"):
        path = make_epub(tmp_path / f"{title}.epub", title=title, author="A",
                         chapters=[("One", _long_html(title))])
        runner.invoke(app, ["ingest", str(path)])
    conn = open_db(tmp_path / "data" / "lore.db")
    ids = [r[0] for r in conn.execute("SELECT id FROM books ORDER BY title")]
    conn.close()
    return ids


def _series(tmp_path: Path) -> list[tuple]:
    conn = open_db(tmp_path / "data" / "lore.db")
    try:
        return conn.execute("SELECT title, series, series_index FROM books ORDER BY title").fetchall()
    finally:
        conn.close()


def test_book_series_sets_by_id_or_title_and_clears(tmp_path: Path, monkeypatch) -> None:
    first, _second = _library(tmp_path, monkeypatch)
    out = runner.invoke(app, ["book", "series", first, "--series", "The Saga", "--index", "1"])
    assert out.exit_code == 0, out.output
    out = runner.invoke(app, ["book", "series", "second tale", "--series", "The Saga", "--index", "2.5"])
    assert out.exit_code == 0, out.output
    assert _series(tmp_path) == [("First Tale", "The Saga", 1.0), ("Second Tale", "The Saga", 2.5)]
    out = runner.invoke(app, ["book", "series", first, "--clear"])
    assert out.exit_code == 0, out.output
    assert _series(tmp_path)[0] == ("First Tale", None, None)


def test_book_series_rejects_an_unknown_book_and_bad_arguments(tmp_path: Path, monkeypatch) -> None:
    first, _ = _library(tmp_path, monkeypatch)
    missing = runner.invoke(app, ["book", "series", "No Such Book", "--series", "S", "--index", "1"])
    assert missing.exit_code != 0 and "no book" in missing.output.lower()
    assert runner.invoke(app, ["book", "series", first, "--series", "S"]).exit_code != 0  # no index
    assert runner.invoke(app, ["book", "series", first, "--index", "1"]).exit_code != 0  # no series
    assert runner.invoke(app, ["book", "series", first]).exit_code != 0
    assert runner.invoke(app, ["book", "series", first, "--clear", "--series", "S"]).exit_code != 0
    assert runner.invoke(app, ["book", "series", first, "--series", "S", "--index", "x"]).exit_code != 0
    assert runner.invoke(app, ["book", "series", first, "--series", "S", "--index", "nan"]).exit_code != 0
    assert _series(tmp_path)[0] == ("First Tale", None, None)


def test_book_series_ambiguous_title_is_refused(tmp_path: Path, monkeypatch) -> None:
    _library(tmp_path, monkeypatch)
    out = runner.invoke(app, ["book", "series", "Tale", "--series", "S", "--index", "1"])
    assert out.exit_code != 0 and "more than one" in out.output.lower()
    assert all(row[1] is None for row in _series(tmp_path))


def test_books_listing_shows_the_series(tmp_path: Path, monkeypatch) -> None:
    first, _ = _library(tmp_path, monkeypatch)
    runner.invoke(app, ["book", "series", first, "--series", "The Saga", "--index", "1"])
    out = runner.invoke(app, ["books"]).output
    assert "The Saga #1" in out
