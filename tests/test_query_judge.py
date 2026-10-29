"""JUDGEMENTS: questions that ask for a judgement ("who is the most ...").

Detection is a question-form rule; candidates come from the graph for the
part's books; the claims step looks for what each candidate does, wants,
conceals and changes; the writer may add ONE labelled judgement sentence,
and the check judges it by its own test. With the switch off every prompt,
schema and output is what it was before (tests/golden/judgements_off.json,
dumped from 23602b6).

Every fixture uses invented books and names. No test reaches a model.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lore.query import judge, synthesize as synth, timeline as tl, write as w
from lore.query.models import Evidence, ProseSentence
from lore.query.synthesize import Faithfulness, Judgement, Synthesis, _build_prompt, faithfulness_prompt, synthesize
from lore.query.write import Prose, Sentence, assemble, compose_text, split_items, verify_prose, writer_prompt
from tests.test_query_story import BOOKS, CS, PS, TITLES

GOLDEN = json.loads((Path(__file__).parent / "golden" / "judgements_off.json").read_text())
Q = "Who is the most compelling figure in the Second Volume, except Rin?"
PARTS = ["Who is the most compelling figure in the First Volume, except Rin?",
         "Who is the most compelling figure in the Second Volume, except Rin?"]
EV = Evidence(edges=(), passages=PS, truncated=False)
TEXTS = [c.text for c in CS]


def S(text, claims, kind="fact"):
    return Sentence(text=text, claims=claims, kind=kind)


def J(text, claims):
    return S(text, claims, kind="judgement")


# ---- detection ------------------------------------------------------------------

@pytest.mark.parametrize("question", [
    "Who is the most complex character in Alpha Tale?",
    "which character is the least trustworthy",
    "Who do you think is the bravest of them?",
    "What is the best decision Zed makes?",
    "Who was the cleverest figure in Beta Tale, in your opinion?",
    "Who is the most interesting person in the story except Zed and Yan?",
    "Which character is most tragic?",
    "Who is your favourite character?",
    "who is the worst of the crew",
    "Who deserves the most credit for the voyage?",
    "Rank the crew from the most to the least loyal.",
    "Would you say Zed is the most reliable narrator?",
])
def test_evaluative_questions_are_detected(question):
    assert judge.is_evaluative(question)


@pytest.mark.parametrize("question", [
    "Who is Zed?",
    "What did Zed do in chapter three?",
    "How did Zed change over the story?",
    "Which character appears most often?",
    "Who arrived at the earliest hour?",
    "Who was the priest at the harbour?",
    "Most of the story takes place where?",
    "What happened in the latest chapter?",
    "Who is the oldest member of the crew?",
    "Where is the forest the crew crosses?",
    "Why did Yan leave the harbour?",
    "Who is the most recent visitor?",
    "",
])
def test_plain_questions_are_not_evaluative(question):
    assert not judge.is_evaluative(question)


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


# ---- the claims step -------------------------------------------------------------

def test_the_switch_defaults_on_and_the_rule_is_named():
    assert judge.JUDGEMENTS is True
    assert "what they do, want, conceal and change" in " ".join(synth._JUDGEMENT_RULE.split())


def test_an_evaluative_part_gets_the_claims_rule(monkeypatch):
    prompt = _build_prompt(PARTS[0], EV, TITLES, part_of=Q, books=BOOKS)
    assert synth._JUDGEMENT_RULE in prompt
    plain = _build_prompt("Who is Rin?", EV, TITLES, books=BOOKS)
    assert synth._JUDGEMENT_RULE not in plain


def test_with_the_switch_off_the_claims_prompt_is_what_it_was(monkeypatch):
    monkeypatch.setattr(judge, "JUDGEMENTS", False)
    assert _build_prompt(Q, EV, TITLES, books=BOOKS) == GOLDEN["claims_whole"]
    assert _build_prompt(PARTS[0], EV, TITLES, part_of=Q, books=BOOKS) == GOLDEN["claims_part"]


def test_a_plain_question_prompt_is_the_same_with_the_switch_on_or_off(monkeypatch):
    on = _build_prompt("Who is Rin?", EV, TITLES, part_of="Who is Rin, and why?", books=BOOKS)
    monkeypatch.setattr(judge, "JUDGEMENTS", False)
    assert on == _build_prompt("Who is Rin?", EV, TITLES, part_of="Who is Rin, and why?", books=BOOKS)


# ---- the writer ------------------------------------------------------------------

def test_the_writer_rule_is_added_for_an_evaluative_question_only():
    prompt = writer_prompt(Q, PARTS, CS, PS, TITLES, books=BOOKS)
    assert w._JUDGEMENT_RULE in prompt and '"judgement"' in prompt
    plain = writer_prompt("How did Rin grow?", ["How did Rin grow?"], CS, PS, TITLES, books=BOOKS)
    assert w._JUDGEMENT_RULE not in plain


def test_any_evaluative_part_is_enough_for_the_writer_rule():
    prompt = writer_prompt("Who is Rin and who is best?", ["Who is Rin?", "Who is the best sailor?"],
                           CS, PS, TITLES, books=BOOKS)
    assert w._JUDGEMENT_RULE in prompt


def test_the_writer_rule_needs_readings(monkeypatch):
    monkeypatch.setattr(w, "READINGS", False)
    assert w._JUDGEMENT_RULE not in writer_prompt(Q, PARTS, CS, PS, TITLES, books=BOOKS)


def test_with_the_switch_off_every_prompt_and_schema_is_what_it_was(monkeypatch):
    monkeypatch.setattr(judge, "JUDGEMENTS", False)
    sents = [Sentence(text="Rin met a teacher.", claims=[1, 2]),
             Sentence(text="This suggests commitment.", claims=[2, 3], kind="reading")]
    assert writer_prompt(Q, PARTS, CS, PS, TITLES, books=BOOKS) == GOLDEN["writer"]
    assert writer_prompt(Q, [], CS, PS, TITLES, books=BOOKS) == GOLDEN["writer_one_part"]
    assert faithfulness_prompt(CS, PS, TITLES, books=BOOKS) == GOLDEN["check_plain"]
    assert faithfulness_prompt(CS, PS, TITLES, sents, books=BOOKS, gaps=["x"]) == GOLDEN["check_sentences"]
    assert json.dumps(w.Prose.model_json_schema()) == GOLDEN["schema_prose"]
    assert json.dumps(Faithfulness.model_json_schema()) == GOLDEN["schema_faithfulness"]


def test_the_schemas_are_unchanged_even_with_the_switch_on():
    assert json.dumps(w.Prose.model_json_schema()) == GOLDEN["schema_prose"]
    assert json.dumps(Faithfulness.model_json_schema()) == GOLDEN["schema_faithfulness"]


def test_a_judgement_kind_is_kept_only_when_judgements_are_asked_for(monkeypatch):
    assert w._kind(J("x", [1]), allow_judgement=True) == "judgement"
    assert w._kind(J("x", [1]), allow_judgement=False) == "fact"
    assert w._kind(S("x", [1], "reading"), allow_judgement=False) == "reading"


async def test_write_answer_keeps_a_judgement_for_an_evaluative_question_only():
    prose = Prose(sentences=[S("Rin trained.", [1]), J("Arguably, Rin stands out.", [1, 2])])

    async def generate(prompt, schema):
        return prose
    kept = await w.write_answer(Q, PARTS, CS, PS, generate, book_titles=TITLES, books=BOOKS)
    assert [s.kind for s in kept.sentences] == ["fact", "judgement"]
    plain = await w.write_answer("How did Rin grow?", [], CS, PS, generate, book_titles=TITLES, books=BOOKS)
    assert [s.kind for s in plain.sentences] == ["fact", "fact"]


# ---- the code guards -------------------------------------------------------------

def _verified(items):
    return verify_prose(Prose(sentences=items), 3, TEXTS)


def test_a_judgement_is_never_split():
    items = split_items(Prose(sentences=[J("Arguably, Rin stands out. It is close.", [1, 2])]), TEXTS)
    assert len(items) == 1 and items[0].kind == "judgement"


def test_a_worded_judgement_citing_two_facts_survives():
    out = _verified([S("Rin trained.", [1]), J("A strong case can be made for Rin.", [1, 2])])
    assert [s.kind for s in out] == ["fact", "judgement"]


@pytest.mark.parametrize("text", [
    "Rin is the most complex.",            # plain fact: no judgement wording
    "Rin trained. Rin met a teacher.",     # two sentences
])
def test_an_unworded_or_multi_sentence_judgement_is_dropped(text):
    out = _verified([S("Rin trained.", [1]), J(text, [1, 2])])
    assert [s.kind for s in out] == ["fact"]


def test_a_judgement_on_one_fact_is_dropped():
    out = _verified([S("Rin trained.", [1]), J("Arguably, Rin stands out.", [1, 1])])
    assert [s.kind for s in out] == ["fact"]


def test_only_one_judgement_is_kept():
    out = _verified([S("Rin trained.", [1]), J("Arguably, Rin stands out.", [1, 2]),
                     J("Of these, Rin stands out most.", [2, 3])])
    assert [s.kind for s in out].count("judgement") == 1
    assert out[1].text == "Arguably, Rin stands out."


def test_judgement_wording_variants_are_accepted():
    for text in ("Of these, Rin stands out because of the training.",
                 "One could argue that Rin is the strongest case.",
                 "Rin seems the strongest case.",
                 "On balance, the case for Rin is strongest."):
        assert [s.kind for s in _verified([S("Rin trained.", [1]), J(text, [1, 2])])] == ["fact", "judgement"], text


def test_prose_of_judgement_and_readings_alone_falls_back():
    items = [J("Arguably, Rin stands out.", [1, 2])]
    assert assemble(items, 1, [True] * 3, [True], [1, 2, 3]) is None
    mixed = [S("Rin trained.", [1]), J("Arguably, Rin stands out.", [1, 2])]
    out = assemble(mixed, 2, [True] * 3, [True, True], [1, 2, 3])
    assert [s.kind for s in out] == ["fact", "judgement"]


def test_kind_judgement_reaches_the_prose_sentence_and_the_text():
    out = assemble([S("Rin trained.", [1]), J("Arguably, Rin stands out.", [1, 2])], 2, [True] * 3,
                   [True, True], [1, 2, 3])
    text = compose_text(out)
    assert "Arguably, Rin stands out. [1][2]" in text
    assert isinstance(out[1], ProseSentence) and out[1].kind == "judgement"


# ---- the check -------------------------------------------------------------------

def test_the_check_labels_and_tests_a_judgement():
    prompt = faithfulness_prompt(CS, PS, TITLES, [S("Rin trained.", [1]), J("Arguably, Rin stands out.", [1, 2])],
                                 books=BOOKS)
    assert "(judgement)" in prompt and synth._JUDGEMENTS_BLOCK.split("{")[0].strip() in prompt
    assert "(reading)" not in prompt


def test_the_check_prompt_of_facts_alone_is_unchanged():
    facts = [S("Rin trained.", [1])]
    assert "judgement" not in faithfulness_prompt(CS, PS, TITLES, facts, books=BOOKS).lower().replace(
        "judgements", "")


def test_the_check_gives_the_reading_test_only_with_a_reading():
    prompt = faithfulness_prompt(CS, PS, TITLES, [J("Arguably, Rin stands out.", [1, 2])], books=BOOKS)
    assert "interprets what its cited claims show" not in prompt


# ---- end to end through synthesize -----------------------------------------------

def _j(n, v):
    return Judgement(claim=n, verdict=v, reason="r")


async def _ask(question, prose, sentence_verdicts, prompts=None):
    async def generate(prompt, schema):
        return Synthesis(can_answer=True, claims=[
            {"text": c.text, "passage": c.passage, "quote": c.quote} for c in CS])

    async def write(prompt, schema):
        return prose

    async def check(prompt, schema):
        if prompts is not None:
            prompts.append(prompt)
        return Faithfulness(judgements=[_j(i, "supported") for i in (1, 2, 3)],
                            sentence_judgements=[_j(i + 1, v) for i, v in enumerate(sentence_verdicts)])
    evidence = Evidence(edges=(), passages=PS[:3], truncated=False)
    return await synthesize(question, evidence, generate, mode="graph", check=check, write=write,
                            book_titles=TITLES, books=BOOKS)


async def test_a_supported_judgement_reaches_the_answer_marked():
    prose = Prose(sentences=[S("Rin met a teacher.", [2]), S("Rin trained with the sword.", [3]),
                             J("Arguably, Rin's path stands out.", [2, 3])])
    prompts = []
    a = await _ask("Who is the most dedicated figure?", prose, ("supported",) * 3, prompts)
    assert a.answer_style == "prose"
    assert [s.kind for s in a.prose] == ["fact", "fact", "judgement"]
    assert "Arguably, Rin's path stands out." in a.text
    assert "(judgement)" in prompts[0]


async def test_an_unsupported_judgement_is_removed_and_the_facts_stay():
    prose = Prose(sentences=[S("Rin met a teacher.", [2]), S("Rin trained with the sword.", [3]),
                             J("Arguably, Rin's path stands out.", [2, 3])])
    a = await _ask("Who is the most dedicated figure?", prose, ("supported", "supported", "not_supported"))
    assert [s.kind for s in a.prose] == ["fact", "fact"]


async def test_a_judgement_kind_on_a_plain_question_is_just_a_fact_and_is_checked_as_one():
    prose = Prose(sentences=[S("Rin met a teacher.", [2]), S("Rin trained with the sword.", [3]),
                             J("Arguably, Rin's path stands out.", [2, 3])])
    prompts = []
    a = await _ask("How did Rin change?", prose, ("supported", "supported", "not_supported"), prompts)
    assert [s.kind for s in a.prose] == ["fact", "fact"]
    assert "(judgement)" not in prompts[0]


# ---- through the agent -----------------------------------------------------------

from lore.extract.embed import QUERY_PREFIX  # noqa: E402
from lore.query.agent import answer_question  # noqa: E402
from lore.query.select import TemplateChoice  # noqa: E402
from tests.test_query_agent import A, Q as QV, VectorLance, _scripted  # noqa: E402

EVAL_Q = "Who is the most complex character in Alpha Tale, except Zed Quill?"


async def _agent(tmp_path, *, question=EVAL_Q):
    from lore.beats.store import Beat, beat_id, save_beats
    k = _graph(tmp_path)
    conn = _beat_conn(tmp_path)
    # Alpha Tale has six chapters of text; the beats of _beat_conn (chapters 1-10) cover them.
    for ch in range(1, 11):
        conn.execute("INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text)"
                     " VALUES (?, 'ba', ?, 0, 0, ?)", (f"ba:{ch}:0", ch, f"MARK{ch} the crew sailed on. " * 5))
    # Zed has beats too: they must never reach the answer.
    for ch in (1, 5, 9):
        quote = f"zed quote {ch}"
        save_beats(conn, "ba", ch, 2, [Beat(id=beat_id("ba", ch, ("Zed Quill",), quote), book_id="ba",
                                            chapter_seq=ch, chunk_id=f"ba:{ch}:0", entities=("Zed Quill",),
                                            kind="action", text=f"Zed Quill acts in {ch}.", quote=quote)])
    conn.execute("UPDATE beat_entities SET entity_id='ez' WHERE name='Zed Quill'")
    conn.commit()
    vectors = {f"ba:{ch}:0": [0.9, 0.01 * ch, 0.0] for ch in range(1, 11)}
    row = {"chunk_id": "ba:1:0", "entity_ids": ["ev"], "_distance": 0.1, "book_id": "ba",
           "chapter_seq": 1, "text": ""}
    lance = VectorLance({tuple(QV): [row]}, vectors)
    models, calls = _scripted({QUERY_PREFIX + question: QV}, [question],
                              [TemplateChoice(template="neighbours", entities=[1])],
                              Synthesis(claims=[], can_answer=False))
    answer = await answer_question(conn, k, lance, question, models)
    conn.close()
    k.close()
    return answer, calls


async def test_an_evaluative_part_starts_from_the_graphs_candidates(tmp_path):
    answer, calls = await _agent(tmp_path)
    ids = [s.id for s in answer.seeds]
    assert ids[:2] == ["ey", "ew"] and "ez" not in ids  # Yan, Wex; Zed is excluded
    prompt = calls["synthesize"][0]
    assert synth._JUDGEMENT_RULE in prompt
    # Beats of the candidates (a few each, spread), and none of the excluded person's.
    assert "Yan acts in" in prompt and "Wex acts in" in prompt and "Zed Quill acts" not in prompt
    for subject in ("Yan", "Wex"):
        assert sum(f"{subject} acts in" in line for line in prompt.splitlines()) == tl.BEATS_PER_CANDIDATE


async def test_with_the_switch_off_the_part_is_seeded_as_before(tmp_path, monkeypatch):
    monkeypatch.setattr(judge, "JUDGEMENTS", False)
    answer, calls = await _agent(tmp_path)
    assert [s.id for s in answer.seeds][0] != "ey"
    assert synth._JUDGEMENT_RULE not in calls["synthesize"][0]


async def test_a_plain_question_is_seeded_as_before(tmp_path):
    answer, calls = await _agent(tmp_path, question="Who is Vo Pell in Alpha Tale?")
    assert synth._JUDGEMENT_RULE not in calls["synthesize"][0]
    assert not any(s.id == "ey" for s in answer.seeds[:1])


# ---- the run_pair arm --------------------------------------------------------------

def test_there_is_a_judgements_arm():
    import sys
    sys.path.insert(0, str(Path(__file__).parents[1] / "evals" / "answer_quality"))
    import run_pair
    assert run_pair.ARM_SETTINGS["judgements"][:2] == ("lore.query.judge", "JUDGEMENTS")
    assert run_pair.parse_arm("judgements=false") == {("lore.query.judge", "JUDGEMENTS"): False}


def test_the_code_and_prompts_name_no_character_book_or_genre():
    """Detection and candidate code must hold for any book a user uploads."""
    import re
    source = (Path(__file__).parents[1] / "lore" / "query" / "judge.py").read_text().lower()
    banned = r"\b(holmes|watson|sherlock|small|hope|drebber|darcy|bennet|wizard|dragon|magic|kingdom|" \
             r"elf|elves|quest|sword|detective|murder|crime|villain|treasure|mormon)\b"
    assert not re.findall(banned, source)
    text = "\n".join((synth._JUDGEMENT_RULE, synth._JUDGEMENTS_BLOCK, w._JUDGEMENT_RULE)).lower()
    assert not re.findall(banned, text)
