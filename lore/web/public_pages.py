"""The head of the public pages in multi mode (release step 7).

`/`, `/privacy`, `/terms`, the blog (`/blog`, `/blog/<slug>`) and `/faq` are
the one web-app page like every other, but link previews and search engines
read the HTML before any script runs, so the server writes each one's title,
description and Open Graph tags into the head, with JSON-LD (a data block,
never run, so the CSP is unchanged). A signed-in visitor is marked too, so the
homepage offers "Open LORE" from its first frame.

The blog's pages are whatever the web build pre-rendered: web/content's
Markdown, listed in static/prerender/blog.json (web/scripts/prerender.tsx).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
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
# Keep in step with web/src/messages.ts (BLOG).
BLOG_TITLE = "Blog · LORE"
BLOG_LEDE = ("Notes on reading with LORE: asking about books without spoilers, using your "
             "library from Claude, and how it works.")
AUTHOR = "Jatin Roopchandani"
SIGNED_IN = '<meta name="lore-signed-in" content="1">'
# web/public/og.png, 1200 x 630, photographed from the hero (web/scripts/og-image.mjs). Bump the
# version whenever the image changes: link previews (LinkedIn, X, Slack) cache it by its address.
OG_IMAGE = "/og.png?v=2"
_TITLE = re.compile(r"<title>.*?</title>", re.S)


@dataclass(frozen=True)
class Page:
    """A public page: its head, and the file the build pre-rendered it to."""

    title: str
    description: str
    name: str            # under static/prerender/
    kind: str = "page"   # page | blog | faq | post
    date: str | None = None
    headline: str = ""   # a post's own title (`title` adds " · LORE")


_SLUG = re.compile(r"/blog/([a-z0-9-]+)")
_BUILT_IN = {"/": "home", "/privacy": "privacy", "/terms": "terms"}


def blog_index(static: Path) -> dict | None:
    """The build's list of posts and the FAQ; None when it made none."""
    try:
        data = json.loads((static / "prerender" / "blog.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def page_for(path: str, blog: dict | None = None) -> Page | None:
    """The public page at `path`, or None (a blog page only when the build made it)."""
    if path in PAGES:
        title, description = PAGES[path]
        return Page(title, description, _BUILT_IN[path])
    if blog is None:
        return None
    if path == "/blog":
        return Page(BLOG_TITLE, BLOG_LEDE, "blog", kind="blog")
    if path == "/faq" and blog.get("faq"):
        faq = blog["faq"]
        return Page(faq["title"], faq["description"], "faq", kind="faq", date=faq.get("date"))
    m = _SLUG.fullmatch(path)
    post = next((p for p in blog.get("posts", []) if m and p.get("slug") == m.group(1)), None)
    if post is None:
        return None
    return Page(f"{post['title']} · LORE", post["description"], f"blog/{post['slug']}",
                kind="post", date=post.get("date"), headline=post["title"])


def head_tags(path: str, origin: str, blog: dict | None = None) -> str:
    page = page_for(path, blog)
    title, description = page.title, page.description
    attr = lambda v: escape(v, quote=True)  # noqa: E731
    origin = origin.rstrip("/")
    tags = [
        f"<title>{escape(title)}</title>",
        f'<meta name="description" content="{attr(description)}">',
        f'<meta property="og:title" content="{attr(title)}">',
        f'<meta property="og:description" content="{attr(description)}">',
        f'<meta property="og:type" content="{"article" if page.kind == "post" else "website"}">',
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
    if page.kind == "post" and page.date:
        tags.append(f'<meta property="article:published_time" content="{attr(page.date)}">')
    data = (structured_data(origin) if path == "/"
            else post_data(origin, path, page) if page.kind == "post"
            else faq_data(blog["faq"]) if page.kind == "faq" else None)
    if data is not None:
        # For search engines. A data block, never run: the CSP's script-src does not apply.
        text = json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("<", "\\u003c")
        tags.append(f'<script type="application/ld+json">{text}</script>')
    return "".join(tags)


def post_data(origin: str, path: str, page: Page) -> dict:
    """A post's schema.org BlogPosting."""
    return {
        "@context": "https://schema.org",
        "@type": "BlogPosting",
        "headline": page.headline,
        "description": page.description,
        "datePublished": page.date,
        "dateModified": page.date,
        "author": {"@type": "Person", "name": AUTHOR, "url": GITHUB.rsplit("/", 1)[0]},
        "publisher": {"@type": "Organization", "name": "LORE", "url": origin + "/",
                      "logo": {"@type": "ImageObject", "url": origin + "/icon-512.png"}},
        "image": origin + OG_IMAGE,
        "mainEntityOfPage": origin + path,
        "url": origin + path,
    }


def faq_data(faq: dict) -> dict:
    """The FAQ's schema.org FAQPage: each question and its plain answer."""
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [{"@type": "Question", "name": item["q"],
                        "acceptedAnswer": {"@type": "Answer", "text": item["a"]}}
                       for item in faq.get("items", [])],
    }


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

_ROOT = re.compile(r'<div id="?root"?>\s*</div>')


def with_prerender(html: str, path: str, static: Path, blog: dict | None = None) -> str:
    """The page with its pre-rendered body inside #root and its chunk's CSS in the
    head, so a crawler that runs no script reads the page; unchanged when the
    build made none."""
    page = page_for(path, blog)
    name = page.name if page else None
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


def public_paths(blog: dict | None) -> list[str]:
    """Every public page, the blog's included."""
    paths = list(PAGES)
    if blog is not None:
        paths += ["/blog", *(["/faq"] if blog.get("faq") else []),
                  *(f"/blog/{p['slug']}" for p in blog.get("posts", []))]
    return paths


def sitemap_xml(origin: str, blog: dict | None = None) -> str:
    origin = origin.rstrip("/")

    def url(path: str) -> str:
        page = page_for(path, blog)
        when = f"<lastmod>{escape(page.date)}</lastmod>" if page and page.date else ""
        return f"<url><loc>{escape(origin + path)}</loc>{when}</url>"

    urls = "".join(url(p) for p in public_paths(blog))
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>\n')


def llms_txt(origin: str, blog: dict | None = None) -> str:
    """The plain summary AI tools read (llmstxt.org)."""
    origin = origin.rstrip("/")
    writing = ""
    if blog is not None:
        lines = [f"- [{p['title']}]({origin}/blog/{p['slug']}): {p['description']}"
                 for p in blog.get("posts", [])]
        if blog.get("faq"):
            lines.append(f"- [FAQ]({origin}/faq): {blog['faq']['description']}")
        writing = "\n## Guides and posts\n\n" + "\n".join(lines) + "\n" if lines else ""
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
{writing}"""


def with_head(html: str, path: str, origin: str, *, signed_in: bool,
              blog: dict | None = None) -> str:
    """The page's HTML with its own title and tags in place of the shell's title."""
    tags = head_tags(path, origin, blog) + (SIGNED_IN if signed_in else "")
    return _TITLE.sub("", html, count=1).replace("</head>", tags + "</head>", 1)
