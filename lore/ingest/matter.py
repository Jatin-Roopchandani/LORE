"""Which sections of a book are the story, and what each story section is called.

An ebook's raw sections (EPUB headings, PDF heading pages) include more than
the story: a licence header, a title page, a table of contents, a preface, a
list of illustrations, an index, the licence again. They are numbered like
chapters (`chapter_seq`), so without this they are extracted, beaten and
counted as chapters ("chapter 7 of 19" for a book's Chapter III).

`classify` labels each section `front`, `story` or `back`. It is
deliberately conservative: hiding a real chapter is worse than keeping a
stray title page, so a section is non-story only on a clear signal:

- **boilerplate**: a licence or copyright notice. Project Gutenberg's
  wording (`is_gutenberg_boilerplate`) or a publisher's ("all rights
  reserved", ISBN) in the title, or in paragraphs making up at least half of
  the text (a last chapter with the end-of-ebook banner glued on is story);
- **contents**: a table of contents or list of plates. Its title says so, or
  its text is a dense list of chapter headings (four or more "Chapter IV" /
  "Part 2" designations, under 200 characters of text per designation);
- **front/back titles**: a section titled only "Preface", "Foreword",
  "Introduction", "Dedication", "Copyright", ... before the story, or "Index",
  "Notes", "Glossary", "Afterword", "About the Author", ... after it. A title
  that merely contains such a word ("The Introduction of Mr. Collins") is not
  one;
- **short opening sections**: under `SHORT_CHARS` of text before the first
  story section (a title page, a "by ..." line, a part's title page), unless
  its heading is a chapter or prologue.

Front matter is the leading run of such sections, back matter the trailing
run. In between only boilerplate and contents count (an omnibus's next
contents page, say), and are labelled front matter of what follows. If
nothing would be left as story, every section that is not boilerplate is.

`heading_label` turns a section's heading into a short name for the reading
position ("CHAPTER III. \\nTHE LAURISTON GARDENS MYSTERY" -> "The Lauriston
Gardens Mystery"; "Part II" stays "Part II"); parsers' made-up titles
("Chapter 7" for a headingless section, "Pages 1-15") give None.

Pure functions only, for any book: nothing here knows a title or an author.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from lore.resolve.entityhood import is_gutenberg_boilerplate

FRONT, STORY, BACK = "front", "story", "back"
MATTERS = (FRONT, STORY, BACK)

# Text shorter than this before the first story section is a title page or
# similar (Gutenberg's "PART I." page with its one-line subtitle is 102).
SHORT_CHARS = 500
# At least this share of the text (by characters) is licence wording.
_BOILERPLATE_SHARE = 0.5
# A contents page: this many heading designations, this dense.
_MIN_TOC_REFS = 4
_MAX_CHARS_PER_REF = 200

_COPYRIGHT_MARKERS = (
    "all rights reserved",
    "isbn",
    "copyright ©",
    "copyright (c)",
    "this ebook is for the use of anyone anywhere",
    "no part of this publication may be reproduced",
    "no part of this book may be reproduced",
)

_NUMBER_WORDS = (
    "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
    "fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|"
    "fifty|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|last|final"
)
_NUMERAL = rf"(?:\d+|[ivxlcdm]+|(?:{_NUMBER_WORDS})(?:[-\s](?:{_NUMBER_WORDS}))?)"
_DESIGNATION_WORDS = r"chapter|chap\.|part|book|section|letter|canto|volume|vol\.|stave|act"
# "CHAPTER III. THE LAURISTON ...", "CHAPTERXXVII.", "Part 2: The River".
_DESIGNATION = re.compile(
    rf"^\s*({_DESIGNATION_WORDS})\s*({_NUMERAL})\b\s*[.:\-—–]?\s*(.*)$", re.IGNORECASE)
_STANDALONE = re.compile(r"^\s*(prologue|epilogue|interlude)\b\s*[.:\-—–]?\s*(.*)$",
                         re.IGNORECASE)
# The same designations anywhere in running text, for contents density.
_DESIGNATION_REF = re.compile(
    rf"\b(?:{_DESIGNATION_WORDS})\s*(?:\d+|[ivxlcdm]+)\b", re.IGNORECASE)
# A heading whose shortness never makes it front matter.
_CHAPTER_WORDS = {"chapter", "chap.", "letter", "stave", "canto", "prologue"}

_FRONT_TITLES = {
    "contents", "table of contents", "list of illustrations", "illustrations",
    "list of plates", "list of figures", "list of maps", "copyright",
    "copyright page", "dedication", "title page", "half title", "frontispiece",
    "preface", "foreword", "introduction", "prefatory note", "authors note",
    "author s note", "note on the text", "a note on the text", "translators note",
    "translator s note", "translators preface", "translator s preface",
    "editors note", "editor s note", "editors preface", "editor s preface",
    "transcribers note", "transcriber s note", "transcribers notes",
    "transcriber s notes", "acknowledgments", "acknowledgements", "also by",
    "by the same author", "praise", "about this ebook", "about the book",
}
_BACK_TITLES = {
    "index", "glossary", "notes", "endnotes", "footnotes", "bibliography",
    "references", "further reading", "afterword", "acknowledgments",
    "acknowledgements", "about the author", "about the authors", "also by",
    "by the same author", "colophon", "copyright", "transcribers note",
    "transcriber s note", "transcribers notes", "transcriber s notes",
    "reading group guide", "discussion questions", "credits", "permissions",
}
_TOC_TITLES = {"contents", "table of contents", "list of illustrations",
               "illustrations", "list of plates", "list of figures", "list of maps"}

_ROMAN = re.compile(r"m{0,4}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})")

_SMALL_WORDS = {"a", "an", "and", "as", "at", "but", "by", "for", "from", "in",
                "into", "nor", "of", "on", "or", "the", "to", "upon", "with"}


@dataclass(frozen=True)
class Section:
    """One raw section of a book: its `chapter_seq`, heading and text."""

    seq: int
    title: str | None
    text: str


@dataclass(frozen=True)
class Classified:
    seq: int
    matter: str  # FRONT, STORY or BACK
    reason: str  # why it is not story ("" for story)
    heading: str | None  # heading_label of its title


# --- headings ------------------------------------------------------------------------


def _lines(title: str | None) -> list[str]:
    return [" ".join(l.split()) for l in (title or "").splitlines() if l.strip()]


def _key(title: str | None) -> str:
    """A title as a lookup key: letters only, lower-case, no leading "the"."""
    words = re.findall(r"[a-z]+", " ".join(_lines(title)).casefold())
    if words and words[0] == "the":
        words = words[1:]
    return " ".join(words)


def _designation(title: str | None) -> tuple[str, str, str] | None:
    """(word, numeral, rest) of the last heading line that is a designation
    ("CHAPTER III."), with the lines after it joined to `rest`."""
    lines = _lines(title)
    for i in range(len(lines) - 1, -1, -1):
        m = _DESIGNATION.match(lines[i])
        if m:
            rest = " ".join([m.group(3), *lines[i + 1:]]).strip()
            return m.group(1), m.group(2), rest
        m = _STANDALONE.match(lines[i])
        if m:
            return m.group(1), "", " ".join([m.group(2), *lines[i + 1:]]).strip()
    return None


def _word_case(word: str, first: bool) -> str:
    if re.fullmatch(r"[A-Za-z](?:\.[A-Za-z])+\.?", word):  # M.D., U.S.
        return word.upper()
    lower = word.lower()
    if not first and lower in _SMALL_WORDS:
        return lower
    if len(lower) > 1 and _ROMAN.fullmatch(lower):  # roman numerals
        return word.upper()
    return lower[:1].upper() + lower[1:]


def _tidy(text: str) -> str:
    text = " ".join(text.split()).strip(" :;,-—–")
    while text.endswith(".") and not re.search(r"(?:^|\s)[A-Za-z](?:\.[A-Za-z])+\.$", text):
        text = text[:-1].rstrip(" :;,-—–")
    text = text.lstrip(".")
    if not text:
        return ""
    letters = [c for c in text if c.isalpha()]
    if letters and all(c.isupper() for c in letters):
        words = text.split(" ")
        text = " ".join(_word_case(w, i == 0) for i, w in enumerate(words))
    return text


def heading_label(title: str | None, seq: int | None = None) -> str | None:
    """A short name for a section's heading, or None when it has none worth
    showing: "CHAPTER III. THE LAURISTON GARDENS MYSTERY" -> "The Lauriston
    Gardens Mystery", "CHAPTER XIII" -> None (its number says it all),
    "PART II." -> "Part II", "Prologue" -> "Prologue". A parser's made-up title
    ("Chapter 7" for section 7, "Pages 1-15") is None."""
    lines = _lines(title)
    if not lines:
        return None
    joined = " ".join(lines)
    if seq is not None and joined == f"Chapter {seq}":
        return None
    if re.fullmatch(r"Pages \d+-\d+", joined):
        return None
    found = _designation(title)
    if found is None:
        return _tidy(joined) or None
    word, numeral, rest = found
    if word.casefold() in ("chapter", "chap."):
        return _tidy(rest) or None
    if word.casefold() in ("prologue", "epilogue", "interlude"):
        return _tidy(f"{word} {rest}" if rest else word)
    head = _tidy(f"{word} {numeral}")
    return f"{head}: {_tidy(rest)}" if _tidy(rest) else head


# --- classification ------------------------------------------------------------------


def _pieces(text: str) -> list[str]:
    """Paragraphs, or sentences when the text has no paragraph breaks (chunk
    text, which the chunker flattened)."""
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paragraphs) > 1:
        return paragraphs
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _notice(piece: str) -> bool:
    lowered = piece.casefold()
    return is_gutenberg_boilerplate(piece) or any(m in lowered for m in _COPYRIGHT_MARKERS)


def is_boilerplate(title: str | None, text: str) -> bool:
    """A licence or copyright notice (see the module docstring)."""
    head = " ".join(_lines(title)).casefold()
    if "gutenberg" in head or re.search(r"\blicen[cs]e\b", head):
        return True
    body = text.strip()
    if not body:
        return False
    pieces = _pieces(body)
    flagged = sum(len(p) for p in pieces if _notice(p))
    return flagged >= _BOILERPLATE_SHARE * sum(len(p) for p in pieces)


def is_contents(title: str | None, text: str) -> bool:
    """A table of contents or list of plates (see the module docstring)."""
    if _key(title) in _TOC_TITLES:
        return True
    refs = len(_DESIGNATION_REF.findall(text))
    return refs >= _MIN_TOC_REFS and len(text.strip()) <= refs * _MAX_CHARS_PER_REF


def _short(section: Section) -> bool:
    if len(section.text.strip()) >= SHORT_CHARS:
        return False
    found = _designation(section.title)
    return not (found and found[0].casefold() in _CHAPTER_WORDS)


def _front_reason(section: Section) -> str:
    if is_boilerplate(section.title, section.text):
        return "boilerplate"
    if is_contents(section.title, section.text):
        return "contents"
    if _key(section.title) in _FRONT_TITLES:
        return "title"
    if _short(section):
        return "short"
    return ""


def _back_reason(section: Section) -> str:
    if is_boilerplate(section.title, section.text):
        return "boilerplate"
    if is_contents(section.title, section.text):
        return "contents"
    if _key(section.title) in _BACK_TITLES:
        return "title"
    return ""


def classify(sections: Iterable[Section]) -> list[Classified]:
    """Each section, in the order given, as front matter, story or back
    matter (see the module docstring)."""
    sections = list(sections)
    n = len(sections)
    matter = [STORY] * n
    reason = [""] * n
    start = 0
    while start < n:
        why = _front_reason(sections[start])
        if not why:
            break
        matter[start], reason[start] = FRONT, why
        start += 1
    end = n - 1
    while end >= start:
        why = _back_reason(sections[end])
        if not why:
            break
        matter[end], reason[end] = BACK, why
        end -= 1
    for i in range(start, end + 1):
        s = sections[i]
        if is_boilerplate(s.title, s.text):
            matter[i], reason[i] = FRONT, "boilerplate"
        elif is_contents(s.title, s.text):
            matter[i], reason[i] = FRONT, "contents"
    if STORY not in matter:
        # Never a book with no story: keep everything but licence text.
        for i, s in enumerate(sections):
            if not is_boilerplate(s.title, s.text):
                matter[i], reason[i] = STORY, ""
        first = matter.index(STORY) if STORY in matter else n
        for i in range(first, n):
            if matter[i] != STORY:
                matter[i] = BACK
    return [Classified(s.seq, matter[i], reason[i], heading_label(s.title, s.seq))
            for i, s in enumerate(sections)]


def classify_book(book) -> list[Classified]:
    """`classify` over a ParsedBook's chapters."""
    return classify(Section(c.seq, c.title, c.text) for c in book.chapters)


def story_numbers(classified: Sequence[Classified]) -> dict[int, int]:
    """chapter_seq -> the story chapter's number, from 1, story sections only."""
    out: dict[int, int] = {}
    for c in sorted(classified, key=lambda c: c.seq):
        if c.matter == STORY:
            out[c.seq] = len(out) + 1
    return out
