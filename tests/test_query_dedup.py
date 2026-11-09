"""DEDUP_SENTENCES (2026-11-09): no shown sentence restates an earlier one.

A blind held-out eval (h9b) showed one impression said twice in a row: a
sentence combining two claims was removed by the check, COVERAGE placed both
claims in their own words, and the two claims -- from two parts, quoting one
line of the book, worded differently -- said the same thing. Code now cites a
claim a shown sentence already says there instead of placing it, and drops a
fact sentence that restates an earlier one, moving its citations to the kept
one. A sentence that adds a distinct fact always stays.

The fixture is a made-up story of that shape."""
import re
from pathlib import Path

from lore.query import synthesize as synth
from lore.query import write as w
from lore.query.models import BookInfo, Claim, Evidence, Passage, ProseSentence, SectionSpec
from lore.query.synthesize import (
    Synthesis, _near_duplicate, dedup_sentences, restated_pairs, sentence_restates, states_claim,
    synthesize,
)
from lore.query.write import Prose, Sentence, place_uncited

PS = (Passage(n=1, chunk_id="b:1:0", book_id="b", chapter_seq=1,
              text="A stooped old woman came slowly up the stair, leaning on a stick. To Teodor "
                   "she looked like a respectable widow who had fallen on hard times. She asked "
                   "for the innkeeper by name."),
      Passage(n=2, chunk_id="b:2:0", book_id="b", chapter_seq=2,
              text="\"That was no widow,\" said Ilse. \"It was a young man, and a clever actor.\""))
STAIR = Claim(text="A stooped old woman came slowly up the stair, leaning on a stick.", passage=1,
              quote="A stooped old woman came slowly up the stair, leaning on a stick")
WIDOW = Claim(text="Teodor thought she looked like a respectable widow who had fallen on hard "
                   "times, so she seemed a poor widow.", passage=1,
              quote="a respectable widow who had fallen on hard times")
# The same impression from another part, worded differently, quoting more of the line.
AGAIN = Claim(text="To Teodor the visitor looked like a respectable widow who had fallen on hard "
                   "times; that is, she passed for a poor, elderly lady down on her luck.",
              passage=1,
              quote="To Teodor she looked like a respectable widow who had fallen on hard times")
NAME = Claim(text="She asked for the innkeeper by name.", passage=1,
             quote="She asked for the innkeeper by name")
ACTOR = Claim(text="Ilse says the visitor was really a young man and a clever actor.", passage=2,
              quote="It was a young man, and a clever actor")
# Shares WIDOW's line, and adds a fact of its own quote (the stick).
STICK = Claim(text="Teodor saw a respectable widow who had fallen on hard times, leaning on a "
                   "stick.", passage=1,
              quote="leaning on a stick. To Teodor she looked like a respectable widow who had "
                    "fallen on hard times")
CLAIMS = [STAIR, WIDOW, AGAIN, NAME, ACTOR]
TITLES = {"b": "The Lantern"}
BOOKS = {"b": BookInfo("The Lantern")}
EVIDENCE = Evidence(edges=(), passages=PS, truncated=False)


def P(claim_numbers, text=None, claims=CLAIMS, new_paragraph=False, kind="fact"):
    text = text if text is not None else claims[claim_numbers[0] - 1].text
    return ProseSentence(text=text, claims=tuple(claim_numbers),
                         passages=tuple(sorted({claims[k - 1].passage for k in claim_numbers})),
                         new_paragraph=new_paragraph, kind=kind)


def test_the_switch_is_on_and_has_an_arm():
    assert synth.DEDUP_SENTENCES is True
    import importlib.util
    path = Path(__file__).parents[1] / "evals/answer_quality/run_pair.py"
    spec = importlib.util.spec_from_file_location("run_pair_dedup", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.parse_arm("dedup_sentences=false") == {
        ("lore.query.synthesize", "DEDUP_SENTENCES"): False}


# ---- detection ----------------------------------------------------------------------------

def test_the_fixture_is_the_case_the_claim_collapse_lets_through():
    # Worded apart enough that the claims step keeps both: the duplicate is only
    # visible once both are shown.
    assert not _near_duplicate(WIDOW, AGAIN)


def test_a_near_identical_restatement_is_dropped_and_its_citations_merged():
    prose = (P([1]), P([2], new_paragraph=True), P([4]), P([3]), P([5], new_paragraph=True))
    assert restated_pairs(prose, CLAIMS) == [(3, 1)]
    out = dedup_sentences(prose, CLAIMS)
    assert [s.text for s in out] == [STAIR.text, WIDOW.text, NAME.text, ACTOR.text]
    assert out[1].claims == (2, 3) and out[1].passages == (1,)
    assert [s.new_paragraph for s in out] == [False, True, False, True]


def test_a_sentence_with_an_extra_distinct_fact_is_kept():
    claims = [WIDOW, STICK]
    prose = (P([1], claims=claims), P([2], claims=claims))
    assert not sentence_restates(prose[0], prose[1], claims)
    assert dedup_sentences(prose, claims) == prose


def test_a_like_sentence_from_other_evidence_is_kept():
    # Word overlap alone is not enough: the later sentence must rest on the same
    # stretch of the text.
    other = Claim(text="Teodor thought the innkeeper looked like a respectable man fallen on hard "
                       "times.", passage=2, quote="It was a young man, and a clever actor")
    claims = [WIDOW, other]
    prose = (P([1], claims=claims), P([2], claims=claims))
    assert dedup_sentences(prose, claims) == prose


def test_a_different_number_is_a_different_fact():
    a = Claim(text="Teodor counted 3 widows on the stair.", passage=1,
              quote="a respectable widow who had fallen on hard times")
    b = Claim(text="Teodor counted 4 widows on the stair.", passage=1,
              quote="a respectable widow who had fallen on hard times")
    prose = (P([1], claims=[a, b]), P([2], claims=[a, b]))
    assert dedup_sentences(prose, [a, b]) == prose


def test_readings_and_code_lines_are_never_compared():
    prose = (P([2]), P([3], kind="reading"))
    assert dedup_sentences(prose, CLAIMS) == prose


def test_a_sentence_the_next_one_leans_on_stays_unless_its_twin_is_right_before_it():
    leaning = P([4], text="She asked for the innkeeper by name.")
    apart = (P([2]), P([1]), P([3]), leaning)
    assert dedup_sentences(apart, CLAIMS) == apart  # dropping 3 would leave "She" after 1
    next_to = (P([1]), P([2]), P([3]), leaning)
    assert [s.claims for s in dedup_sentences(next_to, CLAIMS)] == [(1,), (2, 3), (4,)]


def test_states_claim_and_place_uncited_cite_a_claim_already_said():
    prose = (P([1]), P([2]), P([4]))
    assert states_claim(prose[1], 3, CLAIMS) and not states_claim(prose[0], 3, CLAIMS)
    texts, passages = [c.text for c in CLAIMS], [c.passage for c in CLAIMS]
    placed = place_uncited(prose, texts, passages, same=lambda s, k: states_claim(s, k, CLAIMS))
    assert [s.claims for s in placed] == [(1,), (2, 3), (4,), (5,)]
    assert [s.text for s in placed][-1] == ACTOR.text
    # Without it, as before: the claim is said again in its own words.
    before = place_uncited(prose, texts, passages)
    assert AGAIN.text in [s.text for s in before]


def test_place_uncited_catches_two_placed_claims_that_say_one_thing():
    # The h9b shape: neither claim is cited by a shown sentence; the second
    # placed one restates the first.
    prose = (P([1]), P([4]), P([5], new_paragraph=True))
    placed = place_uncited(prose, [c.text for c in CLAIMS], [c.passage for c in CLAIMS],
                           same=lambda s, k: states_claim(s, k, CLAIMS))
    assert [s.claims for s in placed] == [(1,), (4,), (2, 3), (5,)]


# ---- in the answer ------------------------------------------------------------------------

class Models:
    """Fake claims step, writer and check: every claim and sentence passes."""

    def __init__(self, draft):
        self.draft = draft

    async def generate(self, prompt, schema):
        return Synthesis(can_answer=True, claims=[
            {"text": c.text, "passage": c.passage, "quote": c.quote} for c in CLAIMS])

    async def write(self, prompt, schema):
        return self.draft

    async def check(self, prompt, schema):
        claims = re.findall(r"^\d+\. claim: (.*)$", prompt, re.M)
        texts = re.findall(r"^\d+\. sentence(?: \(\w+\))?: (.*)$", prompt, re.M)
        return schema.model_validate({
            "judgements": [{"claim": i, "verdict": "supported", "reason": "r", "restated_by": 0}
                           for i in range(1, len(claims) + 1)],
            "sentence_judgements": [{"claim": i, "verdict": "supported", "reason": "r",
                                     "inference_in_fact": False}
                                    for i in range(1, len(texts) + 1)]})

    async def answer(self, section=None):
        return await synthesize("Who came up the stair?", EVIDENCE, self.generate, mode="graph",
                                check=self.check, write=self.write, book_titles=TITLES,
                                books=BOOKS, section=section)


def S(claim_numbers, new_paragraph=False):
    return Sentence(text=CLAIMS[claim_numbers[0] - 1].text, claims=claim_numbers,
                    new_paragraph=new_paragraph)


DOUBLED = Prose(sentences=[S([1]), S([2]), S([4]), S([3]), S([5], True)])
LEFT_OUT = Prose(sentences=[S([1]), S([2]), S([4]), S([5], True)])


async def test_a_prose_answer_drops_the_restatement():
    a = await Models(DOUBLED).answer()
    assert a.answer_style == "prose"
    assert [s.text for s in a.prose] == [STAIR.text, WIDOW.text, NAME.text, ACTOR.text]
    assert a.prose[1].claims == (2, 3)
    assert a.text.count("respectable widow") == 1


async def test_coverage_cites_the_left_out_claim_instead_of_saying_it_again():
    a = await Models(LEFT_OUT).answer()
    assert [s.text for s in a.prose] == [STAIR.text, WIDOW.text, NAME.text, ACTOR.text]
    assert a.prose[1].claims == (2, 3)


async def test_a_deep_section_drops_the_restatement():
    section = SectionSpec(part_of="Who visits the inn?", max_claims=12, length="section")
    a = await Models(DOUBLED).answer(section=section)
    assert a.answer_style == "prose"
    assert a.text.count("respectable widow") == 1
    assert any(s.claims == (2, 3) for s in a.prose)


async def test_switched_off_the_answers_are_as_before(monkeypatch):
    monkeypatch.setattr(synth, "DEDUP_SENTENCES", False)
    a = await Models(DOUBLED).answer()
    assert [s.text for s in a.prose] == [STAIR.text, WIDOW.text, NAME.text, AGAIN.text, ACTOR.text]
    b = await Models(LEFT_OUT).answer()
    assert AGAIN.text in [s.text for s in b.prose]
    assert b.text.count("respectable widow") == 2
