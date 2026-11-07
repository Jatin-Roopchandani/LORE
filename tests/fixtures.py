"""Programmatic test fixtures — no binary files in git."""
from pathlib import Path

from ebooklib import epub
import pymupdf


def make_epub(
    path: Path,
    title: str,
    author: str,
    chapters: list[tuple[str, str]],
    raw: set[int] | None = None,
    series: tuple[str, float] | None = None,
) -> Path:
    """chapters: list of (chapter_title, html_body) pairs.

    raw: 1-based indices (matching enumerate(chapters, start=1)) whose
    body_html is used as-is, WITHOUT the default "<h1>{title}</h1>"
    wrapper — for tests that need to control heading placement precisely
    (e.g. a headingless spine document).
    """
    book = epub.EpubBook()
    book.set_identifier("fixture-id")
    book.set_title(title)
    book.set_language("en")
    book.add_author(author)
    if series is not None:  # calibre-style series metadata
        book.add_metadata(None, "meta", "", {"name": "calibre:series", "content": series[0]})
        book.add_metadata(None, "meta", "", {"name": "calibre:series_index", "content": str(series[1])})
    items = []
    for i, (chapter_title, body_html) in enumerate(chapters, start=1):
        item = epub.EpubHtml(
            title=chapter_title, file_name=f"ch{i}.xhtml", lang="en"
        )
        item.content = (
            body_html if raw and i in raw else f"<h1>{chapter_title}</h1>{body_html}"
        )
        book.add_item(item)
        items.append(item)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.toc = items
    book.spine = ["nav", *items]
    epub.write_epub(str(path), book)
    return path


def make_raw_epub(
    path: Path,
    docs: list[tuple[str, str]],
    ncx: list | None = None,
    nav: list | None = None,
    title: str = "Raw Book",
) -> Path:
    """An EPUB written by hand, for table-of-contents tests ebooklib cannot
    write (no headings, NCX only, nav only, src#fragment anchors).

    docs: (file name under OEBPS/, body html) in spine order.
    ncx / nav: TOC entries (label, href) or (label, href, [children]);
    None leaves that TOC out of the book.
    """
    import zipfile
    from html import escape

    def page(body: str) -> str:
        return ('<?xml version="1.0" encoding="utf-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml" '
                'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>x</title></head>'
                f"<body>{body}</body></html>")

    def ncx_points(entries, counter=[0]) -> str:
        out = []
        for e in entries:
            counter[0] += 1
            kids = ncx_points(e[2]) if len(e) > 2 else ""
            out.append(f'<navPoint id="np{counter[0]}" playOrder="{counter[0]}"><navLabel><text>'
                       f'{escape(e[0])}</text></navLabel><content src="{e[1]}"/>{kids}</navPoint>')
        return "".join(out)

    def nav_list(entries) -> str:
        items = []
        for e in entries:
            kids = nav_list(e[2]) if len(e) > 2 else ""
            items.append(f'<li><a href="{e[1]}">{escape(e[0])}</a>{kids}</li>')
        return f"<ol>{''.join(items)}</ol>"

    manifest = [f'<item id="d{i}" href="{name}" media-type="application/xhtml+xml"/>'
                for i, (name, _) in enumerate(docs)]
    if ncx is not None:
        manifest.append('<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>')
    if nav is not None:
        manifest.append('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
                        'properties="nav"/>')
    spine_attr = ' toc="ncx"' if ncx is not None else ""
    opf = ('<?xml version="1.0" encoding="utf-8"?>\n<package xmlns="http://www.idpf.org/2007/opf" '
           'version="3.0" unique-identifier="uid"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           f'<dc:identifier id="uid">raw-1</dc:identifier><dc:title>{escape(title)}</dc:title>'
           '<dc:language>en</dc:language><dc:creator>A</dc:creator></metadata>'
           f"<manifest>{''.join(manifest)}</manifest><spine{spine_attr}>"
           + "".join(f'<itemref idref="d{i}"/>' for i in range(len(docs)))
           + "</spine></package>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="OEBPS/content.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", opf)
        for name, body in docs:
            z.writestr(f"OEBPS/{name}", page(body))
        if ncx is not None:
            z.writestr("OEBPS/toc.ncx",
                       '<?xml version="1.0" encoding="utf-8"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" '
                       f'version="2005-1"><head/><docTitle><text>{escape(title)}</text></docTitle>'
                       f"<navMap>{ncx_points(ncx, [0])}</navMap></ncx>")
        if nav is not None:
            z.writestr("OEBPS/nav.xhtml",
                       page(f'<nav epub:type="toc"><h1>Contents</h1>{nav_list(nav)}</nav>'))
    return path


def make_pdf(
    path: Path,
    pages: list[str],
    heading_pages: dict[int, str] | None = None,
    header: str | None = None,
) -> Path:
    """pages: body text per page. heading_pages: {page_index: heading} drawn
    in a large font. header: line repeated at the top of every page."""
    doc = pymupdf.open()
    for i, body in enumerate(pages):
        page = doc.new_page()
        y = 60.0
        if header is not None:
            page.insert_text((72, 40), header, fontsize=9)
        if heading_pages and i in heading_pages:
            page.insert_text((72, 80), heading_pages[i], fontsize=24)
            y = 120.0
        page.insert_textbox(
            pymupdf.Rect(72, y, 520, 780), body, fontsize=11
        )
    doc.save(str(path))
    doc.close()
    return path


def seed_book(book_id: str, chunks: int = 1) -> None:
    """Register `book_id` with `chunks` chunks in the library the env points at.

    `lore process` runs through the job store, whose rows reference `books`,
    so a CLI test that fakes extraction and load still needs the book row.
    """
    from lore.config import LoreConfig
    from lore.db.sqlite import open_db

    config = LoreConfig.load()
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    try:
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO books (id, title, file_path) VALUES (?, ?, ?)",
                (book_id, "T", "t.epub"))
            for i in range(chunks):
                conn.execute(
                    "INSERT OR IGNORE INTO chunks (id, book_id, chapter_seq,"
                    " scene_seq, seq, text) VALUES (?, ?, 0, 0, ?, 'x')",
                    (f"{book_id}-c{i}", book_id, i))
    finally:
        conn.close()
