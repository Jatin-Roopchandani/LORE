"""lore.tracing: every model call of one question, in order, capped and redacted."""
from __future__ import annotations

import asyncio
import dataclasses
import json

import httpx
import pytest
from pydantic import BaseModel

from lore import tracing
from lore.inference import key_scope
from lore.inference.gemini import GeminiClient, GeminiError

FAKE_KEY = "AIzaFAKEfakeFAKEfakeFAKEfakeFAKEfake123"
ODD_KEY = "odd-shaped-user-key-0123456789abcdef"  # not Google-shaped: only the scope knows it


class Reply(BaseModel):
    text: str


@dataclasses.dataclass(frozen=True)
class FakeModels:
    """The shape of QueryModels that tracing touches."""
    select: object
    synthesize: object
    synthesis_model: object
    selection_model: object
    decompose: object = None
    check: object = None
    write: object = None


def fake_models(log=None, fail_on=None, delay=0.0):
    def gen(stage):
        async def generate(prompt, schema):
            if delay:
                await asyncio.sleep(delay)
            if fail_on == stage:
                raise GeminiError(f"boom in {stage}")
            if log is not None:
                log.append(stage)
            return Reply(text=f"{stage} answer to {prompt}")
        return generate
    return FakeModels(select=gen("select"), synthesize=gen("synthesize"),
                      decompose=gen("decompose"), check=gen("check"), write=None,
                      synthesis_model=lambda: "flash", selection_model=lambda: "lite")


async def test_every_call_is_recorded_in_order_with_its_reply():
    trace = tracing.Trace()
    models = tracing.traced_models(fake_models(), trace)
    assert models.write is None  # an absent step stays absent
    await models.decompose("q", Reply)
    await models.select("q1", Reply)
    await models.synthesize("q2", Reply)
    calls = trace.calls_out()
    assert [c["stage"] for c in calls] == ["decompose", "select", "synthesize"]
    assert [c["model"] for c in calls] == ["flash", "lite", "flash"]
    assert calls[1]["prompt"] == "q1"
    assert json.loads(calls[1]["reply"]) == {"text": "select answer to q1"}
    assert all(c["ok"] and c["latency_ms"] is not None and not c["error"] for c in calls)
    assert "_t0" not in calls[0]


async def test_a_failed_call_is_recorded_and_still_raises():
    trace = tracing.Trace()
    models = tracing.traced_models(fake_models(fail_on="check"), trace)
    with pytest.raises(GeminiError):
        await models.check("judge this", Reply)
    (call,) = trace.calls_out()
    assert call["ok"] is False and "boom in check" in call["error"] and call["reply"] is None


def _ok(text):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def _model_of(request):
    return str(request.url).rsplit("/", 1)[-1].split(":")[0]


async def test_retries_inside_one_call_are_its_attempts():
    def handler(request):
        if _model_of(request) == "m1":
            return httpx.Response(503, json={})
        return httpx.Response(200, json=_ok('{"text": "hi"}'))

    client = GeminiClient(FAKE_KEY, models=["m1", "m2"], transport=httpx.MockTransport(handler))
    trace = tracing.Trace()

    async def generate(prompt, schema):
        return await client.generate_json(prompt, schema)

    models = tracing.traced_models(fake_models(), trace)
    models = dataclasses.replace(models, synthesize=tracing._wrap(trace, "synthesize", generate))
    with tracing.bound(trace):
        await models.synthesize("q", Reply)
    (call,) = trace.calls_out()
    assert [(a["model"], a["ok"]) for a in call["attempts"]] == [("m1", False), ("m2", True)]
    assert "503" in call["attempts"][0]["error"]
    assert call["model"] == "m2"
    assert trace.model_usage() == {"m1": {"requests": 1, "failed": 1},
                                   "m2": {"requests": 1, "failed": 0}}
    await client.aclose()


async def test_no_trace_bound_means_nothing_is_noted():
    tracing.note_attempt("m", ok=True, latency_s=0.1)  # no error, no effect
    models = fake_models()
    assert tracing.maybe_traced(models) is models


def test_long_text_is_capped_with_a_marker():
    text = "é" * 100_000  # two bytes each
    out = tracing.cap(text)
    assert len(out.encode()) < tracing.MAX_TEXT_BYTES + 100
    assert out.endswith(f"more bytes]") and "truncated" in out
    assert tracing.cap("short") == "short"


async def test_prompts_and_replies_over_the_cap_are_cut():
    trace = tracing.Trace()
    models = tracing.traced_models(fake_models(), trace)
    await models.select("x" * 200_000, Reply)
    (call,) = trace.calls_out()
    assert len(call["prompt"].encode()) < tracing.MAX_TEXT_BYTES + 100
    assert len(call["reply"].encode()) < tracing.MAX_TEXT_BYTES + 100
    assert "truncated" in call["prompt"] and "truncated" in call["reply"]


async def test_a_key_never_enters_a_trace_even_through_a_failing_call():
    """A failing request whose error text carries the key, and a prompt that
    contains it, under the user's bound scope: the trace holds neither the
    Google-shaped key nor the odd-shaped one only the scope knows."""
    def handler(request):
        return httpx.Response(500, text=f"bad request for key {request.headers['x-goog-api-key']}"
                                        f" and {FAKE_KEY}")

    client = GeminiClient(ODD_KEY, models=["m1"], transport=httpx.MockTransport(handler))
    trace = tracing.Trace()

    async def generate(prompt, schema):
        return await client.generate_json(prompt, schema)

    wrapped = tracing._wrap(trace, "synthesize", generate)
    with key_scope.bound(key_scope.KeyScope("alice", ODD_KEY)), tracing.bound(trace):
        with pytest.raises(GeminiError):
            await wrapped(f"prompt mentioning {ODD_KEY} and {FAKE_KEY}", Reply)
        trace.finish(error=f"failed with {ODD_KEY}")
    blob = json.dumps({"calls": trace.calls_out(), "error": trace.error})
    assert ODD_KEY not in blob and FAKE_KEY not in blob
    assert "[redacted key]" in blob
    assert trace.calls_out()[0]["attempts"][0]["ok"] is False
    await client.aclose()


async def test_two_questions_at_once_keep_separate_complete_traces():
    async def question(name):
        trace = tracing.Trace()
        with tracing.bound(trace):
            models = tracing.maybe_traced(fake_models(delay=0.01))
            await models.decompose(f"{name}-d", Reply)
            # parallel sub-questions inside one question, as the agent does
            await asyncio.gather(models.select(f"{name}-s1", Reply),
                                 models.select(f"{name}-s2", Reply))
            await models.synthesize(f"{name}-w", Reply)
        return trace

    a, b = await asyncio.gather(question("A"), question("B"))
    for trace, name in ((a, "A"), (b, "B")):
        prompts = [c["prompt"] for c in trace.calls_out()]
        assert len(prompts) == 4 and all(p.startswith(name) for p in prompts)
    assert a.request_id != b.request_id
    assert tracing.current() is None


async def test_the_request_id_is_bound_with_the_trace():
    trace = tracing.Trace()
    assert tracing.current_request_id() is None
    with tracing.bound(trace):
        assert tracing.current_request_id() == trace.request_id
        seen = await asyncio.create_task(asyncio.sleep(0, tracing.current_request_id()))
    assert seen == trace.request_id and tracing.current_request_id() is None


def test_steps_and_decomposition_come_from_the_stream():
    trace = tracing.Trace()
    trace.step({"type": "step", "t": 0.1, "name": "split", "state": "start"})
    trace.step({"type": "parts", "t": 0.2, "sub_questions": ["a", "b"]})
    trace.step({"type": "answer", "t": 0.3, "result": {}})
    assert [s["type"] for s in trace.steps] == ["step", "parts"]
    assert trace.decomposition == {"parts": {"sub_questions": ["a", "b"]}}
    trace.finish(answer={"answer": "x"})
    t = trace.timings()
    assert t["total_ms"] is not None and t["first_event_ms"] is not None


async def test_the_evals_trace_through_the_shared_helper():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parents[1] / "evals" / "answer_quality"))
    try:
        from run_eval import _traced
    finally:
        sys.path.pop(0)
    sink: list = []
    models = _traced(fake_models(), sink)
    await models.decompose("d", Reply)
    await models.synthesize("s", Reply)
    assert [(c["kind"], c["prompt"]) for c in sink] == [("decompose", "d"), ("synthesize", "s")]
    assert sink[1]["reply"] == {"text": "synthesize answer to s"}
