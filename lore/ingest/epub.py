"""EPUB → ParsedBook via ebooklib + stdlib HTML text extraction."""
from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree

import ebooklib
from ebooklib import epub

from lore.ingest.models import IngestError, ParsedBook, ParsedChapter

_BLOCK_TAGS = {
    "p", "div", "section", "article", "blockquote", "li", "tr",
    "h1", "h2", "h3", "h4", "h5", "h6", "br",
}
_HEADING_TAGS = {"h1", "h2", "h3"}


class _TextExtractor(HTMLParser):
    """Splits a document's text into sections at every heading tag.

    Each heading (h1/h2/h3) starts a new section: the heading's own text
    becomes that section's title (kept out of the body), and everything
    until the next heading (or end of document) becomes the section's body.
    Text before the first heading forms a leading section with title None.
    """

    def __init__(self, anchors: frozenset[str] | set[str] = frozenset(),
                 split_on_headings: bool = True) -> None:
        super().__init__()
        # anchors: element ids (or <a name>) that start a new section, for a
        # table of contents pointing inside a document (src#fragment). With
        # split_on_headings False, heading tags are ordinary text blocks.
        self._anchors = set(anchors)
        self._split_on_headings = split_on_headings
        self._sections: list[dict] = [{"heading": None, "anchor": None, "parts": []}]
        self._skip_depth = 0
        self._in_heading = False
        self._heading_parts: list[str] = []
        # Per-open-block bookkeeping: (parts_list, position_at_open), pushed
        # on a block tag's start tag and popped on its end tag. At close
        # time, parts_list[position_at_open:] is exactly the segment this
        # block (and anything nested inside it) produced, so we can look at
        # its *content* rather than assume a raw length delta means "this
        # block itself emitted text" — the naive length check double-counts
        # a nested block's own separator as the parent's content too.
        self._block_stack: list[tuple[list[str], int]] = []

    def _start_section(self, anchor: str | None = None) -> None:
        self._sections.append({"heading": None, "anchor": anchor, "parts": []})
        # A new section swaps the active one mid-stream. Any block still
        # open now continues writing into the NEW section, so its segment
        # must be re-anchored to start at position 0 of the new parts list —
        # otherwise its start position would point into the old list.
        new_parts = self._sections[-1]["parts"]
        self._block_stack = [(new_parts, 0) for _ in self._block_stack]

    def handle_starttag(self, tag: str, attrs) -> None:
        if self._anchors:
            names = {v for k, v in attrs if v and (k == "id" or (k == "name" and tag == "a"))}
            hit = sorted(names & self._anchors)
            if hit:
                self._anchors.difference_update(names)
                self._start_section(hit[0])
        if tag in ("script", "style"):
            self._skip_depth += 1
        elif tag == "hr":
            self._sections[-1]["parts"].append("\n***\n")
        elif tag == "br":
            self._sections[-1]["parts"].append("\n")
        elif tag in _BLOCK_TAGS:
            parts = self._sections[-1]["parts"]
            self._block_stack.append((parts, len(parts)))
        if tag in _HEADING_TAGS and self._split_on_headings:
            # (the heading tag's own just-pushed block entry is re-anchored
            # into the new section too)
            self._start_section()
            self._in_heading = True
            self._heading_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            if self._skip_depth:
                self._skip_depth -= 1
        elif tag in ("hr", "br"):
            pass  # already emitted on the start tag
        elif tag in _BLOCK_TAGS:
            if self._block_stack:
                parts, pos = self._block_stack.pop()
            else:
                parts, pos = self._sections[-1]["parts"], len(self._sections[-1]["parts"])
            seg = "".join(parts[pos:])
            target = self._sections[-1]["parts"]
            if seg.strip():
                # This block (or something nested in it) produced real text.
                # Append exactly enough newlines to end the block with
                # "\n\n": a nested block may already have closed with "\n\n"
                # (add nothing), and a trailing <br> leaves a bare "\n" (top
                # up with one more, not a full "\n\n" — that would make
                # "\n\n\n", a spurious scene break).
                if not seg.endswith("\n\n"):
                    target.append("\n" if seg.endswith("\n") else "\n\n")
            elif seg:
                pass  # nested separators only (e.g. blockquote around an
                # empty <p>) — nothing of our own to add
            else:
                target.append("\n")  # a genuinely empty leaf block
        if tag in _HEADING_TAGS and self._in_heading:
            self._in_heading = False
            heading = " ".join(self._heading_parts).strip()
            self._sections[-1]["heading"] = heading or None

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_heading:
            self._heading_parts.append(data)
        elif data.strip():
            # Whitespace-only text nodes are formatting/indentation between
            # sibling tags (ebooklib pretty-prints its XHTML output, and
            # real-world EPUBs are often indented too) — not real content.
            # Ignoring them keeps paragraph-boundary newline counts exact,
            # so the block-open/close bookkeeping isn't thrown off by an
            # incidental "\n    " between two <p> tags.
            self._sections[-1]["parts"].append(data)
        else:
            # A whitespace-only text node CAN be real content: inline tags
            # split by whitespace in the source ("<i>proud</i> <b>and</b>")
            # arrive as separate handle_data calls with a whitespace-only
            # node in between, and dropping it fuses the words together
            # ("proudand"). Distinguish that from pure formatting/
            # indentation — whitespace next to a BLOCK tag boundary, which
            # always closes with a separator ending in "\n" (an empty
            # "<p> </p>", or "\n    " between/around sibling or nested
            # block tags) — by checking whether the most recently emitted
            # text *since the current block opened* is itself non-
            # whitespace: if so, this node is joining two inline runs, so
            # collapse it to a single space; if the block is still empty,
            # or the last thing emitted was already a separator, keep
            # dropping it so the empty-paragraph-run scene-split behavior
            # is unaffected.
            parts = self._sections[-1]["parts"]
            pos = self._block_stack[-1][1] if self._block_stack else 0
            segment = parts[pos:]
            if segment and not segment[-1][-1].isspace():
                parts.append(" ")

    @staticmethod
    def _clean(raw: str) -> str:
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r" ?\n ?", "\n", raw)
        return re.sub(r"\n{4,}", "\n\n\n", raw).strip()

    @property
    def sections(self) -> list[tuple[str | None, str]]:
        """(heading title or None, body text) per section; empty ones dropped."""
        result = []
        for section in self._sections:
            text = self._clean("".join(section["parts"]))
            if text:
                result.append((section["heading"], text))
        return result

    @property
    def anchored_sections(self) -> list[tuple[str | None, str]]:
        """(anchor id that opened it or None, body text) per section, empty
        ones kept: a table-of-contents entry with no text still marks where
        its chapter starts."""
        return [(s["anchor"], self._clean("".join(s["parts"]))) for s in self._sections]


def _metadata(book: epub.EpubBook, field: str) -> str | None:
    values = book.get_metadata("DC", field)
    return values[0][0] if values else None


def _number(text: str | None) -> float | None:
    try:
        value = float((text or "").strip())
    except ValueError:
        return None
    return value if value == value and abs(value) != float("inf") else None


def series_from_opf(opf_xml: str) -> tuple[str | None, float | None]:
    """(series name, position in it) from an OPF document, or (None, None).

    Two conventions: calibre's `<meta name="calibre:series">` with
    `calibre:series_index`, and EPUB3's `belongs-to-collection` with the
    `group-position` refinement (a collection whose type is given and is not
    "series" is ignored). Calibre's wins when both are present. Anything
    unreadable is simply no series: story position is optional metadata and
    must never fail an ingest.
    """
    try:
        root = ElementTree.fromstring(opf_xml)
    except ElementTree.ParseError:
        return None, None
    metas = [el for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "meta"]
    named = {el.get("name"): el.get("content") for el in metas if el.get("name")}
    name = (named.get("calibre:series") or "").strip()
    if name:
        return name, _number(named.get("calibre:series_index"))
    refined: dict[str, dict[str, str]] = {}
    for el in metas:
        target = (el.get("refines") or "").lstrip("#")
        if target and el.get("property"):
            refined.setdefault(target, {})[el.get("property")] = (el.text or "").strip()
    for el in metas:
        if el.get("property") != "belongs-to-collection":
            continue
        name = (el.text or "").strip()
        props = refined.get(el.get("id") or "", {})
        if name and props.get("collection-type", "series").lower() == "series":
            return name, _number(props.get("group-position"))
    return None, None


def _series(path: Path) -> tuple[str | None, float | None]:
    try:
        with zipfile.ZipFile(path) as z:
            container = ElementTree.fromstring(z.read("META-INF/container.xml"))
            rootfile = next(el for el in container.iter()
                            if el.tag.rsplit("}", 1)[-1] == "rootfile")
            return series_from_opf(z.read(rootfile.get("full-path")).decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 -- optional metadata, see series_from_opf
        return None, None


def _spine_documents(book: epub.EpubBook) -> list:
    """Document items in reading (spine) order.

    Manifest order (book.get_items_of_type) need not match reading order —
    the spine is the authoritative source for the order chapters should be
    read/parsed in. Falls back to manifest order when the spine is empty
    or none of its idrefs resolve to a document item.
    """
    items = []
    for idref, _linear in book.spine:
        item = book.get_item_with_id(idref)
        if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        items.append(item)
    if items:
        return items
    return list(book.get_items_of_type(ebooklib.ITEM_DOCUMENT))


def _is_chapter(item) -> bool:
    """Check if item is a chapter. Fallback for older ebooklib versions."""
    # Try using is_chapter() method if available
    if hasattr(item, 'is_chapter') and callable(item.is_chapter):
        try:
            return item.is_chapter()
        except Exception:
            pass
    # Fallback: check if filename contains "nav"
    file_name = getattr(item, 'file_name', '')
    return 'nav' not in file_name.lower()


@dataclass(frozen=True)
class TocEntry:
    """One table-of-contents entry: its label and where it points, as a
    path inside the EPUB zip plus an optional fragment (element id)."""

    title: str
    path: str
    fragment: str | None
    depth: int


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _resolve(base_file: str, href: str) -> tuple[str, str | None]:
    """(zip path, fragment) of an href relative to the file it appears in."""
    raw, _, frag = href.partition("#")
    target = posixpath.normpath(posixpath.join(posixpath.dirname(base_file), unquote(raw)))
    if not raw:
        target = base_file
    return target.lstrip("/"), (unquote(frag) or None)


def _ncx_entries(ncx_path: str, xml: bytes) -> list[TocEntry]:
    root = ElementTree.fromstring(xml)
    nav_map = next((el for el in root.iter() if _local(el.tag) == "navMap"), None)
    out: list[TocEntry] = []

    def walk(node, depth: int) -> None:
        for point in node:
            if _local(point.tag) != "navPoint":
                continue
            label = next((el for el in point.iter() if _local(el.tag) == "text"), None)
            content = next((el for el in point if _local(el.tag) == "content"), None)
            src = content.get("src") if content is not None else None
            title = " ".join((label.text or "").split()) if label is not None else ""
            if src and title:
                target, frag = _resolve(ncx_path, src)
                out.append(TocEntry(title, target, frag, depth))
            walk(point, depth + 1)

    if nav_map is not None:
        walk(nav_map, 0)
    return out


def _nav_entries(nav_path: str, xml: bytes) -> list[TocEntry]:
    root = ElementTree.fromstring(xml)
    navs = [el for el in root.iter() if _local(el.tag) == "nav"]
    typed = [n for n in navs if any(_local(k) == "type" and "toc" in (v or "").split()
                                    for k, v in n.attrib.items())]
    nav = (typed or navs or [None])[0]
    out: list[TocEntry] = []

    def walk(node, depth: int) -> None:
        for child in node:
            name = _local(child.tag)
            if name == "li":
                link = next((el for el in child if _local(el.tag) == "a"), None)
                if link is not None and link.get("href"):
                    title = " ".join("".join(link.itertext()).split())
                    if title:
                        target, frag = _resolve(nav_path, link.get("href"))
                        out.append(TocEntry(title, target, frag, depth))
                walk(child, depth + 1)
            elif name == "ol" or name == "ul":
                walk(child, depth)

    if nav is not None:
        for child in nav:
            if _local(child.tag) in ("ol", "ul"):
                walk(child, 0)
    return out


def read_toc(path: Path) -> tuple[str, list[TocEntry]]:
    """(the OPF's folder inside the zip, the book's own table of contents
    flattened in document order). The EPUB3 nav document wins over the EPUB2
    NCX when both list entries. Anything unreadable is no TOC: the table of
    contents is optional, a parser falls back to headings without it."""
    try:
        with zipfile.ZipFile(path) as z:
            container = ElementTree.fromstring(z.read("META-INF/container.xml"))
            opf_path = next(el for el in container.iter()
                            if _local(el.tag) == "rootfile").get("full-path")
            opf_dir = posixpath.dirname(opf_path)
            opf = ElementTree.fromstring(z.read(opf_path))
            manifest = {}
            nav_href = None
            for el in opf.iter():
                if _local(el.tag) == "item" and el.get("href"):
                    href = posixpath.normpath(posixpath.join(opf_dir, unquote(el.get("href"))))
                    manifest[el.get("id")] = (href, el.get("media-type") or "")
                    if "nav" in (el.get("properties") or "").split():
                        nav_href = href
            spine = next((el for el in opf.iter() if _local(el.tag) == "spine"), None)
            ncx_href = None
            if spine is not None and spine.get("toc") in manifest:
                ncx_href = manifest[spine.get("toc")][0]
            if ncx_href is None:
                ncx_href = next((h for h, t in manifest.values()
                                 if t == "application/x-dtbncx+xml"), None)
            for href, reader in ((nav_href, _nav_entries), (ncx_href, _ncx_entries)):
                if href is None:
                    continue
                try:
                    entries = reader(href, z.read(href))
                except Exception:  # noqa: BLE001 -- try the other one
                    continue
                if entries:
                    return opf_dir, entries
            return opf_dir, []
    except Exception:  # noqa: BLE001 -- see docstring
        return "", []


# A table of contents replaces the headings only when it points at this many
# distinct places, and the headings found fewer than half as many chapters.
_MIN_TOC_CHAPTERS = 3


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def _paragraphs(text: str) -> list[str]:
    return [p for p in re.split(r"\n\s*\n", text) if p.strip()]


def _drop_first_paragraph(text: str) -> str:
    parts = re.split(r"(\n\s*\n)", text.strip(), maxsplit=1)
    return parts[2].strip() if len(parts) == 3 else ""


_BARE_DESIGNATION = re.compile(
    r"^(?:chapter|chap\.|part|book|section|letter|canto|volume|stave|act)\s+"
    r"(?:\d+|[ivxlcdm]+|[a-z]+(?:[-\s][a-z]+)?)\.?$", re.IGNORECASE)


def _tidy_toc_chapters(chapters: list[tuple[str | None, str]]) -> list[tuple[str | None, str]]:
    """Take what the TOC's labels and the page furniture put at the top of
    each chapter's text out of it, the way a heading is kept out of the body:

    - a running header: the same first paragraph opening at least three
      chapters and at least half of them (a book-title line repeated at every
      chapter start) is dropped from each;
    - a first paragraph that repeats the chapter's TOC label is dropped;
    - after a bare designation label ("Chapter 2"), a short next line (at
      most 8 words, no sentence punctuation at its end) is the chapter's
      subtitle: it joins the title ("Chapter 2: Strangers") and leaves the
      body.
    """
    titled = [i for i, (t, _) in enumerate(chapters) if t is not None]
    for _ in range(3):
        firsts: dict[str, int] = {}
        for i in titled:
            paras = _paragraphs(chapters[i][1])
            if paras:
                firsts[_norm(paras[0])] = firsts.get(_norm(paras[0]), 0) + 1
        header = next((p for p, n in sorted(firsts.items())
                       if n >= 3 and n * 2 >= len(titled)), None)
        if header is None:
            break
        for i in titled:
            title, text = chapters[i]
            paras = _paragraphs(text)
            if paras and _norm(paras[0]) == header:
                chapters[i] = (title, _drop_first_paragraph(text))
    out = []
    for title, text in chapters:
        if title is not None:
            paras = _paragraphs(text)
            if paras and _norm(paras[0]) == _norm(title):
                text = _drop_first_paragraph(text)
                paras = paras[1:]
            if (_BARE_DESIGNATION.match(title.strip()) and len(paras) > 1
                    and len(paras[0]) <= 80 and len(paras[0].split()) <= 8
                    and "\n" not in paras[0].strip()
                    and not re.search(r"[.!?\"”’]$", paras[0].strip())):
                title = f"{title.strip()}: {' '.join(paras[0].split())}"
                text = _drop_first_paragraph(text)
        out.append((title, text))
    return out


def _chapters_by_toc(docs: list, doc_paths: list[str],
                     toc: list[TocEntry]) -> list[tuple[str | None, str]] | None:
    """(TOC label or None for front matter, text) per chapter, split where
    the TOC's entries point, or None when the TOC is not usable (it resolves
    to fewer than _MIN_TOC_CHAPTERS distinct places in the spine).

    Entries are ordered by where they point (spine position, then position
    in the document), not by TOC order. Several entries pointing at one
    place (a part and its first chapter) make one chapter, named by the
    last of them, the most nested. Text before the first entry is front
    matter, one chapter as with headings; a spine document no entry points
    into continues the chapter before it.
    """
    index = {p: i for i, p in enumerate(doc_paths)}
    contents = [d.get_content().decode("utf-8", errors="replace") for d in docs]
    # position -> title; position = (spine index, anchor or None for the start)
    starts: dict[tuple[int, str | None], str] = {}
    for entry in toc:
        if entry.path not in index:
            continue
        i = index[entry.path]
        frag = entry.fragment
        if frag is not None and not re.search(
                r"""\b(?:id|name)\s*=\s*["']""" + re.escape(frag) + r"""["']""", contents[i]):
            frag = None  # an anchor that is not there points at the document
        starts[(i, frag)] = entry.title
    if len({k for k in starts}) < _MIN_TOC_CHAPTERS:
        return None
    chapters: list[tuple[str | None, list[str]]] = []
    for i, html in enumerate(contents):
        anchors = {a for (j, a) in starts if j == i and a is not None}
        extractor = _TextExtractor(anchors=anchors, split_on_headings=False)
        extractor.feed(html)
        for k, (anchor, text) in enumerate(extractor.anchored_sections):
            key = (i, anchor) if anchor is not None else ((i, None) if k == 0 else None)
            if key is not None and key in starts:
                chapters.append((starts[key], [text] if text else []))
            elif text:
                if not chapters:
                    chapters.append((None, []))
                chapters[-1][1].append(text)
    joined = [(title, "\n\n".join(parts)) for title, parts in chapters]
    return [(t, x) for t, x in _tidy_toc_chapters(joined) if x.strip()]


def _chapters_by_headings(docs: list) -> list[tuple[str | None, str]]:
    """(heading or None, text) per chapter, split at h1/h2/h3."""
    chapters: list[tuple[str | None, str]] = []
    for item in docs:
        extractor = _TextExtractor()
        extractor.feed(item.get_content().decode("utf-8", errors="replace"))
        for heading, text in extractor.sections:
            # A headingless section is either true front matter (no chapter
            # seen yet -> its own fallback chapter) or the tail of a chapter
            # whose spine document was split mid-chapter (e.g. at an
            # illustration) -> append it to the chapter still in progress.
            if heading is None and chapters:
                prev_title, prev_text = chapters[-1]
                chapters[-1] = (prev_title, f"{prev_text}\n\n{text}")
                continue
            chapters.append((heading, text))
    return chapters


def parse_epub(path: Path) -> ParsedBook:
    """Chapters split at the book's h1/h2/h3 headings, or where its own table
    of contents points (EPUB3 nav or EPUB2 NCX, src#fragment anchors
    included) when the headings find fewer than half as many chapters as a
    usable TOC: converted books often have no heading tags at all."""
    try:
        book = epub.read_epub(str(path), options={"ignore_ncx": True})
    except Exception as exc:
        raise IngestError(f"cannot read EPUB {path.name}: {exc}") from exc

    docs = [item for item in _spine_documents(book) if _is_chapter(item)]  # no nav docs
    raw = _chapters_by_headings(docs)
    headed = sum(1 for title, _ in raw if title is not None)
    opf_dir, toc = read_toc(path)
    if toc:
        doc_paths = [posixpath.normpath(posixpath.join(opf_dir, d.file_name)).lstrip("/")
                     for d in docs]
        by_toc = _chapters_by_toc(docs, doc_paths, toc)
        if by_toc is not None and headed * 2 < len(by_toc):
            raw = by_toc

    chapters: list[ParsedChapter] = []
    for seq, (title, text) in enumerate(raw, start=1):
        chapters.append(ParsedChapter(seq=seq, title=title or f"Chapter {seq}", text=text))
    if not chapters:
        raise IngestError(f"EPUB {path.name} contains no readable chapters")
    series, series_index = _series(path)
    return ParsedBook(
        title=_metadata(book, "title") or path.stem,
        author=_metadata(book, "creator"),
        chapters=chapters,
        series=series,
        series_index=series_index,
    )
