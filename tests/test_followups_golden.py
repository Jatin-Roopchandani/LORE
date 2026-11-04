"""Without history, a question is answered exactly as before follow-ups existed.

`golden/followups_none.json` was dumped from 2a92976, the commit before
follow-up questions, by running `lore_search` (the call the web UI and MCP
make) over the position fixture library with scripted models, and storing
every model call (each prompt and the schema it was sent with), every stream
event and the result. Regenerate it only by doing the same on a commit
without the feature.
"""
from __future__ import annotations

import json
from pathlib import Path

from lore.mcp import tools
from lore.query.agent import QueryModels
from lore.query.decompose import PartSpec, RecapDecomposition
from lore.query.select import TemplateChoice
from lore.query.synthesize import Synthesis
from tests.position_fixtures import HOLLOW, build_position_library
from tests.worlds_fixtures import QUESTION_VECTOR

GOLDEN = Path(__file__).parent / "golden" / "followups_none.json"
CASES = {
    "world": dict(question="Who is Ren Ashby?", world=HOLLOW, parts=["Who is Ren Ashby?"]),
    "all_worlds": dict(question="Who is Ren Ashby?", world=None, parts=["Who is Ren Ashby?"]),
    "decomposed": dict(question="Who is Old Tam and what did Mira Cole know?", world=HOLLOW,
                       parts=["Who is Old Tam?", "What did Mira Cole know?"]),
    "recap": dict(question="Recap the story so far.", world=HOLLOW,
                  parts=["Recap the story so far."], recap={"span": "story"}),
}


def recording_models(parts: list[str], recap: dict | None = None):
    calls: dict[str, list] = {"embed": [], "decompose": [], "select": [], "synthesize": []}

    async def embed(texts):
        calls["embed"].append(list(texts))
        return [QUESTION_VECTOR for _ in texts]

    async def decompose(prompt, schema):
        calls["decompose"].append({"prompt": prompt,
                                   "schema": schema.model_json_schema()})
        return RecapDecomposition(sub_questions=[PartSpec(question=p) for p in parts],
                                  depth="focused", recap=recap)

    async def select(prompt, schema):
        calls["select"].append(prompt)
        return TemplateChoice(template="neighbours", entities=[1])

    async def synthesize(prompt, schema, **_):
        calls["synthesize"].append(prompt)
        return Synthesis(claims=[], can_answer=False)

    async def aclose():
        return None

    models = QueryModels(embed, select, synthesize, lambda: "writer", aclose,
                         lambda: "chooser", decompose=decompose)
    return models, calls


async def capture(config, **extra) -> dict:
    out = {}
    for name, case in CASES.items():
        models, calls = recording_models(case["parts"], case.get("recap"))
        events: list[dict] = []
        scope = {} if case["world"] is None else {"world": case["world"]}
        result = await tools.lore_search(config, case["question"], models=models,
                                         on_event=events.append, **scope, **extra)
        out[name] = {"result": result, "calls": calls, "events": events}
    return json.loads(json.dumps(out, sort_keys=True, default=str))


async def test_without_history_every_call_event_and_result_is_golden(tmp_path) -> None:
    config = build_position_library(tmp_path)
    got = await capture(config)
    assert got == json.loads(GOLDEN.read_text())


async def test_an_empty_history_is_no_history(tmp_path) -> None:
    config = build_position_library(tmp_path)
    assert await capture(config, history=[]) == json.loads(GOLDEN.read_text())
