"""COVERAGE (spec 2026-11-04, writer fidelity): no checked claim is lost
between the claims step and the shown answer. One repair round for the claims
the shown prose leaves uncited, then code places any still uncited; a claim the
check says a sentence restates is cited by that sentence instead."""
import re
from pathlib import Path

import pytest

from lore.query import synthesize as synth
from lore.query import write as w
from lore.query.models import Answer, AnswerSection, Claim, Evidence, Passage, ProseSentence
from lore.query.synthesize import Judgement, Synthesis, check_schema, faithfulness_prompt, synthesize
from lore.query.write import (
    Prose, Repair, Sentence, answer_coverage, cite_restated, coverage, place_uncited, writer_prompt,
)
from tests.test_query_story import BOOKS, CS, PS, TITLES

# Story order of CS: shown 1 = claim 2 (b1 ch1), shown 2 = claim 3 (b1 ch3),
# shown 3 = claim 1 (b2 ch5). The writer cites shown numbers.
EVIDENCE = Evidence(edges=(), passages=PS[:3], truncated=False)


def S(text, claims, new_paragraph=False, kind="fact"):
    return Sentence(text=text, claims=claims, new_paragraph=new_paragraph, kind=kind)


def _j(n, v="supported", **extra):
    return Judgement(claim=n, verdict=v, reason="r", **extra)


def sentences_in(prompt: str) -> int:
    return len(re.findall(r"^\d+\. sentence", prompt, re.M))


class Models:
    """Fake writer and check. `drafts`: the writer's replies in turn (the last
    repeats). `claim_verdicts`: per claim. `sentence_verdict(i, text)`: per
    sentence, "supported" unless it returns otherwise. `restated`: per claim."""

    def __init__(self, drafts, claim_verdicts=("supported",) * 3, sentence_verdict=None,
                 restated=None, inference=None):
        self.drafts, self.claim_verdicts = list(drafts), claim_verdicts
        self.sentence_verdict = sentence_verdict or (lambda i, text: "supported")
        self.inference = inference or (lambda i, text: False)
        self.restated = restated
        self.write_prompts, self.check_prompts = [], []

    async def generate(self, prompt, schema):
        return Synthesis(can_answer=True, claims=[
            {"text": c.text, "passage": c.passage, "quote": c.quote} for c in CS])

    async def write(self, prompt, schema):
        self.write_prompts.append(prompt)
        draft = self.drafts[min(len(self.write_prompts), len(self.drafts)) - 1]
        if isinstance(draft, Exception):
            raise draft
        return draft

    async def check(self, prompt, schema):
        self.check_prompts.append(prompt)
        texts = re.findall(r"^\d+\. sentence(?: \(\w+\))?: (.*)$", prompt, re.M)
        restated = list(self.restated or [0] * len(self.claim_verdicts))
        return schema.model_validate({
            "judgements": [{"claim": i, "verdict": v, "reason": "r", "restated_by": r}
                           for i, (v, r) in enumerate(zip(self.claim_verdicts, restated), start=1)],
            "sentence_judgements": [
                {"claim": i, "verdict": self.sentence_verdict(i, t), "reason": "r",
                 "inference_in_fact": self.inference(i, t)}
                for i, t in enumerate(texts, start=1)]})

    async def ask(self):
        return await synthesize("How did Rin grow?", EVIDENCE, self.generate, mode="graph",
                                check=self.check, write=self.write, book_titles=TITLES, books=BOOKS)


def cited(answer: Answer) -> set[int]:
    return {k for s in (*answer.prose, *answer.short_answer) for k in s.claims}


def assert_invariant(answer: Answer):
    """Every kept claim (Answer.claims are the check's survivors) is cited."""
    assert answer.answer_style == "prose"
    assert cited(answer) == set(range(1, len(answer.claims) + 1))
    assert coverage(answer.prose, len(answer.claims)) == 1.0


def test_the_switch_is_on_by_default():
    assert w.COVERAGE is True


def test_run_pair_has_a_coverage_arm():
    import importlib.util
    path = Path(__file__).parents[1] / "evals/answer_quality/run_pair.py"
    spec = importlib.util.spec_from_file_location("run_pair_coverage", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.parse_arm("coverage=off") == {("lore.query.write", "COVERAGE"): False}


# ---- the repair round ----------------------------------------------------------------------

async def test_a_claim_the_writer_left_out_gets_one_repair_round():
    first = Prose(sentences=[S("Rin met the old teacher.", [1]), S("Later Rin trained.", [3])])
    second = Prose(sentences=[S("Rin met the old teacher.", [1]),
                              S("Rin learned to read maps.", [2]), S("Later Rin trained.", [3])])
    m = Models([first, second])
    a = await m.ask()
    assert len(m.write_prompts) == 2 and len(m.check_prompts) == 2
    repair = m.write_prompts[1]
    assert w.REPAIR_LEAD in repair
    # the draft, with the writer's own (story-order) numbers, and the missing fact by its number
    assert "1. Rin met the old teacher. (facts 1)" in repair
    assert "2. Later Rin trained. (facts 3)" in repair
    assert "These facts are not in the draft yet: 2." in repair
    assert "Add only what is new" in repair
    assert [s.text for s in a.prose] == ["Rin met the old teacher.", "Rin learned to read maps.",
                                         "Later Rin trained."]
    assert_invariant(a)


async def test_no_repair_when_every_claim_is_cited():
    m = Models([Prose(sentences=[S("Rin met the old teacher.", [1]), S("He read maps.", [2]),
                                 S("He trained.", [3])])])
    a = await m.ask()
    assert len(m.write_prompts) == 1 and len(m.check_prompts) == 1
    assert_invariant(a)


async def test_a_repair_that_fails_its_check_leaves_the_first_draft_and_code_places_the_claim():
    first = Prose(sentences=[S("Rin met the old teacher.", [1]), S("Rin trained.", [3])])
    bad = Prose(sentences=[S("Rin met the teacher in secret.", [1]), S("Rin dreamed of maps.", [2]),
                           S("Rin trained hard for a war.", [3])])
    m = Models([first, bad], sentence_verdict=lambda i, t: (
        "not_supported" if ("secret" in t or "dreamed" in t or "war" in t) else "supported"))
    a = await m.ask()
    assert len(m.write_prompts) == 2
    assert "secret" not in a.text and "dreamed" not in a.text
    assert "Rin learned to read maps." in [s.text for s in a.prose]
    assert_invariant(a)


async def test_a_writer_error_in_the_repair_round_still_keeps_every_claim():
    first = Prose(sentences=[S("Rin met the old teacher.", [1]), S("Rin trained.", [3])])
    m = Models([first, RuntimeError("quota")])
    a = await m.ask()
    assert_invariant(a)


# ---- the deterministic placement ----------------------------------------------------------

async def test_a_claim_still_uncited_after_the_repair_is_placed_by_code():
    same = Prose(sentences=[S("Rin met the old teacher.", [1]), S("Rin trained.", [3])])
    m = Models([same, same])
    a = await m.ask()
    placed = [s for s in a.prose if s.text == "Rin learned to read maps."]
    assert len(placed) == 1 and placed[0].kind == "fact"
    assert placed[0].passages == (3,)
    assert_invariant(a)


def P(text, claims, passages, new_paragraph=False, kind="fact"):
    return ProseSentence(text=text, claims=tuple(claims), passages=tuple(passages),
                         new_paragraph=new_paragraph, kind=kind)


def test_placement_goes_to_the_end_of_the_paragraph_sharing_most_passages():
    prose = (P("A.", [1], [10]), P("B.", [2], [20]),
             P("C.", [3], [30], new_paragraph=True), P("D.", [4], [40]))
    out = place_uncited(prose, ["a", "b", "c", "d", "the gate was locked"], [10, 20, 30, 40, 30])
    assert [s.text for s in out] == ["A.", "B.", "C.", "D.", "The gate was locked."]
    out = place_uncited(prose, ["a", "b", "c", "d", "the gate was locked"], [10, 20, 30, 40, 20])
    assert [s.text for s in out] == ["A.", "B.", "The gate was locked.", "C.", "D."]
    assert out[2].claims == (5,) and out[2].passages == (20,) and not out[2].new_paragraph


def test_placement_with_no_shared_passage_goes_to_the_last_paragraph():
    prose = (P("A.", [1], [10]), P("C.", [2], [30], new_paragraph=True))
    out = place_uncited(prose, ["a", "c", "a bell rang"], [10, 30, 99])
    assert [s.text for s in out] == ["A.", "C.", "A bell rang."]


def test_placement_leaves_cited_claims_alone_and_keeps_a_claims_end_punctuation():
    prose = (P("A.", [1], [10]),)
    assert place_uncited(prose, ["a"], [10]) == prose
    out = place_uncited(prose, ["a", "Did it rain?"], [10, 10])
    assert out[-1].text == "Did it rain?"


# ---- drops by the check -------------------------------------------------------------------

async def test_a_sentence_the_check_rejects_does_not_take_its_claim_with_it():
    draft = Prose(sentences=[S("Rin met the old teacher.", [1]),
                             S("Rin learned maps because he feared the sea.", [2]),
                             S("Rin trained.", [3])])
    m = Models([draft], sentence_verdict=lambda i, t: "not_supported" if "feared" in t else "supported")
    a = await m.ask()
    assert "feared" not in a.text
    assert_invariant(a)
    # the repair saw only what passed, and asked for the rejected sentence's fact
    assert "feared" not in m.write_prompts[1] and "not in the draft yet: 2." in m.write_prompts[1]


async def test_a_claim_the_check_cuts_is_not_required():
    draft = Prose(sentences=[S("Rin met the old teacher.", [1]), S("Rin trained.", [3])])
    m = Models([draft], claim_verdicts=("supported", "supported", "not_supported"))
    a = await m.ask()
    assert len(a.claims) == 2 and "maps" not in a.text
    assert len(m.write_prompts) == 1  # nothing to repair
    assert_invariant(a)


# ---- duplicates ---------------------------------------------------------------------------

async def test_a_claim_the_check_says_a_sentence_restates_is_cited_by_it_not_repeated():
    # Shown fact 2 is claim 3 ("maps"); the second sentence says it without citing it.
    draft = Prose(sentences=[S("Rin met the old teacher.", [1]),
                             S("He trained, and he learned to read maps.", [3])])
    m = Models([draft], restated=(0, 0, 2))
    a = await m.ask()
    assert len(m.write_prompts) == 1  # covered: no repair, nothing placed
    assert len(a.prose) == 2 and a.text.count("maps") == 1
    assert set(a.prose[1].claims) == {1, 3}
    assert_invariant(a)


async def test_restated_by_a_rejected_sentence_does_not_count():
    draft = Prose(sentences=[S("Rin met the old teacher.", [1]),
                             S("He trained for a war and read maps.", [3])])
    second = Prose(sentences=[S("Rin met the old teacher.", [1]), S("He learned to read maps.", [2]),
                              S("He trained.", [3])])
    m = Models([draft, second], restated=(0, 0, 2),
               sentence_verdict=lambda i, t: "not_supported" if "war" in t else "supported")
    a = await m.ask()
    assert len(m.write_prompts) == 2
    assert_invariant(a)


def test_cite_restated_unit():
    ss = [S("x", [1]), S("y", [2])]
    out = cite_restated(ss, [True, True, True], [True, True], [0, 0, 1])
    assert out[0].claims == [1, 3] and out[1].claims == [2]
    # not when the sentence failed, the claim was cut, or it is already cited
    assert cite_restated(ss, [True, True, True], [False, True], [0, 0, 1])[0].claims == [1]
    assert cite_restated(ss, [True, True, False], [True, True], [0, 0, 1])[0].claims == [1]
    assert cite_restated(ss, [True, True], [True, True], [2, 0])[1].claims == [2]


def test_the_check_asks_for_restated_by_only_with_coverage_on(monkeypatch):
    sentences = [S("Rin met the old teacher.", [2])]
    prompt = faithfulness_prompt(CS, PS, TITLES, sentences, BOOKS)
    assert "restated_by" in prompt
    assert "restated_by" in check_schema(True).model_json_schema()["$defs"]["RestatedJudgement"]["properties"]
    assert check_schema(False) is synth.Faithfulness
    monkeypatch.setattr(w, "COVERAGE", False)
    assert "restated_by" not in faithfulness_prompt(CS, PS, TITLES, sentences, BOOKS)
    assert "restated_by" not in str(check_schema(True).model_json_schema())
    monkeypatch.setattr(w, "FACT_FIDELITY", False)
    assert check_schema(True) is synth.Faithfulness


async def test_a_restated_number_out_of_range_is_ignored():
    draft = Prose(sentences=[S("Rin met the old teacher.", [1]), S("Rin trained.", [3])])
    m = Models([draft, draft], restated=(0, 0, 9))
    a = await m.ask()
    assert_invariant(a)
    assert "Rin learned to read maps." in a.text


# ---- switch off ---------------------------------------------------------------------------

async def test_switch_off_no_repair_and_no_placement(monkeypatch):
    monkeypatch.setattr(w, "COVERAGE", False)
    draft = Prose(sentences=[S("Rin met the old teacher.", [1]), S("Rin trained.", [3])])
    m = Models([draft])
    a = await m.ask()
    assert len(m.write_prompts) == 1 and len(m.check_prompts) == 1
    assert "maps" not in a.text
    assert coverage(a.prose, len(a.claims)) == round(2 / 3, 3)


def test_a_short_answer_is_exempt():
    import inspect
    assert '"short", "recap_short"' in inspect.getsource(synth._finish)


# ---- the metric ---------------------------------------------------------------------------

def test_coverage_metric():
    prose = (P("A.", [1, 2], [1]), P("B.", [2], [1]))
    assert coverage(prose, 4) == 0.5
    assert coverage((), 4) is None and coverage(prose, 0) is None


def test_answer_coverage_for_prose_claims_and_sectioned_answers():
    claims = tuple(Claim(text=f"c{i}", passage=1, quote="q") for i in range(4))
    base = dict(text="t", passages=(), mode="graph", template=None, note=None, claims=claims)
    assert answer_coverage(Answer(**base)) is None
    a = Answer(**base, answer_style="prose", prose=(P("A.", [1, 2, 3], [1]),),
               short_answer=(P("S.", [4], [1]),))
    assert answer_coverage(a) == 1.0
    sections = (
        AnswerSection(heading="h1", prose=(P("A.", [1], [1]),), text="", claims_start=1, claims_end=2),
        AnswerSection(heading="h2", prose=(), text="claims", claims_start=3, claims_end=3, style="claims"),
        AnswerSection(heading="close", prose=(P("R.", [4], [1], kind="reading"),), text="",
                      claims_start=0, claims_end=-1, kind="close"),
    )
    assert answer_coverage(Answer(**base, answer_style="sectioned", sections=sections)) == 0.5


def test_the_search_result_carries_coverage():
    from lore.mcp.shapes import to_search_result
    claims = (Claim(text="c", passage=1, quote="q"), Claim(text="d", passage=1, quote="q"))
    a = Answer(text="t", passages=(), mode="graph", template=None, note=None, claims=claims,
               answer_style="prose", prose=(P("A.", [1], [1]),))
    assert to_search_result(a, {}).coverage == 0.5


def test_repair_block_numbers_follow_the_prompts_story_order():
    repair = Repair(draft=(S("Rin trained.", [1]), S("He met him.", [2], new_paragraph=True)),
                    missing=(3,))
    prompt = writer_prompt("q", [], CS, PS, TITLES, books=BOOKS, repair=repair)
    tail = prompt.split(w.REPAIR_LEAD, 1)[1]
    assert "1. Rin trained. (facts 3)" in tail
    assert "2. (new paragraph) He met him. (facts 1)" in tail
    assert "not in the draft yet: 2." in tail
    assert w.REPAIR_LEAD not in writer_prompt("q", [], CS, PS, TITLES, books=BOOKS)


async def test_a_draft_that_falls_back_gets_no_repair_round():
    draft = Prose(sentences=[S("Rin met the old teacher in secret.", [1]),
                             S("Rin trained for a war.", [3])])
    m = Models([draft], sentence_verdict=lambda i, t: "not_supported")
    a = await m.ask()
    assert a.answer_style == "claims" and len(m.write_prompts) == 1
    assert len(a.claims) == 3  # the claim list shows every kept claim
