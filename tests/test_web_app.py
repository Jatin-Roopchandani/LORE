"""The read endpoints: thin pass-throughs to lore.mcp.tools."""
import pytest
from fastapi.testclient import TestClient

from lore.web.app import create_app
from tests.mcp_fixtures import build_store_triple


@pytest.fixture
def config(tmp_path):
    return build_store_triple(
        tmp_path,
        books=[("b1", "A Study in Scarlet")],
        chunks=[("c1", "b1", 1)],
        entities=[("e1", "Holmes", "Character", ["b1"], []),
                  ("e2", "Watson", "Character", ["b1"], [])],
        edges=[("e1", "e2", "INTERACTS_WITH", "c1", "b1", 1)],
    )


@pytest.fixture
def client(config):
    return TestClient(create_app(config), base_url="http://127.0.0.1:8766")


def test_library(client):
    books = client.get("/api/library").json()["books"]
    assert books[0]["title"] == "A Study in Scarlet"


def test_entity_by_name_and_by_id(client):
    assert client.get("/api/entity", params={"name": "Holmes"}).json()["id"] == "e1"
    assert client.get("/api/entity", params={"id": "e2"}).json()["name"] == "Watson"


def test_entity_needs_a_name_or_id(client):
    assert client.get("/api/entity").status_code == 400


def test_subgraph_by_id(client):
    body = client.get("/api/subgraph", params={"id": "e1"}).json()
    assert {n["id"] for n in body["nodes"]} == {"e1", "e2"}


def test_unknown_entity_is_a_tool_error_not_a_crash(client):
    r = client.get("/api/entity", params={"name": "Moriarty"})
    assert r.status_code == 200 and r.json()["error"]["code"] == "not_found"


def test_quota_reports_models_and_the_reset_time(client):
    body = client.get("/api/quota").json()
    assert "resets_at" in body and isinstance(body["models"], list)


def test_mcp_details(client):
    body = client.get("/api/mcp").json()
    assert body["stdio_command"].startswith("claude mcp add lore -- ")
    assert body["http_url"] == "http://127.0.0.1:8765/mcp"
    assert body["http_running"] in (True, False)


def test_settings_follow_config(config):
    hidden = config.model_copy(update={"web": config.web.model_copy(
        update={"show_internals": False})})
    client = TestClient(create_app(hidden), base_url="http://127.0.0.1:8766")
    assert client.get("/api/settings").json() == {"show_internals": False}


def test_root_serves_a_page_even_before_the_frontend_is_built(client):
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
