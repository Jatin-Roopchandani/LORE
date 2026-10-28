"""STORY_ORDER: the writer sees facts in story order and may use order words
the positions support; the check judges meaning and accepts order from
positions; gaps name aspects of a question the facts do not cover."""
import pytest

from lore.query import write as write_module
from lore.query.models import BookInfo, Claim, Passage, ProseSentence
from lore.query.synthesize import (
    Faithfulness, Judgement, Synthesis, check_answer, faithfulness_prompt, synthesize,
    validated_gap_verdicts,
)
from lore.query.write import (
    UNANSWERED_LEAD, WRITER_PROMPT, Prose, Sentence, clean_gaps, compose_text, write_answer,
    writer_prompt,
)

# Three passages in two books of one series, and one from a book with no series.
PS = (
    Passage(n=1, chunk_id="b2:5:0", book_id="b2", chapter_seq=5, text="Rin trained with the sword."),
    Passage(n=2, chunk_id="b1:1:0", book_id="b1", chapter_seq=1, text="Rin met the old teacher."),
    Passage(n=3, chunk_id="b1:3:2", book_id="b1", chapter_seq=3, text="Rin learned to read maps."),
    Passage(n=4, chunk_id="x:2:0", book_id="x", chapter_seq=2, text="Rin sailed a small boat."),
)
CS = [
    Claim(text="Rin trained with the sword.", passage=1, quote="Rin trained with the sword"),
    Claim(text="Rin met the old teacher.", passage=2, quote="Rin met the old teacher"),
    Claim(text="Rin learned to read maps.", passage=3, quote="Rin learned to read maps"),
]
BOOKS = {"b1": BookInfo("First Volume", "The Saga", 1.0), "b2": BookInfo("Second Volume", "The Saga", 2.0),
         "x": BookInfo("Standalone")}
TITLES = {k: v.title for k, v in BOOKS.items()}


def S(text, claims, new_paragraph=False):
    return Sentence(text=text, claims=claims, new_paragraph=new_paragraph)


def facts(prompt: str) -> list[str]:
    return [line for line in prompt.splitlines() if line[:1].isdigit() and ". " in line[:4]
            and "quote:" not in line and not line.startswith(("1. Why", "1. How"))]


# ---- the writer prompt ------------------------------------------------------

def test_facts_are_listed_in_story_order_and_numbered_in_that_order():
    prompt = writer_prompt("How did Rin grow?", ["How did Rin grow?"], CS, PS, TITLES, books=BOOKS)
    fact_lines = [l for l in prompt.splitlines() if l.startswith(("1. Rin", "2. Rin", "3. Rin"))]
    assert fact_lines == [
        "1. Rin met the old teacher. (First Volume, book 1 of The Saga, chapter 1)",
        "2. Rin learned to read maps. (First Volume, book 1 of The Saga, chapter 3)",
        "3. Rin trained with the sword. (Second Volume, book 2 of The Saga, chapter 5)",
    ]


def test_a_fact_of_a_book_with_no_series_is_labelled_without_one():
    claim = Claim(text="Rin sailed a small boat.", passage=4, quote="Rin sailed a small boat")
    prompt = writer_prompt("q", ["q"], [claim], PS, TITLES, books=BOOKS)
    assert "(Standalone, chapter 2)" in prompt


def test_the_prompt_says_which_books_have_an_unknown_order():
    claim = Claim(text="Rin sailed a small boat.", passage=4, quote="Rin sailed a small boat")
    prompt = writer_prompt("q", ["q"], [CS[1], claim], PS, TITLES, books=BOOKS)
    assert "Relative story order is unknown between:" in prompt
    assert "Standalone" in prompt and "First Volume" in prompt


def test_no_unknown_order_line_when_all_books_are_ordered():
    prompt = writer_prompt("q", ["q"], CS, PS, TITLES, books=BOOKS)
    assert "unknown between" not in prompt


def test_books_without_rows_still_work_from_titles_alone():
    prompt = writer_prompt("q", ["q"], [CS[0], CS[1]], PS, TITLES)
    assert "Relative story order is unknown between:" in prompt
    assert "(Second Volume, chapter 5)" in prompt and "(First Volume, chapter 1)" in prompt


def test_the_story_prompt_rules():
    p = write_module.STORY_WRITER_PROMPT
    for needle in ("own words", "story order", "at first", "by the end", "in the next book",
                   "unknown", "also", "however", "because", "gaps", "unanswered"):
        assert needle in p, needle
    assert "{order_note}" in p and "{facts}" in p


# ---- numbers map back --------------------------------------------------------

async def test_the_writers_numbers_are_converted_back_to_the_claims():
    # shown order is claims 2, 3, 1; the writer cites shown 1 and 3
    async def gen(prompt, schema):
        return Prose(sentences=[S("Rin met a teacher, and later trained.", [1, 3]),
                                S("Rin learned maps.", [2]),
                                S("Out of range.", [9]),
                                S("Zero.", [0])])
    out = await write_answer("q", ["q"], CS, PS, gen, book_titles=TITLES, books=BOOKS)
    assert [s.claims for s in out.sentences] == [[2, 1], [3], [0], [0]]
    kept = write_module.verify_prose(out, n_claims=3)
    assert [s.text for s in kept] == ["Rin met a teacher, and later trained.", "Rin learned maps."]


async def test_a_claim_with_a_missing_passage_is_listed_last_and_maps_back():
    stray = Claim(text="Rin lost a ring.", passage=99, quote="Rin lost a ring")
    seen = {}
    async def gen(prompt, schema):
        seen["prompt"] = prompt
        return Prose(sentences=[S("A.", [1]), S("B.", [4])])
    out = await write_answer("q", ["q"], [stray, *CS], PS, gen, book_titles=TITLES, books=BOOKS)
    assert "4. Rin lost a ring." in seen["prompt"]
    assert [s.claims for s in out.sentences] == [[3], [1]]


async def test_a_claim_list_already_in_story_order_maps_to_itself():
    ordered = [CS[1], CS[2], CS[0]]
    async def gen(prompt, schema):
        return Prose(sentences=[S("A.", [1, 2, 3])])
    out = await write_answer("q", ["q"], ordered, PS, gen, book_titles=TITLES, books=BOOKS)
    assert out.sentences[0].claims == [1, 2, 3]


async def test_every_displayed_number_maps_to_its_own_claim():
    # exhaustive: whatever the shown number, the fact text it labels is the
    # text of the claim it maps back to
    import re
    shown = {}
    async def gen(prompt, schema):
        for line in prompt.splitlines():
            m = re.match(r"(\d+)\. (Rin [^(]*?)(?: \(|$)", line)
            if m and "quote:" not in line:
                shown[int(m.group(1))] = m.group(2)
        return Prose(sentences=[S(str(k), [k]) for k in range(1, 4)])
    out = await write_answer("q", ["q"], CS, PS, gen, book_titles=TITLES, books=BOOKS)
    for s in out.sentences:
        assert shown[int(s.text)] == CS[s.claims[0] - 1].text


# ---- the switch --------------------------------------------------------------

def test_switch_off_the_prompt_is_the_old_one_byte_for_byte(monkeypatch):
    monkeypatch.setattr(write_module, "STORY_ORDER", False)
    prompt = writer_prompt("Why?", ["Why?"], CS, PS, TITLES, books=BOOKS)
    expected_facts = "\n".join(
        f"{n}. {c.text}{' (' + TITLES[{1: 'b2', 2: 'b1', 3: 'b1'}[c.passage]] + ', chapter ' + str({1: 5, 2: 1, 3: 3}[c.passage]) + ')'}\n   quote: {c.quote}"
        for n, c in enumerate(CS, start=1))
    assert prompt == WRITER_PROMPT.format(question="Why?", parts="1. Why?", facts=expected_facts)
    assert "story order" not in prompt and "gaps" not in prompt


async def test_switch_off_numbers_are_not_remapped_and_the_schema_has_no_gaps(monkeypatch):
    monkeypatch.setattr(write_module, "STORY_ORDER", False)
    seen = {}
    async def gen(prompt, schema):
        seen["schema"] = schema
        return Prose(sentences=[S("A.", [1, 3])], gaps=["x"])
    out = await write_answer("q", ["q"], CS, PS, gen, book_titles=TITLES, books=BOOKS)
    assert out.sentences[0].claims == [1, 3]
    assert "gaps" not in seen["schema"].model_fields and "unanswered" in seen["schema"].model_fields


async def test_switch_on_the_schema_carries_gaps():
    seen = {}
    async def gen(prompt, schema):
        seen["schema"] = schema
        return Prose(sentences=[S("A.", [1])])
    await write_answer("q", ["q"], CS, PS, gen, book_titles=TITLES, books=BOOKS)
    assert seen["schema"] is Prose and "gaps" in Prose.model_fields


def test_prose_gaps_default_empty():
    assert Prose(sentences=[]).gaps == []


# ---- gaps ---------------------------------------------------------------------

def test_clean_gaps_caps_count_and_length_and_strips_citations():
    long = " ".join(f"w{i}" for i in range(30))
    out = clean_gaps(["whether he used these skills later [3]", "  ", long, "a third gap here", "(1, 2)"])
    assert out[0] == "whether he used these skills later"
    assert len(out) == 2 and len(out[1].split()) == 15


def test_clean_gaps_removes_duplicates_and_trailing_punctuation():
    assert clean_gaps(["How it ended.", "how it ended", "Why it began;"]) == ["How it ended", "Why it began"]


def test_clean_gaps_ignores_non_text():
    assert clean_gaps([]) == [] and clean_gaps(["", "[2]"]) == []


def test_compose_text_puts_parts_and_gaps_in_one_closing_line():
    prose = (ProseSentence("A.", (1,), (3,)),)
    text = compose_text(prose, ["Part two?"], ["whether it was used later"])
    assert text == f"A. [3]\n\n{UNANSWERED_LEAD} Part two?; whether it was used later"
    assert compose_text(prose, (), ["only a gap"]).endswith(f"{UNANSWERED_LEAD} only a gap")
    assert compose_text(prose) == "A. [3]"


# ---- the check ------------------------------------------------------------------

def _check_prompt(sentences=None, gaps=None, books=BOOKS, claims=CS):
    return faithfulness_prompt(claims, PS, TITLES, sentences, books=books, gaps=gaps)


def test_check_prompt_gives_every_claim_its_position():
    p = _check_prompt()
    assert "cites passage [2] (First Volume, book 1 of The Saga, chapter 1, passage 1 in the chapter)" in p
    assert "cites passage [1] (Second Volume, book 2 of The Saga, chapter 5, passage 1 in the chapter)" in p
    assert "unknown between" not in p


def test_check_prompt_states_unknown_order():
    claim = Claim(text="Rin sailed a small boat.", passage=4, quote="Rin sailed a small boat")
    p = _check_prompt(claims=[CS[1], claim])
    assert "Relative story order is unknown between:" in p


def test_sentence_block_judges_meaning_and_takes_order_from_positions():
    p = _check_prompt([S("Rin met a teacher and later trained.", [2, 1])])
    block = p[p.index("Sentences:"):]
    assert "meaning" in block and "paraphrase" in block.lower()
    assert "positions" in block
    assert "never make a sentence" in block.lower()
    assert '"not_supported"' in block and "always" in block and "every" in block
    assert "sentence_judgements" in block


def test_switch_off_check_prompt_is_the_old_one(monkeypatch):
    monkeypatch.setattr(write_module, "STORY_ORDER", False)
    p = _check_prompt([S("x", [1])], gaps=["whether more"])
    assert "book 1 of" not in p and "Gaps:" not in p and "unknown between" not in p
    assert "(First Volume, chapter 1)" in p
    assert "together, state everything the sentence says" in p


def test_gaps_block_only_when_there_are_gaps_and_sentences():
    assert "Gaps:" not in _check_prompt([S("x", [1])])
    p = _check_prompt([S("x", [1])], gaps=["whether he used these later", "how it ended"])
    assert "Gaps:" in p and "1. gap: whether he used these later" in p and "2. gap: how it ended" in p
    assert "gap_judgements" in p
    assert "Gaps:" not in _check_prompt(None, gaps=["whether more"])


def _j(n, v):
    return Judgement(claim=n, verdict=v, reason="r")


def test_validated_gap_verdicts():
    ok = Faithfulness(judgements=[], gap_judgements=[_j(1, "supported"), _j(2, "Not Supported")])
    assert validated_gap_verdicts(ok, 2) == [True, False]
    assert validated_gap_verdicts(Faithfulness(judgements=[], gap_judgements=[_j(2, "supported")]), 1) is None
    assert validated_gap_verdicts(Faithfulness(judgements=[], gap_judgements=[_j(1, "maybe")]), 1) is None
    assert validated_gap_verdicts(Faithfulness(judgements=[]), 1) is None


async def test_check_answer_returns_gap_verdicts_and_garbled_ones_are_none():
    async def good(prompt, schema):
        return Faithfulness(judgements=[_j(i, "supported") for i in (1, 2, 3)],
                            sentence_judgements=[_j(1, "supported")],
                            gap_judgements=[_j(1, "not_supported"), _j(2, "supported")])
    out = await check_answer(CS, PS, good, book_titles=TITLES, books=BOOKS,
                             sentences=[S("x", [1])], gaps=["a", "b"])
    assert out.gap_ok == [False, True] and out.sentence_ok == [True]

    async def garbled(prompt, schema):
        return Faithfulness(judgements=[_j(i, "supported") for i in (1, 2, 3)],
                            sentence_judgements=[_j(1, "supported")], gap_judgements=[_j(1, "supported")])
    out = await check_answer(CS, PS, garbled, book_titles=TITLES, books=BOOKS,
                             sentences=[S("x", [1])], gaps=["a", "b"])
    assert out.gap_ok is None and out.sentence_ok == [True] and out.ran


async def test_no_gaps_asked_means_no_gap_verdicts():
    async def check(prompt, schema):
        return Faithfulness(judgements=[_j(i, "supported") for i in (1, 2, 3)],
                            sentence_judgements=[_j(1, "supported")])
    out = await check_answer(CS, PS, check, books=BOOKS, sentences=[S("x", [1])])
    assert out.gap_ok is None


# ---- end to end ------------------------------------------------------------------

EV = write_module  # alias for readability below


def _gen(reply):
    async def generate(prompt, schema):
        return reply
    return generate


def _checker(gap_verdicts, sentence_verdicts=("supported",), prompts=None):
    async def check(prompt, schema):
        if prompts is not None:
            prompts.append((prompt, schema))
        gaps = ([_j(i + 1, v) for i, v in enumerate(gap_verdicts)] if gap_verdicts is not None else [])
        return Faithfulness(judgements=[_j(i, "supported") for i in (1, 2, 3)],
                            sentence_judgements=[_j(i + 1, v) for i, v in enumerate(sentence_verdicts)],
                            gap_judgements=gaps)
    return check


def _evidence():
    from lore.query.models import Evidence
    return Evidence(edges=(), passages=PS[:3], truncated=False)


async def _ask(prose, check, **kw):
    return await synthesize("How did Rin grow?", _evidence(), _gen(Synthesis(claims=CS, can_answer=True)),
                            mode="graph", book_titles=TITLES, books=BOOKS, check=check,
                            write=_gen(prose), **kw)


SENT = [S("Rin met a teacher at first and later trained.", [1, 3])]  # shown 1 and 3


async def test_a_gap_the_facts_do_not_answer_is_shown_in_the_closing_line():
    a = await _ask(Prose(sentences=SENT, gaps=["whether Rin used these skills later"]),
                   _checker(["supported"]))
    assert a.answer_style == "prose"
    assert a.gaps == ("whether Rin used these skills later",)
    assert a.text.endswith(f"{UNANSWERED_LEAD} whether Rin used these skills later")
    # the writer's citations were converted back: shown 1 and 3 are claims 2 and 1
    assert a.prose[0].claims == (2, 1)


async def test_a_gap_the_check_says_the_facts_answer_is_dropped():
    a = await _ask(Prose(sentences=SENT, gaps=["how Rin trained", "whether Rin used these later"]),
                   _checker(["not_supported", "supported"]))
    assert a.gaps == ("whether Rin used these later",)
    assert "how Rin trained" not in a.text


async def test_gaps_the_check_did_not_verify_are_dropped():
    a = await _ask(Prose(sentences=SENT, gaps=["whether more"]), _checker(None))
    assert a.gaps == () and UNANSWERED_LEAD not in a.text and a.answer_style == "prose"


async def test_at_most_two_gaps_and_citations_are_stripped():
    prompts = []
    a = await _ask(Prose(sentences=SENT, gaps=["first gap [1]", "second gap (2)", "third gap"]),
                   _checker(["supported", "supported"], prompts=prompts))
    assert a.gaps == ("first gap", "second gap")
    assert "3. gap" not in prompts[0][0]


async def test_parts_and_gaps_share_one_closing_line():
    from lore.query.models import Evidence
    from lore.query.synthesize import synthesize_parts
    subs = ["How did Rin grow?", "Who taught Rin?"]
    ev = Evidence(edges=(), passages=PS[:3], truncated=False, shares=((1, 2, 3), (1, 2, 3)))
    a = await synthesize_parts(
        "How did Rin grow and who taught Rin?", subs, ev, _gen(Synthesis(claims=CS, can_answer=True)),
        mode="graph", book_titles=TITLES, books=BOOKS, check=_checker(["supported"]),
        write=_gen(Prose(sentences=SENT, unanswered=[2], gaps=["whether he used them"])))
    assert a.unanswered == ("Who taught Rin?",) and a.gaps == ("whether he used them",)
    assert a.text.endswith(f"{UNANSWERED_LEAD} Who taught Rin?; whether he used them")
    assert a.text.count(UNANSWERED_LEAD) == 1


async def test_switch_off_gaps_are_ignored_and_the_old_check_runs(monkeypatch):
    monkeypatch.setattr(write_module, "STORY_ORDER", False)
    prompts = []
    a = await _ask(Prose(sentences=[S("Rin met a teacher.", [2])], gaps=["whether more"]),
                   _checker(["supported"], prompts=prompts))
    assert a.gaps == () and UNANSWERED_LEAD not in a.text
    assert "Gaps:" not in prompts[0][0] and "book 1 of" not in prompts[0][0]


async def test_gaps_do_not_survive_a_fallback_to_claims():
    a = await _ask(Prose(sentences=[S("a.", [1]), S("b.", [2])], gaps=["whether more"]),
                   _checker(["supported"], sentence_verdicts=("not_supported", "not_supported")))
    assert a.answer_style == "claims" and a.gaps == ()


def test_switch_off_is_byte_identical_to_the_previous_release(monkeypatch):
    """tests/golden_story_off.json was dumped from the commit before this
    feature (12e9dbc) for the same inputs."""
    import json
    from pathlib import Path
    monkeypatch.setattr(write_module, "STORY_ORDER", False)
    golden = json.loads((Path(__file__).parent / "golden_story_off.json").read_text())
    ps = (Passage(n=1, chunk_id="b2:5:0", book_id="b2", chapter_seq=5, text="Rin trained with the sword."),
          Passage(n=2, chunk_id="b1:1:0", book_id="b1", chapter_seq=1, text="Rin met the old teacher."),
          Passage(n=3, chunk_id="x:2:0", book_id="x", chapter_seq=2, text="Rin sailed a small boat."))
    cs = [Claim(text="Rin trained with the sword.", passage=1, quote="Rin trained with the sword"),
          Claim(text="Rin met the old teacher.", passage=2, quote="Rin met the old teacher"),
          Claim(text="Rin sailed a small boat.", passage=3, quote="Rin sailed a small boat")]
    titles = {"b1": "First Volume", "b2": "Second Volume", "x": "Standalone"}
    sents = [S("Rin met a teacher and trained.", [1, 2]), S("Rin sailed.", [3])]
    # `books` is passed on purpose: with the switch off it changes nothing
    assert writer_prompt("How did Rin grow?", ["How did Rin grow?", "Who taught Rin?"], cs, ps, titles,
                         books=BOOKS) == golden["writer"]
    assert writer_prompt("Q?", [], cs, ps, None) == golden["writer_one_part"]
    assert faithfulness_prompt(cs, ps, titles, books=BOOKS) == golden["check_plain"]
    assert faithfulness_prompt(cs, ps, titles, sents, books=BOOKS, gaps=["x"]) == golden["check_sentences"]


def test_run_pair_has_a_story_order_arm():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).parents[1] / "evals/answer_quality/run_pair.py"
    spec = importlib.util.spec_from_file_location("run_pair_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.parse_arm("story_order=off") == {("lore.query.write", "STORY_ORDER"): False}
    assert module.parse_arm("story_order=on,prose_answers=1") == {
        ("lore.query.write", "STORY_ORDER"): True, ("lore.query.write", "PROSE_ANSWERS"): True}


def test_story_position_is_not_behind_the_switch(monkeypatch):
    # item 1 (series, index, ordering) works whatever STORY_ORDER says
    from lore.query.position import order_passages
    monkeypatch.setattr(write_module, "STORY_ORDER", False)
    order = order_passages(PS, BOOKS)
    assert [p.n for p in order.ordered] == [2, 3, 1, 4]
