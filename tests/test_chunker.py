from lore.ingest.chunker import chunk_book
from lore.ingest.models import ParsedBook, ParsedChapter

BOOK_ID = "abcdef0123456789"


def _book(chapters: list[tuple[str, str]]) -> ParsedBook:
    return ParsedBook(
        title="T",
        author="A",
        chapters=[
            ParsedChapter(seq=i + 1, title=t, text=x)
            for i, (t, x) in enumerate(chapters)
        ],
    )


def _long_text(n_sentences: int) -> str:
    return " ".join(
        f"This is sentence number {i} padding the scene with enough words to matter."
        for i in range(n_sentences)
    )


def test_deterministic() -> None:
    book = _book([("One", _long_text(60)), ("Two", _long_text(40))])
    first = chunk_book(book, BOOK_ID)
    second = chunk_book(book, BOOK_ID)
    assert first == second
    assert len(first) > 2


def test_ids_and_chapter_boundaries() -> None:
    book = _book([("One", _long_text(60)), ("Two", _long_text(60))])
    chunks = chunk_book(book, BOOK_ID)
    for chunk in chunks:
        assert chunk.id == f"{BOOK_ID}:{chunk.chapter_seq}:{chunk.seq}"
        assert chunk.book_id == BOOK_ID
    seqs_ch1 = [c.seq for c in chunks if c.chapter_seq == 1]
    seqs_ch2 = [c.seq for c in chunks if c.chapter_seq == 2]
    assert seqs_ch1 == list(range(len(seqs_ch1)))  # per-chapter numbering
    assert seqs_ch2 == list(range(len(seqs_ch2)))  # restarts at 0


def test_budget_respected() -> None:
    book = _book([("One", _long_text(120))])
    for chunk in chunk_book(book, BOOK_ID):
        # budget + slack for the sentence that tipped it over
        assert len(chunk.text) < 1600 + 200


def test_overlap_between_consecutive_chunks() -> None:
    book = _book([("One", _long_text(120))])
    chunks = chunk_book(book, BOOK_ID)
    assert len(chunks) >= 2
    # the first two sentences of chunk n+1 are the last two of chunk n
    prev, nxt = chunks[0].text, chunks[1].text
    assert prev[-80:] in nxt  # the overlap makes chunk n's ending appear in n+1


def test_scene_markers_split_scenes() -> None:
    text = _long_text(10) + "\n\n***\n\n" + _long_text(10)
    book = _book([("One", text)])
    chunks = chunk_book(book, BOOK_ID)
    scene_seqs = {c.scene_seq for c in chunks}
    assert scene_seqs == {0, 1}
    # no chunk mixes text from both scenes
    for chunk in chunks:
        assert "***" not in chunk.text


def test_blank_line_runs_split_scenes() -> None:
    text = _long_text(10) + "\n\n\n" + _long_text(10)
    book = _book([("One", text)])
    chunks = chunk_book(book, BOOK_ID)
    assert {c.scene_seq for c in chunks} == {0, 1}


def test_empty_chapter_produces_no_chunks() -> None:
    book = _book([("One", "   \n\n  "), ("Two", _long_text(5))])
    chunks = chunk_book(book, BOOK_ID)
    assert all(c.chapter_seq == 2 for c in chunks)


def test_no_sentence_is_ever_lost() -> None:
    # Exact repro of the fixed bug: with a tiny budget, the first sentence
    # fills a pack alone, the reseed carries only 1 sentence, and the final
    # pack (carried + 1 new = 2 <= overlap_sentences) used to be dropped —
    # silently deleting the second sentence.
    first = "A" * 60 + "."
    second = "B" * 60 + "."
    book = _book([("One", first + " " + second)])
    chunks = chunk_book(book, BOOK_ID, budget_chars=61)
    combined = " ".join(c.text for c in chunks)
    assert "A" * 60 in combined
    assert "B" * 60 in combined


def _oversized_dialogue_sentence(turns: int = 80) -> str:
    # pysbd can return an entire run of quoted dialogue turns as a single
    # "sentence" (observed up to 6267 chars on real prose) because it does
    # not split between a closing curly quote and the next quoted turn.
    # This reproduces exactly that: repeated curly-quoted turns with no
    # sentence-ending punctuation pysbd will split on between them.
    turn = "“Will you give me leave to defer your raptures till I write again, Darcy?” "
    return (turn * turns).strip()


def test_oversized_pysbd_sentence_is_hard_split_and_bounded() -> None:
    text = _oversized_dialogue_sentence()
    assert len(text) > 5000  # confirms the input really is one giant blob
    book = _book([("One", text)])
    chunks = chunk_book(book, BOOK_ID)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk.text) <= 1600 + 200  # budget + slack, same tolerance
        # as test_budget_respected
    combined = " ".join(c.text for c in chunks)
    # No text lost: overlap can only duplicate turns, never drop them, so
    # every turn's marker phrase must appear at least as many times as it
    # did in the source.
    assert combined.count("Darcy?") >= text.count("Darcy?")


def test_oversized_pysbd_sentence_hard_split_is_deterministic() -> None:
    text = _oversized_dialogue_sentence()
    book = _book([("One", text)])
    first = chunk_book(book, BOOK_ID)
    second = chunk_book(book, BOOK_ID)
    assert first == second


def test_every_chunk_respects_budget_even_with_large_sentences() -> None:
    # Two near-budget real sentences used to compound through overlap.
    big_a = "A" * 900 + "."
    big_b = "B" * 900 + "."
    book = _book([("One", f"{big_a} {big_b}")])
    for chunk in chunk_book(book, BOOK_ID):
        assert len(chunk.text) <= 1600


def test_long_sentences_still_participate_in_overlap() -> None:
    # A 900-char sentence followed by short ones must not be isolated:
    # the short sentences that follow it share a chunk with it.
    big = "A" * 900 + "."
    tail = " ".join(f"Short sentence number {i}." for i in range(6))
    book = _book([("One", f"{big} {tail}")])
    chunks = chunk_book(book, BOOK_ID)
    first = chunks[0].text
    assert "A" * 900 in first
    assert "Short sentence number 0." in first  # packed together, not isolated


def test_overlap_survives_below_half_budget_boundary() -> None:
    # Restores coverage of the carried-overlap path for sentences just
    # under half the budget, which the old isolation branch intercepted.
    sentences = " ".join("C" * 700 + "." for _ in range(4))
    book = _book([("One", sentences)])
    chunks = chunk_book(book, BOOK_ID)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk.text) <= 1600


def test_whitespace_free_run_is_hard_cut_to_budget() -> None:
    # A space-free run (like a long URL) used to be emitted whole.
    # It must now be hard-cut to budget_chars to keep chunks bounded.
    text = "Y" * 5000
    book = _book([("One", text)])
    chunks = chunk_book(book, BOOK_ID)
    for chunk in chunks:
        assert len(chunk.text) <= 1600
    combined = " ".join(c.text for c in chunks)
    # Overlap duplication means count might exceed 5000, but must include all.
    assert combined.count("Y") >= 5000


def test_split_oversized_terminates_on_pathological_input() -> None:
    # Pathological input: 20000 chars with no spaces should not cause
    # infinite loops; the hard-cut path must terminate.
    text = "Z" * 20000
    book = _book([("One", text)])
    chunks = chunk_book(book, BOOK_ID)
    assert len(chunks) <= 40  # 20000 / 1600 ≈ 13, plus overlap gives ~26
