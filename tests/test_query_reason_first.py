"""REASON_FIRST and VERDICT_GUARD (2026-11-09).

A production trace (Claude Sonnet as checker) rejected four of eight claims
while its own reasons said they were stated ("...; fully stated"): asked for
claim, verdict, reason in that order, the model committed to a verdict before
reasoning. The check now asks for the reason first, and a rejection whose
reason affirms the claim and negates nothing is asked again, once.

The fixtures are made up."""
import re
from pathlib import Path

from lore.query import synthesize as synth
from lore.query.models import BookInfo, Claim, Evidence, Passage
from lore.query.synthesize import (
    IntroCheck, Synthesis, check_answer, check_schema, faithfulness_prompt, inconsistent_items,
    inconsistent_verdict, intro_check_prompt, synthesize,
)
from lore.query.write import Prose, Sentence

PS = (Passage(n=1, chunk_id="b:1:0", book_id="b", chapter_seq=1,
              text="Mara met Corin at the mill at noon. She thanked him for the loan and "
                   "asked about his journey."),)
CLAIMS = [Claim(text="Mara met Corin at the mill.", passage=1, quote="Mara met Corin at the mill"),
          Claim(text="They met at noon, and Mara thanked Corin for the loan.", passage=1,
                quote="She thanked him for the loan"),
          Claim(text="Mara asked about his journey because she feared for him.", passage=1,
                quote="asked about his journey")]
TITLES = {"b": "The Mill"}
BOOKS = {"b": BookInfo("The Mill")}


def _run_pair():
    import importlib.util
    path = Path(__file__).parents[1] / "evals/answer_quality/run_pair.py"
    spec = importlib.util.spec_from_file_location("run_pair_reason_first", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_switches_are_on_and_have_arms():
    assert synth.REASON_FIRST is True and synth.VERDICT_GUARD is True
    module = _run_pair()
    assert module.parse_arm("reason_first=false") == {
        ("lore.query.synthesize", "REASON_FIRST"): False}
    assert module.parse_arm("verdict_guard=false") == {
        ("lore.query.synthesize", "VERDICT_GUARD"): False}


# ---- the schema --------------------------------------------------------------------------

def _orders(schema):
    defs = schema.model_json_schema()["$defs"]
    return ({name: list(d["properties"]) for name, d in defs.items()},
            {name: d.get("required") for name, d in defs.items()})


def test_with_the_switch_on_every_judgement_gives_its_reason_before_its_verdict():
    for schema in (check_schema(False), check_schema(True), check_schema(True, True), IntroCheck):
        props, required = _orders(schema)
        for name, fields in props.items():
            if "verdict" in fields:
                assert fields[:3] == ["claim", "reason", "verdict"], (name, fields)
                assert "reason" in required[name]
    props, _ = _orders(check_schema(True, True))
    assert props["FlaggedOpenerJudgement"] == ["claim", "reason", "verdict", "inference_in_fact",
                                               "supported_by"]


def test_with_the_switch_off_the_schema_is_as_before(monkeypatch):
    monkeypatch.setattr(synth, "REASON_FIRST", False)
    props, required = _orders(check_schema(True, True))
    assert props == {"FlaggedOpenerJudgement": ["claim", "verdict", "reason", "inference_in_fact",
                                                "supported_by"],
                     "Judgement": ["claim", "verdict", "reason"],
                     "RestatedJudgement": ["claim", "verdict", "reason", "restated_by"]}
    assert all(r == ["claim", "verdict"] for r in required.values())


def test_a_reply_validates_whatever_its_field_order():
    reply = {"judgements": [{"claim": 1, "verdict": "supported"}]}
    assert check_schema(False).model_validate(reply).judgements[0].reason == ""


# ---- the prompt --------------------------------------------------------------------------

def test_the_prompt_asks_for_the_reason_first_and_longer(monkeypatch):
    sents = [Sentence(text="Mara met Corin at the mill.", claims=[1])]
    on = faithfulness_prompt(CLAIMS, PS, TITLES, sents, BOOKS, gaps=["why"])
    assert on.index("- reason: at most 20 words, written first") < on.index('- verdict: "supported"')
    assert on.count("with a reason of at most 20 words, written first") == 2  # sentences, gaps
    assert "with a verdict and a reason" not in on and "12 words" not in on
    intro = intro_check_prompt(CLAIMS, PS, sents, TITLES, BOOKS)
    assert "with a reason of at most 20 words, written first" in intro
    monkeypatch.setattr(synth, "REASON_FIRST", False)
    off = faithfulness_prompt(CLAIMS, PS, TITLES, sents, BOOKS, gaps=["why"])
    assert '- verdict: "supported" or "not_supported"\n- reason: at most 12 words.\n' in off
    assert "with a verdict and a reason of at most\n12 words." in off
    assert "claim field, with a verdict and a reason of at most 12 words." in off
    assert "written first" not in off
    intro_off = intro_check_prompt(CLAIMS, PS, sents, TITLES, BOOKS)
    assert ('with a verdict ("supported" or "not_supported"), a reason\nof at most 12 words, '
            "and supported_by.") in intro_off


# ---- the guard ---------------------------------------------------------------------------

def test_what_counts_as_inconsistent():
    assert inconsistent_verdict("not_supported", "Passage states noon meeting and thanks; fully stated")
    assert inconsistent_verdict("not_supported", "'set off' stated: supported actually")
    assert inconsistent_verdict("Not supported", "Matches claim 2.")
    assert not inconsistent_verdict("supported", "fully stated")
    assert not inconsistent_verdict("not_supported", "Adds a motive the passage does not state.")
    assert not inconsistent_verdict("not_supported", "Passage says one student, but not that it was her")
    assert not inconsistent_verdict("not_supported", "Stated only for the brother.")
    assert not inconsistent_verdict("not_supported", "The passage doesn't name the mill.")
    assert not inconsistent_verdict("not_supported", "The cause goes beyond what is stated.")
    assert not inconsistent_verdict("not_supported", "")
    assert not inconsistent_verdict("not_supported", "not_supported")


def J(n, verdict, reason="", **more):
    return {"claim": n, "verdict": verdict, "reason": reason, **more}


class Checker:
    """A fake check: its replies in turn (the last repeats), and the prompts it saw."""

    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    async def __call__(self, prompt, schema):
        self.prompts.append(prompt)
        reply = self.replies[min(len(self.prompts), len(self.replies)) - 1]
        return schema.model_validate(reply(prompt) if callable(reply) else reply)


def claims_in(prompt):
    return re.findall(r"^\d+\. claim: (.*)$", prompt, re.M)


FIRST = {"judgements": [J(1, "supported", "Stated."),
                        J(2, "not_supported", "Passage states the noon meeting and thanks; fully stated"),
                        J(3, "not_supported", "Adds a motive the passage does not state.")]}


def everything_supported(prompt):
    return {"judgements": [J(i, "supported", "Stated.")
                           for i in range(1, len(claims_in(prompt)) + 1)]}


async def test_the_guard_reasks_only_the_inconsistent_claims_and_takes_a_supported():
    check = Checker(FIRST, everything_supported)
    out = await check_answer(CLAIMS, PS, check, book_titles=TITLES, books=BOOKS)
    assert len(check.prompts) == 2
    again = check.prompts[1]
    assert again.startswith(synth.REASK_LEAD)
    assert claims_in(again) == [CLAIMS[1].text]
    assert out.claim_ok == [True, True, False]  # the real embellishment still fails
    assert [c for c, _ in out.dropped] == [CLAIMS[2]]


async def test_a_reask_that_rejects_again_keeps_the_rejection():
    still = {"judgements": [J(1, "not_supported", "Thanks stated; fully stated")]}
    check = Checker(FIRST, still)
    out = await check_answer(CLAIMS, PS, check, book_titles=TITLES, books=BOOKS)
    assert len(check.prompts) == 2 and out.claim_ok == [True, False, False]


async def test_a_failed_reask_keeps_the_first_verdicts():
    async def check(prompt, schema):
        check.calls += 1
        if check.calls > 1:
            raise RuntimeError("quota")
        return schema.model_validate(FIRST)
    check.calls = 0
    out = await check_answer(CLAIMS, PS, check, book_titles=TITLES, books=BOOKS)
    assert out.ran and out.claim_ok == [True, False, False]


async def test_consistent_rejections_are_not_reasked():
    consistent = {"judgements": [J(1, "supported", "Stated."),
                                 J(2, "not_supported", "The passage never says noon."),
                                 J(3, "not_supported", "Adds a motive the passage does not state.")]}
    check = Checker(consistent)
    out = await check_answer(CLAIMS, PS, check, book_titles=TITLES, books=BOOKS)
    assert len(check.prompts) == 1 and out.claim_ok == [True, False, False]


async def test_the_spend_check_comes_before_the_reask():
    check = Checker(FIRST, everything_supported)
    out = await check_answer(CLAIMS, PS, check, book_titles=TITLES, books=BOOKS,
                             spend_ok=lambda: False)
    assert len(check.prompts) == 1 and out.claim_ok == [True, False, False]


async def test_switched_off_there_is_no_reask(monkeypatch):
    monkeypatch.setattr(synth, "VERDICT_GUARD", False)
    check = Checker(FIRST, everything_supported)
    out = await check_answer(CLAIMS, PS, check, book_titles=TITLES, books=BOOKS)
    assert len(check.prompts) == 1 and out.claim_ok == [True, False, False]


SENTS = [Sentence(text="Mara met Corin at the mill.", claims=[1]),
         Sentence(text="They met at noon, and she thanked him for the loan.", claims=[2])]


def with_sentences(prompt):
    n = len(claims_in(prompt))
    texts = re.findall(r"^\d+\. sentence(?: \(\w+\))?: (.*)$", prompt, re.M)
    if n == len(CLAIMS):  # the first call: sentence 2 rejected with a reason that agrees
        return {"judgements": [J(i, "supported", "Stated.", restated_by=0) for i in range(1, n + 1)],
                "sentence_judgements": [J(1, "supported", "Matches claim 1.", inference_in_fact=False),
                                        J(2, "not_supported", "Matches claim 2.",
                                          inference_in_fact=False)]}
    return {"judgements": [J(i, "supported", "Stated.", restated_by=0) for i in range(1, n + 1)],
            "sentence_judgements": [J(i, "supported", "Matches.", inference_in_fact=False)
                                    for i in range(1, len(texts) + 1)]}


async def test_an_inconsistent_sentence_is_reasked_with_its_own_claims():
    check = Checker(with_sentences)
    out = await check_answer(CLAIMS, PS, check, book_titles=TITLES, books=BOOKS, sentences=SENTS)
    assert len(check.prompts) == 2
    again = check.prompts[1]
    assert claims_in(again) == [CLAIMS[1].text]
    assert re.findall(r"^\d+\. sentence: (.*)$", again, re.M) == [SENTS[1].text]
    assert "uses claims 1" in again
    assert out.sentence_ok == [True, True]


def test_the_inconsistent_items_of_a_reply():
    reply = check_schema(True).model_validate(with_sentences("\n".join(
        f"{i}. claim: x" for i in range(1, 4))))
    assert inconsistent_items(reply, SENTS) == ([], [1])
    assert inconsistent_items(reply, None) == ([], [])


# ---- in an answer ------------------------------------------------------------------------

class Models:
    def __init__(self, check):
        self.check = check

    async def generate(self, prompt, schema):
        return Synthesis(can_answer=True, claims=[
            {"text": c.text, "passage": c.passage, "quote": c.quote} for c in CLAIMS])

    async def write(self, prompt, schema):
        return Prose(sentences=[Sentence(text=c.text, claims=[i])
                                for i, c in enumerate(CLAIMS, start=1)])


def answer_check(first_reason):
    def reply(prompt):
        n = len(claims_in(prompt))
        texts = re.findall(r"^\d+\. sentence(?: \(\w+\))?: (.*)$", prompt, re.M)
        lead = prompt.startswith(synth.REASK_LEAD)

        def verdict(text):
            if "because she feared" in text:
                return "not_supported", "Adds a motive the passage does not state."
            if "noon" in text and not lead:
                return "not_supported", first_reason
            return "supported", "Stated."

        return {"judgements": [dict(zip(("verdict", "reason"), verdict(t)), claim=i, restated_by=0)
                               for i, t in enumerate(claims_in(prompt), start=1)][:n],
                "sentence_judgements": [dict(zip(("verdict", "reason"), verdict(t)), claim=i,
                                             inference_in_fact=False)
                                        for i, t in enumerate(texts, start=1)]}
    return reply


async def _ask(check):
    m = Models(check)
    return await synthesize("When did Mara meet Corin?", Evidence(edges=(), passages=PS,
                                                                  truncated=False),
                            m.generate, mode="graph", check=check, write=m.write,
                            book_titles=TITLES, books=BOOKS)


async def test_an_answer_keeps_a_claim_its_check_wrongly_rejected_and_drops_the_embellishment():
    check = Checker(answer_check("Passage states the noon meeting and the thanks; fully stated"))
    a = await _ask(check)
    texts = [c.text for c in a.claims]
    assert CLAIMS[1].text in texts and CLAIMS[2].text not in texts
    assert sum(p.startswith(synth.REASK_LEAD) for p in check.prompts) == 1


async def test_a_consistent_rejection_in_an_answer_is_not_reasked():
    check = Checker(answer_check("The passage never says they met at noon."))
    a = await _ask(check)
    assert CLAIMS[1].text not in [c.text for c in a.claims]
    assert not any(p.startswith(synth.REASK_LEAD) for p in check.prompts)
