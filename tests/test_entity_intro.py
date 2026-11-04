"""The spoiler-safe entity intro and the profile's display order.

Real stores over tests/position_fixtures.py (Ren Ashby, met as "Old Tam" in
chapters 1-2 of Hollow Gate; chapter 3 holds the spoilers), with Ren's codex
entry replaced by one that has identity and role lines early and late. A fake
model records every prompt; nothing touches the network.
"""
from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from lore.codex import intro as intro_module
from lore.codex.store import CodexEntry, Statement, save_entry
from lore.db.sqlite import open_db
from lore.query import explore
from lore.web.app import create_app
from tests.position_fixtures import HOLLOW, build_position_library

# (aspect, text, book, chapter): stored in aspect then story order, as a build saves them
STATEMENTS = [
    ("identity", "IDENTEARLY Ren Ashby is a gatekeeper of the hollow.", "hb1", 1),
    ("identity", "IDENTLATE Ren Ashby is an agent of Dorn Vale.", "hb1", 3),
    ("nature", "NATUREEARLY Ren Ashby is quiet.", "hb1", 2),
    ("history", "HISTORYEARLY Ren Ashby came from the hills.", "hb1", 1),
    ("role", "ROLEEARLY Ren Ashby guides Mira Cole.", "hb1", 2),
    ("role", "ROLELATE Ren Ashby betrays Mira Cole.", "hb1", 3),
    ("change", "CHANGESEQUEL Ren Ashby holds the keep.", "hb2", 2),
]
LATE_MARKERS = ("IDENTLATE", "ROLELATE", "CHANGESEQUEL", "LATEBEAT", "SEQUELBEAT",
                "SECRET", "Ren Ashby", "Dorn", "WHOLESTORYSUMMARY", "BETRAY")


@pytest.fixture
def config(tmp_path):
    config = build_position_library(tmp_path)
    conn = open_db(config.sqlite_path)
    world_id = conn.execute("SELECT id FROM worlds WHERE name=?", (HOLLOW,)).fetchone()[0]
    save_entry(conn, CodexEntry(
        entity_id="h_ren", world_id=world_id, name="Ren Ashby", kind="Character",
        summary="WHOLESTORYSUMMARY",
        statements=tuple(Statement(aspect=a, text=t, quote=f"MARK-{b}-{c}",
                                   chunk_id=f"{b}:{c}:0") for a, t, b, c in STATEMENTS)))
    conn.close()
    return config


@pytest.fixture
def client(config):
    return TestClient(create_app(config), base_url="http://127.0.0.1:8766")


def put(client, *, enabled=True, book="hb1", chapter=2):
    r = client.put("/api/reading-position", params={"world": HOLLOW},
                   json={"enabled": enabled, "book_id": book, "chapter": chapter,
                         "read_books": []})
    assert r.status_code == 200, r.text


def get(client, path, **params):
    params.setdefault("world", HOLLOW)
    r = client.get(path, params=params)
    assert r.status_code == 200, r.text
    return r.json()


class FakeModel:
    """A model factory: records each prompt and answers `reply` (or raises)."""

    def __init__(self, reply="Old Tam is a gatekeeper who guides Mira Cole.", fail=False):
        self.reply, self.fail = reply, fail
        self.prompts: list[str] = []
        self.closed = 0

    def __call__(self, config):
        async def generate_json(prompt, schema):
            self.prompts.append(prompt)
            if self.fail:
                raise RuntimeError("upstream down")
            return schema(intro=self.reply)

        async def aclose():
            self.closed += 1
        return generate_json, aclose, lambda: "fake-lite"


# --- the profile ------------------------------------------------------------------------


def test_no_position_shows_the_whole_story_summary_and_no_intro(client, config):
    got = get(client, "/api/entity/h_ren/profile")
    assert got["summary"] == "WHOLESTORYSUMMARY"
    assert "intro" not in got and "summary_hidden" not in got
    model = FakeModel()
    assert explore.intro(config, "h_ren", world=HOLLOW, model_factory=model) == {
        "intro": None, "source": "none"}
    assert model.prompts == []


def test_the_profile_shows_aspects_identity_first_in_display_order(client):
    got = get(client, "/api/entity/h_ren/profile")
    assert [a["aspect"] for a in got["aspects"]] == [
        "identity", "role", "nature", "history", "change"]
    put(client, chapter=2)
    got = get(client, "/api/entity/h_ren/profile")
    assert [a["aspect"] for a in got["aspects"]] == ["identity", "role", "nature", "history"]
    assert [s["text"] for s in got["aspects"][0]["statements"]] == [
        "IDENTEARLY Old Tam is a gatekeeper of the hollow."]


def test_under_a_position_the_profile_carries_the_fallback_intro_until_one_is_written(client):
    put(client, chapter=2)
    got = get(client, "/api/entity/h_ren/profile")
    assert got["summary"] is None
    assert got["intro"] == ("IDENTEARLY Old Tam is a gatekeeper of the hollow."
                            " ROLEEARLY Old Tam guides Mira Cole.")
    assert got["intro_source"] == "fallback" and got["intro_pending"] is True
    assert not any(m in json.dumps(got) for m in LATE_MARKERS)


# --- the model intro ------------------------------------------------------------------------


def test_the_intro_prompt_holds_only_what_the_position_lets_through(client, config):
    put(client, chapter=2)
    model = FakeModel()
    got = explore.intro(config, "h_ren", world=HOLLOW, model_factory=model)
    assert got == {"intro": "Old Tam is a gatekeeper who guides Mira Cole.", "source": "model"}
    (prompt,) = model.prompts
    for seen in ("IDENTEARLY", "ROLEEARLY", "NATUREEARLY", "HISTORYEARLY", "EARLYBEAT",
                 "Subject: Old Tam (Character)"):
        assert seen in prompt
    for hidden in LATE_MARKERS:
        assert hidden not in prompt
    # identity first, then role, then the rest
    assert (prompt.index("IDENTEARLY") < prompt.index("ROLEEARLY")
            < prompt.index("NATUREEARLY") < prompt.index("HISTORYEARLY"))
    assert "Add nothing from memory or general knowledge" in prompt
    assert model.closed == 1


def test_a_written_intro_is_cached_per_visible_scope(client, config):
    put(client, chapter=2)
    model = FakeModel()
    explore.intro(config, "h_ren", world=HOLLOW, model_factory=model)
    again = explore.intro(config, "h_ren", world=HOLLOW, model_factory=model)
    assert again["source"] == "model" and len(model.prompts) == 1  # a cache hit
    got = get(client, "/api/entity/h_ren/profile")
    assert got["intro"] == "Old Tam is a gatekeeper who guides Mira Cole."
    assert got["intro_source"] == "model" and got["intro_pending"] is False
    conn = open_db(config.sqlite_path)
    rows = conn.execute("SELECT entity_id, scope_hash, prompt_hash, model FROM entity_intros").fetchall()
    conn.close()
    assert len(rows) == 1 and rows[0][0] == "h_ren" and rows[0][2] == intro_module.prompt_hash()
    assert rows[0][3] == "fake-lite"
    # moving the position changes what is visible: a new intro, and nothing late leaks
    put(client, chapter=1)
    explore.intro(config, "h_ren", world=HOLLOW, model_factory=model)
    assert len(model.prompts) == 2 and "ROLEEARLY" not in model.prompts[1]
    put(client, chapter=2)  # back again: the first one is found
    assert explore.intro(config, "h_ren", world=HOLLOW, model_factory=model)["source"] == "model"
    assert len(model.prompts) == 2


def test_the_fallback_without_a_model_on_failure_and_with_the_switch_off(client, config, monkeypatch):
    put(client, chapter=2)
    expected = ("IDENTEARLY Old Tam is a gatekeeper of the hollow."
                " ROLEEARLY Old Tam guides Mira Cole.")
    assert explore.intro(config, "h_ren", world=HOLLOW, model_factory=lambda c: None) == {
        "intro": expected, "source": "fallback"}
    failing = FakeModel(fail=True)
    assert explore.intro(config, "h_ren", world=HOLLOW, model_factory=failing) == {
        "intro": expected, "source": "fallback"}
    assert failing.closed == 1
    blank = FakeModel(reply="   ")
    assert explore.intro(config, "h_ren", world=HOLLOW, model_factory=blank)["source"] == "fallback"

    def broken(config):
        raise RuntimeError("no key file")
    assert explore.intro(config, "h_ren", world=HOLLOW, model_factory=broken)["source"] == "fallback"
    monkeypatch.setattr(intro_module, "INTRO", False)
    off = FakeModel()
    assert explore.intro(config, "h_ren", world=HOLLOW, model_factory=off)["source"] == "fallback"
    assert off.prompts == []
    assert get(client, "/api/entity/h_ren/profile")["intro_pending"] is False


def test_the_default_model_is_none_when_the_allowance_is_spent(config, monkeypatch):
    from lore.orchestrator import gemini_budget

    config.selection.provider = "gemini"
    monkeypatch.setattr(gemini_budget, "calls_today", lambda conn, model, day: 10 ** 6)
    assert explore._intro_model(config) is None
    config.selection.provider = "ollama"
    assert explore._intro_model(config) is None


def test_the_intro_endpoint(client, monkeypatch):
    put(client, chapter=2)
    model = FakeModel()
    monkeypatch.setattr(explore, "_intro_model", model)
    got = get(client, "/api/entity/h_ren/intro")
    assert got == {"intro": "Old Tam is a gatekeeper who guides Mira Cole.", "source": "model"}
    assert get(client, "/api/entity/h_dorn/intro")["error"]["code"] == "not_found"


# --- the pieces ---------------------------------------------------------------------------


def test_fallback_intro_rules():
    inp = intro_module.make_input("Ash", "Character", [
        ("nature", "Ash is tall"), ("role", "Ash keeps the gate."), ("role", "Ash sings."),
        ("identity", "Ash is a smith.")], [])
    assert [a for a, _t in inp.statements] == ["identity", "role", "role", "nature"]
    assert intro_module.fallback_intro(inp) == "Ash is a smith. Ash keeps the gate."
    only = intro_module.make_input("Ash", "Character", [("nature", "Ash is tall")], [])
    assert intro_module.fallback_intro(only) == "Ash is tall."
    assert intro_module.fallback_intro(intro_module.make_input("Ash", "", [], [])) is None


def test_intro_inputs_are_trimmed_and_cleaned():
    beats = [f"beat {i}" for i in range(40)]
    inp = intro_module.make_input("Ash", "Character", [("role", f"r{i}") for i in range(50)], beats)
    assert len(inp.statements) == intro_module.MAX_INTRO_STATEMENTS
    assert len(inp.beats) == intro_module.MAX_INTRO_BEATS
    assert inp.beats[0] == "beat 0" and inp.beats[-1] == "beat 39"
    long = "One sentence here. " * 60
    cut = intro_module.clean(long)
    assert len(cut) <= intro_module.MAX_INTRO_CHARS and cut.endswith(".")
    assert intro_module.clean("  ") is None
    assert intro_module.scope_hash(inp) != intro_module.scope_hash(
        intro_module.make_input("Ash", "Character", [], beats))


def test_the_intro_prompt_names_no_book_character_or_genre():
    banned = (r"\b(holmes|watson|sherlock|detective|crime|murder|villain|treasure|"
              r"darcy|bennet|wizard|dragon|magic|kingdom|quest|sword)\b")
    assert not re.findall(banned, intro_module.INTRO_PROMPT.lower())
