"""Opt-in: a question that ends before any step or model call is traced on
Postgres too (no event, no call: empty steps, calls and usage, a null
first-event time), stored with its message in one commit.

Run with `LORE_TEST_POSTGRES=1 pytest -m postgres tests/test_postgres_traces.py`.
"""
from __future__ import annotations

import pytest
import sqlalchemy as sa

from lore.central import db as central
from lore.central.models import Message, User
from lore.config import LoreConfig
from tests.auth_fixtures import give_key, make_app, sign_in
from tests.postgres_fixtures import postgres_url, requires_postgres
from tests.test_traces import _unprocessed_world, ask, stored

pytestmark = [pytest.mark.postgres, requires_postgres]


@pytest.fixture(scope="module")
def sessions():
    with postgres_url() as url:
        engine = central.make_engine(url)
        made = central.make_session_factory(engine)
        with made() as s:
            s.add(User(id="alice", email="alice@example.org"))
            s.commit()
        yield made
        engine.dispose()


def test_an_empty_answer_with_no_events_is_traced_on_postgres(sessions, tmp_path, monkeypatch):
    from lore.query.agent import QueryModels

    async def embed(texts):
        return [[0.1] * 768 for _ in texts]

    async def never(prompt, schema):
        raise AssertionError("no model is asked")

    async def aclose():
        return None
    monkeypatch.setattr("lore.mcp.tools._build_models", lambda config: QueryModels(
        embed, never, never, lambda: "w", aclose, lambda: "s"))
    root = LoreConfig(data_dir=tmp_path / "root")
    app = make_app(root, sessions)
    give_key(sessions, "alice")
    alice = sign_in(app, sessions, "alice")
    wid = _unprocessed_world(root)
    events, _ = ask(alice, {"question": "Who is Fantine?", "world": wid})
    rid = events[0][1]["request_id"]
    assert dict(events)["answer"]["result"]["mode"] == "empty"
    with sessions() as s:
        assert s.scalars(sa.select(Message.request_id)).all() == [rid]
    row = stored(sessions, rid)
    assert row is not None and row.calls_json == [] and row.steps_json == []
