"""lore ui is unauthenticated, so it must only be reachable from this machine
and only be drivable by its own page."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lore.config import LoreConfig
from lore.web.security import (
    NonLoopbackHost, OriginGuard, allowed_hosts, allowed_origins, check_loopback,
)


def test_web_config_defaults():
    config = LoreConfig()
    assert config.web.show_internals is True and config.web.port == 8766


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "localhost", "::1"])
def test_loopback_hosts_are_allowed(host):
    check_loopback(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com", ""])
def test_other_hosts_are_refused(host):
    with pytest.raises(NonLoopbackHost):
        check_loopback(host)


def _client(dev=False):
    app = FastAPI()

    @app.get("/api/ping")
    def ping():
        return {"ok": True}

    guarded = OriginGuard(app, allowed_hosts("127.0.0.1", 8766),
                          allowed_origins("127.0.0.1", 8766, dev))
    return TestClient(guarded, base_url="http://127.0.0.1:8766")


def test_same_origin_requests_pass():
    r = _client().get("/api/ping", headers={"Origin": "http://127.0.0.1:8766"})
    assert r.status_code == 200


def test_requests_without_origin_pass():  # curl, the browser's own navigation
    assert _client().get("/api/ping").status_code == 200


def test_a_foreign_origin_is_refused():
    r = _client().get("/api/ping", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"


def test_a_rebound_host_is_refused():
    r = _client().get("/api/ping", headers={"Host": "evil.example:8766"})
    assert r.status_code == 403


def test_the_vite_dev_origin_passes_only_in_dev():
    headers = {"Origin": "http://localhost:5173"}
    assert _client(dev=False).get("/api/ping", headers=headers).status_code == 403
    assert _client(dev=True).get("/api/ping", headers=headers).status_code == 200
