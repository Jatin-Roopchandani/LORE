"""Which model and how much effort each LORE model call gets, per provider.

ROLES. Every call site has a role:
- `fast`: short structured steps (template selection, decomposition, the
  faithfulness check, extraction, adjudication, the narrator, disambiguation,
  beats, codex, the entity intro);
- `writer`: the claims an answer is built from and the prose itself, which
  is also what interpretive and deep answers use.

Gemini keeps its own measured cascades per site (lore.config), unchanged.
Claude: the user's model for both roles (default `claude-sonnet-5-5`), or a
separate `fast` model. OpenAI: the user picks from their key's model list;
`suggest_openai_model` proposes the cheapest general chat model it sees.

EFFORT (Claude only; see `anthropic_request_options`). `low` for the cheap
structured steps, `medium` for writing, and for beats and codex, which
summarise a whole chapter or entity. These are starting values, not
measurements: no Claude eval has been run yet. Each site can be overridden
in `[lore.llm] effort = {site = "high"}`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

FAST = "fast"
WRITER = "writer"

EFFORTS = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class Site:
    role: str
    effort: str


SITES: dict[str, Site] = {
    "select": Site(FAST, "low"),
    "decompose": Site(FAST, "low"),
    "check": Site(FAST, "low"),
    "synthesize": Site(WRITER, "medium"),
    "write": Site(WRITER, "medium"),
    "extract": Site(FAST, "low"),
    "adjudicate": Site(FAST, "low"),
    "narrator": Site(FAST, "low"),
    "disambiguate": Site(FAST, "low"),
    # Whether two names are one thing: a few requests per book, and a wrong
    # "same" merges two things for good, so the writer model answers.
    "synonyms": Site(WRITER, "low"),
    "beats": Site(FAST, "medium"),
    "codex": Site(FAST, "medium"),
    "intro": Site(FAST, "low"),
}

# --- Claude ----------------------------------------------------------------------

CLAUDE_SONNET = "claude-sonnet-5-5"
CLAUDE_OPUS = "claude-opus-5-5"
CLAUDE_HAIKU = "claude-haiku-5-5"
ANTHROPIC_MODELS = (CLAUDE_SONNET, CLAUDE_OPUS, CLAUDE_HAIKU)  # exact ids, no dates
ANTHROPIC_DEFAULT = CLAUDE_SONNET

# US dollars per million tokens (input, output), from Anthropic's price list.
ANTHROPIC_PRICES: dict[str, tuple[float, float]] = {
    CLAUDE_OPUS: (4.00, 20.00),
    CLAUDE_SONNET: (2.00, 10.00),
    CLAUDE_HAIKU: (0.10, 0.50),
}


def anthropic_request_options(model: str, effort: str) -> dict:
    """`thinking` and `output_config.effort` for one Claude request.

    - Opus 5.5: thinking cannot be switched off (`disabled` is a 400), so
      `thinking` is omitted and the effort is always sent (its default is
      medium, not high).
    - Sonnet 5.5: `disabled` is a 400. A `low` step turns thinking off the
      supported way, `{"type": "between_tools"}` with no other field (allowed
      at effort high or below); other steps leave thinking adaptive.
    - Haiku 5.5: adaptive by default (default effort medium); the effort is
      sent explicitly.
    No `temperature`, `top_p` or `top_k`: those are a 400 on current models.
    """
    if effort not in EFFORTS:
        raise ValueError(f"unknown effort {effort!r}")
    options: dict = {"output_config": {"effort": effort}}
    if model == CLAUDE_SONNET and effort == "low":
        options["thinking"] = {"type": "between_tools"}
    return options


def anthropic_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    price = ANTHROPIC_PRICES.get(model)
    if price is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


# --- Gemini (paid plan) -------------------------------------------------------------

# US dollars per million tokens (input text, output including thinking), Google's
# Standard paid tier, from https://ai.google.dev/gemini-api/docs/pricing (now
# served at aistudio.google.com/docs/pricing), read on 2026-11-07. Each model
# lists (first day the price applies, input, output); "" is "since before
# LORE read it". Only the models LORE's cascades use (lore.inference.gemini);
# a model with no verified price (gemini-3.7-flash is not on the page) is left
# out, so its usage shows tokens only. Never guess a price here. A free-tier
# key is not billed at all: these apply only to a user on a paid Gemini plan.
GEMINI_PRICES: dict[str, tuple[tuple[str, float, float], ...]] = {
    "gemini-3.1-flash-lite": (("", 0.25, 1.50),),
    "gemini-3.5-flash-lite": (("", 0.30, 2.50),),
    # Announced on the page: the launch price through 2026-12-31, then double.
    "gemini-3.8-flash": (("", 0.75, 3.75), ("2027-01-01", 1.50, 7.50)),
    "gemini-3.6-flash": (("", 0.75, 3.75), ("2027-01-01", 1.50, 7.50)),
}


def _pacific_day() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")


def gemini_price(model: str, day: str | None = None) -> tuple[float, float] | None:
    """(input, output) dollars per million tokens on `day` (YYYY-MM-DD, Pacific;
    default today), or None when LORE has no verified price for `model`."""
    schedule = GEMINI_PRICES.get(model)
    if schedule is None:
        return None
    day = day or _pacific_day()
    price = None
    for since, tokens_in, tokens_out in schedule:
        if since <= day:
            price = (tokens_in, tokens_out)
    return price


def gemini_cost(model: str, input_tokens: int, output_tokens: int,
                day: str | None = None) -> float | None:
    price = gemini_price(model, day)
    if price is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


# --- OpenAI ----------------------------------------------------------------------

# Not general chat models: embeddings, speech, images, moderation, search
# and realtime variants. Matched anywhere in the id.
_OPENAI_EXCLUDE = ("embedding", "tts", "whisper", "dall-e", "image", "audio",
                   "realtime", "transcribe", "moderation", "search", "instruct",
                   "babbage", "davinci", "computer-use", "codex", "deep-research")
_OPENAI_CHAT = re.compile(r"^(gpt-|chatgpt-|o\d)")
_DATED = re.compile(r"-\d{4}-\d{2}-\d{2}$|-\d{4}$")


def openai_chat_models(ids: list[str]) -> list[str]:
    """The ids that look like general chat models, sorted."""
    return sorted(i for i in set(ids)
                  if _OPENAI_CHAT.match(i) and not any(x in i for x in _OPENAI_EXCLUDE))


def suggest_openai_model(ids: list[str]) -> str | None:
    """The cheapest general chat model in the key's list, by name alone.

    The heuristic, in order: a `nano` model before a `mini` model before any
    other; an undated alias before a dated snapshot; then the newest-looking
    id (the greatest by natural sort, so gpt-10 beats gpt-9). It never
    guesses prices: the user sees the suggestion and can pick another.
    """
    chat = openai_chat_models(ids)
    if not chat:
        return None

    def natural(text: str):
        return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", text)]

    def rank(model_id: str):
        tier = 0 if "nano" in model_id else 1 if "mini" in model_id else 2
        return (tier, bool(_DATED.search(model_id)))

    best = min(rank(i) for i in chat)
    return max((i for i in chat if rank(i) == best), key=natural)
