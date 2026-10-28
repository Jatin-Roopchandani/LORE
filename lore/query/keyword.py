"""Keyword (BM25) search over passage text, to sit beside vector search.

Vector search ranks by meaning and misses passages whose words match the
question but whose overall topic does not -- one line about Carlyle in a
passage about Holmes's ignorance of astronomy. BM25 ranks by the
question's own rare words and finds those. On the dev set's 96 key facts
(evals/answer_quality), fusing the two put the gold passage in the top 10
for 63% of facts against vector search's 52%, and in the top 20 for 77%
against 68%.

The index is SQLite FTS5, built in memory from the catalog's chunks on
each search session: ~1,100 chunks and 1.5 MB on the current library,
built in well under a second. A library much larger than that wants a
persisted FTS table maintained at ingest instead.
"""
from __future__ import annotations

import re
import sqlite3

from lore.resolve.entityhood import is_boilerplate_chunk

# Words too common to say anything about a passage; FTS5 itself has no
# stopword list, and with OR-ed terms a single "the" matches everything.
_STOP = frozenset("""
a an the and or but of to in on at by for from with as is are was were be been
has have had do does did this that these those it its he him his she her they
them their what who whom whose which when where why how each every one both into
than then there not no so novel novels book books story stories chapter
""".split())
_WORD = re.compile(r"[a-z]+")

# Reciprocal-rank fusion's damping constant: the standard 60 (Cormack et
# al., 2009), not tuned here. It keeps one list's top rank from swamping
# the other's.
RRF_K = 60


def query_terms(text: str) -> list[str]:
    """The question's content words, lowercased, first occurrence order."""
    return list(dict.fromkeys(
        w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 2))


class KeywordIndex:
    """BM25 over every chunk in the catalog, read once at construction."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._db = sqlite3.connect(":memory:")
        self._db.execute(
            "CREATE VIRTUAL TABLE c USING fts5(id UNINDEXED, book_id UNINDEXED,"
            " chapter_seq UNINDEXED, text)")
        self._db.executemany(
            "INSERT INTO c VALUES (?, ?, ?, ?)",
            (row for row in conn.execute(
                "SELECT id, book_id, chapter_seq, text FROM chunks")
             if not is_boilerplate_chunk(row[3])))

    def search(self, text: str, limit: int,
               book_id: str | None = None) -> list[tuple[str, str, int]]:
        """(chunk_id, book_id, chapter_seq) of the best BM25 matches for any
        of `text`'s content words, best first. No content words, no hits."""
        terms = query_terms(text)
        if not terms or limit <= 0:
            return []
        # Each term quoted, so a word FTS5 treats as syntax (AND, NEAR) is
        # searched as a word.
        match = " OR ".join(f'"{t}"' for t in terms)
        where, args = (" AND book_id = ?", [book_id]) if book_id else ("", [])
        rows = self._db.execute(
            "SELECT id, book_id, chapter_seq FROM c WHERE c MATCH ?" + where
            + " ORDER BY bm25(c) LIMIT ?", (match, *args, limit))
        return [(str(i), str(b), int(s)) for i, b, s in rows]

    def close(self) -> None:
        self._db.close()


def fused(cosines: dict[str, float], keyword_ranked: list[str]) -> dict[str, float]:
    """chunk_id -> reciprocal-rank-fusion score of vector and keyword ranks.

    Every chunk in `cosines` (the candidates, with their cosine to the
    question) is ranked by cosine; a chunk the keyword search also found
    adds its keyword rank's share. Higher is better, as with a cosine, so
    the result drops in wherever a part's relevance map is expected.
    """
    by_vector = sorted(cosines, key=lambda c: (-cosines[c], c))
    keyword_rank = {c: i + 1 for i, c in enumerate(keyword_ranked)}
    return {
        c: 1 / (RRF_K + i + 1)
        + (1 / (RRF_K + keyword_rank[c]) if c in keyword_rank else 0.0)
        for i, c in enumerate(by_vector)
    }
