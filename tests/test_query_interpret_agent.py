"""Interpretive answers through answer_question, over the synthetic worlds
library (tests/worlds_fixtures.py): lens expansion and its part budget, the
beyond part's one retrieval, per-lens evidence and claims, the candidate helper
for a lens about people, the answer's shape end to end, and INTERPRETIVE off.

Models are scripted; nothing reaches a real one."""
from __future__ import annotations

import re

import pytest

from lore.mcp.stores import open_stores
from lore.query import decompose as decompose_module, interpret, judge as judge_module
from lore.query.agent import QueryModels, answer_question
from lore.query.decompose import Decomposition, PartSpec
from lore.query.select import TemplateChoice
from lore.query.synthesize import Faithfulness, Judgement, Synthesis
from lore.query.write import Prose, Sentence
from tests.worlds_fixtures import ALPHA, QUESTION_VECTOR, build_library

QUESTION = "Was Zed Quill right to keep walking?"
LENS_A, LENS_B = "What did Zed Quill do on the road?", "How did the road end for Zed Quill?"
BEYOND = "Why did the author write the road?"


@pytest.fixture
def config(tmp_path):
    return build_library(tmp_path)


def plan_of(*specs, depth="focused"):
    return Decomposition(sub_questions=list(specs), depth=depth)


INTERPRETIVE = PartSpec(question=QUESTION, kind="interpretive", lenses=[LENS_A, LENS_B])


class Models:
    """Scripted QueryModels: decomposition, claims, writer and check in one."""

    def __init__(self, decomposition, *, write=None):
        self.decomposition = decomposition
        self.claims_prompts: list[str] = []
        self.writer_prompts: list[str] = []
        self.embedded: list[list[str]] = []
        self.write_reply = write

    async def embed(self, texts):
        self.embedded.append(list(texts))
        return [QUESTION_VECTOR for _ in texts]

    async def decompose(self, prompt, schema):
        return self.decomposition

    async def select(self, prompt, schema):
        return TemplateChoice(template="neighbours", entities=[1])

    async def generate(self, prompt, schema):
        if schema is Synthesis:
            self.claims_prompts.append(prompt)
            n = int(re.search(r"^\[(\d+)\]", prompt, re.M).group(1))
            # Each question's claims quote a different stretch of the same passage.
            quote = ("walked on with Yan Vale" if LENS_A in prompt
                     else "the traveller Zed Quill" if LENS_B in prompt else "through the")
            return Synthesis(can_answer=True, claims=[
                {"text": f"Zed Quill: {quote}.", "passage": n, "quote": quote}])
        if schema in (Prose, Faithfulness) or hasattr(schema, "model_fields"):
            if "sentences" in schema.model_fields:
                self.writer_prompts.append(prompt)
                return self.write_reply
            count = prompt.count("\n   cites passage")
            sentences = prompt.count("\n   uses claims")
            return Faithfulness(
                judgements=[Judgement(claim=i, verdict="supported") for i in range(1, count + 1)],
                sentence_judgements=[Judgement(claim=i, verdict="supported")
                                     for i in range(1, sentences + 1)])
        raise AssertionError(schema)

    def models(self):
        async def aclose():
            return None
        return QueryModels(self.embed, self.select, self.generate, lambda: "writer", aclose,
                           lambda: "chooser", decompose=self.decompose, check=self.generate,
                           write=self.generate)


async def ask(config, scripted, question=QUESTION, **kw):
    events = []
    with open_stores(config) as s:
        answer = await answer_question(s.conn, s.kuzu, s.lance, question, scripted.models(),
                                       world=ALPHA, on_event=events.append, **kw)
    return answer, events


def part_events(events, kind):
    return [e for e in events if e["type"] == kind]


# ---- lens expansion and the budget -------------------------------------------------

async def test_an_interpretive_part_is_retrieved_through_its_lenses(config):
    scripted = Models(plan_of(INTERPRETIVE))
    answer, events = await ask(config, scripted)
    (parts,) = part_events(events, "parts")
    assert parts["sub_questions"] == [LENS_A, LENS_B]
    assert parts["lens_of"] == [0, 0] and parts["plan_questions"] == [QUESTION]
    assert parts["kinds"] == ["interpretive"] and parts["lenses"] == [[LENS_A, LENS_B]]
    # Seeds, search and claims ran once per lens, not for the part itself.
    assert sorted({e["part"] for e in part_events(events, "seeds")}) == [0, 1]
    assert answer.sub_questions == (LENS_A, LENS_B)
    assert len(scripted.claims_prompts) == 2
    assert [LENS_A in p for p in scripted.claims_prompts] == [True, False]
    assert all("one side of a larger interpretive question: " + QUESTION in p
               for p in scripted.claims_prompts)
    # The lenses were embedded in one call (the question's own vector is reused).
    assert len(scripted.embedded[-1]) == 2 and LENS_A in scripted.embedded[-1][0]


async def test_a_factual_part_is_retrieved_as_itself_beside_the_lenses(config):
    scripted = Models(plan_of(PartSpec(question="Who is Zed Quill?"), INTERPRETIVE))
    answer, events = await ask(config, scripted)
    (parts,) = part_events(events, "parts")
    assert parts["sub_questions"] == ["Who is Zed Quill?", LENS_A, LENS_B]
    assert parts["lens_of"] == [None, 1, 1]
    assert len(scripted.claims_prompts) == 3
    plain = [p for p in scripted.claims_prompts if "Question: Who is Zed Quill?" in p][0]
    assert "interpretive question" not in plain


async def test_a_beyond_part_gets_one_retrieval_and_no_lenses(config):
    scripted = Models(plan_of(INTERPRETIVE, PartSpec(question=BEYOND, kind="beyond", lenses=["x?"])))
    answer, events = await ask(config, scripted)
    (parts,) = part_events(events, "parts")
    assert parts["sub_questions"] == [LENS_A, LENS_B, BEYOND]
    assert parts["lens_of"] == [0, 0, None]
    assert len(scripted.claims_prompts) == 3
    assert any("cannot settle" in p and BEYOND in p for p in scripted.claims_prompts)


async def test_the_part_budget_holds_when_lenses_are_expanded(config):
    specs = [PartSpec(question=f"Was Zed Quill {w}?", kind="interpretive",
                      lenses=[f"What did Zed Quill do for {w}, aspect {i}?" for i in range(4)])
             for w in ("right", "kind", "wise")]
    scripted = Models(plan_of(*specs))
    answer, events = await ask(config, scripted)
    (parts,) = part_events(events, "parts")
    assert len(parts["sub_questions"]) == decompose_module.MAX_PARTS == 6
    assert len(scripted.claims_prompts) <= 6
    # Trimmed from the part with most lenses, never below one each.
    per_part = [parts["lens_of"].count(i) for i in range(3)]
    assert sum(per_part) == 6 and min(per_part) >= 1


# ---- the candidate helper ----------------------------------------------------------

async def test_only_a_lens_about_people_that_names_none_gets_candidates(config, monkeypatch):
    seen = []
    real = judge_module.judge_seeds

    async def spy(kuzu, names, part, question, seeds, named, book_ids, exclude_names, **kw):
        seen.append((part, kw.get("conn") is not None))
        return await real(kuzu, names, part, question, seeds, named, book_ids, exclude_names, **kw)
    monkeypatch.setattr(judge_module, "judge_seeds", spy)
    # The lens names nobody; the part (Who is ...) is factual and gets no candidates.
    lens = "What do the walkers do on the road?"
    await ask(config, Models(plan_of(PartSpec(question="Who walks?"),
                                     PartSpec(question="Were they right?", kind="interpretive",
                                              lenses=[lens, "How does the road end?"]))),
              question="Who walks, and were they right?")
    assert (lens, True) in seen and all(p != "Who walks?" for p, _ in seen)


async def test_a_lens_that_names_an_entity_gets_no_candidates(config, monkeypatch):
    seen = []

    async def spy(*a, **kw):
        seen.append(a[2])
        raise AssertionError("candidates for a lens that names a character")
    monkeypatch.setattr(judge_module, "judge_seeds", spy)
    await ask(config, Models(plan_of(INTERPRETIVE)))  # both lenses name Zed Quill
    assert seen == []


# ---- the answer, end to end ----------------------------------------------------------

def S(text, claims, kind="fact"):
    return Sentence(text=text, claims=claims, kind=kind)


async def test_the_answer_has_evidence_a_reading_a_counter_case_and_the_beyond_limit(config):
    # Claim 1 is the first lens's and claim 2 the second's (shown in that order).
    reply = Prose(sentences=[
        S("Zed Quill walked on.", [1]), S("The traveller kept going.", [2]),
        S("The evidence suggests Zed was right to go on.", [1, 2], "reading"),
        S("Against that, the road was long.", [2], "counter")])
    scripted = Models(plan_of(INTERPRETIVE, PartSpec(question=BEYOND, kind="beyond")), write=reply)
    answer, _ = await ask(config, scripted)
    assert answer.answer_style == "prose"
    assert [s.kind for s in answer.prose] == ["fact", "fact", "reading", "counter", "limit"]
    assert answer.prose[-1].text == f"The books don't settle: {BEYOND}"
    assert answer.prose[-1].claims == () and answer.prose[-1].passages == ()
    assert answer.text.endswith(f"The books don't settle: {BEYOND}")
    assert interpret.NO_POSITION_LINE not in answer.text
    # Both lens headings and the plan's own parts reached the writer.
    prompt = scripted.writer_prompts[0]
    assert f"Evidence for: {LENS_A}" in prompt and f"Evidence for: {LENS_B}" in prompt
    assert f"1. {QUESTION} (interpretive" in prompt and f"2. {BEYOND} (beyond the books)" in prompt


async def test_a_reading_that_cites_one_lens_is_dropped_and_the_answer_says_so(config):
    reply = Prose(sentences=[
        S("Zed Quill walked on.", [1]), S("The traveller kept going.", [2]),
        S("The evidence suggests Zed was right to go on.", [1], "reading")])
    answer, _ = await ask(config, Models(plan_of(INTERPRETIVE), write=reply))
    assert [s.kind for s in answer.prose] == ["fact", "fact", "limit"]
    assert answer.prose[-1].text == interpret.NO_POSITION_LINE


# ---- INTERPRETIVE off ---------------------------------------------------------------

async def test_with_the_switch_off_the_parts_are_the_plain_parts_and_nothing_is_shaped(config, monkeypatch):
    monkeypatch.setattr(decompose_module, "INTERPRETIVE", False)
    # The model still returns kinds and lenses (a Decomposition): they are ignored.
    scripted = Models(plan_of(INTERPRETIVE), write=Prose(sentences=[
        S("Zed Quill walked on.", [1]), S("Zed Quill met Yan.", [1])]))
    answer, events = await ask(config, scripted)
    assert answer.sub_questions == (QUESTION,)
    (parts,) = part_events(events, "parts")
    assert "lens_of" not in parts
    assert all("interpretive" not in p for p in scripted.claims_prompts + scripted.writer_prompts)
    assert all(s.kind == "fact" for s in answer.prose)
