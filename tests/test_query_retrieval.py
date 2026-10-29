"""Retrieval guards: no boilerplate passages, no book-title seeds, and a
book-scoped part with nothing for the graph still gets that book's passages.
No model or network: every fake is local."""
import sqlite3
from pathlib import Path

from lore.db.sqlite import open_db
from lore.extract.embed import QUERY_PREFIX
from lore.query.agent import SCOPED_K, QueryModels, answer_question
from lore.query.decompose import SubQuestions
from lore.query.keyword import KeywordIndex
from lore.query.retrieval import (
    books_named, drop_boilerplate, search_chunks, title_names,
)
from lore.query.select import TemplateChoice
from lore.query.seed import named_seeds, seed_entities
from lore.query.subgraph import assemble_ranked, passages_from_chunks
from lore.query.synthesize import Synthesis
from lore.resolve.entityhood import is_boilerplate_chunk, is_table_of_contents

LICENCE = ("This eBook is for the use of anyone anywhere in the United States "
           "and most other parts of the world at no cost. Project Gutenberg License.")
TOC_CAPS = ("A STUDY IN SCARLET. PART I. CHAPTER I. MR. SHERLOCK HOLMES. CHAPTER II. "
            "THE SCIENCE OF DEDUCTION. CHAPTER III. THE LAURISTON GARDENS MYSTERY "
            "CHAPTER IV. WHAT JOHN RANCE HAD TO TELL.")
TOC_MIXED = ("Chapter I. The Science of Deduction Chapter II. The Statement of the "
             "Case Chapter III. In Quest of a Solution Chapter IV. The Story")
STORY = ("In the second chapter Holmes explains his methods, and Watson, who has "
         "read chapter one twice, is astonished at them.")


# --- A: the detector --------------------------------------------------------

def test_tables_of_contents_are_boilerplate_and_story_is_not() -> None:
    assert is_table_of_contents(TOC_CAPS) and is_table_of_contents(TOC_MIXED)
    assert is_table_of_contents("PREFACE. List of Illustrations. Chapter: I., II.")
    assert not is_table_of_contents(STORY)
    assert is_boilerplate_chunk(LICENCE) and is_boilerplate_chunk(TOC_CAPS)
    assert not is_boilerplate_chunk(STORY)


def _row(cid, text, book="b1", chapter=1, distance=0.1, entities=()):
    return {"chunk_id": cid, "book_id": book, "chapter_seq": chapter, "text": text,
            "_distance": distance, "entity_ids": list(entities)}


class Lance:
    """Nearest-first rows; search_chunks_in_book prefilters like LanceStore."""

    def __init__(self, rows, vectors=None):
        self.rows, self.calls, self.vectors = rows, [], vectors or {}

    def search_chunks(self, vector, limit=5):
        self.calls.append(("all", limit))
        return self.rows[:limit]

    def search_chunks_in_book(self, vector, book_id, limit=5):
        self.calls.append((book_id, limit))
        return [r for r in self.rows if r["book_id"] == book_id][:limit]

    def chunk_vectors(self, ids):
        return {c: self.vectors.get(c, [1.0, 0.0]) for c in ids}


def test_search_skips_boilerplate_and_still_returns_limit_story_chunks() -> None:
    rows = [_row("lic", LICENCE), _row("toc", TOC_CAPS)] + [
        _row(f"s{i}", f"story passage {i}") for i in range(6)]
    got = search_chunks(Lance(rows), [1.0], 5)
    assert [r["chunk_id"] for r in got] == [f"s{i}" for i in range(5)]


def test_search_widens_past_a_wall_of_boilerplate() -> None:
    rows = [_row(f"l{i}", LICENCE) for i in range(60)] + [_row("s0", "story")]
    assert [r["chunk_id"] for r in search_chunks(Lance(rows), [1.0], 1)] == ["s0"]


def test_search_in_one_book_stays_in_the_book() -> None:
    rows = [_row("a", "x", book="b1"), _row("b", "y", book="b2")]
    assert [r["chunk_id"] for r in search_chunks(Lance(rows), [1.0], 5, book_id="b2")] == ["b"]


def test_passages_from_chunks_drops_boilerplate() -> None:
    got = passages_from_chunks([_row("lic", LICENCE), _row("s", "story")])
    assert [p.chunk_id for p in got] == ["s"]
    assert [r["chunk_id"] for r in drop_boilerplate([_row("t", TOC_MIXED)])] == []


def _conn(tmp_path: Path, chunks, titles=(("b1", "A Study in Scarlet"),)):
    conn = open_db(tmp_path / "lore.db")
    for book_id, title in titles:
        conn.execute("INSERT INTO books (id, title, author, source, file_path, status)"
                     " VALUES (?, ?, NULL, 'upload', '/x', 'chunked')", (book_id, title))
    for i, (cid, book, chapter, text) in enumerate(chunks):
        conn.execute("INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text)"
                     " VALUES (?, ?, ?, 0, ?, ?)", (cid, book, chapter, i, text))
    conn.commit()
    return conn


def test_keyword_search_never_finds_boilerplate(tmp_path: Path) -> None:
    conn = _conn(tmp_path, [("lic", "b1", 1, LICENCE + " Holmes"),
                            ("toc", "b1", 2, TOC_CAPS + " Holmes"),
                            ("s", "b1", 3, "Holmes smiled")])
    index = KeywordIndex(conn)
    assert [h[0] for h in index.search("Holmes", 10)] == ["s"]
    conn.close()


def test_keyword_search_can_be_scoped_to_a_book(tmp_path: Path) -> None:
    conn = _conn(tmp_path, [("a", "b1", 1, "villain"), ("b", "b2", 1, "villain")],
                 titles=(("b1", "One"), ("b2", "Two")))
    index = KeywordIndex(conn)
    assert [h[0] for h in index.search("villain", 10, book_id="b2")] == ["b"]
    conn.close()


def test_graph_evidence_drops_boilerplate_chunks(tmp_path: Path) -> None:
    conn = _conn(tmp_path, [("lic", "b1", 1, LICENCE), ("toc", "b1", 1, TOC_CAPS),
                            ("s", "b1", 2, "Holmes smiled")])
    evidence = assemble_ranked(
        conn, [], extra_chunks=[("lic", "b1", 1), ("toc", "b1", 1), ("s", "b1", 2)],
        score_lists=[{"lic": 0.9, "toc": 0.8, "s": 0.1}])
    assert [p.chunk_id for p in evidence.passages] == ["s"]
    conn.close()


# --- B: book-title entities are not seeds -----------------------------------

class Kuzu:
    def __init__(self, entities):
        self.entities = entities  # id -> (name, kind)

    def execute(self, cypher, params=None):
        ids = (params or {}).get("ids", [])
        if "ALIAS_OF" in cypher:
            return []
        if "count(r)" in cypher:
            return [[i, 2] for i in ids if i in self.entities]
        if "RETURN e.id, e.name" in cypher:
            return [[i, *self.entities[i], ["b2"]] for i in ids if i in self.entities]
        return []


ENTITIES = {"t": ("A Study in Scarlet", "Object"), "j": ("Jonathan Small", "Character")}
EXCLUDE = title_names(["A Study in Scarlet", "The Sign of the Four"])


async def _embed(texts):
    return [[1.0, 0.0] for _ in texts]


async def test_vector_seeds_skip_a_title_entity() -> None:
    lance = Lance([_row("c1", "x", entities=["t", "j"])])
    seeds = await seed_entities("q", lance, Kuzu(ENTITIES), _embed,
                                vector=[1.0, 0.0], exclude_names=EXCLUDE)
    assert [s.name for s in seeds] == ["Jonathan Small"]


async def test_named_seeds_skip_a_title_entity() -> None:
    seeds = await named_seeds(Kuzu(ENTITIES), ["t", "j"], exclude_names=EXCLUDE)
    assert [s.name for s in seeds] == ["Jonathan Small"]


async def test_without_exclusions_a_title_entity_is_still_a_seed() -> None:
    seeds = await named_seeds(Kuzu(ENTITIES), ["t", "j"])
    assert [s.name for s in seeds] == ["A Study in Scarlet", "Jonathan Small"]


def test_books_named_matches_titles_as_phrases() -> None:
    titles = {"b1": "A Study in Scarlet", "b2": "The Sign of the Four"}
    assert books_named("villains in A Study in Scarlet?", titles) == ["b1"]
    assert books_named("in the sign of the four and study in scarlet", titles) == ["b1", "b2"]
    assert books_named("who is Holmes?", titles) == []


# --- C: an abstract book-scoped part draws its candidates from its book -----

Q, P1 = [0.6, 0.6], [1.0, 0.0]


class TitleOnlyKuzu(Kuzu):
    """Every vector seed is the title entity; its one edge is in the OTHER book."""

    def execute(self, cypher, params=None):
        if "e.aliases" in cypher:
            return []
        if "b:Book" in cypher:
            return [["A Study in Scarlet"]]
        return super().execute(cypher, params)


def _models(sub_questions, select_calls):
    async def embed(texts):
        return [P1 if t != QUERY_PREFIX + "q" else Q for t in texts]

    async def decompose(prompt, schema):
        return SubQuestions(sub_questions=sub_questions)

    async def select(prompt, schema):
        select_calls.append(prompt)
        return TemplateChoice(template="neighbours", entities=[1])

    async def synthesize(prompt, schema):
        return Synthesis(claims=[], can_answer=False)

    async def aclose():
        return None

    return QueryModels(embed, select, synthesize, lambda: "w", aclose, lambda: "c",
                       decompose=decompose, evidence_chars=100_000)


async def test_a_part_with_only_a_title_seed_gets_scoped_book_candidates(
        tmp_path: Path) -> None:
    story = [("b1:5:%d" % i, "b1", 5, f"villain passage {i} " + "w" * 50) for i in range(20)]
    other = [("b2:5:0", "b2", 5, "villain of the other book")]
    conn = _conn(tmp_path, story + other + [("b1:1:0", "b1", 1, LICENCE)],
                 titles=(("b1", "A Study in Scarlet"), ("b2", "The Sign of the Four")))
    rows = [_row("b1:1:0", LICENCE, entities=["t"], distance=0.1)] + [
        _row(cid, text, book=b, chapter=ch, distance=0.2 + i / 100, entities=["t"])
        for i, (cid, b, ch, text) in enumerate(story + other) for _ in [0]]
    lance = Lance(rows)
    selects: list[str] = []
    models = _models(["What drives the villains in A Study in Scarlet?"], selects)
    answer = await answer_question(conn, TitleOnlyKuzu({"t": ENTITIES["t"]}), lance,
                                   "q", models)
    ids = [p.chunk_id for p in answer.passages]
    # The title entity was never offered to the selector.
    assert selects == []
    assert "b1:1:0" not in ids and "b2:5:0" not in ids
    assert len(ids) == SCOPED_K
    assert all(i.startswith("b1:5:") for i in ids)
    assert ("b1", SCOPED_K + 24) in lance.calls or any(c[0] == "b1" for c in lance.calls)
    conn.close()


async def test_a_part_with_strong_evidence_is_not_widened(tmp_path: Path) -> None:
    story = [("b1:5:%d" % i, "b1", 5, f"passage {i}") for i in range(30)]
    conn = _conn(tmp_path, story, titles=(("b1", "A Study in Scarlet"),))
    rows = [_row(cid, text, chapter=5, distance=0.2 + i / 100, entities=["j"])
            for i, (cid, _, _, text) in enumerate(story)]
    lance = Lance(rows)

    class K(TitleOnlyKuzu):
        def execute(self, cypher, params=None):
            if "ALIAS_OF" not in cypher and "count" not in cypher \
                    and "e.id, e.name" not in cypher and "e.aliases" not in cypher \
                    and "b:Book" not in cypher:
                return [["J", "X", "d", "O", "e", 0.9, "b1", 5, f"b1:5:{i}", "j", "o"]
                        for i in range(6)]
            return super().execute(cypher, params)

    models = _models(["villains in A Study in Scarlet?"], [])
    answer = await answer_question(conn, K({"j": ENTITIES["j"]}), lance, "q", models)
    assert not any(c[0] == "b1" for c in lance.calls)
    assert len(answer.passages) <= 6 + 5  # graph chunks plus CANDIDATE_K nearest
    conn.close()


async def test_a_scoped_part_ranks_its_own_book_before_the_other(tmp_path: Path) -> None:
    mine = [("b1:5:%d" % i, "b1", 5, f"mine {i}") for i in range(4)]
    theirs = [("b2:5:%d" % i, "b2", 5, f"theirs {i}") for i in range(4)]
    conn = _conn(tmp_path, mine + theirs,
                 titles=(("b1", "A Study in Scarlet"), ("b2", "The Sign of the Four")))
    # The other book's chunks are NEARER to the part (cosine 1.0 against 0.5).
    rows = [_row(cid, t, book=b, chapter=5) for cid, b, _, t in theirs + mine]
    vectors = {cid: ([1.0, 0.0] if b == "b2" else [0.5, 0.0]) for cid, b, _, _ in theirs + mine}
    models = _models(["villains in A Study in Scarlet?"], [])
    models = QueryModels(models.embed, models.select, models.synthesize, models.synthesis_model,
                         models.aclose, models.selection_model, decompose=models.decompose,
                         evidence_chars=24)  # four 6-character passages fit
    answer = await answer_question(conn, TitleOnlyKuzu({"t": ENTITIES["t"]}),
                                   Lance(rows, vectors), "q", models)
    assert [p.chunk_id for p in answer.passages] == [c for c, *_ in mine]
    conn.close()


def test_search_chunks_within_a_set_of_books_with_and_without_a_prefilter() -> None:
    from lore.query.retrieval import search_chunks

    rows = [{"chunk_id": f"{b}:{n}", "book_id": b, "text": "story"}
            for n in range(3) for b in "abc"]

    class Plain:  # no prefilter: the global ranking is filtered
        def search_chunks(self, vector, limit=5):
            return rows[:limit]

    class Filtering(Plain):
        def search_chunks_in_books(self, vector, book_ids, limit=5):
            return [r for r in rows if r["book_id"] in book_ids][:limit]

    for lance in (Plain(), Filtering()):
        got = search_chunks(lance, [0.0], 3, book_ids={"b", "c"})
        assert {r["book_id"] for r in got} == {"b", "c"} and len(got) == 3
        assert search_chunks(lance, [0.0], 3, book_ids=set()) == []
