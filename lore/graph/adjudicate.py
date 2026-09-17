"""Embedding and merge adjudication for resolution.

Embeddings are always local (nomic-embed-text on Ollama, DOCUMENT_PREFIX).
Adjudication, the resolver's pass-3 yes/no on a borderline merge, runs on
the local 3B (`make_ollama_pair`, the code default) or on Gemini
(`make_gemini_pair`, `[lore.adjudication] provider = "gemini"`). Both send
the same `_TEMPLATE` prompt and read the verdict the same way.

Gemini answers are cached on disk (`AdjudicationCache`), because reloading
a book is the common operation and must spend zero requests on questions
already answered.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from lore.config import LoreConfig
from lore.extract.embed import DOCUMENT_PREFIX
from lore.inference.gemini import (
    GeminiClient,
    default_key_file,
    resolve_api_key,
)
from lore.inference.ollama import OllamaProvider
from lore.resolve.disambiguation import _atomic_write
from lore.resolve.narrator import (
    DetectNarrator,
    NarratorAnswer,
    NarratorCache,
    make_narrator_detector,
)

_TEMPLATE = """You are matching characters across two novels by the same author.

Example: A: "the Doctor" (Character). Also called: the doctor. B: "the Doctor" (Character). evidence: The doctor examined the wound. Even though the same title appears on both sides, they are NOT the same person, because "the Doctor" is a generic role, not a distinctive name. Correct answer: no.

A: "{a_name}" ({a_kind}). Also called: {a_aliases}.
B: "{b_name}" ({b_kind}). evidence: {b_evidence}

Do A and B refer to the same person? A shared distinctive name is strong
evidence they do. A shared generic title (like "the Doctor" above) is not.

Answer yes or no."""


def render_prompt(canonical, mention) -> str:
    """The adjudication prompt for one pair, identical for every provider."""
    return _TEMPLATE.format(
        # A is a CanonicalEntity: it carries no context, only the
        # normalized surface forms already merged into it. Say that,
        # rather than labelling them "evidence" the model never got.
        a_name=canonical.name, a_kind=canonical.kind,
        a_aliases=", ".join(canonical.aliases) or "(none)",
        b_name=mention.name, b_kind=mention.kind,
        b_evidence=mention.context or "(none)",
    )


def parse_verdict(answer: str) -> bool:
    """Yes means merge; anything else, prose included, means do not."""
    return answer.strip().lower().startswith("y")


def _embedder(config: LoreConfig, provider):
    async def embed(texts: list[str]) -> list[list[float]]:
        return await provider.embed(
            [DOCUMENT_PREFIX + text for text in texts],
            model=config.extraction.embed_model_ollama,
        )

    return embed


def make_ollama_pair(config: LoreConfig):
    """Return (embed, adjudicate, aclose) backed by one Ollama client."""
    provider = OllamaProvider(config.ollama)
    embed = _embedder(config, provider)

    async def adjudicate(canonical, mention) -> bool:
        prompt = render_prompt(canonical, mention)
        response = await provider._client.post(
            "/api/chat",
            # temperature 0: Ollama defaults to 0.8, which would make a
            # merge decision — and therefore the graph — a sample rather
            # than a function of its inputs. temperature 0 alone was not
            # enough to make this reproducible in practice: back-to-back
            # calls with different prompts could still flip a borderline
            # verdict (observed on "the Captain" vs "the Captain"), most
            # likely from batching-order floating-point noise in the
            # local server. A fixed seed removes that remaining source
            # of variance.
            json={"model": config.ollama.model,
                  "messages": [{"role": "user", "content": prompt}],
                  "options": {"temperature": 0, "seed": 0},
                  "stream": False},
        )
        response.raise_for_status()
        return parse_verdict(response.json()["message"]["content"])

    async def aclose() -> None:
        await provider.aclose()

    return embed, adjudicate, aclose


class AdjudicationCache:
    """Answers already paid for: `<dir>/answers.json`.

    The same store pattern as `lore.resolve.disambiguation.DisambiguationStore`:

      * Key: sha256 of the provider name and the fully RENDERED prompt. The
        prompt carries both names, kinds, the canonical's aliases and the
        evidence sentence, so any change to what the model would be shown
        (a re-extraction, a different merge upstream) is a different key,
        and a stale answer can never be applied to a changed question.
        Model ids are not in the key: within a provider, the cascade's
        models are interchangeable capacity, not different questions.
      * Value: {"merge": bool, "model": id}. No prompt text, no key.
      * Writes are atomic (write-temp, `os.replace`) and merge with what is
        on disk, so a concurrent writer's answers survive.
      * One entry is written per COMPLETED answer, as soon as it arrives.
        A quota or key failure mid-load therefore leaves every answer
        received so far (each a whole, verified answer to its exact prompt,
        so the next attempt does not pay for it twice) and nothing at all
        for the question that failed: never a default "no".
      * A missing, corrupt or wrong-typed file or entry reads as "not
        cached", and the next save repairs it.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self._answers: dict[str, dict] | None = None

    @property
    def path(self) -> Path:
        return self.directory / "answers.json"

    @staticmethod
    def key(provider: str, prompt: str) -> str:
        return hashlib.sha256(f"{provider}\x1f{prompt}".encode("utf-8")).hexdigest()

    def _read(self) -> dict[str, dict]:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            str(key): value for key, value in raw.items()
            if isinstance(value, dict) and isinstance(value.get("merge"), bool)
        }

    def get(self, key: str) -> bool | None:
        if self._answers is None:
            self._answers = self._read()
        entry = self._answers.get(key)
        return None if entry is None else entry["merge"]

    def put(self, key: str, merge: bool, model: str | None) -> None:
        merged = self._read()
        merged[key] = {"merge": merge, "model": model or ""}
        _atomic_write(self.path, merged)
        self._answers = merged


ADJUDICATION_EXHAUSTED_HINT = (
    "The graph load (merge adjudication) was stopped before anything was"
    " written; every answer already received is cached, so re-run"
    " `lore process <book_id> --skip-extract` when a daily budget resets."
)


def _adjudication_client(config: LoreConfig, api_key: str | None) -> GeminiClient:
    """A client on `[lore.adjudication]`'s cascade, or the clear no-key refusal."""
    key = api_key or resolve_api_key(key_file=default_key_file())
    if not key:
        raise ValueError(
            '[lore.adjudication] provider = "gemini" but no API key: set'
            " GEMINI_API_KEY (or GOOGLE_API_KEY), or put one line"
            " `GEMINI_API_KEY=...` in ~/.config/lore/gemini.env with mode"
            ' 600. Or set provider = "ollama" to adjudicate locally.'
        )
    return GeminiClient(key, models=config.adjudication.models)


def make_gemini_pair(
    config: LoreConfig,
    api_key: str | None = None,
    *,
    client: GeminiClient | None = None,
    embedder=None,
    cache: AdjudicationCache | None = None,
):
    """Return (embed, adjudicate, aclose): local embeddings, Gemini verdicts.

    `client`, `embedder` and `cache` are for tests. Without a client this
    needs a key (argument, environment, or ~/.config/lore/gemini.env) and
    raises ValueError before opening anything.

    A GeminiError from the request (every model refused, key rejected,
    network, empty answer) propagates and fails the load: an unanswered
    question must never read as "no", because a silent "no" quietly
    under-merges the graph.
    """
    if client is None:
        client = _adjudication_client(config, api_key)
    local = embedder if embedder is not None else OllamaProvider(config.ollama)
    store = cache if cache is not None else AdjudicationCache(config.adjudication_dir)
    embed = _embedder(config, local)

    async def adjudicate(canonical, mention) -> bool:
        prompt = render_prompt(canonical, mention)
        key = AdjudicationCache.key("gemini", prompt)
        cached = store.get(key)
        if cached is not None:
            return cached
        answer = await client.generate(prompt, exhausted_hint=ADJUDICATION_EXHAUSTED_HINT)
        verdict = parse_verdict(answer)
        store.put(key, verdict, client.last_model)
        return verdict

    async def aclose() -> None:
        try:
            await client.aclose()
        finally:
            await local.aclose()

    return embed, adjudicate, aclose


NARRATOR_EXHAUSTED_HINT = (
    "The graph load (narrator detection) was stopped before anything was"
    " written; re-run `lore process <book_id> --skip-extract` when a daily"
    " budget resets."
)


def make_gemini_narrator(
    config: LoreConfig,
    api_key: str | None = None,
    *,
    client: GeminiClient | None = None,
    cache: NarratorCache | None = None,
) -> tuple[DetectNarrator, object]:
    """Return (detect, aclose): Task 10c's per-book narrator question on Gemini.

    Follows `[lore.adjudication]`: the same cascade and the same no-key
    refusal as `make_gemini_pair`. One `generate_json` request per book and
    opening text, cached in `config.narrators_dir`, so a reload spends zero.
    A GeminiError propagates and fails the load before anything is written.
    """
    if client is None:
        client = _adjudication_client(config, api_key)
    store = cache if cache is not None else NarratorCache(config.narrators_dir)

    async def ask(prompt: str) -> tuple[NarratorAnswer, str | None]:
        answer = await client.generate_json(
            prompt, NarratorAnswer, exhausted_hint=NARRATOR_EXHAUSTED_HINT
        )
        return answer, client.last_model

    async def aclose() -> None:
        await client.aclose()

    return make_narrator_detector(ask, store, "gemini"), aclose
