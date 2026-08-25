from pathlib import Path

import pytest

from lore.ingest.chunker import chunk_book
from lore.ingest.epub import parse_epub
from lore.ingest.models import IngestError
from tests.fixtures import make_epub


def test_parses_metadata_and_chapters(tmp_path: Path) -> None:
    path = make_epub(
        tmp_path / "b.epub",
        title="The Test Book",
        author="Ann Author",
        chapters=[
            ("Chapter One", "<p>First paragraph.</p><p>Second paragraph.</p>"),
            ("Chapter Two", "<p>More text here.</p>"),
        ],
    )
    book = parse_epub(path)
    assert book.title == "The Test Book"
    assert book.author == "Ann Author"
    assert [c.title for c in book.chapters] == ["Chapter One", "Chapter Two"]
    assert [c.seq for c in book.chapters] == [1, 2]
    assert "First paragraph." in book.chapters[0].text
    assert "Second paragraph." in book.chapters[0].text


def test_html_becomes_clean_text_with_paragraph_breaks(tmp_path: Path) -> None:
    path = make_epub(
        tmp_path / "b.epub",
        title="T",
        author="A",
        chapters=[("One", "<p>Para one.</p><p>Para two.</p><p><i>Styled</i> text.</p>")],
    )
    text = parse_epub(path).chapters[0].text
    assert "<" not in text and ">" not in text
    assert "Para one.\n\nPara two." in text.replace("\n\n\n", "\n\n") or (
        "Para one." in text and "Para two." in text
    )
    assert "Styled text." in text


def test_hr_becomes_scene_marker(tmp_path: Path) -> None:
    path = make_epub(
        tmp_path / "b.epub",
        title="T",
        author="A",
        chapters=[("One", "<p>Scene one.</p><hr/><p>Scene two.</p>")],
    )
    text = parse_epub(path).chapters[0].text
    assert "***" in text


def test_empty_or_navigation_docs_are_skipped(tmp_path: Path) -> None:
    path = make_epub(
        tmp_path / "b.epub",
        title="T",
        author="A",
        chapters=[("One", "<p>Real content.</p>"), ("Blank", "<p>  </p>")],
    )
    book = parse_epub(path)
    assert [c.title for c in book.chapters] == ["One"]


def test_unreadable_file_raises_ingest_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.epub"
    bad.write_bytes(b"this is not an epub")
    with pytest.raises(IngestError):
        parse_epub(bad)


def test_multiple_headings_in_one_document_become_chapters(tmp_path: Path) -> None:
    # make_epub wraps the body in "<h1>{title}</h1>", so this single spine
    # document is: <h1>Doc</h1><h2>Chapter One</h2>...<h2>Chapter Three</h2>...
    # The h1 "Doc" section has no text before the first h2, so it's empty
    # and skipped, leaving exactly the three h2-delimited chapters.
    path = make_epub(
        tmp_path / "b.epub",
        title="T",
        author="A",
        chapters=[
            (
                "Doc",
                "<h2>Chapter One</h2><p>First body.</p>"
                "<h2>Chapter Two</h2><p>Second body.</p>"
                "<h2>Chapter Three</h2><p>Third body.</p>",
            )
        ],
    )
    book = parse_epub(path)
    assert [c.title for c in book.chapters] == [
        "Chapter One",
        "Chapter Two",
        "Chapter Three",
    ]
    assert [c.seq for c in book.chapters] == [1, 2, 3]
    assert "First body." in book.chapters[0].text
    assert "Second body." in book.chapters[1].text
    assert "Third body." in book.chapters[2].text
    for chapter in book.chapters:
        assert "Chapter Two" not in chapter.text


def test_preamble_before_first_heading_kept(tmp_path: Path) -> None:
    # make_epub wraps the body in "<h1>{title}</h1>", so the preamble text
    # lands between the h1 and the first h2 — it becomes the h1's section
    # body (titled by the h1 heading), followed by a separate h2 section.
    path = make_epub(
        tmp_path / "b.epub",
        title="T",
        author="A",
        chapters=[
            ("Doc", "<p>Preamble text here.</p><h2>Real Chapter</h2><p>Body.</p>")
        ],
    )
    book = parse_epub(path)
    assert [c.title for c in book.chapters] == ["Doc", "Real Chapter"]
    assert "Preamble text here." in book.chapters[0].text
    assert "Body." in book.chapters[1].text
    assert "Preamble text here." not in book.chapters[1].text


def test_adjacent_paragraphs_do_not_scene_split(tmp_path: Path) -> None:
    # Two normal, non-empty adjacent paragraphs must produce a single
    # paragraph break ("\n\n"), not a scene break ("\n\n\n"+).
    path = make_epub(
        tmp_path / "b.epub",
        title="T",
        author="A",
        chapters=[("One", "<p>Para one.</p><p>Para two.</p>")],
    )
    book = parse_epub(path)
    text = book.chapters[0].text
    assert "\n\n\n" not in text
    assert "Para one." in text and "Para two." in text
    chunks = chunk_book(book, book_id="b")
    assert all(c.scene_seq == 0 for c in chunks)


def test_empty_paragraph_run_still_scene_splits(tmp_path: Path) -> None:
    # A run of empty/whitespace-only paragraphs between two real ones must
    # still accumulate into a scene break.
    path = make_epub(
        tmp_path / "b.epub",
        title="T",
        author="A",
        chapters=[
            ("One", "<p>Scene one.</p><p> </p><p> </p><p>Scene two.</p>")
        ],
    )
    book = parse_epub(path)
    text = book.chapters[0].text
    assert "\n\n\n" in text
    chunks = chunk_book(book, book_id="b")
    assert {c.scene_seq for c in chunks} == {0, 1}


def test_headingless_continuation_appends_to_previous_chapter(
    tmp_path: Path,
) -> None:
    # A Gutenberg-style spine document that splits mid-chapter (e.g. at an
    # illustration) leaves the chapter's tail as a headingless document with
    # no h1/h2/h3 at all. That tail must be appended to the chapter already
    # in progress, not treated as a new bogus chapter.
    path = make_epub(
        tmp_path / "b.epub",
        title="T",
        author="A",
        chapters=[
            ("One", "<p>Chapter text starts.</p>"),
            ("Two", "<p>And continues here.</p>"),
        ],
        raw={2},
    )
    book = parse_epub(path)
    assert [c.title for c in book.chapters] == ["One"]
    assert "Chapter text starts." in book.chapters[0].text
    assert "And continues here." in book.chapters[0].text


def test_nested_blockquote_does_not_scene_split(tmp_path: Path) -> None:
    # A block nested inside another block (blockquote>p) must not cause the
    # OUTER block's close to add a second separator on top of the inner
    # block's own — that double-separator is what manufactures a spurious
    # scene break out of ordinary nested markup.
    path = make_epub(
        tmp_path / "b.epub",
        title="T",
        author="A",
        chapters=[
            (
                "One",
                "<p>Para A.</p><blockquote><p>Quoted.</p></blockquote>"
                "<p>Para B.</p>",
            )
        ],
    )
    book = parse_epub(path)
    text = book.chapters[0].text
    assert "\n\n\n" not in text
    chunks = chunk_book(book, book_id="b")
    assert all(c.scene_seq == 0 for c in chunks)


def test_block_wrapped_heading_does_not_scene_split(tmp_path: Path) -> None:
    # A heading nested inside a still-open block (div>...<h2>...</h2>...)
    # swaps the active section mid-block. When that outer block finally
    # closes (in the NEW section), it must not add a spurious extra
    # separator on top of what its own nested content already closed with.
    path = make_epub(
        tmp_path / "b.epub",
        title="T",
        author="A",
        chapters=[
            (
                "One",
                "<div><p>Para A.</p><h2>Ch2</h2><p>Para B.</p></div>"
                "<p>Para C.</p>",
            )
        ],
    )
    book = parse_epub(path)
    ch2 = next(c for c in book.chapters if c.title == "Ch2")
    assert "Para B." in ch2.text
    assert "Para C." in ch2.text
    assert "\n\n\n" not in ch2.text
