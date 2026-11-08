"""The head of the public pages in multi mode (release step 7).

`/`, `/privacy` and `/terms` are the one web-app page like every other, but
link previews and search engines read the HTML before any script runs, so the
server writes each one's title, description and Open Graph tags into the
head. A signed-in visitor is marked too, so the homepage offers "Open LORE"
from its first frame. Only <meta> and <title>: nothing a strict CSP refuses.
"""
from __future__ import annotations

import re
from html import escape

PITCH = ("Ask about the books you own. LORE answers from the text, cites every "
         "sentence, and lights up the people and links behind the answer.")

# path -> (title, description); keep the titles in step with web/src/messages.ts (HOME).
PAGES: dict[str, tuple[str, str]] = {
    "/": ("LORE · Ask your books", PITCH),
    "/privacy": ("Privacy · LORE",
                 "What LORE stores, where it goes, and how to delete it: in plain English."),
    "/terms": ("Terms · LORE",
               "The few rules of LORE's private beta: in plain English."),
}
SIGNED_IN = '<meta name="lore-signed-in" content="1">'
OG_IMAGE = "/og.png"  # web/public/og.png, 1200 x 630, drawn from the hero
_TITLE = re.compile(r"<title>.*?</title>", re.S)


def head_tags(path: str, origin: str) -> str:
    title, description = PAGES[path]
    attr = lambda v: escape(v, quote=True)  # noqa: E731
    origin = origin.rstrip("/")
    tags = [
        f"<title>{escape(title)}</title>",
        f'<meta name="description" content="{attr(description)}">',
        f'<meta property="og:title" content="{attr(title)}">',
        f'<meta property="og:description" content="{attr(description)}">',
        '<meta property="og:type" content="website">',
        '<meta property="og:site_name" content="LORE">',
        f'<meta property="og:url" content="{attr(origin + path)}">',
        f'<meta property="og:image" content="{attr(origin + OG_IMAGE)}">',
        '<meta property="og:image:width" content="1200">',
        '<meta property="og:image:height" content="630">',
        '<meta property="og:image:alt" content="A constellation of the people and places in the first '
        'two chapters of A Study in Scarlet, with LORE\'s name and pitch.">',
        '<meta name="twitter:card" content="summary_large_image">',
    ]
    return "".join(tags)


def with_head(html: str, path: str, origin: str, *, signed_in: bool) -> str:
    """The page's HTML with its own title and tags in place of the shell's title."""
    tags = head_tags(path, origin) + (SIGNED_IN if signed_in else "")
    return _TITLE.sub("", html, count=1).replace("</head>", tags + "</head>", 1)
