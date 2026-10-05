import json

import pytest

from lore.mcp.shapes import (
    ERROR_CODES, IngestStatus, SearchResult, SubgraphOut, error, to_search_result,
)
from lore.query.models import Answer, Claim, EdgeRow, Passage, SeedEntity


def test_error_envelope_shape():
    payload = error("not_found", "no entity named 'Nobody'")
    assert payload == {"error": {"code": "not_found",
                                 "message": "no entity named 'Nobody'"}}


def test_every_code_is_declared():
    assert ERROR_CODES == frozenset(
        {"no_graph", "budget_exhausted", "cypher_rejected", "not_found", "timeout",
         "upstream_unavailable"}
    )


def test_an_undeclared_code_raises_value_error():
    with pytest.raises(ValueError):
        error("made_up", "nope")


def _answer() -> Answer:
    return Answer(
        text="Jefferson Hope killed Enoch Drebber.",
        passages=(Passage(n=1, chunk_id="c1", book_id="b1", chapter_seq=3,
                          text="..."),),
        mode="graph",
        template="two_hop",
        note=None,
        model="gemini-3.5-flash-lite",
        selection_model="gemini-3.5-flash-lite",
        sub_questions=("Who killed Enoch Drebber?",),
        claims=(Claim(text="Jefferson Hope killed Enoch Drebber.",
                      passage=1, quote="Hope"),),
        edges=(EdgeRow(subject="Jefferson Hope", predicate="KILLS",
                       predicate_raw="killed", object="Enoch Drebber",
                       evidence="...", confidence=0.9, book_id="b1",
                       chapter_seq=3, chunk_id="c1"),),
        seeds=(SeedEntity(id="e1", name="Mr. Jefferson Hope", kind="Character",
                          books=("b1",), score=0.5),),
    )


def test_to_search_result_carries_the_structure():
    result = to_search_result(_answer(), {"b1": "A Study in Scarlet"})
    assert isinstance(result, SearchResult)
    assert result.answer.startswith("Jefferson Hope")
    assert result.mode == "graph"
    assert [c.quote for c in result.claims] == ["Hope"]
    assert [e.predicate for e in result.edges] == ["KILLS"]
    assert [s.name for s in result.seeds] == ["Mr. Jefferson Hope"]


def test_book_titles_are_resolved_for_the_reader():
    result = to_search_result(_answer(), {"b1": "A Study in Scarlet"})
    assert result.passages[0].book_title == "A Study in Scarlet"


def test_an_unknown_book_id_falls_back_to_the_id():
    result = to_search_result(_answer(), {})
    assert result.passages[0].book_title == "b1"


def test_a_baseline_answer_serialises_with_empty_structure():
    answer = Answer(text="...", passages=(), mode="vector-baseline",
                    template=None, note=None)
    result = to_search_result(answer, {})
    assert result.claims == []
    assert result.edges == []
    assert result.seeds == []


def test_the_result_is_plain_json_with_lists_not_tuples():
    dumped = to_search_result(_answer(), {"b1": "T"}).model_dump()
    json.dumps(dumped)
    assert dumped["sub_questions"] == ["Who killed Enoch Drebber?"]
    assert dumped["seeds"][0]["books"] == ["b1"]


def test_subgraph_carries_the_depth_walked_and_a_clamp_note():
    sub = SubgraphOut(nodes=[], edges=[], truncated=False, depth=2)
    assert sub.note is None
    json.dumps(sub.model_dump())


def test_a_finite_seed_score_passes_through():
    result = to_search_result(_answer(), {})
    assert result.seeds[0].score == 0.5


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_a_non_finite_seed_score_becomes_null(bad):
    answer = _answer()
    seed = SeedEntity(id="e1", name="n", kind="Character", books=("b1",), score=bad)
    answer = Answer(**{**answer.__dict__, "seeds": (seed,)})
    result = to_search_result(answer, {})
    assert result.seeds[0].score is None
    json.dumps(result.model_dump(), allow_nan=False)


def test_every_float_in_a_full_result_is_strict_json():
    json.dumps(to_search_result(_answer(), {"b1": "T"}).model_dump(),
               allow_nan=False)


def test_ingest_status_accepts_the_shape_task_7_builds():
    jobs = [
        {"kind": "extract", "state": "done", "detail": None,
         "updated_at": "2026-09-24 10:00:00"},
        {"kind": "embed", "state": "dead", "detail": "boom",
         "updated_at": "2026-09-24 10:05:00"},
    ]
    dead = [j for j in jobs if j["state"] == "dead"]
    out = IngestStatus(book_id="b1", title="T", status="failed",
                       jobs=jobs, dead_letters=dead).model_dump()
    assert out["jobs"][0]["detail"] is None
    assert [j["kind"] for j in out["dead_letters"]] == ["embed"]
    json.dumps(out, allow_nan=False)


def test_chunk_hit_distance_may_be_none_and_stays_strict_json():
    import json
    from lore.mcp.shapes import ChunkHit
    hit = ChunkHit(chunk_id="c", book_id="b", book_title="B", chapter_seq=1,
                   text="t", distance=None).model_dump()
    assert hit["distance"] is None
    json.dumps(hit, allow_nan=False)
