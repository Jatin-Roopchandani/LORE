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


def test_building_while_running_serves_the_app_on_reload(config, tmp_path,
                                                         monkeypatch):
    from lore.web import app as app_module
    static = tmp_path / "static"
    static.mkdir()
    monkeypatch.setattr(app_module, "STATIC", static)
    client = TestClient(create_app(config), base_url="http://127.0.0.1:8766")
    assert "not built yet" in client.get("/").text
    (static / "index.html").write_text("<!doctype html><title>built</title>")
    (static / "assets").mkdir()
    (static / "assets" / "app.js").write_text("console.log(1)")
    r = client.get("/")
    assert r.status_code == 200 and "<title>built</title>" in r.text
    assert client.get("/assets/app.js").text == "console.log(1)"


def test_entities_by_ids_endpoint(client):
    body = client.get("/api/entities", params={"ids": "e2,missing,e1"}).json()
    assert [(e["id"], e["kind"]) for e in body["entities"]] == [
        ("e1", "Character"), ("e2", "Character")]
    assert client.get("/api/entities").json() == {"entities": []}


def test_stdio_command_quotes_the_lore_path(client, monkeypatch):
    from lore.web import app as app_module
    monkeypatch.setattr(app_module, "_lore_executable", lambda: "/home/a b/lore")
    body = client.get("/api/mcp").json()
    assert body["stdio_command"] == "claude mcp add lore -- '/home/a b/lore' mcp"


def test_overview_endpoint(client):
    body = client.get("/api/overview", params={"limit": 40}).json()
    assert [n["id"] for n in body["nodes"]] == ["e1", "e2"]
    assert body["edges"][0]["subject_id"] == "e1"
    assert body["truncated"] is False and body["depth"] == 0
    assert client.get("/api/overview").status_code == 200
