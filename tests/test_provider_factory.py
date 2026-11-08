"""The one factory: every model call site builds the user's provider.

Multi mode: a user on Claude or OpenAI gets Claude or OpenAI at every site,
with their own key, and never Gemini or the server's key. Local mode:
[lore.llm] picks the provider; its key comes from the environment or
~/.config/lore/<provider>.env. Gemini is covered byte for byte by
test_gemini_golden.py. All offline."""
import asyncio
import json
from types import SimpleNamespace as NS

import pytest
from pydantic import BaseModel

from lore.config import LoreConfig
from lore.inference import gemini, key_scope
from lore.inference.gemini import GeminiClient
from lore.inference.key_scope import KeyRequired, KeyScope
from lore.inference.providers import factory, roles
from lore.inference.providers.anthropic_model import AnthropicTextModel
from lore.inference.providers.base import ProviderRefusal
from lore.inference.providers.openai_model import OpenAITextModel

ALICE_CLAUDE = "sk-ant-api03-ALICEaliceALICEaliceALICEalice-0001"
BOB_OPENAI = "sk-proj-BOBbobBOBbobBOBbobBOBbobBOBbob0002"
SERVER_CLAUDE = "sk-ant-api03-SERVERserverSERVERserver-9999"
SERVER_GEMINI = "AIzaSERVERenvSERVERenvSERVERenvSERVE6789"


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "LORE_MULTI_USER",
                 "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.fixture
def built(monkeypatch):
    """Every model the factory builds: (class name, key, model, effort)."""
    made = []

    def spy(cls):
        real = cls.__init__

        def init(self, api_key, **kwargs):
            made.append((cls.__name__, api_key, kwargs.get("model"), kwargs.get("effort"),
                         tuple(kwargs.get("models") or ())))
            if cls is GeminiClient:
                real(self, api_key, **kwargs)
            else:
                real(self, api_key, client=NS(close=None), **kwargs)
        monkeypatch.setattr(cls, "__init__", init)

    for cls in (GeminiClient, AnthropicTextModel, OpenAITextModel):
        spy(cls)
    return made


def _config(tmp_path, **llm):
    cfg = LoreConfig(data_dir=tmp_path / "data", extraction_provider="gemini",
                     selection={"provider": "gemini"}, synthesis={"provider": "gemini"},
                     adjudication={"provider": "gemini"}, disambiguation={"enabled": True},
                     llm=llm or {})
    cfg.ensure_dirs()
    return cfg


def _sites(cfg):
    from lore.graph.adjudicate import _adjudication_client, make_gemini_first_person
    from lore.mcp.tools import _build_models
    from lore.orchestrator import stages
    from lore.orchestrator.local import make_gemini_tier
    from lore.query import explore
    return {
        "query models": lambda: _build_models(cfg),
        "extraction": lambda: make_gemini_tier(cfg),
        "adjudication": lambda: _adjudication_client(cfg, None),
        "narrator": lambda: make_gemini_first_person(cfg),
        "disambiguation": lambda: stages._gemini_client(cfg),
        "beats": lambda: stages._beats_client(cfg),
        "codex": lambda: stages._codex_client(cfg),
        "intro": lambda: explore._intro_model(cfg),
    }


EXPECTED_SITES = {  # site -> how many models it builds, and their efforts
    "query models": ["low", "medium", "low", "low", "medium"],
    "extraction": ["low"], "adjudication": ["low"], "narrator": ["low"],
    "disambiguation": ["low"], "beats": ["medium"], "codex": ["medium"], "intro": ["low"],
}


def test_multi_mode_claude_user_gets_claude_at_every_site(clean_env, monkeypatch, built):
    monkeypatch.setenv("LORE_MULTI_USER", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", SERVER_CLAUDE)  # the server's: never used
    monkeypatch.setenv("GEMINI_API_KEY", SERVER_GEMINI)
    cfg = _config(clean_env)
    scope = KeyScope("alice", ALICE_CLAUDE, provider="anthropic",
                     model=roles.CLAUDE_OPUS, fast_model=roles.CLAUDE_HAIKU)
    with key_scope.bound(scope):
        for name, make in _sites(cfg).items():
            built.clear()
            assert make() is not None, name
            assert {b[0] for b in built} == {"AnthropicTextModel"}, name
            assert {b[1] for b in built} == {ALICE_CLAUDE}, name
            assert [b[3] for b in built] == EXPECTED_SITES[name], name
    # roles: the writer sites on Opus, the fast ones on Haiku (the faithfulness
    # check is a writer site: the fast model refused verbatim-quoted claims)
    with key_scope.bound(scope):
        built.clear()
        _sites(cfg)["query models"]()
    assert [b[2] for b in built] == [roles.CLAUDE_HAIKU, roles.CLAUDE_OPUS,
                                     roles.CLAUDE_HAIKU, roles.CLAUDE_OPUS,
                                     roles.CLAUDE_OPUS]


def test_multi_mode_openai_user_gets_their_model(clean_env, monkeypatch, built):
    monkeypatch.setenv("LORE_MULTI_USER", "1")
    cfg = _config(clean_env)
    scope = KeyScope("bob", BOB_OPENAI, provider="openai", model="gpt-9-mini")
    with key_scope.bound(scope):
        for name, make in _sites(cfg).items():
            built.clear()
            make()
            assert {(b[0], b[1], b[2]) for b in built} == {
                ("OpenAITextModel", BOB_OPENAI, "gpt-9-mini")}, name


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_a_user_without_a_key_never_falls_back(clean_env, monkeypatch, built, provider):
    monkeypatch.setenv("LORE_MULTI_USER", "1")
    monkeypatch.setenv("GEMINI_API_KEY", SERVER_GEMINI)
    monkeypatch.setenv("ANTHROPIC_API_KEY", SERVER_CLAUDE)
    cfg = _config(clean_env)
    with key_scope.bound(KeyScope("carol", None, provider=provider, model="m")):
        for name, make in _sites(cfg).items():
            if name == "intro":
                assert make() is None  # no intro, nothing built
                continue
            with pytest.raises(Exception) as caught:
                make()
            chain = caught.value
            while chain is not None and not isinstance(chain, KeyRequired):
                chain = chain.__cause__ or chain.__context__
            assert isinstance(chain, KeyRequired) or "Key" in type(caught.value).__name__, name
    assert built == []


def test_multi_mode_without_a_scope_reads_no_server_key(clean_env, monkeypatch):
    monkeypatch.setenv("LORE_MULTI_USER", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", SERVER_CLAUDE)
    with pytest.raises(KeyRequired):
        factory.local_key("anthropic")
    with pytest.raises(KeyRequired):
        factory.credentials(_config(clean_env, provider="anthropic"))


def test_local_mode_reads_the_env_then_the_key_file(clean_env, monkeypatch, built):
    cfg = _config(clean_env, provider="anthropic")
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        factory.credentials(cfg)
    path = clean_env / ".config" / "lore" / "anthropic.env"
    path.parent.mkdir(parents=True)
    path.write_text(f"ANTHROPIC_API_KEY={ALICE_CLAUDE}\n")
    path.chmod(0o644)
    with pytest.raises(gemini.GeminiKeyFileError):
        factory.credentials(cfg)
    path.chmod(0o600)
    creds = factory.credentials(cfg)
    assert (creds.provider, creds.key, creds.model) == ("anthropic", ALICE_CLAUDE, None)
    assert ALICE_CLAUDE not in repr(creds)
    monkeypatch.setenv("ANTHROPIC_API_KEY", SERVER_CLAUDE)
    assert factory.credentials(cfg).key == SERVER_CLAUDE  # the environment first
    for name, make in _sites(cfg).items():
        built.clear()
        make()
        assert {(b[0], b[2]) for b in built} == {
            ("AnthropicTextModel", roles.CLAUDE_SONNET)}, name


def test_local_openai_needs_a_model(clean_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", BOB_OPENAI)
    creds = factory.credentials(_config(clean_env, provider="openai"))
    with pytest.raises(ValueError, match="choose an OpenAI model"):
        factory.build(creds, _config(clean_env), "write", gemini_models=[])


def test_local_mode_default_is_gemini(clean_env, monkeypatch, built):
    monkeypatch.setenv("GEMINI_API_KEY", SERVER_GEMINI)
    monkeypatch.setenv("ANTHROPIC_API_KEY", SERVER_CLAUDE)  # ignored: gemini chosen
    cfg = _config(clean_env)
    for name, make in _sites(cfg).items():
        built.clear()
        make()
        assert {b[0] for b in built} == {"GeminiClient"}, name


def test_unknown_claude_model_and_effort_override(clean_env):
    creds = factory.Credentials("anthropic", ALICE_CLAUDE, "claude-sonnet-4-6")
    with pytest.raises(ValueError, match="not one of the Claude models"):
        factory.build(creds, _config(clean_env), "write", gemini_models=[])
    cfg = _config(clean_env, provider="anthropic", effort={"write": "high"})
    assert factory.effort_for(cfg, "write") == "high"
    assert factory.effort_for(cfg, "check") == "low"
    with pytest.raises(ValueError):
        factory.build(factory.Credentials("anthropic", "k"), cfg, "nowhere",
                      gemini_models=[])


# --- budgets: Gemini's ledger only for Gemini; the token cap for the others ------

def test_the_gemini_ledger_does_not_gate_claude(clean_env, monkeypatch):
    from lore.db.sqlite import open_db
    from lore.mcp.tools import _check_synthesis_budget
    from lore.orchestrator.gemini_budget import record_call, today

    cfg = _config(clean_env)
    conn = open_db(cfg.sqlite_path)
    for model in cfg.synthesis.models:
        for _ in range(500):
            record_call(conn, model, today())
    conn.close()
    monkeypatch.setenv("GEMINI_API_KEY", SERVER_GEMINI)
    assert _check_synthesis_budget(cfg) is not None  # Gemini: spent
    capped = [False]
    scope = KeyScope("alice", ALICE_CLAUDE, provider="anthropic",
                     over_cap=lambda: capped[0])
    with key_scope.bound(scope):
        assert _check_synthesis_budget(cfg) is None
        capped[0] = True
        assert "token cap" in _check_synthesis_budget(cfg)


def test_processing_spend_check_uses_the_cap_not_the_reserve(clean_env):
    from lore.db.sqlite import open_db
    from lore.orchestrator.gemini_budget import record_call, today
    from lore.orchestrator.pipeline import JobCtl, StageContext
    from lore.orchestrator.pipeline import QuotaStop

    cfg = _config(clean_env)
    conn = open_db(cfg.sqlite_path)
    for model in cfg.extraction.gemini_models:
        for _ in range(500):
            record_call(conn, model, today())
    ctl = JobCtl(StageContext(conn, cfg, "book"))
    with pytest.raises(QuotaStop):
        ctl.spend_check()  # Gemini: the reserve is reached
    capped = [False]
    with key_scope.bound(KeyScope("a", ALICE_CLAUDE, provider="anthropic",
                                  over_cap=lambda: capped[0])):
        ctl.spend_check()  # Claude: no ledger
        capped[0] = True
        with pytest.raises(QuotaStop, match="token cap"):
            ctl.spend_check()
    conn.close()


def test_a_refusal_is_a_friendly_model_refused_error():
    from lore.mcp.tools import _upstream
    out = _upstream(ProviderRefusal("x", provider="anthropic", model="m", category="cyber"))
    assert out["error"]["code"] == "model_refused"
    assert "Claude" in out["error"]["message"]


def test_claude_answers_a_question_end_to_end(clean_env, monkeypatch):
    """make_query_models with Claude credentials routes each call to its site."""
    from lore.query.agent import make_query_models

    calls = []

    class Reply(BaseModel):
        ok: bool

    async def create(**kwargs):
        calls.append((kwargs["model"], kwargs["output_config"]["effort"]))
        return NS(content=[NS(type="text", text=json.dumps({"ok": True}))],
                  stop_reason="end_turn", stop_details=None,
                  usage=NS(input_tokens=1, output_tokens=1))

    class Client:
        messages = NS(create=create)

        async def close(self):
            pass

    real = AnthropicTextModel.__init__

    def init(self, api_key, **kwargs):
        real(self, api_key, client=Client(), **kwargs)
    monkeypatch.setattr(AnthropicTextModel, "__init__", init)
    creds = factory.Credentials("anthropic", ALICE_CLAUDE, roles.CLAUDE_SONNET,
                                roles.CLAUDE_HAIKU)
    models = make_query_models(_config(clean_env), credentials=creds)

    async def go():
        for step in (models.select, models.decompose, models.check, models.synthesize,
                     models.write):
            assert (await step("p", Reply)).ok
        await models.aclose()
    asyncio.run(go())
    assert calls == [(roles.CLAUDE_HAIKU, "low"), (roles.CLAUDE_HAIKU, "low"),
                     (roles.CLAUDE_SONNET, "low"), (roles.CLAUDE_SONNET, "medium"),
                     (roles.CLAUDE_SONNET, "medium")]
