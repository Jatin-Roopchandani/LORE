import json

import pytest

from lore.mcp.shapes import (
    ERROR_CODES, SearchResult, SubgraphOut, error, to_search_result,
)
from lore.query.models import Answer, Claim, EdgeRow, Passage, SeedEntity


def test_error_envelope_shape():
    payload = error("not_found", "no entity named 'Nobody'")
    assert payload == {"error": {"code": "not_found",
                                 "message": "no entity named 'Nobody'"}}


def test_every_code_is_declared():
    assert ERROR_CODES == frozenset(
        {"no_graph", "budget_exhausted", "cypher_rejected", "not_found", "timeout"}
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
