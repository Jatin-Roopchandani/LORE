from pathlib import Path

import pytest

from lore.db.sqlite import open_db
from lore.query.models import EdgeRow, Evidence, TemplateCall
from lore.query.subgraph import assemble, passages_from_chunks, relevance, run_template


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


def test_assemble_dedup_keeps_one_of_each_edge_in_content_order(tmp_path: Path) -> None:
    # Task 13 changed this: the pin was `evidence.edges == (late, early)`,
    # "row order, not reading order". Row order is exactly what varied
    # between runs, so the edges are now ordered on their own content and a
    # repeated row still collapses to one.
    conn = open_db(tmp_path / "lore.db")
    _seed(conn, "b1:1:0", "one")
    _seed(conn, "b1:2:0", "two", chapter_seq=2)
    late = EdgeRow("Z", "P", "p", "Y", "e", 0.5, "b1", 2, "b1:2:0")
    early = EdgeRow("A", "P", "p", "B", "e", 0.5, "b1", 1, "b1:1:0")
    evidence = assemble(conn, [late, early, late, early])
    assert evidence.edges == (early, late)
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


# --- Task 9b: select by relevance to the question, cite in reading order ---
#
# Task 9's graph agent lost to plain vector search because evidence was
# ordered by chapter and then cut, so the earliest chapters survived rather
# than the passages that answer the question.


def _edge(book: str, ch: int, chunk_id: str, subject: str = "A") -> EdgeRow:
    return EdgeRow(subject, "P", "p", "B", "e", 0.5, book, ch, chunk_id)


def test_relevance_is_the_dot_product_with_each_stored_vector() -> None:
    scores = relevance([1.0, 0.0], {"c1": [0.6, 0.8], "c2": [0.0, 1.0]})
    assert scores == {"c1": pytest.approx(0.6), "c2": pytest.approx(0.0)}


def test_a_relevant_late_chapter_survives_the_cap_and_early_noise_is_dropped(
        tmp_path: Path) -> None:
    # THE regression test for Task 9: the budget fits two passages, the
    # answer sits in chapter 12, and chapters 1-4 are irrelevant.
    conn = open_db(tmp_path / "lore.db")
    rows = []
    for ch in (1, 2, 3, 4, 12):
        chunk_id = f"b1:{ch}:0"
        _seed(conn, chunk_id, "x" * 100, chapter_seq=ch)
        rows.append(_edge("b1", ch, chunk_id))
    scores = {"b1:1:0": 0.10, "b1:2:0": 0.20, "b1:3:0": 0.55,
              "b1:4:0": 0.15, "b1:12:0": 0.90}
    evidence = assemble(conn, rows, max_chars=200, scores=scores)
    assert evidence.truncated is True
    assert [p.chunk_id for p in evidence.passages] == ["b1:3:0", "b1:12:0"]
    conn.close()


def test_ranked_selection_keeps_both_books_when_one_book_scores_highest(
        tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    rows = _two_books(conn, per_book=4, size=100)
    # Every b1 passage outscores every b2 passage.
    scores = {f"b1:{ch}:0": 0.9 - ch / 100 for ch in range(1, 5)}
    scores.update({f"b2:{ch}:0": 0.3 - ch / 100 for ch in range(1, 5)})
    evidence = assemble(conn, rows, max_chars=300, scores=scores)
    assert {p.book_id for p in evidence.passages} == {"b1", "b2"}
    assert len(evidence.passages) == 3
    conn.close()


def test_ranked_selection_takes_each_books_most_relevant_not_its_earliest(
        tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    rows = _two_books(conn, per_book=4, size=100)
    scores = {"b1:1:0": 0.1, "b1:2:0": 0.2, "b1:3:0": 0.3, "b1:4:0": 0.8,
              "b2:1:0": 0.1, "b2:2:0": 0.7, "b2:3:0": 0.2, "b2:4:0": 0.6}
    evidence = assemble(conn, rows, max_chars=400, scores=scores)
    assert {p.chunk_id for p in evidence.passages} == {
        "b1:4:0", "b1:3:0", "b2:2:0", "b2:4:0"}
    conn.close()


def test_ranked_passages_are_numbered_in_reading_order(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    rows = _two_books(conn, per_book=3, size=100)
    scores = {"b1:1:0": 0.1, "b1:2:0": 0.5, "b1:3:0": 0.9,
              "b2:1:0": 0.8, "b2:2:0": 0.2, "b2:3:0": 0.6}
    evidence = assemble(conn, rows, max_chars=400, scores=scores)
    assert [(p.n, p.chunk_id) for p in evidence.passages] == [
        (1, "b1:2:0"), (2, "b1:3:0"), (3, "b2:1:0"), (4, "b2:3:0")]
    conn.close()


def test_a_chunk_with_no_stored_vector_ranks_last_but_is_not_dropped(
        tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    for ch in (1, 2, 3):
        _seed(conn, f"b1:{ch}:0", "x" * 100, chapter_seq=ch)
    rows = [_edge("b1", ch, f"b1:{ch}:0") for ch in (1, 2, 3)]
    # b1:1:0 has no vector, and a negative cosine is still a real score.
    scores = {"b1:2:0": -0.2, "b1:3:0": 0.4}
    capped = assemble(conn, rows, max_chars=200, scores=scores)
    assert [p.chunk_id for p in capped.passages] == ["b1:2:0", "b1:3:0"]
    roomy = assemble(conn, rows, max_chars=1000, scores=scores)
    assert [p.chunk_id for p in roomy.passages] == ["b1:1:0", "b1:2:0", "b1:3:0"]
    conn.close()


def test_ranked_edges_follow_their_chunks_relevance_and_stay_deduped(
        tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    for ch in (1, 2):
        _seed(conn, f"b1:{ch}:0", "x" * 10, chapter_seq=ch)
    early = _edge("b1", 1, "b1:1:0", subject="Early")
    late = _edge("b1", 2, "b1:2:0", subject="Late")
    ghost = _edge("b1", 3, "ghost", subject="Ghost")
    evidence = assemble(conn, [early, ghost, late, early],
                        scores={"b1:1:0": 0.1, "b1:2:0": 0.9})
    assert evidence.edges == (late, early, ghost)
    conn.close()


def test_ranked_selection_never_truncates_to_nothing(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    _seed(conn, "b1:1:0", "x" * 500)
    evidence = assemble(conn, [_edge("b1", 1, "b1:1:0")], max_chars=100,
                        scores={"b1:1:0": 0.5})
    assert [p.chunk_id for p in evidence.passages] == ["b1:1:0"]
    conn.close()


# --- Task 10d: vector passages join; every sub-question gets its turn ----------

from lore.query.subgraph import assemble_ranked  # noqa: E402


def test_a_vector_only_chunk_joins_the_evidence(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    _seed(conn, "b1:1:0", "graph passage")
    _seed(conn, "b1:5:0", "vector passage", chapter_seq=5)
    evidence = assemble_ranked(conn, [_edge("b1", 1, "b1:1:0")],
                               extra_chunks=[("b1:5:0", "b1", 5)],
                               score_lists=[{"b1:1:0": 0.2, "b1:5:0": 0.9}])
    assert [(p.n, p.chunk_id) for p in evidence.passages] == [(1, "b1:1:0"), (2, "b1:5:0")]
    conn.close()


def test_a_chunk_from_both_sources_appears_once(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    _seed(conn, "b1:1:0", "shared passage")
    edge = _edge("b1", 1, "b1:1:0")
    evidence = assemble_ranked(conn, [edge, edge],
                               extra_chunks=[("b1:1:0", "b1", 1), ("b1:1:0", "b1", 1)],
                               score_lists=[{"b1:1:0": 0.5}, {"b1:1:0": 0.4}])
    assert [p.chunk_id for p in evidence.passages] == ["b1:1:0"]
    assert evidence.edges == (edge,)
    conn.close()


def test_every_part_gets_a_passage_before_any_part_gets_a_second(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    for ch in (1, 2, 3, 4):
        _seed(conn, f"b1:{ch}:0", "x" * 100, chapter_seq=ch)
    rows = [_edge("b1", ch, f"b1:{ch}:0") for ch in (1, 2, 3, 4)]
    part_a = {"b1:1:0": 0.9, "b1:2:0": 0.8, "b1:3:0": 0.1, "b1:4:0": 0.1}
    part_b = {"b1:1:0": 0.3, "b1:2:0": 0.0, "b1:3:0": 0.5, "b1:4:0": 0.0}
    evidence = assemble_ranked(conn, rows, score_lists=[part_a, part_b], max_chars=200)
    # b1:2:0 outscores part B's best passage, but B gets its first turn first.
    assert [p.chunk_id for p in evidence.passages] == ["b1:1:0", "b1:3:0"]
    assert evidence.truncated is True
    conn.close()


def test_a_part_whose_next_chunk_is_already_taken_moves_down_its_list(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    for ch in (1, 2, 3):
        _seed(conn, f"b1:{ch}:0", "x" * 100, chapter_seq=ch)
    rows = [_edge("b1", ch, f"b1:{ch}:0") for ch in (1, 2, 3)]
    same_top = {"b1:1:0": 0.9, "b1:2:0": 0.1, "b1:3:0": 0.5}
    other = {"b1:1:0": 0.9, "b1:2:0": 0.6, "b1:3:0": 0.1}
    evidence = assemble_ranked(conn, rows, score_lists=[same_top, other], max_chars=200)
    assert [p.chunk_id for p in evidence.passages] == ["b1:1:0", "b1:2:0"]
    conn.close()


def test_one_part_ranks_like_assemble_with_scores(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    rows = _two_books(conn, per_book=4, size=100)
    scores = {"b1:1:0": 0.1, "b1:2:0": 0.2, "b1:3:0": 0.3, "b1:4:0": 0.8,
              "b2:1:0": 0.1, "b2:2:0": 0.7, "b2:3:0": 0.2, "b2:4:0": 0.6}
    single = assemble(conn, rows, max_chars=400, scores=scores)
    # Task 10e: book-fair only when asked for (the no-decomposition fallback).
    ranked = assemble_ranked(conn, rows, score_lists=[scores], max_chars=400, book_fair=True)
    assert ranked.passages == single.passages
    assert ranked.truncated == single.truncated
    conn.close()


def test_relationship_lines_keep_only_edges_whose_chunk_is_kept(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    _seed(conn, "b1:1:0", "x" * 100)
    _seed(conn, "b1:2:0", "x" * 100, chapter_seq=2)
    kept = _edge("b1", 1, "b1:1:0", subject="Kept")
    cut = _edge("b1", 2, "b1:2:0", subject="Cut")
    evidence = assemble_ranked(conn, [cut, kept], max_chars=150,
                               score_lists=[{"b1:1:0": 0.9, "b1:2:0": 0.1}])
    assert evidence.edges == (kept,)
    conn.close()


def test_no_rows_and_no_vector_chunks_is_empty(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    evidence = assemble_ranked(conn, [], score_lists=[{}])
    assert evidence.passages == () and evidence.edges == ()
    conn.close()


# --- Task 10e: pure relevance within a part; each part's share -----------------


def test_within_a_part_a_less_relevant_other_book_chunk_no_longer_displaces_a_better_one(
        tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    rows = _two_books(conn, per_book=3, size=100)
    scores = {"b1:1:0": 0.9, "b1:2:0": 0.8, "b1:3:0": 0.7,
              "b2:1:0": 0.2, "b2:2:0": 0.1, "b2:3:0": 0.0}
    evidence = assemble_ranked(conn, rows, score_lists=[scores], max_chars=300)
    assert [p.chunk_id for p in evidence.passages] == ["b1:1:0", "b1:2:0", "b1:3:0"]
    conn.close()


def test_book_fair_ranking_is_kept_for_the_fallback_path(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    rows = _two_books(conn, per_book=3, size=100)
    scores = {"b1:1:0": 0.9, "b1:2:0": 0.8, "b1:3:0": 0.7,
              "b2:1:0": 0.2, "b2:2:0": 0.1, "b2:3:0": 0.0}
    evidence = assemble_ranked(conn, rows, score_lists=[scores], max_chars=300,
                               book_fair=True)
    assert [p.chunk_id for p in evidence.passages] == ["b1:1:0", "b1:2:0", "b2:1:0"]
    conn.close()


def test_each_part_shares_its_own_passages_and_the_ones_it_ranked_but_another_took(
        tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    for ch in (1, 2, 3, 4, 5):
        _seed(conn, f"b1:{ch}:0", "x" * 100, chapter_seq=ch)
    rows = [_edge("b1", ch, f"b1:{ch}:0") for ch in (1, 2, 3, 4, 5)]
    # A takes 1 then 2. B's best is 1 (taken by A), so B takes 3, then 4.
    part_a = {"b1:1:0": 0.9, "b1:2:0": 0.8, "b1:3:0": 0.1, "b1:4:0": 0.0, "b1:5:0": 0.3}
    part_b = {"b1:1:0": 0.9, "b1:2:0": 0.1, "b1:3:0": 0.8, "b1:4:0": 0.7, "b1:5:0": 0.0}
    evidence = assemble_ranked(conn, rows, score_lists=[part_a, part_b], max_chars=400)
    assert [p.chunk_id for p in evidence.passages] == ["b1:1:0", "b1:2:0", "b1:3:0", "b1:4:0"]
    # Passage numbers are the global ones.
    assert evidence.shares == ((1, 2), (1, 3, 4))
    conn.close()


def test_a_part_whose_turn_falls_past_the_budget_shares_nothing_it_did_not_reach(
        tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    for ch in (1, 2, 3):
        _seed(conn, f"b1:{ch}:0", "x" * 100, chapter_seq=ch)
    rows = [_edge("b1", ch, f"b1:{ch}:0") for ch in (1, 2, 3)]
    part_a = {"b1:1:0": 0.9, "b1:2:0": 0.1, "b1:3:0": 0.0}
    part_b = {"b1:1:0": 0.0, "b1:2:0": 0.9, "b1:3:0": 0.1}
    part_c = {"b1:1:0": 0.1, "b1:2:0": 0.0, "b1:3:0": 0.9}
    evidence = assemble_ranked(conn, rows, score_lists=[part_a, part_b, part_c],
                               max_chars=200)
    assert [p.chunk_id for p in evidence.passages] == ["b1:1:0", "b1:2:0"]
    assert evidence.shares == ((1,), (2,), ())
    conn.close()


def test_one_part_shares_every_kept_passage(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    rows = _two_books(conn, per_book=2, size=100)
    evidence = assemble_ranked(conn, rows, score_lists=[{}], max_chars=1000)
    assert evidence.shares == ((1, 2, 3, 4),)
    conn.close()


def test_evidence_gains_shares_without_breaking_old_constructions() -> None:
    assert Evidence(edges=(), passages=(), truncated=False).shares == ()


# --- Task 13: the same rows in, the same evidence out --------------------------

def _tied(evidence: str, chunk_id: str = "b1:1:0") -> EdgeRow:
    """Edges that tie on relevance (same chunk) and differ only in evidence."""
    return EdgeRow("A", "P", "p", "B", evidence, 0.5, "b1", 1, chunk_id)


def test_equally_relevant_edges_are_ordered_by_content_not_input_order(
        tmp_path: Path) -> None:
    # Ranking sorts edges by their passage's relevance; every edge of one
    # passage ties. A stable sort then preserves whatever order the rows
    # arrived in, so the same question could list its leads differently on
    # two runs (measured: three templates returned 20 different orders in
    # 20 runs before the templates were given a total order).
    conn = open_db(tmp_path / "lore.db")
    _seed(conn, "b1:1:0", "one passage, several leads")
    rows = [_tied("gamma"), _tied("alpha"), _tied("beta")]
    scores = {"b1:1:0": 0.9}
    first = assemble_ranked(conn, rows, score_lists=[scores], max_chars=400)
    shuffled = assemble_ranked(conn, list(reversed(rows)), score_lists=[scores],
                               max_chars=400)
    assert [e.evidence for e in first.edges] == ["alpha", "beta", "gamma"]
    assert first.edges == shuffled.edges
    conn.close()


def test_unranked_assembly_orders_tied_edges_by_content_too(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    _seed(conn, "b1:1:0", "one passage, several leads")
    rows = [_tied("gamma"), _tied("alpha"), _tied("beta")]
    evidence = assemble(conn, rows)
    assert [e.evidence for e in evidence.edges] == ["alpha", "beta", "gamma"]
    conn.close()
