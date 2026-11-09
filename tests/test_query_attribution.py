"""ATTRIBUTION_FIDELITY (2026-11-09): a written sentence keeps who did what to
whom, and how often.

A blind held-out eval (h9) found prose that gave a message sent to one person
to another, and turned one question into a repeated one. The writer is told to
keep actors, recipients, speakers and frequency; the claim check and the
sentence check refuse a swap or an unstated repetition. The fixture is a
made-up story of that shape.
"""
import re
from pathlib import Path

from lore.query import synthesize as synth
from lore.query import write as w
from lore.query.models import BookInfo, Claim, Evidence, Passage
from lore.query.synthesize import Synthesis, faithfulness_prompt, synthesize
from lore.query.write import Prose, Sentence

PS = (Passage(n=1, chunk_id="b:1:0", book_id="b", chapter_seq=1,
              text="Corin sent a note to the warden: \"Go to the mill and wait for me.\" "
                   "Mara stayed at the inn."),
      Passage(n=2, chunk_id="b:1:1", book_id="b", chapter_seq=1,
              text="At the bridge Corin asked Pell once whether the ferryman had seen the boat, "
                   "and Pell said no."))
CLAIMS = [Claim(text="Corin's note told the warden to go to the mill and wait.", passage=1,
                quote="Go to the mill and wait for me"),
          Claim(text="Corin asked Pell whether the ferryman had seen the boat; Pell said no.",
                passage=2, quote="Corin asked Pell once whether the ferryman had seen the boat")]
TITLES = {"b": "The Lantern"}
BOOKS = {"b": BookInfo("The Lantern")}
SWAPPED = "Corin's note told Mara to go to the mill and wait."
REPEATED = "Corin later asked Pell again whether the ferryman had seen the boat."
FAITHFUL = "Corin's note sent the warden to the mill to wait for him."
ASKED = "At the bridge Corin asked Pell whether the ferryman had seen the boat, and Pell said no."


def test_the_switch_is_on_and_has_an_arm():
    assert synth.ATTRIBUTION_FIDELITY is True
    import importlib.util
    path = Path(__file__).parents[1] / "evals/answer_quality/run_pair.py"
    spec = importlib.util.spec_from_file_location("run_pair_attribution", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.parse_arm("attribution_fidelity=false") == {
        ("lore.query.synthesize", "ATTRIBUTION_FIDELITY"): False}


def test_the_writer_rule(monkeypatch):
    on = w.writer_prompt("What did Corin do?", [], CLAIMS, PS, TITLES, BOOKS)
    assert w.ATTRIBUTION_RULE in on
    assert "who did what to" in w.ATTRIBUTION_RULE and '"again"' in w.ATTRIBUTION_RULE
    section = w.writer_prompt("What did Corin do?", [], CLAIMS, PS, TITLES, BOOKS, length="section")
    assert w.ATTRIBUTION_RULE in section
    monkeypatch.setattr(synth, "ATTRIBUTION_FIDELITY", False)
    off = w.writer_prompt("What did Corin do?", [], CLAIMS, PS, TITLES, BOOKS)
    assert off == on.replace(w.ATTRIBUTION_RULE, "")


def test_the_check_rules(monkeypatch):
    sents = [Sentence(text=SWAPPED, claims=[1])]
    on = faithfulness_prompt(CLAIMS, PS, TITLES, sentences=sents, books=BOOKS)
    assert synth._ATTRIBUTION_CHECK in on
    claim_rule = synth._ATTRIBUTION_CLAIM_CHECK.format(not_supported=synth.NOT_SUPPORTED)
    assert claim_rule in on
    # next to STRICT_LINKS: the claim rule follows it, the sentence rule is a
    # not_supported case of the sentence block
    assert on.index(synth._STRICT_LINKS_CHECK.format(not_supported=synth.NOT_SUPPORTED)) < on.index(claim_rule)
    block = on.split("Then check each numbered sentence")[1]
    assert block.index('"not_supported": the sentence') < block.index("attributes an action")
    monkeypatch.setattr(synth, "ATTRIBUTION_FIDELITY", False)
    off = faithfulness_prompt(CLAIMS, PS, TITLES, sentences=sents, books=BOOKS)
    assert off == on.replace(synth._ATTRIBUTION_CHECK, "").replace(claim_rule, "")


class Models:
    """A fake checker that applies the rule as a careful model would: a
    sentence naming someone its claims do not give that role to, or saying
    "again" that no claim says, is not_supported."""

    def __init__(self, draft):
        self.draft = draft
        self.check_prompts = []

    async def generate(self, prompt, schema):
        return Synthesis(can_answer=True, claims=[
            {"text": c.text, "passage": c.passage, "quote": c.quote} for c in CLAIMS])

    async def write(self, prompt, schema):
        return self.draft

    async def check(self, prompt, schema):
        self.check_prompts.append(prompt)
        rule = synth._ATTRIBUTION_CHECK in prompt
        claims = re.findall(r"^\d+\. claim: (.*)$", prompt, re.M)
        texts = re.findall(r"^\d+\. sentence(?: \(\w+\))?: (.*)$", prompt, re.M)

        def verdict(text):
            bad = rule and ("told Mara" in text or " again" in text)
            return "not_supported" if bad else "supported"

        return schema.model_validate({
            "judgements": [{"claim": i, "verdict": "supported", "reason": "r", "restated_by": 0}
                           for i in range(1, len(claims) + 1)],
            "sentence_judgements": [{"claim": i, "verdict": verdict(t), "reason": "r",
                                     "inference_in_fact": False}
                                    for i, t in enumerate(texts, start=1)]})

    async def answer(self):
        return await synthesize("What did Corin do?", Evidence(edges=(), passages=PS,
                                                               truncated=False),
                                self.generate, mode="graph", check=self.check, write=self.write,
                                book_titles=TITLES, books=BOOKS)


async def test_a_swapped_recipient_is_rejected_and_a_faithful_paraphrase_passes():
    bad = Models(Prose(sentences=[Sentence(text=SWAPPED, claims=[1]),
                                  Sentence(text=REPEATED, claims=[2])]))
    a = await bad.answer()
    assert SWAPPED not in a.text and REPEATED not in a.text
    good = Models(Prose(sentences=[Sentence(text=FAITHFUL, claims=[1]),
                                   Sentence(text=ASKED, claims=[2])]))
    b = await good.answer()
    assert b.answer_style == "prose" and [s.text for s in b.prose] == [FAITHFUL, ASKED]
