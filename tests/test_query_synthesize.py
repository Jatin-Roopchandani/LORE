import pytest

from lore.query.models import EdgeRow, Evidence, Passage
from lore.query.synthesize import (
    PARTIAL_NOTE,
    REFUSAL,
    Claim,
    Synthesis,
    synthesize,
    verify_claims,
)

P1 = Passage(n=1, chunk_id="b1:1:0", book_id="b1", chapter_seq=1,
             text="Holmes spoke of the case at length.")
P2 = Passage(n=2, chunk_id="b1:2:0", book_id="b1", chapter_seq=2,
             text="Watson wrote it all down in his notebook.")
EV = Evidence(edges=(), passages=(P1, P2), truncated=False)

GOOD1 = Claim(text="Holmes discussed the case.", passage=1,
              quote="Holmes spoke of the case")
GOOD2 = Claim(text="Watson took notes.", passage=2,
              quote="wrote it all down in his notebook")


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
        Synthesis(claims=[GOOD1], can_answer=True), sink), mode="graph", **kwargs)
    return sink["prompt"]


async def _text(*claims: Claim, can_answer: bool = True, evidence: Evidence = EV) -> str:
    result = await synthesize(
        "q", evidence,
        _generate(Synthesis(claims=list(claims), can_answer=can_answer)),
        mode="graph",
    )
    return result.text


def _dropped(*claims: Claim, passages=(P1, P2)) -> list[str]:
    kept, dropped = verify_claims(Synthesis(claims=list(claims), can_answer=True),
                                  passages)
    assert kept == []
    return [reason for _, reason in dropped]


# --- the plan's seven, on the claims schema --------------------------------

@pytest.mark.asyncio
async def test_a_grounded_answer_keeps_its_passages() -> None:
    answer = await synthesize(
        "q", EV, _generate(Synthesis(claims=[GOOD1], can_answer=True)),
        mode="graph", template="neighbours",
    )
    assert answer.text == "Holmes discussed the case. [1]"
    assert answer.mode == "graph" and answer.template == "neighbours"
    assert answer.passages == (P1, P2)


@pytest.mark.asyncio
async def test_the_model_refusing_produces_the_refusal_text() -> None:
    assert await _text(can_answer=False) == REFUSAL


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
    assert P1.text in prompt and P2.text in prompt
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
        "q", EV, _generate(Synthesis(claims=[GOOD1], can_answer=True)),
        mode="vector-fallback", note="the graph contributed nothing",
    )
    assert answer.note == "the graph contributed nothing"
    assert answer.mode == "vector-fallback"


@pytest.mark.asyncio
async def test_truncated_evidence_is_disclosed_in_the_prompt() -> None:
    prompt = await _prompt(Evidence(edges=(), passages=(P1,), truncated=True))
    assert "truncated" in prompt.lower()


# --- Task 7 change 2: titles, not hex ids ----------------------------------

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


# --- Task 7 change 3: relationship lines are leads, tied to a passage -----

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


# --- Task 7b: quote-checked claims -----------------------------------------

@pytest.mark.asyncio
async def test_the_prompt_demands_word_for_word_quotes_checked_by_code() -> None:
    prompt = (await _prompt(EV)).lower()
    assert "word for word" in prompt
    assert "checked" in prompt and "discarded" in prompt
    assert "combine" in prompt
    assert "can_answer" in prompt


@pytest.mark.asyncio
async def test_a_claim_with_an_exact_quote_is_kept_and_ends_with_its_number() -> None:
    kept, dropped = verify_claims(Synthesis(claims=[GOOD2], can_answer=True), EV.passages)
    assert kept == [GOOD2] and dropped == []
    text = await _text(GOOD2)
    assert text.endswith(" [2]")
    assert text == "Watson took notes. [2]"


@pytest.mark.asyncio
async def test_kept_claims_are_joined_in_order_with_their_numbers() -> None:
    assert await _text(GOOD1, GOOD2) == (
        "Holmes discussed the case. [1] Watson took notes. [2]")


def test_a_quote_from_a_different_passage_is_dropped() -> None:
    # Q4's failure shape: a real quote, cited to a passage that does not say it.
    misplaced = Claim(text="Watson took notes.", passage=1,
                      quote="wrote it all down in his notebook")
    assert _dropped(misplaced) == ["quote not in passage"]


def test_an_out_of_range_passage_number_is_dropped() -> None:
    assert _dropped(Claim(text="t", passage=3, quote="Holmes spoke of the case")) == [
        "bad passage number"]
    assert _dropped(Claim(text="t", passage=0, quote="Holmes spoke of the case")) == [
        "bad passage number"]


def test_a_three_word_quote_is_dropped_as_too_short() -> None:
    assert _dropped(Claim(text="t", passage=1, quote="Holmes spoke of")) == [
        "quote too short"]


def test_a_quote_that_is_not_in_any_passage_is_dropped() -> None:
    assert _dropped(Claim(text="t", passage=1, quote="Moriarty spoke of the case")) == [
        "quote not in passage"]


def test_normalisation_forgives_quotes_dashes_whitespace_and_case_only() -> None:
    p = Passage(n=1, chunk_id="b1:1:0", book_id="b1", chapter_seq=1,
                text="“It’s a long—drawn   affair,”\nsaid Holmes – quietly.")
    synthesis = Synthesis(can_answer=True, claims=[
        Claim(text="a", passage=1, quote="\"it's A LONG-drawn affair,\" said holmes - quietly."),
        Claim(text="b", passage=1, quote="  It’s a long–drawn affair  "),
    ])
    kept, dropped = verify_claims(synthesis, (p,))
    assert [c.text for c in kept] == ["a", "b"] and dropped == []

    one_word_changed = Claim(text="c", passage=1, quote="it's a short-drawn affair")
    no_punctuation = Claim(text="d", passage=1, quote="its a long drawn affair")
    assert _dropped(one_word_changed, no_punctuation, passages=(p,)) == [
        "quote not in passage", "quote not in passage"]


def test_a_quote_must_match_whole_words() -> None:
    assert _dropped(Claim(text="t", passage=1, quote="olmes spoke of the cas")) == [
        "quote not in passage"]


@pytest.mark.asyncio
async def test_all_claims_dropped_is_a_refusal() -> None:
    bad = Claim(text="Moriarty fled.", passage=1, quote="Moriarty fled the scene")
    assert await _text(bad) == REFUSAL
    assert await _text() == REFUSAL  # no claims at all


@pytest.mark.asyncio
async def test_can_answer_false_keeps_verified_claims_as_a_partial_answer() -> None:
    # Task 10d changed this. It was `can_answer_false_refuses_even_with_valid_claims`:
    #     assert await _text(GOOD1, GOOD2, can_answer=False) == REFUSAL
    # Task 10a measured H6 and H4 throwing away verified gold claims that way.
    answer = await synthesize(
        "q", EV, _generate(Synthesis(claims=[GOOD1, GOOD2], can_answer=False)),
        mode="graph")
    assert answer.text == "Holmes discussed the case. [1] Watson took notes. [2]"
    assert answer.note == PARTIAL_NOTE
    assert "partly" in PARTIAL_NOTE


@pytest.mark.asyncio
async def test_can_answer_false_with_no_surviving_claim_refuses() -> None:
    bad = Claim(text="Moriarty fled.", passage=1, quote="Moriarty fled the scene")
    assert await _text(bad, can_answer=False) == REFUSAL
    answer = await synthesize("q", EV, _generate(Synthesis(claims=[], can_answer=False)),
                              mode="graph")
    assert answer.text == REFUSAL and answer.note is None


@pytest.mark.asyncio
async def test_a_partial_answer_keeps_an_existing_note_too() -> None:
    answer = await synthesize(
        "q", EV, _generate(Synthesis(claims=[GOOD1], can_answer=False)),
        mode="vector-fallback", note="the graph had nothing")
    assert answer.note.startswith("the graph had nothing")
    assert PARTIAL_NOTE in answer.note


# --- Task 10d: quotes with an ellipsis ----------------------------------------

LONG = Passage(n=1, chunk_id="b1:1:0", book_id="b1", chapter_seq=1,
               text="The ring, man, the ring: that was what he came back for. "
                    "He pretended to be drunk at the gate, and the constable let him go.")
OTHER = Passage(n=2, chunk_id="b1:2:0", book_id="b1", chapter_seq=2,
                text="He pretended to be drunk at the gate, said the cabman.")


def _kept(quote: str, passage: int = 1) -> bool:
    kept, _ = verify_claims(
        Synthesis(claims=[Claim(text="t", passage=passage, quote=quote)], can_answer=True),
        (LONG, OTHER))
    return bool(kept)


@pytest.mark.parametrize("dots", ["...", "…", " ... "])
def test_an_ellipsis_quote_whose_segments_verify_in_order_is_kept(dots) -> None:
    assert _kept(f"that was what he came back for{dots}pretended to be drunk at the gate")


def test_an_ellipsis_quote_with_segments_out_of_order_is_dropped() -> None:
    reasons = _dropped(Claim(text="t", passage=1,
                             quote="pretended to be drunk at the gate ... "
                                   "that was what he came back for"), passages=(LONG, OTHER))
    assert reasons == ["quote not in passage"]


def test_an_ellipsis_segment_from_a_different_passage_is_dropped() -> None:
    # Each segment exists somewhere, but not both in passage 2.
    assert not _kept("that was what he came back for ... pretended to be drunk at the gate",
                     passage=2)
    assert _dropped(Claim(text="t", passage=2,
                          quote="that was what he came back for ... said the cabman"),
                    passages=(LONG, OTHER)) == ["quote not in passage"]


def test_every_ellipsis_segment_needs_three_words_and_the_quote_four() -> None:
    assert _dropped(Claim(text="t", passage=1, quote="the ring ... came back for"),
                    passages=(LONG, OTHER)) == ["quote too short"]
    assert _kept("The ring, man, ... came back for")  # 3 + 3 words


def test_a_leading_or_trailing_ellipsis_is_not_a_segment() -> None:
    assert _dropped(Claim(text="t", passage=1, quote="... came back for ..."),
                    passages=(LONG, OTHER)) == ["quote too short"]  # 4 words overall
    assert _kept("...that was what he came back for...")


def test_ellipsis_segments_still_match_whole_words() -> None:
    assert not _kept("hat was what he came ... pretended to be drunk")


@pytest.mark.asyncio
async def test_only_kept_claims_reach_the_answer() -> None:
    bad = Claim(text="Holmes fled.", passage=2, quote="Holmes spoke of the case")
    assert await _text(bad, GOOD2) == "Watson took notes. [2]"


@pytest.mark.asyncio
async def test_duplicate_claims_collapse_to_one() -> None:
    assert await _text(GOOD1, GOOD2, GOOD1) == (
        "Holmes discussed the case. [1] Watson took notes. [2]")


def test_dropped_claims_carry_the_claim_itself() -> None:
    bad = Claim(text="t", passage=9, quote="Holmes spoke of the case")
    kept, dropped = verify_claims(Synthesis(claims=[GOOD1, bad], can_answer=True),
                                  EV.passages)
    assert kept == [GOOD1]
    assert dropped == [(bad, "bad passage number")]


# --- Task 10d: the sub-questions as a checklist ---------------------------------

@pytest.mark.asyncio
async def test_sub_questions_reach_the_prompt_as_a_checklist() -> None:
    sink: dict = {}
    await synthesize("Who and why?", EV, _generate(Synthesis(claims=[GOOD1], can_answer=True),
                                                   sink),
                     mode="graph", checklist=["Who did it?", "Why did they?"])
    prompt = sink["prompt"]
    assert "Question: Who and why?" in prompt
    assert "make sure the answer covers" in prompt.lower()
    assert "- Who did it?" in prompt and "- Why did they?" in prompt


@pytest.mark.asyncio
async def test_a_single_part_gets_no_checklist() -> None:
    sink: dict = {}
    await synthesize("q", EV, _generate(Synthesis(claims=[GOOD1], can_answer=True), sink),
                     mode="graph", checklist=["q"])
    assert "covers" not in sink["prompt"].lower()
