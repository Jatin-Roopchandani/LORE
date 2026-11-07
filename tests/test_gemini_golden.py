"""Gemini requests stay byte-identical through the provider layer.

The golden file was recorded from the code BEFORE the provider layer existed
(Claude and OpenAI support). Every Gemini construction site is driven through
a stubbed transport, and every request it sends (URL, the two headers, the raw
body bytes) must equal the recording, in local mode (the key from the
environment) and in multi mode (a bound user's key). Regenerate only on a
deliberate Gemini change: LORE_WRITE_GOLDEN=1 pytest tests/test_gemini_golden.py
"""
import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from lore.config import LoreConfig
from lore.inference import key_scope
from lore.inference.key_scope import KeyScope

GOLDEN = Path(__file__).parent / "golden" / "gemini_requests.json"
KEY = "AIzaGOLDENgoldenGOLDENgoldenGOLDENgold0001"


class G(BaseModel):
    ok: bool
    note: str | None = None
    items: list[str] = []


def _reply(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    wants_json = body["generationConfig"].get("responseMimeType") == "application/json"
    text = '{"ok": true}' if wants_json else "yes"
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})


@pytest.fixture
def recorded(monkeypatch):
    seen: list[dict] = []
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({
            "method": request.method,
            "url": str(request.url),
            "x-goog-api-key": request.headers.get("x-goog-api-key"),
            "content-type": request.headers.get("content-type"),
            "body": request.content.decode(),
        })
        return _reply(request)

    def fake(*args, transport=None, timeout=None, **kwargs):
        return real(transport=httpx.MockTransport(handler), timeout=timeout)

    monkeypatch.setattr(httpx, "AsyncClient", fake)

    async def no_wait(self, model):  # RPM spacing changes timing, not bytes
        return None
    from lore.inference.gemini import GeminiClient
    monkeypatch.setattr(GeminiClient, "_throttle", no_wait)
    return seen


def _config(tmp_path) -> LoreConfig:
    cfg = LoreConfig(data_dir=tmp_path / "data", extraction_provider="gemini",
                     selection={"provider": "gemini"}, synthesis={"provider": "gemini"},
                     adjudication={"provider": "gemini"},
                     disambiguation={"enabled": True})
    cfg.ensure_dirs()
    return cfg


async def _drive(cfg: LoreConfig) -> None:
    from lore.graph.adjudicate import _adjudication_client
    from lore.mcp.tools import _build_models
    from lore.orchestrator import stages
    from lore.orchestrator.local import make_gemini_tier
    from lore.query import explore

    models = _build_models(cfg)
    try:
        await models.select("select: who is Watson?", G)
        await models.synthesize("synthesize: claims", G)
        await models.decompose("decompose: parts", G)
        await models.check("check: faithful?", G)
        await models.write("write: prose", G)
    finally:
        await models.aclose()

    tier = make_gemini_tier(cfg)
    try:
        await tier.client.generate_json("extract: chunk", G, exhausted_hint="hint")
    finally:
        await tier.aclose()

    adjudicator = _adjudication_client(cfg, None)
    await adjudicator.generate("adjudicate: same?", exhausted_hint="hint")
    await adjudicator.generate_json("narrator: who?", G, exhausted_hint="hint")
    await adjudicator.aclose()

    for build, call in ((stages._gemini_client, "generate"),
                        (stages._beats_client, "generate_json"),
                        (stages._codex_client, "generate_json")):
        client = build(cfg)
        if call == "generate":
            await client.generate(f"{build.__name__}: text")
        else:
            await client.generate_json(f"{build.__name__}: json", G,
                                       exhausted_hint="Continue the job")
        await client.aclose()

    intro = explore._intro_model(cfg)
    assert intro is not None
    generate_json, aclose, _model = intro
    await generate_json("intro: entity", G)
    await aclose()


def _check(seen: list[dict]) -> None:
    if os.environ.get("LORE_WRITE_GOLDEN"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(seen, indent=1) + "\n")
    golden = json.loads(GOLDEN.read_text())
    assert len(seen) == len(golden)
    for got, want in zip(seen, golden):
        assert got == want


def test_local_mode_gemini_requests_are_unchanged(tmp_path, monkeypatch, recorded):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LORE_MULTI_USER", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    asyncio.run(_drive(_config(tmp_path)))
    _check(recorded)


def test_multi_mode_gemini_requests_are_unchanged(tmp_path, monkeypatch, recorded):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LORE_MULTI_USER", "1")
    cfg = _config(tmp_path)
    with key_scope.bound(KeyScope("alice", KEY)):
        asyncio.run(_drive(cfg))
    _check(recorded)


def test_a_paid_gemini_plan_sends_the_same_requests(tmp_path, monkeypatch, recorded):
    # The switch changes LORE's budgeting, never what is sent to Google.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LORE_MULTI_USER", "1")
    cfg = _config(tmp_path)
    with key_scope.bound(KeyScope("alice", KEY, paid_tier=True)):
        asyncio.run(_drive(cfg))
    _check(recorded)
