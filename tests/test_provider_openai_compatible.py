"""The OpenAI-compatible provider (llama-server, vLLM, LM Studio, Ollama's /v1)
through a faked HTTP transport: request shape, the schema-validated reply and
its one corrective retry, the fallback for servers that refuse json_schema,
error mapping, usage, the concurrency gate, the factory wiring and the
multi-mode refusal. No network."""
import asyncio
import json

import httpx
import pytest
from pydantic import BaseModel

from lore.config import LlmConfig, LoreConfig
from lore.inference import key_scope
from lore.inference.gemini import GeminiOutputError
from lore.inference.key_scope import KeyScope
from lore.inference.providers import factory, roles
from lore.inference.providers.base import (
    OPENAI_COMPATIBLE, ProviderError, ProviderKeyError, ProviderOutputError,
    ProviderQuotaError, ProviderTransportError, ProviderUnavailableError,
)
from lore.inference.providers.openai_compatible import (
    MultiUserRefused, OpenAICompatibleTextModel,
)
from lore.inference.providers.schema import strict_schema

BASE = "http://gpu-box.lan:8080/v1"
KEY = "local-secret-KEYkeyKEYkeyKEY-0001"


class Reply(BaseModel):
    answer: str
    note: str | None = None


def completion(content, finish="stop", usage=(11, 5)):
    body = {"id": "x", "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": finish,
                         "message": {"role": "assistant", "content": content}}]}
    if usage is not None:
        body["usage"] = {"prompt_tokens": usage[0], "completion_tokens": usage[1],
                         "total_tokens": usage[0] + usage[1]}
    return httpx.Response(200, json=body)


class Server:
    """A fake chat-completions server: replies in order, records requests."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def body(self, i=0):
        return json.loads(self.requests[i].content)


def model(server, key=None, name="qwen-28b", **kw):
    return OpenAICompatibleTextModel(
        key, base_url=BASE, model=name,
        transport=httpx.MockTransport(server), **kw)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def local_mode(monkeypatch):
    monkeypatch.delenv("LORE_MULTI_USER", raising=False)


# --- request shape --------------------------------------------------------------

def test_json_goes_to_chat_completions_with_a_json_schema():
    server = Server(completion('{"answer": "yes", "note": null}'))
    m = model(server)
    got = run(m.generate_json("Is it?", Reply))
    assert got == Reply(answer="yes")
    request = server.requests[0]
    assert str(request.url) == f"{BASE}/chat/completions"
    assert "authorization" not in request.headers  # no key configured: none sent
    body = server.body()
    assert body["model"] == "qwen-28b"
    assert body["messages"] == [{"role": "user", "content": "Is it?"}]
    assert body["temperature"] == 0
    assert body["stream"] is False
    assert body["max_tokens"] == 8192  # a cap, so a small model's runaway repetition ends
    fmt = body["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "Reply"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"] == strict_schema(Reply)
    assert (m.last_model, m.requests_made, m.attempts) == ("qwen-28b", 1, {"qwen-28b": 1})


def test_a_key_is_sent_as_a_bearer_token_and_never_shown():
    server = Server(completion("plain words"))
    m = model(server, key=KEY)
    assert run(m.generate("Say it.")) == "plain words"
    assert server.requests[0].headers["authorization"] == f"Bearer {KEY}"
    assert "response_format" not in server.body()
    assert KEY not in repr(m)


def test_max_tokens_follows_the_setting_and_zero_sends_none(tmp_path, monkeypatch):
    server = Server(completion("ok"), completion("ok"))
    run(model(server, max_tokens=1000).generate("x"))
    run(model(server, max_tokens=0).generate("x"))
    assert server.body(0)["max_tokens"] == 1000
    assert "max_tokens" not in server.body(1)
    assert LlmConfig().max_output_tokens == 8192


def test_a_trailing_slash_in_base_url_is_harmless():
    server = Server(completion("ok"))
    m = OpenAICompatibleTextModel(None, base_url=BASE + "/", model="m",
                                  transport=httpx.MockTransport(server))
    run(m.generate("x"))
    assert str(server.requests[0].url) == f"{BASE}/chat/completions"


# --- validation and the one corrective retry --------------------------------------

def test_an_invalid_reply_is_retried_once_with_the_error_in_the_prompt():
    server = Server(completion('{"nope": 1}'), completion('{"answer": "fixed"}'))
    m = model(server)
    assert run(m.generate_json("Q?", Reply)) == Reply(answer="fixed")
    retry = server.body(1)["messages"]
    assert retry[0] == {"role": "user", "content": "Q?"}
    assert retry[1] == {"role": "assistant", "content": '{"nope": 1}'}
    assert retry[2]["role"] == "user"
    assert "answer" in retry[2]["content"] and "not valid" in retry[2]["content"]
    assert m.requests_made == 2


def test_two_invalid_replies_are_an_output_error_like_any_provider():
    server = Server(completion("not json"), completion('{"answer": 3}'))
    with pytest.raises(ProviderOutputError) as caught:
        run(model(server).generate_json("Q?", Reply))
    assert isinstance(caught.value, GeminiOutputError)  # callers sort it as ever
    assert caught.value.raw_output == '{"answer": 3}'
    assert caught.value.model == "qwen-28b"


def test_fences_and_think_blocks_around_json_are_tolerated():
    text = '<think>Let me see.</think>\n```json\n{"answer": "a"}\n```'
    assert run(model(Server(completion(text))).generate_json("Q", Reply)) == Reply(answer="a")
    assert run(model(Server(completion("<think>hm</think>\nHello"))).generate("Q")) == "Hello"


def test_a_server_that_refuses_json_schema_gets_json_object_and_the_schema_in_words():
    refused = httpx.Response(400, json={"error": {"message": "response_format json_schema"
                                                  " is not supported"}})
    server = Server(refused, completion('{"answer": "ok"}'), completion('{"answer": "2"}'))
    m = model(server)
    assert run(m.generate_json("Q?", Reply)) == Reply(answer="ok")
    fallback = server.body(1)
    assert fallback["response_format"] == {"type": "json_object"}
    assert fallback["messages"][0]["content"].startswith("Q?")
    assert '"answer"' in fallback["messages"][0]["content"]  # the schema, in words
    run(m.generate_json("again", Reply))  # remembered: no second refusal
    assert server.body(2)["response_format"] == {"type": "json_object"}


def test_a_cut_off_reply_is_an_output_error():
    with pytest.raises(ProviderOutputError, match="length"):
        run(model(Server(completion("half a sen", finish="length"))).generate("Q"))


def test_an_empty_reply_is_an_output_error():
    with pytest.raises(ProviderOutputError, match="empty"):
        run(model(Server(completion(""))).generate("Q"))


# --- errors -----------------------------------------------------------------------

@pytest.mark.parametrize("reply, lore_error", [
    (httpx.Response(401, json={}), ProviderKeyError),
    (httpx.Response(403, json={}), ProviderKeyError),
    (httpx.Response(429, json={}, headers={"retry-after": "7"}), ProviderQuotaError),
    (httpx.Response(503, json={}), ProviderUnavailableError),
    (httpx.Response(404, json={"error": "model not found"}), ProviderError),
    (httpx.ConnectError("refused"), ProviderTransportError),
    (httpx.ReadTimeout("slow"), ProviderTransportError),
])
def test_errors_map_to_lore_errors(reply, lore_error):
    with pytest.raises(lore_error) as caught:
        run(model(Server(reply), key=KEY).generate("Q"))
    assert KEY not in str(caught.value)
    if lore_error is ProviderQuotaError:
        assert caught.value.retry_after == 7


# --- usage, never a price ------------------------------------------------------------

def test_tokens_are_reported_when_the_server_gives_them():
    seen = []
    m = model(Server(completion("a", usage=(30, 7)), completion("b", usage=None)),
              on_usage=lambda *a: seen.append(a))
    scope_usage = []
    scope = KeyScope("local", None, provider=OPENAI_COMPATIBLE,
                     on_usage=lambda *a: scope_usage.append(a))
    with key_scope.bound(scope):
        run(m.generate("x"))
        run(m.generate("y"))
    assert seen == [(OPENAI_COMPATIBLE, "qwen-28b", 30, 7)]
    assert scope_usage == seen
    assert (m.input_tokens, m.output_tokens) == (30, 7)
    from lore.inference.providers import estimate
    out = estimate.estimate(500, OPENAI_COMPATIBLE, "qwen-28b")
    assert out["estimated_cost_usd"] is None and out["needs_confirm"] is False


# --- concurrency ----------------------------------------------------------------------

@pytest.mark.parametrize("limit", [1, 3])
def test_the_concurrency_setting_caps_requests_in_flight(limit):
    state = {"now": 0, "peak": 0}

    async def handler(request):
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.01)
        state["now"] -= 1
        return completion("ok")

    async def go():
        # Two models (two call sites) on one server share the one gate.
        a = OpenAICompatibleTextModel(None, base_url=BASE, model="m", concurrency=limit,
                                      transport=httpx.MockTransport(handler))
        b = OpenAICompatibleTextModel(None, base_url=BASE, model="m", concurrency=limit,
                                      transport=httpx.MockTransport(handler))
        await asyncio.gather(*(x.generate("q") for x in [a, b] * 4))

    run(go())
    assert state["peak"] == limit


# --- config and the factory -------------------------------------------------------------

def _config(tmp_path, **llm):
    cfg = LoreConfig(data_dir=tmp_path / "data", extraction_provider="gemini",
                     selection={"provider": "gemini"}, synthesis={"provider": "gemini"},
                     adjudication={"provider": "gemini"}, disambiguation={"enabled": True},
                     llm={"provider": "openai_compatible", "base_url": BASE,
                          "model": "qwen-28b", **llm})
    cfg.ensure_dirs()
    return cfg


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "LORE_MULTI_USER", "LORE_LLM_API_KEY",
                 "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_config_needs_a_base_url_and_a_model():
    assert LlmConfig().concurrency == 1
    with pytest.raises(ValueError, match="base_url"):
        LlmConfig(provider="openai_compatible", model="m")
    with pytest.raises(ValueError, match="base_url"):
        LlmConfig(provider="openai_compatible", model="m", base_url="file:///etc")
    with pytest.raises(ValueError, match="model"):
        LlmConfig(provider="openai_compatible", base_url=BASE)
    with pytest.raises(ValueError):
        LlmConfig(provider="openai_compatible", base_url=BASE, model="m", api_key="x")
    with pytest.raises(ValueError):
        LlmConfig(concurrency=0)


def test_the_config_loads_from_toml(tmp_path):
    path = tmp_path / "lore.toml"
    path.write_text('[lore.llm]\nprovider = "openai_compatible"\n'
                    f'base_url = "{BASE}"\nmodel = "qwen-28b"\nconcurrency = 2\n')
    cfg = LoreConfig.load(path)
    assert (cfg.llm.provider, cfg.llm.base_url, cfg.llm.model, cfg.llm.concurrency) == (
        "openai_compatible", BASE, "qwen-28b", 2)


def test_the_key_is_optional_and_comes_from_the_env_or_llm_env(clean_env, monkeypatch):
    cfg = _config(clean_env)
    creds = factory.credentials(cfg)
    assert (creds.provider, creds.model, creds.base_url) == (OPENAI_COMPATIBLE, "qwen-28b", BASE)
    assert creds.key  # "configured", so every "is there a key?" check passes
    assert factory.build(creds, cfg, "write", gemini_models=[])._key is None  # none sent
    path = clean_env / ".config" / "lore" / "llm.env"
    path.parent.mkdir(parents=True)
    path.write_text(f"LORE_LLM_API_KEY={KEY}\n")
    path.chmod(0o644)
    with pytest.raises(Exception, match="chmod 600"):
        factory.credentials(cfg)
    path.chmod(0o600)
    assert factory.credentials(cfg).key == KEY
    assert KEY not in repr(factory.credentials(cfg))
    monkeypatch.setenv("LORE_LLM_API_KEY", "from-env")
    assert factory.credentials(cfg).key == "from-env"


def test_every_remote_site_runs_on_it_with_model_or_fast_model(clean_env, monkeypatch):
    from tests.test_provider_factory import _sites
    built = []
    real = OpenAICompatibleTextModel.__init__

    def spy(self, api_key, **kw):
        built.append((kw["model"], kw["base_url"], kw.get("concurrency")))
        assert kw["max_tokens"] == 8192
        real(self, api_key, **kw)
    monkeypatch.setattr(OpenAICompatibleTextModel, "__init__", spy)

    cfg = _config(clean_env, concurrency=2)
    for name, make in _sites(cfg).items():
        built.clear()
        assert make() is not None, name
        assert built and {b for b in built} == {("qwen-28b", BASE, 2)}, name

    cfg = _config(clean_env, fast_model="qwen-7b")
    for site, spec in roles.SITES.items():
        creds = factory.credentials(cfg)
        m = factory.build(creds, cfg, site, gemini_models=[])
        assert m.model == ("qwen-7b" if spec.role == roles.FAST else "qwen-28b"), site


def test_processing_concurrency_follows_the_setting(clean_env):
    from lore.orchestrator.concurrency import llm_concurrency
    assert llm_concurrency(_config(clean_env)) == 1
    assert llm_concurrency(_config(clean_env, concurrency=3)) == 3
    assert factory.on_gemini(_config(clean_env)) is False
    assert factory.processing_model(_config(clean_env, fast_model="f")) == "f"


# --- multi mode: never ----------------------------------------------------------------

def test_multi_mode_refuses_it_everywhere(clean_env, monkeypatch):
    cfg = _config(clean_env)
    monkeypatch.setenv("LORE_MULTI_USER", "1")
    with pytest.raises(MultiUserRefused):
        factory.credentials(cfg)
    with pytest.raises(MultiUserRefused):
        factory.build(factory.Credentials(OPENAI_COMPATIBLE, "k", "m", base_url=BASE),
                      cfg, "write", gemini_models=[])
    with pytest.raises(MultiUserRefused):
        OpenAICompatibleTextModel(None, base_url=BASE, model="m")
    # A bound user can never carry it either (KeyService offers only PROVIDERS).
    with key_scope.bound(KeyScope("eve", "k", provider=OPENAI_COMPATIBLE, model="m")):
        with pytest.raises(MultiUserRefused):
            factory.credentials(cfg)


def test_users_cannot_choose_it_in_multi_mode():
    from lore.inference.providers.base import PROVIDERS
    assert OPENAI_COMPATIBLE not in PROVIDERS


def test_the_multi_user_server_will_not_start_with_it(clean_env, monkeypatch):
    from lore.web.app import create_app
    with pytest.raises(MultiUserRefused):
        create_app(_config(clean_env), multi_user=True)
