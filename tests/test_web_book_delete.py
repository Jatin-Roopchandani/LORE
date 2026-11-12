"""DELETE /api/books/{id}: a reader deletes a book from their own library.

Multi mode on test_sample's three-book library copied into alice's
directory; local mode on a small store triple. No model, no network.
"""
from __future__ import annotations

import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lore import library_lock
from lore.central import queue
from lore.central.models import Job
from lore.codex import build
from lore.config import LoreConfig
from lore.db.kuzu_store import KuzuStore
from lore.db.sqlite import open_db
from lore.orchestrator import jobs
from lore.tenancy import config_for
from lore.web.app import create_app
from tests.auth_fixtures import ACT_AS as HEADER, ActAsClient, give_key, make_app, sign_in
from tests.central_fixtures import central_sessions
from tests.mcp_fixtures import build_store_triple
from tests.test_sample import ONE, THREE, TWO, _built, _refresh  # noqa: F401
from tests.test_web_books import FakeStage, _wait_done

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _low_threshold(monkeypatch):
    monkeypatch.setattr(build, "MIN_MENTION_CHUNKS", 1)


@pytest.fixture
def root(_built: Path, tmp_path: Path, monkeypatch) -> LoreConfig:
    monkeypatch.setenv("LORE_SAMPLE_LIBRARY", "0")
    root = LoreConfig(data_dir=tmp_path / "root")
    data = root.data_dir / "users" / "alice"
    shutil.copytree(_built, data)
    conn = open_db(data / "lore.db")
    for book, path in conn.execute("SELECT id, file_path FROM books").fetchall():
        conn.execute("UPDATE books SET file_path = ? WHERE id = ?",
                     (str(data / "inbox" / Path(path).name), book))
    conn.commit()
    conn.close()
    return root


@pytest.fixture
def central(tmp_path):
    sessions, _ = central_sessions(tmp_path, users=("alice", "bob"))
    for user in ("alice", "bob"):
        give_key(sessions, user)
    return sessions


@pytest.fixture
def app(root, central):
    return make_app(root, central, clock=lambda: NOW, codex_refresh_factory=_refresh)


@pytest.fixture
def client(app, central):
    return ActAsClient(app, central)


def _books(client, user="alice"):
    r = client.get("/api/books", headers={HEADER: user})
    assert r.status_code == 200, r.text
    return r.json()["books"]


def _alice(root) -> LoreConfig:
    return config_for(root, "alice")


def test_a_reader_deletes_a_book(client, root) -> None:
    r = client.delete(f"/api/books/{TWO}", headers={HEADER: "alice"})
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": True, "book": {"id": TWO, "title": f"title-{TWO}"},
                        "world": "World A", "world_removed": False}
    assert sorted(b["id"] for b in _books(client)) == sorted([ONE, THREE])
    kuzu = KuzuStore(_alice(root).kuzu_dir / "graph", read_only=True)
    try:
        books = {r[0]: r[1] for r in kuzu.execute("MATCH (e:Entity) RETURN e.name, e.books")}
    finally:
        kuzu.close()
    assert books["Maren Vex"] == [ONE] and "Zed" not in books
    again = client.delete(f"/api/books/{TWO}", headers={HEADER: "alice"})
    assert again.status_code == 404 and again.json()["error"]["code"] == "not_found"


def test_the_last_book_of_a_world_takes_the_world(client) -> None:
    r = client.delete(f"/api/books/{THREE}", headers={HEADER: "alice"})
    assert r.status_code == 200 and r.json()["world_removed"] is True
    worlds = client.get("/api/worlds", headers={HEADER: "alice"}).json()
    assert [w["name"] for w in worlds] == ["World A"]


def test_an_unknown_book_is_404(client) -> None:
    r = client.delete("/api/books/0000000000000000", headers={HEADER: "alice"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_only_the_readers_own_library(client, root) -> None:
    # bob's library is his own (empty, seeding off): alice's book is unknown there
    r = client.delete(f"/api/books/{TWO}", headers={HEADER: "bob"})
    assert r.status_code == 404
    assert TWO in {b["id"] for b in _books(client)}


def test_a_queued_book_is_409_and_listed_as_processing(client, central) -> None:
    with central() as session:
        queue.enqueue(session, "alice", TWO, "process")
    listed = {b["id"]: b["processing"] for b in _books(client)}
    assert listed == {ONE: False, TWO: True, THREE: False}
    r = client.delete(f"/api/books/{TWO}", headers={HEADER: "alice"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "processing"
    assert TWO in {b["id"] for b in _books(client)}


def test_a_running_book_is_409(client, root) -> None:
    conn = open_db(_alice(root).sqlite_path)
    jobs.claim_job(conn, TWO, now=NOW)
    conn.close()
    assert {b["id"]: b["processing"] for b in _books(client)}[TWO] is True
    r = client.delete(f"/api/books/{TWO}", headers={HEADER: "alice"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "processing"


def test_a_paused_job_is_closed_with_the_book(client, central) -> None:
    with central() as session:
        job, _ = queue.enqueue(session, "alice", TWO, "process")
        session.query(Job).filter(Job.id == job.id).update({"state": "paused"})
        session.commit()
        job_id = job.id
    r = client.delete(f"/api/books/{TWO}", headers={HEADER: "alice"})
    assert r.status_code == 200, r.text
    with central() as session:
        closed = session.get(Job, job_id)
        assert closed.state == "failed" and closed.outcome == "book_deleted"


def test_an_upload_in_progress_is_409_busy(client, root) -> None:
    with library_lock.hold(_alice(root).data_dir, exclusive=False):
        r = client.delete(f"/api/books/{TWO}", headers={HEADER: "alice"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "busy"


def test_an_upload_waits_for_no_delete(client, root) -> None:
    with library_lock.hold(_alice(root).data_dir, exclusive=True):
        r = client.post("/api/books/upload", headers={HEADER: "alice"},
                        files={"file": ("book.epub", b"PK\x03\x04", "application/octet-stream")})
    assert r.status_code == 409 and r.json()["error"]["code"] == "busy"


def test_deleting_needs_the_csrf_token(app, central) -> None:
    signed_in = sign_in(app, central, "alice")
    r = signed_in.delete(f"/api/books/{TWO}", headers={"X-CSRF-Token": "forged"})
    assert r.status_code == 403
    assert TWO in {b["id"] for b in _books(ActAsClient(app, central))}


# --- local mode ---------------------------------------------------------------

@pytest.fixture
def local_config(tmp_path):
    return build_store_triple(
        tmp_path, books=[("b1", "A Study in Scarlet"), ("b2", "The Sign of Four")],
        chunks=[("c1", "b1", 1), ("c2", "b2", 1)])


def _local(config, stages):
    app = create_app(config, stages_factory=lambda cfg: stages, clock=lambda: NOW,
                     codex_refresh_factory=_refresh)
    return TestClient(app, base_url="http://127.0.0.1:8766")


def test_local_mode_deletes_without_a_csrf_token(local_config) -> None:
    client = _local(local_config, [])
    r = client.delete("/api/books/b2")
    assert r.status_code == 200, r.text
    assert [b["id"] for b in client.get("/api/books").json()["books"]] == ["b1"]
    assert client.delete("/api/books/b2").status_code == 404


def test_local_mode_refuses_while_a_book_is_processed(local_config) -> None:
    gate = threading.Event()
    client = _local(local_config, [FakeStage(gate=gate)])
    try:
        assert client.post("/api/books/b1/continue").status_code == 202
        running = client.delete("/api/books/b1")
        assert running.status_code == 409 and running.json()["error"]["code"] == "processing"
        other = client.delete("/api/books/b2")
        assert other.status_code == 409 and other.json()["error"]["code"] == "busy"
        assert client.delete("/api/books/nope").status_code == 404
    finally:
        gate.set()
    _wait_done(local_config)
    assert client.delete("/api/books/b2").status_code == 200
