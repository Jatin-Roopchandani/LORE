"""The head of the public pages in multi mode (release step 7).

`/`, `/privacy` and `/terms` are the one web-app page like every other, but
link previews and search engines read the HTML before any script runs, so the
server writes each one's title, description and Open Graph tags into the
head. A signed-in visitor is marked too, so the homepage offers "Open LORE"
from its first frame. Only <meta> and <title>: nothing a strict CSP refuses.
"""
from __future__ import annotations

import json
import re
from html import escape
from pathlib import Path

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
        f'<link rel="canonical" href="{attr(origin + path)}">',
    ]
    if path == "/":
        # What LORE is, for search engines. A data block, never run: the CSP's script-src does not apply.
        data = json.dumps(structured_data(origin), separators=(",", ":")).replace("<", "\\u003c")
        tags.append(f'<script type="application/ld+json">{data}</script>')
    return "".join(tags)


GITHUB = "https://github.com/Jatin-Roopchandani/LORE"


def structured_data(origin: str) -> dict:
    """schema.org's description of LORE (the homepage's JSON-LD)."""
    return {
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        "name": "LORE",
        "alternateName": "LORE Reader",
        "url": origin + "/",
        "description": PITCH,
        "applicationCategory": "LifestyleApplication",
        "operatingSystem": "Web",
        "image": origin + OG_IMAGE,
        "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
        "featureList": [
            "Answers questions about your own ebooks (EPUB or PDF) from the text",
            "Every sentence cites the passage it comes from, and is checked against it",
            "A knowledge graph of the characters, places and relationships in each book",
            "Spoiler-safe: answers stop at the chapter you have reached",
            "Deep, cited analyses of themes, characters and worlds across a series",
            "Bring your own AI key: Gemini, Claude or OpenAI",
            "An MCP server, so the library can be used from Claude",
        ],
        "sameAs": [GITHUB],
        "isAccessibleForFree": True,
    }


# --- pre-rendered pages (web/scripts/prerender.tsx) ------------------------------------

PRERENDERED = {"/": "home", "/privacy": "privacy", "/terms": "terms"}
_ROOT = re.compile(r'<div id="?root"?>\s*</div>')


def with_prerender(html: str, path: str, static: Path) -> str:
    """The page with its pre-rendered body inside #root and its chunk's CSS in the
    head, so a crawler that runs no script reads the page; unchanged when the
    build made none."""
    name = PRERENDERED.get(path)
    body = static / "prerender" / f"{name}.html" if name else None
    if body is None or not body.is_file():
        return html
    head = static / "prerender" / f"{name}.head.html"
    if head.is_file():
        html = html.replace("</head>", head.read_text(encoding="utf-8") + "</head>", 1)
    return _ROOT.sub(lambda _m: f'<div id="root">{body.read_text(encoding="utf-8")}</div>', html, count=1)


def robots_txt(origin: str) -> str:
    """Every crawler welcome on the public pages (AI search included); the API
    and sign-in machinery are not for indexing (the app and admin pages already
    send a signed-out visitor to sign in)."""
    origin = origin.rstrip("/")
    return ("User-agent: *\n"
            "Allow: /\n"
            "Disallow: /api/\n"
            "Disallow: /auth/\n"
            "Disallow: /oauth/\n"
            "Disallow: /mcp\n"
            f"\nSitemap: {origin}/sitemap.xml\n")


def sitemap_xml(origin: str) -> str:
    origin = origin.rstrip("/")
    urls = "".join(f"<url><loc>{escape(origin + p)}</loc></url>" for p in PAGES)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>\n')


def llms_txt(origin: str) -> str:
    """The plain summary AI tools read (llmstxt.org)."""
    origin = origin.rstrip("/")
    return f"""# LORE

> {PITCH}

LORE is an AI reader for the ebooks you own (EPUB or PDF). It turns each book into a
knowledge graph of its characters, places, groups and events, then answers questions
from the text itself: every sentence cites the passage it stands on, and a second pass
checks each one against that passage and cuts what the book doesn't say.

- Spoiler-safe: tell LORE which chapter you have reached, and answers, the graph and every
  character profile stop there.
- Questions across a whole series: how a character changes, how a world or magic system
  works, deep cited analyses with LORE's reading and the other side.
- Bring your own AI key (Gemini, Claude or OpenAI); each reader's library is private.
- An MCP server, so the library can be searched from Claude.
- Open source. The hosted version is in an invite-only beta.

## Links

- [Homepage]({origin}/): what LORE does and how it answers, with a worked example
- [Request an invite]({origin}/#beta)
- [Source code on GitHub]({GITHUB})
- [Privacy]({origin}/privacy)
- [Terms]({origin}/terms)
"""


def with_head(html: str, path: str, origin: str, *, signed_in: bool) -> str:
    """The page's HTML with its own title and tags in place of the shell's title."""
    tags = head_tags(path, origin) + (SIGNED_IN if signed_in else "")
    return _TITLE.sub("", html, count=1).replace("</head>", tags + "</head>", 1)
