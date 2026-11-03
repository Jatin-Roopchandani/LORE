"""Keyword (BM25) search beside vector search, and the rank fusion."""
from __future__ import annotations

from pathlib import Path

from lore.db.sqlite import open_db
from lore.query.keyword import RRF_K, KeywordIndex, fused, query_terms


def _library(tmp_path: Path, texts: dict[str, str]):
    conn = open_db(tmp_path / "lore.db")
    conn.execute("INSERT INTO books (id, title, author, source, file_path, status)"
                 " VALUES ('b1', 'T', NULL, 'upload', '/x', 'chunked')")
    for seq, (chunk_id, text) in enumerate(texts.items()):
        conn.execute("INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text)"
                     " VALUES (?, 'b1', ?, 0, ?, ?)", (chunk_id, seq + 1, seq, text))
    conn.commit()
    return conn


def test_query_terms_keep_content_words_once_in_order() -> None:
    assert query_terms("Who was Carlyle, and did Holmes know who Carlyle was?") == [
        "carlyle", "holmes", "know"]


def test_a_rare_word_finds_its_passage(tmp_path: Path) -> None:
    conn = _library(tmp_path, {
        "b1:1:0": "Holmes knew nothing of the Copernican theory or of the solar system.",
        "b1:2:0": "Upon my quoting Thomas Carlyle, he inquired who he might be.",
        "b1:3:0": "Holmes and Watson took rooms together in Baker Street.",
    })
    index = KeywordIndex(conn)
    hits = index.search("Did Holmes know who Carlyle was?", limit=5)
    assert hits[0] == ("b1:2:0", "b1", 2)
    assert {h[0] for h in hits} == {"b1:1:0", "b1:2:0", "b1:3:0"}
    assert index.search("Did Holmes know who Carlyle was?", limit=1) == [("b1:2:0", "b1", 2)]
    index.close()
    conn.close()


def test_fts_syntax_words_are_searched_as_words(tmp_path: Path) -> None:
    conn = _library(tmp_path, {"b1:1:0": "The near window and the door."})
    index = KeywordIndex(conn)
    # Unquoted, NEAR and AND are FTS5 operators and this would raise.
    assert index.search("NEAR AND window", limit=5) == [("b1:1:0", "b1", 1)]
    index.close()
    conn.close()


def test_a_question_of_only_common_words_finds_nothing(tmp_path: Path) -> None:
    conn = _library(tmp_path, {"b1:1:0": "What was it, and who was he?"})
    index = KeywordIndex(conn)
    assert index.search("What was it?", limit=5) == []
    assert index.search("Carlyle", limit=0) == []
    index.close()
    conn.close()


def test_a_keyword_match_lifts_a_passage_vector_search_ranked_lower() -> None:
    cosines = {"near": 0.80, "mid": 0.75, "keyword": 0.70}
    scores = fused(cosines, ["keyword"])
    assert max(scores, key=scores.get) == "keyword"
    assert scores["near"] == 1 / (RRF_K + 1)
    assert scores["keyword"] == 1 / (RRF_K + 3) + 1 / (RRF_K + 1)


def test_without_keyword_matches_fusion_keeps_the_vector_order() -> None:
    cosines = {"a": 0.9, "b": 0.5, "c": 0.7}
    scores = fused(cosines, [])
    assert sorted(cosines, key=scores.get, reverse=True) == ["a", "c", "b"]


def test_a_keyword_hit_outside_the_candidates_scores_nothing() -> None:
    # Only candidates (chunks with a stored vector) are scored; the agent
    # makes every keyword hit a candidate before fusing.
    assert set(fused({"a": 0.9}, ["elsewhere", "a"])) == {"a"}


def test_search_within_a_set_of_books() -> None:
    import sqlite3
    from lore.query.keyword import KeywordIndex
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE chunks (id, book_id, chapter_seq, text)")
    for chunk, book in (("a1", "a"), ("b1", "b"), ("c1", "c")):
        conn.execute("INSERT INTO chunks VALUES (?, ?, 1, 'the lighthouse keeper sailed')",
                     (chunk, book))
    index = KeywordIndex(conn)
    assert {h[0] for h in index.search("lighthouse keeper", 10, book_ids={"a", "c"})} == {"a1", "c1"}
    assert index.search("lighthouse keeper", 10, book_ids=set()) == []
    assert len(index.search("lighthouse keeper", 10)) == 3
