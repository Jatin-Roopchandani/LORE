"""Ollama-backed embedding and adjudication for resolution.

Both come from one provider so a load opens a single client. Adjudication
is the only LLM call in the phase and it runs locally, so resolution costs
no Kaggle quota and works offline.
"""
from __future__ import annotations

from lore.config import LoreConfig
from lore.extract.embed import DOCUMENT_PREFIX
from lore.inference.ollama import OllamaProvider

_TEMPLATE = """Two names appear in a book. Decide whether they refer to the \
SAME entity.

A: {a_name} ({a_kind}) — other names it is known by: {a_aliases}
B: {b_name} ({b_kind}) — evidence: {b_evidence}

Answer with one word: yes or no."""


def make_ollama_pair(config: LoreConfig):
    """Return (embed, adjudicate, aclose) backed by one Ollama client."""
    provider = OllamaProvider(config.ollama)

    async def embed(texts: list[str]) -> list[list[float]]:
        return await provider.embed(
            [DOCUMENT_PREFIX + text for text in texts],
            model=config.extraction.embed_model_ollama,
        )

    async def adjudicate(canonical, mention) -> bool:
        prompt = _TEMPLATE.format(
            # A is a CanonicalEntity: it carries no context, only the
            # normalized surface forms already merged into it. Say that,
            # rather than labelling them "evidence" the model never got.
            a_name=canonical.name, a_kind=canonical.kind,
            a_aliases=", ".join(canonical.aliases) or "(none)",
            b_name=mention.name, b_kind=mention.kind,
            b_evidence=mention.context or "(none)",
        )
        response = await provider._client.post(
            "/api/chat",
            # temperature 0: Ollama defaults to 0.8, which would make a
            # merge decision — and therefore the graph — a sample rather
            # than a function of its inputs.
            json={"model": config.ollama.model,
                  "messages": [{"role": "user", "content": prompt}],
                  "options": {"temperature": 0},
                  "stream": False},
        )
        response.raise_for_status()
        answer = response.json()["message"]["content"].strip().lower()
        return answer.startswith("y")

    async def aclose() -> None:
        await provider.aclose()

    return embed, adjudicate, aclose
