"""One factory for every Gemini client's key (release step 3, task 3).

Local mode reads the server's key file exactly as before. Multi mode, or any
bound user scope, uses that user's key and NEVER the file or the server's
environment. Every construction site is checked: query models (ask, MCP),
extraction, adjudication, the first-person narrator, disambiguation, beats
and the codex. All offline (a stubbed transport, a spied constructor)."""
import asyncio
import pickle

import httpx
import pytest

from lore.config import LoreConfig
from lore.inference import gemini, key_scope
from lore.inference.gemini import GeminiClient, GeminiError
from lore.inference.key_scope import KeyRequired, KeyScope, gemini_api_key

FILE_KEY = "AIzaSERVERfileSERVERfileSERVERfile12345"   # the server's own key file
ENV_KEY = "AIzaSERVERenvSERVERenvSERVERenvSERVE6789"  # the server's environment
ALICE = "AIzaALICEaliceALICEaliceALICEaliceALI0001"
BOB = "AIzaBOBbobBOBbobBOBbobBOBbobBOBbobBOBb0002"


@pytest.fixture
def server_key_file(tmp_path, monkeypatch):
    """A real ~/.config/lore/gemini.env holding FILE_KEY, and no key in the env."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "LORE_MULTI_USER"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / ".config" / "lore" / "gemini.env"
    path.parent.mkdir(parents=True)
    path.write_text(f"GEMINI_API_KEY={FILE_KEY}\n")
    path.chmod(0o600)
    return path


@pytest.fixture
def file_reads(monkeypatch):
    """Every read of the key file, recorded (and still performed)."""
    reads = []
    real = gemini._key_from_file

    def spy(path):
        reads.append(path)
        return real(path)

    monkeypatch.setattr(gemini, "_key_from_file", spy)
    return reads


class _Built(Exception):
    def __init__(self, key):
        super().__init__("a GeminiClient was built")
        self.key = key


@pytest.fixture
def built(monkeypatch):
    """Stop at GeminiClient(...) and report the key it was given."""
    def spy(self, api_key, **kwargs):
        raise _Built(api_key)
    monkeypatch.setattr(GeminiClient, "__init__", spy)


def _config(tmp_path):
    # Questions on Gemini (lore.toml's choice in practice; the default is local).
    cfg = LoreConfig(data_dir=tmp_path / "data",
                     selection={"provider": "gemini"}, synthesis={"provider": "gemini"})
    cfg.ensure_dirs()
    return cfg


def _sites(cfg):
    """Each construction site, as a call that builds its GeminiClient."""
    from lore.graph.adjudicate import _adjudication_client, make_gemini_first_person
    from lore.mcp.tools import _build_models
    from lore.orchestrator import stages
    from lore.orchestrator.local import make_gemini_tier
    return {
        "query models (ask, MCP)": lambda: _build_models(cfg),
        "extraction": lambda: make_gemini_tier(cfg),
        "adjudication": lambda: _adjudication_client(cfg, None),
        "first-person narrator": lambda: make_gemini_first_person(cfg),
        "disambiguation": lambda: stages._gemini_client(cfg),
        "beats": lambda: stages._beats_client(cfg),
        "codex": lambda: stages._codex_client(cfg),
    }


SITES = ["query models (ask, MCP)", "extraction", "adjudication",
         "first-person narrator", "disambiguation", "beats", "codex"]


# --- local mode: unchanged ------------------------------------------------------------

def test_local_mode_reads_the_key_file(server_key_file, file_reads):
    assert gemini_api_key() == FILE_KEY
    assert file_reads == [server_key_file]


def test_local_mode_still_prefers_the_environment(server_key_file, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", ENV_KEY)
    assert gemini_api_key() == ENV_KEY


def test_an_explicit_key_wins(server_key_file):
    assert gemini_api_key("AIzaEXPLICITexplicitEXPLICITexplicit0000") == \
        "AIzaEXPLICITexplicitEXPLICITexplicit0000"


@pytest.mark.parametrize("site", SITES)
def test_local_mode_every_site_uses_the_key_file(site, tmp_path, server_key_file,
                                                 built):
    with pytest.raises(_Built) as got:
        _sites(_config(tmp_path))[site]()
    assert got.value.key == FILE_KEY


# --- multi mode: the user's key, never the server's ----------------------------------

@pytest.mark.parametrize("site", SITES)
def test_multi_mode_every_site_uses_the_bound_users_key_and_never_the_file(
        site, tmp_path, server_key_file, file_reads, built, monkeypatch):
    monkeypatch.setenv("LORE_MULTI_USER", "1")
    monkeypatch.setenv("GEMINI_API_KEY", ENV_KEY)  # a server env key is no fallback either
    with key_scope.bound(KeyScope("alice", ALICE)):
        with pytest.raises(_Built) as got:
            _sites(_config(tmp_path))[site]()
    assert got.value.key == ALICE
    assert file_reads == []


@pytest.mark.parametrize("site", SITES)
def test_multi_mode_with_no_user_key_refuses_and_never_reads_the_file(
        site, tmp_path, server_key_file, file_reads, built, monkeypatch):
    monkeypatch.setenv("LORE_MULTI_USER", "1")
    monkeypatch.setenv("GEMINI_API_KEY", ENV_KEY)
    with pytest.raises(KeyRequired):
        _sites(_config(tmp_path))[site]()
    assert file_reads == []


@pytest.mark.parametrize("site", SITES)
def test_a_bound_scope_without_a_key_refuses_even_in_local_mode(
        site, tmp_path, server_key_file, file_reads, built):
    # The worker binds an empty scope when it has no key store: never the file.
    with key_scope.bound(KeyScope("alice", None)):
        with pytest.raises(KeyRequired):
            _sites(_config(tmp_path))[site]()
    assert file_reads == []


def test_multi_mode_never_reads_gemini_env_even_when_asked_directly(
        server_key_file, monkeypatch):
    """The guard under the factory: in multi mode the key file cannot be read
    at all, whoever asks."""
    monkeypatch.setenv("LORE_MULTI_USER", "1")
    opened = []
    real_read = type(server_key_file).read_text

    def read_text(self, *a, **k):
        if self.name == "gemini.env":
            opened.append(self)
        return real_read(self, *a, **k)

    monkeypatch.setattr(type(server_key_file), "read_text", read_text)
    with pytest.raises(KeyRequired):
        gemini.resolve_api_key(key_file=server_key_file)
    with pytest.raises(KeyRequired):
        gemini._key_from_file(server_key_file)
    with key_scope.bound(KeyScope("alice", ALICE)):
        assert gemini_api_key() == ALICE
    assert opened == []


def test_the_scope_does_not_outlive_its_block(server_key_file):
    with key_scope.bound(KeyScope("alice", ALICE)):
        assert gemini_api_key() == ALICE
    assert key_scope.current() is None
    assert gemini_api_key() == FILE_KEY


# --- one user's requests use that user's key --------------------------------------------

def _ok(_request):
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})


def test_concurrent_users_each_send_their_own_key():
    seen = []

    def handler(request):
        seen.append(request.headers["x-goog-api-key"])
        return _ok(request)

    async def one(user, key):
        with key_scope.bound(KeyScope(user, key)):
            await asyncio.sleep(0)  # interleave the two users
            client = GeminiClient(gemini_api_key(), models=["m"],
                                  transport=httpx.MockTransport(handler))
            try:
                await client.generate("q")
            finally:
                await client.aclose()

    async def both():
        await asyncio.gather(one("alice", ALICE), one("bob", BOB),
                             one("alice", ALICE))

    asyncio.run(both())
    assert sorted(seen) == sorted([ALICE, BOB, ALICE])


def test_a_task_started_in_the_scope_keeps_its_key():
    async def go():
        with key_scope.bound(KeyScope("alice", ALICE)):
            task = asyncio.create_task(asyncio.sleep(0, result=None))
            inner = asyncio.create_task(_key_later())
        await task
        return await inner

    async def _key_later():
        await asyncio.sleep(0)
        return gemini_api_key()

    assert asyncio.run(go()) == ALICE


# --- what the client tells the scope -------------------------------------------------

def _reply(status, text=""):
    def handler(_request):
        if status == 200:
            return _ok(_request)
        return httpx.Response(status, text=text or '{"error": {}}')
    return handler


@pytest.mark.parametrize("status,text", [
    (401, ""), (403, ""),
    (400, '{"error": {"code": 400, "message": "API key not valid.",'
          ' "details": [{"reason": "API_KEY_INVALID"}]}}'),
])
def test_a_refused_key_marks_the_scope(status, text):
    scope = KeyScope("alice", ALICE)

    async def go():
        with key_scope.bound(scope):
            client = GeminiClient(ALICE, models=["m"],
                                  transport=httpx.MockTransport(_reply(status, text)))
            try:
                with pytest.raises(GeminiError):
                    await client.generate("q")
            finally:
                await client.aclose()

    asyncio.run(go())
    assert scope.rejected is True


@pytest.mark.parametrize("status", [200, 429, 503, 500, 400])
def test_other_answers_do_not(status):
    scope = KeyScope("alice", ALICE)

    async def go():
        with key_scope.bound(scope):
            client = GeminiClient(ALICE, models=["m"],
                                  transport=httpx.MockTransport(_reply(status)))
            try:
                await client.generate("q")
            except GeminiError:
                pass
            finally:
                await client.aclose()

    asyncio.run(go())
    assert scope.rejected is False


def test_every_attempt_is_counted_for_the_bound_user():
    counted = []
    calls = iter([429, 200])

    def handler(request):
        status = next(calls)
        return _ok(request) if status == 200 else httpx.Response(429)

    async def go():
        with key_scope.bound(KeyScope("alice", ALICE, on_request=counted.append)):
            client = GeminiClient(ALICE, models=["a", "b"],
                                  transport=httpx.MockTransport(handler))
            try:
                await client.generate("q")
            finally:
                await client.aclose()

    asyncio.run(go())
    assert counted == ["a", "b"]


def test_a_rejection_outside_any_scope_changes_nothing():
    async def go():
        client = GeminiClient(ALICE, models=["m"], transport=httpx.MockTransport(_reply(401)))
        try:
            with pytest.raises(GeminiError):
                await client.generate("q")
        finally:
            await client.aclose()
    asyncio.run(go())  # local mode: no scope, no error from the hook


def test_the_scope_never_shows_its_key():
    scope = KeyScope("alice", ALICE)
    assert ALICE not in repr(scope) and ALICE not in str(scope)
    assert ALICE not in repr(KeyRequired()) and ALICE not in str(KeyRequired())
    with pytest.raises(TypeError):
        pickle.dumps(scope)
