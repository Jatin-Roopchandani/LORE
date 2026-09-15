import pytest

from lore.query.models import EdgeRow, Evidence, Passage
from lore.query.synthesize import REFUSAL, Synthesis, cited_numbers, synthesize

P1 = Passage(n=1, chunk_id="b1:1:0", book_id="b1", chapter_seq=1, text="Holmes spoke.")
P2 = Passage(n=2, chunk_id="b1:2:0", book_id="b1", chapter_seq=2, text="Watson wrote.")
EV = Evidence(edges=(), passages=(P1, P2), truncated=False)


def _generate(result: Synthesis, sink: dict | None = None):
    async def generate(prompt: str, schema):
        if sink is not None:
            sink["prompt"] = prompt
        return result
    return generate


def _edge(subject="Holmes", predicate="OPPOSES", raw="distrusts", obj="Moriarty",
          book="b1", chapter=1, chunk="b1:1:0") -> EdgeRow:
    return EdgeRow(subject, predicate, raw, obj, "ev", 0.9, book, chapter, chunk)


async def _prompt(evidence: Evidence, **kwargs) -> str:
    sink: dict = {}
    await synthesize("q", evidence, _generate(
        Synthesis(answer="a [1]", can_answer=True), sink), mode="graph", **kwargs)
    return sink["prompt"]


async def _text(answer: str, can_answer: bool = True, evidence: Evidence = EV) -> str:
    result = await synthesize(
        "q", evidence, _generate(Synthesis(answer=answer, can_answer=can_answer)),
        mode="graph",
    )
    return result.text


# --- the plan's seven ------------------------------------------------------

@pytest.mark.asyncio
async def test_a_grounded_answer_keeps_its_passages() -> None:
    answer = await synthesize(
        "q", EV, _generate(Synthesis(answer="Holmes spoke [1].", can_answer=True)),
        mode="graph", template="neighbours",
    )
    assert answer.text == "Holmes spoke [1]."
    assert answer.mode == "graph" and answer.template == "neighbours"
    assert answer.passages == (P1, P2)


@pytest.mark.asyncio
async def test_the_model_refusing_produces_the_refusal_text() -> None:
    assert await _text("I think maybe [1]...", can_answer=False) == REFUSAL


@pytest.mark.asyncio
async def test_no_passages_refuses_without_calling_the_model() -> None:
    called = False

    async def generate(prompt: str, schema):
        nonlocal called
        called = True
        raise AssertionError("must not reach the model")

    answer = await synthesize(
        "q", Evidence(edges=(), passages=(), truncated=False), generate, mode="empty"
    )
    assert answer.text == REFUSAL
    assert answer.mode == "empty"
    assert called is False


@pytest.mark.asyncio
async def test_the_prompt_numbers_every_passage_and_forbids_outside_knowledge() -> None:
    prompt = await _prompt(EV)
    assert "[1]" in prompt and "[2]" in prompt
    assert "Holmes spoke." in prompt and "Watson wrote." in prompt
    assert "only" in prompt.lower()


@pytest.mark.asyncio
async def test_edges_are_shown_to_the_model_when_present() -> None:
    # Changed from the plan: the line carries predicate_raw, not the
    # curated predicate (see the edge-line test below).
    prompt = await _prompt(Evidence(edges=(_edge(),), passages=(P1,), truncated=False))
    assert "Holmes" in prompt and "distrusts" in prompt and "Moriarty" in prompt


@pytest.mark.asyncio
async def test_the_note_travels_onto_the_answer() -> None:
    answer = await synthesize(
        "q", EV, _generate(Synthesis(answer="a [1]", can_answer=True)),
        mode="vector-fallback", note="the graph contributed nothing",
    )
    assert answer.note == "the graph contributed nothing"
    assert answer.mode == "vector-fallback"


@pytest.mark.asyncio
async def test_truncated_evidence_is_disclosed_in_the_prompt() -> None:
    prompt = await _prompt(Evidence(edges=(), passages=(P1,), truncated=True))
    assert "truncated" in prompt.lower()


# --- change 2: titles, not hex ids -----------------------------------------

@pytest.mark.asyncio
async def test_passage_headers_and_edge_lines_show_book_titles() -> None:
    q1 = Passage(n=1, chunk_id="05ab:1:0", book_id="05ab", chapter_seq=1, text="x")
    q2 = Passage(n=2, chunk_id="0ee2:3:0", book_id="0ee2", chapter_seq=3, text="y")
    edge = _edge(book="0ee2", chapter=3, chunk="0ee2:3:0")
    prompt = await _prompt(
        Evidence(edges=(edge,), passages=(q1, q2), truncated=False),
        book_titles={"05ab": "A Study in Scarlet", "0ee2": "The Sign of the Four"},
    )
    assert "A Study in Scarlet" in prompt
    assert prompt.count("The Sign of the Four") == 2  # the header and the edge
    assert "05ab" not in prompt.replace("05ab:1:0", "")  # chunk ids aside
    assert "0ee2" not in prompt.replace("0ee2:3:0", "")


@pytest.mark.asyncio
async def test_a_missing_title_falls_back_to_the_book_id() -> None:
    prompt = await _prompt(EV, book_titles={"other": "Other Book"})
    assert "b1" in prompt and "Other Book" not in prompt
    assert "b1" in await _prompt(EV)  # no titles at all


# --- change 3: relationship lines are leads, tied to a passage ------------

@pytest.mark.asyncio
async def test_edge_lines_use_predicate_raw_and_the_kept_passage_number() -> None:
    edge = _edge(subject="Mr. Drebber", predicate="UNCATEGORIZED", raw="killed",
                 obj="John Ferrier", chapter=2, chunk="b1:2:0")
    prompt = await _prompt(Evidence(edges=(edge,), passages=(P1, P2), truncated=False))
    line = next(l for l in prompt.splitlines() if "Mr. Drebber" in l)
    assert "killed" in line and "John Ferrier" in line
    assert line.rstrip().endswith("[2]") or " [2]" in line
    assert "UNCATEGORIZED" not in prompt


@pytest.mark.asyncio
async def test_an_edge_whose_chunk_was_not_kept_is_omitted() -> None:
    kept = _edge(subject="Kept", chunk="b1:1:0")
    dropped = _edge(subject="Dropped", chunk="b1:9:0", chapter=9)
    prompt = await _prompt(Evidence(edges=(kept, dropped), passages=(P1,), truncated=True))
    assert "Kept" in prompt
    assert "Dropped" not in prompt


@pytest.mark.asyncio
async def test_the_prompt_calls_relationships_possibly_wrong_leads() -> None:
    prompt = (await _prompt(
        Evidence(edges=(_edge(),), passages=(P1,), truncated=False))).lower()
    assert "lead" in prompt
    assert "wrong" in prompt
    assert "cited passage" in prompt


# --- change 4: a citation that points nowhere is a refusal ----------------

@pytest.mark.asyncio
async def test_an_answer_with_no_citation_is_refused() -> None:
    assert await _text("Holmes spoke.") == REFUSAL


@pytest.mark.asyncio
async def test_an_out_of_range_citation_is_refused() -> None:
    assert await _text("Holmes spoke [1] and Moriarty fled [3].") == REFUSAL
    assert await _text("Holmes spoke [0].") == REFUSAL
    assert await _text("Holmes spoke [1, 7].") == REFUSAL


@pytest.mark.asyncio
async def test_grouped_and_adjacent_citations_are_accepted() -> None:
    assert await _text("Both wrote [1, 2].") == "Both wrote [1, 2]."
    assert await _text("Both wrote [1][2].") == "Both wrote [1][2]."
    assert await _text("Both wrote [1,2].") == "Both wrote [1,2]."


def test_cited_numbers_parses_the_accepted_forms_only() -> None:
    assert cited_numbers("a [1] b [2, 3] c [4][5] d [6,7]") == [1, 2, 3, 4, 5, 6, 7]
    assert cited_numbers("no citations here") == []
    assert cited_numbers("a range [1-3] is not a citation form") == []
