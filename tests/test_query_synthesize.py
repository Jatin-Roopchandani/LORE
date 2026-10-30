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
    # the header, the edge, and (story order) the unknown-order note: these
    # two books have no series, so their relative order is unknown
    assert prompt.count("The Sign of the Four") == 3
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


# --- Task 10e: quotation marks do not decide a quote ---------------------------

SPEECH = Passage(n=1, chunk_id="b1:1:0", book_id="b1", chapter_seq=1,
                 text="“And his name is?”\n\n“Arthur Charpentier, sub-lieutenant,” cried "
                      "Gregson. ‘Hope’s ring,’ he said, 'is lost.' The boys' hats were wet.")


def _speech_kept(quote: str) -> bool:
    kept, _ = verify_claims(
        Synthesis(claims=[Claim(text="t", passage=1, quote=quote)], can_answer=True),
        (SPEECH,))
    return bool(kept)


@pytest.mark.parametrize("quote", [
    "And his name is? “Arthur Charpentier, sub-lieutenant,” cried Gregson",  # closing ” left out
    "And his name is?” Arthur Charpentier, sub-lieutenant, cried Gregson",   # opening “ left out
    '"And his name is? Arthur Charpentier, sub-lieutenant," cried Gregson',  # straight, misplaced
    "And his name is? Arthur Charpentier, sub-lieutenant, cried Gregson",    # none at all
    "Hope's ring, he said, is lost.",                                        # ‘ ’ and ' as marks
    "The boys hats were wet.",                                               # trailing ' mark
], ids=["no-closing", "no-opening", "straight", "none", "single-marks", "plural-possessive"])
def test_a_quote_differing_only_in_quotation_marks_is_kept(quote) -> None:
    assert _speech_kept(quote)


def test_an_apostrophe_inside_a_word_still_counts() -> None:
    assert _speech_kept("‘Hope’s ring,’ he said")
    assert _speech_kept("Hope's ring, he said")  # ' vs ’ inside the word
    # Dropping an in-word apostrophe is a different word, not a quotation mark.
    assert not _speech_kept("Hopes ring, he said, is lost")


def test_a_quote_with_one_word_changed_still_fails_despite_quotation_marks() -> None:
    assert not _speech_kept("And his name is? “Arthur Charpentier, sub-lieutenant,” cried Lestrade")
    assert not _speech_kept("“And her name is?” “Arthur Charpentier")


# --- Task 10e: one synthesis call per sub-question, merged ----------------------

from lore.query.synthesize import synthesize_parts  # noqa: E402

S1 = Passage(n=1, chunk_id="b1:1:0", book_id="b1", chapter_seq=1,
             text="Hope loved Lucy Ferrier and swore to avenge her.")
S2 = Passage(n=2, chunk_id="b1:2:0", book_id="b1", chapter_seq=2,
             text="Drebber had forced Lucy into a marriage she hated.")
S3 = Passage(n=3, chunk_id="b1:3:0", book_id="b1", chapter_seq=3,
             text="Stangerson shot her father John Ferrier dead.")
PARTS_EV = Evidence(edges=(_edge(subject="Drebber", chunk="b1:2:0", chapter=2),),
                    passages=(S1, S2, S3), truncated=False,
                    shares=((1, 2), (3,)))
WHY_D = Claim(text="Drebber forced Lucy to marry him.", passage=2,
              quote="forced Lucy into a marriage")
WHY_S = Claim(text="Stangerson shot her father.", passage=3,
              quote="Stangerson shot her father")
SUBS = ("Why kill Drebber?", "Why kill Stangerson?")


def _per_part(results: list[Synthesis], prompts: list[str]):
    async def generate(prompt: str, schema):
        prompts.append(prompt)
        return results[len(prompts) - 1]
    return generate


@pytest.mark.asyncio
async def test_each_sub_question_gets_its_own_call_over_its_own_passages() -> None:
    prompts: list[str] = []
    answer = await synthesize_parts(
        "Why did Hope kill Drebber and Stangerson?", SUBS, PARTS_EV,
        _per_part([Synthesis(claims=[WHY_D], can_answer=True),
                   Synthesis(claims=[WHY_S], can_answer=True)], prompts),
        mode="graph", template="path_between")
    assert len(prompts) == 2
    first, second = prompts
    assert "Question: Why kill Drebber?" in first and "Question: Why kill Stangerson?" in second
    # The original question travels as context to both.
    assert all("Why did Hope kill Drebber and Stangerson?" in p for p in prompts)
    # Each call sees only its share, under its GLOBAL number.
    assert "[1] (b1, chapter 1," in first and "[2] (b1, chapter 2," in first
    assert S3.text not in first
    assert "[3] (b1, chapter 3," in second and S1.text not in second and S2.text not in second
    # A lead is shown only beside a passage the call can cite.
    assert "Drebber —distrusts→" in first and "Drebber —distrusts→" not in second
    # Composed in sub-question order, numbering global, every passage kept.
    assert answer.text == ("Drebber forced Lucy to marry him. [2] "
                           "Stangerson shot her father. [3]")
    assert answer.passages == (S1, S2, S3)
    assert answer.note is None
    assert answer.mode == "graph" and answer.template == "path_between"


@pytest.mark.asyncio
async def test_a_claim_citing_a_passage_outside_its_share_is_dropped() -> None:
    prompts: list[str] = []
    stray = Claim(text="Stangerson shot her father.", passage=3,
                  quote="Stangerson shot her father")  # passage 3 is not part one's
    answer = await synthesize_parts(
        "q", SUBS, PARTS_EV,
        _per_part([Synthesis(claims=[stray, WHY_D], can_answer=True),
                   Synthesis(claims=[WHY_S], can_answer=True)], prompts), mode="graph")
    assert answer.text == ("Drebber forced Lucy to marry him. [2] "
                           "Stangerson shot her father. [3]")


@pytest.mark.asyncio
async def test_exact_duplicate_claims_across_parts_appear_once() -> None:
    prompts: list[str] = []
    shared = Evidence(edges=(), passages=(S1, S2, S3), truncated=False,
                      shares=((1, 2), (2, 3)))
    answer = await synthesize_parts(
        "q", SUBS, shared,
        _per_part([Synthesis(claims=[WHY_D], can_answer=True),
                   Synthesis(claims=[WHY_D, WHY_S], can_answer=True)], prompts), mode="graph")
    assert answer.text == ("Drebber forced Lucy to marry him. [2] "
                           "Stangerson shot her father. [3]")


@pytest.mark.asyncio
async def test_a_sub_question_with_no_kept_claim_is_named_in_the_partial_note() -> None:
    prompts: list[str] = []
    bad = Claim(text="Stangerson fled.", passage=3, quote="Stangerson fled to Utah alone")
    answer = await synthesize_parts(
        "q", SUBS, PARTS_EV,
        _per_part([Synthesis(claims=[WHY_D], can_answer=True),
                   Synthesis(claims=[bad], can_answer=True)], prompts),
        mode="graph", note="earlier note")
    assert answer.text == "Drebber forced Lucy to marry him. [2]"
    assert answer.note.startswith("earlier note")
    assert PARTIAL_NOTE in answer.note
    assert "not found: Why kill Stangerson?" in answer.note
    assert "Why kill Drebber?" not in answer.note


@pytest.mark.asyncio
async def test_can_answer_false_on_a_part_with_kept_claims_is_not_partial() -> None:
    prompts: list[str] = []
    answer = await synthesize_parts(
        "q", SUBS, PARTS_EV,
        _per_part([Synthesis(claims=[WHY_D], can_answer=False),
                   Synthesis(claims=[WHY_S], can_answer=False)], prompts), mode="graph")
    assert answer.note is None
    assert answer.text.endswith("[3]")


@pytest.mark.asyncio
async def test_no_sub_question_keeping_a_claim_is_a_refusal() -> None:
    prompts: list[str] = []
    answer = await synthesize_parts(
        "q", SUBS, PARTS_EV,
        _per_part([Synthesis(claims=[], can_answer=False),
                   Synthesis(claims=[], can_answer=False)], prompts), mode="graph")
    assert answer.text == REFUSAL and answer.note is None
    assert len(prompts) == 2


@pytest.mark.asyncio
async def test_a_sub_question_with_no_passages_is_not_asked_and_is_not_found() -> None:
    prompts: list[str] = []
    empty_share = Evidence(edges=(), passages=(S1, S2, S3), truncated=False,
                           shares=((1, 2, 3), ()))
    answer = await synthesize_parts(
        "q", SUBS, empty_share,
        _per_part([Synthesis(claims=[WHY_D, WHY_S], can_answer=True)], prompts), mode="graph")
    assert len(prompts) == 1
    assert "not found: Why kill Stangerson?" in answer.note


@pytest.mark.asyncio
async def test_a_single_part_question_is_one_call_exactly_as_today() -> None:
    today: dict = {}
    await synthesize("Who?", EV, _generate(Synthesis(claims=[GOOD1], can_answer=False),
                                           today), mode="graph")
    prompts: list[str] = []
    answer = await synthesize_parts(
        "Who?", ("Who?",), EV,
        _per_part([Synthesis(claims=[GOOD1], can_answer=False)], prompts), mode="graph")
    assert prompts == [today["prompt"]]
    assert answer.text == "Holmes discussed the case. [1]"
    assert answer.note == PARTIAL_NOTE  # can_answer=false keeps today's meaning


@pytest.mark.asyncio
async def test_parts_without_shares_fall_back_to_one_call_over_everything() -> None:
    prompts: list[str] = []
    answer = await synthesize_parts(
        "q", SUBS, EV, _per_part([Synthesis(claims=[GOOD1], can_answer=True)], prompts),
        mode="graph")
    assert len(prompts) == 1 and "- Why kill Drebber?" in prompts[0]
    assert answer.text == "Holmes discussed the case. [1]"


# --- Task 10f: ask for every supported fact, capped in code ----------------------

from lore.query.synthesize import MAX_CLAIMS  # noqa: E402


def test_the_claim_cap_is_eight() -> None:
    assert MAX_CLAIMS == 8


@pytest.mark.asyncio
async def test_the_prompt_asks_for_every_distinct_fact_as_its_own_claim() -> None:
    prompt = await _prompt(EV)
    assert "its own claim with its own quote" in prompt
    assert "its own claim with its own quote" in prompt
    assert "not one claim that summarises several" in prompt
    assert f"at most {MAX_CLAIMS} claims" in prompt


@pytest.mark.asyncio
async def test_the_prompt_prefers_passages_that_state_the_answer_directly() -> None:
    prompt = await _prompt(EV)
    assert "state the answer directly" in prompt
    assert "only mention" in prompt


@pytest.mark.asyncio
async def test_the_prompt_reserves_can_answer_false_for_no_bearing_passage() -> None:
    prompt = await _prompt(EV)
    assert "no passage bears on" in prompt


@pytest.mark.asyncio
async def test_a_part_prompt_asks_for_completeness_too() -> None:
    prompts: list[str] = []
    await synthesize_parts(
        "Why did Hope kill Drebber and Stangerson?", SUBS, PARTS_EV,
        _per_part([Synthesis(claims=[WHY_D], can_answer=True),
                   Synthesis(claims=[WHY_S], can_answer=True)], prompts), mode="graph")
    for prompt in prompts:
        assert "its own claim with its own quote" in prompt
        assert f"at most {MAX_CLAIMS} claims" in prompt


@pytest.mark.asyncio
async def test_the_existing_rules_survive_the_completeness_instruction() -> None:
    prompt = await _prompt(Evidence(edges=(_edge(),), passages=(P1, P2), truncated=False),
                           book_titles={"b1": "A Study in Scarlet"})
    assert "copied word for word" in prompt
    assert "They can be\nwrong" in prompt or "can be wrong" in prompt.replace("\n", " ")
    assert "A Study in Scarlet" in prompt


def _numbered(count: int, passage: Passage = P1) -> list[Claim]:
    # Every claim verifies against its passage; only the text tells them apart.
    return [Claim(text=f"fact {i}.", passage=passage.n, quote="Holmes spoke of the case")
            for i in range(count)]


@pytest.mark.asyncio
async def test_claims_beyond_the_cap_are_ignored_in_model_order() -> None:
    text = await _text(*_numbered(MAX_CLAIMS + 3))
    for i in range(MAX_CLAIMS):
        assert f"fact {i}. [1]" in text
    for i in range(MAX_CLAIMS, MAX_CLAIMS + 3):
        assert f"fact {i}." not in text


@pytest.mark.asyncio
async def test_the_cap_counts_proposed_claims_so_output_stays_bounded() -> None:
    # Deterministic and simple: the first MAX_CLAIMS the model wrote are the
    # ones considered, whether or not they verify.
    bad = [Claim(text=f"bad {i}.", passage=1, quote="not in the passage at all")
           for i in range(MAX_CLAIMS)]
    answer = await synthesize("q", EV, _generate(Synthesis(
        claims=bad + [GOOD1], can_answer=True)), mode="graph")
    assert answer.text == REFUSAL


@pytest.mark.asyncio
async def test_the_cap_applies_per_part() -> None:
    prompts: list[str] = []
    extra = [Claim(text=f"why {i}.", passage=2, quote="forced Lucy into a marriage")
             for i in range(MAX_CLAIMS + 2)]
    answer = await synthesize_parts(
        "Why did Hope kill Drebber and Stangerson?", SUBS, PARTS_EV,
        _per_part([Synthesis(claims=extra, can_answer=True),
                   Synthesis(claims=[WHY_S], can_answer=True)], prompts), mode="graph")
    assert f"why {MAX_CLAIMS - 1}. [2]" in answer.text
    assert f"why {MAX_CLAIMS}." not in answer.text
    assert "Stangerson shot her father. [3]" in answer.text


# --- Task 11b: near-duplicate claims collapse -------------------------------------

HABIT = Passage(n=1, chunk_id="b2:4:0", book_id="b2", chapter_seq=4,
                text="With a long, thin forefinger he adjusted the tiny needle and pressed "
                     "down the piston. His sinewy forearm was dotted and scarred with "
                     "innumerable puncture-marks. Twice a day for many weeks I had watched "
                     "this ritual, and my patience with it had worn thin.")
HABIT2 = Passage(n=2, chunk_id="b2:5:0", book_id="b2", chapter_seq=5,
                 text="Twice a day for many weeks I had watched this ritual, the clerk "
                      "wrote in his own diary.")
HABIT_EV = Evidence(edges=(), passages=(HABIT, HABIT2), truncated=False)

WATCHED = Claim(text="The narrator has watched the doctor perform this injection twice a day "
                     "for many weeks.", passage=1,
                quote="Twice a day for many weeks I had watched this ritual")
WATCHED_AGAIN = Claim(text="The narrator had watched the doctor perform this habit twice a day "
                           "for many weeks.", passage=1,
                      quote="for many weeks I had watched this ritual, and my patience")


async def _habit_text(*claims: Claim) -> str:
    return await _text(*claims, evidence=HABIT_EV)


@pytest.mark.asyncio
async def test_a_near_duplicate_claim_is_dropped_keeping_the_first() -> None:
    other = Claim(text="The doctor pressed down the piston.", passage=1,
                  quote="adjusted the tiny needle and pressed down the piston")
    assert await _habit_text(WATCHED, other, WATCHED_AGAIN) == (
        f"{WATCHED.text} [1] {other.text} [1]")
    assert await _habit_text(WATCHED_AGAIN, WATCHED) == f"{WATCHED_AGAIN.text} [1]"


@pytest.mark.asyncio
async def test_a_contained_quote_counts_as_the_same_evidence() -> None:
    inner = WATCHED_AGAIN.model_copy(update={"quote": "Twice a day for many weeks"})
    assert await _habit_text(WATCHED, inner) == f"{WATCHED.text} [1]"


@pytest.mark.asyncio
async def test_distinct_facts_from_the_same_quote_both_survive() -> None:
    quote = "pressed down the piston. His sinewy forearm was dotted and scarred"
    syringe = Claim(text="The doctor used a syringe on himself.", passage=1, quote=quote)
    scars = Claim(text="The doctor's forearm is scarred with puncture marks.", passage=1,
                  quote=quote)
    assert await _habit_text(syringe, scars) == f"{syringe.text} [1] {scars.text} [1]"


@pytest.mark.asyncio
async def test_similar_claims_citing_different_passages_both_survive() -> None:
    elsewhere = WATCHED_AGAIN.model_copy(update={
        "passage": 2, "quote": "Twice a day for many weeks I had watched this ritual"})
    assert await _habit_text(WATCHED, elsewhere) == (
        f"{WATCHED.text} [1] {elsewhere.text} [2]")


@pytest.mark.asyncio
async def test_similar_claims_with_unrelated_quotes_in_one_passage_both_survive() -> None:
    apart = WATCHED_AGAIN.model_copy(update={
        "quote": "his sinewy forearm was dotted and scarred"})
    assert await _habit_text(WATCHED, apart) == f"{WATCHED.text} [1] {apart.text} [1]"


@pytest.mark.asyncio
async def test_near_duplicates_from_different_parts_collapse() -> None:
    prompts: list[str] = []
    both = Evidence(edges=(), passages=(HABIT, HABIT2), truncated=False,
                    shares=((1,), (1, 2)))
    answer = await synthesize_parts(
        "q", SUBS, both,
        _per_part([Synthesis(claims=[WATCHED], can_answer=True),
                   Synthesis(claims=[WATCHED_AGAIN], can_answer=True)], prompts),
        mode="graph")
    assert answer.text == f"{WATCHED.text} [1]"
    assert answer.note is None  # the second part did keep a claim


@pytest.mark.asyncio
async def test_claims_differing_only_in_a_number_both_survive() -> None:
    quote = "Twice a day for many weeks I had watched this ritual"
    first = Claim(text="He watched it 2 times a day.", passage=1, quote=quote)
    second = Claim(text="He watched it 3 times a day.", passage=1, quote=quote)
    assert await _habit_text(first, second) == f"{first.text} [1] {second.text} [1]"


# --- Task 12: the faithfulness check over kept claims ------------------------------

from lore.query.synthesize import (  # noqa: E402
    CHECK_SKIPPED_NOTE,
    NOT_SUPPORTED,
    SUPPORTED,
    Faithfulness,
    Judgement,
    check_claims,
    validated_verdicts,
)

TITLES = {"b1": "A Study in Scarlet"}


def _judged(*verdicts: str, reason: str = "reason", start: int = 1) -> Faithfulness:
    return Faithfulness(judgements=[
        Judgement(claim=n, verdict=v, reason=reason)
        for n, v in enumerate(verdicts, start=start)])


def _checker(result: Faithfulness | None = None, prompts: list[str] | None = None,
             error: BaseException | None = None):
    async def check(prompt: str, schema):
        if prompts is not None:
            prompts.append(prompt)
        if error is not None:
            raise error
        return result
    return check


async def _checked(*claims: Claim, verdicts: Faithfulness | None = None,
                   evidence: Evidence = EV, prompts: list[str] | None = None,
                   error: BaseException | None = None, can_answer: bool = True):
    return await synthesize(
        "q", evidence, _generate(Synthesis(claims=list(claims), can_answer=can_answer)),
        mode="graph", book_titles=TITLES,
        check=_checker(verdicts, prompts, error))


@pytest.mark.asyncio
async def test_a_claim_the_check_calls_unsupported_is_dropped() -> None:
    answer = await _checked(GOOD1, GOOD2, verdicts=_judged(SUPPORTED, NOT_SUPPORTED))
    assert answer.text == "Holmes discussed the case. [1]"
    assert answer.note is None


@pytest.mark.asyncio
async def test_supported_claims_survive_in_the_order_they_were_kept() -> None:
    answer = await _checked(GOOD1, GOOD2, verdicts=_judged(SUPPORTED, SUPPORTED))
    assert answer.text == "Holmes discussed the case. [1] Watson took notes. [2]"
    assert answer.note is None


@pytest.mark.asyncio
async def test_without_a_checker_the_answer_is_exactly_what_it_was() -> None:
    plain = await _text(GOOD1, GOOD2)
    answer = await synthesize("q", EV, _generate(
        Synthesis(claims=[GOOD1, GOOD2], can_answer=True)), mode="graph")
    assert answer.text == plain and answer.note is None


@pytest.mark.asyncio
async def test_a_misaligned_check_answer_keeps_every_claim_and_says_so() -> None:
    rotated = Faithfulness(judgements=[
        Judgement(claim=2, verdict=NOT_SUPPORTED, reason="r"),
        Judgement(claim=1, verdict=SUPPORTED, reason="r")])
    answer = await _checked(GOOD1, GOOD2, verdicts=rotated)
    assert answer.text == "Holmes discussed the case. [1] Watson took notes. [2]"
    assert answer.note == CHECK_SKIPPED_NOTE


@pytest.mark.asyncio
async def test_a_short_check_answer_keeps_every_claim_and_says_so() -> None:
    answer = await _checked(GOOD1, GOOD2, verdicts=_judged(NOT_SUPPORTED))
    assert answer.text == "Holmes discussed the case. [1] Watson took notes. [2]"
    assert answer.note == CHECK_SKIPPED_NOTE


@pytest.mark.asyncio
async def test_an_extra_check_entry_keeps_every_claim_and_says_so() -> None:
    answer = await _checked(GOOD1, verdicts=_judged(NOT_SUPPORTED, NOT_SUPPORTED))
    assert answer.text == "Holmes discussed the case. [1]"
    assert answer.note == CHECK_SKIPPED_NOTE


@pytest.mark.asyncio
async def test_a_verdict_that_is_neither_word_keeps_every_claim_and_says_so() -> None:
    answer = await _checked(GOOD1, GOOD2,
                            verdicts=_judged("maybe", NOT_SUPPORTED))
    assert answer.text == "Holmes discussed the case. [1] Watson took notes. [2]"
    assert answer.note == CHECK_SKIPPED_NOTE


@pytest.mark.asyncio
async def test_a_checker_that_raises_leaves_the_answer_unchanged() -> None:
    answer = await _checked(GOOD1, GOOD2, error=RuntimeError("no quota"))
    assert answer.text == "Holmes discussed the case. [1] Watson took notes. [2]"
    assert answer.note == CHECK_SKIPPED_NOTE


@pytest.mark.asyncio
async def test_the_skipped_note_joins_the_partial_note_rather_than_replacing_it() -> None:
    answer = await _checked(GOOD1, verdicts=None, can_answer=False,
                            error=RuntimeError("boom"))
    assert PARTIAL_NOTE in answer.note and CHECK_SKIPPED_NOTE in answer.note


@pytest.mark.asyncio
async def test_the_check_sees_the_title_chapter_quote_and_passage_text() -> None:
    prompts: list[str] = []
    await _checked(GOOD1, GOOD2, verdicts=_judged(SUPPORTED, SUPPORTED), prompts=prompts)
    prompt, = prompts
    assert GOOD1.text in prompt and GOOD2.text in prompt
    assert GOOD1.quote in prompt and GOOD2.quote in prompt
    assert P1.text in prompt and P2.text in prompt
    assert "A Study in Scarlet" in prompt
    assert "chapter 1" in prompt and "chapter 2" in prompt
    assert "[1]" in prompt and "[2]" in prompt


@pytest.mark.asyncio
async def test_the_check_prompt_names_both_verdicts_and_the_two_rules() -> None:
    prompts: list[str] = []
    await _checked(GOOD1, verdicts=_judged(SUPPORTED), prompts=prompts)
    prompt, = prompts
    assert SUPPORTED in prompt and NOT_SUPPORTED in prompt
    assert "book" in prompt and "place" in prompt
    assert "cause" in prompt and "motive" in prompt and "consequence" in prompt
    assert "12 words" in prompt
    assert "numbered 1 to 1" in prompt


@pytest.mark.asyncio
async def test_a_passage_cited_twice_is_shown_to_the_check_once() -> None:
    twin = Claim(text="Holmes talked for a long time.", passage=1,
                 quote="spoke of the case at length")
    prompts: list[str] = []
    await _checked(GOOD1, twin, verdicts=_judged(SUPPORTED, SUPPORTED), prompts=prompts)
    assert prompts[0].count(P1.text) == 1
    assert P2.text not in prompts[0]


@pytest.mark.asyncio
async def test_the_check_sees_only_the_claims_the_near_duplicate_collapse_kept() -> None:
    prompts: list[str] = []
    answer = await synthesize(
        "q", HABIT_EV,
        _generate(Synthesis(claims=[WATCHED, WATCHED_AGAIN], can_answer=True)),
        mode="graph", check=_checker(_judged(SUPPORTED), prompts))
    assert WATCHED_AGAIN.text not in prompts[0]
    assert answer.text == f"{WATCHED.text} [1]"


@pytest.mark.asyncio
async def test_dropping_every_claim_yields_the_refusal() -> None:
    answer = await _checked(GOOD1, GOOD2, verdicts=_judged(NOT_SUPPORTED, NOT_SUPPORTED))
    assert answer.text == REFUSAL and answer.note is None


@pytest.mark.asyncio
async def test_no_kept_claim_never_asks_the_checker() -> None:
    prompts: list[str] = []
    bad = Claim(text="Nope.", passage=1, quote="not in the passage at all")
    answer = await _checked(bad, verdicts=_judged(SUPPORTED), prompts=prompts)
    assert answer.text == REFUSAL and prompts == []


def _parts_checked(verdicts: Faithfulness | None, prompts: list[str],
                   results: list[Synthesis], subs=SUBS, evidence=PARTS_EV,
                   error: BaseException | None = None, note: str | None = None):
    return synthesize_parts(
        "q", subs, evidence, _per_part(results, []), mode="graph", note=note,
        check=_checker(verdicts, prompts, error))


@pytest.mark.asyncio
async def test_several_sub_questions_share_one_batched_check_call() -> None:
    prompts: list[str] = []
    answer = await _parts_checked(
        _judged(SUPPORTED, SUPPORTED), prompts,
        [Synthesis(claims=[WHY_D], can_answer=True),
         Synthesis(claims=[WHY_S], can_answer=True)])
    assert len(prompts) == 1
    assert WHY_D.text in prompts[0] and WHY_S.text in prompts[0]
    assert answer.text == ("Drebber forced Lucy to marry him. [2] "
                           "Stangerson shot her father. [3]")


@pytest.mark.asyncio
async def test_dropping_every_claim_of_one_sub_question_names_it_not_found() -> None:
    prompts: list[str] = []
    answer = await _parts_checked(
        _judged(SUPPORTED, NOT_SUPPORTED), prompts,
        [Synthesis(claims=[WHY_D], can_answer=True),
         Synthesis(claims=[WHY_S], can_answer=True)])
    assert answer.text == "Drebber forced Lucy to marry him. [2]"
    assert PARTIAL_NOTE in answer.note
    assert "not found: Why kill Stangerson?" in answer.note
    assert "Why kill Drebber?" not in answer.note


@pytest.mark.asyncio
async def test_dropping_every_claim_of_every_part_is_a_refusal() -> None:
    prompts: list[str] = []
    answer = await _parts_checked(
        _judged(NOT_SUPPORTED, NOT_SUPPORTED), prompts,
        [Synthesis(claims=[WHY_D], can_answer=True),
         Synthesis(claims=[WHY_S], can_answer=True)])
    assert answer.text == REFUSAL and answer.note is None


@pytest.mark.asyncio
async def test_a_failed_check_over_parts_keeps_every_claim_and_says_so() -> None:
    prompts: list[str] = []
    answer = await _parts_checked(
        None, prompts,
        [Synthesis(claims=[WHY_D], can_answer=True),
         Synthesis(claims=[WHY_S], can_answer=True)],
        error=RuntimeError("no quota"))
    assert answer.text == ("Drebber forced Lucy to marry him. [2] "
                           "Stangerson shot her father. [3]")
    assert answer.note == CHECK_SKIPPED_NOTE


@pytest.mark.asyncio
async def test_a_part_losing_only_a_near_duplicate_is_still_found() -> None:
    prompts: list[str] = []
    both = Evidence(edges=(), passages=(HABIT, HABIT2), truncated=False,
                    shares=((1,), (1, 2)))
    answer = await _parts_checked(
        _judged(SUPPORTED), prompts,
        [Synthesis(claims=[WATCHED], can_answer=True),
         Synthesis(claims=[WATCHED_AGAIN], can_answer=True)], evidence=both)
    assert answer.text == f"{WATCHED.text} [1]"
    assert answer.note is None


def test_validated_verdicts_refuses_an_answer_that_does_not_line_up() -> None:
    assert validated_verdicts(_judged(SUPPORTED, SUPPORTED), 2) == [
        (True, "reason"), (True, "reason")]
    assert validated_verdicts(_judged(SUPPORTED), 2) is None
    assert validated_verdicts(_judged(SUPPORTED, SUPPORTED, start=2), 2) is None
    assert validated_verdicts(_judged(SUPPORTED, "unsupported"), 2) is None


def test_validated_verdicts_reads_a_tidier_spelling_of_each_word() -> None:
    assert validated_verdicts(_judged("Supported", "Not Supported"), 2) == [
        (True, "reason"), (False, "reason")]


@pytest.mark.asyncio
async def test_check_claims_reports_what_it_dropped_and_why() -> None:
    kept, dropped, ran = await check_claims(
        [GOOD1, GOOD2], EV.passages,
        _checker(_judged(SUPPORTED, NOT_SUPPORTED, reason="names another book")),
        book_titles=TITLES)
    assert kept == [GOOD1]
    assert dropped == [(GOOD2, "names another book")]
    assert ran is True


@pytest.mark.asyncio
async def test_check_claims_keeps_everything_when_it_could_not_run() -> None:
    kept, dropped, ran = await check_claims(
        [GOOD1, GOOD2], EV.passages, _checker(error=RuntimeError("boom")))
    assert kept == [GOOD1, GOOD2] and dropped == [] and ran is False


def test_claim_is_importable_from_both_modules():
    from lore.query.models import Claim as FromModels
    from lore.query.synthesize import Claim as FromSynthesize

    assert FromModels is FromSynthesize


def test_the_prompt_leaks_nothing_from_the_eval_set() -> None:
    """No distinctive 3-word phrase of a dev or held-out question or verified
    answer may appear in the synthesis prompt. An early round-1 draft used
    "221B Baker Street" (eval Q8's answer) as an example, which would have
    inflated exactly the question being scored.

    A phrase counts when at least two of its three words are not common
    function words, so answer terms ("221b baker street") are caught and
    shared grammar ("the passage gives") is not. It does NOT catch a
    paraphrase built from common words -- leakage review of every prompt
    change stays a manual step (evals/answer_quality/GRADING.md).
    """
    import json
    import re
    from pathlib import Path

    from lore.query import synthesize as synthesize_module
    from lore.query import write as write_module

    common = set(
        "a an the and or but nor of to in on at by for from with as into is"
        " are was were be been being has have had do does did this that these"
        " those it its he him his she her they them their i me my we us our"
        " you your not no so than then there here which who whom whose what"
        " when where why how each every one other own all any some only also"
        " just more most such very can could would should may might will"
        " shall must said say says".split())

    def grams(text: str) -> set[str]:
        words = re.findall(r"[a-z0-9']+", text.lower())
        return {" ".join(words[i:i + 3]) for i in range(len(words) - 2)
                if sum(w not in common for w in words[i:i + 3]) >= 2}

    evals = Path(__file__).parents[1] / "evals/answer_quality"
    sets = [evals / "questions.json", evals / "heldout.json", evals / "arc.json",
            evals / "interpretive.json", evals / "deep.json"]
    prompt = grams("\n".join((
        synthesize_module._PROMPT, synthesize_module._FACTS_RULE,
        synthesize_module._FULL_FACTS_RULE, synthesize_module._PART_CONTEXT,
        synthesize_module._FULL_PART_CONTEXT, synthesize_module._NO_RESTATE_RULE,
        synthesize_module._ATTRIBUTE_SPEECH_RULE, synthesize_module._STRICT_LINKS_RULE, synthesize_module._ROLE_COVERAGE_RULE,
        synthesize_module._CHANGE_RULE, synthesize_module._ACTIONS_RULE,
        synthesize_module._TIMELINE_BLOCK,
        synthesize_module._READINGS_BLOCK, write_module._READINGS_RULE,
        synthesize_module._JUDGEMENT_RULE, synthesize_module._JUDGEMENTS_BLOCK,
        write_module._JUDGEMENT_RULE,
        synthesize_module._STRICT_LINKS_CHECK, __import__('lore.query.decompose', fromlist=['x'])._THEMATIC_RULE,
        write_module.WRITER_PROMPT, write_module.STORY_WRITER_PROMPT,
        synthesize_module._STORY_SENTENCES_BLOCK, synthesize_module._GAPS_BLOCK,
        __import__('lore.beats.build', fromlist=['x']).BEATS_PROMPT)))
    for q in (q for path in sets for q in json.loads(path.read_text())):
        leaked = (grams(q["question"]) | grams(q["verified"])) & prompt
        assert not leaked, f"{q['id']} leaks into the prompt: {sorted(leaked)}"


def test_full_answers_asks_for_circumstances_and_a_full_part(monkeypatch) -> None:
    from lore.query import synthesize as synthesize_module
    from lore.query.synthesize import _build_prompt
    evidence = Evidence(edges=(), passages=(
        Passage(n=1, chunk_id="b1:1:0", book_id="b1", chapter_seq=1, text="Some text."),),
        truncated=False)
    monkeypatch.setattr(synthesize_module, "FULL_ANSWERS", False)
    plain = _build_prompt("why?", evidence, part_of="who and why?")
    assert "List every distinct fact" in plain and "Answer only this part." in plain
    monkeypatch.setattr(synthesize_module, "FULL_ANSWERS", True)
    full = _build_prompt("why?", evidence, part_of="who and why?")
    assert "circumstances, reasons, methods and consequences" in full
    assert "Answer this part fully" in full and "Answer only this part." not in full
    assert "List every distinct fact" not in full


def test_no_restate_adds_its_rule_only_when_on(monkeypatch) -> None:
    from lore.query import synthesize as synthesize_module
    from lore.query.synthesize import _build_prompt
    evidence = Evidence(edges=(), passages=(
        Passage(n=1, chunk_id="b1:1:0", book_id="b1", chapter_seq=1, text="Some text."),),
        truncated=False)
    monkeypatch.setattr(synthesize_module, "NO_RESTATE", False)
    assert "must add a fact" not in _build_prompt("why?", evidence)
    monkeypatch.setattr(synthesize_module, "NO_RESTATE", True)
    assert "must add a fact the earlier claims do not already state" in _build_prompt("why?", evidence)


def test_full_answers_and_no_restate_are_the_default() -> None:
    # Adopted after rounds 4-5 (GRADING.md, "Decisions").
    from lore.query import synthesize as synthesize_module
    assert synthesize_module.FULL_ANSWERS is True
    assert synthesize_module.NO_RESTATE is True


# --- prose answers: the combined check -----------------------------------------

from lore.query.synthesize import (  # noqa: E402
    CheckOutcome, Faithfulness, Judgement, check_answer, faithfulness_prompt,
    validated_sentence_verdicts,
)
from lore.query.write import Sentence  # noqa: E402

_PS = (Passage(n=1, chunk_id="b1:1:0", book_id="b1", chapter_seq=1, text="Holmes played the violin."),
       Passage(n=2, chunk_id="b1:2:0", book_id="b1", chapter_seq=2, text="Watson kept a bull pup."))
_CS = [Claim(text="Holmes played the violin.", passage=1, quote="Holmes played the violin"),
       Claim(text="Watson kept a bull pup.", passage=2, quote="Watson kept a bull pup")]


def test_without_sentences_the_check_prompt_is_unchanged():
    assert "Sentences:" not in faithfulness_prompt(_CS, _PS)


def test_with_sentences_the_prompt_numbers_them_and_names_their_facts():
    p = faithfulness_prompt(_CS, _PS, sentences=[Sentence(text="Holmes played, and Watson kept a pup.", claims=[1, 2])])
    assert "Sentences:" in p and "1. sentence: Holmes played, and Watson kept a pup." in p
    assert "uses claims 1, 2" in p
    assert "sentence_judgements" in p


def _j(n, v):
    return Judgement(claim=n, verdict=v, reason="r")


async def test_check_answer_applies_both_verdict_lists():
    async def check(prompt, schema):
        return Faithfulness(judgements=[_j(1, "supported"), _j(2, "not_supported")],
                            sentence_judgements=[_j(1, "supported"), _j(2, "not_supported")])
    out = await check_answer(_CS, _PS, check, sentences=[Sentence(text="a", claims=[1]), Sentence(text="b", claims=[1])])
    assert out.ran and out.claim_ok == [True, False] and out.sentence_ok == [True, False]
    assert out.kept == [_CS[0]] and out.dropped[0][0] == _CS[1]


async def test_garbled_sentence_verdicts_keep_the_claim_verdicts():
    async def check(prompt, schema):
        return Faithfulness(judgements=[_j(1, "supported"), _j(2, "not_supported")],
                            sentence_judgements=[_j(2, "supported")])   # misnumbered
    out = await check_answer(_CS, _PS, check, sentences=[Sentence(text="a", claims=[1])])
    assert out.ran and out.claim_ok == [True, False] and out.sentence_ok is None


async def test_garbled_claim_verdicts_keep_every_claim_and_drop_the_prose():
    async def check(prompt, schema):
        return Faithfulness(judgements=[_j(2, "supported")], sentence_judgements=[_j(1, "supported")])
    out = await check_answer(_CS, _PS, check, sentences=[Sentence(text="a", claims=[1])])
    assert not out.ran and out.kept == _CS and out.claim_ok == [True, True] and out.sentence_ok is None


async def test_a_raising_check_keeps_everything():
    async def check(prompt, schema):
        raise RuntimeError("503")
    out = await check_answer(_CS, _PS, check, sentences=[Sentence(text="a", claims=[1])])
    assert not out.ran and out.kept == _CS and out.sentence_ok is None


def test_validated_sentence_verdicts():
    assert validated_sentence_verdicts(Faithfulness(judgements=[], sentence_judgements=[_j(1, "supported"), _j(2, "Not Supported")]), 2) == [True, False]
    assert validated_sentence_verdicts(Faithfulness(judgements=[], sentence_judgements=[_j(1, "maybe")]), 1) is None
    assert validated_sentence_verdicts(Faithfulness(judgements=[]), 1) is None


# --- prose answers: synthesis ----------------------------------------------------

from lore.query import write as write_module  # noqa: E402
from lore.query.synthesize import PROSE_FALLBACK_NOTE  # noqa: E402
from lore.query.write import Prose  # noqa: E402

_EV = Evidence(edges=(), passages=_PS, truncated=False)
_TWO = Synthesis(claims=_CS, can_answer=True)


def _gen(reply):
    calls = []
    async def generate(prompt, schema):
        calls.append(prompt)
        return reply
    return generate, calls


def _check(claim_v, sent_v):
    async def check(prompt, schema):
        return Faithfulness(judgements=[_j(i + 1, v) for i, v in enumerate(claim_v)],
                            sentence_judgements=[_j(i + 1, v) for i, v in enumerate(sent_v)])
    return check


async def test_prose_becomes_the_answer():
    gen, _ = _gen(_TWO)
    write, wcalls = _gen(Prose(sentences=[Sentence(text="Holmes played the violin while Watson kept a bull pup.", claims=[1, 2])]))
    a = await synthesize("Habits?", _EV, gen, mode="graph", check=_check(["supported"] * 2, ["supported"]), write=write)
    assert a.answer_style == "prose" and a.prose[0].passages == (1, 2)
    assert a.text == "Holmes played the violin while Watson kept a bull pup. [1][2]"
    assert len(a.claims) == 2 and len(wcalls) == 1


async def test_failing_prose_falls_back_to_claims_with_a_note():
    gen, _ = _gen(_TWO)
    write, _ = _gen(Prose(sentences=[Sentence(text="x", claims=[1]), Sentence(text="y", claims=[2])]))
    a = await synthesize("q", _EV, gen, mode="graph", check=_check(["supported"] * 2, ["not_supported"] * 2), write=write)
    assert a.answer_style == "claims" and a.prose == () and PROSE_FALLBACK_NOTE in a.note


async def test_no_writer_call_for_a_single_claim():
    gen, _ = _gen(Synthesis(claims=[_CS[0]], can_answer=True))
    write, wcalls = _gen(Prose(sentences=[]))
    a = await synthesize("q", _EV, gen, mode="graph", check=_check(["supported"], []), write=write)
    assert wcalls == [] and a.answer_style == "claims"


async def test_no_writer_call_without_a_check():
    gen, _ = _gen(_TWO)
    write, wcalls = _gen(Prose(sentences=[]))
    a = await synthesize("q", _EV, gen, mode="graph", check=None, write=write)
    assert wcalls == [] and a.answer_style == "claims"


async def test_prose_off_is_todays_behaviour(monkeypatch):
    monkeypatch.setattr(write_module, "PROSE_ANSWERS", False)
    gen, calls = _gen(_TWO)
    write, wcalls = _gen(Prose(sentences=[Sentence(text="x", claims=[1])]))
    check_prompts = []
    async def check(prompt, schema):
        check_prompts.append(prompt)
        return Faithfulness(judgements=[_j(1, "supported"), _j(2, "supported")])
    a = await synthesize("q", _EV, gen, mode="graph", check=check, write=write)
    assert wcalls == [] and a.answer_style == "claims"
    assert "Sentences:" not in check_prompts[0]


async def test_a_writer_failure_keeps_the_claim_check():
    gen, _ = _gen(_TWO)
    async def write(prompt, schema):
        raise RuntimeError("quota")
    a = await synthesize("q", _EV, gen, mode="graph", check=_check(["supported", "not_supported"], []), write=write)
    assert a.answer_style == "claims" and len(a.claims) == 1 and PROSE_FALLBACK_NOTE in a.note


# --- prose answers: parts, check failure, remapping, refusal ---------------------

from lore.query.synthesize import CHECK_SKIPPED_NOTE  # noqa: E402


async def test_synthesize_parts_writes_one_prose_answer():
    prompts: list[str] = []
    gen = _per_part([Synthesis(claims=[WHY_D], can_answer=True),
                     Synthesis(claims=[WHY_S], can_answer=True)], prompts)
    write, wcalls = _gen(Prose(sentences=[Sentence(
        text="Drebber forced Lucy to marry, and Stangerson shot her father.", claims=[1, 2])]))
    a = await synthesize_parts("Why?", SUBS, PARTS_EV, gen, mode="graph",
                               check=_check(["supported"] * 2, ["supported"]), write=write)
    assert a.answer_style == "prose" and len(wcalls) == 1 and len(prompts) == 2


async def test_check_that_does_not_run_after_prose_falls_back_with_both_notes():
    gen, _ = _gen(_TWO)
    write, _ = _gen(Prose(sentences=[Sentence(text="x", claims=[1, 2])]))
    async def check(prompt, schema):
        raise RuntimeError("quota")
    a = await synthesize("q", _EV, gen, mode="graph", check=check, write=write)
    assert a.answer_style == "claims" and len(a.claims) == 2
    assert PROSE_FALLBACK_NOTE in a.note and CHECK_SKIPPED_NOTE in a.note


async def test_prose_indices_are_remapped_after_a_claim_is_dropped():
    ps = _PS + (Passage(n=3, chunk_id="b1:3:0", book_id="b1", chapter_seq=3,
                        text="Lestrade visited Baker Street."),)
    c3 = Claim(text="Lestrade visited.", passage=3, quote="Lestrade visited Baker Street")
    gen, _ = _gen(Synthesis(claims=[_CS[0], _CS[1], c3], can_answer=True))
    write, _ = _gen(Prose(sentences=[
        Sentence(text="Holmes played while Lestrade visited.", claims=[1, 3]),
        Sentence(text="Watson kept a pup.", claims=[2])]))
    ev = Evidence(edges=(), passages=ps, truncated=False)
    a = await synthesize("q", ev, gen, mode="graph",
                         check=_check(["supported", "not_supported", "supported"],
                                      ["supported", "supported"]), write=write)
    assert a.answer_style == "prose" and len(a.prose) == 1
    assert a.prose[0].claims == (1, 2) and a.prose[0].passages == (1, 3)
    assert [c.passage for c in a.claims] == [1, 3]


async def test_all_claims_dropped_with_the_writer_called_is_a_refusal():
    gen, _ = _gen(_TWO)
    write, wcalls = _gen(Prose(sentences=[Sentence(text="x", claims=[1, 2])]))
    a = await synthesize("q", _EV, gen, mode="graph",
                         check=_check(["not_supported"] * 2, ["supported"]), write=write)
    assert len(wcalls) == 1 and a.text == REFUSAL and a.claims == ()


async def test_unanswered_part_is_reported_and_rendered_by_code():
    gen, _ = _gen(_TWO)
    write, _ = _gen(Prose(sentences=[Sentence(text="Holmes played the violin while Watson kept a bull pup.", claims=[1, 2])],
                          unanswered=[2, 9]))
    a = await synthesize("Habits and why?", _EV, gen, mode="graph", check=_check(["supported"] * 2, ["supported"]),
                         write=write)
    # single-part question: part 1 is the question itself; 2 and 9 are out of range
    assert a.answer_style == "prose" and a.unanswered == ()
    write2, _ = _gen(Prose(sentences=[Sentence(text="Holmes played the violin while Watson kept a bull pup.", claims=[1, 2])],
                           unanswered=[1, 1]))
    b = await synthesize("Habits and why?", _EV, gen, mode="graph", check=_check(["supported"] * 2, ["supported"]),
                         write=write2)
    assert b.unanswered == ("Habits and why?",)
    assert b.text.endswith("The passages found don't answer: Habits and why?")


async def test_claims_fallback_leaves_unanswered_empty():
    gen, _ = _gen(_TWO)
    write, _ = _gen(Prose(sentences=[Sentence(text="x", claims=[1]), Sentence(text="y", claims=[2])], unanswered=[1]))
    a = await synthesize("Habits?", _EV, gen, mode="graph", check=_check(["supported"] * 2, ["unsupported", "unsupported"]),
                         write=write)
    assert a.answer_style == "claims" and a.unanswered == ()


async def test_one_sided_prose_over_parts_falls_back_to_claims():
    gen = _per_part([Synthesis(claims=[WHY_D], can_answer=True),
                     Synthesis(claims=[WHY_S], can_answer=True)], [])
    write, _ = _gen(Prose(sentences=[Sentence(text="Stangerson shot her father.", claims=[2])]))
    a = await synthesize_parts("Why?", SUBS, PARTS_EV, gen, mode="graph",
                               check=_check(["supported"] * 2, ["supported"]), write=write)
    assert a.answer_style == "claims" and len(a.claims) == 2
    assert PROSE_FALLBACK_NOTE in a.note


def test_attribute_speech_adds_its_rule_only_when_on(monkeypatch) -> None:
    from lore.query import synthesize as synthesize_module
    from lore.query.synthesize import _build_prompt
    evidence = Evidence(edges=(), passages=(
        Passage(n=1, chunk_id="b1:1:0", book_id="b1", chapter_seq=1, text="Some text."),),
        truncated=False)
    monkeypatch.setattr(synthesize_module, "ATTRIBUTE_SPEECH", False)
    assert "that character's account" not in _build_prompt("why?", evidence)
    monkeypatch.setattr(synthesize_module, "ATTRIBUTE_SPEECH", True)
    assert "that character's account" in _build_prompt("why?", evidence)


def test_strict_links_adds_its_rules_only_when_on(monkeypatch) -> None:
    from lore.query import synthesize as synthesize_module
    from lore.query.synthesize import _build_prompt, faithfulness_prompt
    passage = Passage(n=1, chunk_id="b1:1:0", book_id="b1", chapter_seq=1, text="Some text.")
    evidence = Evidence(edges=(), passages=(passage,), truncated=False)
    claim = Claim(text="A did B.", passage=1, quote="Some text.")
    monkeypatch.setattr(synthesize_module, "STRICT_LINKS", False)
    assert "Two facts from one passage" not in _build_prompt("why?", evidence)
    assert "Connecting words count" not in faithfulness_prompt([claim], [passage])
    monkeypatch.setattr(synthesize_module, "STRICT_LINKS", True)
    assert "Two facts from one passage stay two claims" in _build_prompt("why?", evidence)
    assert "even when both facts it joins are true" in faithfulness_prompt([claim], [passage])


def test_thematic_parts_adds_its_rule_only_when_on(monkeypatch) -> None:
    from lore.query import decompose
    monkeypatch.setattr(decompose, "THEMATIC_PARTS", False)
    off = decompose.decomposition_prompt("q?")
    assert "role or a theme" not in off and "{thematic}" not in off
    monkeypatch.setattr(decompose, "THEMATIC_PARTS", True)
    assert "every character it applies to" in decompose.decomposition_prompt("q?")


def test_role_coverage_adds_its_rule_only_when_on(monkeypatch) -> None:
    from lore.query import synthesize as synthesize_module
    from lore.query.synthesize import _build_prompt
    evidence = Evidence(edges=(), passages=(
        Passage(n=1, chunk_id="b1:1:0", book_id="b1", chapter_seq=1, text="Some text."),),
        truncated=False)
    monkeypatch.setattr(synthesize_module, "ROLE_COVERAGE", False)
    assert "every character the passages show" not in _build_prompt("who?", evidence)
    monkeypatch.setattr(synthesize_module, "ROLE_COVERAGE", True)
    assert "every character the passages show in that role" in _build_prompt("who?", evidence)


def test_prompt_rules_name_no_character_book_or_genre() -> None:
    # Rules must hold for any book a user uploads: no names or genre words
    # from the books the rules were tuned on.
    import re
    from lore.query import decompose, synthesize as s, write as w
    text = "\n".join((s._PROMPT, s._FACTS_RULE, s._FULL_FACTS_RULE, s._NO_RESTATE_RULE,
                      s._ATTRIBUTE_SPEECH_RULE, s._STRICT_LINKS_RULE, s._STRICT_LINKS_CHECK,
                      s._ROLE_COVERAGE_RULE, s._CHANGE_RULE, s._ACTIONS_RULE, s._TIMELINE_BLOCK, s._READINGS_BLOCK, w._READINGS_RULE,
                      s._JUDGEMENT_RULE, s._JUDGEMENTS_BLOCK, w._JUDGEMENT_RULE,
                      s._CHECK_PROMPT, s._SENTENCES_BLOCK,
                      w.WRITER_PROMPT, w.STORY_WRITER_PROMPT, s._STORY_SENTENCES_BLOCK, s._GAPS_BLOCK,
                      decompose._PROMPT, decompose._THEMATIC_RULE,
                      __import__('lore.beats.build', fromlist=['x']).BEATS_PROMPT))
    banned = r"\b(holmes|watson|sherlock|small|hope|drebber|stangerson|sholto|ferrier|tonga|" \
             r"morstan|lestrade|baker street|mormon|agra|crime|crimes|murder|villain|villains|" \
             r"detective|treasure|darcy|bennet|wizard|dragon|magic|kingdom|elf|elves|quest|sword)\b"
    assert not re.findall(banned, text.lower())
