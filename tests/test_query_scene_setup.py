"""SCENE_SETUP (spec order-and-setup, 2026-11-09).

Seen live: "how did Watson meet Holmes" found the scene of the meeting, whose
passage opens "On the day he decided to move ...", but not the passage before
it, which says why he had to move. For an event question, a best-ranked passage
that opens mid-scene brings the passage just before it in its chapter, within
the evidence budget, never across a chapter, the reading position or a world.

The fixture is a made-up story of that shape.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from lore.db.sqlite import open_db
from lore.query import scene
from lore.query import synthesize as synth
from lore.query.models import BookInfo, Evidence, Passage
from lore.query.subgraph import assemble_ranked
from lore.query.synthesize import Synthesis, synthesize
from lore.query.visible import VisibleBooks

SETUP = ("Mara could not pay for her room at the inn on what the guild paid her, and she "
         "made up her mind to leave it and look for cheaper rooms.")
SCENE = ("On the day she decided to move, someone tapped her shoulder at the Lantern Inn; "
         "it was Pell, who took her to meet Corin, who also wanted rooms.")
CHAPTER_1 = [
    ("b:1:0", "The guild hall stood at the end of a long grey street by the river."),
    ("b:1:1", SETUP),
    ("b:1:2", SCENE),
    ("b:1:3", "Corin shook her hand warmly and the two went to see the rooms together."),
    ("b:1:4", "Corin kept a lamp burning late into the night over his maps and charts."),
]
CHAPTER_2 = [
    ("b:2:5", "That evening, Corin showed Mara the rooms on the hill above the harbour."),
    ("b:2:6", "The rooms were bright and airy, with two bedrooms and a sitting room."),
    ("b:2:7", "Then Mara paid the first month's rent to the landlady."),
]
OTHER = [("o:1:0", "A shepherd counted his flock on the far hills."),
         ("o:1:1", "Then he took them down to the river to drink.")]


def _conn(tmp_path: Path):
    conn = open_db(tmp_path / "lore.db")
    conn.execute("INSERT INTO books (id, title, file_path) VALUES ('b', 'The Lantern', 'x')")
    conn.execute("INSERT INTO books (id, title, file_path) VALUES ('o', 'The Shepherd', 'x')")
    for rows, chapter, book in ((CHAPTER_1, 1, "b"), (CHAPTER_2, 2, "b"), (OTHER, 1, "o")):
        for chunk_id, text in rows:
            conn.execute("INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text)"
                         " VALUES (?, ?, ?, 0, ?, ?)",
                         (chunk_id, book, chapter, int(chunk_id.rsplit(":", 1)[1]), text))
    conn.commit()
    return conn


def _extra(*ids):
    return [(c, c.split(":")[0], int(c.split(":")[1])) for c in ids]


def _scores(*ids):
    """The ids most relevant first."""
    return {c: 0.9 - 0.05 * i for i, c in enumerate(ids)}


# --- the switch ------------------------------------------------------------------

def test_the_switch_is_on_by_default():
    assert scene.SCENE_SETUP is True and scene.MAX_SETUP == 2


def test_run_pair_has_an_arm():
    import importlib.util
    path = Path(__file__).parents[1] / "evals/answer_quality/run_pair.py"
    spec = importlib.util.spec_from_file_location("run_pair_scene", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.parse_arm("scene_setup=false") == {("lore.query.scene", "SCENE_SETUP"): False}


# --- what counts --------------------------------------------------------------------

@pytest.mark.parametrize("question", [
    "how did watson meet holmes", "Why did Mara leave the inn?", "When did Corin find the map?",
    "What happened at the Lantern Inn?", "Who killed the miller?", "What led to the fire?",
])
def test_an_event_question(question):
    assert scene.event_question(question)


@pytest.mark.parametrize("question", [
    "Who is Corin?", "What is the Lantern Inn?", "Describe Mara's character traits",
    "List the members of the guild",
])
def test_not_an_event_question(question):
    assert not scene.event_question(question)


@pytest.mark.parametrize("text", [
    SCENE, "That evening, Corin showed Mara the rooms.", "The decision was made at dawn.",
    "He walked on.", "Afterwards the two parted.", "The next morning the ship sailed.",
])
def test_a_passage_opening_mid_flow(text):
    assert scene.mid_flow(text)


@pytest.mark.parametrize("text", [SETUP, "Corin kept a lamp burning late.",
                                  "The guild hall stood at the end of a street."])
def test_a_passage_opening_on_its_own(text):
    assert not scene.mid_flow(text)


# --- the evidence -----------------------------------------------------------------

def test_the_preceding_passage_joins_as_the_scenes_setup(tmp_path):
    conn = _conn(tmp_path)
    ids = ("b:1:2", "b:1:4")
    off = assemble_ranked(conn, [], extra_chunks=_extra(*ids), score_lists=[_scores(*ids)])
    on = assemble_ranked(conn, [], extra_chunks=_extra(*ids), score_lists=[_scores(*ids)],
                         scene_setup=True)
    assert [p.chunk_id for p in off.passages] == ["b:1:2", "b:1:4"] and off.setups == ()
    # numbered in story order, the setup first
    assert [p.chunk_id for p in on.passages] == ["b:1:1", "b:1:2", "b:1:4"]
    assert on.setups == ((1, 2),)
    assert on.shares == ((1, 2, 3),)


def test_the_earliest_of_a_run_brings_its_setup_too(tmp_path):
    conn = _conn(tmp_path)
    ids = ("b:1:4", "b:1:3")  # b:1:3 opens on its own but is the start of a run
    on = assemble_ranked(conn, [], extra_chunks=_extra(*ids), score_lists=[_scores(*ids)],
                         scene_setup=True)
    assert [p.chunk_id for p in on.passages] == ["b:1:2", "b:1:3", "b:1:4"]
    assert on.setups == ((1, 2),)


def test_with_the_switch_off_the_evidence_is_unchanged(tmp_path):
    conn = _conn(tmp_path)
    ids = ("b:1:2", "b:1:4", "b:2:5")
    args = dict(extra_chunks=_extra(*ids), score_lists=[_scores(*ids)])
    assert assemble_ranked(conn, [], **args) == assemble_ranked(conn, [], **args,
                                                                scene_setup=False)


def test_never_across_a_chapter(tmp_path):
    conn = _conn(tmp_path)
    # b:2:5 opens mid-flow ("That evening") but is the first of chapter 2
    on = assemble_ranked(conn, [], extra_chunks=_extra("b:2:5"),
                         score_lists=[_scores("b:2:5")], scene_setup=True)
    assert [p.chunk_id for p in on.passages] == ["b:2:5"] and on.setups == ()


def test_never_past_the_reading_position_or_outside_the_world(tmp_path):
    conn = _conn(tmp_path)
    ids = ("b:2:6", "b:1:2")
    # the reader is at chapter 1: chapter 2 is no evidence and brings nothing
    at_one = VisibleBooks({"b"}, {"b": 1})
    ev = assemble_ranked(conn, [], extra_chunks=_extra(*ids), score_lists=[_scores(*ids)],
                         visible=at_one, scene_setup=True)
    assert [p.chunk_id for p in ev.passages] == ["b:1:1", "b:1:2"]
    # the selection itself never offers a passage the reader may not see
    ranked = [("b:1:2", "b", 1, SCENE)]
    assert scene.setups(conn, ranked, {"b:1:2"}, visible=VisibleBooks({"b"}, {"b": 0})) == []
    assert scene.setups(conn, ranked, {"b:1:2"}, visible=frozenset({"o"})) == []
    assert scene.setups(conn, ranked, {"b:1:2"}, visible=frozenset({"b"}))[0][0][0] == "b:1:1"


def test_the_cap_holds(tmp_path):
    conn = _conn(tmp_path)
    # three passages open mid-flow ("On the day", "Then he", "Then Mara"): the two
    # best ranked bring their setups, the third does not
    ids = ("b:1:2", "o:1:1", "b:2:7")
    ev = assemble_ranked(conn, [], extra_chunks=_extra(*ids), score_lists=[_scores(*ids)],
                         scene_setup=True)
    assert len(ev.setups) == scene.MAX_SETUP == 2
    by_n = {p.n: p.chunk_id for p in ev.passages}
    assert {(by_n[s], by_n[a]) for s, a in ev.setups} == {("b:1:1", "b:1:2"),
                                                          ("o:1:0", "o:1:1")}
    assert "b:2:6" not in by_n.values()


def test_the_budget_holds(tmp_path, monkeypatch):
    monkeypatch.setattr(scene, "SETUP_TOP", 2)  # a small fixture: two best-ranked passages
    conn = _conn(tmp_path)
    ids = tuple(c for c, _ in CHAPTER_1[2:]) + tuple(c for c, _ in CHAPTER_2) + ("o:1:0",)
    texts = {c: t for c, t in CHAPTER_1 + CHAPTER_2 + OTHER}
    budget = sum(len(texts[c]) for c in ids)  # every candidate fits, and no more
    ev = assemble_ranked(conn, [], extra_chunks=_extra(*ids), score_lists=[_scores(*ids)],
                         max_chars=budget, scene_setup=True)
    assert sum(len(p.text) for p in ev.passages) <= budget
    # the setup took the place of the least relevant passage, beyond the best ranked
    assert ev.setups and "b:1:1" in {p.chunk_id for p in ev.passages}
    assert ev.truncated
    # a setup that only fits by cutting a best-ranked passage is left out
    tight = sum(len(texts[c]) for c in ids[:2])
    small = assemble_ranked(conn, [], extra_chunks=_extra(*ids[:2]),
                            score_lists=[_scores(*ids[:2])], max_chars=tight, scene_setup=True)
    assert small.setups == () and [p.chunk_id for p in small.passages] == list(ids[:2])


# --- synthesis --------------------------------------------------------------------------

PS = (Passage(n=1, chunk_id="b:1:1", book_id="b", chapter_seq=1, text=SETUP),
      Passage(n=2, chunk_id="b:1:2", book_id="b", chapter_seq=1, text=SCENE))
TITLES = {"b": "The Lantern"}
BOOKS = {"b": BookInfo("The Lantern")}


def test_the_prompt_says_what_the_setup_is_for():
    plain = synth._build_prompt("How did Mara meet Corin?",
                                Evidence(edges=(), passages=PS, truncated=False), TITLES,
                                books=BOOKS)
    marked = synth._build_prompt("How did Mara meet Corin?",
                                 Evidence(edges=(), passages=PS, truncated=False,
                                          setups=((1, 2),)), TITLES, books=BOOKS)
    note = synth._SETUP_NOTE.format(setup=1, scene=2)
    assert note not in plain and note in marked
    assert marked.replace(note, "") == plain


async def test_synthesis_can_make_a_claim_from_the_setup():
    async def generate(prompt, schema):
        return Synthesis(can_answer=True, claims=[
            {"text": "Mara could not pay for her room at the inn and decided to leave.",
             "passage": 1, "quote": "Mara could not pay for her room at the inn"},
            {"text": "Pell took Mara to meet Corin.", "passage": 2,
             "quote": "it was Pell, who took her to meet Corin"},
            # a claim must still quote the passage it cites
            {"text": "Mara was poor.", "passage": 1, "quote": "Mara had no money at all"}])

    ev = Evidence(edges=(), passages=PS, truncated=False, setups=((1, 2),))
    a = await synthesize("How did Mara meet Corin?", ev, generate, mode="graph",
                         book_titles=TITLES, books=BOOKS)
    assert [c.passage for c in a.claims] == [1, 2]
    assert "could not pay" in a.claims[0].text
