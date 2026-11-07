"""Live smoke test: one decomposition and one claims call on each provider.

Marked `live` and skipped by default; nothing here runs without its key in the
environment (GEMINI_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY; for OpenAI
also LORE_LIVE_OPENAI_MODEL, since LORE never assumes an OpenAI model). Run:

    ANTHROPIC_API_KEY=... pytest -m live tests/test_provider_live.py

It spends a handful of real requests per provider.
"""
import asyncio
import os

import pytest

from lore.config import LoreConfig
from lore.inference.gemini import LITE_MODELS
from lore.inference.providers import factory
from lore.query.decompose import plan
from lore.query.models import Evidence, Passage
from lore.query.synthesize import Synthesis, _build_prompt

pytestmark = [pytest.mark.live, pytest.mark.network]

PASSAGE = ("Holmes leaned back in his chair. \"You have been in Afghanistan, I perceive,\" he said"
           " to Watson, who had only just arrived in London after his wound at Maiwand.")


def _credentials(provider: str):
    variable = {"gemini": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
                "openai": "OPENAI_API_KEY"}[provider]
    key = os.environ.get(variable, "").strip()
    if not key:
        pytest.skip(f"{variable} is not set")
    model = None
    if provider == "openai":
        model = os.environ.get("LORE_LIVE_OPENAI_MODEL", "").strip()
        if not model:
            pytest.skip("LORE_LIVE_OPENAI_MODEL is not set")
    return factory.Credentials(provider, key, model)


@pytest.mark.parametrize("provider", ["gemini", "anthropic", "openai"])
def test_decompose_and_claims(provider):
    creds = _credentials(provider)
    config = LoreConfig()

    async def run():
        decomposer = factory.build(creds, config, "decompose", gemini_models=list(LITE_MODELS))
        writer = factory.build(creds, config, "synthesize", gemini_models=list(LITE_MODELS))
        try:
            parts = await plan("Where had Watson been before he met Holmes?",
                               decomposer.generate_json, book_titles=["A Study in Scarlet"])
            assert parts is not None
            evidence = Evidence(edges=(), passages=(Passage(1, "c1", "b1", 1, PASSAGE),),
                                truncated=False)
            prompt = _build_prompt("Where had Watson been before he met Holmes?", evidence,
                                   {"b1": "A Study in Scarlet"})
            claims = await writer.generate_json(prompt, Synthesis)
            assert isinstance(claims, Synthesis)
        finally:
            await decomposer.aclose()
            await writer.aclose()

    asyncio.run(run())
