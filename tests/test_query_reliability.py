"""Deep-answer reliability: a malformed model reply is retried once on the next
model (and counted), a section whose prose has a few rejected sentences keeps
its prose or gets one rewrite before it falls back, and a section that is not
about the question's subject is left out."""
from __future__ import annotations

import httpx
import pytest
from pydantic import BaseModel

from lore.inference.gemini import GeminiClient, GeminiOutputError
from lore.query import deep
from lore.query.deep import DeepPlan, PlanSection, Spend
from lore.query.models import Claim
from lore.query.synthesize import Synthesis
from lore.query.write import AVOID_LEAD, Sentence, assemble
from tests.deep_fixtures import Scripted, build_deep_library, run_scenario
from tests.test_query_deep import DEEP_PLAN, GENERAL, ROAD, TRAVELLER, TWO, of


@pytest.fixture
def config(tmp_path):
    return build_deep_library(tmp_path)


# ---- 1. a malformed reply is retried once on the next model -----------------------------

class _Pick(BaseModel):
    name: str


def _ok(text):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def _model_of(request):
    return str(request.url).rsplit("/", 1)[-1].split(":")[0]


@pytest.mark.asyncio
async def test_generate_json_can_avoid_the_model_that_just_failed():
    tried = []

    def handler(request):
        tried.append(_model_of(request))
        return httpx.Response(200, json=_ok('{"name": "x"}'))

    client = GeminiClient("k", models=["m-a", "m-b"], transport=httpx.MockTransport(handler))
    await client.generate_json("p", _Pick, avoid="m-a")
    assert tried == ["m-b"]


@pytest.mark.asyncio
async def test_avoiding_the_only_model_uses_it_again():
    tried = []

    def handler(request):
        tried.append(_model_of(request))
        return httpx.Response(200, json=_ok('{"name": "x"}'))

    client = GeminiClient("k", models=["m-a"], transport=httpx.MockTransport(handler))
    await client.generate_json("p", _Pick, avoid="m-a")
    assert tried == ["m-a"]


@pytest.mark.asyncio
async def test_an_output_error_names_the_model_that_wrote_it():
    def handler(request):
        return httpx.Response(200, json=_ok('{"name": '))

    client = GeminiClient("k", models=["m-a"], transport=httpx.MockTransport(handler))
    with pytest.raises(GeminiOutputError) as err:
        await client.generate_json("p", _Pick)
    assert err.value.model == "m-a"


@pytest.mark.asyncio
async def test_a_json_request_allows_a_long_reply():
    import json
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content)["generationConfig"])
        return httpx.Response(200, json=_ok('{"name": "x"}'))

    await GeminiClient("k", models=["m-a"], transport=httpx.MockTransport(handler)).generate_json("p", _Pick)
    assert seen["maxOutputTokens"] >= 16384


class _Flaky:
    def __init__(self, bad):
        self.bad, self.calls = bad, []

    async def __call__(self, prompt, schema, **kw):
        self.calls.append(kw)
        if len(self.calls) <= self.bad:
            raise GeminiOutputError("bad json", "{", model="m-a")
        return "ok"


@pytest.mark.asyncio
async def test_spend_retries_a_malformed_reply_once_on_the_next_model_and_counts_it():
    flaky, spend = _Flaky(1), Spend()
    assert await spend.counted(flaky)("p", None) == "ok"
    assert spend.n == 2 and flaky.calls == [{}, {"avoid": "m-a"}]


@pytest.mark.asyncio
async def test_two_malformed_replies_fail_as_before():
    flaky, spend = _Flaky(2), Spend()
    with pytest.raises(GeminiOutputError):
        await spend.counted(flaky)("p", None)
    assert spend.n == 2


class _Hiccup(Scripted):
    """The first claims call comes back malformed."""
    async def generate(self, prompt, schema, **kw):
        if schema is Synthesis and not any(k == "claims" for k, _ in self.calls):
            self.calls.append(("claims", prompt))
            raise GeminiOutputError("not valid json", "{")
        return await super().generate(prompt, schema)


async def test_a_deep_section_survives_one_malformed_claims_reply(config):
    clean = Scripted(DEEP_PLAN, planner=TWO, tagged=True)
    base, _ = await run_scenario(config, clean, GENERAL)
    hiccup = _Hiccup(DEEP_PLAN, planner=TWO, tagged=True)
    answer, _ = await run_scenario(config, hiccup, GENERAL)
    assert [s.heading for s in answer.sections] == ["The traveller", "The road"]
    assert answer.unwritten == 0 and answer.requests == base.requests + 1


def test_a_section_may_keep_twelve_claims():
    assert deep.SECTION_CLAIMS == 12


# ---- 2. a few rejected sentences do not cost the prose ------------------------------------

def _facts(n):
    return [Sentence(text=f"F{i}.", claims=[1]) for i in range(n)]


def test_three_quarters_passing_keeps_the_prose_and_drops_the_rest():
    built = assemble(_facts(8), 8, [True], [True] * 6 + [False] * 2, [3])
    assert built is not None and len(built) == 6
    assert assemble(_facts(8), 8, [True], [True] * 5 + [False] * 3, [3]) is None


class _Rewriter(Scripted):
    """Rejects the sentences named in `reject`; a writer prompt that lists
    rejected sentences gets a reply without them."""
    async def generate(self, prompt, schema, **kw):
        reply = await super().generate(prompt, schema)
        if AVOID_LEAD in prompt and hasattr(reply, "sentences"):
            reply.sentences = [s for s in reply.sentences if " ".join(s.text.split()) not in prompt]
        return reply


def _sections_of(scripted):
    return scripted.prompts("write")


async def test_two_rejected_of_three_gets_one_rewrite_that_lists_them(config):
    scripted = _Rewriter(DEEP_PLAN, planner=TWO, tagged=True, reject=("sentence 1.", "sentence 2."))
    answer, _ = await run_scenario(config, scripted, GENERAL)
    one, two = answer.sections
    assert one.style == "prose" and two.style == "prose"
    again = [p for p in scripted.prompts("write") if AVOID_LEAD in p]
    assert len(again) == 2 and "were not supported by the cited facts" in again[0]
    assert "sentence 1." in again[0] and "sentence 2." in again[0]
    # the rewritten prose keeps only what the check accepted
    assert all("sentence 1." not in s.text and "sentence 2." not in s.text for s in one.prose)


async def test_a_rewrite_that_fails_again_falls_back_to_the_claims(config):
    scripted = Scripted(DEEP_PLAN, planner=TWO, tagged=True, reject=(ROAD,))
    answer, _ = await run_scenario(config, scripted, GENERAL)
    one, two = answer.sections
    assert one.style == "prose" and two.style == "claims"
    road_writes = [p for p in scripted.prompts("write") if f"Question: {ROAD}" in p]
    assert len(road_writes) == 2 and AVOID_LEAD in road_writes[1]


async def test_the_rewrite_counts_against_the_request_budget(config):
    scripted = Scripted(DEEP_PLAN, planner=TWO, tagged=True, reject=(ROAD,))
    answer, _ = await run_scenario(config, scripted, GENERAL)
    clean, _ = await run_scenario(config, Scripted(DEEP_PLAN, planner=TWO, tagged=True), GENERAL)
    assert answer.requests == clean.requests + 2  # a second write and a second check


# ---- 3. a section must be about the question's subject ------------------------------------

TOPICAL = "Tell me about Zed Quill."
FACETS = DeepPlan(sections=[
    PlanSection(heading="What it is", focus="What Zed is", entities=["a_zed#nature"]),
    PlanSection(heading="The gate", focus="The gate and its keeper", entities=["a_gate"]),
    PlanSection(heading="Its history", focus="How Zed's story went", entities=["a_zed#history", "a_gate"])])


class _OffTopic(Scripted):
    """Claims for the gate section never mention Zed."""
    async def generate(self, prompt, schema, **kw):
        reply = await super().generate(prompt, schema)
        if schema is Synthesis and "The gate and its keeper" in prompt:
            reply.claims = [c.model_copy(update={"text": c.text.replace("Zed Quill", "The keeper")})
                            for c in reply.claims]
        return reply


async def test_the_claims_rule_asks_for_facts_about_the_questions_subject(config):
    scripted = _OffTopic(DEEP_PLAN, planner=FACETS, tagged=True)
    await run_scenario(config, scripted, TOPICAL)
    for prompt in scripted.prompts("claims"):
        assert "must also bear on Zed Quill" in " ".join(prompt.split())
    clean = Scripted(DEEP_PLAN, planner=TWO, tagged=True)
    await run_scenario(config, clean, GENERAL)
    assert not any("must also bear on" in p for p in clean.prompts("claims"))


async def test_a_section_whose_claims_miss_the_subject_is_left_out_not_counted_unwritten(config):
    scripted = _OffTopic(DEEP_PLAN, planner=FACETS, tagged=True)
    answer, events = await run_scenario(config, scripted, TOPICAL)
    assert [s.heading for s in answer.sections] == ["What it is", "Its history"]
    assert answer.unwritten == 0
    assert all("The keeper" not in c.text for c in answer.claims)
    gate = [e for e in of(events, "section") if e.get("heading") == "The gate" and e["state"] == "done"]
    assert gate and gate[0]["style"] == "empty"


async def test_a_section_of_the_subject_alone_is_never_dropped_for_its_wording(config):
    plan = DeepPlan(sections=[PlanSection(heading="What it is", focus="What Zed is",
                                          entities=["a_zed#nature"])])

    class Pronouns(Scripted):
        async def generate(self, prompt, schema, **kw):
            reply = await super().generate(prompt, schema)
            if schema is Synthesis:
                reply.claims = [c.model_copy(update={"text": c.text.replace("Zed Quill", "He")})
                                for c in reply.claims]
            return reply

    answer, _ = await run_scenario(config, Pronouns(DEEP_PLAN, planner=plan, tagged=True), TOPICAL)
    assert [s.heading for s in answer.sections] == ["What it is"]
