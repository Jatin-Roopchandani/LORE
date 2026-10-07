"""The event helpers: emitting never fails an answer, payloads are JSON-safe."""
import json
import math

from lore.query.events import EDGES_PER_PART_CAP, edge_key, emit, json_safe
from lore.query.models import EdgeRow, SeedEntity


def test_emit_calls_the_hook_with_a_typed_dict():
    seen = []
    emit(seen.append, "step", name="split", state="start", part=None, detail=None)
    assert seen == [{"type": "step", "name": "split", "state": "start",
                     "part": None, "detail": None}]


def test_emit_without_a_hook_does_nothing():
    emit(None, "step", name="split", state="start")


def test_a_raising_hook_never_fails_the_caller(caplog):
    def broken(event):
        raise RuntimeError("socket closed")
    emit(broken, "parts", sub_questions=["a"])
    assert "socket closed" in caplog.text


def test_json_safe_turns_dataclasses_and_infinities_into_json():
    seed = SeedEntity(id="e1", name="Holmes", kind="Character", books=("b1",),
                      score=math.inf)
    out = json_safe({"entities": [seed]})
    assert out == {"entities": [{"id": "e1", "name": "Holmes", "kind": "Character",
                                 "books": ["b1"], "score": None}]}
    json.dumps(out, allow_nan=False)


def test_edge_key_is_subject_predicate_object_chunk():
    e = EdgeRow("Holmes", "KILLS", "k", "X", "ev", 0.9, "b1", 1, "b1:1:0",
                subject_id="e1", object_id="e2")
    assert edge_key(e) == "e1|KILLS|e2|b1:1:0"


def test_the_cap_is_150():
    assert EDGES_PER_PART_CAP == 150
