"""READINGS: labelled, checked readings. A reading interprets what the cited
facts show together (what actions and choices reveal); it is worded as a
reading, cites at least two facts, is judged by its own test, and is shown
distinctly. With the switch off every prompt, schema and output is what it
was before."""
import json
from pathlib import Path

import pytest

from lore.query import synthesize as synth, write as w
from lore.query.models import Claim, Passage, ProseSentence
from lore.query.synthesize import (
    Faithfulness, Judgement, Synthesis, _build_prompt, check_answer, faithfulness_prompt, synthesize,
)
from lore.query.write import (
    Prose, Sentence, assemble, compose_text, split_items, verify_prose, write_answer, writer_prompt,
)
from tests.test_query_story import BOOKS, CS, PS, TITLES, _claims_prompt_fixture

GOLDEN = json.loads((Path(__file__).parent / "golden_readings_off.json").read_text())
TEXTS = [c.text for c in CS]


def flat(text):
    return " ".join(text.split())


def S(text, claims, kind="fact", new_paragraph=False):
    return Sentence(text=text, claims=claims, kind=kind, new_paragraph=new_paragraph)


def R(text, claims):
    return S(text, claims, kind="reading")


# ---- schema ------------------------------------------------------------------

def test_a_sentence_is_a_fact_unless_it_says_reading():
    assert Sentence(text="a", claims=[1]).kind == "fact"
    assert ProseSentence("a", (1,), (1,)).kind == "fact"
    assert Sentence(text="a", claims=[1], kind="reading").kind == "reading"


def test_the_switch_is_on_by_default():
    assert w.READINGS is True


def test_kind_survives_split_and_assemble():
    items = split_items(Prose(sentences=[S("Rin met a teacher.", [1, 2]), R("This suggests trust.", [1, 2])]), TEXTS)
    assert [i.kind for i in items] == ["fact", "reading"]
    out = assemble(items, 2, [True] * 3, [True, True], [1, 2, 3])
    assert [s.kind for s in out] == ["fact", "reading"]


# ---- splitting: a reading is never split -----------------------------------------

def test_a_reading_item_is_kept_whole_not_split():
    items = split_items(Prose(sentences=[R("This suggests trust. It seems he relied on them.", [1, 2])]), TEXTS)
    assert len(items) == 1 and items[0].kind == "reading" and items[0].claims == [1, 2]


def test_a_fact_item_still_splits_into_facts():
    items = split_items(Prose(sentences=[S("Rin met a teacher. Rin trained with the sword.", [1, 2])]), TEXTS)
    assert [i.kind for i in items] == ["fact", "fact"]


def test_a_reading_of_more_than_one_sentence_is_dropped_by_verify():
    out = verify_prose(Prose(sentences=[S("Rin met a teacher.", [1]),
                                        R("This suggests trust. It seems he relied on them.", [1, 2])]), 3, TEXTS)
    assert [s.kind for s in out] == ["fact"]


# ---- code guards -----------------------------------------------------------------

def _kinds(prose, n=3):
    return [(s.kind, s.text) for s in verify_prose(prose, n, TEXTS)]


def test_a_reading_without_a_hedge_word_is_dropped():
    out = _kinds(Prose(sentences=[S("Rin met a teacher.", [1]), R("Rin admired the teacher.", [1, 2])]))
    assert out == [("fact", "Rin met a teacher.")]


@pytest.mark.parametrize("word", ["suggests", "shows", "seems", "appears", "reflects", "indicates",
                                  "Suggests", "implies", "reveals", "hints"])
def test_each_hedge_word_passes(word):
    text = f"His conduct {word} a growing trust."
    assert _kinds(Prose(sentences=[S("Rin met a teacher.", [1]), R(text, [1, 2])]))[1] == ("reading", text)


def test_a_hedge_inside_another_word_does_not_count():
    # "showroom", "seemly" are not hedges
    assert _kinds(Prose(sentences=[S("A.", [1]), R("The showroom grew.", [1, 2])])) == [("fact", "A.")]


def test_a_reading_citing_fewer_than_two_claims_is_dropped():
    prose = Prose(sentences=[S("A.", [1]), R("It suggests trust.", [1]), R("It seems calm.", [2, 2])])
    assert _kinds(prose) == [("fact", "A.")]


def test_only_the_first_two_readings_are_kept():
    prose = Prose(sentences=[S("A.", [1]), R("It suggests one.", [1, 2]), R("It seems two.", [2, 3]),
                             R("It appears three.", [1, 3])])
    assert [t for _, t in _kinds(prose)] == ["A.", "It suggests one.", "It seems two."]


def test_a_reading_with_an_out_of_range_citation_is_dropped_like_any_sentence():
    assert _kinds(Prose(sentences=[S("A.", [1]), R("It suggests trust.", [1, 9])])) == [("fact", "A.")]


def test_facts_are_not_touched_by_the_reading_guards():
    # no hedge word, one claim: fine for a fact
    assert _kinds(Prose(sentences=[S("Rin admired the teacher.", [1])])) == [("fact", "Rin admired the teacher.")]


def test_with_the_switch_off_verify_prose_ignores_kind(monkeypatch):
    monkeypatch.setattr(w, "READINGS", False)
    out = verify_prose(Prose(sentences=[R("Rin admired the teacher.", [1])]), 3, TEXTS)
    assert len(out) == 1


# ---- assemble fallbacks --------------------------------------------------------------

def test_prose_of_only_readings_falls_back():
    sents = [R("It suggests trust.", [1, 2]), R("It seems calm.", [2, 3])]
    assert assemble(sents, 2, [True] * 3, [True, True], [1, 2, 3]) is None


def test_prose_of_only_readings_left_after_the_check_falls_back():
    sents = [S("A.", [1]), R("It suggests trust.", [1, 2])]
    assert assemble(sents, 2, [True] * 3, [False, True], [1, 2, 3]) is None


def test_an_unsupported_reading_is_removed_like_one_unsupported_sentence():
    sents = [S("A.", [1]), S("B.", [2]), R("It suggests trust.", [1, 2])]
    out = assemble(sents, 3, [True] * 3, [True, True, False], [1, 2, 3])
    assert [s.text for s in out] == ["A.", "B."]


def test_a_reading_counts_as_a_sentence_in_the_fallback_rules():
    # one fact + one reading unsupported: two failures among three sentences
    sents = [S("A.", [1]), S("B.", [2]), R("It suggests trust.", [1, 2])]
    assert assemble(sents, 3, [True] * 3, [True, False, False], [1, 2, 3]) is None
    # readings the guards dropped still count in `written`
    assert assemble([S("A.", [1])], 3, [True] * 3, [True], [1, 2, 3]) is None


def test_a_reading_resting_on_a_dropped_claim_is_not_shown():
    sents = [S("A.", [1]), S("B.", [3]), R("It suggests trust.", [1, 2])]
    out = assemble(sents, 3, [True, False, True], [True, True, True], [1, 2, 3])
    assert [s.text for s in out] == ["A.", "B."]


# ---- output ---------------------------------------------------------------------------

def test_compose_text_shows_a_reading_as_just_the_sentence():
    prose = (ProseSentence("Rin met a teacher.", (1,), (2,)),
             ProseSentence("This suggests trust.", (1, 2), (2, 3), kind="reading"))
    assert compose_text(prose) == "Rin met a teacher. [2] This suggests trust. [2][3]"


def test_the_mcp_result_carries_kind():
    from lore.mcp.shapes import to_search_result
    from lore.query.models import Answer
    answer = Answer(text="x", passages=(), mode="graph", template=None, note=None,
                    claims=(Claim(text="A", passage=1, quote="a b c d"),),
                    prose=(ProseSentence("A.", (1,), (1,)),
                           ProseSentence("It suggests B.", (1, 1), (1,), kind="reading")),
                    answer_style="prose")
    out = to_search_result(answer, {}).model_dump()
    assert [p["kind"] for p in out["prose"]] == ["fact", "reading"]


# ---- the writer prompt ----------------------------------------------------------------

def _writer(**kw):
    return writer_prompt("How did Rin change?", ["How did Rin change?"], CS, PS, TITLES, books=BOOKS)


def test_the_writer_prompt_carries_the_reading_rules():
    p = flat(_writer())
    for needle in ('"reading"', "up to 2", "character", "relationship", "change", "motives", "themes",
                   "actions", "suggests", "seems", "appears", "reflects", "indicates", "at least 2",
                   "never state", "plain fact"):
        assert needle in p, needle
    assert "{readings}" not in p


def test_the_writer_prompt_has_no_reading_rules_when_off(monkeypatch):
    monkeypatch.setattr(w, "READINGS", False)
    assert "reading" not in _writer()


def test_the_reading_rule_only_applies_with_story_order(monkeypatch):
    monkeypatch.setattr(w, "STORY_ORDER", False)
    assert "reading" not in _writer()


async def test_the_writer_is_asked_for_the_reading_schema_only_when_on(monkeypatch):
    seen = []

    async def gen(prompt, schema):
        seen.append(schema)
        return Prose(sentences=[S("A.", [1])])

    await write_answer("q", ["q"], CS, PS, gen, book_titles=TITLES, books=BOOKS)
    monkeypatch.setattr(w, "READINGS", False)
    await write_answer("q", ["q"], CS, PS, gen, book_titles=TITLES, books=BOOKS)
    assert "kind" in seen[0].model_json_schema()["$defs"]["Sentence"]["properties"]
    assert "kind" not in seen[1].model_json_schema()["$defs"]["Sentence"]["properties"]


async def test_the_numbers_of_a_reading_map_back_too():
    async def gen(prompt, schema):
        return Prose(sentences=[S("A.", [1]), R("It suggests trust.", [1, 3])])
    out = await write_answer("q", ["q"], CS, PS, gen, book_titles=TITLES, books=BOOKS)
    assert [s.kind for s in out.sentences] == ["fact", "reading"]
    assert out.sentences[1].claims == [2, 1]


async def test_a_reply_without_kind_is_all_facts():
    class Plain(Prose):
        pass

    async def gen(prompt, schema):
        return schema.model_validate({"sentences": [{"text": "A.", "claims": [1]}]})
    out = await write_answer("q", ["q"], CS, PS, gen, book_titles=TITLES, books=BOOKS)
    assert out.sentences[0].kind == "fact"


async def test_with_the_switch_off_the_writer_cannot_produce_a_reading(monkeypatch):
    monkeypatch.setattr(w, "READINGS", False)

    async def gen(prompt, schema):
        return Prose(sentences=[R("A.", [1])])
    out = await write_answer("q", ["q"], CS, PS, gen, book_titles=TITLES, books=BOOKS)
    assert out.sentences[0].kind == "fact"


# ---- the claims step -------------------------------------------------------------------

def test_the_claims_step_asks_for_what_characters_do_and_choose():
    evidence, books = _claims_prompt_fixture()
    p = _build_prompt("how did it change?", evidence, books=books)
    assert "what characters do, choose and how they behave toward each other" in flat(p)


def test_the_claims_prompt_is_unchanged_when_off(monkeypatch):
    monkeypatch.setattr(w, "READINGS", False)
    evidence, books = _claims_prompt_fixture()
    assert _build_prompt("how did it change?", evidence, books=books) == GOLDEN["claims"]
    assert _build_prompt("how did it change?", evidence, checklist=["a", "b"], part_of="p",
                         books=books) == GOLDEN["claims_checklist"]


# ---- the check -------------------------------------------------------------------------

def _check_prompt(sentences):
    return faithfulness_prompt(CS, PS, TITLES, sentences, books=BOOKS)


def test_each_sentence_shows_its_kind():
    p = _check_prompt([S("A.", [1]), R("It suggests trust.", [1, 2])])
    assert "1. sentence (fact): A." in p
    assert "2. sentence (reading): It suggests trust." in p


def test_readings_get_their_own_test():
    p = flat(_check_prompt([S("A.", [1]), R("It suggests trust.", [1, 2])]))
    assert "a careful reader would agree" in p
    assert "fairly support" in p and "adds no event, name, number or detail" in p
    assert "overreaches beyond the claims, is stated as plain fact, or adds detail" in p
    assert "Judge meaning, not wording." in p  # the fact test is still there


def test_an_answer_of_facts_alone_is_checked_exactly_as_before():
    p = _check_prompt([S("A.", [1])])
    assert "careful reader" not in p and "(fact)" not in p and "1. sentence: A." in p


def test_the_check_prompt_is_unchanged_when_off(monkeypatch):
    monkeypatch.setattr(w, "READINGS", False)
    sents = [S("Rin met a teacher and trained.", [1, 2]), S("Rin trained.", [3])]
    assert faithfulness_prompt(CS, PS, TITLES, books=BOOKS) == GOLDEN["check_plain"]
    assert faithfulness_prompt(CS, PS, TITLES, sents, books=BOOKS, gaps=["x"]) == GOLDEN["check_sentences"]
    # a reading, if one were handed in, is not labelled when off
    assert "reading" not in faithfulness_prompt(CS, PS, TITLES, [R("It suggests.", [1, 2])], books=BOOKS)


# ---- the switch off: byte for byte ----------------------------------------------------------

def test_switch_off_is_byte_identical_to_the_previous_release(monkeypatch):
    """tests/golden_readings_off.json was dumped from e9471a1 (STORY_ORDER on)."""
    monkeypatch.setattr(w, "READINGS", False)
    assert writer_prompt("How did Rin grow?", ["How did Rin grow?", "Who taught Rin?"], CS, PS, TITLES,
                         books=BOOKS) == GOLDEN["writer"]
    assert writer_prompt("Q?", [], CS, PS, None, books=BOOKS) == GOLDEN["writer_one_part"]
    assert json.dumps(w._StoryProse.model_json_schema()) == GOLDEN["schema_prose"]
    assert json.dumps(w._PlainProse.model_json_schema()) == GOLDEN["schema_plain_prose"]
    assert json.dumps(Faithfulness.model_json_schema()) == GOLDEN["schema_faithfulness"]
    assert json.dumps(synth._PlainFaithfulness.model_json_schema()) == GOLDEN["schema_plain_faithfulness"]


# ---- end to end ---------------------------------------------------------------------------------

def _j(n, v):
    return Judgement(claim=n, verdict=v, reason="r")


async def _ask(prose, sentence_verdicts, prompts=None):
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
    from lore.query.models import Evidence
    evidence = Evidence(edges=(), passages=PS[:3], truncated=False)
    return await synthesize("How did Rin change?", evidence, generate, mode="graph", check=check,
                            write=write, book_titles=TITLES, books=BOOKS)


async def test_a_supported_reading_reaches_the_answer_marked():
    prose = Prose(sentences=[S("Rin met a teacher.", [2]), S("Rin trained with the sword.", [3]),
                             R("This suggests a growing commitment.", [2, 3])])
    prompts = []
    a = await _ask(prose, ("supported",) * 3, prompts)
    assert a.answer_style == "prose"
    assert [s.kind for s in a.prose] == ["fact", "fact", "reading"]
    assert "This suggests a growing commitment." in a.text
    assert "(reading)" in prompts[0]


async def test_an_unsupported_reading_is_removed_and_the_facts_stay():
    prose = Prose(sentences=[S("Rin met a teacher.", [2]), S("Rin trained with the sword.", [3]),
                             R("This suggests a growing commitment.", [2, 3])])
    a = await _ask(prose, ("supported", "supported", "not_supported"))
    assert a.answer_style == "prose" and [s.kind for s in a.prose] == ["fact", "fact"]
    assert "commitment" not in a.text


async def test_prose_of_readings_alone_falls_back_to_claims():
    prose = Prose(sentences=[R("This suggests a growing commitment.", [2, 3]),
                             R("It seems he valued the teacher.", [1, 2])])
    a = await _ask(prose, ("supported", "supported"))
    assert a.answer_style == "claims" and a.prose == ()


# ---- general: no book, character or genre words ------------------------------------------------

def test_the_new_prompt_texts_name_no_character_book_or_genre():
    import re
    text = "\n".join((w._READINGS_RULE, synth._ACTIONS_RULE, synth._READINGS_BLOCK))
    banned = (r"\b(holmes|watson|sherlock|small|hope|drebber|stangerson|sholto|ferrier|tonga|"
              r"morstan|lestrade|baker street|mormon|agra|crime|crimes|murder|villain|villains|"
              r"detective|treasure|darcy|bennet|elizabeth|wizard|dragon|magic|kingdom|elf|elves|"
              r"quest|sword)\b")
    assert not re.findall(banned, text.lower())


def test_run_pair_has_a_readings_arm():
    import importlib.util
    path = Path(__file__).parents[1] / "evals/answer_quality/run_pair.py"
    spec = importlib.util.spec_from_file_location("run_pair_readings", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.parse_arm("readings=off") == {("lore.query.write", "READINGS"): False}
    assert module.parse_arm("readings=on") == {("lore.query.write", "READINGS"): True}
