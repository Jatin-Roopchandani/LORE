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


def test_blank_line_runs_do_not_split_scenes() -> None:
    # CHANGED, deliberately. This test previously asserted the opposite
    # ({0, 1}) and so pinned the bug: a blank-line run was a hard scene
    # boundary, which made chunk size depend on an edition's whitespace
    # habits and collapsed the Sherlock Holmes EPUBs to a 170-char median
    # against a 1600-char budget. A blank line separates paragraphs; only
    # an explicit marker separates scenes. See the neighbouring
    # test_an_explicit_scene_marker_still_ends_a_chunk for the boundary
    # that remains hard.
    text = _long_text(10) + "\n\n\n" + _long_text(10)
    book = _book([("One", text)])
    chunks = chunk_book(book, BOOK_ID)
    assert {c.scene_seq for c in chunks} == {0}
    # and the two sides really did pack together rather than merely
    # sharing a scene number
    assert any(
        "This is sentence number 0 padding" in c.text
        and "This is sentence number 9 padding" in c.text
        for c in chunks
    )


def test_paragraphs_separated_by_blank_lines_pack_to_budget() -> None:
    # A Gutenberg EPUB may separate every paragraph with a blank-line
    # run. Those are paragraph breaks, not scene breaks: packing must
    # cross them, or every paragraph becomes its own tiny chunk and the
    # extractor gets no context to work with.
    #
    # Every separator width is exercised because editions really do
    # differ: Pride and Prejudice writes "\n\n" between paragraphs, the
    # Gutenberg Holmes editions write "\n\n\n" — and it was only the
    # 3-or-more form that the old blank-run scene split fired on, which
    # is exactly why the bug was edition-dependent and silent.
    para = "Holmes lit his pipe and considered the matter at length."
    for sep in ("\n\n", "\n\n\n", "\n\n\n\n"):
        text = sep.join([para] * 40)
        chunks = chunk_book(_book([("One", text)]), "b" * 16)
        assert len(chunks) < 10, repr(sep)
        assert max(len(c.text) for c in chunks) > 800, repr(sep)


def test_an_explicit_scene_marker_still_ends_a_chunk() -> None:
    # The marked break is the one that carries authorial meaning, so it
    # stays hard even though blank lines no longer are.
    text = "Before the break happened here.\n\n***\n\nAfter the break happened here."
    chunks = chunk_book(_book([("One", text)]), "b" * 16)
    assert len(chunks) == 2
    assert "Before" in chunks[0].text and "After" in chunks[1].text


def test_a_marker_still_splits_when_paragraphs_surround_it() -> None:
    para = "Some sentence of reasonable length to pack together."
    # Blank-line runs on both sides of the marker: they pack across, the
    # marker does not.
    text = ("\n\n\n".join([para] * 6) + "\n\n***\n\n"
            + "\n\n\n".join([para] * 6))
    chunks = chunk_book(_book([("One", text)]), "b" * 16)
    assert len(chunks) >= 2
    # nothing may span the marker
    for chunk in chunks:
        assert not ("***" in chunk.text)


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


_GUTENBERG_LICENCE = (
    "This eBook is for the use of anyone anywhere in the United States and "
    "most other parts of the world at no cost and with almost no restrictions "
    "whatsoever. You may copy it, give it away or re-use it under the terms "
    "of the Project Gutenberg License included with this eBook or online at "
    "www.gutenberg.org."
)


def test_front_matter_chunks_are_dropped() -> None:
    book = _book([("Licence", _GUTENBERG_LICENCE), ("One", _long_text(60))])
    chunks = chunk_book(book, BOOK_ID)
    assert all("Project Gutenberg" not in c.text for c in chunks)
    assert any(c.chapter_seq == 2 for c in chunks)


def test_chunk_ids_stay_contiguous_after_dropping() -> None:
    text = _GUTENBERG_LICENCE + "\n\n\n" + _long_text(60)
    book = _book([("One", text)])
    chunks = chunk_book(book, BOOK_ID)
    assert all("Project Gutenberg" not in c.text for c in chunks)
    seqs = [c.seq for c in chunks if c.chapter_seq == 1]
    assert seqs == list(range(len(seqs)))
    for chunk in chunks:
        assert chunk.id == f"{BOOK_ID}:{chunk.chapter_seq}:{chunk.seq}"


def test_drop_front_matter_can_be_disabled() -> None:
    book = _book([("Licence", _GUTENBERG_LICENCE), ("One", _long_text(60))])
    chunks = chunk_book(book, BOOK_ID, drop_front_matter=False)
    assert any("Project Gutenberg" in c.text for c in chunks)


def test_front_matter_adjacent_to_narrative_keeps_the_narrative() -> None:
    # A licence sentence sitting beside ordinary narrative in the SAME
    # scene (plain "\n\n" paragraph breaks, not a "\n\n\n" scene break) must
    # only remove itself, not the whole pack it lands in alongside it.
    licence = (
        "This short passage is Project Gutenberg boilerplate text mentioning "
        "the eBook license terms."
    )
    narrative = "\n\n".join(
        f"Narrative sentence number {i} continues the scene." for i in range(12)
    )
    text = licence + "\n\n" + narrative
    book = _book([("One", text)])
    chunks = chunk_book(book, BOOK_ID)
    combined = " ".join(c.text for c in chunks)
    assert "Project Gutenberg" not in combined
    for i in range(12):
        assert f"Narrative sentence number {i} continues the scene." in combined


def test_a_front_matter_banner_does_not_swallow_the_prose_before_it() -> None:
    # Found by probing the real EPUBs, not by any listed test. Gutenberg
    # closes a book with "*** END OF THE PROJECT GUTENBERG EBOOK ... ***"
    # on its own line. That is NOT a scene marker (_SCENE_MARKER wants a
    # bare ***, not *** with text after it), and once blank-line runs
    # stopped being scene boundaries there was nothing left to stop pysbd
    # gluing the banner onto the closing quote of the last narrative
    # sentence — is_front_matter then read the merged blob as boilerplate
    # and deleted the end of the book. Silently. Segmenting sentences per
    # paragraph is what keeps the banner a sentence of its own.
    prose = (
        "“That’s the result of all our Study in Scarlet: to get them a "
        "testimonial!”"
    )
    text = (
        prose
        + "\n\n\n*** END OF THE PROJECT GUTENBERG EBOOK A STUDY IN SCARLET ***"
        + "\n\n\nUpdated editions will replace the previous one."
    )
    chunks = chunk_book(_book([("One", text)]), BOOK_ID)
    combined = " ".join(c.text for c in chunks)
    assert "result of all our Study in Scarlet" in combined
    assert "END OF THE PROJECT GUTENBERG" not in combined


def test_a_sentence_never_spans_a_paragraph_break() -> None:
    # A paragraph break is a sentence break even where pysbd would not
    # split: packing may cross it, segmentation may not.
    first = "He spoke without a full stop"
    second = "A new paragraph begins here."
    text = f"{first}\n\n\n{second}"
    chunks = chunk_book(_book([("One", text)]), BOOK_ID, budget_chars=40)
    # 40 chars cannot hold both. Segmented per paragraph they are two
    # clean sentences and come back verbatim; segmented as one flat blob
    # pysbd returns a single 57-char pseudo-sentence that _split_oversized
    # then hard-cuts mid-phrase, so the paragraphs come back mangled.
    assert [c.text for c in chunks] == [first, second]
