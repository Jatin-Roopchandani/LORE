"""FACT_FIDELITY (spec 2026-11-04, writer fidelity): readings only in labelled
sentences. A fact sentence says only what its cited claims say; the check flags
one that states an interpretation (inference_in_fact); the repair round reduces
it to its claims' wording, or makes it a labelled reading where the answer
allows readings; an answer with no interpretive part has no readings at all.

Fake models, a made-up story, no network."""
import re
from pathlib import Path

import pytest

from lore.query import interpret
from lore.query import synthesize as synth
from lore.query import write as w
from lore.query.decompose import retrieval_parts
from lore.query.models import Evidence
from lore.query.synthesize import (
    Synthesis, check_answer, check_schema, faithfulness_prompt, synthesize, synthesize_parts,
)
from lore.query.write import Prose, Sentence, writer_prompt
from tests.test_query_interpret import LENS_CLAIMS, PLAN, SHAPE
from tests.test_query_story import BOOKS, CS, PS, TITLES

EVIDENCE = Evidence(edges=(), passages=PS[:3], truncated=False)


def S(text, claims, kind="fact", new_paragraph=False):
    return Sentence(text=text, claims=claims, kind=kind, new_paragraph=new_paragraph)


class Models:
    """Claims step, writer and check in one, routed by the schema. `drafts`:
    the writer's replies in turn (the last repeats). `flag(text)`: whether the
    check marks a sentence inference_in_fact. `reject(text)`: not_supported."""

    def __init__(self, drafts, flag=None, reject=None, lens_claims=None):
        self.drafts = list(drafts)
        self.flag = flag or (lambda text: False)
        self.reject = reject or (lambda text: False)
        self.lens_claims = lens_claims
        self.write_prompts, self.check_prompts, self.write_schemas = [], [], []

    async def __call__(self, prompt, schema):
        if schema is Synthesis:
            if self.lens_claims is None:
                return Synthesis(can_answer=True, claims=[
                    {"text": c.text, "passage": c.passage, "quote": c.quote} for c in CS])
            for question, claims in self.lens_claims.items():
                if f"Question: {question}" in prompt:
                    return Synthesis(can_answer=True, claims=claims)
            return Synthesis(can_answer=False, claims=[])
        if schema in (w.Prose, w._StoryProse):
            self.write_prompts.append(prompt)
            self.write_schemas.append(schema)
            draft = self.drafts[min(len(self.write_prompts), len(self.drafts)) - 1]
            if isinstance(draft, Exception):
                raise draft
            return draft
        self.check_prompts.append(prompt)
        n = prompt.count("\n   cites passage")
        texts = re.findall(r"^\d+\. sentence(?: \(\w+\))?: (.*)$", prompt, re.M)
        return schema.model_validate({
            "judgements": [{"claim": i, "verdict": "supported", "reason": "r"}
                           for i in range(1, n + 1)],
            "sentence_judgements": [
                {"claim": i, "verdict": "not_supported" if self.reject(t) else "supported",
                 "reason": "r", "inference_in_fact": self.flag(t)}
                for i, t in enumerate(texts, start=1)]})

    async def ask(self):
        return await synthesize("How did Rin grow?", EVIDENCE, self, mode="graph", check=self,
                                write=self, book_titles=TITLES, books=BOOKS)

    async def ask_interpretive(self):
        rparts = retrieval_parts(PLAN)
        return await synthesize_parts(
            "Was Rin right to leave?", [r.question for r in rparts], EVIDENCE, self,
            mode="graph", book_titles=TITLES, books=BOOKS, check=self, write=self,
            plan=PLAN, retrieval=rparts)


def test_the_switch_is_on_by_default_and_prose_factual_stays_off():
    assert w.FACT_FIDELITY is True
    assert w.PROSE_FACTUAL is True  # pinned on by tests/conftest.py for these tests


@pytest.mark.factual_default
def test_prose_factual_ships_on():
    assert w.PROSE_FACTUAL is True


def test_run_pair_has_a_fact_fidelity_arm():
    import importlib.util
    path = Path(__file__).parents[1] / "evals/answer_quality/run_pair.py"
    spec = importlib.util.spec_from_file_location("run_pair_fidelity", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.parse_arm("fact_fidelity=off") == {("lore.query.write", "FACT_FIDELITY"): False}


# ---- the writer prompt ----------------------------------------------------------------------

def test_the_writer_prompt_carries_the_fact_rule_with_a_made_up_contrast():
    prompt = writer_prompt("How did Rin grow?", [], CS, PS, TITLES, books=BOOKS)
    assert w.FACT_RULE in prompt
    flat = " ".join(w.FACT_RULE.split())
    assert "says only what its cited facts say" in flat
    assert "motive, intention, judgement or evaluation" in flat
    assert '"Mara locked the lighthouse door and kept the key", not' in flat


def test_a_factual_answer_is_told_it_has_no_readings_and_gets_no_reading_rule():
    prompt = writer_prompt("How did Rin grow?", [], CS, PS, TITLES, books=BOOKS)
    assert w.NO_READINGS_RULE in prompt and w._READINGS_RULE not in prompt
    assert w.READING_HOME_RULE not in prompt


def test_an_interpretive_answer_is_told_where_an_interpretation_goes():
    prompt = writer_prompt("q", list(SHAPE.parts), CS, PS, TITLES, books=BOOKS, shape=SHAPE)
    assert w.READING_HOME_RULE in prompt and w.FACT_RULE in prompt
    assert w.NO_READINGS_RULE not in prompt


def test_switch_off_the_writer_prompt_has_no_fidelity_rules(monkeypatch):
    monkeypatch.setattr(w, "FACT_FIDELITY", False)
    prompt = writer_prompt("How did Rin grow?", [], CS, PS, TITLES, books=BOOKS)
    assert w.FACT_RULE not in prompt and w.NO_READINGS_RULE not in prompt
    assert w._READINGS_RULE in prompt


# ---- no readings in factual-only answers ---------------------------------------------------

async def test_a_factual_answer_asks_for_no_kinds_and_shows_no_reading():
    draft = Prose(sentences=[S("Rin met the old teacher.", [1]), S("He read maps.", [2]),
                             S("He trained.", [3]),
                             S("This suggests a growing commitment.", [1, 3], "reading")])
    m = Models([draft])
    a = await m.ask()
    assert m.write_schemas[0] is w._StoryProse
    assert a.answer_style == "prose"
    assert all(s.kind == "fact" for s in a.prose)
    assert "(reading)" not in m.check_prompts[0]


async def test_switch_off_a_factual_answer_may_carry_a_reading(monkeypatch):
    monkeypatch.setattr(w, "FACT_FIDELITY", False)
    draft = Prose(sentences=[S("Rin met the old teacher.", [1]), S("He read maps.", [2]),
                             S("He trained.", [3]),
                             S("This suggests a growing commitment.", [1, 3], "reading")])
    m = Models([draft])
    a = await m.ask()
    assert [s.kind for s in a.prose] == ["fact", "fact", "fact", "reading"]


# ---- the check ------------------------------------------------------------------------------

def test_the_check_asks_for_inference_in_fact_only_with_the_switch(monkeypatch):
    sentences = [S("Rin met the old teacher.", [2])]
    prompt = faithfulness_prompt(CS, PS, TITLES, sentences, BOOKS)
    assert "inference_in_fact" in prompt and "a motive, an intention, a judgement" in prompt
    schema = check_schema(True).model_json_schema()
    assert "inference_in_fact" in schema["$defs"]["FlaggedJudgement"]["properties"]
    assert "inference_in_fact" not in faithfulness_prompt(CS, PS, TITLES, books=BOOKS)
    monkeypatch.setattr(w, "FACT_FIDELITY", False)
    assert "inference_in_fact" not in faithfulness_prompt(CS, PS, TITLES, sentences, BOOKS)
    assert "inference_in_fact" not in str(check_schema(True).model_json_schema())


async def test_check_answer_returns_the_flags_and_only_a_true_flag_counts():
    async def check(prompt, schema):
        return schema.model_validate({
            "judgements": [{"claim": i, "verdict": "supported"} for i in (1, 2, 3)],
            "sentence_judgements": [{"claim": 1, "verdict": "supported", "inference_in_fact": True},
                                    {"claim": 2, "verdict": "supported"}]})
    out = await check_answer(CS, PS, check, sentences=[S("a", [1]), S("b", [2])])
    assert out.inference == [True, False]


# ---- an inference in a fact: reduced in the repair round ----------------------------------

DRAFT = Prose(sentences=[S("Rin met the old teacher.", [1]),
                         S("Rin learned maps to escape his village.", [2]),
                         S("Rin trained.", [3])])
REDUCED = Prose(sentences=[S("Rin met the old teacher.", [1]), S("Rin learned to read maps.", [2]),
                           S("Rin trained.", [3])])


def flags_escape(text):
    return "escape" in text


async def test_an_inference_in_a_fact_is_reduced_in_the_repair_round():
    m = Models([DRAFT, REDUCED], flag=flags_escape)
    a = await m.ask()
    assert len(m.write_prompts) == 2 and len(m.check_prompts) == 2
    repair = m.write_prompts[1]
    assert "2. Rin learned maps to escape his village. (facts 2)" in repair
    assert "Draft sentences 2 state an interpretation their facts do not" in " ".join(repair.split())
    assert w.AS_READING_RULE not in repair  # a factual answer has no readings
    assert "escape" not in a.text and "Rin learned to read maps." in a.text
    assert all(s.kind == "fact" for s in a.prose)
    assert {k for s in a.prose for k in s.claims} == {1, 2, 3}


async def test_a_repair_that_still_states_the_inference_drops_it_and_keeps_its_claim():
    m = Models([DRAFT, DRAFT], flag=flags_escape)
    a = await m.ask()
    assert len(m.write_prompts) == 2
    assert "escape" not in a.text
    # its claim is placed by code, in the claim's own words
    assert "Rin learned to read maps." in [s.text for s in a.prose]
    assert {k for s in a.prose for k in s.claims} == {1, 2, 3}


async def test_a_failed_repair_round_still_hides_the_inference():
    m = Models([DRAFT, RuntimeError("quota")], flag=flags_escape)
    a = await m.ask()
    assert a.answer_style == "prose" and "escape" not in a.text
    assert {k for s in a.prose for k in s.claims} == {1, 2, 3}


async def test_a_hidden_inference_does_not_count_against_the_prose():
    # Two of three sentences flagged and the repair fails: the prose still stands
    # on its facts (code places them) rather than falling back.
    draft = Prose(sentences=[S("Rin bravely met the old teacher.", [1]),
                             S("Rin learned maps to escape his village.", [2]), S("Rin trained.", [3])])
    m = Models([draft, RuntimeError("quota")], flag=lambda t: "bravely" in t or "escape" in t)
    a = await m.ask()
    assert a.answer_style == "prose" and "bravely" not in a.text and "escape" not in a.text
    assert {k for s in a.prose for k in s.claims} == {1, 2, 3}


async def test_switch_off_an_inference_flag_is_ignored(monkeypatch):
    monkeypatch.setattr(w, "FACT_FIDELITY", False)
    m = Models([DRAFT], flag=flags_escape)
    a = await m.ask()
    assert len(m.write_prompts) == 1 and "escape" in a.text


async def test_with_coverage_off_the_inference_is_still_repaired(monkeypatch):
    monkeypatch.setattr(w, "COVERAGE", False)
    m = Models([DRAFT, REDUCED], flag=flags_escape)
    a = await m.ask()
    assert len(m.write_prompts) == 2 and "escape" not in a.text
    assert "not in the draft yet" not in m.write_prompts[1]


# ---- an inference in a fact: a labelled reading where the answer allows one -----------------

# Shown to the writer (interpret.py): lens 1 claims 1 and 2 in story order, then lens 2.
INTERP_DRAFT = Prose(sentences=[
    S("Rin met a teacher.", [1]), S("Rin trained with the sword to prove his worth.", [2]),
    S("Rin learned maps.", [3]),
    S("Against that, the training came late.", [2], "counter")])
INTERP_REPAIRED = Prose(sentences=[
    S("Rin met a teacher.", [1]), S("Rin trained with the sword.", [2]), S("Rin learned maps.", [3]),
    S("The evidence suggests Rin wanted to prove his worth.", [1, 3], "reading"),
    S("Against that, the training came late.", [2], "counter")])


async def test_in_an_interpretive_answer_the_inference_may_become_a_labelled_reading():
    m = Models([INTERP_DRAFT, INTERP_REPAIRED], flag=lambda t: "to prove his worth" in t,
               lens_claims=LENS_CLAIMS)
    a = await m.ask_interpretive()
    repair = m.write_prompts[1]
    assert w.AS_READING_RULE in repair and "Draft sentences 2 state" in " ".join(repair.split())
    assert a.answer_style == "prose"
    # the interpretation is no longer stated as a fact; it is the labelled reading
    assert all("prove his worth" not in s.text for s in a.prose if s.kind == "fact")
    assert [(s.kind, s.text) for s in a.short_answer] == [
        ("reading", "The evidence suggests Rin wanted to prove his worth.")]
    # and the reading was judged as a reading in the second check
    assert "(reading)" in m.check_prompts[1]


def test_readings_allowed_needs_readings_on_and_an_interpretive_answer(monkeypatch):
    assert w.readings_allowed(SHAPE)
    assert not w.readings_allowed(None)
    assert not w.readings_allowed(SHAPE, "section")
    beyond = interpret.Shape(viewpoint="v", parts=("p",), kinds=("beyond",),
                             retrieval=(("p", 0, False),))
    assert not w.readings_allowed(beyond)
    monkeypatch.setattr(w, "READINGS", False)
    assert not w.readings_allowed(SHAPE)


# ---- general ----------------------------------------------------------------------------------

def test_the_new_prompt_texts_name_no_character_book_or_genre():
    text = "\n".join((w.FACT_RULE, w.NO_READINGS_RULE, w.READING_HOME_RULE, w.REDUCE_RULE,
                      w.AS_READING_RULE, w.REPAIR_RULE, w.REPAIR_LEAD, w.MISSING_RULE,
                      synth._INFERENCE_BLOCK, synth._RESTATED_BLOCK))
    banned = (r"\b(holmes|watson|sherlock|small|hope|drebber|stangerson|sholto|ferrier|tonga|"
              r"morstan|lestrade|baker street|mormon|agra|crime|crimes|murder|villain|villains|"
              r"detective|treasure|darcy|bennet|elizabeth|wizard|dragon|magic|kingdom|elf|elves|"
              r"quest|sword|revenge|rache)\b")
    assert not re.findall(banned, text.lower())
