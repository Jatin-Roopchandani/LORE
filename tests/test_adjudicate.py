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
from lore.graph.adjudicate import make_ollama_pair
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
    assert "names" in a_line.lower(), a_line
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
