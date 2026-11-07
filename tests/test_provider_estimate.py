"""The rough processing estimate shown before a paid provider starts a book."""
import sqlite3

from lore.inference.providers import estimate as E


def test_a_typical_novel_on_each_provider():
    sonnet = E.typical_novel("anthropic", "claude-sonnet-5-5")
    assert sonnet["is_estimate"] and sonnet["chunks"] == E.TYPICAL_NOVEL_CHUNKS
    total = sonnet["input_tokens"] + sonnet["output_tokens"]
    assert 1_000_000 < total < 1_500_000
    assert sonnet["estimated_cost_usd"] > E.CONFIRM_ABOVE_USD and sonnet["needs_confirm"]
    haiku = E.typical_novel("anthropic", "claude-haiku-5-5")
    assert haiku["estimated_cost_usd"] < 1 and not haiku["needs_confirm"]
    openai = E.typical_novel("openai", "gpt-9-mini")
    assert openai["estimated_cost_usd"] is None and openai["needs_confirm"]
    assert not E.typical_novel("gemini", None)["needs_confirm"]


def test_a_short_book_needs_no_confirmation():
    assert not E.estimate(10, "anthropic", "claude-opus-5-5")["needs_confirm"]
    assert not E.estimate(10, "openai", "gpt-9")["needs_confirm"]


def test_chunk_count():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE chunks (id TEXT, book_id TEXT)")
    conn.executemany("INSERT INTO chunks VALUES (?, ?)",
                     [("a", "b1"), ("b", "b1"), ("c", "b2")])
    assert E.chunk_count(conn, "b1") == 2 and E.chunk_count(conn, "zz") == 0
