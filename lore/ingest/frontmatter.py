"""Detects Project Gutenberg front/back matter so it never reaches extraction.

This is deliberately conservative: it targets licence, credits and
transcription-notice boilerplate — the kind of text that produces junk
triplets like ``Pride and Prejudice -[POSSESSES]-> Project Gutenberg
License`` — not narrative prose that merely mentions a place or an
organisation in passing. When in doubt, a chunk is left alone.
"""
from __future__ import annotations

_MARKERS = (
    "project gutenberg",
    "gutenberg.org",
    "www.gutenberg",
    "start of the project gutenberg ebook",
    "end of the project gutenberg ebook",
    "distributed proofreading",
    "public domain",
    "creative commons",
    "all rights reserved",
)


def is_front_matter(text: str) -> bool:
    """True when `text` looks like Gutenberg boilerplate rather than prose."""
    normalised = text.casefold()
    return any(marker in normalised for marker in _MARKERS)
