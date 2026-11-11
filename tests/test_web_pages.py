"""Multi mode's routes and pages: the public front, the signed-in app, and the
user row that sign-in guarantees. Local mode serves `/` exactly as before."""
from urllib.parse import parse_qs, urlparse

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from lore.auth import accounts
from lore.auth.providers import Profile
from lore.central.models import Job, User
from lore.config import LoreConfig
from lore.tenancy import config_for
from lore.web import app as web_app
from tests.auth_fixtures import (
    BASE_URL, NOW, FakeMailer, FakeProvider, give_key, make_app, sign_in,
)
from tests.central_fixtures import central_sessions
from tests.test_worker import library

INDEX = "<!doctype html><html><head><title>LORE</title></head><body><div id=root></div></body></html>"
MARK = '<meta name="lore-multi" content="1">'


@pytest.fixture
def static(tmp_path, monkeypatch):
    out = tmp_path / "static"
    out.mkdir()
    (out / "index.html").write_text(INDEX)
    monkeypatch.setattr(web_app, "STATIC", out)
    return out


@pytest.fixture
def env(tmp_path, static):
    root = LoreConfig(data_dir=tmp_path / "root")
    sessions, _ = central_sessions(tmp_path, users=("alice",))
    provider = FakeProvider()
    app = make_app(root, sessions, mailer=FakeMailer(), providers={"google": provider})
    anon = TestClient(app, base_url=BASE_URL, follow_redirects=False)
    return app, sessions, anon, root, provider


def test_the_homepage_is_public_and_marks_the_page_as_multi_user(env):
    _, _, anon, *_ = env
    r = anon.get("/")
    assert r.status_code == 200 and MARK in r.text and "<div id=root>" in r.text
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize("path", ["/waitlist", "/privacy", "/terms", "/signin"])
def test_the_public_pages_are_served_to_anyone(env, path):
    _, _, anon, *_ = env
    r = anon.get(path)
    assert r.status_code == 200 and MARK in r.text


@pytest.mark.parametrize("path", ["/app", "/admin"])
def test_the_app_and_admin_need_a_session(env, path):
    app, sessions, anon, *_ = env
    r = anon.get(path)
    assert r.status_code == 303 and r.headers["location"] == "/signin"
    signed = sign_in(app, sessions, "alice")
    signed.follow_redirects = False
    r = signed.get(path)
    assert r.status_code == 200 and MARK in r.text


def test_an_expired_session_is_sent_to_sign_in(env):
    app, sessions, anon, *_ = env
    anon.cookies.set("lore_session", "not-a-session")
    assert anon.get("/app").headers["location"] == "/signin"


def test_a_signed_in_visitor_to_sign_in_goes_to_the_app(env):
    app, sessions, *_ = env
    signed = sign_in(app, sessions, "alice")
    signed.follow_redirects = False
    r = signed.get("/signin")
    assert r.status_code == 303 and r.headers["location"] == "/app"


def test_the_page_says_so_when_the_app_is_not_built(env, tmp_path, monkeypatch):
    _, _, anon, *_ = env
    monkeypatch.setattr(web_app, "STATIC", tmp_path / "nothing")
    assert "not built yet" in anon.get("/").text
    assert "not built yet" in anon.get("/privacy").text


def test_local_mode_serves_the_app_at_the_root_as_before(tmp_path, static, monkeypatch):
    monkeypatch.delenv("LORE_MULTI_USER", raising=False)
    client = TestClient(web_app.create_app(LoreConfig(data_dir=tmp_path / "d")),
                        base_url=BASE_URL, follow_redirects=False)
    r = client.get("/")
    assert r.status_code == 200 and "lore-multi" not in r.text and "<div id=root>" in r.text
    assert r.headers["cache-control"] == "no-cache"
    assert r.text == INDEX  # the file as built: no title, meta or marker added
    for path in ("/signin", "/admin", "/app", "/waitlist", "/auth/providers", "/api/me"):
        assert client.get(path).status_code == 404, path


# --- the public pages' head: title, description, Open Graph (release step 7) ---

SIGNED_IN = '<meta name="lore-signed-in" content="1">'
TITLES = {"/": "LORE · Ask your books", "/privacy": "Privacy · LORE", "/terms": "Terms · LORE"}


@pytest.mark.parametrize("path", ["/", "/privacy", "/terms"])
def test_the_public_pages_carry_their_title_description_and_open_graph_tags(env, path):
    _, _, anon, *_ = env
    html = anon.get(path).text
    title = TITLES[path]
    assert f"<title>{title}</title>" in html and html.count("<title>") == 1
    assert '<meta name="description" content="' in html
    assert f'<meta property="og:title" content="{title}">' in html
    assert '<meta property="og:description" content="' in html
    assert '<meta property="og:type" content="website">' in html
    assert f'<meta property="og:url" content="{BASE_URL}{path}">' in html
    assert f'<meta property="og:image" content="{BASE_URL}/og.png?v=2">' in html
    assert '<meta name="twitter:card" content="summary_large_image">' in html
    assert MARK in html and "<div id=root>" in html
    # Nothing a strict CSP (no inline scripts) would refuse: the homepage's one script
    # element is JSON-LD, a data block that never runs.
    assert html.count("<script") == (1 if path == "/" else 0)
    assert html.count('<script type="application/ld+json">') == (1 if path == "/" else 0)
    assert f'<link rel="canonical" href="{BASE_URL}{path}">' in html


def test_the_homepage_description_is_the_pitch(env):
    _, _, anon, *_ = env
    html = anon.get("/").text
    assert ('<meta name="description" content="Ask about the books you own. LORE answers from the text, '
            'cites every sentence, and lights up the people and links behind the answer.">') in html


@pytest.mark.parametrize("path", ["/", "/privacy", "/terms"])
def test_a_signed_in_visitor_is_marked_so_the_page_offers_to_open_lore(env, path):
    app, sessions, anon, *_ = env
    assert SIGNED_IN not in anon.get(path).text
    signed = sign_in(app, sessions, "alice")
    signed.follow_redirects = False
    r = signed.get(path)
    assert r.status_code == 200 and SIGNED_IN in r.text


@pytest.mark.parametrize("path", ["/waitlist", "/signin"])
def test_the_other_public_pages_keep_the_shell_title(env, path):
    _, _, anon, *_ = env
    html = anon.get(path).text
    assert "<title>LORE</title>" in html and "og:title" not in html and SIGNED_IN not in html


def test_the_homepage_describes_lore_as_structured_data(env):
    import json
    import re
    _, _, anon, *_ = env
    html = anon.get("/").text
    data = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', html).group(1))
    assert data["@type"] == "SoftwareApplication" and data["name"] == "LORE"
    assert data["url"] == f"{BASE_URL}/" and "github.com/Jatin-Roopchandani/LORE" in data["sameAs"][0]


# --- for search engines and AI crawlers ---

def test_robots_txt_welcomes_every_crawler_and_points_at_the_sitemap(env):
    _, _, anon, *_ = env
    r = anon.get("/robots.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    lines = r.text.splitlines()
    assert "User-agent: *" in lines and "Allow: /" in lines
    assert "Disallow: /api/" in lines and f"Sitemap: {BASE_URL}/sitemap.xml" in lines
    # a prefix rule: never one that would also hide /apple-touch-icon.png or the public pages
    assert not any(line.startswith("Disallow: /app") or line == "Disallow: /" for line in lines)


def test_the_sitemap_lists_the_public_pages(env):
    _, _, anon, *_ = env
    r = anon.get("/sitemap.xml")
    assert r.status_code == 200 and "xml" in r.headers["content-type"]
    for path in ("/", "/privacy", "/terms"):
        assert f"<loc>{BASE_URL}{path}</loc>" in r.text
    assert "/app" not in r.text and "/signin" not in r.text


def test_llms_txt_says_what_lore_is_with_its_links(env):
    _, _, anon, *_ = env
    r = anon.get("/llms.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert r.text.startswith("# LORE\n") and "Spoiler-safe" in r.text
    assert f"{BASE_URL}/privacy" in r.text and "github.com/Jatin-Roopchandani/LORE" in r.text


def prerender(static, name="home", body="<main><h1>Every story is a web.</h1></main>",
              head='<link rel="stylesheet" href="/assets/Chrome-x.css">'):
    (static / "prerender").mkdir(exist_ok=True)
    (static / "prerender" / f"{name}.html").write_text(body)
    (static / "prerender" / f"{name}.head.html").write_text(head)


@pytest.mark.parametrize("path,name", [("/", "home"), ("/privacy", "privacy"), ("/terms", "terms")])
def test_a_signed_out_visitor_gets_the_pre_rendered_page_and_its_stylesheet(env, static, path, name):
    _, _, anon, *_ = env
    prerender(static, name)
    html = anon.get(path).text
    assert '<div id="root"><main><h1>Every story is a web.</h1></main></div>' in html
    head = html.split("</head>")[0]
    assert '<link rel="stylesheet" href="/assets/Chrome-x.css">' in head


def test_a_signed_in_visitor_gets_the_empty_root_the_app_draws_itself(env, static):
    app, sessions, *_ = env
    prerender(static)
    signed = sign_in(app, sessions, "alice")
    signed.follow_redirects = False
    html = signed.get("/").text
    assert "Every story is a web." not in html and "<div id=root></div>" in html
    assert "Chrome-x.css" not in html


def test_without_a_pre_render_the_page_is_the_shell_as_before(env):
    _, _, anon, *_ = env
    assert "<div id=root></div>" in anon.get("/").text


def test_only_the_public_pages_are_pre_rendered(env, static):
    _, _, anon, *_ = env
    for name in ("home", "privacy", "terms"):
        prerender(static, name)
    for path in ("/waitlist", "/signin"):
        assert "Every story is a web." not in anon.get(path).text


def test_local_mode_serves_no_robots_or_sitemap_routes(tmp_path, static):
    client = TestClient(web_app.create_app(LoreConfig(data_dir=tmp_path / "local")))
    assert "Sitemap:" not in client.get("/robots.txt").text


def test_the_og_image_ships_with_the_web_app():
    from pathlib import Path
    png = Path(__file__).parent.parent / "web" / "public" / "og.png"
    data = png.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert int.from_bytes(data[16:20], "big") == 1200 and int.from_bytes(data[20:24], "big") == 630
    assert len(data) < 300_000


# --- every id the hook returns has a users row ---

def test_a_new_user_exists_centrally_and_can_queue_work(env):
    app, sessions, anon, root, provider = env
    with sessions() as db:
        code = accounts.create_invite(db, "alice", NOW)
        db.commit()
    provider.profile = Profile("g-new", "new@example.org", True, "New")
    r = anon.get(f"/auth/google/start?invite={code}")
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    assert anon.get("/auth/google/callback",
                    params={"code": "c", "state": state}).headers["location"] == "/app"
    uid = anon.get("/api/me").json()["user"]["id"]
    with sessions() as db:
        assert db.get(User, uid) is not None
    library(root, uid, "b1")  # their own library, as the worker tests build it
    token = anon.cookies["lore_csrf"]
    r = anon.post("/api/books/b1/continue", headers={"X-CSRF-Token": token})
    assert r.json()["error"]["code"] == "key_required"  # onboarding first
    give_key(sessions, uid)
    r = anon.post("/api/books/b1/continue", headers={"X-CSRF-Token": token})
    assert r.status_code == 202, r.text
    with sessions() as db:
        assert db.scalars(sa.select(Job)).one().user_id == uid


def test_a_session_whose_user_is_gone_reaches_no_library_and_makes_no_directory(env):
    app, sessions, anon, root, _ = env
    signed = sign_in(app, sessions, "alice")
    with sessions() as db:
        db.execute(sa.text("PRAGMA foreign_keys=OFF"))
        db.execute(sa.text("DELETE FROM users WHERE id = 'alice'"))
        db.commit()
    assert signed.get("/api/books").status_code == 401
    assert signed.post("/api/ask", json={"question": "q"}).status_code == 401
    assert not (root.data_dir / "users" / "alice").exists()


def test_signed_out_requests_make_no_directory(env):
    _, _, anon, root, _ = env
    for path in ("/api/books", "/api/library", "/api/overview"):
        assert anon.get(path).status_code == 401
    assert not (root.data_dir / "users").exists()


# --- the blog and FAQ (web/content, pre-rendered by the web build) ---

BLOG = {
    "posts": [{"slug": "no-spoilers", "title": "Ask without spoilers", "description": "How to ask safely.",
               "date": "2026-11-11"}],
    "faq": {"title": "LORE FAQ", "description": "Common questions.", "date": "2026-11-11",
            "items": [{"q": "What is LORE?", "a": "An AI reader for your ebooks."}]},
}


def built_blog(static):
    import json as _json
    (static / "prerender" / "blog").mkdir(parents=True, exist_ok=True)
    (static / "prerender" / "blog.json").write_text(_json.dumps(BLOG))
    prerender(static, "blog", body="<main><h1>Blog</h1></main>")
    prerender(static, "faq", body="<main><h1>LORE FAQ</h1></main>")
    prerender(static, "blog/no-spoilers", body="<main><h1>Ask without spoilers</h1></main>")


def json_ld(html):
    import json as _json
    import re
    return _json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', html).group(1))


def test_a_post_is_served_pre_rendered_with_its_title_canonical_and_blogposting(env, static):
    _, _, anon, *_ = env
    built_blog(static)
    r = anon.get("/blog/no-spoilers")
    assert r.status_code == 200
    html = r.text
    assert "<title>Ask without spoilers · LORE</title>" in html
    assert '<meta name="description" content="How to ask safely.">' in html
    assert '<meta property="og:type" content="article">' in html
    assert f'<link rel="canonical" href="{BASE_URL}/blog/no-spoilers">' in html
    assert '<div id="root"><main><h1>Ask without spoilers</h1></main></div>' in html
    data = json_ld(html)
    assert data["@type"] == "BlogPosting" and data["headline"] == "Ask without spoilers"
    assert data["datePublished"] == "2026-11-11" and data["url"] == f"{BASE_URL}/blog/no-spoilers"


def test_the_faq_carries_faqpage_structured_data(env, static):
    _, _, anon, *_ = env
    built_blog(static)
    html = anon.get("/faq").text
    assert "<title>LORE FAQ</title>" in html and "<h1>LORE FAQ</h1>" in html
    data = json_ld(html)
    assert data["@type"] == "FAQPage"
    assert data["mainEntity"] == [{"@type": "Question", "name": "What is LORE?",
                                   "acceptedAnswer": {"@type": "Answer",
                                                      "text": "An AI reader for your ebooks."}}]


def test_the_blog_index_is_served(env, static):
    _, _, anon, *_ = env
    built_blog(static)
    r = anon.get("/blog")
    assert r.status_code == 200 and "<title>Blog · LORE</title>" in r.text and "<h1>Blog</h1>" in r.text


@pytest.mark.parametrize("path", ["/blog/not-a-post", "/blog/NO-SPOILERS", "/blog/a.b"])
def test_an_unknown_post_is_a_404(env, static, path):
    _, _, anon, *_ = env
    built_blog(static)
    r = anon.get(path)
    assert r.status_code == 404 and "og:title" not in r.text and "<h1>" not in r.text


def test_without_a_built_blog_the_blog_and_faq_are_404s(env):
    _, _, anon, *_ = env
    assert anon.get("/blog").status_code == 404 and anon.get("/faq").status_code == 404


def test_the_sitemap_and_llms_txt_list_the_blog_and_faq(env, static):
    _, _, anon, *_ = env
    built_blog(static)
    sitemap = anon.get("/sitemap.xml").text
    for path in ("/blog", "/faq", "/blog/no-spoilers"):
        assert f"<loc>{BASE_URL}{path}</loc>" in sitemap
    assert f"<loc>{BASE_URL}/blog/no-spoilers</loc><lastmod>2026-11-11</lastmod>" in sitemap
    llms = anon.get("/llms.txt").text
    assert f"[Ask without spoilers]({BASE_URL}/blog/no-spoilers): How to ask safely." in llms
    assert f"[FAQ]({BASE_URL}/faq)" in llms


def test_a_signed_in_reader_gets_the_post_drawn_by_the_app(env, static):
    app, sessions, *_ = env
    built_blog(static)
    signed = sign_in(app, sessions, "alice")
    signed.follow_redirects = False
    html = signed.get("/blog/no-spoilers").text
    assert "<title>Ask without spoilers · LORE</title>" in html and "<h1>Ask without spoilers</h1>" not in html
