"""One strict JSON schema for Claude's and OpenAI's structured outputs.

Both providers constrain decoding to a schema, and both accept only a strict
subset of JSON Schema: every object has `additionalProperties: false` and
lists EVERY property in `required`. LORE's pydantic schemas are not written
that way (a field with a default is optional), so this module is the one
place they are converted:

- every object gets `additionalProperties: false` and all its properties in
  `required`;
- a property that was optional becomes NULLABLE (`anyOf [..., null]`), so the
  model can still leave it out by answering null; `decode` then drops those
  nulls again, so pydantic applies the field's own default exactly as it
  would have when the field was missing;
- `default` and `title` are dropped (not supported, and not needed);
- a `$ref` with siblings keeps only the `$ref`; `oneOf` becomes `anyOf`;
- keywords neither provider enforces (`minimum`, `maximum`, `minLength`, a
  `minItems` above 1, unknown string formats, ...) move into the property's
  description, so the model still reads them; pydantic still enforces them
  on the reply, and a reply that breaks one is an ordinary output error.

Gemini does not use this: its requests stay exactly as they were.
"""
from __future__ import annotations

import json
from typing import Any, TypeVar

from pydantic import BaseModel

_STRING_FORMATS = {"date-time", "time", "date", "duration", "email", "hostname",
                   "uri", "ipv4", "ipv6", "uuid"}
_NULL = {"type": "null"}

Model = TypeVar("Model", bound=BaseModel)


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """`model`'s JSON schema in the strict form both providers accept."""
    return _strict(model.model_json_schema())


def _nullable(node: dict[str, Any]) -> dict[str, Any]:
    variants = node.get("anyOf")
    if isinstance(variants, list):
        if _NULL not in variants:
            node = {**node, "anyOf": [*variants, dict(_NULL)]}
        return node
    if node.get("type") == "null":
        return node
    description = node.get("description")
    inner = {k: v for k, v in node.items() if k != "description"}
    out: dict[str, Any] = {"anyOf": [inner, dict(_NULL)]}
    if description is not None:
        out["description"] = description
    return out


def _strict(source: dict[str, Any]) -> dict[str, Any]:
    node = dict(source)
    out: dict[str, Any] = {}

    defs = node.pop("$defs", None)
    if isinstance(defs, dict):
        out["$defs"] = {name: _strict(sub) for name, sub in defs.items()}

    ref = node.pop("$ref", None)
    if ref is not None:
        out["$ref"] = ref
        return out

    node.pop("title", None)
    node.pop("default", None)
    any_of = node.pop("anyOf", None)
    one_of = node.pop("oneOf", None)
    all_of = node.pop("allOf", None)
    type_ = node.pop("type", None)
    if isinstance(any_of, list) or isinstance(one_of, list):
        out["anyOf"] = [_strict(v) for v in (any_of if isinstance(any_of, list) else one_of)]
    elif isinstance(all_of, list):
        if len(all_of) == 1:
            merged = _strict(all_of[0])
            out.update(merged)
        else:
            out["allOf"] = [_strict(v) for v in all_of]
    if type_ is not None:
        out["type"] = type_

    if "const" in node:
        out["enum"] = [node.pop("const")]
    enum = node.pop("enum", None)
    if isinstance(enum, list):
        out["enum"] = enum
    description = node.pop("description", None)
    if description is not None:
        out["description"] = description

    if type_ == "object":
        properties = node.pop("properties", {}) or {}
        required = set(node.pop("required", []) or [])
        extra = node.pop("additionalProperties", None)
        if isinstance(extra, dict) or extra is True:
            raise ValueError("a free-form mapping (dict field) cannot be sent as a"
                             " strict schema; give it named properties")
        strict_props = {}
        for name, prop in properties.items():
            converted = _strict(prop)
            strict_props[name] = converted if name in required else _nullable(converted)
        out["properties"] = strict_props
        out["required"] = list(properties)
        out["additionalProperties"] = False
    elif type_ == "string":
        fmt = node.pop("format", None)
        if fmt in _STRING_FORMATS:
            out["format"] = fmt
        elif fmt is not None:
            node["format"] = fmt
    elif type_ == "array":
        items = node.pop("items", None)
        if isinstance(items, dict):
            out["items"] = _strict(items)
        min_items = node.pop("minItems", None)
        if min_items in (0, 1):
            out["minItems"] = min_items
        elif min_items is not None:
            node["minItems"] = min_items

    if node:  # what neither provider enforces: tell the model in words
        words = ", ".join(f"{k}: {json.dumps(v)}" for k, v in sorted(node.items()))
        out["description"] = (f"{out['description']}\n\n" if "description" in out else "") \
            + "{" + words + "}"
    return out


# --- decoding ------------------------------------------------------------------------

def _resolve(node: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    seen = 0
    while isinstance(node, dict) and "$ref" in node and seen < 32:
        path = node["$ref"].removeprefix("#/").split("/")
        target: Any = root
        for part in path:
            target = target[part]
        node, seen = target, seen + 1
    return node


def _allows_null(node: dict[str, Any], root: dict[str, Any]) -> bool:
    node = _resolve(node, root)
    if node.get("type") == "null":
        return True
    return any(_allows_null(v, root) for v in node.get("anyOf", []) or [])


def _drop(value: Any, node: dict[str, Any], root: dict[str, Any]) -> Any:
    node = _resolve(node, root)
    if isinstance(value, dict):
        branches = [node] + [_resolve(v, root) for v in node.get("anyOf", []) or []]
        for branch in branches:
            properties = branch.get("properties")
            if not isinstance(properties, dict):
                continue
            required = set(branch.get("required", []) or [])
            out = {}
            for key, item in value.items():
                prop = properties.get(key)
                if prop is None:
                    out[key] = item
                    continue
                if item is None and key not in required and not _allows_null(prop, root):
                    continue  # optional and answered null: let the default apply
                out[key] = _drop(item, prop, root)
            return out
        return value
    if isinstance(value, list):
        items = node.get("items")
        if not isinstance(items, dict):
            for branch in node.get("anyOf", []) or []:
                branch = _resolve(branch, root)
                if isinstance(branch.get("items"), dict):
                    items = branch["items"]
                    break
        if isinstance(items, dict):
            return [_drop(v, items, root) for v in value]
    return value


def drop_optional_nulls(data: Any, model: type[BaseModel]) -> Any:
    """`data` without the nulls the strict schema forced on optional fields."""
    root = model.model_json_schema()
    return _drop(data, root, root)


def decode(text: str, model: type[Model]) -> Model:
    """Parse a strict-schema reply into `model` (json and pydantic errors raise)."""
    data = json.loads(text)
    return model.model_validate(drop_optional_nulls(data, model))
