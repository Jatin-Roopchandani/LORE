"""The writer: prompt, citation check, assembly and fallback rules."""
from lore.query.models import Claim, Passage, ProseSentence
from lore.query.write import (
    Prose, Sentence, assemble, compose_text, verify_prose, write_answer, writer_prompt,
)

P = (Passage(n=3, chunk_id="b1:3:0", book_id="b1", chapter_seq=3, text="Hope loved Lucy Ferrier."),
     Passage(n=5, chunk_id="b1:5:0", book_id="b1", chapter_seq=5, text="Lucy was forced to marry Drebber."))
C = [Claim(text="Hope loved Lucy.", passage=3, quote="Hope loved Lucy Ferrier"),
     Claim(text="Lucy was forced to marry Drebber.", passage=5, quote="Lucy was forced to marry Drebber")]


def S(text, claims, new_paragraph=False):
    return Sentence(text=text, claims=claims, new_paragraph=new_paragraph)


def test_writer_prompt_numbers_the_facts_and_shows_book_and_chapter():
    prompt = writer_prompt("Why did Hope hate Drebber?", ["Why did Hope hate Drebber?"], C, P, {"b1": "A Study in Scarlet"})
    assert "1. Hope loved Lucy." in prompt and "2. Lucy was forced to marry Drebber." in prompt
    assert "A Study in Scarlet, chapter 3" in prompt
    assert "Why did Hope hate Drebber?" in prompt


def test_verify_prose_drops_uncited_and_out_of_range_sentences():
    prose = Prose(sentences=[S("Good.", [1]), S("No cite.", []), S("Zero.", [0]), S("Too high.", [3]), S("Both.", [1, 2])])
    kept = verify_prose(prose, n_claims=2)
    assert [s.text for s in kept] == ["Good.", "Both."]


def test_assemble_maps_claims_to_passages_and_survivor_indices():
    out = assemble([S("A.", [1]), S("B.", [1, 2], True)], written=2,
                   claim_ok=[True, True], sentence_ok=[True, True], passage_of=[3, 5])
    assert out == (ProseSentence("A.", (1,), (3,), False), ProseSentence("B.", (1, 2), (3, 5), True))


def test_a_dropped_claim_takes_its_sentences_and_citations_renumber():
    # claim 1 fails the check; the survivor (old claim 2) becomes claim 1.
    out = assemble([S("A.", [1]), S("B.", [2]), S("C.", [2])], written=3,
                   claim_ok=[False, True], sentence_ok=[True, True, True], passage_of=[3, 5])
    assert out == (ProseSentence("B.", (1,), (5,)), ProseSentence("C.", (1,), (5,)))


def test_one_unsupported_sentence_is_removed():
    out = assemble([S("A.", [1]), S("B.", [2]), S("C.", [1, 2])], written=3,
                   claim_ok=[True, True], sentence_ok=[True, False, True], passage_of=[3, 5])
    assert [s.text for s in out] == ["A.", "C."]


def test_two_unsupported_sentences_fall_back():
    assert assemble([S("A.", [1]), S("B.", [2]), S("C.", [1])], written=3,
                    claim_ok=[True, True], sentence_ok=[False, False, True], passage_of=[3, 5]) is None


def test_fewer_than_half_surviving_falls_back():
    # 5 written, 3 lost to verify_prose earlier, 1 removed now -> 1 of 5 survives.
    assert assemble([S("A.", [1]), S("B.", [2])], written=5,
                    claim_ok=[True, True], sentence_ok=[True, False], passage_of=[3, 5]) is None


def test_misaligned_verdicts_fall_back():
    # zip() would silently drop the second sentence; the guard refuses instead.
    assert assemble([S("A.", [1]), S("B.", [2])], written=2,
                    claim_ok=[True, True], sentence_ok=[True], passage_of=[3, 5]) is None
    assert assemble([S("A.", [1])], written=1,
                    claim_ok=[True, True], sentence_ok=[True], passage_of=[3]) is None


def test_nothing_surviving_falls_back():
    assert assemble([S("A.", [1])], written=1, claim_ok=[False, True], sentence_ok=[True], passage_of=[3, 5]) is None


def test_compose_text_cites_and_splits_paragraphs():
    text = compose_text((ProseSentence("A.", (1,), (3,)), ProseSentence("B.", (1, 2), (3, 5)),
                         ProseSentence("C.", (2,), (5,), True)))
    assert text == "A. [3] B. [3][5]\n\nC. [5]"


async def test_write_answer_returns_none_on_garbage_or_exception():
    async def raises(prompt, schema):
        raise RuntimeError("quota")
    async def garbage(prompt, schema):
        return "not a model"
    assert await write_answer("q", ["q"], C, P, raises) is None
    assert await write_answer("q", ["q"], C, P, garbage) is None


async def test_write_answer_returns_the_prose():
    async def ok(prompt, schema):
        assert schema is Prose
        return Prose(sentences=[S("Hope loved Lucy.", [1])])
    out = await write_answer("q", ["q"], C, P, ok)
    assert out.sentences[0].claims == [1]


# ---- Fix A: citation residue ----

from lore.query.write import (  # noqa: E402
    UNANSWERED_LEAD, strip_residue, valid_unanswered,
)


def test_strip_residue_exact_example():
    t = "Jonathan Small lost his right leg, which is replaced by a wooden stump (The Sign of the Four, 1, 2)."
    assert strip_residue(t) == "Jonathan Small lost his right leg, which is replaced by a wooden stump."


def test_strip_residue_bare_numbers_and_brackets_and_chapter():
    assert strip_residue("He left (1, 2).") == "He left."
    assert strip_residue("He left [3] early.") == "He left early."
    assert strip_residue("He left [1, 2].") == "He left."
    assert strip_residue("He left (chapter 3).") == "He left."
    assert strip_residue("He left (A Study in Scarlet, ch. 4).") == "He left."


def test_strip_residue_keeps_normal_parentheticals():
    t = "He went with Watson (his old friend) to the house (in 1888)."
    assert strip_residue(t) == t


def test_verify_prose_cleans_and_drops_emptied_sentences():
    prose = Prose(sentences=[S("Hope loved Lucy (The Book, 1, 2).", [1]), S("(1, 2) [3]", [2])])
    kept = verify_prose(prose, n_claims=2)
    assert [s.text for s in kept] == ["Hope loved Lucy."]


def test_writer_prompt_forbids_inline_citations_and_lists_parts_always():
    one = writer_prompt("Q one?", ["Q one?"], C, P, None)
    assert "Parts:\n1. Q one?" in one
    two = writer_prompt("Q?", ["A?", "B?"], C, P, None)
    assert "1. A?" in two and "2. B?" in two
    assert "unanswered" in one and "inside the sentence text" in one


# ---- Fix B: unanswered ----

def test_valid_unanswered_keeps_in_range_deduplicated():
    p = Prose(sentences=[S("A.", [1])], unanswered=[2, 2, 0, 5, 1, -1])
    assert valid_unanswered(p, 2) == [2, 1]
    assert valid_unanswered(Prose(sentences=[S("A.", [1])]), 2) == []


def test_prose_unanswered_defaults_empty():
    assert Prose(sentences=[]).unanswered == []


def test_compose_text_appends_unanswered_paragraph():
    s = (ProseSentence("A.", (1,), (3,)),)
    assert compose_text(s, ("How did he lose it?",)) == f"A. [3]\n\n{UNANSWERED_LEAD} How did he lose it?"
    assert compose_text(s, ("X?", "Y?")).endswith(f"{UNANSWERED_LEAD} X?; Y?")
    assert compose_text(s) == "A. [3]"


# ---- writer v2: coverage, length, attribution ----

from lore.query.write import WRITER_PROMPT, coverage_gaps  # noqa: E402


def test_prompt_demands_coverage_length_and_attribution():
    text = " ".join(WRITER_PROMPT.split())
    assert "Use every fact" in text and "Every part that has facts must be answered" in text
    assert "in order" in text and "never skip a part's facts" in text
    assert "four short paragraphs" in text and "400 words" in text
    assert "own account" in text and "first person" in text


def _ps(*cl):
    return tuple(ProseSentence(text="x", claims=c, passages=(1,)) for c in cl)


def test_coverage_gaps_finds_a_part_whose_claims_are_all_uncited():
    # survivors 1,2 belong to part 1; 3 to part 2; prose cites only 3
    assert coverage_gaps(_ps((3,)), [[1, 2], [3]]) == [0]


def test_coverage_gaps_none_when_each_part_is_cited_once():
    assert coverage_gaps(_ps((1,), (3,)), [[1, 2], [3]]) == []


def test_coverage_gaps_ignores_parts_without_surviving_claims():
    assert coverage_gaps(_ps((3,)), [[], [3]]) == []


# --- one sentence per item (2026-10-28: the writer packed paragraphs) -----
from lore.query.write import split_items  # noqa: E402


def test_a_paragraph_item_is_split_into_sentences_citing_the_same_claims() -> None:
    prose = Prose(sentences=[Sentence(
        text="Mr. Hope wanted revenge. He saw himself as a judge! Was he right?",
        claims=[1, 2], new_paragraph=True)])
    items = split_items(prose)
    assert [s.text for s in items] == [
        "Mr. Hope wanted revenge.", "He saw himself as a judge!", "Was he right?"]
    assert all(s.claims == [1, 2] for s in items)
    assert [s.new_paragraph for s in items] == [True, False, False]


def test_new_paragraph_typed_into_the_text_becomes_the_flag() -> None:
    items = split_items(Prose(sentences=[
        Sentence(text="First.", claims=[1]),
        Sentence(text="new_paragraph: In the second book, Small sought the treasure.", claims=[2])]))
    assert items[1].text == "In the second book, Small sought the treasure."
    assert items[1].new_paragraph is True


def test_abbreviations_do_not_split_a_sentence() -> None:
    items = split_items(Prose(sentences=[Sentence(
        text="Dr. Watson met Mrs. Hudson at No. 221B Baker St. in London.", claims=[1])]))
    assert len(items) == 1


def test_verify_prose_judges_split_sentences() -> None:
    kept = verify_prose(Prose(sentences=[Sentence(text="One. Two.", claims=[1])]), 1)
    assert [s.text for s in kept] == ["One.", "Two."]


def test_a_long_answer_tolerates_one_failure_in_five() -> None:
    sents = [Sentence(text=f"S{i}.", claims=[1]) for i in range(10)]
    ok = [True] * 8 + [False, False]
    built = assemble(sents, 10, [True], ok, [3])
    assert built is not None and len(built) == 8
    assert assemble(sents, 10, [True], [True] * 7 + [False] * 3, [3]) is None


from lore.query.write import paragraphs_by_book  # noqa: E402


def _bs(text, passages, flag=False):
    return ProseSentence(text=text, claims=(1,), passages=passages, new_paragraph=flag)


def test_a_multi_book_answer_breaks_paragraphs_where_the_book_changes() -> None:
    prose = (_bs("A1.", (1,)), _bs("A2.", (2,)), _bs("A3.", (1,), flag=True),
             _bs("B1.", (5,)), _bs("B2.", (6,)))
    out = paragraphs_by_book(prose, {1: "a", 2: "a", 5: "b", 6: "b"})
    assert [s.new_paragraph for s in out] == [False, False, False, True, False]


def test_a_one_book_answer_keeps_the_writers_paragraphs() -> None:
    prose = (_bs("A1.", (1,)), _bs("A2.", (2,), flag=True))
    assert paragraphs_by_book(prose, {1: "a", 2: "a"}) == prose


def test_split_sentences_keep_only_the_claims_they_use() -> None:
    claims = ["Hope wanted revenge for John Ferrier.", "Hope saw himself as an officer of justice.",
              "Drebber and Hope were rivals for Lucy."]
    prose = Prose(sentences=[Sentence(
        text="Hope sought revenge for John Ferrier. He saw himself as an officer of justice. "
             "Drebber was his rival for Lucy.", claims=[1, 2, 3])])
    items = split_items(prose, claims)
    assert [s.claims for s in items] == [[1], [2], [3]]


def test_a_single_sentence_item_keeps_all_its_claims() -> None:
    items = split_items(Prose(sentences=[Sentence(text="Hope and Drebber.", claims=[1, 2])]),
                        ["Unrelated words entirely.", "Nothing shared here."])
    assert items[0].claims == [1, 2]


def test_a_split_sentence_sharing_no_words_keeps_the_items_claims() -> None:
    items = split_items(Prose(sentences=[Sentence(text="Alpha beta. Gamma delta.", claims=[1, 2])]),
                        ["Alpha beta words.", "Epsilon zeta."])
    assert [s.claims for s in items] == [[1], [1, 2]]
