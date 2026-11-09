"""ORPHAN_REPAIR, READER_PROSE and SUMMARY_CHECK (spec orphan-repair, 2026-11-08).

When the check removes a sentence, the next kept one can open with a pronoun
or connective whose meaning was in the removed one ("He ran toward ..."). Code
finds it, the writer gets one repair round over the claims the check kept, the
repair is checked once, and a section that still has an orphan falls back to
its claim list. A section whose opening the check removed gets a new,
supported introduction the same way. The writer is told to write for a reader
and never to open with "The facts show"; the check judges a summary sentence
against the union of its claims.

The fixture is a made-up story with the shape of the failure seen on a real
deep answer: an opening summary and the sentence after it removed, then
"He ran ..."."""
import re
from pathlib import Path

import pytest

from lore.query import synthesize as synth
from lore.query import write as w
from lore.query.models import BookInfo, Claim, Evidence, Passage, SectionSpec
from lore.query.synthesize import Synthesis, faithfulness_prompt, synthesize
from lore.query.write import Prose, Sentence, leans_back, opener_cut, orphans, writer_prompt

TEXTS = [
    "At the farm, Oren leapt through a front window.",
    "Oren ran toward the back of the house, ignoring the creatures behind him.",
    "Oren shouted to Ash to run and hide.",
    "Raiders burned half the village.",
]
# One book, one chapter per claim: story order is claim order, so the numbers a
# writer cites are the claims' own numbers (and, in a repair over claims 2-4,
# 1-3 stand for 2-4).
PS = tuple(Passage(n=i, chunk_id=f"b:{i}:0", book_id="b", chapter_seq=i, text=t)
           for i, t in enumerate(TEXTS, start=1))
CS = [Claim(text=t, passage=i, quote=t.rstrip(".")) for i, t in enumerate(TEXTS, start=1)]
BOOKS = {"b": BookInfo("The Chronicle")}
TITLES = {"b": "The Chronicle"}
EVIDENCE = Evidence(edges=(), passages=PS, truncated=False)
SECTION = SectionSpec(part_of="What happens in the valley?", max_claims=12, length="section",
                      focus="The raid")


def S(text, claims, new_paragraph=False, kind="fact"):
    return Sentence(text=text, claims=claims, new_paragraph=new_paragraph, kind=kind)


# The first draft, as the writer gave it on the real answer: a meta opener citing
# a claim the check cuts, an embellished sentence, then "He ran ...".
OPENER = "The facts show a raid on the farm and then on the village."
EMBELLISHED = "At the farm, Oren leapt through a window while creatures fought him inside."
HE_RAN = "He ran toward the back of the house, ignoring the creatures behind him."
FIRST = Prose(sentences=[
    S(OPENER, [1, 4], True),
    S(EMBELLISHED, [1], True),
    S(HE_RAN, [2]),
    S("Oren shouted to Ash to run and hide.", [3]),
    S("Raiders burned half the village.", [4], True)])
# The repair, citing the repair prompt's numbers (claims 2, 3, 4 shown as 1, 2, 3).
REPAIRED = Prose(sentences=[
    S("Oren fled toward the back of his house, ignoring the creatures behind him.", [1], True),
    S("Oren shouted to Ash to run and hide.", [2]),
    S("Raiders burned half the village.", [3], True)])


def unsupported_if(*needles):
    return lambda text: "not_supported" if any(n in text for n in needles) else "supported"


class Models:
    """Fake claims step, writer and check. `drafts`: the writer's replies in
    turn (the last repeats). The check judges claims with `claim_verdict(text)`
    and sentences with `sentence_verdict(text)`; every reason is "why: <text>"."""

    def __init__(self, drafts, *, claim_verdict=unsupported_if("leapt"),
                 sentence_verdict=unsupported_if("while creatures", "The facts show"),
                 claims=CS):
        self.drafts = list(drafts)
        self.claim_verdict, self.sentence_verdict = claim_verdict, sentence_verdict
        self.claims = claims
        self.write_prompts: list[str] = []
        self.check_prompts: list[str] = []
        self.log: list[str] = []

    async def generate(self, prompt, schema):
        return Synthesis(can_answer=True, claims=[
            {"text": c.text, "passage": c.passage, "quote": c.quote} for c in self.claims])

    async def write(self, prompt, schema):
        self.write_prompts.append(prompt)
        self.log.append("write")
        draft = self.drafts[min(len(self.write_prompts), len(self.drafts)) - 1]
        if isinstance(draft, Exception):
            raise draft
        return draft

    async def check(self, prompt, schema):
        self.check_prompts.append(prompt)
        self.log.append("check")
        claims = re.findall(r"^\d+\. claim: (.*)$", prompt, re.M)
        texts = re.findall(r"^\d+\. sentence(?: \(\w+\))?: (.*)$", prompt, re.M)
        return schema.model_validate({
            "judgements": [{"claim": i, "verdict": self.claim_verdict(t), "reason": "r",
                            "restated_by": 0} for i, t in enumerate(claims, start=1)],
            "sentence_judgements": [
                {"claim": i, "verdict": self.sentence_verdict(t), "reason": f"why: {t[:20]}",
                 "inference_in_fact": False} for i, t in enumerate(texts, start=1)]})

    async def section(self, spend_ok=None, section=SECTION):
        return await synthesize("The raid", EVIDENCE, self.generate, mode="graph", check=self.check,
                                write=self.write, book_titles=TITLES, books=BOOKS, section=section,
                                spend_ok=spend_ok)


def opens_with_context_word(answer) -> bool:
    """Whether the prose, or any paragraph of it, opens leaning on nothing shown."""
    starts = [s for i, s in enumerate(answer.prose) if i == 0 or s.new_paragraph]
    return any(leans_back(s.text) for s in starts)


# ---- switches and arms --------------------------------------------------------------------

def test_the_switches_are_on_by_default():
    assert w.ORPHAN_REPAIR is True and w.READER_PROSE is True and synth.SUMMARY_CHECK is True


def test_run_pair_has_an_arm_for_each():
    import importlib.util
    path = Path(__file__).parents[1] / "evals/answer_quality/run_pair.py"
    spec = importlib.util.spec_from_file_location("run_pair_orphans", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.parse_arm("orphan_repair=false") == {("lore.query.write", "ORPHAN_REPAIR"): False}
    assert module.parse_arm("reader_prose=off") == {("lore.query.write", "READER_PROSE"): False}
    assert module.parse_arm("summary_check=true") == {("lore.query.synthesize", "SUMMARY_CHECK"): True}


# ---- detection ----------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "He ran toward the back.", "She says she studies the old stories.", "They left at dawn.",
    "It burned for a day.", "His sword was drawn.", "Her answer came late.", "Their house stood.",
    "Its walls fell.", "This village was spared.", "That night the raiders came.",
    "These men were sought.", "Those who stayed hid.", "Then the raiders struck.",
    "Later, the mayor spoke.", "Afterwards the fires died.", "Also, the bridge fell.",
    "Still, nobody left.", "But the raid failed.", "However, the forge burned.",
    "Meanwhile the elders met.", "There, Oren found the key.", "“He is gone,” Ash said.",
])
def test_a_sentence_leaning_on_an_earlier_one_is_detected(text):
    assert leans_back(text)


@pytest.mark.parametrize("text", [
    "Oren ran toward the back.", "The raiders burned half the village.",
    "Moiraine says the blood is strong.", "There was no killing in the valley.",
    "There are three villages.", "Hearth fires burned.", "Thenard rode west.",
    "Heather grows on the hills.", "Italy is far away.", "",
])
def test_a_sentence_that_stands_alone_is_not(text):
    assert not leans_back(text)


def test_orphans_after_a_cut_at_an_opening_and_at_a_paragraph():
    sents = [S("Oren leapt.", [1]), S("He ran.", [2]), S("He shouted.", [3]),
             S("Ash hid.", [4], True), S("She waited.", [4], True)]
    # the first is removed: "He ran" is the first kept and follows a cut
    assert orphans(sents, [False, True, True, True, True]) == [1, 4]
    # nothing removed: only the paragraph that opens with a pronoun
    assert orphans(sents, [True] * 5) == [4]
    # a removed middle sentence, followed by a sentence that names its subject
    assert orphans(sents[:4], [True, True, False, True]) == []
    assert opener_cut([False, True]) and not opener_cut([True, False]) and not opener_cut([False])


# ---- the repair round: the exact failure ----------------------------------------------------

async def test_the_two_rivers_shape_is_repaired_and_he_never_opens_the_section():
    m = Models([FIRST, REPAIRED])
    a = await m.section()
    assert len(m.write_prompts) == 2 and len(m.check_prompts) == 2
    repair = m.write_prompts[1]
    # only the claims the check kept reach the writer
    assert "leapt" not in repair.split("Facts:")[1].split(w.REPAIR_LEAD)[0]
    assert "1. Oren ran toward the back of the house" in repair
    # the shown draft, the orphan marked by its number, the lost opening, and the
    # removed sentences with the check's reasons
    assert w.REPAIR_LEAD in repair
    assert "1. He ran toward the back of the house" in repair
    assert "Draft sentences 1 begin with a word" in repair
    assert "The draft's opening sentence was removed" in repair
    assert f"- {EMBELLISHED} (why: At the farm, Oren le)" in repair
    assert f"- {OPENER}" in repair
    # the repair is checked once, over the kept claims only
    assert len(re.findall(r"^\d+\. claim: ", m.check_prompts[1], re.M)) == 3
    assert a.answer_style == "prose"
    assert not opens_with_context_word(a)
    assert a.prose[0].text.startswith("Oren")
    assert all(not s.text.startswith("He ") for s in a.prose)
    # numbering: the answer's claims are the survivors, and the prose cites them all
    assert [c.text for c in a.claims] == TEXTS[1:]
    assert {k for s in a.prose for k in s.claims} == {1, 2, 3}


async def test_a_repair_that_still_leaves_an_orphan_falls_back_to_the_claim_list(monkeypatch):
    monkeypatch.setattr(w, "GENTLE_FALLBACK", False)  # pinned before intro-reliability
    monkeypatch.setattr(w, "INTRO_REPAIR", False)
    still = Prose(sentences=[S("Raiders struck the farm at night.", [1], True),
                             S(HE_RAN, [1]), S("Oren shouted to Ash to run and hide.", [2]),
                             S("Raiders burned half the village.", [3], True)])
    m = Models([FIRST, still], sentence_verdict=unsupported_if(
        "while creatures", "The facts show", "at night"))
    a = await m.section()
    assert len(m.write_prompts) == 2 and len(m.check_prompts) == 2
    assert a.answer_style == "claims" and a.prose == ()
    assert "He ran" not in a.text
    assert synth.PROSE_FALLBACK_NOTE in (a.note or "")
    assert "Oren ran toward the back of the house" in a.text  # each claim on its own, name first


async def test_the_fallback_is_for_that_section_only(monkeypatch):
    monkeypatch.setattr(w, "GENTLE_FALLBACK", False)  # pinned before intro-reliability
    monkeypatch.setattr(w, "INTRO_REPAIR", False)
    broken = Models([FIRST, Exception("the writer failed")])
    clean = Models([Prose(sentences=[
        S("Oren ran toward the back of the house, ignoring the creatures behind him.", [2], True),
        S("Oren shouted to Ash to run and hide.", [3]),
        S("Raiders burned half the village.", [4], True)])])
    first, second = await broken.section(), await clean.section()
    assert first.answer_style == "claims"
    assert second.answer_style == "prose" and len(clean.write_prompts) == 1


async def test_a_section_with_no_orphan_makes_no_extra_call():
    m = Models([Prose(sentences=[
        S("Oren's farm and then the village were raided.", [2, 4], True),
        S("Oren ran toward the back of the house, ignoring the creatures behind him.", [2], True),
        S("He shouted to Ash to run and hide.", [3]),
        S("Raiders burned half the village.", [4], True)])])
    a = await m.section()
    assert len(m.write_prompts) == 1 and len(m.check_prompts) == 1
    assert a.answer_style == "prose" and len(a.prose) == 4


async def test_a_removed_sentence_followed_by_a_named_one_makes_no_repair():
    m = Models([Prose(sentences=[
        S("Oren's farm was raided.", [2], True),
        S("Oren ran toward the back of the house, ignoring the creatures behind him.", [2]),
        S("Oren shouted to Ash to run and hide.", [3]),
        S("At night the shouting woke the whole valley.", [3]),
        S("Oren saw raiders burn half the village.", [4])])],
        sentence_verdict=unsupported_if("At night"))
    a = await m.section()
    assert len(m.write_prompts) == 1 and len(m.check_prompts) == 1
    assert [s.text for s in a.prose][-1] == "Oren saw raiders burn half the village."


async def test_the_spend_check_is_asked_before_the_repair_call():
    m = Models([FIRST, REPAIRED])

    def spend_ok():
        m.log.append("spend_ok")
        return True
    a = await m.section(spend_ok=spend_ok)
    assert m.log == ["write", "check", "spend_ok", "write", "check"]
    assert a.answer_style == "prose"


async def test_no_room_to_spend_means_no_repair_and_no_orphan_shown(monkeypatch):
    monkeypatch.setattr(w, "GENTLE_FALLBACK", False)  # pinned before intro-reliability
    m = Models([FIRST, REPAIRED])
    a = await m.section(spend_ok=lambda: False)
    assert len(m.write_prompts) == 1 and len(m.check_prompts) == 1
    assert a.answer_style == "claims" and "He ran" not in a.text


async def test_switch_off_shows_the_orphan_as_before(monkeypatch):
    monkeypatch.setattr(w, "ORPHAN_REPAIR", False)
    m = Models([FIRST, REPAIRED])
    a = await m.section()
    assert len(m.write_prompts) == 1
    assert a.prose[0].text == HE_RAN


# ---- the introduction ---------------------------------------------------------------------

async def test_a_rejected_opener_triggers_a_repair_that_writes_a_named_one():
    first = Prose(sentences=[
        S("The facts portray the valley as a place of endless war.", [2, 3, 4], True),
        S("Oren ran toward the back of the house, ignoring the creatures behind him.", [2]),
        S("Oren shouted to Ash to run and hide.", [3]),
        S("Raiders burned half the village.", [4], True)])
    again = Prose(sentences=[
        S("Oren's household was caught in a raid that ended with half the village burned.",
          [2, 3, 4], True),
        S("Oren ran toward the back of the house, ignoring the creatures behind him.", [2]),
        S("Oren shouted to Ash to run and hide.", [3]),
        S("Raiders burned half the village.", [4], True)])
    m = Models([first, again], claims=CS[1:], claim_verdict=lambda t: "supported",
               sentence_verdict=unsupported_if("endless war"))
    # the claims step keeps claims 2-4 only, so the writer's numbers are 1-3
    first.sentences[:] = [s.model_copy(update={"claims": [k - 1 for k in s.claims]})
                          for s in first.sentences]
    again.sentences[:] = [s.model_copy(update={"claims": [k - 1 for k in s.claims]})
                          for s in again.sentences]
    a = await m.section()
    assert len(m.write_prompts) == 2
    assert "The draft's opening sentence was removed" in m.write_prompts[1]
    assert "begin with a word" not in m.write_prompts[1]  # no orphan, only the opening
    assert a.prose[0].text.startswith("Oren's household")


async def test_an_opener_repair_that_fails_leaves_the_first_draft(monkeypatch):
    monkeypatch.setattr(w, "INTRO_REPAIR", False)  # pinned before intro-reliability
    first = Prose(sentences=[
        S("The facts portray the valley as a place of endless war.", [1, 2, 3], True),
        S("Oren ran toward the back of the house, ignoring the creatures behind him.", [1]),
        S("Oren shouted to Ash to run and hide.", [2]),
        S("Raiders burned half the village.", [3], True)])
    m = Models([first, Exception("no")], claims=CS[1:], claim_verdict=lambda t: "supported",
               sentence_verdict=unsupported_if("endless war"))
    a = await m.section()
    assert len(m.write_prompts) == 2
    assert a.answer_style == "prose" and a.prose[0].text.startswith("Oren ran")


async def test_a_recap_section_opening_with_its_first_event_is_left_alone():
    first = Prose(sentences=[
        S("The facts portray the valley as a place of endless war.", [1, 2, 3], True),
        S("Oren ran toward the back of the house, ignoring the creatures behind him.", [1]),
        S("Oren shouted to Ash to run and hide.", [2]),
        S("Raiders burned half the village.", [3], True)])
    m = Models([first], claims=CS[1:], claim_verdict=lambda t: "supported",
               sentence_verdict=unsupported_if("endless war"))
    recap = SectionSpec(part_of="What happens?", max_claims=12, length="recap", focus="ch 2-4")
    await m.section(section=recap)
    assert len(m.write_prompts) == 1


# ---- prose answers and the deep paths -------------------------------------------------------

async def test_a_prose_answer_is_repaired_too(monkeypatch):
    monkeypatch.setattr(w, "PROSE_FACTUAL", True)
    m = Models([FIRST, REPAIRED])
    a = await synthesize("What happened at the farm?", EVIDENCE, m.generate, mode="graph",
                         check=m.check, write=m.write, book_titles=TITLES, books=BOOKS)
    assert len(m.write_prompts) == 2
    assert a.answer_style == "prose" and not opens_with_context_word(a)


async def test_a_lens_section_of_a_deep_answer_is_repaired():
    from types import SimpleNamespace
    from lore.query.deep import cited_prose
    m = Models([FIRST, REPAIRED])
    models = SimpleNamespace(write=m.write, check=m.check, spend_ok=None)
    got = await cited_prose("The raid", ["The raid"], CS, PS, models, length="lens",
                            book_titles=TITLES, books=BOOKS)
    assert len(m.write_prompts) == 2 and got
    assert not leans_back(got[0].text)
    # numbered into the claims given: the survivors 2-4
    assert {k for s in got for k in s.claims} == {2, 3, 4}


async def test_a_lens_section_that_cannot_be_repaired_gives_no_prose(monkeypatch):
    monkeypatch.setattr(w, "GENTLE_FALLBACK", False)  # pinned before intro-reliability
    from types import SimpleNamespace
    from lore.query.deep import cited_prose
    m = Models([FIRST, REPAIRED])
    models = SimpleNamespace(write=m.write, check=m.check, spend_ok=lambda: False)
    got = await cited_prose("The raid", ["The raid"], CS, PS, models, length="lens",
                            book_titles=TITLES, books=BOOKS)
    assert got is None and len(m.write_prompts) == 1


def test_a_deep_answer_bounds_the_repair_by_its_request_budget():
    from lore.query import deep
    spend = deep.Spend(n=deep.DEEP_MAX_REQUESTS - 2)
    assert deep.repair_budget(spend, None, 0)()
    assert not deep.repair_budget(spend, None, 1)()
    assert not deep.repair_budget(deep.Spend(), lambda: False, 0)()


# ---- the writer prompt ----------------------------------------------------------------------

def test_the_writer_is_told_to_write_for_a_reader_and_never_to_say_the_facts_show():
    p = writer_prompt("The raid", ["The raid"], CS, PS, TITLES, books=BOOKS, length="section")
    assert w.READER_RULE.split("{depth}")[0] in p and w.READER_DEPTH_RULE in p
    for needle in ("connected", "fuller sentences", "Prefer depth to brevity",
                   '"The facts show"', "understandable if the sentence before it were",
                   "topic sentence that introduces the subject"):
        assert needle in p, needle
    # the opening that produced "The facts show ..." is gone
    assert "says how these facts answer that aspect" not in p


def test_the_lens_opening_names_its_subject_too():
    p = writer_prompt("The raid", ["The raid"], CS, PS, TITLES, books=BOOKS, length="lens")
    assert "topic sentence that introduces the subject" in p
    assert "says how these facts bear on the question" not in p


def test_a_short_answer_is_not_asked_for_depth():
    p = writer_prompt("q", ["q"], CS, PS, TITLES, books=BOOKS, length="short")
    assert w.READER_DEPTH_RULE not in p and '"The facts show"' in p


def test_reader_prose_off_gives_the_prompt_before_it(monkeypatch):
    monkeypatch.setattr(w, "READER_PROSE", False)
    p = writer_prompt("The raid", ["The raid"], CS, PS, TITLES, books=BOOKS, length="section")
    assert w.SECTION_LENGTH_RULE in p and "thoughtful companion" not in p


def test_the_new_prompt_texts_name_no_book():
    banned = r"\b(holmes|watson|sherlock|rand|tam|moiraine|trolloc|emond|wheel|darcy|wizard|" \
             r"dragon|magic|sword|crime|murder|detective)\b"
    text = "\n".join((w.READER_RULE, w.READER_DEPTH_RULE, w._READER_OPENING, w.ORPHAN_RULE,
                      w.INTRO_RULE, w.REMOVED_RULE, synth._SUMMARY_RULE)).lower()
    assert not re.findall(banned, text)


# ---- the check: a summary against the union of its claims ------------------------------------

def test_the_check_judges_a_summary_against_the_union_of_its_claims():
    p = faithfulness_prompt(CS, PS, TITLES, [S("Oren's farm and the village were raided.", [2, 4])],
                            books=BOOKS)
    assert "judged\n  against all of them together" in p
    assert "adds a fact, cause, order or evaluation" in p
    # strictness against real additions stays
    assert "adds a fact, name, number or detail the cited claims do not state" in p


def test_summary_check_off_gives_the_check_before_it(monkeypatch):
    monkeypatch.setattr(synth, "SUMMARY_CHECK", False)
    p = faithfulness_prompt(CS, PS, TITLES, [S("A.", [2])], books=BOOKS)
    assert "against all of them together" not in p


async def test_a_supported_summary_opener_is_shown_and_an_embellished_sentence_is_not():
    draft = Prose(sentences=[
        S("Oren's farm was raided before raiders burned half the village.", [2, 3, 4], True),
        S("Oren ran toward the back of the house, ignoring the creatures behind him.", [2]),
        S("Oren shouted to Ash to run and hide.", [3]),
        S("Oren bravely ran on while the house burned.", [2]),
        S("Raiders burned half the village.", [4], True)])
    m = Models([draft], claims=CS[1:], claim_verdict=lambda t: "supported",
               sentence_verdict=unsupported_if("while the house burned"))
    draft.sentences[:] = [s.model_copy(update={"claims": [k - 1 for k in s.claims]})
                          for s in draft.sentences]
    a = await m.section()
    texts = [s.text for s in a.prose]
    assert texts[0].startswith("Oren's farm was raided")
    assert not any("bravely" in t for t in texts)
    assert len(m.write_prompts) == 1  # nothing orphaned or uncited, the opening stands
