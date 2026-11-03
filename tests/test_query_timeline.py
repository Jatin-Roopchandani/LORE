"""The query timeline (beats): selection, the budget slice, the prompt block,
and the byte-identical guarantee when it is off or has nothing to show.

Fixtures use invented books and names. No test reaches a model.
"""
from __future__ import annotations

import json
from pathlib import Path

from lore.db.sqlite import open_db
from lore.extract.embed import QUERY_PREFIX
from lore.query.agent import answer_question
from lore.query.models import EdgeRow
from lore.query.select import TemplateChoice
from lore.query.subgraph import assemble_ranked
from lore.query.synthesize import Claim, Synthesis, _build_prompt
from tests.test_query_agent import A, B, Q, EntityKuzu, VectorLance, _edge_row, _scripted

GOLDEN = Path(__file__).parent / "golden" / "timeline_off.json"


# --- the golden scenarios: written against a42a86f, before any timeline code.
# They use only the API that existed then.

def _golden_assemble() -> dict:
    """assemble_ranked + _build_prompt over two books, two parts."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        conn = open_db(Path(tmp) / "lore.db")
        conn.execute("INSERT INTO books (id, title, file_path, series, series_index)"
                     " VALUES ('ba', 'Alpha Tale', 'x', 'Alpha Cycle', 1)")
        conn.execute("INSERT INTO books (id, title, file_path, series, series_index)"
                     " VALUES ('bb', 'Beta Tale', 'x', 'Alpha Cycle', 2)")
        for book in ("ba", "bb"):
            for ch in range(1, 5):
                conn.execute(
                    "INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text)"
                    " VALUES (?, ?, ?, 0, 0, ?)",
                    (f"{book}:{ch}:0", book, ch, f"Marker {book}{ch}: the traveller crossed the river at dawn. " * 20))
        conn.commit()
        rows = [EdgeRow(subject="Ana", predicate="X", predicate_raw="met", object="Bo",
                        evidence="ev", confidence=0.9, book_id=b, chapter_seq=ch,
                        chunk_id=f"{b}:{ch}:0", subject_id="ea", object_id="eb")
                for b, ch in (("ba", 2), ("bb", 3))]
        extra = [(f"{b}:{ch}:0", b, ch) for b in ("ba", "bb") for ch in (1, 4)]
        scores = [{f"{b}:{ch}:0": 0.9 - 0.05 * (ch + (b == "bb")) for b in ("ba", "bb")
                   for ch in range(1, 5)},
                  {f"{b}:{ch}:0": 0.1 * ch + (b == "bb") * 0.01 for b in ("ba", "bb")
                   for ch in range(1, 5)}]
        out = {}
        for name, max_chars in (("wide", 36_000), ("tight", 3000)):
            ev = assemble_ranked(conn, rows, extra_chunks=extra, max_chars=max_chars,
                                 score_lists=scores)
            out[name] = {
                "passages": [[p.n, p.chunk_id, p.book_id, p.chapter_seq, p.text]
                             for p in ev.passages],
                "edges": [[e.subject, e.chunk_id] for e in ev.edges],
                "truncated": ev.truncated,
                "shares": [list(s) for s in ev.shares],
                "prompt": _build_prompt("Who is Ana?", ev, {"ba": "Alpha Tale",
                                                            "bb": "Beta Tale"}),
                "part_prompt": _build_prompt("Who is Ana?", ev, part_of="Who is Ana, and why?"),
            }
        conn.close()
    return out


def _agent_conn(tmp_path: Path):
    conn = open_db(tmp_path / "lore.db")
    conn.execute("INSERT INTO books (id, title, file_path) VALUES ('b1', 'A Study in Scarlet', 'x')")
    for ch in range(1, 13):
        conn.execute("INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text)"
                     " VALUES (?, 'b1', ?, 0, 0, ?)",
                     (f"b1:{ch}:0", ch, f"MARK{ch} " + f"Passage {ch} of the invented tale. " * 20))
    conn.commit()
    return conn


class _KindKuzu(EntityKuzu):
    kind = "Character"

    def execute(self, cypher, params=None):
        if "e.kind" in cypher and "e.aliases" not in cypher:
            return [[i, i.upper(), self.kind, ["b1"]] for i in (params or {})["ids"]]
        return super().execute(cypher, params)


async def _golden_agent(tmp_path: Path, *, prepare=None, kind="Character") -> dict:
    """A two-part question through answer_question; every claims prompt and the
    evidence passages."""
    conn = _agent_conn(tmp_path)
    if prepare is not None:
        prepare(conn)
    question = "Who is A, and why B?"
    subs = ["Who is A?", "Why B?"]
    vectors = {f"b1:{ch}:0": [0.95 - 0.05 * ch, 0.01 * ch, 0.0] for ch in range(1, 13)}
    def row(chunk, distance, ch, entity):
        return {"chunk_id": chunk, "entity_ids": [entity], "_distance": distance,
                "book_id": "b1", "chapter_seq": ch, "text": ""}

    lance = VectorLance(
        {tuple(Q): [row("b1:2:0", 0.4, 2, "ea")],
         tuple(A): [row("b1:2:0", 0.1, 2, "ea"), row("b1:3:0", 0.2, 3, "ea")],
         tuple(B): [row("b1:4:0", 0.5, 4, "eb")]},
        vectors)
    kuzu = _KindKuzu({"ea": [_edge_row("b1:2:0", 2, "A"), _edge_row("b1:3:0", 3, "A")],
                      "eb": [_edge_row("b1:4:0", 4, "B")]})
    kuzu.kind = kind
    synthesis = Synthesis(claims=[Claim(text="A is in chapter two.", passage=1,
                                        quote="MARK2 Passage 2 of")], can_answer=True)
    models, calls = _scripted(
        {QUERY_PREFIX + question: Q, QUERY_PREFIX + subs[0]: A, QUERY_PREFIX + subs[1]: B},
        subs, [TemplateChoice(template="neighbours", entities=[1]),
               TemplateChoice(template="two_hop", entities=[1])], synthesis)
    answer = await answer_question(conn, kuzu, lance, question, models)
    conn.close()
    return {"prompts": calls["synthesize"],
            "passages": [[p.n, p.chunk_id, p.text] for p in answer.passages],
            "text": answer.text}


def test_golden_assemble_matches_head(monkeypatch) -> None:
    assert _golden_assemble() == json.loads(GOLDEN.read_text())["assemble"]


async def test_golden_agent_matches_head(tmp_path) -> None:
    assert await _golden_agent(tmp_path) == json.loads(GOLDEN.read_text())["agent"]


# --- the feature -----------------------------------------------------------

import sqlite3  # noqa: E402

import pytest  # noqa: E402

from lore.beats.store import Beat, beat_id, save_beats  # noqa: E402
from lore.query import timeline as tl  # noqa: E402
from lore.query.models import BookInfo, Evidence, Passage, SeedEntity, TimelineBeat  # noqa: E402


def _seed(i: str, kind: str = "Character") -> SeedEntity:
    return SeedEntity(id=i, name=i.upper(), kind=kind, books=("b1",), score=0.1)


def _beat(book: str, chapter: int, n: int = 0, subject: str = "A",
          text: str | None = None, chunk: str | None = None) -> Beat:
    quote = f"quote {book} {chapter} {n}"
    return Beat(id=beat_id(book, chapter, (subject,), quote), book_id=book,
                chapter_seq=chapter, chunk_id=chunk or f"{book}:{chapter}:{n}",
                entities=(subject,), kind="action",
                text=text or f"{subject} acts in chapter {chapter}.", quote=quote)


class _BeatLance:
    """search_beats answers from a fixed ranking, restricted to the ids asked."""

    def __init__(self, ranking: list[str]) -> None:
        self.ranking, self.calls = ranking, []

    def search_beats(self, vector, beat_ids, k):
        self.calls.append((tuple(vector), list(beat_ids), k))
        allowed = set(beat_ids)
        return [{"id": i} for i in self.ranking if i in allowed][:k]


BOOKS = {"b1": BookInfo(title="Alpha Tale")}


def test_timeline_is_on_and_takes_thirty_percent() -> None:
    assert tl.TIMELINE is True and tl.TIMELINE_SHARE == 0.30


def test_timeline_entities_keep_characters_factions_objects_capped_at_three() -> None:
    seeds = [_seed("a"), _seed("p", "Place"), _seed("f", "Faction"),
             _seed("o", "Object"), _seed("c")]
    assert [s.id for s in tl.timeline_entities(seeds)] == ["a", "f", "o"]
    assert [s.id for s in tl.timeline_entities(seeds, kinds=("Place",))] == ["p"]
    assert tl.timeline_entities([_seed("p", "Place")]) == []


def test_few_beats_are_all_kept_in_story_order() -> None:
    beats = [_beat("b1", 5), _beat("b1", 2), _beat("b1", 9)]
    got = tl.select_beats(beats, [0.1], _BeatLance([]), BOOKS)
    assert [b.chapter_seq for b in got] == [2, 5, 9]


def test_story_order_across_books_follows_the_series() -> None:
    books = {"x": BookInfo("Second", "Cycle", 2), "y": BookInfo("First", "Cycle", 1)}
    beats = [_beat("x", 1), _beat("y", 7), _beat("x", 3)]
    got = tl.select_beats(beats, [0.1], _BeatLance([]), books)
    assert [(b.book_id, b.chapter_seq) for b in got] == [("y", 7), ("x", 1), ("x", 3)]


def test_many_beats_are_spread_plus_relevant_with_both_ends() -> None:
    beats = [_beat("b1", ch) for ch in range(1, 61)]  # 60 beats, one per chapter
    # The most relevant, best first: chapters 7, 8, 9 ... (7 and 8 may be spread too).
    lance = _BeatLance([b.id for b in beats if b.chapter_seq in range(7, 40)])
    got = tl.select_beats(beats, [0.3, 0.4], lance, BOOKS)
    chapters = [b.chapter_seq for b in got]
    assert chapters == sorted(chapters) and len(set(chapters)) == len(chapters)
    assert chapters[0] == 1 and chapters[-1] == 60
    assert len(chapters) == 24
    # The relevance search saw only the beats the spread did not take.
    (vector, ids, k), = lance.calls
    assert vector == (0.3, 0.4) and k == 12
    spread = {b.id for b in beats} - set(ids)
    assert len(spread) == 12
    assert {1, 60} <= {b.chapter_seq for b in beats if b.id in spread}
    # Every relevant beat is among the 12 best of those not already spread.
    wanted = [b.id for b in beats if b.id in lance.ranking and b.id in ids][:12]
    assert {b.id for b in got} == spread | set(wanted)


def test_spread_is_even() -> None:
    beats = [_beat("b1", ch) for ch in range(1, 101)]
    got = tl.select_beats(beats, [0.1], _BeatLance([]), BOOKS)
    chapters = [b.chapter_seq for b in got]
    assert chapters == [1, 10, 19, 28, 37, 46, 55, 64, 73, 82, 91, 100]


def test_part_timeline_reads_the_entities_beats_and_scopes_to_named_books(tmp_path) -> None:
    conn = open_db(tmp_path / "lore.db")
    for b in ("b1", "b2"):
        conn.execute("INSERT INTO books (id, title, file_path) VALUES (?, ?, 'x')", (b, b))
    save_beats(conn, "b1", 1, 0, [_beat("b1", 1)])
    save_beats(conn, "b2", 1, 0, [_beat("b2", 1)])
    conn.execute("UPDATE beat_entities SET entity_id='ea'")
    conn.commit()
    books = {"b1": BookInfo("b1"), "b2": BookInfo("b2")}
    both = tl.part_timeline(conn, _BeatLance([]), [_seed("ea")], [0.1], books)
    assert [b.book_id for b in both] == ["b1", "b2"]
    only = tl.part_timeline(conn, _BeatLance([]), [_seed("ea")], [0.1], books,
                            book_ids=["b2"])
    assert [b.book_id for b in only] == ["b2"]
    # No timeline entity (a place), or none of its beats: nothing.
    assert tl.part_timeline(conn, _BeatLance([]), [_seed("ea", "Place")], [0.1], books) == []
    assert tl.part_timeline(conn, _BeatLance([]), [_seed("zz")], [0.1], books) == []
    conn.close()


def test_a_library_without_the_beats_tables_has_no_timeline(tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "old.db")
    assert tl.part_timeline(conn, _BeatLance([]), [_seed("ea")], [0.1], {}) == []


def test_coverage_order_takes_both_ends_then_bisects() -> None:
    assert tl.coverage_order(0) == []
    assert tl.coverage_order(1) == [0]
    assert tl.coverage_order(2) == [0, 1]
    assert tl.coverage_order(10)[:3] == [0, 9, 4]
    assert sorted(tl.coverage_order(10)) == list(range(10))


# --- the budget slice ------------------------------------------------------

def _slice_conn(tmp_path, ranked=10, timeline=10, size=1000):
    conn = open_db(tmp_path / "slice.db")
    conn.execute("INSERT INTO books (id, title, file_path) VALUES ('b1', 'Alpha Tale', 'x')")
    for ch in range(1, ranked + timeline + 1):
        body = (f"Marker{ch} " + "the traveller crossed the river at dawn. " * 40)[:size]
        conn.execute("INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text)"
                     " VALUES (?, 'b1', ?, 0, 0, ?)", (f"b1:{ch}:0", ch, body))
    conn.commit()
    return conn


def _tbeat(ch: int, subject="A", text=None) -> TimelineBeat:
    return TimelineBeat(chunk_id=f"b1:{ch}:0", book_id="b1", chapter_seq=ch,
                        subject=subject, text=text or f"{subject} acts in {ch}.")


def test_the_timeline_slice_keeps_thirty_percent_and_ranked_keeps_the_rest(tmp_path) -> None:
    conn = _slice_conn(tmp_path)
    ranked_ids = [f"b1:{ch}:0" for ch in range(1, 11)]
    scores = [{c: 1.0 - 0.01 * i for i, c in enumerate(ranked_ids)}]
    timeline = [_tbeat(ch) for ch in range(11, 21)]
    extra = [(b.chunk_id, "b1", b.chapter_seq) for b in timeline] \
        + [(c, "b1", i + 1) for i, c in enumerate(ranked_ids)]
    plain = assemble_ranked(conn, [], extra_chunks=extra, max_chars=10_000, score_lists=scores)
    assert [p.chunk_id for p in plain.passages] == ranked_ids  # all ten ranked fit
    ev = assemble_ranked(conn, [], extra_chunks=extra, max_chars=10_000, score_lists=scores,
                         timelines=[timeline], timeline_share=0.30)
    got = [p.chunk_id for p in ev.passages]
    # 3 timeline chunks: the earliest, the latest, then the middle; 7 ranked.
    assert sorted(c for c in got if int(c.split(":")[1]) > 10) == sorted(
        ["b1:11:0", "b1:15:0", "b1:20:0"])
    assert [c for c in got if int(c.split(":")[1]) <= 10] == ranked_ids[:7]
    assert ev.truncated is True
    assert sum(len(p.text) for p in ev.passages) <= 10_000
    assert len(ev.timeline) == 10 and [len(t) for t in ev.timelines] == [10]
    conn.close()


def test_an_unused_timeline_slice_goes_back_to_ranked_evidence(tmp_path) -> None:
    conn = _slice_conn(tmp_path)
    ranked_ids = [f"b1:{ch}:0" for ch in range(1, 11)]
    scores = [{c: 1.0 - 0.01 * i for i, c in enumerate(ranked_ids)}]
    extra = [(c, "b1", i + 1) for i, c in enumerate(ranked_ids)]
    ev = assemble_ranked(conn, [], extra_chunks=extra, max_chars=10_000, score_lists=scores,
                         timelines=[[_tbeat(11)]], timeline_share=0.30)
    got = [p.chunk_id for p in ev.passages]
    assert "b1:11:0" in got and len(got) == 10  # 1 timeline + 9 ranked
    assert [c for c in got if c != "b1:11:0"] == ranked_ids[:9]
    conn.close()


def test_a_timeline_chunk_joins_the_candidates_and_is_not_duplicated(tmp_path) -> None:
    conn = _slice_conn(tmp_path, ranked=3, timeline=1)
    ids = [f"b1:{ch}:0" for ch in (1, 2, 3)]
    scores = [{c: 0.9 for c in ids}]
    ev = assemble_ranked(conn, [], extra_chunks=[(c, "b1", 1) for c in ids],
                         max_chars=36_000, score_lists=scores,
                         timelines=[[_tbeat(2), _tbeat(4)]], timeline_share=0.30)
    got = [p.chunk_id for p in ev.passages]
    assert sorted(got) == ["b1:1:0", "b1:2:0", "b1:3:0", "b1:4:0"]  # no duplicate of 2
    assert [p.n for p in ev.passages] == [1, 2, 3, 4]  # numbered in reading order
    conn.close()


def test_each_part_share_holds_its_own_timeline_passages(tmp_path) -> None:
    conn = _slice_conn(tmp_path, ranked=2, timeline=2)
    # Chunks 1 and 2 are what each part ranks first; 3 and 4 only come from
    # a timeline. Budget for three: the slice (1,050) holds one, the part
    # taken first, and ranked holds the other two.
    low = {"b1:3:0": 0.0, "b1:4:0": 0.0}
    scores = [{"b1:1:0": 0.9, "b1:2:0": 0.2, **low}, {"b1:2:0": 0.9, "b1:1:0": 0.2, **low}]
    extra = [("b1:1:0", "b1", 1), ("b1:2:0", "b1", 2)]
    ev = assemble_ranked(conn, [], extra_chunks=extra, max_chars=3500, score_lists=scores,
                         timelines=[[_tbeat(3)], [_tbeat(4, "B")]], timeline_share=0.30)
    number = {p.chunk_id: p.n for p in ev.passages}
    assert sorted(number) == ["b1:1:0", "b1:2:0", "b1:3:0"]  # part 2's beat is cut
    assert number["b1:3:0"] in ev.shares[0]
    assert {number["b1:1:0"], number["b1:2:0"]} <= set(ev.shares[0]) | set(ev.shares[1])
    assert ev.truncated is True
    assert [[b.chunk_id for b in t] for t in ev.timelines] == [["b1:3:0"], ["b1:4:0"]]
    conn.close()


def test_no_timeline_or_no_share_leaves_assembly_alone(tmp_path) -> None:
    conn = _slice_conn(tmp_path, ranked=4, timeline=0)
    ids = [f"b1:{ch}:0" for ch in range(1, 5)]
    scores = [{c: 0.9 - 0.1 * i for i, c in enumerate(ids)}]
    extra = [(c, "b1", i + 1) for i, c in enumerate(ids)]
    base = assemble_ranked(conn, [], extra_chunks=extra, max_chars=2500, score_lists=scores)
    assert assemble_ranked(conn, [], extra_chunks=extra, max_chars=2500, score_lists=scores,
                           timelines=[[]], timeline_share=0.30) == base
    assert assemble_ranked(conn, [], extra_chunks=extra, max_chars=2500, score_lists=scores,
                           timelines=(), timeline_share=0.30) == base
    assert base.timeline == () and base.timelines == ()
    conn.close()


# --- the prompt block ------------------------------------------------------

def _evidence(timeline=(), timelines=()):
    passages = tuple(Passage(n=i + 1, chunk_id=f"b1:{ch}:0", book_id="b1", chapter_seq=ch,
                             text=f"Passage text {ch}.") for i, ch in enumerate((2, 5, 9)))
    return Evidence(edges=(), passages=passages, truncated=False,
                    timeline=tuple(timeline), timelines=tuple(timelines))


def test_the_block_lists_each_beat_in_story_order_with_its_passage() -> None:
    from lore.query import synthesize as s
    beats = [_tbeat(9, "Zed", "Zed leaves."), _tbeat(2, "Zed", "Zed arrives."),
             _tbeat(5, "Zed", "Zed waits.")]
    prompt = s._build_prompt("Who is Zed?", _evidence(beats), {"b1": "Alpha Tale"})
    block = prompt[prompt.index("Story timeline"):prompt.index("Passages")]
    lines = [ln for ln in block.splitlines() if ln.startswith("- [")]
    assert lines == [
        "- [1] (Alpha Tale, chapter 2, passage 1 in the chapter) Zed: Zed arrives.",
        "- [2] (Alpha Tale, chapter 5, passage 1 in the chapter) Zed: Zed waits.",
        "- [3] (Alpha Tale, chapter 9, passage 1 in the chapter) Zed: Zed leaves."]
    assert "map, not evidence" in block and "word for word" in block
    assert prompt.index("Question:") < prompt.index("Story timeline") < prompt.index("Passages")


def test_a_beat_whose_passage_was_cut_is_not_shown() -> None:
    from lore.query import synthesize as s
    prompt = s._build_prompt("Who is Zed?", _evidence([_tbeat(7, "Zed", "Zed hides."),
                                                       _tbeat(5, "Zed", "Zed waits.")]),
                             {"b1": "Alpha Tale"})
    assert "Zed waits." in prompt and "Zed hides." not in prompt
    assert "Story timeline" not in s._build_prompt(
        "q", _evidence([_tbeat(7, "Zed", "Zed hides.")]), {"b1": "Alpha Tale"})


def test_no_beats_no_block() -> None:
    from lore.query import synthesize as s
    assert "Story timeline" not in s._build_prompt("q", _evidence(), {"b1": "Alpha Tale"})


async def test_each_part_prompt_shows_only_its_own_timeline() -> None:
    from lore.query import synthesize as s
    ev = _evidence(
        timeline=[_tbeat(2, "Zed", "Zed arrives."), _tbeat(9, "Yan", "Yan leaves.")],
        timelines=[[_tbeat(2, "Zed", "Zed arrives.")], [_tbeat(9, "Yan", "Yan leaves.")]])
    ev = Evidence(edges=ev.edges, passages=ev.passages, truncated=False,
                  shares=((1, 2), (2, 3)), timeline=ev.timeline, timelines=ev.timelines)
    prompts = []

    async def generate(prompt, schema):
        prompts.append(prompt)
        return Synthesis(claims=[], can_answer=False)

    await s.synthesize_parts("Q?", ["Part one?", "Part two?"], ev, generate, mode="graph",
                             book_titles={"b1": "Alpha Tale"})
    assert "Zed arrives." in prompts[0] and "Yan leaves." not in prompts[0]
    assert "Yan leaves." in prompts[1] and "Zed arrives." not in prompts[1]


def test_the_timeline_rule_text_is_general() -> None:
    from lore.query import synthesize as s
    assert "{lines}" in s._TIMELINE_BLOCK


# --- through the agent -----------------------------------------------------

def _with_beats(conn) -> None:
    """Two moments of entity ea, one in a passage nothing else would find."""
    save_beats(conn, "b1", 9, 0, [_beat("b1", 9, 0, "A", "A sets out alone.", "b1:9:0")])
    save_beats(conn, "b1", 11, 0, [_beat("b1", 11, 0, "A", "A comes back changed.", "b1:11:0")])
    conn.execute("UPDATE beat_entities SET entity_id='ea' WHERE name='A'")
    conn.commit()


async def test_agent_off_with_beats_present_equals_head(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(tl, "TIMELINE", False)
    got = await _golden_agent(tmp_path, prepare=_with_beats)
    assert got == json.loads(GOLDEN.read_text())["agent"]


async def test_agent_on_without_beats_equals_head(tmp_path) -> None:
    assert tl.TIMELINE is True
    assert await _golden_agent(tmp_path) == json.loads(GOLDEN.read_text())["agent"]


async def test_agent_on_with_beats_of_a_place_equals_head(tmp_path) -> None:
    got = await _golden_agent(tmp_path, prepare=_with_beats, kind="Place")
    assert got == json.loads(GOLDEN.read_text())["agent"]


async def test_agent_without_the_beats_tables_equals_head(tmp_path) -> None:
    def drop(conn):
        conn.execute("DROP TABLE beat_entities")
        conn.execute("DROP TABLE beats")
        conn.commit()

    assert await _golden_agent(tmp_path, prepare=drop) == json.loads(GOLDEN.read_text())["agent"]


async def test_agent_on_with_beats_brings_their_passages_and_the_block(tmp_path) -> None:
    got = await _golden_agent(tmp_path, prepare=_with_beats)
    chunks = [p[1] for p in got["passages"]]
    # The two beat passages were in no candidate list; now they are evidence.
    assert "b1:9:0" in chunks and "b1:11:0" in chunks
    first, second = got["prompts"]
    # Part 1 seeds entity ea: its timeline, in story order, with the rule.
    assert "Story timeline" in first
    assert first.index("A sets out alone.") < first.index("A comes back changed.")
    # Part 2 seeds eb, which has no beats: its prompt is as before.
    assert "Story timeline" not in second
    head = json.loads(GOLDEN.read_text())["agent"]
    # Everything the head showed is still shown.
    assert {p[1] for p in head["passages"]} <= set(chunks)


def test_run_pair_has_a_timeline_arm() -> None:
    import importlib.util
    path = Path(__file__).parents[1] / "evals/answer_quality/run_pair.py"
    spec = importlib.util.spec_from_file_location("run_pair_timeline", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.parse_arm("timeline=off") == {("lore.query.timeline", "TIMELINE"): False}
    assert module.parse_arm("timeline=on") == {("lore.query.timeline", "TIMELINE"): True}
