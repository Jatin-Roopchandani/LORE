"""With the reading position off, every output is what it was before it existed.

`golden/position_off.json` was dumped from the commit before the reading
position (2e9f5e0) by running `lore_search` (the same call the web UI and MCP
make) over this fixture library with scripted models, and storing the claims
prompt, the structured result and the model calls. Regenerate it only by doing
the same on a commit without the feature.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lore.db.sqlite import open_db
from lore.mcp import tools
from lore.reading_position import ReadingPosition, set_position
from tests.position_fixtures import HOLLOW, build_position_library
from tests.worlds_fixtures import scripted_models
from tests.precision_pins import before_prose_precision

GOLDEN = Path(__file__).parent / "golden" / "position_off.json"
CASES = {
    "world": dict(question="Who is Ren Ashby?", world=HOLLOW, sub_questions=None),
    "all_worlds": dict(question="Who is Ren Ashby?", world=None, sub_questions=None),
    "decomposed": dict(question="Who is Old Tam and what did Mira Cole know?", world=HOLLOW,
                       sub_questions=["Who is Old Tam?", "What did Mira Cole know?"]),
}


async def capture(config) -> dict:
    out = {}
    for name, case in CASES.items():
        models, calls = scripted_models(case["sub_questions"])
        extra = {} if case["world"] is None else {"world": case["world"]}
        result = await tools.lore_search(config, case["question"], models=models, **extra)
        out[name] = {"result": result, "calls": calls}
    return out


async def test_with_the_position_off_outputs_are_golden(tmp_path, monkeypatch) -> None:
    before_prose_precision(monkeypatch)  # pinned before prose-precision
    # The golden predates recap answers, whose rule the decomposer prompt carries.
    monkeypatch.setattr("lore.query.decompose.RECAP", False)
    config = build_position_library(tmp_path)
    got = json.loads(json.dumps(await capture(config), sort_keys=True))
    assert got == json.loads(GOLDEN.read_text())
    # A stored but disabled position changes nothing either.
    conn = open_db(config.sqlite_path)
    try:
        wid = conn.execute("SELECT id FROM worlds WHERE name = ?", (HOLLOW,)).fetchone()[0]
        set_position(conn, ReadingPosition(world_id=wid, book_id="hb1", chapter=1,
                                           read_books=(), enabled=False))
    finally:
        conn.close()
    again = json.loads(json.dumps(await capture(config), sort_keys=True))
    assert again == got
