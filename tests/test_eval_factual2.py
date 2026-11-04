"""The fresh factual set for the writer-fidelity eval (evals/answer_quality/
factual2.json, spec 2026-11-04): its shape, its registration, and that it
shares no question or id with the sets that came before it."""
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

EVALS = Path(__file__).parents[1] / "evals" / "answer_quality"
NEW = json.loads((EVALS / "factual2.json").read_text())
OLD_SETS = ("questions", "heldout", "arc", "interpretive", "deep", "recap")
OLD = [(name, q) for name in OLD_SETS for q in json.loads((EVALS / f"{name}.json").read_text())]
SIS, SOF = "05ab674df048d13f", "0ee225a9a3b5fbc2"


def _module(name):
    sys.path.insert(0, str(EVALS))
    spec = importlib.util.spec_from_file_location(f"{name}_factual2", EVALS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_set_has_24_questions_with_key_facts():
    assert len(NEW) == 24
    assert [q["id"] for q in NEW] == [f"F{i}" for i in range(1, 25)]
    for q in NEW:
        assert q["question"].strip().endswith("?"), q["id"]
        assert q["verified"].strip(), q["id"]
        assert q["key_facts"] and all(isinstance(k, str) and k.strip() for k in q["key_facts"]), q["id"]
        assert q["type"] in ("single", "multi", "comparison"), q["id"]
        assert q["evidence"] and all(re.fullmatch(r"[0-9a-f]{16}:\d+:\d+", e) for e in q["evidence"])


def test_the_set_mixes_single_multi_fact_and_comparison_questions_over_both_books():
    types = [q["type"] for q in NEW]
    assert types.count("single") >= 3 and types.count("multi") >= 10 and types.count("comparison") >= 5
    books = {e.split(":")[0] for q in NEW for e in q["evidence"]}
    assert books == {SIS, SOF}
    for q in NEW:  # a comparison draws on both books
        if q["type"] == "comparison":
            assert {e.split(":")[0] for e in q["evidence"]} == {SIS, SOF}, q["id"]


def test_no_id_is_shared_with_an_earlier_set():
    assert not {q["id"] for q in NEW} & {q["id"] for _, q in OLD}


def _words(text: str) -> set[str]:
    stop = set("a an the and or of to in on at by for with from as is was were be what which who"
               " whom how why did does do each his her their its it he she they that this when"
               " where novel novels book books both two".split())
    return {w for w in re.findall(r"[a-z]+", text.lower()) if w not in stop and len(w) > 2}


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def test_no_question_repeats_or_closely_paraphrases_an_earlier_one():
    for q in NEW:
        for name, old in OLD:
            assert _norm(q["question"]) != _norm(old["question"]), (q["id"], name, old["id"])
            a, b = _words(q["question"]), _words(old["question"])
            # Checked 2026-11-04: the closest pair shares 0.33 (names only).
            assert len(a & b) / len(a | b) < 0.45, (q["id"], name, old["id"])


def test_no_key_fact_repeats_an_earlier_key_fact():
    old_facts = [(old["id"], k) for _, old in OLD for k in old.get("key_facts", []) if isinstance(k, str)]
    for q in NEW:
        for k in q["key_facts"]:
            for oid, ok in old_facts:
                assert _norm(k) != _norm(ok), (q["id"], oid)
                a, b = _words(k), _words(ok)
                if len(a) >= 3:
                    assert len(a & b) / len(a | b) < 0.6, (q["id"], k, oid, ok)


def test_the_set_is_registered_for_every_runner():
    assert _module("run_eval").SETS["factual2"] == EVALS / "factual2.json"
    assert _module("grade_pair").SETS["factual2"] == EVALS / "factual2.json"
    assert "factual2" in _module("run_pair").SETS
