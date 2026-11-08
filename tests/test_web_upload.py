"""POST /api/books/upload (release step 5): a book uploaded in the browser.

Every guard is tested with a file built here. Fake stages, a fake clock, no
Gemini and no network."""
from __future__ import annotations

import os
import stat
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pymupdf
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from lore.central.models import Job
from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.ingest.upload_check import Limits
from lore.orchestrator import jobs
from lore.tenancy import config_for
from lore.web import limits
from lore.web.app import create_app
from lore.web.upload import UploadSettings, display_name
from tests.auth_fixtures import BASE_URL, give_key, make_app, sign_in
from tests.central_fixtures import central_sessions
from tests.fixtures import make_epub, make_pdf

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
URL = "/api/books/upload"


class FakeStage:
    def __init__(self, ran: list, name: str = "chunk") -> None:
        self.name, self._ran = name, ran

    def total(self, ctx):
        return 1

    def run(self, ctx, job_ctl):
        job_ctl.advance(1)
        self._ran.append((str(ctx.config.data_dir), ctx.book_id))


def _story(tag: str) -> str:
    return "<p>" + " ".join(f"{tag} sentence {i} with words in it." for i in range(60)) + "</p>"


def epub_bytes(tmp_path: Path, title: str = "The Upload") -> bytes:
    path = make_epub(tmp_path / f"{title}.epub", title, "An Author",
                     [("One", _story(title)), ("Two", _story(title + "2"))])
    return path.read_bytes()


def pdf_bytes(tmp_path: Path, tag: str = "pdf") -> bytes:
    path = make_pdf(tmp_path / f"{tag}.pdf", [f"{tag} words " * 120, f"{tag} more " * 120])
    return path.read_bytes()


def send(client, data: bytes, name: str = "book.epub", world: str | None = None, **kw):
    form = {"world": world} if world is not None else None
    return client.post(URL, files={"file": (name, data, "application/octet-stream")},
                       data=form, **kw)


def books_of(cfg: LoreConfig) -> list[dict]:
    conn = open_db(cfg.sqlite_path)
    try:
        return [dict(zip(("id", "title", "file_path", "source", "world"), r)) for r in conn.execute(
            "SELECT b.id, b.title, b.file_path, b.source, w.name FROM books b"
            " LEFT JOIN worlds w ON w.id = b.world_id ORDER BY b.created_at")]
    finally:
        conn.close()


def inbox(cfg: LoreConfig) -> list[str]:
    return sorted(p.name for p in cfg.inbox_dir.iterdir()) if cfg.inbox_dir.exists() else []


def wait_job(cfg: LoreConfig, book: str, states=(jobs.DONE, jobs.FAILED)):
    for _ in range(300):
        conn = open_db(cfg.sqlite_path)
        try:
            job = jobs.get_job(conn, book)
        finally:
            conn.close()
        if job is not None and job.state in states:
            return job
        time.sleep(0.02)
    raise AssertionError("the job never finished")


# --- local mode ---------------------------------------------------------------

@pytest.fixture
def local(tmp_path):
    cfg = LoreConfig(data_dir=tmp_path / "lib")
    ran: list = []
    app = create_app(cfg, stages_factory=lambda c: [FakeStage(ran)], clock=lambda: NOW,
                     upload_settings=UploadSettings(checks=Limits(max_pages=50)))
    return TestClient(app, base_url="http://127.0.0.1:8766"), cfg, ran


def test_an_epub_upload_is_registered_and_processed_in_process(local, tmp_path):
    client, cfg, ran = local
    r = send(client, epub_bytes(tmp_path), "My Book.epub", world="Uploads")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["title"] == "The Upload" and body["filename"] == "My Book.epub"
    assert body["world"]["name"] == "Uploads"
    assert body["processing"] == {"state": "processing"}
    (book,) = books_of(cfg)
    assert book["id"] == body["book_id"] and book["source"] == "upload"
    assert book["world"] == "Uploads"
    stored = Path(book["file_path"])
    assert stored.parent == cfg.inbox_dir and stored.suffix == ".epub"
    assert stored.stem != "My Book" and len(stored.stem) == 32      # a server-made name
    assert inbox(cfg) == [stored.name]                               # no .part left
    assert wait_job(cfg, body["book_id"]).state == jobs.DONE
    assert ran == [(str(cfg.data_dir), body["book_id"])]
    listed = client.get("/api/books").json()["books"]
    assert [b["id"] for b in listed] == [body["book_id"]]


def test_a_pdf_upload_is_registered(local, tmp_path):
    client, cfg, _ = local
    r = send(client, pdf_bytes(tmp_path), "scan.pdf")
    assert r.status_code == 202, r.text
    (book,) = books_of(cfg)
    assert book["file_path"].endswith(".pdf")
    assert r.json()["title"] == "scan"  # no title in the file: the upload's name


def test_the_book_id_is_the_content_hash(local, tmp_path):
    import hashlib
    client, cfg, _ = local
    data = epub_bytes(tmp_path)
    r = send(client, data)
    assert r.json()["book_id"] == hashlib.sha256(data).hexdigest()[:16]


def test_an_existing_world_by_id_and_the_suggestion(local, tmp_path):
    client, cfg, _ = local
    w = client.post("/api/worlds", json={"name": "Shelf"}).json()
    r = send(client, epub_bytes(tmp_path, "A"), world=str(w["id"]))
    assert r.json()["world"] == {"id": w["id"], "name": "Shelf"}
    r = send(client, epub_bytes(tmp_path, "B"))                 # no world: the suggestion
    assert r.status_code == 202 and r.json()["world"]["name"]
    r = send(client, epub_bytes(tmp_path, "C"), world="999")    # an unknown id
    assert r.status_code == 400 and r.json()["error"]["code"] == "unknown_world"
    r = send(client, epub_bytes(tmp_path, "D"), world="²")    # a digit int() refuses
    assert r.status_code == 202 and r.json()["world"]["name"] == "²"


def test_a_refused_book_leaves_no_new_world_behind(local, tmp_path):
    client, cfg, _ = local
    r = send(client, b"PK\x03\x04 not really", world="Brand New")
    assert r.status_code == 400
    assert [w["name"] for w in client.get("/api/worlds").json()] == []


def test_a_duplicate_is_409_already_added_with_its_id(local, tmp_path):
    client, cfg, _ = local
    data = epub_bytes(tmp_path)
    first = send(client, data).json()
    before = inbox(cfg)
    r = send(client, data, "again.epub")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "already_added"
    assert r.json()["book_id"] == first["book_id"]
    assert inbox(cfg) == before and len(books_of(cfg)) == 1


@pytest.mark.parametrize("name", ["book.txt", "book.epub.exe", "book", "book.docx"])
def test_a_wrong_extension_is_not_a_book(local, tmp_path, name):
    client, cfg, _ = local
    r = send(client, epub_bytes(tmp_path), name)
    assert r.status_code == 400 and r.json()["error"]["code"] == "not_a_book"
    assert books_of(cfg) == [] and inbox(cfg) == []


def test_wrong_magic_bytes_are_not_a_book(local, tmp_path):
    client, cfg, _ = local
    for data, name in ((pdf_bytes(tmp_path), "book.epub"),     # a PDF named .epub
                       (epub_bytes(tmp_path), "book.pdf"),     # an EPUB named .pdf
                       (b"<html>hello</html>" * 50, "book.epub"),
                       (b"", "book.pdf")):
        r = client.post(URL, files={"file": (name, data, "application/epub+zip")})
        assert r.status_code == 400 and r.json()["error"]["code"] == "not_a_book", name
    assert books_of(cfg) == [] and inbox(cfg) == []


def test_the_content_type_header_of_the_part_decides_nothing(local, tmp_path):
    client, cfg, _ = local
    r = client.post(URL, files={"file": ("x.pdf", b"%PDF-1.7 junk", "application/pdf")})
    assert r.status_code == 400 and r.json()["error"]["code"] == "not_a_book"


def test_too_large_by_streamed_file_bytes(tmp_path):
    cfg = LoreConfig(data_dir=tmp_path / "lib")
    app = create_app(cfg, stages_factory=lambda c: [], clock=lambda: NOW,
                     upload_settings=UploadSettings(max_file_bytes=10_000))
    client = TestClient(app, base_url="http://127.0.0.1:8766")
    r = send(client, b"PK\x03\x04" + b"x" * 20_000)
    assert r.status_code == 413 and r.json()["error"]["code"] == "too_large"
    assert inbox(cfg) == [] and books_of(cfg) == []


def test_too_large_by_content_length(local, tmp_path, monkeypatch):
    client, cfg, _ = local
    monkeypatch.setattr(limits, "UPLOAD_BYTES", 500)
    r = send(client, epub_bytes(tmp_path))
    assert "content-length" in {k.lower() for k in r.request.headers}
    assert r.status_code == 413 and r.json()["error"]["code"] == "too_large"
    assert inbox(cfg) == []


def _multipart(boundary: str, parts: list[tuple[str, str | None, bytes]]) -> bytes:
    out = b""
    for name, filename, data in parts:
        disp = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
        out += (f"--{boundary}\r\nContent-Disposition: {disp}\r\n\r\n").encode() + data + b"\r\n"
    return out + f"--{boundary}--\r\n".encode()


def test_too_large_by_streamed_body_without_content_length(local, tmp_path, monkeypatch):
    client, cfg, _ = local
    monkeypatch.setattr(limits, "UPLOAD_BYTES", 50_000)
    body = _multipart("XyZ", [("file", "b.epub", b"PK\x03\x04" + b"y" * 200_000)])

    def chunks():
        for i in range(0, len(body), 8192):
            yield body[i:i + 8192]
    r = client.post(URL, content=chunks(),
                    headers={"Content-Type": "multipart/form-data; boundary=XyZ"})
    assert "content-length" not in {k.lower() for k in r.request.headers}
    assert r.status_code == 413 and r.json()["error"]["code"] == "too_large"
    assert inbox(cfg) == []


def test_the_real_route_limit_is_fifty_megabytes():
    assert limits.limit_for("POST", URL) == limits.UPLOAD_BYTES
    assert limits.UPLOAD_FILE_BYTES == 50 * 1024 * 1024
    assert limits.limit_for("POST", "/api/worlds") == limits.DEFAULT_BYTES
    assert limits.limit_for("POST", "/api/books/b1/continue") == limits.DEFAULT_BYTES


@pytest.mark.parametrize("parts", [
    [("file", "a.epub", b"PK\x03\x04"), ("file", "b.epub", b"PK\x03\x04")],   # two files
    [("file", "a.epub", b"PK\x03\x04"), ("other", None, b"x")],              # a stray field
    [("world", None, b"w")],                                                # no file
    [("file", None, b"PK\x03\x04")],                                        # no filename
    [("world", "w.txt", b"w"), ("file", "a.epub", b"PK\x03\x04")],          # world as a file
    [("file", "a.epub", b"PK\x03\x04"), ("world", None, b"w" * 5000)],       # a huge world
])
def test_a_malformed_form_is_bad_upload(local, parts):
    client, cfg, _ = local
    r = client.post(URL, content=_multipart("B0und", parts),
                    headers={"Content-Type": "multipart/form-data; boundary=B0und"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_upload"
    assert inbox(cfg) == []


def test_not_multipart_is_bad_upload(local):
    client, cfg, _ = local
    r = client.post(URL, content=b"PK\x03\x04", headers={"Content-Type": "application/epub+zip"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_upload"
    r = client.post(URL, content=b"--x\r\n", headers={"Content-Type": "multipart/form-data; boundary=x"})
    assert r.status_code == 400 and inbox(cfg) == []


def _zip(path: Path, entries: list, *, bomb_member: bool = False) -> bytes:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", b"<container/>")
        for name, data in entries:
            z.writestr(name, data, zipfile.ZIP_DEFLATED)
    return path.read_bytes()


def test_a_zip_bomb_by_ratio(local, tmp_path):
    client, cfg, _ = local
    data = _zip(tmp_path / "bomb.epub", [("OEBPS/a.xhtml", b"\0" * (30 * 1024 * 1024))])
    assert len(data) < 1024 * 1024
    r = send(client, data)
    assert r.status_code == 400 and r.json()["error"]["code"] == "unsafe_file"
    assert inbox(cfg) == [] and books_of(cfg) == []


def test_a_zip_bomb_by_entry_count_and_total(tmp_path):
    cfg = LoreConfig(data_dir=tmp_path / "lib")
    app = create_app(cfg, stages_factory=lambda c: [], clock=lambda: NOW,
                     upload_settings=UploadSettings(checks=Limits(max_entries=20,
                                                                  max_uncompressed=500_000)))
    client = TestClient(app, base_url="http://127.0.0.1:8766")
    many = _zip(tmp_path / "many.epub", [(f"OEBPS/{i}.x", b"x") for i in range(30)])
    assert send(client, many).json()["error"]["code"] == "unsafe_file"
    big = _zip(tmp_path / "big.epub", [(f"OEBPS/{i}.bin", os.urandom(200_000)) for i in range(3)])
    assert send(client, big).json()["error"]["code"] == "unsafe_file"
    assert inbox(cfg) == []


@pytest.mark.parametrize("entry", ["../../escape.xhtml", "/abs/escape.xhtml", "C:/x.xhtml"])
def test_path_traversal_and_absolute_entries(local, tmp_path, entry):
    client, cfg, _ = local
    r = send(client, _zip(tmp_path / "t.epub", [(entry, b"<html/>")]))
    assert r.status_code == 400 and r.json()["error"]["code"] == "unsafe_file"
    assert not (tmp_path / "escape.xhtml").exists() and not Path("/abs").exists()
    assert inbox(cfg) == []


def test_a_symlink_entry(local, tmp_path):
    client, cfg, _ = local
    info = zipfile.ZipInfo("OEBPS/link")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    r = send(client, _zip(tmp_path / "s.epub", [(info, b"/etc/passwd")]))
    assert r.status_code == 400 and r.json()["error"]["code"] == "unsafe_file"


def test_an_encrypted_epub(local, tmp_path):
    client, cfg, _ = local
    r = send(client, _zip(tmp_path / "e.epub", [("META-INF/rights.xml", b"<rights/>")]))
    assert r.status_code == 400 and r.json()["error"]["code"] == "encrypted"


def test_an_encrypted_pdf(local, tmp_path):
    client, cfg, _ = local
    p = tmp_path / "e.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "secret " * 80)
    doc.save(str(p), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")
    doc.close()
    r = send(client, p.read_bytes(), "e.pdf")
    assert r.status_code == 400 and r.json()["error"]["code"] == "encrypted"
    assert "password" in r.json()["error"]["message"]
    assert inbox(cfg) == []


def test_too_many_pages(local, tmp_path):
    client, cfg, _ = local
    data = make_pdf(tmp_path / "long.pdf", ["page text " * 30] * 51).read_bytes()
    r = send(client, data, "long.pdf")
    assert r.status_code == 400 and r.json()["error"]["code"] == "too_many_pages"


def test_an_unreadable_book_is_not_a_book(local, tmp_path):
    client, cfg, _ = local
    p = make_pdf(tmp_path / "blank.pdf", [""])  # no text layer
    r = send(client, p.read_bytes(), "blank.pdf")
    assert r.status_code == 400 and r.json()["error"]["code"] == "not_a_book"
    assert str(cfg.inbox_dir) not in r.json()["error"]["message"]
    assert inbox(cfg) == [] and books_of(cfg) == []


def test_the_uploaded_name_never_builds_a_path(local, tmp_path):
    client, cfg, _ = local
    r = send(client, epub_bytes(tmp_path), "../../../outside.epub")
    assert r.status_code == 202
    assert r.json()["filename"] == "outside.epub"
    assert not (tmp_path / "outside.epub").exists()
    assert all(n.endswith(".epub") and len(n) == 37 for n in inbox(cfg))


def test_display_names_are_sanitised():
    assert display_name("C:\\Users\\me\\Book.epub") == "Book.epub"
    assert display_name("a/b/../c.pdf") == "c.pdf"
    assert display_name("evil\u202etxt.epub") == "eviltxt.epub"        # bidi override
    assert display_name("tab\there\n.epub") == "tabhere.epub"
    assert display_name("   ") == "book" and display_name(None) == "book"
    assert len(display_name("x" * 500 + ".epub")) == 200


def test_a_spent_budget_registers_the_book_and_waits(local, tmp_path, monkeypatch):
    client, cfg, ran = local
    monkeypatch.setattr("lore.web.app.processing_budget_left", lambda conn, c, day: 0)
    r = send(client, epub_bytes(tmp_path))
    assert r.status_code == 202
    proc = r.json()["processing"]
    assert proc["state"] == "waiting" and proc["resumable_at"]
    job = client.get("/api/books").json()["books"][0]["job"]
    assert job["state"] == "waiting_quota" and job["resumable_at"] == proc["resumable_at"]
    assert job["can_continue"] is False
    time.sleep(0.1)
    assert ran == []  # nothing started


def test_local_busy_runner_still_registers(tmp_path):
    import threading
    gate = threading.Event()

    class Slow(FakeStage):
        def run(self, ctx, job_ctl):
            gate.wait(10)

    cfg = LoreConfig(data_dir=tmp_path / "lib")
    app = create_app(cfg, stages_factory=lambda c: [Slow([])], clock=lambda: NOW)
    client = TestClient(app, base_url="http://127.0.0.1:8766")
    try:
        assert send(client, epub_bytes(tmp_path, "A")).json()["processing"]["state"] == "processing"
        r = send(client, epub_bytes(tmp_path, "B"))
        assert r.status_code == 202 and r.json()["processing"] == {"state": "busy"}
        assert len(books_of(cfg)) == 2
    finally:
        gate.set()


def test_local_mode_has_no_quotas(tmp_path):
    cfg = LoreConfig(data_dir=tmp_path / "lib")
    app = create_app(cfg, stages_factory=lambda c: [], clock=lambda: NOW,
                     upload_settings=UploadSettings(per_day=0, max_books=0, per_hour=0))
    client = TestClient(app, base_url="http://127.0.0.1:8766")
    assert send(client, epub_bytes(tmp_path)).status_code == 202


# --- multi mode ---------------------------------------------------------------

@pytest.fixture
def multi(tmp_path):
    base = LoreConfig(data_dir=tmp_path / "root")
    sessions, _ = central_sessions(tmp_path, users=("alice", "bob", "carol"))
    for user in ("alice", "bob"):
        give_key(sessions, user)
    calls: list = []

    def stages(cfg):  # processing must never run in the web process
        calls.append(cfg.data_dir)
        return []

    def build(**settings):
        app = make_app(base, sessions, stages_factory=stages, clock=lambda: NOW,
                       upload_settings=UploadSettings(**settings))
        return app
    return base, sessions, calls, build


def central_jobs(sessions):
    with sessions() as s:
        return s.scalars(sa.select(Job).order_by(Job.id)).all()


def test_multi_mode_enqueues_and_returns_202(multi, tmp_path):
    base, sessions, calls, build = multi
    alice = sign_in(build(), sessions, "alice")
    r = send(alice, epub_bytes(tmp_path))
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["processing"]["state"] == "queued"
    (job,) = central_jobs(sessions)
    assert (job.id, job.user_id, job.book_id, job.kind, job.state) == (
        body["processing"]["job"], "alice", body["book_id"], "process", "queued")
    assert calls == []
    acfg = config_for(base, "alice")
    assert [b["id"] for b in books_of(acfg)] == [body["book_id"]]
    assert Path(books_of(acfg)[0]["file_path"]).parent == acfg.inbox_dir


def test_signed_out_is_401_and_nothing_is_stored(multi, tmp_path):
    base, sessions, _, build = multi
    anon = TestClient(build(), base_url=BASE_URL)
    r = send(anon, epub_bytes(tmp_path))
    assert r.status_code == 401
    assert not (base.data_dir / "users").exists() or not any(
        (base.data_dir / "users").rglob("*.epub"))


def test_a_missing_csrf_token_is_403(multi, tmp_path):
    base, sessions, _, build = multi
    alice = sign_in(build(), sessions, "alice")
    del alice.headers["X-CSRF-Token"]
    r = send(alice, epub_bytes(tmp_path))
    assert r.status_code == 403
    assert books_of(config_for(base, "alice")) == []
    alice.headers["X-CSRF-Token"] = "forged"
    assert send(alice, epub_bytes(tmp_path)).status_code == 403


def test_uploads_are_per_user(multi, tmp_path):
    base, sessions, _, build = multi
    app = build()
    alice, bob = sign_in(app, sessions, "alice"), sign_in(app, sessions, "bob")
    data = epub_bytes(tmp_path)
    book = send(alice, data).json()["book_id"]
    assert [b["id"] for b in alice.get("/api/books").json()["books"]] == [book]
    assert bob.get("/api/books").json()["books"] == []
    assert inbox(config_for(base, "bob")) == []
    # the same file is new to bob: his own copy, in his own library
    r = send(bob, data)
    assert r.status_code == 202 and r.json()["book_id"] == book
    assert Path(books_of(config_for(base, "bob"))[0]["file_path"]).is_relative_to(
        config_for(base, "bob").data_dir)


def test_a_user_without_a_key_is_told_first(multi, tmp_path):
    base, sessions, _, build = multi
    carol = sign_in(build(), sessions, "carol")
    r = send(carol, epub_bytes(tmp_path))
    assert r.status_code == 409 and r.json()["error"]["code"] == "key_required"
    assert inbox(config_for(base, "carol")) == []


def test_daily_upload_quota(multi, tmp_path):
    base, sessions, _, build = multi
    alice = sign_in(build(per_day=2), sessions, "alice")
    assert send(alice, epub_bytes(tmp_path, "A")).status_code == 202
    assert send(alice, epub_bytes(tmp_path, "B")).status_code == 202
    r = send(alice, epub_bytes(tmp_path, "C"))
    assert r.status_code == 429 and r.json()["error"]["code"] == "daily_limit"
    assert len(books_of(config_for(base, "alice"))) == 2
    # a refused upload (not a book) does not count toward the day
    bob = sign_in(build(per_day=1), sessions, "bob")
    send(bob, b"junk", "x.epub")
    assert send(bob, epub_bytes(tmp_path, "D")).status_code == 202


def test_library_size_quota(multi, tmp_path):
    base, sessions, _, build = multi
    alice = sign_in(build(max_books=1), sessions, "alice")
    assert send(alice, epub_bytes(tmp_path, "A")).status_code == 202
    r = send(alice, epub_bytes(tmp_path, "B"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "too_many_books"


def test_upload_attempts_are_rate_limited(multi, tmp_path):
    base, sessions, _, build = multi
    alice = sign_in(build(per_hour=2), sessions, "alice")
    send(alice, b"junk", "x.epub")
    send(alice, b"junk", "x.epub")
    r = send(alice, epub_bytes(tmp_path))
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"


def test_multi_mode_spent_budget_waits_and_queues_nothing(multi, tmp_path, monkeypatch):
    base, sessions, _, build = multi
    monkeypatch.setattr("lore.web.app.processing_budget_left", lambda conn, c, day: 0)
    alice = sign_in(build(), sessions, "alice")
    r = send(alice, epub_bytes(tmp_path))
    assert r.status_code == 202 and r.json()["processing"]["state"] == "waiting"
    assert central_jobs(sessions) == []
    job = alice.get("/api/books").json()["books"][0]["job"]
    assert job["state"] == "waiting_quota" and job["can_continue"] is False


def test_upload_settings_from_env():
    s = UploadSettings.from_env({"LORE_UPLOADS_PER_DAY": "3", "LORE_MAX_BOOKS": "7",
                                 "LORE_UPLOADS_PER_HOUR": "9"})
    assert (s.per_day, s.max_books, s.per_hour) == (3, 7, 9)
    d = UploadSettings.from_env({})
    assert (d.per_day, d.max_books, d.per_hour, d.max_file_bytes) == (
        10, 30, 30, 50 * 1024 * 1024)
    with pytest.raises(ValueError):
        UploadSettings.from_env({"LORE_MAX_BOOKS": "lots"})
    with pytest.raises(ValueError):
        UploadSettings.from_env({"LORE_UPLOADS_PER_DAY": "-1"})


def test_a_lying_content_length_on_the_upload_route_is_still_counted():
    """The streamed route is not buffered, but the bytes are still counted."""
    import asyncio
    got = []

    async def app(scope, receive, send):
        try:
            while True:
                message = await receive()
                got.append(len(message.get("body", b"")))
                if not message.get("more_body"):
                    break
        except limits.TooLarge:
            got.append("too large")

    messages = [{"type": "http.request", "body": b"x" * 600, "more_body": True},
                {"type": "http.request", "body": b"x" * 600, "more_body": False}]

    async def receive():
        return messages.pop(0)

    scope = {"type": "http", "method": "POST", "path": URL,
             "headers": [(b"content-length", b"10")]}
    old = limits.UPLOAD_BYTES
    limits.UPLOAD_BYTES = 1000
    try:
        asyncio.run(limits.BodyLimit(app)(scope, receive, lambda m: None))
    finally:
        limits.UPLOAD_BYTES = old
    assert got == [600, "too large"]  # passed through as it came, then stopped


def test_partial_uploads_left_by_a_crash_are_swept(local, tmp_path):
    client, cfg, _ = local
    cfg.inbox_dir.mkdir(parents=True)
    old, fresh = cfg.inbox_dir / ".upload-old.part", cfg.inbox_dir / ".upload-new.part"
    old.write_bytes(b"x")
    fresh.write_bytes(b"x")
    hours_ago = time.time() - 2 * 3600
    os.utime(old, (hours_ago, hours_ago))
    send(client, b"junk", "x.epub")
    assert not old.exists() and fresh.exists()


def test_the_worker_then_processes_the_uploaded_book_in_its_users_library(multi, tmp_path):
    from lore.worker import Worker
    base, sessions, _, build = multi
    alice = sign_in(build(), sessions, "alice")
    book = send(alice, epub_bytes(tmp_path)).json()["book_id"]
    ran: list = []
    Worker(base, sessions, worker_id="w1", make_stages=lambda cfg: [FakeStage(ran)],
           clock=lambda: NOW).run_once()
    assert ran == [(str(config_for(base, "alice").data_dir), book)]
    (shown,) = alice.get("/api/books").json()["books"]
    assert shown["job"]["state"] == "done"
