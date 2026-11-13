"""The sample book on a multi-user server: a new account's library starts as
a copy of the template; an existing empty library can add it from the
Library panel (POST /api/library/sample); nothing else is ever seeded."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from lore import sample
from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.tenancy import config_for
from tests.auth_fixtures import ACT_AS as HEADER, ActAsClient, give_key, make_app, sign_in
from tests.central_fixtures import central_sessions
from tests.test_sample import ONE, _built, _made  # noqa: F401 - module fixtures

SAMPLE_TITLE = f"title-{ONE}"


@pytest.fixture
def root(_made: Path, tmp_path: Path, monkeypatch) -> LoreConfig:
    monkeypatch.delenv(sample.ENV_SAMPLE, raising=False)
    shutil.copytree(_made, tmp_path / "root" / "sample")
    return LoreConfig(data_dir=tmp_path / "root")


@pytest.fixture
def central(tmp_path):
    sessions, _ = central_sessions(tmp_path, users=("alice", "bob", "carol"))
    for user in ("alice", "bob", "carol"):
        give_key(sessions, user)
    return sessions


def _empty(root: LoreConfig, user: str) -> Path:
    """An existing library with no books (made before the sample existed)."""
    data = config_for(root, user).data_dir
    open_db(data / "lore.db").close()
    return data


@pytest.fixture
def app(root, central):
    return make_app(root, central)


@pytest.fixture
def client(app, central):
    return ActAsClient(app, central)


def books(client, user):
    r = client.get("/api/books", headers={HEADER: user})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_new_account_starts_with_the_sample_book(client, root) -> None:
    out = books(client, "carol")
    assert [(b["id"], b["title"], b["world"], b["status"]) for b in out["books"]] == [
        (ONE, SAMPLE_TITLE, "Sherlock Holmes", "graphed")]
    assert out["sample"] is False  # it has a book now
    data = root.data_dir / "users" / "carol"
    conn = open_db(data / "lore.db")
    assert conn.execute("SELECT file_path FROM books").fetchone()[0] == str(
        data / "inbox" / f"{ONE}-stored.epub")
    conn.close()
    graph = client.get("/api/worlds", headers={HEADER: "carol"})
    assert graph.status_code == 200


def test_an_existing_library_is_never_seeded(client, root) -> None:
    _empty(root, "alice")
    out = books(client, "alice")
    assert out["books"] == [] and out["sample"] is True


def test_seeding_off_leaves_new_libraries_empty(client, monkeypatch) -> None:
    monkeypatch.setenv(sample.ENV_SAMPLE, "0")
    out = books(client, "carol")
    assert out["books"] == [] and out["sample"] is False


def test_no_template_no_seed_no_offer(central, tmp_path) -> None:
    root = LoreConfig(data_dir=tmp_path / "bare")
    client = ActAsClient(make_app(root, central), central)
    out = books(client, "carol")
    assert out["books"] == [] and out["sample"] is False
    r = client.post("/api/library/sample", headers={HEADER: "carol"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_an_empty_library_adds_the_sample_once(client, root) -> None:
    _empty(root, "alice")
    r = client.post("/api/library/sample", headers={HEADER: "alice"})
    assert r.status_code == 201, r.text
    assert r.json() == {"added": True, "book": {
        "id": ONE, "title": SAMPLE_TITLE, "world": "Sherlock Holmes"}}
    out = books(client, "alice")
    assert [b["id"] for b in out["books"]] == [ONE] and out["sample"] is False
    again = client.post("/api/library/sample", headers={HEADER: "alice"})
    assert again.status_code == 409 and again.json()["error"]["code"] == "not_empty"
    # bob's library is his own: untouched
    assert books(client, "bob")["books"][0]["id"] == ONE  # bob is new: seeded


def test_adding_is_refused_while_an_upload_is_in_progress(client, root) -> None:
    data = _empty(root, "alice")
    (data / "inbox").mkdir(exist_ok=True)
    (data / "inbox" / ".upload-1.part").write_bytes(b"x")
    r = client.post("/api/library/sample", headers={HEADER: "alice"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "busy"


def test_adding_needs_the_csrf_token(app, central, root) -> None:
    _empty(root, "alice")
    signed_in = sign_in(app, central, "alice")
    r = signed_in.post("/api/library/sample", headers={"X-CSRF-Token": "forged"})
    assert r.status_code == 403
    assert books(ActAsClient(app, central), "alice")["books"] == []


def test_local_mode_is_never_seeded_and_offers_nothing(root, tmp_path) -> None:
    from fastapi.testclient import TestClient

    from lore.web.app import create_app

    # A local library whose data root happens to hold a template.
    client = TestClient(create_app(root), base_url="http://127.0.0.1:8766")
    r = client.get("/api/books")
    assert r.status_code == 200
    assert r.json()["books"] == [] and r.json()["sample"] is False
    assert client.post("/api/library/sample").status_code == 404
    assert not (root.data_dir / "users").exists()


def test_the_sample_book_is_marked_as_the_sample(client, root) -> None:
    out = books(client, "carol")
    assert [(b["id"], b["is_sample"]) for b in out["books"]] == [(ONE, True)]


def test_without_a_template_no_book_is_marked_sample(client, root) -> None:
    books(client, "carol")  # seeded while the template exists
    shutil.rmtree(root.data_dir / "sample")
    assert [b["is_sample"] for b in books(client, "carol")["books"]] == [False]


def test_the_template_ids_are_read_once_per_version(root, monkeypatch) -> None:
    calls = []
    real = sample.sqlite3.connect
    monkeypatch.setattr(sample.sqlite3, "connect", lambda *a, **k: calls.append(a) or real(*a, **k))
    sample._IDS.clear()
    assert sample.book_ids(root.data_dir) == frozenset({ONE})
    assert sample.book_ids(root.data_dir) == frozenset({ONE})
    assert len(calls) == 1
    assert sample.book_ids(root.data_dir / "nowhere") == frozenset()
