"""Chapters from the EPUB's own table of contents when headings find too few."""
from pathlib import Path

from lore.ingest.epub import parse_epub, read_toc
from tests.fixtures import make_epub, make_raw_epub

HEADER = "<p>The Great Series Book 1: The Novel</p>"


def _chapter(n: int, sub: str, body: str) -> str:
    # Calibre-like: no heading tags, the chapter label in a styled paragraph
    return (f'<div class="c">{HEADER}<blockquote><span class="bold">Chapter {n}</span>'
            f"</blockquote><blockquote>{sub}</blockquote><p>{body}</p></div>")


def _docs(n: int = 4) -> list[tuple[str, str]]:
    docs = [("title.html", "<p>My Book</p><p>Copyright notice.</p>")]
    docs += [(f"split_{i:03d}.html", _chapter(i, f"Sub {i}", f"Body of chapter {i}."))
             for i in range(1, n + 1)]
    return docs


def test_ncx_only_book_without_headings_splits_at_its_toc(tmp_path: Path) -> None:
    path = make_raw_epub(tmp_path / "b.epub", _docs(),
                         ncx=[(f"Chapter {i}", f"split_{i:03d}.html") for i in range(1, 5)])
    book = parse_epub(path)
    # front matter before the first TOC entry is its own leading chapter, as today
    assert [c.title for c in book.chapters] == [
        "Chapter 1", "Chapter 1: Sub 1", "Chapter 2: Sub 2", "Chapter 3: Sub 3",
        "Chapter 4: Sub 4"]
    assert "Copyright notice." in book.chapters[0].text
    assert [c.seq for c in book.chapters] == [1, 2, 3, 4, 5]
    for i, c in enumerate(book.chapters[1:], start=1):
        assert c.text == f"Body of chapter {i}."  # running header, label, subtitle out


def test_nav_only_book_splits_at_its_nav(tmp_path: Path) -> None:
    path = make_raw_epub(tmp_path / "b.epub", _docs(3),
                         nav=[(f"Chapter {i}", f"split_{i:03d}.html") for i in range(1, 4)])
    assert [c.title for c in parse_epub(path).chapters][1:] == [
        "Chapter 1: Sub 1", "Chapter 2: Sub 2", "Chapter 3: Sub 3"]


def test_fragment_anchors_split_one_document(tmp_path: Path) -> None:
    body = ("<p>Opening words.</p>"
            '<div id="c1"><p>One</p><p>First text.</p></div>'
            '<p id="c2">Two</p><p>Second text.</p>'
            '<p><a name="c3"></a>Three</p><p>Third text.</p>')
    path = make_raw_epub(tmp_path / "b.epub", [("all.html", body)],
                         ncx=[("One", "all.html#c1"), ("Two", "all.html#c2"),
                              ("Three", "all.html#c3")])
    book = parse_epub(path)
    assert [(c.title, c.text) for c in book.chapters] == [
        ("Chapter 1", "Opening words."), ("One", "First text."),
        ("Two", "Second text."), ("Three", "Third text.")]


def test_entries_are_ordered_by_where_they_point_not_toc_order(tmp_path: Path) -> None:
    path = make_raw_epub(tmp_path / "b.epub", _docs(3),
                         ncx=[("C", "split_003.html"), ("A", "split_001.html"),
                              ("B", "split_002.html")])
    assert [c.title for c in parse_epub(path).chapters][1:] == ["A", "B", "C"]


def test_nested_toc_parts_and_chapters(tmp_path: Path) -> None:
    docs = [("p1.html", "<p>Part One</p><p>The Beginning</p>"),
            ("c1.html", "<p>First chapter text.</p>"),
            ("c2.html", "<p>Second chapter text.</p>"),
            ("p2.html", "<p>Part Two</p>"),
            ("c3.html", "<p>Third chapter text.</p>"),
            ("c4.html", "<p>Fourth chapter text.</p>")]
    toc = [("Part One", "p1.html", [("Chapter 1", "c1.html"), ("Chapter 2", "c2.html")]),
           ("Part Two", "p2.html", [("Chapter 3", "c3.html"),
                                    # a part's first chapter in the part's own file
                                    ("Chapter 4", "c4.html")])]
    for kind in ("ncx", "nav"):
        path = make_raw_epub(tmp_path / f"{kind}.epub", docs, **{kind: toc})
        book = parse_epub(path)
        # "Part Two"'s page holds only its label: no text, no chapter
        assert [c.title for c in book.chapters] == [
            "Part One", "Chapter 1", "Chapter 2", "Chapter 3", "Chapter 4"], kind
        assert book.chapters[0].text == "The Beginning"


def test_part_and_chapter_pointing_at_one_place_make_one_chapter(tmp_path: Path) -> None:
    docs = [(f"c{i}.html", f"<p>Text {i}.</p>") for i in range(1, 4)]
    toc = [("Part One", "c1.html", [("Chapter 1", "c1.html"), ("Chapter 2", "c2.html"),
                                    ("Chapter 3", "c3.html")])]
    book = parse_epub(make_raw_epub(tmp_path / "b.epub", docs, ncx=toc))
    assert [(c.title, c.text) for c in book.chapters] == [
        ("Chapter 1", "Text 1."), ("Chapter 2", "Text 2."), ("Chapter 3", "Text 3.")]


def test_document_no_entry_points_at_continues_the_chapter(tmp_path: Path) -> None:
    docs = [("a.html", "<p>A starts.</p>"), ("a2.html", "<p>A goes on.</p>"),
            ("b.html", "<p>B.</p>"), ("c.html", "<p>C.</p>")]
    book = parse_epub(make_raw_epub(tmp_path / "b.epub", docs,
                                    ncx=[("A", "a.html"), ("B", "b.html"), ("C", "c.html")]))
    assert [(c.title, c.text) for c in book.chapters] == [
        ("A", "A starts.\n\nA goes on."), ("B", "B."), ("C", "C.")]


def test_a_toc_of_too_few_places_is_not_used(tmp_path: Path) -> None:
    docs = [("a.html", "<p>A.</p>"), ("b.html", "<p>B.</p>")]
    book = parse_epub(make_raw_epub(tmp_path / "b.epub", docs,
                                    ncx=[("A", "a.html"), ("B", "b.html")]))
    assert [c.title for c in book.chapters] == ["Chapter 1"]  # as before: one chapter


def test_headings_win_when_they_find_the_chapters(tmp_path: Path) -> None:
    # headings in the text and a TOC that names them differently: headings stay
    docs = [(f"c{i}.html", f"<h2>Heading {i}</h2><p>Text {i}.</p>") for i in range(1, 5)]
    book = parse_epub(make_raw_epub(tmp_path / "b.epub", docs,
                                    ncx=[(f"Toc {i}", f"c{i}.html") for i in range(1, 5)]))
    assert [c.title for c in book.chapters] == [f"Heading {i}" for i in range(1, 5)]


def test_missing_anchor_points_at_the_document_start(tmp_path: Path) -> None:
    docs = [(f"c{i}.html", f"<p>Text {i}.</p>") for i in range(1, 4)]
    book = parse_epub(make_raw_epub(tmp_path / "b.epub", docs,
                                    ncx=[(f"C{i}", f"c{i}.html#nowhere") for i in range(1, 4)]))
    assert [c.title for c in book.chapters] == ["C1", "C2", "C3"]


def test_read_toc_resolves_paths_and_survives_garbage(tmp_path: Path) -> None:
    path = make_raw_epub(tmp_path / "b.epub", _docs(2),
                         ncx=[("Chapter 1", "split_001.html#x%20y"), ("Chapter 2", "split_002.html")])
    opf_dir, toc = read_toc(path)
    assert opf_dir == "OEBPS"
    assert [(e.title, e.path, e.fragment) for e in toc] == [
        ("Chapter 1", "OEBPS/split_001.html", "x y"), ("Chapter 2", "OEBPS/split_002.html", None)]
    bad = tmp_path / "bad.epub"
    bad.write_bytes(b"nope")
    assert read_toc(bad) == ("", [])


# Regression: books whose headings work parse exactly as before the TOC path
# existed (the fixtures of tests/test_epub.py, which all carry an NCX + nav).
_HEADING_BOOKS = {
    "two chapters": ([("Chapter One", "<p>First.</p>"), ("Chapter Two", "<p>More.</p>")], None,
                     ["Chapter One", "Chapter Two"]),
    "blank doc skipped": ([("One", "<p>Real content.</p>"), ("Blank", "<p>  </p>")], None,
                          ["One"]),
    "h2s in one doc": ([("Doc", "<h2>Chapter One</h2><p>a</p><h2>Chapter Two</h2><p>b</p>"
                                "<h2>Chapter Three</h2><p>c</p>")], None,
                       ["Chapter One", "Chapter Two", "Chapter Three"]),
    "preamble": ([("Doc", "<p>Pre.</p><h2>Real Chapter</h2><p>Body.</p>")], None,
                 ["Doc", "Real Chapter"]),
    "headingless tail": ([("One", "<p>Starts.</p>"), ("Two", "<p>Goes on.</p>")], {2}, ["One"]),
    "many chapters": ([(f"Chapter {i}", f"<p>t{i}</p>") for i in range(1, 13)], None,
                      [f"Chapter {i}" for i in range(1, 13)]),
}


def test_heading_books_parse_as_before(tmp_path: Path) -> None:
    for name, (chapters, raw, titles) in _HEADING_BOOKS.items():
        book = parse_epub(make_epub(tmp_path / "h.epub", title="T", author="A",
                                    chapters=chapters, raw=raw))
        assert [c.title for c in book.chapters] == titles, name
        assert [c.seq for c in book.chapters] == list(range(1, len(titles) + 1)), name
