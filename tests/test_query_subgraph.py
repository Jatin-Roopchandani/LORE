from pathlib import Path

from lore.db.sqlite import open_db
from lore.query.models import EdgeRow, TemplateCall
from lore.query.subgraph import assemble, passages_from_chunks, run_template


class FakeKuzu:
    def __init__(self, rows: list[list]) -> None:
        self._rows = rows
        self.seen: tuple[str, dict] | None = None

    def execute(self, cypher: str, params: dict | None = None):
        self.seen = (cypher, params or {})
        return self._rows


def _row(chunk_id: str = "b1:1:0", subject: str = "Holmes") -> list:
    return [subject, "OPPOSES", "distrusts", "Moriarty", "ev", 0.9,
            "b1", 1, chunk_id]


def _seed(conn, chunk_id: str, text: str, book: str = "b1",
          chapter_seq: int = 1) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO books (id, title, author, source, file_path, status)"
        " VALUES (?, 'T', NULL, 'upload', '/x', 'chunked')", (book,)
    )
    conn.execute(
        "INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text)"
        " VALUES (?, ?, ?, 0, 0, ?)", (chunk_id, book, chapter_seq, text)
    )
    conn.commit()


def test_run_template_binds_slots_as_parameters() -> None:
    kuzu = FakeKuzu([_row()])
    run_template(kuzu, TemplateCall(template="neighbours",
                                    slots={"entity_id": "e1", "limit": 40}))
    cypher, params = kuzu.seen
    assert params == {"entity_id": "e1", "limit": 40}
    assert "e1" not in cypher      # the value never reaches the query text


def test_run_template_maps_rows_onto_edge_row() -> None:
    rows = run_template(FakeKuzu([_row()]),
                        TemplateCall(template="neighbours",
                                     slots={"entity_id": "e1", "limit": 40}))
    assert rows == [EdgeRow("Holmes", "OPPOSES", "distrusts", "Moriarty", "ev",
                            0.9, "b1", 1, "b1:1:0")]


def test_an_unknown_template_raises_rather_than_guessing() -> None:
    try:
        run_template(FakeKuzu([]), TemplateCall(template="nope", slots={}))
    except KeyError:
        return
    raise AssertionError("expected KeyError")


def test_assemble_numbers_passages_and_dedupes_chunks(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    _seed(conn, "b1:1:0", "first chunk")
    _seed(conn, "b1:2:0", "second chunk")
    rows = [EdgeRow("A", "P", "p", "B", "e", 0.5, "b1", 2, "b1:2:0"),
            EdgeRow("C", "P", "p", "D", "e", 0.5, "b1", 1, "b1:1:0"),
            EdgeRow("E", "P", "p", "F", "e", 0.5, "b1", 1, "b1:1:0")]
    evidence = assemble(conn, rows)
    assert [p.n for p in evidence.passages] == [1, 2]
    # ordered by (book, chapter), not by row order
    assert [p.chunk_id for p in evidence.passages] == ["b1:1:0", "b1:2:0"]
    assert evidence.truncated is False
    conn.close()


def test_assemble_skips_a_chunk_id_with_no_row_in_sqlite(tmp_path: Path) -> None:
    # An edge can carry a chunk_id the registry does not know; the edge
    # still counts, but there is no passage to cite.
    conn = open_db(tmp_path / "lore.db")
    _seed(conn, "b1:1:0", "known")
    rows = [EdgeRow("A", "P", "p", "B", "e", 0.5, "b1", 1, "b1:1:0"),
            EdgeRow("C", "P", "p", "D", "e", 0.5, "b1", 1, "ghost")]
    evidence = assemble(conn, rows)
    assert [p.chunk_id for p in evidence.passages] == ["b1:1:0"]
    assert len(evidence.edges) == 2
    conn.close()


def test_assemble_caps_total_characters_and_says_so(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    for i in range(5):
        _seed(conn, f"b1:{i}:0", "x" * 100)
    rows = [EdgeRow("A", "P", "p", "B", "e", 0.5, "b1", i, f"b1:{i}:0")
            for i in range(5)]
    evidence = assemble(conn, rows, max_chars=250)
    assert evidence.truncated is True
    assert sum(len(p.text) for p in evidence.passages) <= 250
    assert len(evidence.passages) >= 1     # never truncate to nothing
    conn.close()


def test_assemble_of_no_rows_is_empty_not_an_error(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    evidence = assemble(conn, [])
    assert evidence.edges == () and evidence.passages == ()
    assert evidence.truncated is False
    conn.close()


def test_passages_from_chunks_numbers_lance_rows() -> None:
    passages = passages_from_chunks([
        {"chunk_id": "b1:1:0", "book_id": "b1", "chapter_seq": 1, "text": "a"},
        {"chunk_id": "b1:2:0", "book_id": "b1", "chapter_seq": 2, "text": "b"},
    ])
    assert [(p.n, p.chunk_id) for p in passages] == [(1, "b1:1:0"), (2, "b1:2:0")]


# --- Brief change 1: edges are deduped on exact EdgeRow equality ----------


def test_assemble_collapses_an_edge_repeated_by_the_template(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    _seed(conn, "b1:1:0", "text")
    edge = EdgeRow("A", "P", "p", "B", "e", 0.5, "b1", 1, "b1:1:0")
    evidence = assemble(conn, [edge, edge, edge])
    assert evidence.edges == (edge,)
    conn.close()


def test_assemble_keeps_edges_that_differ_only_in_evidence(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    _seed(conn, "b1:1:0", "text")
    one = EdgeRow("A", "P", "p", "B", "said once", 0.5, "b1", 1, "b1:1:0")
    two = EdgeRow("A", "P", "p", "B", "said twice", 0.5, "b1", 1, "b1:1:0")
    evidence = assemble(conn, [one, two, one])
    assert evidence.edges == (one, two)
    conn.close()


def test_assemble_dedup_preserves_first_occurrence_order(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    _seed(conn, "b1:1:0", "one")
    _seed(conn, "b1:2:0", "two", chapter_seq=2)
    late = EdgeRow("Z", "P", "p", "Y", "e", 0.5, "b1", 2, "b1:2:0")
    early = EdgeRow("A", "P", "p", "B", "e", 0.5, "b1", 1, "b1:1:0")
    evidence = assemble(conn, [late, early, late, early])
    assert evidence.edges == (late, early)   # row order, not reading order
    conn.close()


# --- Brief change 2: a cross-book question keeps both books under the cap --


def _two_books(conn, per_book: int, size: int) -> list[EdgeRow]:
    rows: list[EdgeRow] = []
    for book in ("b1", "b2"):
        for ch in range(1, per_book + 1):
            chunk_id = f"{book}:{ch}:0"
            _seed(conn, chunk_id, book[-1] * size, book=book, chapter_seq=ch)
            rows.append(EdgeRow("A", "P", "p", "B", "e", 0.5, book, ch, chunk_id))
    return rows


def test_over_budget_cross_book_evidence_keeps_both_books(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    rows = _two_books(conn, per_book=4, size=100)
    evidence = assemble(conn, rows, max_chars=400)
    assert evidence.truncated is True
    assert sum(len(p.text) for p in evidence.passages) <= 400
    assert {p.book_id for p in evidence.passages} == {"b1", "b2"}
    # round-robin: two from each, the earliest chapters of each book
    assert [p.chunk_id for p in evidence.passages] == [
        "b1:1:0", "b1:2:0", "b2:1:0", "b2:2:0"]
    conn.close()


def test_kept_cross_book_passages_are_numbered_in_reading_order(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    rows = _two_books(conn, per_book=3, size=100)
    # Kept in round-robin order b1:1, b2:1, b1:2, b2:2 -- but cited in
    # reading order.
    evidence = assemble(conn, list(reversed(rows)), max_chars=400)
    assert [p.n for p in evidence.passages] == [1, 2, 3, 4]
    keys = [(p.book_id, p.chapter_seq) for p in evidence.passages]
    assert keys == [("b1", 1), ("b1", 2), ("b2", 1), ("b2", 2)]
    conn.close()


def test_cross_book_evidence_under_budget_is_untouched(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    rows = _two_books(conn, per_book=2, size=10)
    evidence = assemble(conn, rows)
    assert evidence.truncated is False
    assert [(p.n, p.chunk_id) for p in evidence.passages] == [
        (1, "b1:1:0"), (2, "b1:2:0"), (3, "b2:1:0"), (4, "b2:2:0")]
    conn.close()


def test_single_book_over_budget_still_cuts_the_latest_chapters(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    for ch in range(1, 5):
        _seed(conn, f"b1:{ch}:0", "x" * 100, chapter_seq=ch)
    rows = [EdgeRow("A", "P", "p", "B", "e", 0.5, "b1", ch, f"b1:{ch}:0")
            for ch in (4, 3, 2, 1)]
    evidence = assemble(conn, rows, max_chars=250)
    assert evidence.truncated is True
    assert [(p.n, p.chunk_id) for p in evidence.passages] == [
        (1, "b1:1:0"), (2, "b1:2:0")]
    conn.close()
