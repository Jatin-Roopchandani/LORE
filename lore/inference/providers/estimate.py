"""A rough token estimate for processing one book on a paid provider.

THIS IS AN ESTIMATE, NOT A MEASUREMENT. No book has been processed on Claude
or OpenAI yet, so the numbers below come from the prompt sizes in the code,
not from traces. Replace them with measured per-chunk averages (the traces'
token counts) once a real book has been processed on each provider.

The shape: one extraction request per chunk (the chunk, about 1,600
characters, plus a ~800-character prompt and the strict schema, about 1,200
input tokens in all, and a few hundred output tokens of triplets), and the
later stages (beats, codex, merge adjudication, the narrator, disambiguation)
adding about half again. A typical novel (about 100,000 words, 600,000
characters) is about 420 chunks, so roughly 0.8 million input and 0.35
million output tokens: about 1.2 million tokens in all.
"""
from __future__ import annotations

import os
import sqlite3

from lore.inference.providers import roles
from lore.inference.providers.base import ANTHROPIC, GEMINI

INPUT_PER_CHUNK = 1200
OUTPUT_PER_CHUNK = 500
LATER_STAGES = 1.5
TYPICAL_NOVEL_CHUNKS = 420

# Ask before processing when the estimate is above this (US dollars), or,
# where no price is known (OpenAI), above this many tokens.
CONFIRM_ABOVE_USD = float(os.environ.get("LORE_CONFIRM_ABOVE_USD", "1.0") or 1.0)
CONFIRM_ABOVE_TOKENS = 500_000


def chunk_count(conn: sqlite3.Connection, book_id: str) -> int:
    row = conn.execute("SELECT COUNT(*) FROM chunks WHERE book_id = ?", (book_id,)).fetchone()
    return int(row[0]) if row else 0


def estimate(chunks: int, provider: str, model: str | None) -> dict:
    tokens_in = int(chunks * INPUT_PER_CHUNK * LATER_STAGES)
    tokens_out = int(chunks * OUTPUT_PER_CHUNK * LATER_STAGES)
    cost = (roles.anthropic_cost(model, tokens_in, tokens_out)
            if provider == ANTHROPIC and model else None)
    if provider == GEMINI:
        needs_confirm = False  # free tier: the ledger and the reserve apply instead
    elif cost is not None:
        needs_confirm = cost > CONFIRM_ABOVE_USD
    else:
        needs_confirm = tokens_in + tokens_out > CONFIRM_ABOVE_TOKENS
    return {"provider": provider, "model": model, "chunks": chunks,
            "input_tokens": tokens_in, "output_tokens": tokens_out,
            "estimated_cost_usd": None if cost is None else round(cost, 2),
            "needs_confirm": needs_confirm, "is_estimate": True}


def typical_novel(provider: str, model: str | None) -> dict:
    return estimate(TYPICAL_NOVEL_CHUNKS, provider, model)
