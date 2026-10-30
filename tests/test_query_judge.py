"""Candidates for a lens about a set of people that names none (judge.py).

Exclusions ("except X"), graph candidates, their beats and the beat-variety
ranking are kept from the JUDGEMENTS work; detecting a judgement question by
its wording is gone (decompose.py classifies each part, interpret.py answers
it). The prompts and schemas that mode used to change are what they were
before it existed (tests/golden/judgements_off.json, dumped from 23602b6).

Every fixture uses invented books and names. No test reaches a model.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lore.query import judge, timeline as tl, write as w
from lore.query.synthesize import Faithfulness, _build_prompt, faithfulness_prompt
from lore.query.models import Evidence
from lore.query.write import Sentence, writer_prompt
from tests.test_query_story import BOOKS, CS, PS, TITLES

GOLDEN = json.loads((Path(__file__).parent / "golden" / "judgements_off.json").read_text())
Q = "Who is the most compelling figure in the Second Volume, except Rin?"
PARTS = ["Who is the most compelling figure in the First Volume, except Rin?",
         "Who is the most compelling figure in the Second Volume, except Rin?"]
EV = Evidence(edges=(), passages=PS, truncated=False)


# ---- exclusions -----------------------------------------------------------------

@pytest.mark.parametrize("question, expected", [
    ("Who is the most complex character, except Zed Quill and Yan Oro?", ["Zed Quill", "Yan Oro"]),
    ("Who is the most complex, other than Zed, Yan, or Wex?", ["Zed", "Yan", "Wex"]),
    ("Who is the best sailor, excluding the Captain?", ["Captain"]),
    ("Who is the best sailor apart from Zed in both books?", ["Zed"]),
    ("Who is the best sailor, except for Zed.", ["Zed"]),
    ("Who is the best sailor besides Zed & Yan?", ["Zed", "Yan"]),
    ("Who is the best sailor?", []),
])
def test_exclusions_are_read_from_the_question(question, expected):
    assert judge.exclusions(question) == expected


def test_exclusion_matches_names_and_aliases_by_tokens():
    assert judge.is_excluded("Zed Quill", ["Quill"], ["Zed"])
    assert judge.is_excluded("The Wanderer", ["Zed Quill"], ["Zed Quill"])
    assert judge.is_excluded("Mr. Quill", (), ["Quill"])
    assert not judge.is_excluded("Yan Oro", ["Oro"], ["Zed"])
    assert not judge.is_excluded("Zedekiah", (), ["Zed"])
    assert not judge.is_excluded("Yan Oro", (), [])


# ---- candidates: real Kuzu --------------------------------------------------------

def _graph(tmp_path):
    from lore.db.kuzu_store import KuzuStore
    k = KuzuStore(tmp_path / "kuzu")
    people = [("ez", "Zed Quill", ["Zed"]), ("ey", "Yan Oro", []), ("ew", "Wex Lin", []),
              ("ev", "Vo Pell", []), ("en", "Nim Dar", [])]
    for i, name, aliases in people:
        k.execute("CREATE (:Entity {id: $i, name: $n, kind: 'Character', aliases: $a, books: ['ba']})",
                  {"i": i, "n": name, "a": aliases})
    k.execute("CREATE (:Entity {id: 'et', name: 'Alpha Tale', kind: 'Object', aliases: [], books: ['ba']})")
    k.execute("CREATE (:Entity {id: 'ep', name: 'The Harbour', kind: 'Place', aliases: [], books: ['ba']})")
    chunks = [(f"{b}:{ch}:0", b, ch) for b, n in (("ba", 6), ("bb", 3)) for ch in range(1, n + 1)]
    for cid, b, ch in chunks:
        k.execute("CREATE (:Chunk {id: $c, book_id: $b, chapter_seq: $s})", {"c": cid, "b": b, "s": ch})
    mentions = {"ez": [("ba", c) for c in range(1, 7)] + [("bb", 1), ("bb", 2), ("bb", 3)],
                "ey": [("ba", c) for c in (1, 2, 3, 4, 5)],
                "ew": [("ba", c) for c in (1, 2, 3)] + [("bb", 1), ("bb", 2), ("bb", 3)],
                "ev": [("ba", 1), ("ba", 2)],
                "en": [("ba", 6)],
                "et": [("ba", c) for c in range(1, 7)],
                "ep": [("ba", c) for c in range(1, 7)]}
    for e, where in mentions.items():
        for b, ch in where:
            k.execute("MATCH (c:Chunk {id: $c}), (e:Entity {id: $e}) CREATE (c)-[:MENTIONS]->(e)",
                      {"c": f"{b}:{ch}:0", "e": e})
    for a, b in (("ez", "ey"), ("ey", "ew"), ("ew", "ev"), ("ev", "en"), ("en", "et"), ("ep", "et")):
        k.execute("MATCH (a:Entity {id: $a}), (b:Entity {id: $b}) CREATE (a)-[:REL {predicate: 'X',"
                  " predicate_raw: 'met', evidence: 'ev', confidence: 0.9, chunk_id: 'ba:1:0',"
                  " book_id: 'ba', chapter_seq: 1, run_id: 'r'}]->(b)", {"a": a, "b": b})
    return k


async def test_candidates_are_the_characters_that_span_the_most_chapters(tmp_path):
    k = _graph(tmp_path)
    got = await judge.candidate_seeds(k, ["ba"], [], frozenset(), limit=3)
    # Characters only (no book-title object, no place); Zed 6 chapters, Yan 5, Wex 3, Vo 2, Nim 1.
    assert [s.id for s in got] == ["ez", "ey", "ew"]
    assert all(s.kind == "Character" for s in got)
    k.close()


async def test_candidates_leave_out_whom_the_question_excludes(tmp_path):
    k = _graph(tmp_path)
    got = await judge.candidate_seeds(k, ["ba"], ["Zed", "Yan Oro"], frozenset(), limit=3)
    assert [s.id for s in got] == ["ew", "ev", "en"]
    k.close()


async def test_candidates_count_chapters_across_the_named_books_only(tmp_path):
    k = _graph(tmp_path)
    both = await judge.candidate_seeds(k, ["ba", "bb"], ["Zed"], frozenset(), limit=2)
    only_b = await judge.candidate_seeds(k, ["bb"], [], frozenset(), limit=5)
    assert [s.id for s in both] == ["ew", "ey"]  # Wex: 3 + 3 chapters, Yan: 5
    assert {s.id for s in only_b} == {"ez", "ew"}  # nobody else is mentioned in book bb
    # With no book named, the whole library counts.
    anywhere = await judge.candidate_seeds(k, [], [], frozenset(), limit=1)
    assert [s.id for s in anywhere] == ["ez"]
    k.close()


async def test_candidates_skip_a_node_named_like_a_book_title(tmp_path):
    k = _graph(tmp_path)
    k.execute("CREATE (:Entity {id: 'eb', name: 'Alpha Tale', kind: 'Character', aliases: [], books: ['ba']})")
    for ch in range(1, 7):
        k.execute("MATCH (c:Chunk {id: $c}), (e:Entity {id: 'eb'}) CREATE (c)-[:MENTIONS]->(e)", {"c": f"ba:{ch}:0"})
    k.execute("MATCH (a:Entity {id: 'eb'}), (b:Entity {id: 'ey'}) CREATE (a)-[:REL {predicate: 'X',"
              " predicate_raw: 'met', evidence: 'ev', confidence: 0.9, chunk_id: 'ba:1:0',"
              " book_id: 'ba', chapter_seq: 1, run_id: 'r'}]->(b)")
    from lore.query.retrieval import title_names
    got = await judge.candidate_seeds(k, ["ba"], [], title_names(["Alpha Tale"]), limit=5)
    assert "eb" not in [s.id for s in got]
    k.close()


async def test_a_failing_graph_gives_no_candidates_not_an_error():
    class Broken:
        def execute(self, *a, **k):
            raise RuntimeError("boom")
    assert await judge.candidate_seeds(Broken(), ["ba"], [], frozenset(), limit=3) == []


# ---- the seeds of a judgement part -----------------------------------------------

def _seed(i, name=None, kind="Character", score=0.5):
    from lore.query.models import SeedEntity
    return SeedEntity(id=i, name=name or i.upper(), kind=kind, books=("ba",), score=score)


async def test_judgement_seeds_drop_the_excluded_and_add_candidates_after_the_named(tmp_path):
    from lore.query.seed import load_name_index
    k = _graph(tmp_path)
    names = load_name_index(k)
    named = [_seed("ez", "Zed Quill", score=0.0)]
    seeds = [*named, _seed("en", "Nim Dar"), _seed("ep", "The Harbour", kind="Place")]
    out = await judge.judge_seeds(k, names, "Who is the most complex in Alpha Tale, except Zed?", "q",
                                  seeds, named, ["ba"], frozenset(), limit=2)
    assert "ez" not in [s.id for s in out.seeds]
    assert [s.id for s in out.candidates] == ["ey", "ew"]
    assert [s.id for s in out.seeds][:2] == ["ey", "ew"]  # no named character is left: candidates first
    assert "en" in [s.id for s in out.seeds] and "ep" in [s.id for s in out.seeds]
    assert out.named == []
    k.close()


async def test_enough_named_characters_means_no_candidates(tmp_path):
    from lore.query.seed import load_name_index
    k = _graph(tmp_path)
    named = [_seed("ey", "Yan Oro", score=0.0), _seed("ew", "Wex Lin", score=0.0)]
    out = await judge.judge_seeds(k, load_name_index(k), "Who is the more complex, Yan Oro or Wex Lin?", "q",
                                  list(named), named, ["ba"], frozenset(), limit=3)
    assert out.candidates == [] and [s.id for s in out.seeds] == ["ey", "ew"]
    k.close()


async def test_exclusions_from_the_whole_question_apply_to_a_rewritten_part(tmp_path):
    from lore.query.seed import load_name_index
    k = _graph(tmp_path)
    # The decomposer dropped "except Zed" from the part, but the question had it.
    out = await judge.judge_seeds(k, load_name_index(k), "Who is the most complex in Alpha Tale?",
                                  "Who is the most complex in Alpha Tale, except Zed?", [], [], ["ba"],
                                  frozenset(), limit=1)
    assert [s.id for s in out.candidates] == ["ey"]
    k.close()


# ---- candidate timelines ---------------------------------------------------------

def _beat_conn(tmp_path):
    from lore.db.sqlite import open_db
    from lore.beats.store import Beat, beat_id, save_beats
    conn = open_db(tmp_path / "lore.db")
    conn.execute("INSERT INTO books (id, title, file_path) VALUES ('ba', 'Alpha Tale', 'x')")
    conn.execute("INSERT INTO books (id, title, file_path) VALUES ('bb', 'Beta Tale', 'x')")
    for book in ("ba", "bb"):
        for ch in range(1, 11):
            for who in ("Yan", "Wex"):
                quote = f"quote {book} {ch} {who}"
                save_beats(conn, book, ch, 0 if who == "Yan" else 1, [Beat(
                    id=beat_id(book, ch, (who,), quote), book_id=book, chapter_seq=ch,
                    chunk_id=f"{book}:{ch}:0", entities=(who,), kind="action",
                    text=f"{who} acts in {ch}.", quote=quote)])
    conn.execute("UPDATE beat_entities SET entity_id='ey' WHERE name='Yan'")
    conn.execute("UPDATE beat_entities SET entity_id='ew' WHERE name='Wex'")
    conn.commit()
    return conn


def test_each_candidate_brings_a_few_beats_spread_over_the_story(tmp_path):
    conn = _beat_conn(tmp_path)
    got = tl.candidate_timeline(conn, [_seed("ey"), _seed("ew")], BOOKS_AB, ["ba"])
    by = {}
    for b in got:
        by.setdefault(b.entities[0], []).append(b.chapter_seq)
    assert set(by) == {"Yan", "Wex"}
    for chapters in by.values():
        assert len(chapters) == tl.BEATS_PER_CANDIDATE
        assert min(chapters) == 1 and max(chapters) == 10  # earliest and latest, with one between
    assert {b.book_id for b in got} == {"ba"}
    conn.close()


def test_many_candidates_share_a_total_beat_cap(tmp_path):
    conn = _beat_conn(tmp_path)
    seeds = [_seed(f"x{i}") for i in range(6)] + [_seed("ey"), _seed("ew")]
    got = tl.candidate_timeline(conn, seeds, BOOKS_AB, None)
    assert len(got) <= tl.MAX_CANDIDATE_BEATS
    assert {b.entities[0] for b in got} == {"Yan", "Wex"}  # candidates with no beats add nothing
    conn.close()


def test_candidate_timeline_without_the_beats_tables_is_empty(tmp_path):
    conn = _beat_conn(tmp_path)
    conn.execute("DROP TABLE beat_entities")
    conn.execute("DROP TABLE beats")
    conn.commit()
    assert tl.candidate_timeline(conn, [_seed("ey")], BOOKS_AB, None) == []
    assert tl.candidate_timeline(conn, [], BOOKS_AB, None) == []
    conn.close()


from lore.query.models import BookInfo  # noqa: E402

BOOKS_AB = {"ba": BookInfo(title="Alpha Tale", series="Cycle", series_index=1),
            "bb": BookInfo(title="Beta Tale", series="Cycle", series_index=2)}



# ---- the seeds of a lens ----------------------------------------------------------

def test_the_regex_detection_and_the_switch_are_gone():
    assert not hasattr(judge, "is_evaluative") and not hasattr(judge, "JUDGEMENTS")
    assert not hasattr(w, "judgement_asked") and not hasattr(w, "MAX_JUDGEMENTS")


def test_a_lens_wants_candidates_when_it_names_nobody_and_is_about_people():
    people = [_seed("a"), _seed("b"), _seed("c", kind="Place")]
    assert judge.wants_candidates(people, [])
    assert not judge.wants_candidates(people, [_seed("a", score=0.0)])
    assert not judge.wants_candidates([_seed("p", kind="Place"), _seed("q", kind="Object"),
                                       _seed("a")], [])
    assert not judge.wants_candidates([], [])


async def test_candidates_are_ranked_by_beat_variety_when_beats_exist(tmp_path):
    from lore.beats.store import Beat, beat_id, save_beats
    from lore.db.sqlite import open_db
    k = _graph(tmp_path)
    conn = open_db(tmp_path / "lore.db")
    conn.execute("INSERT INTO books (id, title, file_path) VALUES ('ba', 'Alpha Tale', 'x')")
    # Wex has the arc: every kind, many chapters. Yan, who spans more chapters in
    # the text, only acts, in one chapter.
    kinds = ("action", "decision", "feeling", "treatment", "change")
    for i, kind in enumerate(kinds):
        q = f"wex {i}"
        save_beats(conn, "ba", i + 1, 0, [Beat(id=beat_id("ba", i + 1, ("Wex",), q), book_id="ba",
                                              chapter_seq=i + 1, chunk_id=f"ba:{i + 1}:0",
                                              entities=("Wex",), kind=kind, text="t", quote=q)])
    for i in range(3):
        q = f"yan {i}"
        save_beats(conn, "ba", 1, 10 + i, [Beat(id=beat_id("ba", 1, ("Yan",), q), book_id="ba",
                                               chapter_seq=1, chunk_id="ba:1:0",
                                               entities=("Yan",), kind="action", text="t", quote=q)])
    conn.execute("UPDATE beat_entities SET entity_id='ey' WHERE name='Yan'")
    conn.execute("UPDATE beat_entities SET entity_id='ew' WHERE name='Wex'")
    conn.commit()
    plain = await judge.candidate_seeds(k, ["ba"], ["Zed"], frozenset(), limit=2)
    ranked = await judge.candidate_seeds(k, ["ba"], ["Zed"], frozenset(), limit=2, conn=conn)
    assert [s.id for s in plain] == ["ey", "ew"]
    assert [s.id for s in ranked] == ["ew", "ey"]
    assert judge.beat_variety(conn, "ew") == (5, 5) and judge.beat_variety(conn, "ey") == (1, 1)
    conn.close()
    k.close()


def test_without_beats_the_given_order_stands(tmp_path):
    conn = _beat_conn(tmp_path)
    conn.execute("DROP TABLE beat_entities")
    conn.execute("DROP TABLE beats")
    conn.commit()
    seeds = [_seed("ey"), _seed("ew")]
    assert judge.rank_by_variety(conn, seeds) == seeds
    assert judge.rank_by_variety(None, seeds) == seeds
    conn.close()


# ---- what the removed switch used to change is what it was before ----------------

def test_the_prompts_and_schemas_are_the_pre_judgement_ones():
    sents = [Sentence(text="Rin met a teacher.", claims=[1, 2]),
             Sentence(text="This suggests commitment.", claims=[2, 3], kind="reading")]
    assert _build_prompt(Q, EV, TITLES, books=BOOKS) == GOLDEN["claims_whole"]
    assert _build_prompt(PARTS[0], EV, TITLES, part_of=Q, books=BOOKS) == GOLDEN["claims_part"]
    assert writer_prompt(Q, PARTS, CS, PS, TITLES, books=BOOKS) == GOLDEN["writer"]
    assert writer_prompt(Q, [], CS, PS, TITLES, books=BOOKS) == GOLDEN["writer_one_part"]
    assert faithfulness_prompt(CS, PS, TITLES, books=BOOKS) == GOLDEN["check_plain"]
    assert faithfulness_prompt(CS, PS, TITLES, sents, books=BOOKS, gaps=["x"]) == GOLDEN["check_sentences"]
    assert json.dumps(w.Prose.model_json_schema()) == GOLDEN["schema_prose"]
    assert json.dumps(Faithfulness.model_json_schema()) == GOLDEN["schema_faithfulness"]


def test_a_stray_judgement_kind_is_a_reading():
    assert w._kind(Sentence(text="x", claims=[1], kind="judgement")) == "reading"
    assert w._kind(Sentence(text="x", claims=[1], kind="reading")) == "reading"
    assert w._kind(Sentence(text="x", claims=[1], kind="counter")) == "fact"  # not asked for


def test_the_candidate_code_names_no_character_book_or_genre():
    import re
    source = (Path(__file__).parents[1] / "lore" / "query" / "judge.py").read_text().lower()
    banned = r"\b(holmes|watson|sherlock|small|hope|drebber|darcy|bennet|wizard|dragon|magic|kingdom|" \
             r"elf|elves|quest|sword|detective|murder|crime|villain|treasure|mormon)\b"
    assert not re.findall(banned, source)
