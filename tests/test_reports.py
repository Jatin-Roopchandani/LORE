"""Reporting an answer (release step 4): only your own request ids; the admin
list and the full trace are for admins only."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from lore import tracing
from lore.central import traces as T
from lore.config import LoreConfig
from lore.web.app import create_app
from tests.auth_fixtures import NOW, make_app, sign_in
from tests.central_fixtures import central_sessions


@pytest.fixture
def central(tmp_path):
    sessions, _ = central_sessions(tmp_path, users=("alice", "bob", "root"))
    from lore.central.models import User
    with sessions() as s:
        s.get(User, "root").role = "admin"
        s.commit()
    return sessions


@pytest.fixture
def app(tmp_path, central):
    return make_app(LoreConfig(data_dir=tmp_path / "root"), central)


def _trace(central, rid, user):
    t = tracing.Trace(request_id=rid)
    call = t.begin_call("synthesize", "the prompt")
    t.end_call(call, reply="the reply", model="flash")
    t.finish(answer={"answer": "Holmes met Watson."})
    with central() as s:
        T.save(s, t, user_id=user, question="Who met Watson?", now=NOW)
        s.commit()


def test_a_user_reports_their_own_answer(app, central):
    _trace(central, "r-alice", "alice")
    alice = sign_in(app, central, "alice")
    r = alice.post("/api/reports", json={"request_id": "r-alice", "note": "wrong book"})
    assert r.status_code == 201 and r.json()["report"]["status"] == "open"


def test_someone_elses_or_an_unknown_request_id_is_404(app, central):
    _trace(central, "r-alice", "alice")
    bob = sign_in(app, central, "bob")
    assert bob.post("/api/reports", json={"request_id": "r-alice"}).status_code == 404
    assert bob.post("/api/reports", json={"request_id": "nope"}).status_code == 404


def test_reporting_needs_csrf_and_a_short_note(app, central):
    _trace(central, "r-alice", "alice")
    alice = sign_in(app, central, "alice")
    assert alice.post("/api/reports", json={"request_id": "r-alice",
                                            "note": "x" * 5000}).status_code == 422
    del alice.headers["X-CSRF-Token"]
    assert alice.post("/api/reports", json={"request_id": "r-alice"}).status_code == 403


def test_admins_list_reports_and_open_the_full_trace(app, central):
    _trace(central, "r-alice", "alice")
    alice = sign_in(app, central, "alice")
    alice.post("/api/reports", json={"request_id": "r-alice", "note": "wrong"})
    admin = sign_in(app, central, "root")
    (report,) = admin.get("/api/admin/reports").json()["reports"]
    assert report["request_id"] == "r-alice" and report["note"] == "wrong"
    assert report["email"] == "alice@example.org" and report["question"] == "Who met Watson?"
    trace = admin.get("/api/admin/traces/r-alice").json()
    assert trace["calls"][0]["prompt"] == "the prompt" and trace["answer"]["answer"] == "Holmes met Watson."
    assert admin.get("/api/admin/traces/nope").status_code == 404
    r = admin.post(f"/api/admin/reports/{report['id']}/resolve")
    assert r.status_code == 200 and r.json()["report"]["status"] == "resolved"


def test_non_admins_get_403(app, central):
    _trace(central, "r-alice", "alice")
    alice = sign_in(app, central, "alice")
    assert alice.get("/api/admin/reports").status_code == 403
    assert alice.get("/api/admin/traces/r-alice").status_code == 403
    assert alice.post("/api/admin/reports/1/resolve").status_code == 403
    anon = TestClient(app, base_url=alice.base_url)
    assert anon.get("/api/admin/reports").status_code == 401


def test_local_mode_reports_without_sign_in(tmp_path, monkeypatch):
    monkeypatch.delenv("LORE_DATABASE_URL", raising=False)
    monkeypatch.delenv("LORE_MULTI_USER", raising=False)
    client = TestClient(create_app(LoreConfig(data_dir=tmp_path)), base_url="http://127.0.0.1:8766")
    assert client.post("/api/reports", json={"request_id": "nope"}).status_code == 404
    from lore.central.db import make_engine, make_session_factory
    sessions = make_session_factory(make_engine(f"sqlite:///{tmp_path / 'central.db'}"))
    _trace(sessions, "r-local", "local")
    assert client.post("/api/reports", json={"request_id": "r-local", "note": "hm"}).status_code == 201
