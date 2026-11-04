"""Front matter, story and back matter: lore/ingest/matter.py.

Invented books only. One has everything an ebook wraps a story in (a licence
header, a "by" line, a contents page, a preface, a part title page, then the
chapters, then notes and a licence); one is nothing but chapters.
"""
from __future__ import annotations

from lore.ingest.matter import (
    BACK, FRONT, STORY, Section, classify, heading_label, is_boilerplate, is_contents,
    story_numbers)


def prose(word: str, n: int = 30) -> str:
    return " ".join(f"The {word} walked on through the grey morning and said little." for _ in range(n))


LICENCE_HEAD = ("This ebook is for the use of anyone anywhere in the United States and most other"
                " parts of the world at no cost and with almost no restrictions whatsoever. You may"
                " copy it, give it away or re-use it under the terms of the Project Gutenberg License"
                " included with this ebook or online at www.gutenberg.org.")
LICENCE_TAIL = "\n\n".join(
    [f"1.F.{i}. The Project Gutenberg Literary Archive Foundation and any distributor of a Project"
     f" Gutenberg electronic work may not be held liable, clause {i}." for i in range(1, 40)])
CONTENTS = ("THE MARROW ROAD.\n\nPART I.\n\nCHAPTER I. THE TOLL HOUSE.\n\nCHAPTER II. A LETTER"
            " FROM VELL.\n\nCHAPTER III. THE FERRY.\n\nPART II.\n\nCHAPTER I. THE SALT"
            " FLATS.\n\nCHAPTER II. THE PROPHET'S BARGAIN.")

WRAPPED = [
    Section(1, "The Project Gutenberg eBook of The Marrow Road", LICENCE_HEAD),
    Section(2, "By Odile Marsh", "***"),
    Section(3, "CONTENTS", CONTENTS),
    Section(4, "PREFACE.", prose("editor", 60)),
    Section(5, "PART I.", "(Being the account of a ferryman.)"),
    Section(6, "CHAPTER I. \nTHE TOLL HOUSE.", prose("tollkeeper")),
    Section(7, "CHAPTER II. \nA LETTER FROM VELL.", prose("courier")),
    Section(8, "CHAPTER III. \nTHE FERRY.", prose("ferryman")),
    Section(9, "CHAPTER I. \nTHE SALT FLATS.", prose("pilgrim")),
    Section(10, "CHAPTER II. \nTHE PROPHET'S BARGAIN.", prose("prophet")
            + "\n\n*** END OF THE PROJECT GUTENBERG EBOOK THE MARROW ROAD ***"),
    Section(11, "NOTES", "1. The ferry is based on a real crossing. 2. Vell is invented."),
    Section(12, "THE FULL PROJECT GUTENBERG™ LICENSE", LICENCE_TAIL),
]

PLAIN = [
    Section(1, "Chapter 1\nThe Well", prose("girl")),
    Section(2, "Chapter 2", "It rained."),  # a very short real chapter
    Section(3, "Chapter 3\nThe Introduction of Mr. Pell", prose("suitor")),
    Section(4, "Interval", "Snow."),  # short, but in the middle: story
    Section(5, "Chapter 4\nHome", prose("girl") + " As chapter 2 told, and chapter 3 hinted."),
]


def matters(classified):
    return [c.matter for c in classified]


def test_a_wrapped_book_keeps_only_its_chapters_as_story():
    got = classify(WRAPPED)
    assert matters(got) == [FRONT] * 5 + [STORY] * 5 + [BACK] * 2
    assert [c.reason for c in got[:5]] == ["boilerplate", "short", "contents", "title", "short"]
    assert [c.reason for c in got[10:]] == ["title", "boilerplate"]


def test_story_chapters_are_numbered_from_one_with_their_headings():
    got = classify(WRAPPED)
    assert story_numbers(got) == {6: 1, 7: 2, 8: 3, 9: 4, 10: 5}
    assert [c.heading for c in got if c.matter == STORY] == [
        "The Toll House", "A Letter from Vell", "The Ferry", "The Salt Flats",
        "The Prophet's Bargain"]


def test_a_book_with_no_front_or_back_matter_is_all_story():
    got = classify(PLAIN)
    assert matters(got) == [STORY] * 5
    assert story_numbers(got) == {1: 1, 2: 2, 3: 3, 4: 4, 5: 5}
    assert [c.heading for c in got] == ["The Well", None, "The Introduction of Mr. Pell",
                                        "Interval", "Home"]


def test_chunk_text_without_titles_is_classified_too():
    """`lore repair matter` without the source file: flattened chunk text only."""
    flat = [Section(s.seq, None, " ".join(s.text.split())) for s in WRAPPED]
    got = classify(flat)
    assert matters(got)[:4] == [FRONT, FRONT, FRONT, STORY]  # the preface needs its title
    assert matters(got)[5:10] == [STORY] * 5
    assert matters(got)[-1] == BACK


def test_one_word_of_a_title_does_not_make_a_section_front_matter():
    sections = [Section(1, "Introduction to the Valley", prose("shepherd")),
                Section(2, "Index Cards", prose("clerk"))]
    assert matters(classify(sections)) == [STORY, STORY]


def test_a_preface_after_the_story_has_begun_is_story():
    sections = [Section(1, "Chapter 1", prose("girl")), Section(2, "Preface", prose("poet")),
                Section(3, "Chapter 2", prose("girl"))]
    assert matters(classify(sections)) == [STORY] * 3


def test_a_contents_page_in_the_middle_is_not_story():
    """An omnibus: the second novel's contents page."""
    sections = [Section(1, "Chapter I", prose("a")), Section(2, None, CONTENTS),
                Section(3, "Chapter I", prose("b"))]
    assert matters(classify(sections)) == [STORY, FRONT, STORY]


def test_a_book_that_would_have_no_story_keeps_everything_but_licence_text():
    sections = [Section(1, "Contents", CONTENTS), Section(2, None, "A short poem."),
                Section(3, "License", LICENCE_TAIL)]
    assert matters(classify(sections)) == [STORY, STORY, BACK]


def test_signals():
    assert is_boilerplate(None, LICENCE_HEAD)
    assert is_boilerplate(None, LICENCE_TAIL)
    assert not is_boilerplate(None, WRAPPED[9].text)  # a chapter with the end banner
    assert matters(classify([Section(1, "Copyright", "Published 2001."),
                             Section(2, "Chapter 1", prose("girl"))])) == [FRONT, STORY]
    assert is_boilerplate(None, "Copyright © 2001 A. Writer. All rights reserved. ISBN 0-00-000000-0")
    assert is_contents(None, CONTENTS) and is_contents(None, " ".join(CONTENTS.split()))
    assert is_contents("Table of Contents", "")
    assert not is_contents(None, PLAIN[4].text)


def test_heading_labels():
    assert heading_label("CHAPTER III. \nTHE LAURISTON GARDENS MYSTERY") == "The Lauriston Gardens Mystery"
    assert heading_label("CHAPTER VI. \nTHE REMINISCENCES OF JOHN WATSON, M.D.") == \
        "The Reminiscences of John Watson, M.D."
    assert heading_label("A caption.\n \n \nCHAPTER XIII.") is None
    assert heading_label("CHAPTERXXVII.") is None
    assert heading_label("Part II") == "Part II"
    assert heading_label("BOOK TWO: THE RIVER") == "Book Two: The River"
    assert heading_label("Prologue") == "Prologue"
    assert heading_label("The Boy Who Lived") == "The Boy Who Lived"
    # Titles a parser made up are no heading.
    assert heading_label("Chapter 7", 7) is None
    assert heading_label("Pages 16-30") is None
    assert heading_label(None) is None and heading_label("  ") is None
