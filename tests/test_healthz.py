"""GET /healthz: the container's health probe (deploy/Dockerfile, update.sh).

Cheap, unauthenticated, checks the central database, never says why it failed."""
from __future__ import annotations

import sqlalchemy as sa
from fastapi.testclient import TestClient

from lore.config import LoreConfig
from lore.web.app import create_app
from tests.auth_fixtures import BASE_URL, make_app
from tests.central_fixtures import central_sessions


def test_healthz_answers_ok_in_multi_mode_without_signing_in(tmp_path):
    sessions, _ = central_sessions(tmp_path, users=())
    app = make_app(LoreConfig(data_dir=tmp_path / "root"), sessions)
    r = TestClient(app, base_url=BASE_URL).get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert r.headers["cache-control"] == "no-store"


def test_healthz_is_503_with_no_details_when_the_database_is_down(tmp_path):
    sessions, _ = central_sessions(tmp_path, users=())
    app = make_app(LoreConfig(data_dir=tmp_path / "root"), sessions)
    # The database goes away after start-up: every new session fails to connect.
    sessions.configure(bind=sa.create_engine(
        f"sqlite:///{tmp_path / 'missing' / 'nowhere' / 'central.db'}"))
    r = TestClient(app, base_url=BASE_URL).get("/healthz")
    assert r.status_code == 503
    assert r.json() == {"ok": False}
    assert "sqlite" not in r.text.lower() and "nowhere" not in r.text


def test_healthz_answers_on_the_containers_own_address(tmp_path):
    """Docker's HEALTHCHECK probes 127.0.0.1 on the port the app was given."""
    sessions, _ = central_sessions(tmp_path, users=())
    app = make_app(LoreConfig(data_dir=tmp_path / "root"), sessions,
                   host="0.0.0.0", port=8000)
    r = TestClient(app, base_url="http://127.0.0.1:8000").get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_healthz_in_local_mode(tmp_path):
    app = create_app(LoreConfig(data_dir=tmp_path / "data"), multi_user=False)
    r = TestClient(app, base_url="http://127.0.0.1:8766").get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}
