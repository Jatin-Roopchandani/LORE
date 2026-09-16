"""The one LLM call in Phase 2B: deciding whether two entities are one.

Both properties here are about the *decision*, not the plumbing: the model
must not be told it is looking at evidence it was never shown, and a merge
must not depend on a sampling temperature.
"""
import json
from unittest.mock import patch

import httpx
import pytest

from lore.config import LoreConfig
from lore.graph.adjudicate import _TEMPLATE, make_ollama_pair
from lore.inference.ollama import OllamaProvider
from lore.resolve.resolver import CanonicalEntity, Mention


def _provider_factory(handler):
    """An OllamaProvider whose transport is `handler`, no socket opened."""

    class _MockProvider(OllamaProvider):
        def __init__(self, config) -> None:
            self._model = config.model
            self._client = httpx.AsyncClient(
                transport=httpx.MockTransport(handler), base_url=config.host
            )

    return _MockProvider


async def _ask(handler) -> bool:
    """Run one adjudication against a mocked Ollama and return its verdict."""
    with patch("lore.graph.adjudicate.OllamaProvider", _provider_factory(handler)):
        _embed, adjudicate, aclose = make_ollama_pair(LoreConfig.load())
    try:
        return await adjudicate(
            CanonicalEntity(id="e1", name="Governor Hale", kind="Character",
                            aliases=("governor hale", "hale"),
                            book_ids=("b1",)),
            Mention(name="Hale", kind="Character", book_id="b2",
                    context="Hale signed the order that morning."),
        )
    finally:
        await aclose()


@pytest.mark.asyncio
async def test_prompt_does_not_claim_to_show_evidence_it_has_not_got() -> None:
    """A-side is passed the canonical's aliases; the label must say so.

    `CanonicalEntity` carries no context, so labelling that field
    `evidence:` showed the model "governor hale" where it promised a
    sentence — an asymmetric, near-empty prompt for the phase's only
    merge decision.

    The label itself is "Also called:", not "other names it is known
    by:" as an earlier draft of this fix used: verified against the live
    model (see task-1e-report.md), the literal word "names" in this line
    made the model merge two unrelated "the Captain"s, regardless of the
    surrounding no-generic-titles instruction. "Also called" is equally
    honest about what the field contains and does not carry that failure.
    """
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["prompt"] = json.loads(request.content)["messages"][0]["content"]
        return httpx.Response(200, json={"message": {"content": "no"}})

    assert await _ask(handler) is False
    prompt = seen["prompt"]
    a_line = next(line for line in prompt.splitlines() if line.startswith("A:"))
    b_line = next(line for line in prompt.splitlines() if line.startswith("B:"))
    assert "evidence:" not in a_line, a_line
    # It must say what it is actually showing: the merged surface forms.
    assert "called" in a_line.lower(), a_line
    assert "governor hale, hale" in a_line
    # The B side really is evidence, and keeps its label.
    assert "evidence:" in b_line and "signed the order" in b_line


@pytest.mark.asyncio
async def test_adjudication_is_asked_at_temperature_zero() -> None:
    """Ollama defaults to 0.8; merge decisions must not be sampled."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": "yes"}})

    assert await _ask(handler) is True
    assert seen["body"]["options"]["temperature"] == 0


def test_prompt_frames_the_decision_across_books() -> None:
    """The high-bar case (which is what actually reaches adjudication) is
    cross-book. Saying "a book" describes the opposite situation and is
    what made the model refuse two identical names. The criterion that
    separates Holmes/Holmes from Captain/Captain must also be stated, or
    the model has nothing to reason from.
    """
    rendered = _TEMPLATE.format(
        a_name="Holmes", a_kind="Character", a_aliases="holmes",
        b_name="Holmes", b_kind="Character", b_evidence="He examined the body.",
    )
    low = rendered.lower()
    assert "a book" not in low
    assert "distinctive" in low
    assert "generic" in low


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply, expected",
    [
        ("Yes", True),
        ("yes", True),
        ("yes.", True),
        (" YES", True),
        ("no", False),
        ("No", False),
        ("", False),
    ],
)
async def test_adjudicate_parses_yes_no_replies(reply: str, expected: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": reply}})

    assert await _ask(handler) is expected


# --- Task R1: the Gemini adjudicator and its answer cache -------------------

from lore.extract.embed import DOCUMENT_PREFIX  # noqa: E402
from lore.graph.adjudicate import (  # noqa: E402
    AdjudicationCache,
    make_gemini_pair,
    render_prompt,
)
from lore.inference.gemini import (  # noqa: E402
    LITE_MODELS,
    GeminiClient,
    GeminiQuotaError,
)

SECRET = "AIza-not-a-real-key-0000"
CANONICAL = CanonicalEntity(id="e1", name="Governor Hale", kind="Character",
                            aliases=("governor hale", "hale"), book_ids=("b1",))
MENTION = Mention(name="Hale", kind="Character", book_id="b1",
                  context="Hale signed the order that morning.")
OTHER = Mention(name="Hales", kind="Character", book_id="b1",
                context="The Hales were a family of farmers.")


class _Embedder:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str]] = []
        self.closed = False

    async def embed(self, texts, model):
        self.calls.append((list(texts), model))
        return [[0.0] * 768 for _ in texts]

    async def aclose(self):
        self.closed = True


def _gemini(handler) -> GeminiClient:
    async def no_sleep(_s: float) -> None:
        return None

    return GeminiClient(SECRET, models=list(LITE_MODELS),
                        transport=httpx.MockTransport(handler), sleep=no_sleep)


def _answering(reply: str, seen: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if seen is not None:
            seen.append(body)
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": reply}]}}]})
    return handler


def _pair(tmp_path, handler, embedder=None, config=None):
    config = config or LoreConfig.load()
    client = _gemini(handler)
    cache = AdjudicationCache(tmp_path / "adjudication")
    embed, adjudicate, aclose = make_gemini_pair(
        config, client=client, embedder=embedder or _Embedder(), cache=cache)
    return client, cache, embed, adjudicate, aclose


@pytest.mark.asyncio
async def test_gemini_is_sent_the_same_template_at_temperature_zero(tmp_path) -> None:
    seen: list = []
    client, _cache, _e, adjudicate, aclose = _pair(tmp_path, _answering("Yes.", seen))
    try:
        assert await adjudicate(CANONICAL, MENTION) is True
    finally:
        await aclose()
    assert len(seen) == 1
    sent = seen[0]["contents"][0]["parts"][0]["text"]
    assert sent == render_prompt(CANONICAL, MENTION)
    assert sent == _TEMPLATE.format(
        a_name="Governor Hale", a_kind="Character", a_aliases="governor hale, hale",
        b_name="Hale", b_kind="Character", b_evidence="Hale signed the order that morning.")
    # Plain text at temperature 0, not the JSON mode.
    assert seen[0]["generationConfig"] == {"temperature": 0}
    assert client._client.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("reply, expected", [
    ("Yes", True), ("yes.", True), (" YES", True), ("No", False), ("no.", False),
    ("They are not the same person.", False),
])
async def test_gemini_verdicts_parse_like_the_local_ones(tmp_path, reply, expected) -> None:
    _c, _cache, _e, adjudicate, aclose = _pair(tmp_path, _answering(reply))
    try:
        assert await adjudicate(CANONICAL, MENTION) is expected
    finally:
        await aclose()


@pytest.mark.asyncio
async def test_embeddings_stay_local_with_the_document_prefix(tmp_path) -> None:
    embedder = _Embedder()
    _c, _cache, embed, _a, aclose = _pair(tmp_path, _answering("no"), embedder)
    try:
        await embed(["Holmes — he examined the body"])
    finally:
        await aclose()
    config = LoreConfig.load()
    assert embedder.calls == [([DOCUMENT_PREFIX + "Holmes — he examined the body"],
                               config.extraction.embed_model_ollama)]
    assert embedder.closed


@pytest.mark.asyncio
async def test_a_reload_spends_zero_requests(tmp_path) -> None:
    seen: list = []
    client, cache, _e, adjudicate, aclose = _pair(tmp_path, _answering("yes", seen))
    try:
        assert await adjudicate(CANONICAL, MENTION) is True
        assert await adjudicate(CANONICAL, MENTION) is True  # same load, cached
    finally:
        await aclose()
    assert client.requests_made == 1
    assert cache.path == tmp_path / "adjudication" / "answers.json"
    stored = json.loads(cache.path.read_text())
    assert len(stored) == 1
    (key, entry), = stored.items()
    assert key == AdjudicationCache.key("gemini", render_prompt(CANONICAL, MENTION))
    assert entry["merge"] is True and entry["model"] == LITE_MODELS[0]
    # No prompt text, no key.
    assert "Hale" not in cache.path.read_text() and SECRET not in cache.path.read_text()

    # A fresh process (new client, new cache object, same directory).
    def must_not_call(request):  # pragma: no cover
        raise AssertionError("a cached question was asked again")

    client2, _c2, _e2, adjudicate2, aclose2 = _pair(tmp_path, must_not_call)
    try:
        assert await adjudicate2(CANONICAL, MENTION) is True
    finally:
        await aclose2()
    assert client2.requests_made == 0


def test_the_cache_key_is_stable_and_names_the_provider() -> None:
    prompt = render_prompt(CANONICAL, MENTION)
    assert AdjudicationCache.key("gemini", prompt) == AdjudicationCache.key("gemini", prompt)
    assert AdjudicationCache.key("gemini", prompt) != AdjudicationCache.key("ollama", prompt)
    assert AdjudicationCache.key("gemini", prompt) != AdjudicationCache.key(
        "gemini", render_prompt(CANONICAL, OTHER))
    assert len(AdjudicationCache.key("gemini", prompt)) == 64


@pytest.mark.asyncio
async def test_quota_exhaustion_mid_load_fails_loudly_and_never_answers_no(tmp_path) -> None:
    calls = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        if calls[0] == 1:
            return httpx.Response(200, json={
                "candidates": [{"content": {"parts": [{"text": "yes"}]}}]})
        return httpx.Response(429, text="RESOURCE_EXHAUSTED")

    _client, cache, _e, adjudicate, aclose = _pair(tmp_path, handler)
    try:
        assert await adjudicate(CANONICAL, MENTION) is True
        before = cache.path.read_text()
        with pytest.raises(GeminiQuotaError) as exc:
            await adjudicate(CANONICAL, OTHER)
    finally:
        await aclose()
    message = str(exc.value)
    assert "adjudicat" in message and "--skip-extract" in message
    assert "disambiguate" not in message
    # The cache holds exactly the one complete answer: no entry, and so no
    # silent "no", for the question that failed.
    assert cache.path.read_text() == before
    assert len(json.loads(before)) == 1
    assert not list(cache.path.parent.glob("*.tmp"))


@pytest.mark.asyncio
async def test_a_rejected_key_is_not_cached_either(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="denied")

    _client, cache, _e, adjudicate, aclose = _pair(tmp_path, handler)
    try:
        with pytest.raises(Exception):
            await adjudicate(CANONICAL, MENTION)
    finally:
        await aclose()
    assert not cache.path.exists()


def test_a_corrupt_cache_reads_as_empty(tmp_path) -> None:
    cache = AdjudicationCache(tmp_path)
    cache.path.parent.mkdir(parents=True, exist_ok=True)
    cache.path.write_text('{"abc": {"merge": tr')
    assert cache.get("abc") is None
    cache.path.write_text('{"abc": {"merge": "yes"}, "def": {"merge": false}}')
    fresh = AdjudicationCache(tmp_path)
    assert fresh.get("abc") is None  # not a bool: not trusted
    assert fresh.get("def") is False


def test_load_book_sync_uses_the_gemini_pair_when_configured(tmp_path, monkeypatch) -> None:
    import lore.cli as cli
    from lore.graph.loader import LoadReport

    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    config = LoreConfig.load(tmp_path / "absent.toml")
    config = config.model_copy(update={
        "data_dir": tmp_path / "data",
        "adjudication": config.adjudication.model_copy(update={"provider": "gemini"}),
    })
    config.ensure_dirs()
    built: dict = {}

    def fake_gemini_pair(cfg, api_key=None, **kwargs):
        built["key"] = api_key

        async def aclose():
            built["closed"] = True
        return "EMBED", "ADJ", aclose

    async def fake_load(cfg, conn, kuzu, lance, book_id, embed, adjudicate, **kwargs):
        built["args"] = (embed, adjudicate)
        return LoadReport(book_id=book_id)

    def no_ollama(cfg):  # pragma: no cover
        raise AssertionError("adjudication is on gemini")

    monkeypatch.setattr(cli, "make_gemini_pair", fake_gemini_pair)
    monkeypatch.setattr(cli, "make_ollama_pair", no_ollama)
    monkeypatch.setattr(cli, "load_book", fake_load)
    cli._load_book_sync(config, None, "b1", api_key="k")
    assert built == {"key": "k", "args": ("EMBED", "ADJ"), "closed": True}
