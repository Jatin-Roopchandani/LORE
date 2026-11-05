"""Hidden internals in multi mode (release step 6, task 1).

Failures are forced from each layer (the provider, the book parser, the graph
store, an unexpected exception) and every response must hold no model name,
file path, traceback or key fragment; errors carry a friendly message and the
request id. Local mode keeps showing everything."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from lore.config import LoreConfig
from lore.inference.gemini import GeminiError
from lore.mcp import tools
from lore.web import friendly
from tests.auth_fixtures import BASE_URL, FAKE_KEY, give_key, make_app, sign_in
from tests.central_fixtures import central_sessions

LEAKY = ("gemini-2.5-flash-lite returned 503 for models/gemini-2.5-flash at"
         " /home/jatin/LORE-release/lore/inference/gemini.py with key " + FAKE_KEY)
FORBIDDEN = ("gemini-2.5", "gemma-", "models/", "/home/", "lore/inference", "Traceback",
             'File "', FAKE_KEY, FAKE_KEY[:12], "AIza", ".py", "Kuzu", "kuzu",
             "RuntimeError", "GeminiError")


def assert_clean(text: str) -> None:
    for bad in FORBIDDEN:
        assert bad not in text, (bad, text[:600])


def events(text: str) -> list[dict]:
    out = []
    for block in text.split("\n\n"):
        kind = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                kind = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if kind:
            out.append({"type": kind, **data})
    return out


@pytest.fixture
def central(tmp_path):
    sessions, _ = central_sessions(tmp_path, users=("alice",))
    give_key(sessions, "alice")
    return sessions


@pytest.fixture
def root(tmp_path):
    return LoreConfig(data_dir=tmp_path / "root")


@pytest.fixture
def alice(root, central):
    app = make_app(root, central)
    client = sign_in(app, central, "alice")
    client.app_under_test = app
    return client


def ask(client, question="Who is Holmes?"):
    r = client.post("/api/ask", json={"question": question})
    assert r.status_code == 200, r.text
    return r


# --- the provider -------------------------------------------------------------

def test_a_provider_failure_is_friendly_with_the_request_id(alice, monkeypatch):
    async def failing(config, query, on_event=None, **_):
        return tools._upstream(GeminiError(LEAKY))
    monkeypatch.setattr("lore.web.app.tools.lore_search", failing)
    r = ask(alice)
    assert_clean(r.text)
    (err,) = [e for e in events(r.text) if e["type"] == "error"]
    assert err["error"]["code"] == "upstream_unavailable"
    assert err["error"]["message"] == friendly.FIXED["upstream_unavailable"]
    assert err["error"]["request_id"] == r.headers["x-request-id"]


def test_steps_notes_and_the_answer_carry_no_model_names(alice, monkeypatch):
    async def chatty(config, query, on_event=None, **_):
        on_event({"type": "step", "name": "write", "state": "done", "part": None,
                  "detail": "written by gemini-2.5-flash in /srv/lore/data/users/x"})
        on_event({"type": "note", "text": "gemma-3-27b-it judged it"})
        return {"answer": "Holmes is a detective.", "mode": "graph", "note": None,
                "model": "gemini-2.5-flash", "selection_model": "gemini-2.5-flash-lite",
                "claims": [], "passages": [], "edges": [], "seeds": [],
                "sub_questions": [query]}
    monkeypatch.setattr("lore.web.app.tools.lore_search", chatty)
    r = ask(alice)
    assert_clean(r.text)
    got = events(r.text)
    (answer,) = [e for e in got if e["type"] == "answer"]
    assert answer["result"]["answer"] == "Holmes is a detective."
    assert answer["result"]["model"] is None and answer["result"]["selection_model"] is None
    (step,) = [e for e in got if e["type"] == "step"]
    assert "the model" in step["detail"]
    # The saved chat replays the same public answer.
    chat = alice.get(f"/api/chats/{answer['chat_id']}")
    assert chat.status_code == 200
    assert_clean(chat.text)


def test_an_unexpected_exception_in_the_search_is_internal(alice, monkeypatch):
    async def crash(config, query, on_event=None, **_):
        raise RuntimeError(LEAKY)
    monkeypatch.setattr("lore.web.app.tools.lore_search", crash)
    r = ask(alice)
    assert_clean(r.text)
    (err,) = [e for e in events(r.text) if e["type"] == "error"]
    rid = r.headers["x-request-id"]
    assert err["error"] == {"code": "internal", "request_id": rid,
                            "message": f"Something went wrong. Reference: {rid}"}


def test_a_tool_error_with_raw_text_is_rewritten(alice, monkeypatch):
    def raw(config, **_):
        return {"error": {"code": "upstream_unavailable", "message": LEAKY}}
    monkeypatch.setattr("lore.web.app.tools.library", raw)
    r = alice.get("/api/library")
    assert_clean(r.text)
    assert r.json()["error"]["request_id"] == r.headers["x-request-id"]


# --- the graph store and anything unexpected ------------------------------------

def test_a_graph_store_crash_is_a_friendly_500(alice, monkeypatch):
    def kuzu_down(config, **_):
        raise RuntimeError("Kuzu IO exception: /data/users/alice/kuzu/catalog.kz " + LEAKY)
    monkeypatch.setattr("lore.web.app.tools.library", kuzu_down)
    r = alice.get("/api/library")
    assert r.status_code == 500
    assert_clean(r.text)
    rid = r.headers["x-request-id"]
    assert r.json()["error"] == {"code": "internal", "request_id": rid,
                                 "message": f"Something went wrong. Reference: {rid}"}


def test_an_unexpected_exception_in_any_route_is_a_friendly_500(alice, monkeypatch):
    def boom(config):
        raise ZeroDivisionError(LEAKY)
    monkeypatch.setattr("lore.web.app._quota", boom)
    r = alice.get("/api/quota")
    assert r.status_code == 500 and r.headers["content-type"] == "application/json"
    assert_clean(r.text)


def test_the_log_still_has_the_traceback_but_not_the_key(alice, monkeypatch, caplog):
    def boom(config):
        raise ZeroDivisionError("kaboom " + FAKE_KEY)
    monkeypatch.setattr("lore.web.app._quota", boom)
    with caplog.at_level("ERROR", logger="lore.web"):
        alice.get("/api/quota")
    record = next(r for r in caplog.records if r.name == "lore.web")
    assert record.exc_info is not None
    assert FAKE_KEY not in caplog.text


def test_validation_errors_never_echo_the_request(alice):
    r = alice.post("/api/worlds", json={"name": {"secret": FAKE_KEY}})
    assert r.status_code == 422
    assert_clean(r.text)
    assert r.json()["error"]["code"] == "invalid_request"
    assert "secret" not in r.text


def test_http_errors_are_friendly_and_keep_detail_for_the_client(alice):
    r = alice.get("/api/entity")  # neither id nor name: HTTPException(400)
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["code"] == "bad_request" and body["detail"]
    assert body["error"]["request_id"] == r.headers["x-request-id"]


# --- the book parser ------------------------------------------------------------

def test_a_parser_failure_on_upload_is_friendly(alice):
    junk = b"%PDF-1.7\n" + b"\x00garbage" * 200
    r = alice.post("/api/books/upload", files={"file": ("broken.pdf", junk, "application/pdf")})
    assert r.status_code == 400
    assert_clean(r.text)
    err = r.json()["error"]
    assert err["code"] == "not_a_book" and err["message"] == friendly.FIXED["not_a_book"]
    assert err["request_id"] == r.headers["x-request-id"]


# --- no internals switched on ----------------------------------------------------

def test_show_internals_is_forced_off(root, central):
    root.web.show_internals = True
    app = make_app(root, central)
    r = sign_in(app, central, "alice").get("/api/settings")
    assert r.json() == {"show_internals": False}


def test_the_quota_names_no_models_and_mcp_is_off(alice):
    q = alice.get("/api/quota").json()
    assert q["models"] == []
    assert alice.get("/api/mcp").status_code == 404


def test_the_mcp_server_refuses_multi_user_mode(monkeypatch, tmp_path):
    from lore.mcp.server import MultiUserRefused, build_server
    monkeypatch.setenv("LORE_MULTI_USER", "1")
    with pytest.raises(MultiUserRefused):
        build_server(LoreConfig(data_dir=tmp_path))


def test_lore_mcp_refuses_multi_user_mode(monkeypatch):
    from typer.testing import CliRunner
    from lore.cli import app
    monkeypatch.setenv("LORE_MULTI_USER", "1")
    result = CliRunner().invoke(app, ["mcp"])
    assert result.exit_code == 2
    assert "local-only" in result.output


# --- local mode is unchanged ------------------------------------------------------

def test_local_mode_still_shows_internals(tmp_path, monkeypatch):
    from lore.web.app import create_app

    async def failing(config, query, on_event=None, **_):
        return tools._upstream(GeminiError("gemini-2.5-flash said no"))
    monkeypatch.setattr("lore.web.app.tools.lore_search", failing)
    client = TestClient(create_app(LoreConfig(data_dir=tmp_path / "lib")), base_url=BASE_URL)
    assert client.get("/api/settings").json() == {"show_internals": True}
    r = client.post("/api/ask", json={"question": "q"})
    assert "gemini-2.5-flash said no" in r.text


def test_scrub_text_removes_paths_models_traces_and_keys():
    text = ("Traceback (most recent call last):\n  File \"/x/y.py\", line 3\n")
    assert friendly.scrub_text(text) == ""
    assert friendly.scrub_text("no world 'Dune'") == "no world 'Dune'"
    assert "the model" in friendly.scrub_text("gemini-2.5-flash is busy")
    assert friendly.scrub_text("at /srv/lore/data/x.db") == "at [path]"
    assert FAKE_KEY not in friendly.scrub_text("key " + FAKE_KEY)
