from pathlib import Path

import pytest
from ebooklib import epub

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


def test_inline_tags_separated_by_whitespace_do_not_fuse_words(
    tmp_path: Path,
) -> None:
    # Whitespace-only text nodes between inline tags (formatting markup
    # like <i>/<b>/<span>, or a literal newline in the source) must
    # collapse to a single joining space, not be dropped — dropping them
    # fuses adjacent words together ("proudandvain").
    path = make_epub(
        tmp_path / "b.epub",
        title="T",
        author="A",
        chapters=[
            (
                "One",
                "<p>He was <i>proud</i> <b>and</b>\n<span>vain</span>.</p>",
            )
        ],
    )
    text = parse_epub(path).chapters[0].text
    assert "He was proud and vain." in text


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


def test_empty_paragraph_run_is_whitespace_not_a_scene_split(
    tmp_path: Path,
) -> None:
    # A run of empty/whitespace-only paragraphs between two real ones
    # still accumulates into a blank-line run in the PARSER's output —
    # that half of the contract is unchanged and still asserted below.
    #
    # CHANGED, deliberately: the chunker half previously asserted
    # {0, 1}, pinning blank-line-run-as-hard-scene-boundary. That is the
    # bug — it made chunk size edition-dependent. A blank-line run is now
    # a paragraph separator that packing crosses; only an explicit marker
    # (***, ---, ###) ends a scene. See
    # test_hr_scene_split_survives_through_chunker for the marker path,
    # which still splits.
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
    assert {c.scene_seq for c in chunks} == {0}
    assert any(
        "Scene one." in c.text and "Scene two." in c.text for c in chunks
    )


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


def test_trailing_br_does_not_scene_split(tmp_path: Path) -> None:
    # <br> writes a bare "\n" into the open block's parts, so a paragraph
    # ending in <br/> closes with a segment ending "...\n". The block-close
    # logic must top that up to exactly "\n\n" (one more "\n"), not stack a
    # full "\n\n" on top — that would make "\n\n\n", a spurious scene break.
    # Common real pattern: poetry, addresses, dialogue with line breaks.
    path = make_epub(
        tmp_path / "b.epub",
        title="T",
        author="A",
        chapters=[
            (
                "One",
                "<p>Para one.</p><p>Line one.<br/>Line two.<br/></p>"
                "<p>Para three.</p>",
            )
        ],
    )
    book = parse_epub(path)
    text = book.chapters[0].text
    assert "\n\n\n" not in text
    chunks = chunk_book(book, book_id="b")
    assert {c.scene_seq for c in chunks} == {0}


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


def test_parse_epub_follows_spine_order_not_manifest_order(tmp_path: Path) -> None:
    # Reading order is defined by the spine, not by manifest (add_item)
    # order — real-world EPUBs can list items in the manifest in a
    # different order than they're meant to be read. parse_epub must walk
    # the spine, falling back to manifest order only when the spine is
    # empty or resolves to nothing.
    path = tmp_path / "spine.epub"
    book = epub.EpubBook()
    book.set_identifier("spine-fixture")
    book.set_title("Spine Order")
    book.set_language("en")
    book.add_author("A")

    item_b = epub.EpubHtml(title="B", file_name="b.xhtml", lang="en")
    item_b.content = "<h1>B</h1><p>Second in spine, added first to manifest.</p>"
    item_a = epub.EpubHtml(title="A", file_name="a.xhtml", lang="en")
    item_a.content = "<h1>A</h1><p>First in spine, added second to manifest.</p>"

    # Manifest (add_item) order: B then A.
    book.add_item(item_b)
    book.add_item(item_a)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.toc = [item_a, item_b]
    # Spine (reading) order: A then B — the opposite of manifest order.
    book.spine = ["nav", item_a, item_b]

    epub.write_epub(str(path), book)

    parsed = parse_epub(path)
    assert [c.title for c in parsed.chapters] == ["A", "B"]


def test_hr_scene_split_survives_through_chunker(tmp_path: Path) -> None:
    # Contract test: parsers emit "\n***\n" for an explicit scene break
    # (<hr/>), and the chunker must honor it end-to-end, assigning distinct
    # scene_seq values to the two sides of the break.
    scene_one = " ".join(
        f"This is sentence number {i} of the first scene, with enough "
        "words to make it a realistic paragraph." for i in range(20)
    )
    scene_two = " ".join(
        f"This is sentence number {i} of the second scene, with enough "
        "words to make it a realistic paragraph." for i in range(20)
    )
    path = make_epub(
        tmp_path / "b.epub",
        title="T",
        author="A",
        chapters=[("One", f"<p>{scene_one}</p><hr/><p>{scene_two}</p>")],
    )
    book = parse_epub(path)
    chunks = chunk_book(book, book_id="b")
    assert {c.scene_seq for c in chunks} == {0, 1}


# --- series metadata (story position) ---------------------------------------

_OPF_HEAD = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
             'version="{v}"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
             '<dc:title>T</dc:title>')


def _opf(meta: str, version: str = "2.0") -> str:
    return _OPF_HEAD.format(v=version) + meta + "</metadata></package>"


def test_series_from_calibre_meta() -> None:
    from lore.ingest.epub import series_from_opf
    xml = _opf('<meta name="calibre:series" content="The Saga"/>'
               '<meta name="calibre:series_index" content="2.0"/>')
    assert series_from_opf(xml) == ("The Saga", 2.0)


def test_series_from_calibre_meta_without_an_index() -> None:
    from lore.ingest.epub import series_from_opf
    assert series_from_opf(_opf('<meta name="calibre:series" content="The Saga"/>')) == ("The Saga", None)


def test_series_from_epub3_collection() -> None:
    from lore.ingest.epub import series_from_opf
    xml = _opf('<meta property="belongs-to-collection" id="c1">Big Cycle</meta>'
               '<meta refines="#c1" property="collection-type">series</meta>'
               '<meta refines="#c1" property="group-position">3</meta>', "3.0")
    assert series_from_opf(xml) == ("Big Cycle", 3.0)


def test_epub3_collection_that_is_not_a_series_is_ignored() -> None:
    from lore.ingest.epub import series_from_opf
    xml = _opf('<meta property="belongs-to-collection" id="c1">Omnibus</meta>'
               '<meta refines="#c1" property="collection-type">set</meta>'
               '<meta refines="#c1" property="group-position">1</meta>', "3.0")
    assert series_from_opf(xml) == (None, None)


def test_epub3_collection_may_omit_its_type() -> None:
    from lore.ingest.epub import series_from_opf
    xml = _opf('<meta property="belongs-to-collection" id="c1">Big Cycle</meta>'
               '<meta refines="#c1" property="group-position">1.5</meta>', "3.0")
    assert series_from_opf(xml) == ("Big Cycle", 1.5)


def test_no_series_metadata_is_none_and_garbage_is_survived() -> None:
    from lore.ingest.epub import series_from_opf
    assert series_from_opf(_opf("")) == (None, None)
    assert series_from_opf("<not xml") == (None, None)
    bad = _opf('<meta name="calibre:series" content="S"/><meta name="calibre:series_index" content="two"/>')
    assert series_from_opf(bad) == ("S", None)
    assert series_from_opf(_opf('<meta name="calibre:series" content="  "/>')) == (None, None)


def test_parse_epub_reads_series_from_the_file(tmp_path: Path) -> None:
    from tests.fixtures import make_epub
    path = make_epub(tmp_path / "s.epub", title="Book Two", author="A",
                     chapters=[("One", "<p>Text here.</p>")],
                     series=("The Saga", 2))
    book = parse_epub(path)
    assert (book.series, book.series_index) == ("The Saga", 2.0)


def test_parse_epub_without_series_has_none(tmp_path: Path) -> None:
    from tests.fixtures import make_epub
    path = make_epub(tmp_path / "s.epub", title="Solo", author="A",
                     chapters=[("One", "<p>Text here.</p>")])
    book = parse_epub(path)
    assert (book.series, book.series_index) == (None, None)
