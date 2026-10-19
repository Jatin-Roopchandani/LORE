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
