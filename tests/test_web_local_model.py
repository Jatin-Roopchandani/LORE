"""A local model chosen in the web app (local mode): GET/PUT/DELETE
/api/local-model and POST /api/local-model/test, the saved choice overriding
lore.toml for every model step, the key file, and none of it in multi mode.
The server under test is a faked HTTP transport (no network)."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from lore.config import LoreConfig, read_saved_llm, saved_llm_path, with_saved_llm
from lore.inference.providers import factory
from lore.web.app import create_app

BASE = "http://localhost:8080/v1"
KEY = "local-SECRET-key-0001"


def reply(content):
    return httpx.Response(200, json={"choices": [{"index": 0, "finish_reason": "stop",
                                                  "message": {"role": "assistant",
                                                              "content": content}}]})


class Server:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        r = self.replies.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in ("LORE_MULTI_USER", "LORE_LLM_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "home"


def client(tmp_path, server=None, **cfg):
    config = LoreConfig(data_dir=tmp_path / "data", **cfg)
    config.ensure_dirs()
    app = create_app(config, local_model_transport=httpx.MockTransport(server or Server()))
    return TestClient(app, base_url="http://127.0.0.1:8766"), config


def test_nothing_saved_shows_lore_toml(tmp_path, home):
    c, _ = client(tmp_path)
    out = c.get("/api/local-model").json()
    assert out["active"] == {"provider": "gemini", "model": None, "fast_model": None,
                             "base_url": None, "source": "lore.toml"}
    assert out["saved"] is None
    assert out["default_base_url"] == BASE


def test_save_overrides_lore_toml_for_every_step_and_delete_restores_it(tmp_path, home):
    c, config = client(tmp_path)
    r = c.put("/api/local-model", json={"base_url": BASE, "model": "qwen-28b", "key": KEY})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["active"]["source"] == "saved"
    assert out["active"]["provider"] == "openai_compatible"
    assert out["saved"] == {"provider": "openai_compatible", "model": "qwen-28b",
                            "fast_model": None, "base_url": BASE, "has_key": True}
    assert KEY not in r.text
    # the files: the choice in the data dir, the key next to the other keys; both 600
    path = saved_llm_path(config.data_dir)
    assert json.loads(path.read_text()) == {"base_url": BASE, "model": "qwen-28b",
                                            "fast_model": ""}
    assert path.stat().st_mode & 0o777 == 0o600
    key_file = home / ".config" / "lore" / "llm.env"
    assert key_file.read_text() == f"LORE_LLM_API_KEY={KEY}\n"
    assert key_file.stat().st_mode & 0o777 == 0o600
    # every model step now runs on it, exactly as the lore.toml setup would
    effective = with_saved_llm(config)
    assert (effective.llm.provider, effective.llm.base_url, effective.llm.model) == (
        "openai_compatible", BASE, "qwen-28b")
    assert effective.extraction_provider == "gemini"  # i.e. "the [lore.llm] model"
    assert {effective.selection.provider, effective.synthesis.provider,
            effective.adjudication.provider} == {"gemini"}
    creds = factory.credentials(effective)
    assert (creds.provider, creds.key, creds.base_url) == ("openai_compatible", KEY, BASE)
    assert LoreConfig.load(tmp_path / "none.toml").llm.provider == "gemini"  # another data dir
    # back to lore.toml as written
    out = c.delete("/api/local-model").json()
    assert out["saved"] is None and out["active"]["source"] == "lore.toml"
    assert with_saved_llm(config).llm.provider == "gemini"
    assert with_saved_llm(effective).extraction_provider == "ollama"


def test_load_applies_the_saved_choice(tmp_path, home, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("LORE_DATA_DIR", str(data))
    data.mkdir()
    saved_llm_path(data).write_text(json.dumps({"base_url": BASE, "model": "m"}))
    cfg = LoreConfig.load(tmp_path / "none.toml")
    assert (cfg.llm.provider, cfg.synthesis.provider) == ("openai_compatible", "gemini")
    assert cfg.file_config.llm.provider == "gemini"
    monkeypatch.setenv("LORE_MULTI_USER", "1")  # never in multi mode
    assert LoreConfig.load(tmp_path / "none.toml").llm.provider == "gemini"


def test_a_saved_key_is_kept_and_can_be_cleared(tmp_path, home):
    c, _ = client(tmp_path)
    c.put("/api/local-model", json={"base_url": BASE, "model": "m", "key": KEY})
    out = c.put("/api/local-model", json={"base_url": BASE, "model": "m2"}).json()
    assert out["saved"]["has_key"] is True and out["saved"]["model"] == "m2"
    out = c.put("/api/local-model", json={"base_url": BASE, "model": "m2",
                                          "clear_key": True}).json()
    assert out["saved"]["has_key"] is False


@pytest.mark.parametrize("body, words", [
    ({"base_url": "ftp://x", "model": "m"}, "http"),
    ({"base_url": "", "model": "m"}, "http"),
    ({"base_url": BASE, "model": ""}, "model"),
    ({"base_url": BASE}, "model"),
])
def test_bad_input_is_refused_in_words(tmp_path, home, body, words):
    c, config = client(tmp_path)
    r = c.put("/api/local-model", json=body)
    assert r.status_code == 400 and words in r.json()["error"]["message"]
    assert read_saved_llm(config.data_dir) is None
    assert c.post("/api/local-model/test", json=body).status_code == 400


def test_test_connection_makes_one_tiny_structured_call(tmp_path, home):
    server = Server(reply('{"ok": true}'))
    c, config = client(tmp_path, server)
    r = c.post("/api/local-model/test", json={"base_url": BASE, "model": "qwen", "key": KEY})
    out = r.json()
    assert out["ok"] is True and out["model"] == "qwen" and out["seconds"] >= 0
    (request,) = server.requests
    assert str(request.url) == f"{BASE}/chat/completions"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert json.loads(request.content)["response_format"]["type"] == "json_schema"
    assert read_saved_llm(config.data_dir) is None  # a test saves nothing


@pytest.mark.parametrize("failure, words", [
    (httpx.ConnectError("refused"), "Couldn't reach a server at"),
    (httpx.Response(401, json={}), "refused the key"),
    (httpx.Response(404, json={"error": {"message": "model 'nope' not found"}}),
     "model 'nope' not found"),
])
def test_a_failed_test_says_why_in_plain_words(tmp_path, home, failure, words):
    c, _ = client(tmp_path, Server(failure))
    out = c.post("/api/local-model/test", json={"base_url": BASE, "model": "nope"}).json()
    assert out["ok"] is False and words in out["message"]


def test_a_model_that_cannot_do_json_is_named(tmp_path, home):
    c, _ = client(tmp_path, Server(reply("sure!"), reply("ok then")))
    out = c.post("/api/local-model/test", json={"base_url": BASE, "model": "tiny"}).json()
    assert out["ok"] is False and "didn't return the JSON" in out["message"]


def test_no_route_in_multi_mode(tmp_path, monkeypatch):
    from tests.auth_fixtures import make_app, sign_in
    from tests.central_fixtures import central_sessions
    sessions, _ = central_sessions(tmp_path, users=("alice",))
    app = make_app(LoreConfig(data_dir=tmp_path / "root"), sessions)
    alice = sign_in(app, sessions, "alice")
    assert alice.get("/api/local-model").status_code == 404
    r = alice.put("/api/local-model", json={"base_url": BASE, "model": "m"})
    assert r.status_code in (403, 404, 405)
    assert not saved_llm_path(tmp_path / "root").exists()
    # and a user cannot pick it as their provider either
    assert alice.put("/api/me/key", json={"provider": "openai_compatible",
                                          "key": "x"}).status_code == 400


def test_upload_with_only_a_local_model_saved_processes_on_it(tmp_path, home):
    """No cloud key anywhere: a book uploaded in local mode is processed, and
    its stages see the saved local model as the provider."""
    from lore.orchestrator import jobs
    from lore.web.upload import Limits, UploadSettings
    from tests.test_web_upload import NOW, epub_bytes, send, wait_job

    seen = []

    class Stage:
        name = "chunk"

        def total(self, ctx):
            return 1

        def run(self, ctx, job_ctl):
            seen.append((ctx.config.llm.provider, ctx.config.extraction_provider,
                         factory.credentials(ctx.config).base_url))

    config = LoreConfig(data_dir=tmp_path / "lib")
    config.ensure_dirs()
    app = create_app(config, stages_factory=lambda c: [Stage()], clock=lambda: NOW,
                     upload_settings=UploadSettings(checks=Limits(max_pages=50)))
    c = TestClient(app, base_url="http://127.0.0.1:8766")
    assert c.put("/api/local-model", json={"base_url": BASE, "model": "q"}).status_code == 200
    r = send(c, epub_bytes(tmp_path), "Book.epub", world="Local")
    assert r.status_code == 202, r.text
    assert wait_job(config, r.json()["book_id"]).state == jobs.DONE
    assert seen == [("openai_compatible", "gemini", BASE)]
