"""Recap answers (lore/query/recap.py): the span's beats in story order, an even
spread across chapters, nothing past the reading position, the fallback without
beats, the chronological writer, and nothing changed with RECAP off.

Real SQLite, Kuzu and LanceDB over tests/position_fixtures.py (invented books);
the models are scripted and no test reaches one.
"""
from __future__ import annotations

import re

import pytest

from lore.beats.store import Beat, save_beats
from lore.db.sqlite import open_db
from lore.mcp.stores import open_stores
from lore.query import decompose as decompose_module
from lore.query import recap as recap_module
from lore.query.agent import answer_question
from lore.query.decompose import PartSpec, Recap, RecapDecomposition
from lore.query.scope import Scope
from lore.query.models import BookInfo, Claim, Passage, SectionSpec
from lore.query.recap import (
    Chapter, Chunk, balanced_split, chapter_label, dedupe_beats, plan_stretches,
    section_evidence, spread_by_chapter,
)
from lore.query.synthesize import Faithfulness, Judgement, Synthesis, _build_prompt
from lore.query.write import Prose, Sentence, writer_prompt
from lore.reading_position import ReadingPosition, set_position
from tests.position_fixtures import HOLLOW, build_position_library
from tests.worlds_fixtures import QUESTION_VECTOR, unit

QUESTION = "Summarise what has happened in the story until now."


def _beat(book: str, chapter: int, n: int, text: str | None = None, seq: int = 0) -> Beat:
    return Beat(id=f"{book}-{chapter}-{n}", book_id=book, chapter_seq=chapter,
                chunk_id=f"{book}:{chapter}:{seq}", entities=("Ana",), kind="action",
                text=text or f"moment {n} of chapter {chapter}", quote="q")


# --- an even spread across chapters ----------------------------------------------------

def test_all_beats_are_kept_within_the_budget() -> None:
    beats = [_beat("b", c, i) for c in (1, 2) for i in range(3)]
    assert spread_by_chapter(beats, 10) == beats


def test_over_the_budget_every_chapter_gets_a_fair_share_in_story_order() -> None:
    sizes = {1: 10, 2: 2, 3: 10, 4: 1}
    beats = [_beat("b", c, i) for c, n in sizes.items() for i in range(n)]
    got = spread_by_chapter(beats, 9)
    per = {c: sum(b.chapter_seq == c for b in got) for c in sizes}
    assert len(got) == 9
    assert all(per[c] >= 1 for c in sizes)  # every chapter represented
    assert per[2] == 2 and per[4] == 1  # a small chapter gives what it has
    assert abs(per[1] - per[3]) <= 1  # the big ones share the rest evenly
    assert got == [b for b in beats if b in got]  # story order kept
    # Along a chapter: its first and last moments before the middle.
    ones = [b.id for b in got if b.chapter_seq == 1]
    assert "b-1-0" in ones and "b-1-9" in ones


def test_fewer_slots_than_chapters_spread_the_chapters() -> None:
    beats = [_beat("b", c, 0) for c in range(1, 10)]
    got = [b.chapter_seq for b in spread_by_chapter(beats, 3)]
    assert got == [1, 5, 9]


def test_near_duplicate_beats_of_one_passage_are_dropped() -> None:
    beats = [_beat("b", 1, 0, "Ana opens the iron door at night"),
             _beat("b", 1, 1, "Ana opens the iron door at night slowly"),
             _beat("b", 1, 2, "Bo sails away across the bay"),
             _beat("b", 1, 3, "Ana opens the iron door at night", seq=1)]  # another passage
    assert [b.id for b in dedupe_beats(beats)] == ["b-1-0", "b-1-2", "b-1-3"]


# --- the sections ----------------------------------------------------------------------

def _chapter(book: str, chapter: int, chunks: int, chars: int = 100) -> Chapter:
    return Chapter(book, chapter, tuple(Chunk(f"{book}:{chapter}:{i}", book, chapter,
                                              f"{book}{chapter}.{i} " + "x" * chars)
                                        for i in range(chunks)))


def test_balanced_split_is_consecutive_non_empty_and_even() -> None:
    groups = balanced_split([5] * 12, 4)
    assert groups == [[0, 1, 2], [3, 4, 5], [6, 7, 8], [9, 10, 11]]
    assert balanced_split([1, 1], 5) == [[0], [1]]
    assert balanced_split([30, 1, 1, 1], 2)[0] == [0]


def test_a_short_span_is_one_section_and_a_long_one_is_capped() -> None:
    short = plan_stretches([_chapter("b", 1, 4), _chapter("b", 2, 4)], {"b": "Alpha"})
    assert [s.heading for s in short] == ["Chapters 1–2"]
    assert short[0].focus == "chapters 1–2 of Alpha" and short[0].group == ""
    whole = plan_stretches([_chapter("b", c, 12) for c in range(1, 21)], {"b": "Alpha"})
    assert len(whole) == recap_module.RECAP_MAX_SECTIONS
    flat = [c.chapter for s in whole for c in s.chapters]
    assert flat == list(range(1, 21))  # every chapter once, in order


def test_sections_never_cross_books_and_are_grouped_by_book() -> None:
    chapters = [_chapter("b1", c, 10) for c in (1, 2, 3)] + [_chapter("b2", c, 10) for c in (1, 2)]
    got = plan_stretches(chapters, {"b1": "Alpha One", "b2": "Alpha Two"})
    assert {len({c.book_id for c in s.chapters}) for s in got} == {1}
    assert [s.group for s in got][0] == "Alpha One" and got[-1].group == "Alpha Two"
    assert [s.book_id for s in got] == sorted(s.book_id for s in got)


def test_chapter_label() -> None:
    assert chapter_label([4]) == "Chapter 4" and chapter_label([6, 3, 4]) == "Chapters 3–6"


def test_a_thin_chapter_gets_passages_spread_over_it() -> None:
    chapters = (_chapter("b", 1, 6), _chapter("b", 2, 6), _chapter("b", 3, 6))
    stretch = recap_module.Stretch("b", chapters, "Chapters 1–3", "chapters 1–3 of B")
    beats = [_beat("b", 1, i, seq=i) for i in range(5)] + [_beat("b", 2, 0, seq=2)]
    evidence, shown = section_evidence(stretch, beats, 100_000)
    by_chapter = {c: [p.chunk_id for p in evidence.passages if p.chapter_seq == c] for c in (1, 2, 3)}
    assert by_chapter[1] == [f"b:1:{i}" for i in range(5)]  # its beats' passages only
    assert len(by_chapter[2]) == 3 and "b:2:2" in by_chapter[2]  # one beat + two spread
    assert len(by_chapter[3]) == 3 and {"b:3:0", "b:3:5"} <= set(by_chapter[3])  # spread, both ends
    assert [p.n for p in evidence.passages] == list(range(1, len(evidence.passages) + 1))
    assert [p.chunk_id for p in evidence.passages] == sorted(
        (p.chunk_id for p in evidence.passages), key=lambda c: (int(c.split(":")[1]), int(c.split(":")[2])))
    assert [b.chunk_id for b in shown] == [t.chunk_id for t in evidence.timeline]


def test_a_tight_budget_still_represents_every_chapter() -> None:
    chapters = tuple(_chapter("b", c, 8, chars=900) for c in range(1, 6))
    stretch = recap_module.Stretch("b", chapters, "Chapters 1–5", "chapters 1–5 of B")
    beats = [_beat("b", c, i, seq=i) for c in range(1, 6) for i in range(8)]
    evidence, _ = section_evidence(stretch, beats, 5 * 1000)
    assert {p.chapter_seq for p in evidence.passages} == {1, 2, 3, 4, 5}
    assert evidence.truncated


def test_a_book_without_beats_is_recapped_from_passages_spread_across_it() -> None:
    chapters = tuple(_chapter("b", c, 9) for c in range(1, 5))
    stretch = recap_module.Stretch("b", chapters, "Chapters 1–4", "chapters 1–4 of B")
    evidence, shown = section_evidence(stretch, [], 100_000)
    assert shown == [] and evidence.timeline == ()
    for c in range(1, 5):
        mine = [p.chunk_id for p in evidence.passages if p.chapter_seq == c]
        assert len(mine) == recap_module.THIN_CHAPTER_PASSAGES
        assert f"b:{c}:0" in mine and f"b:{c}:8" in mine


# --- the prompts -----------------------------------------------------------------------

def _claims_and_passages():
    passages = [Passage(n=1, chunk_id="b:2:0", book_id="b", chapter_seq=2, text="late text"),
                Passage(n=2, chunk_id="b:1:0", book_id="b", chapter_seq=1, text="early text")]
    claims = [Claim(text="Later, Ana leaves.", passage=1, quote="late text"),
              Claim(text="First, Ana arrives.", passage=2, quote="early text")]
    return claims, passages


def test_the_writer_is_asked_for_a_chronological_narrative_without_readings() -> None:
    claims, passages = _claims_and_passages()
    prompt = writer_prompt("What happens in chapters 1–2 of Alpha?", [], claims, passages,
                           {"b": "Alpha"}, length="recap")
    flat = " ".join(prompt.split())
    assert "chronological narrative" in flat and "story order" in flat
    assert "transitions" in flat
    assert "Do not interpret them" in flat
    assert '"reading"' not in prompt  # no readings rule in a recap section
    # The facts reach the writer in story order: the early one first.
    facts = prompt.split("Facts:")[-1]
    assert facts.index("Ana arrives") < facts.index("Ana leaves")


def test_the_short_answer_is_asked_for_the_gist() -> None:
    claims, passages = _claims_and_passages()
    flat = " ".join(writer_prompt("q", [], claims, passages, {"b": "Alpha"},
                                  length="recap_short").split())
    assert "one or two sentences that give the gist of the whole stretch" in flat


def test_the_claims_step_is_asked_for_the_stretchs_events_in_order() -> None:
    from lore.query.models import Evidence, TimelineBeat
    passages = (Passage(n=1, chunk_id="b:1:0", book_id="b", chapter_seq=1, text="t " * 10),)
    beat = TimelineBeat(chunk_id="b:1:0", book_id="b", chapter_seq=1, subject="Ana", text="arrives")
    evidence = Evidence(edges=(), passages=passages, truncated=False, timeline=(beat,),
                        timelines=((beat,),))
    spec = SectionSpec(part_of=QUESTION, max_claims=12, length="recap", focus="chapter 1 of B")
    prompt = " ".join(_build_prompt("What happens in chapter 1 of B?", evidence, {"b": "B"},
                                    section=spec).split())
    assert "one section of a recap, told in story order" in prompt
    assert "This section covers chapter 1 of B" in prompt
    assert "Story beats:" in prompt and "Story timeline:" not in prompt
    assert "skip incidental plot detail" not in prompt


# --- through the agent, over a library -------------------------------------------------

class RecapModels:
    """Scripted models: the decomposer says recap; every claims call quotes each
    passage's marker; the writer writes one sentence per fact; the check supports all."""

    def __init__(self, recap: Recap | dict | None, *, vectors=None):
        self.recap = recap
        # text fragment -> the vector any text containing it is embedded as
        self.vectors = vectors or {}
        self.calls: list[tuple[str, str]] = []

    def prompts(self, kind):
        return [p for k, p in self.calls if k == kind]

    async def embed(self, texts):
        self.calls.append(("embed", "\n".join(texts)))
        return [next((v for k, v in self.vectors.items() if k in t), QUESTION_VECTOR)
                for t in texts]

    async def decompose(self, prompt, schema):
        self.calls.append(("decompose", prompt))
        recap = (self.recap if isinstance(self.recap, dict) or self.recap is None
                 else {"span": self.recap.span, "book": self.recap.book,
                       "from_chapter": self.recap.first, "to_chapter": self.recap.last,
                       "since": self.recap.since})
        return schema.model_validate({"sub_questions": [{"question": QUESTION}],
                                      "depth": "focused", "recap": recap})

    async def select(self, prompt, schema):
        from lore.query.select import TemplateChoice
        self.calls.append(("select", prompt))
        return TemplateChoice(template="neighbours", entities=[1])

    async def generate(self, prompt, schema):
        if schema is Synthesis:
            self.calls.append(("claims", prompt))
            claims = []
            block = prompt.split("\nPassages:\n", 1)[1]
            for n in re.findall(r"^\[(\d+)\]", block, re.M):
                body = block.split(f"[{n}] (", 1)[1].split("\n", 2)[1]
                marker = re.search(r"MARK-\w+-\d+", body).group(0)
                claims.append({"text": f"Event at {marker}.", "passage": int(n),
                               "quote": body.split(" ", 1)[1][:60].rsplit(" ", 1)[0]})
            return Synthesis(can_answer=True, claims=claims)
        if "sentences" in schema.model_fields:
            self.calls.append(("write", prompt))
            count = len(re.findall(r"^\d+\. ", prompt.split("Facts:")[-1], re.M))
            return Prose(sentences=[Sentence(text=f"Then event {i + 1} happens.", claims=[i + 1])
                                    for i in range(count)])
        self.calls.append(("check", prompt))
        n_claims = prompt.count("\n   cites passage")
        n_sentences = len(re.findall(r"^\d+\. sentence(?: \(\w+\))?: ", prompt, re.M))
        return Faithfulness(
            judgements=[Judgement(claim=i, verdict="supported") for i in range(1, n_claims + 1)],
            sentence_judgements=[Judgement(claim=i, verdict="supported")
                                 for i in range(1, n_sentences + 1)])

    def models(self):
        from lore.query.agent import QueryModels

        async def aclose():
            return None
        return QueryModels(self.embed, self.select, self.generate, lambda: "writer", aclose,
                           lambda: "chooser", decompose=self.decompose, check=self.generate,
                           write=self.generate, evidence_chars=36_000)


@pytest.fixture
def config(tmp_path):
    config = build_position_library(tmp_path)
    # More beats: every chapter of the series, and two in the reader's chapter.
    conn = open_db(config.sqlite_path)
    for book, chapter in (("hb1", 2), ("hb2", 2), ("hb2", 3)):
        beat = Beat(id=f"extra-{book}-{chapter}", book_id=book, chapter_seq=chapter,
                    chunk_id=f"{book}:{chapter}:0", entities=("Mira Cole",), kind="action",
                    text=f"BEAT-{book}-{chapter} Mira acts", quote=f"MARK-{book}-{chapter}")
        save_beats(conn, book, chapter, 1, [beat])
    conn.commit()
    conn.close()
    return config


def position(config, book="hb1", chapter=2, read=()):
    conn = open_db(config.sqlite_path)
    try:
        wid = conn.execute("SELECT id FROM worlds WHERE name = ?", (HOLLOW,)).fetchone()[0]
        set_position(conn, ReadingPosition(wid, book, chapter, tuple(read), True))
    finally:
        conn.close()


async def ask(config, scripted, question=QUESTION, **kw):
    events: list[dict] = []
    with open_stores(config) as s:
        answer = await answer_question(s.conn, s.kuzu, s.lance, question, scripted.models(),
                                       world=HOLLOW, on_event=events.append, **kw)
    return answer, events


def _seen(scripted) -> str:
    return "\n".join(p for k, p in scripted.calls if k != "decompose")


async def test_a_recap_reads_the_span_in_story_order_and_writes_sections(config) -> None:
    scripted = RecapModels(Recap("story"))
    position(config, "hb2", 2)  # Hollow Gate read in full, Hollow Keep up to chapter 2
    answer, events = await ask(config, scripted)
    assert answer.template == "recap" and answer.answer_style == "sectioned"
    claims_prompts = scripted.prompts("claims")
    joined = "\n".join(claims_prompts)
    # Series order across the two books: every chapter of book 1 before book 2's.
    marks = re.findall(r"MARK-(hb\d)-(\d)", "\n".join(p.split("Passages:")[-1] for p in claims_prompts))
    order = list(dict.fromkeys(marks))
    assert order == [("hb1", "1"), ("hb1", "2"), ("hb1", "3"), ("hb2", "1"), ("hb2", "2")]
    # Past the position, and a book of unknown order the reader did not mark: never.
    assert "MARK-hb2-3" not in joined and "BEAT-hb2-3" not in joined and "MARK-hb3" not in joined
    assert "BEAT-hb1-2" in joined and "Story beats:" in joined
    # Sections are grouped under the book titles, in story order.
    assert [s.group for s in answer.sections][0] == "Hollow Gate"
    assert answer.sections[-1].group == "Hollow Keep"
    assert answer.short_answer and answer.short_answer[0].short
    assert answer.text.startswith("Short answer")
    names = [(e["name"], e["state"]) for e in events if e["type"] == "step"]
    assert ("recap", "start") in names and ("recap", "done") in names
    assert ("seeds", "start") not in names  # no seed-entity retrieval
    plan = next(e for e in events if e["type"] == "plan")
    assert [s["heading"] for s in plan["sections"]] == list(
        [e for e in events if e["type"] == "parts"][-1]["sub_questions"])  # after the split's
    # Every claim is checked and quotes a numbered passage.
    assert scripted.prompts("check")
    assert all(c.quote for c in answer.claims)


async def test_beats_past_the_reading_position_are_never_selected(config) -> None:
    scripted = RecapModels(Recap("story"))
    position(config, "hb1", 2)
    answer, _ = await ask(config, scripted)
    seen = _seen(scripted)
    for late in ("MARK-hb1-3", "SECRETFACT", "LATEBEAT", "MARK-hb2", "SEQUELBEAT", "BEAT-hb2",
                 "MARK-hb3"):
        assert late not in seen
    assert "EARLYBEAT" in seen and "BEAT-hb1-2" in seen
    assert {p.chapter_seq for p in answer.passages} <= {1, 2}
    assert {p.book_id for p in answer.passages} == {"hb1"}


async def test_a_book_span_and_a_chapter_range(config) -> None:
    scripted = RecapModels(Recap("book", book="Hollow Keep"))
    answer, _ = await ask(config, scripted, question="What happened in Hollow Keep?")
    assert {p.book_id for p in answer.passages} == {"hb2"}
    scripted = RecapModels(Recap("chapters", book="Hollow Gate", first=2, last=3))
    answer, _ = await ask(config, scripted, question="Recap chapters 2 to 3 of Hollow Gate.")
    assert {(p.book_id, p.chapter_seq) for p in answer.passages} == {("hb1", 2), ("hb1", 3)}
    assert [s.heading for s in answer.sections] == ["Chapters 2–3"]


async def test_a_book_by_its_number_in_the_series(config) -> None:
    scripted = RecapModels(Recap("book", book="2"))
    answer, _ = await ask(config, scripted, question="What happened in book 2?")
    assert {p.book_id for p in answer.passages} == {"hb2"}


async def test_a_book_beyond_the_position_says_nothing_is_visible(config) -> None:
    position(config, "hb1", 2)
    scripted = RecapModels(Recap("book", book="Hollow Keep"))
    answer, _ = await ask(config, scripted, question="What happened in Hollow Keep?")
    assert answer.text == recap_module.NOTHING_VISIBLE and answer.mode == "empty"
    assert "MARK-hb2" not in _seen(scripted)


async def test_since_an_event_starts_at_the_event(config) -> None:
    # The event's words are embedded as the vector of the chunk where it happens
    # (Hollow Keep, chapter 1: the fourth chunk of the fixture, unit(0, 8, 0.05)).
    scripted = RecapModels(Recap("since", since="Ren returned to the keep"),
                           vectors={"Ren returned to the keep": unit(0, 8, 0.05)})
    answer, _ = await ask(config, scripted, question="What has happened since Ren came back?")
    assert any("Ren returned to the keep" in p for p in scripted.prompts("embed"))
    got = {(p.book_id, p.chapter_seq) for p in answer.passages}
    assert got == {("hb2", 1), ("hb2", 2), ("hb2", 3)}  # from the event on, nothing before
    assert "MARK-hb1" not in _seen(scripted)
    assert "MARK-hb3" not in _seen(scripted)  # its order against Hollow Keep is unknown


def test_since_keeps_the_rest_of_the_book_and_the_books_known_to_follow() -> None:
    infos = {"a": BookInfo("A", "S", 1.0), "b": BookInfo("B", "S", 2.0), "c": BookInfo("C")}
    chunks = [Chunk(f"{b}:{ch}:{q}", b, ch, "t") for b in "abc" for ch in (1, 2) for q in (0, 1)]
    anchor = Chunk("a:1:1", "a", 1, "t")
    kept = [c.chunk_id for c in recap_module._since(chunks, anchor, infos)]
    assert kept == ["a:1:1", "a:2:0", "a:2:1", "b:1:0", "b:1:1", "b:2:0", "b:2:1"]


async def test_the_fallback_without_beats_spreads_passages(config) -> None:
    scripted = RecapModels(Recap("book", book="Wanderer Road"))
    answer, events = await ask(config, scripted, question="Summarise Wanderer Road.")
    assert {p.book_id for p in answer.passages} == {"hb3"}
    joined = "\n".join(scripted.prompts("claims"))
    assert "MARK-hb3-1" in joined and "MARK-hb3-2" in joined
    assert "Story beats:" not in joined
    done = next(e for e in events if e["type"] == "step" and e["name"] == "recap"
                and e["state"] == "done")
    assert "passages" in done["detail"]


async def test_another_world_is_never_read(config) -> None:
    scripted = RecapModels(Recap("story"))
    answer, _ = await ask(config, scripted)
    assert "MARK-ob1" not in _seen(scripted)
    assert "ob1" not in {p.book_id for p in answer.passages}


# --- the switch ------------------------------------------------------------------------

def _snapshot(answer, events, scripted) -> dict:
    from lore.query.events import json_safe
    return json_safe({"events": [e for e in events], "calls": scripted.calls,
                      "answer": {"text": answer.text, "mode": answer.mode,
                                 "style": answer.answer_style, "template": answer.template,
                                 "claims": [c.model_dump() for c in answer.claims],
                                 "passages": [p.chunk_id for p in answer.passages]}})


async def test_recap_off_changes_nothing(config, monkeypatch) -> None:
    """Golden: with RECAP off, a reply that carries a recap is answered exactly as
    a reply without one (the ordinary path), and the decomposer is asked exactly
    what it was asked before the feature."""
    monkeypatch.setattr(decompose_module, "RECAP", False)
    plain = RecapModels(None)
    a1, e1 = await ask(config, plain, question="Who is Ren Ashby?")
    flagged = RecapModels(Recap("story"))
    a2, e2 = await ask(config, flagged, question="Who is Ren Ashby?")
    assert _snapshot(a1, e1, plain) == _snapshot(a2, e2, flagged)
    assert a2.template != "recap"
    assert '"recap"' not in flagged.prompts("decompose")[0]


async def test_recap_on_but_no_recap_asked_answers_as_before(config, monkeypatch) -> None:
    on = RecapModels(None)
    a_on, e_on = await ask(config, on, question="Who is Ren Ashby?")
    monkeypatch.setattr(decompose_module, "RECAP", False)
    off = RecapModels(None)
    a_off, e_off = await ask(config, off, question="Who is Ren Ashby?")
    strip = lambda calls: [c for c in calls if c[0] != "decompose"]  # noqa: E731
    on.calls, off.calls = strip(on.calls), strip(off.calls)
    assert _snapshot(a_on, e_on, on) == _snapshot(a_off, e_off, off)


# --- the eval arm and set --------------------------------------------------------------

def test_run_pair_has_a_recap_arm_and_set() -> None:
    import importlib.util
    import json
    from pathlib import Path
    path = Path(__file__).parents[1] / "evals/answer_quality/run_pair.py"
    spec = importlib.util.spec_from_file_location("run_pair_recap", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.parse_arm("recap=off") == {("lore.query.decompose", "RECAP"): False}
    assert module.parse_arm("recap=on") == {("lore.query.decompose", "RECAP"): True}
    questions = json.loads(module.SETS["recap"].read_text())
    assert len(questions) == 8 and len({q["id"] for q in questions}) == 8
    for q in questions:
        assert q["question"] and q["verified"]
        assert q["rubric"]["span"] in ("story", "book", "chapters", "since")
        assert q["rubric"]["covers"] and q["rubric"]["chapters"]


async def test_sections_classified_as_front_or_back_matter_are_left_out(config) -> None:
    conn = open_db(config.sqlite_path)
    conn.execute("CREATE TABLE IF NOT EXISTS sections (book_id TEXT, chapter_seq INTEGER,"
                 " matter TEXT, reason TEXT DEFAULT '', heading TEXT)")
    conn.execute("DELETE FROM sections")
    conn.execute("INSERT INTO sections (book_id, chapter_seq, matter) VALUES ('hb1', 1, 'front')")
    conn.commit()
    conn.close()
    scripted = RecapModels(Recap("book", book="Hollow Gate"))
    answer, _ = await ask(config, scripted, question="Summarise Hollow Gate.")
    assert {p.chapter_seq for p in answer.passages} == {2, 3}
    assert "MARK-hb1-1" not in _seen(scripted)


async def test_the_recap_reaches_the_api_as_a_sectioned_answer(config) -> None:
    from lore.mcp import tools
    scripted = RecapModels(Recap("story"))
    result = await tools.lore_search(config, QUESTION, models=scripted.models(), world=HOLLOW)
    assert result["answer_style"] == "sectioned" and result["template"] == "recap"
    assert [s["group"] for s in result["sections"]][0] == "Hollow Gate"
    assert result["short_answer"]


def test_a_book_is_found_by_title_then_by_series_number_never_by_a_stray_digit() -> None:
    titles = {"a": "Hollow Gate", "b": "Hollow Keep", "c": "Catch-22"}
    infos = {"a": BookInfo("Hollow Gate", "Hollow", 1.0), "b": BookInfo("Hollow Keep", "Hollow", 2.0),
             "c": BookInfo("Catch-22")}
    assert recap_module.resolve_books("Hollow Keep", titles, infos) == ["b"]
    assert recap_module.resolve_books("the second book", titles, infos) == ["b"]
    assert recap_module.resolve_books("2", titles, infos) == ["b"]
    assert recap_module.resolve_books("3", titles, infos) == []
    assert recap_module.resolve_books("keep", titles, infos) == ["b"]


async def test_each_recap_section_stages_its_entities_and_relationships_for_the_graph(config) -> None:
    scripted = RecapModels(Recap("story"))
    position(config, "hb2", 2)
    answer, events = await ask(config, scripted)
    seeds = [e for e in events if e["type"] == "seeds"]
    edges = [e for e in events if e["type"] == "edges"]
    assert seeds, "a recap section stages the entities its beats name"
    assert all(e["entities"] for e in seeds) and {e["part"] for e in edges} <= {e["part"] for e in seeds}
    # Nothing past the reading position reaches the graph.
    for e in edges:
        for row in e["edges"]:
            r = row if isinstance(row, dict) else row.__dict__
            assert (r["book_id"], r["chapter_seq"]) != ("hb2", 3) and r["book_id"] != "hb3"
    evidence = next(e for e in events if e["type"] == "evidence")
    assert evidence["edge_keys"] == list(dict.fromkeys(evidence["edge_keys"]))


def test_stretch_graph_is_empty_without_a_graph_or_linked_beats() -> None:
    chapters = (_chapter("b", 1, 1),)
    stretch = recap_module.Stretch("b", chapters, "Chapter 1", "chapter 1 of B")
    assert recap_module.stretch_graph(None, stretch, [_beat("b", 1, 0)], Scope(), {}) == ([], [])


def test_a_one_chapter_section_is_headed_with_the_chapters_own_name() -> None:
    chapters = [_chapter("b", 5, 20), _chapter("b", 6, 20), _chapter("b", 7, 20)]
    out = recap_module.plan_stretches(chapters, {"b": "B"}, per_section=20,
                                      numbers={("b", 5): 1, ("b", 6): 2, ("b", 7): 3},
                                      names={("b", 5): "Mr. Ana", ("b", 7): "The Gate"})
    assert [s.heading for s in out] == ["Chapter 1 · Mr. Ana", "Chapter 2", "Chapter 3 · The Gate"]
    assert out[0].focus == "chapter 1 of B"  # what the claims step is asked: no name in it
    joined = recap_module.plan_stretches(chapters, {"b": "B"}, per_section=60,
                                         numbers={("b", 5): 1, ("b", 6): 2, ("b", 7): 3},
                                         names={("b", 5): "Mr. Ana"})
    assert [s.heading for s in joined] == ["Chapters 1–3"]


async def test_sections_carry_no_repeated_subtitle_and_stay_in_story_order(config) -> None:
    scripted = RecapModels(Recap("story"))
    position(config, "hb2", 2)
    answer, events = await ask(config, scripted)
    assert all(s.title == "" for s in answer.sections)
    plan = next(e for e in events if e["type"] == "plan")
    assert all(s["title"] == "" for s in plan["sections"])
    done = [e["index"] for e in events if e["type"] == "section" and e["state"] == "done"]
    assert done == sorted(done)  # registered and shown in story order however they were drafted
    starts = [c.claims_start for c in answer.sections]
    assert starts == sorted(starts)


async def test_the_gist_is_told_to_reach_the_end_of_the_stretch(config) -> None:
    scripted = RecapModels(Recap("story"))
    position(config, "hb2", 2)
    answer, _ = await ask(config, scripted)
    last = answer.sections[-1].heading
    short_prompts = [p for p in scripted.prompts("write") if "where things stand at its end" in p]
    assert short_prompts and last in short_prompts[-1]
