"""Every schema LORE sends to a model converts to the strict form Claude and
OpenAI accept, and a reply in that form round-trips back into the model."""
import json

import pytest
from pydantic import BaseModel

from lore.inference.providers.schema import decode, drop_optional_nulls, strict_schema


def _lore_schemas() -> dict[str, type[BaseModel]]:
    from lore.beats.build import BeatsAnswer
    from lore.codex.build import CodexAnswer
    from lore.codex.intro import IntroAnswer
    from lore.extract.schemas import ChunkExtraction
    from lore.query import decompose, deep, select, synthesize, write
    from lore.resolve.narrator import NarratorAnswer, SpeakerAnswers

    found = {
        "BeatsAnswer": BeatsAnswer, "CodexAnswer": CodexAnswer, "IntroAnswer": IntroAnswer,
        "ChunkExtraction": ChunkExtraction, "NarratorAnswer": NarratorAnswer,
        "SpeakerAnswers": SpeakerAnswers, "DeepPlan": deep.DeepPlan,
        "TemplateChoice": select.TemplateChoice, "Synthesis": synthesize.Synthesis,
        "Faithfulness": synthesize.Faithfulness,
        "_PlainFaithfulness": synthesize._PlainFaithfulness,
        "Prose": write.Prose, "_PlainProse": write._PlainProse,
        "_StoryProse": write._StoryProse,
    }
    for name in ("SubQuestions", "Decomposition", "RecapDecomposition", "_Standalone",
                 "FollowupSubQuestions", "FollowupDecomposition",
                 "FollowupRecapDecomposition"):
        found[name] = getattr(decompose, name)
    for restated in (False, True):
        for inference in (False, True):
            found[f"fidelity({restated},{inference})"] = synthesize._fidelity_schema(
                restated, inference)
    return found


SCHEMAS = _lore_schemas()


def _walk(node, root, path="$"):
    if "$ref" in node:
        assert set(node) == {"$ref"}, path
        return
    for key in ("anyOf", "allOf"):
        for i, sub in enumerate(node.get(key, [])):
            _walk(sub, root, f"{path}.{key}[{i}]")
    assert "default" not in node and "title" not in node, path
    for bad in ("minimum", "maximum", "minLength", "maxLength", "pattern", "oneOf"):
        assert bad not in node, f"{path}: {bad}"
    if node.get("type") == "object":
        assert node["additionalProperties"] is False, path
        assert sorted(node["required"]) == sorted(node["properties"]), path
        for name, prop in node["properties"].items():
            _walk(prop, root, f"{path}.{name}")
    if node.get("type") == "array" and "items" in node:
        _walk(node["items"], root, f"{path}[]")


def _sample(node, root):
    """The smallest reply the strict schema allows (null wherever it may)."""
    if "$ref" in node:
        target = root
        for part in node["$ref"][2:].split("/"):
            target = target[part]
        return _sample(target, root)
    if "anyOf" in node:
        if {"type": "null"} in node["anyOf"]:
            return None
        return _sample(node["anyOf"][0], root)
    if "enum" in node:
        return node["enum"][0]
    kind = node.get("type")
    if kind == "object":
        return {k: _sample(v, root) for k, v in node["properties"].items()}
    if kind == "array":
        return [_sample(node["items"], root)] if node.get("minItems") else []
    return {"string": "x", "integer": 1, "number": 1.0, "boolean": False,
            "null": None}[kind]


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_every_lore_schema_is_strict(name):
    schema = strict_schema(SCHEMAS[name])
    _walk(schema, schema)
    for sub in schema.get("$defs", {}).values():
        _walk(sub, schema)
    json.dumps(schema)  # serialisable as sent


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_every_lore_schema_round_trips(name):
    model = SCHEMAS[name]
    schema = strict_schema(model)
    reply = json.dumps(_sample(schema, schema))
    decoded = decode(reply, model)
    assert isinstance(decoded, model)


class Inner(BaseModel):
    text: str
    tags: list[str] = []


class Outer(BaseModel):
    items: list[Inner]
    note: str | None = None
    count: int = 3
    nested: Inner | None = None


def test_optional_fields_become_nullable_and_required():
    schema = strict_schema(Outer)
    assert schema["required"] == ["items", "note", "count", "nested"]
    assert schema["properties"]["count"] == {"anyOf": [{"type": "integer"}, {"type": "null"}]}
    inner = schema["$defs"]["Inner"]
    assert inner["required"] == ["text", "tags"]
    assert inner["properties"]["tags"]["anyOf"][-1] == {"type": "null"}
    assert inner["properties"]["text"] == {"type": "string"}


def test_a_null_for_an_optional_field_takes_its_default():
    reply = {"items": [{"text": "a", "tags": None}], "note": None, "count": None,
             "nested": None}
    out = decode(json.dumps(reply), Outer)
    assert out == Outer(items=[Inner(text="a")])
    assert out.count == 3


def test_a_null_the_field_allows_is_kept():
    assert drop_optional_nulls({"items": [], "note": None}, Outer) == {"items": [], "note": None}


def test_constraints_move_into_the_description():
    from pydantic import Field

    class Bounded(BaseModel):
        score: int = Field(ge=1, le=5, description="how sure")

    prop = strict_schema(Bounded)["properties"]["score"]
    assert prop["type"] == "integer"
    assert "minimum" not in prop and "maximum" not in prop
    assert prop["description"].startswith("how sure\n\n{")
    assert '"maximum": 5' not in prop["description"]  # rendered as key: value
    assert "maximum: 5" in prop["description"] and "minimum: 1" in prop["description"]


def test_a_free_form_mapping_is_refused():
    class Loose(BaseModel):
        extra: dict[str, int]

    with pytest.raises(ValueError):
        strict_schema(Loose)
