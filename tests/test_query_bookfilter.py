"""A part that names exactly one library book answers from that book alone.

Retrieval v2 only ranked a named book's passages higher (SCOPE_PENALTY); the
other book's passages still reached the part's share through the graph
edges, the shared candidate pool and the timeline slice. Fixtures use
invented books and names; no test reaches a model.
"""
from __future__ import annotations

from pathlib import Path

from lore.db.sqlite import open_db
from lore.extract.embed import QUERY_PREFIX
from lore.query.agent import answer_question
from lore.query.models import EdgeRow, TimelineBeat
from lore.query.select import TemplateChoice
from lore.query.subgraph import assemble_ranked
from lore.query.synthesize import Claim, Synthesis
from tests.test_query_agent import A, B, Q, EntityKuzu, VectorLance, _scripted


def _conn(tmp_path: Path):
    conn = open_db(tmp_path / "lore.db")
    conn.execute("INSERT INTO books (id, title, file_path) VALUES ('ba', 'Alpha Tale', 'x')")
    conn.execute("INSERT INTO books (id, title, file_path) VALUES ('bb', 'Beta Tale', 'x')")
    for book in ("ba", "bb"):
        for ch in range(1, 5):
            conn.execute("INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text)"
                         " VALUES (?, ?, ?, 0, 0, ?)",
                         (f"{book}:{ch}:0", book, ch, f"MARK{book}{ch} the traveller walked on. " * 5))
    conn.commit()
    return conn


def _scores(favour: str) -> dict[str, float]:
    # `favour`'s book scores higher than the other, chapter by chapter.
    return {f"{b}:{ch}:0": 0.5 - 0.01 * ch + (0.3 if b == favour else 0.0)
            for b in ("ba", "bb") for ch in range(1, 5)}


def _share_books(ev, index: int) -> set[str]:
    return {p.book_id for p in ev.passages if p.n in ev.shares[index]}


def _chunks(*books: str, chapters=(1, 2, 3, 4)):
    return [(f"{b}:{ch}:0", b, ch) for b in books for ch in chapters]


def test_a_part_naming_one_book_takes_no_passage_from_another(tmp_path) -> None:
    conn = _conn(tmp_path)
    # The other book is the more relevant one for BOTH parts.
    ev = assemble_ranked(conn, [], extra_chunks=_chunks("ba", "bb"),
                         score_lists=[_scores("bb"), _scores("bb")],
                         part_books=[["ba"], []])
    assert _share_books(ev, 0) == {"ba"}
    assert _share_books(ev, 1) == {"bb"}  # unscoped: relevance alone decides


def test_parts_naming_none_or_two_books_are_unchanged(tmp_path) -> None:
    conn = _conn(tmp_path)
    plain = assemble_ranked(conn, [], extra_chunks=_chunks("ba", "bb"),
                            score_lists=[_scores("bb"), _scores("ba")], max_chars=400)
    for part_books in ([[], []], [["ba", "bb"], ["ba", "bb"]], [["ba", "bb"], []]):
        same = assemble_ranked(conn, [], extra_chunks=_chunks("ba", "bb"),
                               score_lists=[_scores("bb"), _scores("ba")], max_chars=400,
                               part_books=part_books)
        assert same == plain


def test_the_other_parts_passages_do_not_leak_in_through_the_pool(tmp_path) -> None:
    conn = _conn(tmp_path)
    # Part 1 (Beta) ranks bb chapters first and takes them; part 0 (Alpha) must
    # not list them as "passed over" either.
    ev = assemble_ranked(conn, [], extra_chunks=_chunks("ba", "bb"),
                         score_lists=[_scores("bb"), _scores("bb")], max_chars=10_000,
                         part_books=[["ba"], ["bb"]])
    assert _share_books(ev, 0) == {"ba"} and _share_books(ev, 1) == {"bb"}


def test_edges_of_the_other_book_are_not_in_a_scoped_share(tmp_path) -> None:
    conn = _conn(tmp_path)
    rows = [EdgeRow(subject="Zed", predicate="X", predicate_raw="met", object="Yan", evidence="ev",
                    confidence=0.9, book_id=b, chapter_seq=2, chunk_id=f"{b}:2:0",
                    subject_id="ez", object_id="ey") for b in ("ba", "bb")]
    ev = assemble_ranked(conn, rows, score_lists=[_scores("bb"), _scores("ba")],
                         part_books=[["ba"], ["bb"]])
    assert _share_books(ev, 0) == {"ba"} and _share_books(ev, 1) == {"bb"}


def test_the_timeline_slice_respects_the_book_too(tmp_path) -> None:
    conn = _conn(tmp_path)
    beats = [TimelineBeat(chunk_id=f"{b}:{ch}:0", book_id=b, chapter_seq=ch, subject="Zed", text="t")
             for b in ("ba", "bb") for ch in (1, 3)]
    ev = assemble_ranked(conn, [], extra_chunks=[], score_lists=[_scores("bb"), _scores("ba")],
                         timelines=[beats, []], timeline_share=0.5, max_chars=10_000,
                         part_books=[["ba"], []])
    assert _share_books(ev, 0) == {"ba"}


def test_no_book_filter_when_the_part_has_no_passage_of_its_book(tmp_path) -> None:
    conn = _conn(tmp_path)
    ev = assemble_ranked(conn, [], extra_chunks=_chunks("bb"),
                         score_lists=[_scores("bb"), _scores("bb")], part_books=[["ba"], []])
    assert ev.shares[0] == ()  # an empty share: the part has no evidence, not another book's


# --- through the agent ------------------------------------------------------

def _row(chunk, ch, book, entity, distance):
    return {"chunk_id": chunk, "entity_ids": [entity], "_distance": distance,
            "book_id": book, "chapter_seq": ch, "text": ""}


def _edge(book, ch):
    return ["Zed", "UNCATEGORIZED", "met", "Yan", "ev", 0.9, book, ch, f"{book}:{ch}:0", "ez", "ey"]


async def _ask(tmp_path, sub_questions):
    conn = _conn(tmp_path)
    question = "Who is Zed in both books?"
    vectors = {f"{b}:{ch}:0": [0.9, 0.1 * ch, 0.0] for b in ("ba", "bb") for ch in range(1, 5)}
    lance = VectorLance(
        {tuple(Q): [_row("bb:1:0", 1, "bb", "ez", 0.1)],
         tuple(A): [_row("bb:2:0", 2, "bb", "ez", 0.05), _row("ba:1:0", 1, "ba", "ez", 0.2)],
         tuple(B): [_row("ba:3:0", 3, "ba", "ez", 0.05), _row("bb:3:0", 3, "bb", "ez", 0.2)]},
        vectors)
    edges = [_edge(b, ch) for b in ("ba", "bb") for ch in (1, 2, 3)]
    kuzu = EntityKuzu({"ez": edges})
    vecs = {QUERY_PREFIX + question: Q}
    for text, vec in zip(sub_questions, (A, B)):
        vecs[QUERY_PREFIX + text] = vec
    models, calls = _scripted(vecs, sub_questions, [TemplateChoice(template="neighbours", entities=[1])] * 2,
                              Synthesis(claims=[], can_answer=False))
    await answer_question(conn, kuzu, lance, question, models)
    conn.close()
    return calls["synthesize"]


async def test_each_part_prompt_holds_only_its_own_books_passages(tmp_path) -> None:
    prompts = await _ask(tmp_path, ["Who is Zed in Alpha Tale?", "Who is Zed in Beta Tale?"])
    assert len(prompts) == 2
    assert "MARKba" in prompts[0] and "MARKbb" not in prompts[0]
    assert "MARKbb" in prompts[1] and "MARKba" not in prompts[1]


async def test_parts_naming_no_book_still_share_both(tmp_path) -> None:
    prompts = await _ask(tmp_path, ["Who is Zed early on?", "Who is Zed later on?"])
    assert all("MARKba" in p and "MARKbb" in p for p in prompts)
