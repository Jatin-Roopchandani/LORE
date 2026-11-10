"""STORY_ORDER_PROSE (spec order-and-setup, 2026-11-09).

A narrative answer tells its events in story order. Seen live: the writer was
shown the facts in story order and still told a detail of passage [2] ("invited
him to lunch") after the meeting of passages [3] and [4]. Code finds such a
sentence from the passages' story positions and moves it back to its place in
its paragraph; a move that would leave a pronoun leaning on a new sentence goes
to one repair round instead.

The fixture is a made-up story with the shape of that answer: five passages of
one chapter in story order, and a sentence citing [2] placed after [4].
"""
import re
from pathlib import Path

import pytest

from lore.query import synthesize as synth
from lore.query import write as w
from lore.query.models import BookInfo, Claim, Evidence, Passage, ProseSentence, SectionSpec
from lore.query.synthesize import Synthesis, synthesize
from lore.query.write import Prose, Sentence

PASSAGES = [
    "Mara could not pay for her room at the inn on what the guild paid her, and she "
    "made up her mind to leave it.",
    "On the day she decided to move, someone tapped her shoulder at the Lantern Inn; it "
    "was Pell, who had once carried her bags at the academy. Mara was lonely and glad of "
    "a friendly face, and she asked Pell to supper.",
    "Pell said, \"Corin, this is Mara,\" and Corin shook her hand warmly.",
    "\"You have come from the coast, I see,\" said Corin, and Mara was astonished.",
    "Pell explained that Mara wanted rooms and that Corin wanted someone to share his, "
    "and Corin seemed delighted.",
]
PS = tuple(Passage(n=i, chunk_id=f"b:1:{i - 1}", book_id="b", chapter_seq=1, text=t)
           for i, t in enumerate(PASSAGES, start=1))
BOOKS = {"b": BookInfo("The Lantern")}
TITLES = {"b": "The Lantern"}
EVIDENCE = Evidence(edges=(), passages=PS, truncated=False)

# One claim per fact of passages 2 to 5 (two of passage 2), in story order, so the
# numbers the writer cites are the claims' own.
CLAIMS = [
    Claim(text="Pell tapped Mara's shoulder at the Lantern Inn.", passage=2,
          quote="someone tapped her shoulder at the Lantern Inn"),
    Claim(text="Mara was lonely and asked Pell to supper.", passage=2,
          quote="Mara was lonely and glad of a friendly face"),
    Claim(text="Pell introduced Corin to Mara, and Corin shook her hand warmly.", passage=3,
          quote="Corin shook her hand warmly"),
    Claim(text="Corin saw that Mara came from the coast, and she was astonished.", passage=4,
          quote="You have come from the coast, I see"),
    Claim(text="Pell explained that Mara wanted rooms and Corin wanted someone to share his.",
          passage=5, quote="Pell explained that Mara wanted rooms"),
]

OPENER = "Mara came to know Corin through an old friend, Pell, who brought the two together."
TAP = "At the Lantern Inn someone tapped Mara's shoulder, and it was Pell."
MEET = "Pell introduced the two, and Corin shook Mara's hand warmly."
COAST = "Corin saw at once that Mara came from the coast, which astonished her."
SUPPER = "Mara was lonely, was glad of a friendly face, and asked Pell to supper."
ROOMS = "Pell explained that Mara wanted rooms and that Corin wanted someone to share his."


def S(text, claims, new_paragraph=False, kind="fact"):
    return Sentence(text=text, claims=claims, new_paragraph=new_paragraph, kind=kind)


def draft(supper=SUPPER):
    """The live answer's shape: the opener, [2], [3], [4], then [2] again; [5]."""
    return Prose(sentences=[S(OPENER, [1, 3, 5]), S(TAP, [1]), S(MEET, [3]), S(COAST, [4]),
                            S(supper, [2]), S(ROOMS, [5], True)])


class Models:
    """Fake claims step, writer and check: the writer's replies in turn (the last
    repeats); the check passes every claim and sentence."""

    def __init__(self, drafts):
        self.drafts = list(drafts)
        self.write_prompts: list[str] = []
        self.check_prompts: list[str] = []

    async def generate(self, prompt, schema):
        return Synthesis(can_answer=True, claims=[
            {"text": c.text, "passage": c.passage, "quote": c.quote} for c in CLAIMS])

    async def write(self, prompt, schema):
        self.write_prompts.append(prompt)
        reply = self.drafts[min(len(self.write_prompts), len(self.drafts)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def check(self, prompt, schema):
        self.check_prompts.append(prompt)
        claims = re.findall(r"^\d+\. claim: (.*)$", prompt, re.M)
        texts = re.findall(r"^\d+\. sentence(?: \(\w+\))?: (.*)$", prompt, re.M)
        return schema.model_validate({
            "judgements": [{"claim": i, "verdict": "supported", "reason": "r", "restated_by": 0}
                           for i in range(1, len(claims) + 1)],
            "sentence_judgements": [
                {"claim": i, "verdict": "supported", "reason": "r", "inference_in_fact": False}
                for i in range(1, len(texts) + 1)]})

    async def answer(self, section=None):
        return await synthesize("How did Mara meet Corin?", EVIDENCE, self.generate,
                                mode="graph", check=self.check, write=self.write,
                                book_titles=TITLES, books=BOOKS, section=section)


def texts(answer):
    return [s.text for s in answer.prose]


# --- switches -------------------------------------------------------------------

def test_the_switch_is_on_by_default():
    assert w.STORY_ORDER_PROSE is True


def test_run_pair_has_an_arm():
    import importlib.util
    path = Path(__file__).parents[1] / "evals/answer_quality/run_pair.py"
    spec = importlib.util.spec_from_file_location("run_pair_story_order", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.parse_arm("story_order_prose=false") == {
        ("lore.query.write", "STORY_ORDER_PROSE"): False}


def test_it_applies_to_narrative_prose_only():
    assert w.story_order_prose_on(None) and w.story_order_prose_on("section")
    assert w.story_order_prose_on("recap")
    assert not w.story_order_prose_on("short") and not w.story_order_prose_on("recap_short")
    assert not w.story_order_prose_on("lens")
    assert not w.story_order_prose_on(None, shape=object())


def test_the_writer_is_told_to_keep_story_order(monkeypatch):
    on = w.writer_prompt("How did Mara meet Corin?", [], CLAIMS, PS, TITLES, BOOKS)
    assert w.EVENT_ORDER_RULE in on
    monkeypatch.setattr(w, "STORY_ORDER_PROSE", False)
    off = w.writer_prompt("How did Mara meet Corin?", [], CLAIMS, PS, TITLES, BOOKS)
    assert w.EVENT_ORDER_RULE not in off and off == on.replace(w.EVENT_ORDER_RULE, "")


# --- detection --------------------------------------------------------------------

def P(text, passages, new_paragraph=False, kind="fact"):
    return ProseSentence(text=text, claims=tuple(passages), passages=tuple(passages),
                         new_paragraph=new_paragraph, kind=kind)


def test_the_live_shape_has_one_fault():
    prose = [P(OPENER, [2, 3, 5]), P(TAP, [2]), P(MEET, [3]), P(COAST, [4]), P(SUPPER, [2]),
             P(ROOMS, [5], True)]
    keys = w.story_keys(prose, PS, TITLES, BOOKS)
    assert w.order_faults(prose, keys) == [4]
    assert w.chronological(prose, keys)


@pytest.mark.parametrize("text", [
    "Earlier, Mara had asked Pell to supper.", "Before that, Mara asked Pell to supper.",
    "Mara had already asked Pell to supper.", "Mara had met Pell at the academy.",
    "Previously Mara asked Pell to supper.",
])
def test_a_flashback_is_not_a_fault(text):
    prose = [P(TAP, [2]), P(MEET, [3]), P(text, [2])]
    assert w.flashback(text)
    assert w.order_faults(prose, w.story_keys(prose, PS, TITLES, BOOKS)) == []


@pytest.mark.parametrize("text", [SUPPER, "Mara had a friend in Pell.", "Mara had to leave."])
def test_a_plain_sentence_is_no_flashback(text):
    assert not w.flashback(text)


def test_the_opener_is_exempt_and_a_later_sentence_is_not():
    prose = [P("Mara and Corin came to share rooms.", [5]), P(TAP, [2]), P(MEET, [3])]
    keys = w.story_keys(prose, PS, TITLES, BOOKS)
    assert w.order_faults(prose, keys, opener=True) == []
    assert w.order_faults(prose, keys, opener=False) == [1, 2]
    # an opening sentence citing several passages is a summary, opener or not
    multi = [P(OPENER, [3, 5]), P(TAP, [2])]
    assert w.order_faults(multi, w.story_keys(multi, PS, TITLES, BOOKS), opener=False) == []


def test_a_sentence_citing_several_passages_counts_from_its_earliest():
    prose = [P(TAP, [2]), P(MEET, [3]), P("Pell and Corin both welcomed Mara.", [2, 5])]
    assert w.order_faults(prose, w.story_keys(prose, PS, TITLES, BOOKS)) == [2]


def test_paragraphs_readings_and_unordered_books_are_left_alone():
    # a new paragraph may start a new thread of the story
    prose = [P(MEET, [3]), P(SUPPER, [2], True)]
    assert w.order_faults(prose, w.story_keys(prose, PS, TITLES, BOOKS)) == []
    # a reading is no event
    prose = [P(MEET, [3]), P("This suggests Mara was lonely.", [2], kind="reading")]
    assert w.order_faults(prose, w.story_keys(prose, PS, TITLES, BOOKS)) == []
    # two books whose order is unknown are never compared
    other = Passage(n=6, chunk_id="c:1:0", book_id="c", chapter_seq=1, text="x")
    prose = [P(MEET, [3]), P("In another tale a bell rang.", [6])]
    keys = w.story_keys(prose, (*PS, other), {**TITLES, "c": "Other"},
                        {**BOOKS, "c": BookInfo("Other")})
    assert w.order_faults(prose, keys) == []


def test_an_answer_organised_by_theme_is_left_alone():
    prose = [P(ROOMS, [5]), P(COAST, [4]), P(MEET, [3]), P(TAP, [2])]
    keys = w.story_keys(prose, PS, TITLES, BOOKS)
    assert len(w.order_faults(prose, keys, opener=False)) == 3
    assert not w.chronological(prose, keys, opener=False)


def test_a_move_that_would_orphan_a_pronoun_is_refused():
    keys_of = lambda prose: w.story_keys(prose, PS, TITLES, BOOKS)  # noqa: E731
    # the moved sentence leans back: it would get a new sentence before it
    prose = [P(TAP, [2]), P(MEET, [3]), P(COAST, [4]), P("She asked Pell to supper.", [2])]
    assert w.story_reorder(prose, keys_of(prose)) is None
    # the sentence after the moved one's old place leans back on it
    prose = [P(TAP, [2]), P(MEET, [3]), P(SUPPER, [2]), P("Then she left.", [3])]
    assert w.story_reorder(prose, keys_of(prose)) is None
    # a move that leaves every leaning sentence its own predecessor is made
    prose = [P(TAP, [2]), P(MEET, [3]), P("He smiled.", [3]), P(SUPPER, [2])]
    assert [s.text for s in w.story_reorder(prose, keys_of(prose))] == [
        TAP, SUPPER, MEET, "He smiled."]


# --- the pipeline -------------------------------------------------------------------

async def test_an_out_of_order_event_is_moved_back_into_order():
    m = Models([draft()])
    a = await m.answer()
    assert a.answer_style == "prose"
    assert texts(a) == [OPENER, TAP, SUPPER, MEET, COAST, ROOMS]
    assert [s.new_paragraph for s in a.prose] == [False] * 5 + [True]
    # by code: no extra call
    assert len(m.write_prompts) == 1 and len(m.check_prompts) == 1
    assert w.EVENT_ORDER_RULE in m.write_prompts[0]


async def test_a_flashback_marked_sentence_is_not_moved():
    back = "Before that, Mara, lonely and glad of a friendly face, had asked Pell to supper."
    m = Models([draft(back)])
    a = await m.answer()
    assert texts(a) == [OPENER, TAP, MEET, COAST, back, ROOMS]
    assert len(m.write_prompts) == 1


async def test_a_move_that_would_orphan_a_pronoun_goes_to_the_repair_round():
    leaning = "She was lonely, was glad of a friendly face, and asked Pell to supper."
    repaired = Prose(sentences=[S(OPENER, [1, 3, 5]), S(TAP, [1]), S(SUPPER, [2]),
                                S(MEET, [3]), S(COAST, [4]), S(ROOMS, [5], True)])
    m = Models([draft(leaning), repaired])
    a = await m.answer()
    assert len(m.write_prompts) == 2 and len(m.check_prompts) == 2
    repair = m.write_prompts[1]
    assert w.REPAIR_LEAD in repair and f"5. {leaning}" in repair
    assert "Draft sentences 5 tell an event that happens earlier" in repair
    assert texts(a) == [OPENER, TAP, SUPPER, MEET, COAST, ROOMS]


async def test_a_failed_order_repair_keeps_the_first_draft():
    leaning = "She was lonely, was glad of a friendly face, and asked Pell to supper."
    m = Models([draft(leaning), Exception("the writer failed")])
    a = await m.answer()
    assert len(m.write_prompts) == 2  # one round, no more
    assert texts(a) == [OPENER, TAP, MEET, COAST, leaning, ROOMS]


async def test_a_repair_no_better_than_the_draft_is_not_used():
    leaning = "She was lonely, was glad of a friendly face, and asked Pell to supper."
    m = Models([draft(leaning)])  # the "repair" is the same draft again
    a = await m.answer()
    assert len(m.write_prompts) == 2 and len(m.check_prompts) == 2
    assert texts(a) == [OPENER, TAP, MEET, COAST, leaning, ROOMS]


async def test_a_recap_section_is_put_in_order_and_a_short_answer_is_not():
    recap = Prose(sentences=[S(TAP, [1]), S(MEET, [3]), S(COAST, [4]), S(SUPPER, [2]),
                             S(ROOMS, [5])])
    spec = SectionSpec(part_of="What happens at the inn?", max_claims=12, length="recap")
    a = await Models([recap]).answer(section=spec)
    assert texts(a) == [TAP, SUPPER, MEET, COAST, ROOMS]
    short = SectionSpec(part_of="What happens at the inn?", max_claims=12, length="short")
    b = await Models([recap]).answer(section=short)
    assert texts(b) == [TAP, MEET, COAST, SUPPER, ROOMS]


async def test_with_the_switch_off_the_writer_order_stands(monkeypatch):
    monkeypatch.setattr(w, "STORY_ORDER_PROSE", False)
    leaning = "She was lonely, was glad of a friendly face, and asked Pell to supper."
    for supper in (SUPPER, leaning):
        m = Models([draft(supper)])
        a = await m.answer()
        assert texts(a) == [OPENER, TAP, MEET, COAST, supper, ROOMS]
        assert len(m.write_prompts) == 1 and len(m.check_prompts) == 1
        assert w.EVENT_ORDER_RULE not in m.write_prompts[0]


def test_the_shown_text_before_and_after():
    """The offline before/after the report quotes (compose_text of each)."""
    before = [P(OPENER, [2, 3, 5]), P(TAP, [2]), P(MEET, [3]), P(COAST, [4]), P(SUPPER, [2]),
              P(ROOMS, [5], True)]
    after = w.story_reorder(before, w.story_keys(before, PS, TITLES, BOOKS))
    assert w.compose_text(after).split("\n\n")[0].endswith(
        f"{TAP} [2] {SUPPER} [2] {MEET} [3] {COAST} [4]")
    assert synth.PROSE_FALLBACK_NOTE  # the module is the one under test
