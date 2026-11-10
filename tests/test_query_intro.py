"""OPENER_CITES, INTRO_REPAIR and GENTLE_FALLBACK (spec intro-reliability, 2026-11-09).

The failure, seen on a deep answer to "explain <a people>": the short answer's
opening summed up several kept claims but cited one, so the check rejected it as
overreach; the repair wrote another detail, not an introduction; the answer
showed code's "no direct summary" line, and a section whose repair lost two
sentences fell back to its whole claim list.

The fixture is the made-up valley story of test_query_orphans: four claims, one
chapter each, so the numbers a writer cites are the claims' own."""
import re
from types import SimpleNamespace

from lore.query import deep
from lore.query import synthesize as synth
from lore.query import write as w
from lore.query.models import AnswerSection, ProseSentence, SectionSpec
from lore.query.synthesize import CheckOutcome, synthesize, with_opener
from lore.query.write import Prose, drop_orphans, leans_back
from tests.test_query_orphans import BOOKS, CS, EVIDENCE, PS, SECTION, TITLES, S

# The opening the writer gives: it sums up three claims and cites one.
SUMMED = "Oren's household is caught in a raid that ends with half the village burned."
# The same opening with a trait no claim gives.
EMBELLISHED = "Oren, a brave and famous swordsman, is caught in a raid on his village."
LEAPT = "Oren had leapt through a front window at the farm."
NAMED = "Oren is a farmer whose household the raiders strike before they burn the village."
DETAILS = [S("Oren ran toward the back of the house, ignoring the creatures behind him.", [2]),
           S("Oren shouted to Ash to run and hide.", [3]),
           S("Raiders burned half the village.", [4], True),
           S(LEAPT, [1])]


def opens(prompt: str) -> bool:
    return "Sentence 1 opens the text" in prompt


class Fake:
    """Claims step, writer and check. `drafts`: the writer's replies to the
    answer and repair prompts in turn (the last repeats); `intros`: its replies to
    the introduction prompt. The check supports every claim; a sentence is judged
    by `verdict(text, prompt)`, and the opening of a unit is given `support` when
    the prompt judges it as one."""

    def __init__(self, drafts, *, intros=(), verdict=None, support=(1, 2, 4), claims=CS):
        self.drafts, self.intros = list(drafts), list(intros)
        self.verdict = verdict or self.default_verdict
        self.support = list(support)
        self.claims = claims
        self.write_prompts: list[str] = []
        self.intro_prompts: list[str] = []
        self.check_prompts: list[str] = []
        self.schemas: list = []
        self.log: list[str] = []

    @staticmethod
    def default_verdict(text, prompt):
        if "swordsman" in text or "endless" in text:
            return False  # what no claim states fails, however it is judged
        if text == SUMMED:
            # under-cited: supported only when judged against every claim
            return opens(prompt) or prompt.startswith("Check an introduction")
        return True

    async def generate(self, prompt, schema):
        return synth.Synthesis(can_answer=True, claims=[
            {"text": c.text, "passage": c.passage, "quote": c.quote} for c in self.claims])

    async def write(self, prompt, schema):
        if prompt.startswith("Write the introduction"):
            self.intro_prompts.append(prompt)
            self.log.append("intro")
            got = self.intros[min(len(self.intro_prompts), len(self.intros)) - 1] if self.intros else None
        else:
            self.write_prompts.append(prompt)
            self.log.append("write")
            got = self.drafts[min(len(self.write_prompts), len(self.drafts)) - 1]
        if got is None or isinstance(got, Exception):
            raise got or RuntimeError("no introduction scripted")
        return got

    async def check(self, prompt, schema):
        self.check_prompts.append(prompt)
        self.schemas.append(schema)
        intro = prompt.startswith("Check an introduction")
        self.log.append("intro_check" if intro else "check")
        claims = re.findall(r"^\d+\. claim: (.*)$", prompt, re.M)
        texts = re.findall(r"^\d+\. sentence(?: \(\w+\))?: (.*)$", prompt, re.M)
        n = len(claims)
        judged = []
        for i, t in enumerate(texts, start=1):
            ok = self.verdict(t, prompt)
            first = intro or (i == 1 and opens(prompt))
            judged.append({"claim": i, "verdict": "supported" if ok else "not_supported",
                           "reason": f"why: {t[:20]}", "inference_in_fact": False,
                           "supported_by": [k for k in self.support if k <= n] if first else []})
        return schema.model_validate({
            "judgements": [{"claim": i, "verdict": "supported", "reason": "r", "restated_by": 0}
                           for i in range(1, n + 1)],
            "sentence_judgements": judged})

    async def section(self, spend_ok=None, section=SECTION):
        return await synthesize("Oren", EVIDENCE, self.generate, mode="graph", check=self.check,
                                write=self.write, book_titles=TITLES, books=BOOKS, section=section,
                                spend_ok=spend_ok)

    def models(self, spend_ok=None):
        return SimpleNamespace(write=self.write, check=self.check, spend_ok=spend_ok)


# ---- switches and arms --------------------------------------------------------------------

def test_the_switches_are_on_by_default():
    assert w.OPENER_CITES is True and w.INTRO_REPAIR is True and w.GENTLE_FALLBACK is True


def test_run_pair_has_an_arm_for_each():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).parents[1] / "evals/answer_quality/run_pair.py"
    spec = importlib.util.spec_from_file_location("run_pair_intro", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.parse_arm("opener_cites=off") == {("lore.query.write", "OPENER_CITES"): False}
    assert module.parse_arm("intro_repair=false") == {("lore.query.write", "INTRO_REPAIR"): False}
    assert module.parse_arm("gentle_fallback=on") == {("lore.query.write", "GENTLE_FALLBACK"): True}


def test_the_new_prompt_texts_name_no_book():
    banned = r"\b(holmes|watson|sherlock|rand|loial|ogier|moiraine|trolloc|stedding|wheel|darcy|" \
             r"wizard|dragon|magic|sword|crime|murder|detective)\b"
    text = "\n".join((w.OPENER_RULE, w.INTRO_PROMPT, synth._SUMMARY_OPENER,
                      synth._INTRO_CHECK_PROMPT)).lower()
    assert not re.findall(banned, text)


# ---- A: an opening cites what it sums up --------------------------------------------------

def test_the_writer_is_told_an_opening_cites_every_fact_it_draws_on():
    for length in ("section", "lens", "short", None):
        p = w.writer_prompt("Oren", ["Oren"], CS, PS, TITLES, books=BOOKS, length=length)
        assert w.OPENER_RULE in p, length
    assert w.OPENER_RULE not in w.writer_prompt("Oren", ["Oren"], CS, PS, TITLES, books=BOOKS,
                                                length="recap")


def test_the_check_extends_the_summary_rule_for_the_opening():
    sents = [S(SUMMED, [1], True), *DETAILS]
    plain = synth.faithfulness_prompt(CS, PS, TITLES, sents, BOOKS)
    opened = synth.faithfulness_prompt(CS, PS, TITLES, sents, BOOKS, opener=True)
    rule = synth._SUMMARY_RULE.format(not_supported=synth.NOT_SUPPORTED)
    assert rule in plain and rule in opened  # one summary rule, extended, not a second copy
    assert not opens(plain) and opens(opened)
    assert opened.index("Sentence 1 opens the text") > opened.index(rule)
    assert "ALL the claims above" in opened and "supported_by" in opened
    assert "that no claim above" in opened  # an addition still fails
    assert "supported_by" in synth.check_schema(True, True).model_fields["sentence_judgements"] \
        .annotation.__args__[0].model_fields
    assert "sentence_judgements" not in synth.check_schema(True, False).model_fields or \
        "supported_by" not in synth.check_schema(True, False).model_fields["sentence_judgements"] \
        .annotation.__args__[0].model_fields


def test_a_reply_without_supported_by_still_reads():
    schema = synth.check_schema(True, True)
    got = schema.model_validate({"judgements": [{"claim": 1, "verdict": "supported", "restated_by": 0}],
                                 "sentence_judgements": [{"claim": 1, "verdict": "supported"}]})
    assert got.sentence_judgements[0].supported_by == []


async def test_an_under_cited_but_supported_opener_is_kept_with_its_citations():
    m = Fake([Prose(sentences=[S(SUMMED, [1], True), *DETAILS])])
    a = await m.section()
    assert len(m.write_prompts) == 1 and len(m.check_prompts) == 1 and not m.intro_prompts
    assert opens(m.check_prompts[0])
    assert a.answer_style == "prose" and a.prose[0].text == SUMMED
    assert a.prose[0].claims == (1, 2, 4)  # the check's supporting claims, attached
    assert set(a.prose[0].passages) == {1, 2, 4}


async def test_an_embellished_opener_fails_however_many_claims_it_cites():
    m = Fake([Prose(sentences=[S(EMBELLISHED, [1, 2, 3, 4], True), *DETAILS]),
              Exception("no repair")], intros=[Exception("no intro")])
    a = await m.section()
    assert all("swordsman" not in s.text for s in a.prose)
    assert a.answer_style == "prose" and a.prose[0].text.startswith("Oren ran")


def test_with_opener_keeps_a_rejected_opening_as_written():
    sents = [S(EMBELLISHED, [1], True), *DETAILS]
    out = CheckOutcome(CS, [], True, [True] * 4, [False, True, True, True], opener_support=[1, 2])
    assert with_opener(sents, out)[0].claims == [1]
    ok = CheckOutcome(CS, [], True, [True, True, False, True], [True] * 4, opener_support=[2, 3])
    assert with_opener(sents, ok)[0].claims == [1, 2, 3]  # a cut supporter is still cited
    assert w.kept_flags(with_opener(sents, ok), ok.claim_ok, ok.sentence_ok)[0] is False


async def test_with_opener_cites_off_the_opening_is_judged_on_its_own_claims(monkeypatch):
    monkeypatch.setattr(w, "OPENER_CITES", False)
    monkeypatch.setattr(w, "INTRO_REPAIR", False)
    m = Fake([Prose(sentences=[S(SUMMED, [1], True), *DETAILS]), Exception("no repair")])
    a = await m.section()
    assert not opens(m.check_prompts[0])
    assert w.OPENER_RULE not in m.write_prompts[0]
    assert a.prose[0].text != SUMMED


async def test_a_lens_section_keeps_its_under_cited_opener():
    m = Fake([Prose(sentences=[S(SUMMED, [1], True), *DETAILS])])
    got = await deep.cited_prose("Oren", ["Oren"], CS, PS, m.models(), length="lens",
                                 book_titles=TITLES, books=BOOKS)
    assert got[0].text == SUMMED and got[0].claims == (1, 2, 4)


async def test_a_prose_answer_keeps_its_under_cited_opener(monkeypatch):
    monkeypatch.setattr(w, "PROSE_FACTUAL", True)
    m = Fake([Prose(sentences=[S(SUMMED, [1], True), *DETAILS])])
    a = await synthesize("Who is Oren?", EVIDENCE, m.generate, mode="graph", check=m.check,
                         write=m.write, book_titles=TITLES, books=BOOKS)
    assert opens(m.check_prompts[0])
    assert a.answer_style == "prose" and a.prose[0].text == SUMMED
    assert a.prose[0].claims == (1, 2, 4)


# ---- B: the introduction ------------------------------------------------------------------

INTRO = Prose(sentences=[S(NAMED, [1, 4])])


async def test_a_rejected_opening_gets_a_named_introduction():
    # the repair writes another detail first, as on the real answer; the
    # introduction call then writes what the section is about
    detail_first = Prose(sentences=[S("Oren shouted to Ash to run and hide.", [2], True),
                                    S("Raiders burned half the village.", [3], True)])
    m = Fake([Prose(sentences=[S(EMBELLISHED, [1, 2], True), *DETAILS]), detail_first],
             intros=[INTRO])
    a = await m.section()
    assert m.log == ["write", "check", "write", "check", "intro", "intro_check"]
    (prompt,) = m.intro_prompts
    assert "about this subject: Oren" in prompt
    assert "what or who the subject is and how it figures in" in " ".join(prompt.split())
    assert "EVERY fact it draws on" in prompt
    for c in CS:
        assert c.text in prompt
    assert a.prose[0].text == NAMED and not leans_back(a.prose[0].text)
    assert a.prose[0].claims == (1, 2, 4)  # its own and the check's supporting claims
    assert all("swordsman" not in s.text for s in a.prose)


async def test_an_introduction_that_fails_its_check_is_not_shown():
    m = Fake([Prose(sentences=[S(EMBELLISHED, [1, 2], True), *DETAILS]), Exception("no repair")],
             intros=[Prose(sentences=[S("Oren is an endless warrior of the valley.", [1])])])
    a = await m.section()
    assert len(m.intro_prompts) == 1 and len(m.check_prompts) == 2
    assert a.answer_style == "prose" and a.prose[0].text.startswith("Oren ran")


async def test_the_spend_check_is_asked_before_each_extra_call():
    m = Fake([Prose(sentences=[S(EMBELLISHED, [1, 2], True), *DETAILS]), Exception("no repair")],
             intros=[INTRO])

    def spend_ok():
        m.log.append("spend_ok")
        return True
    await m.section(spend_ok=spend_ok)
    assert m.log == ["write", "check", "spend_ok", "write", "spend_ok", "intro", "intro_check"]


async def test_no_room_to_spend_means_no_introduction_call():
    m = Fake([Prose(sentences=[S(EMBELLISHED, [1, 2], True), *DETAILS])], intros=[INTRO])
    a = await m.section(spend_ok=lambda: False)
    assert m.log == ["write", "check"]
    assert a.answer_style == "prose" and a.prose[0].text.startswith("Oren ran")


async def test_a_deep_answer_counts_the_introduction_against_its_budget():
    spend = deep.Spend(n=deep.DEEP_MAX_REQUESTS - 2)
    assert deep.repair_budget(spend, None, 0)() and not deep.repair_budget(spend, None, 1)()


# ---- B: the short answer ------------------------------------------------------------------

def _section(heading, sentences, start):
    prose = tuple(ProseSentence(text=t, claims=(start + i,), passages=(start + i,),
                                new_paragraph=i == 0) for i, t in enumerate(sentences))
    return AnswerSection(heading=heading, prose=prose, text=" ".join(sentences),
                         claims_start=start, claims_end=start + len(sentences) - 1)


SECTIONS = [_section("Who Oren is", ["Oren is a farmer of the valley.",
                                     "Oren ran toward the back of the house."], 1),
            _section("The raid", ["Oren shouted to Ash to run and hide.",
                                  "Raiders burned half the village."], 3)]


async def test_a_short_answer_with_no_supported_opening_is_built_from_the_sections():
    gist = Prose(sentences=[S(EMBELLISHED, [1, 2]), S("Oren shouted to Ash to run and hide.", [3])])
    m = Fake([gist], intros=[Prose(sentences=[S("Oren is an endless warrior.", [1])])])
    short = await deep.short_answer("Who is Oren?", SECTIONS, CS, PS, m.models(),
                                    book_titles=TITLES, books=BOOKS)
    assert len(m.intro_prompts) == 1  # the introduction was tried first
    assert "the short answer that opens a longer answer" in m.intro_prompts[0]
    # the first section's opening paragraph, checked already
    assert [s.text for s in short] == ["Oren is a farmer of the valley.",
                                       "Oren ran toward the back of the house."]
    assert all(s.short for s in short) and short[0].new_paragraph
    assert deep.NO_SUMMARY_LINE not in [s.text for s in short]
    assert [s.claims for s in short] == [(1,), (2,)]


async def test_a_short_answer_whose_writer_fails_is_built_from_the_sections():
    m = Fake([Exception("writer down")])
    short = await deep.short_answer("Who is Oren?", SECTIONS, CS, PS, m.models(),
                                    book_titles=TITLES, books=BOOKS)
    assert short[0].text == "Oren is a farmer of the valley." and len(short) == 2


async def test_a_short_answer_gets_its_introduction_when_the_opening_fails():
    gist = Prose(sentences=[S(EMBELLISHED, [1, 2]), S("Oren shouted to Ash to run and hide.", [3])])
    m = Fake([gist], intros=[INTRO])
    short = await deep.short_answer("Who is Oren?", SECTIONS, CS, PS, m.models(),
                                    book_titles=TITLES, books=BOOKS)
    assert [s.text for s in short] == [NAMED, "Oren shouted to Ash to run and hide."]


async def test_a_short_answer_whose_under_cited_opening_is_supported_keeps_it():
    gist = Prose(sentences=[S(SUMMED, [1]), S("Oren shouted to Ash to run and hide.", [3])])
    m = Fake([gist])
    short = await deep.short_answer("Who is Oren?", SECTIONS, CS, PS, m.models(),
                                    book_titles=TITLES, books=BOOKS)
    assert not m.intro_prompts
    assert short[0].text == SUMMED and short[0].claims == (1, 2, 4)


async def test_with_intro_repair_off_the_limit_line_is_shown_as_before(monkeypatch):
    monkeypatch.setattr(w, "INTRO_REPAIR", False)
    m = Fake([Exception("writer down")])
    short = await deep.short_answer("Who is Oren?", SECTIONS, CS, PS, m.models(),
                                    book_titles=TITLES, books=BOOKS)
    assert [s.text for s in short] == [deep.NO_SUMMARY_LINE]


def test_the_section_opening_skips_claim_lists_and_leaning_sentences():
    listed = AnswerSection(heading="x", prose=(), text="- a", claims_start=1, claims_end=1,
                           style="claims")
    leaning = _section("y", ["He ran.", "Oren hid."], 1)
    assert deep.section_opening([listed, leaning, *SECTIONS])[0].text == "Oren is a farmer of the valley."
    assert deep.section_opening([listed]) == []


async def test_a_recap_gist_that_fails_takes_the_sections_opening():
    from lore.query import recap
    m = Fake([Exception("writer down")])
    gist = await recap.short_gist("What happens?", SECTIONS, CS, PS, m.models(),
                                  book_titles=TITLES, books=BOOKS)
    assert gist[0].text == "Oren is a farmer of the valley."
    assert deep.NO_SUMMARY_LINE not in [s.text for s in gist]


# ---- C: the gentler fallback --------------------------------------------------------------

def test_orphans_are_dropped_one_by_one_in_cascade():
    sents = [S("Oren leapt.", [1]), S("He ran.", [2]), S("He shouted.", [3]),
             S("Ash hid.", [4], True), S("She waited.", [4])]
    # "He ran" leans on the cut opening; "He shouted" then follows a dropped one
    assert drop_orphans(sents, [False, True, True, True, True]) == [False, False, False, True, True]
    assert drop_orphans(sents, [True] * 5) == [True] * 5


HE_RAN = "He ran toward the back of the house, ignoring the creatures behind him."
FIRST = Prose(sentences=[S(EMBELLISHED, [1, 2], True), S(HE_RAN, [2]),
                         S("Oren shouted to Ash to run and hide.", [3]),
                         S("Raiders burned half the village.", [4], True), S(LEAPT, [1])])


async def test_first_the_repaired_draft_minus_its_rejected_sentences():
    # the repair loses two sentences of five: too many for the usual rule, but
    # nothing left in it is orphaned, so it is shown without them
    repaired = Prose(sentences=[
        S(NAMED, [1, 4], True), S("Oren fled toward the back of his house.", [2]),
        S("Oren was an endless runner.", [2]), S("Oren shouted to Ash to run and hide.", [3]),
        S("Oren, a swordsman, watched raiders burn half the village.", [4], True)])
    m = Fake([FIRST, repaired], intros=[INTRO])
    a = await m.section()
    assert [s.text for s in a.prose] == [NAMED, "Oren fled toward the back of his house.",
                                         "Oren shouted to Ash to run and hide."]
    assert a.answer_style == "prose" and not m.intro_prompts


async def test_then_the_first_draft_minus_rejects_and_orphans_after_a_supported_intro():
    # the repair's own opening passes, but it leaves an orphan: its opening is
    # put before the first draft, whose orphan is dropped
    repaired = Prose(sentences=[S(NAMED, [1, 4], True), S("Oren was an endless runner.", [2]),
                                S("He shouted to Ash to run and hide.", [3])])
    m = Fake([FIRST, repaired], intros=[INTRO])
    a = await m.section()
    texts = [s.text for s in a.prose]
    assert texts[0] == NAMED and HE_RAN not in texts
    assert "Oren shouted to Ash to run and hide." in texts
    assert not m.intro_prompts  # it has its opening already
    assert a.answer_style == "prose"


async def test_then_the_first_draft_with_an_introduction_of_its_own():
    m = Fake([FIRST, Exception("no repair")], intros=[INTRO])
    a = await m.section()
    texts = [s.text for s in a.prose]
    assert texts[0] == NAMED and HE_RAN not in texts and a.answer_style == "prose"
    assert m.log == ["write", "check", "write", "intro", "intro_check"]


async def test_the_claim_list_only_when_nothing_usable_remains():
    chain = Prose(sentences=[S(EMBELLISHED, [1, 2], True), S(HE_RAN, [2]),
                             S("He shouted to Ash to run and hide.", [3]),
                             S("They saw raiders burn half the village.", [4])])
    m = Fake([chain, Exception("no repair")], intros=[INTRO])
    a = await m.section()
    assert a.answer_style == "claims" and a.prose == ()
    assert not m.intro_prompts


async def test_a_deep_section_that_loses_only_its_opener_keeps_its_prose():
    # the last section of the real answer: the first draft lost its opener, the
    # repair lost two sentences and left an orphan; the section stays prose
    first = Prose(sentences=[S(EMBELLISHED, [1, 2], True), *DETAILS])
    repaired = Prose(sentences=[S("Oren was an endless runner.", [1], True),
                                S("He shouted to Ash to run and hide.", [2]),
                                S("Oren, a swordsman, saw the village burn.", [3])])
    m = Fake([first, repaired], intros=[INTRO])
    a = await m.section()
    assert a.answer_style == "prose"
    assert [s.text for s in a.prose][0] == NAMED
    assert "Raiders burned half the village." in [s.text for s in a.prose]


async def test_with_gentle_fallback_off_an_orphan_gives_the_claim_list(monkeypatch):
    monkeypatch.setattr(w, "GENTLE_FALLBACK", False)
    monkeypatch.setattr(w, "INTRO_REPAIR", False)
    m = Fake([FIRST, Exception("no repair")])
    a = await m.section()
    assert a.answer_style == "claims"


async def test_a_lens_section_falls_back_gently_too():
    m = Fake([FIRST, Exception("no repair")], intros=[INTRO])
    got = await deep.cited_prose("Oren", ["Oren"], CS, PS, m.models(), length="lens",
                                 book_titles=TITLES, books=BOOKS)
    assert got and got[0].text == NAMED and HE_RAN not in [s.text for s in got]


async def test_a_recap_section_is_not_given_an_introduction():
    recap = SectionSpec(part_of="What happens?", max_claims=12, length="recap", focus="ch 1-4")
    m = Fake([FIRST, Exception("no repair")], intros=[INTRO])
    a = await m.section(section=recap)
    assert not m.intro_prompts
    assert all(s.text != HE_RAN for s in a.prose)


def test_the_introduction_check_judges_every_sentence_against_every_claim():
    p = synth.intro_check_prompt(CS, PS, [S(NAMED, [1, 4])], TITLES, BOOKS)
    flat = " ".join(p.split())
    assert synth._SUMMARY_RULE.format(not_supported=synth.NOT_SUPPORTED) in p
    assert "Each sentence opens the text" in p and "For every other sentence" not in p
    assert "ALL the claims above together" in flat
    assert '"not an introduction"' in flat  # a detail is not an introduction
    for c in CS:
        assert c.text in p
