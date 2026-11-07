"""Gemini's 402 "prepayment credits are depleted" (RESOURCE_EXHAUSTED).

It pauses a book (Continue resumes it) with a friendly reason instead of
failing it with Google's raw JSON; it is never cascaded (every model bills the
same account); a question gets the same friendly text; and, since only a paid
plan has prepaid credit, it is a signal for the paid-plan hint. Offline."""
import asyncio

import httpx
import pytest
import sqlalchemy as sa

from lore.central import keys as K
from lore.central.models import Job
from lore.inference import key_scope
from lore.inference.gemini import CREDIT_MESSAGE, GeminiClient, GeminiCreditError
from lore.inference.key_scope import KeyScope
from lore.orchestrator import jobs
from lore.orchestrator.pipeline import QuotaStop
from tests.auth_fixtures import FAKE_KEY, FakeKeyChecker, master_keys
from tests.central_fixtures import central_sessions
from tests.test_worker import NOW, FakeStage, book_job, enqueue, library, make_worker
from tests.test_worker_keys import ALICE, GeminiStage

DEPLETED = {"error": {"code": 402, "status": "RESOURCE_EXHAUSTED",
                      "message": "Your prepayment credits are depleted. SECRET-DETAIL"}}
LITE, LITE2 = "gemini-3.1-flash-lite", "gemini-3.5-flash-lite"


def _client(handler):
    client = GeminiClient("AIzaTESTtestTESTtestTESTtestTESTtest0001", models=[LITE, LITE2],
                          transport=httpx.MockTransport(handler))

    async def no_wait(model):
        return None
    client._throttle = no_wait
    return client


def test_a_402_is_one_request_a_friendly_error_and_a_signal():
    urls, credit = [], []

    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(402, json=DEPLETED)

    async def go():
        client = _client(handler)
        try:
            await client.generate("q")
        finally:
            await client.aclose()

    with key_scope.bound(KeyScope("alice", "k", on_credit=credit.append)):
        with pytest.raises(GeminiCreditError) as caught:
            asyncio.run(go())
    assert len(urls) == 1  # not cascaded: the next model bills the same account
    assert str(caught.value) == CREDIT_MESSAGE
    assert "SECRET-DETAIL" not in str(caught.value) and "k" != str(caught.value)
    assert credit == [LITE]
    assert CREDIT_MESSAGE == ("Your Gemini key's prepaid credit has run out. Top it up in"
                              " Google AI Studio, or switch provider in Settings → AI"
                              " providers.")


def test_a_question_gets_the_friendly_text():
    from lore.mcp.tools import _upstream
    out = _upstream(GeminiCreditError())
    assert out["error"]["code"] == "upstream_unavailable"
    assert out["error"]["message"] == CREDIT_MESSAGE


@pytest.fixture
def base(tmp_path):
    from lore.config import LoreConfig
    return LoreConfig(data_dir=tmp_path / "root")


@pytest.fixture
def sessions(tmp_path):
    return central_sessions(tmp_path, users=("alice", "bob"))[0]


@pytest.fixture
def keys(sessions):
    return K.KeyService(sessions, master_keys(), FakeKeyChecker(), clock=lambda: NOW)


@pytest.mark.parametrize("wrap", ["runtime", "quota"])
def test_the_runner_pauses_the_book_and_continue_is_allowed(base, wrap):
    from lore.db.sqlite import open_db
    from lore.orchestrator.pipeline import run_book
    from lore.tenancy import config_for

    library(base, "alice", "b1")
    cfg = config_for(base, "alice")
    try:
        raise GeminiCreditError()
    except GeminiCreditError as exc:
        boom = (RuntimeError(f"extraction stopped: {exc}") if wrap == "runtime"
                else QuotaStop(f"Gemini cannot extract right now: {exc}"))
        boom.__cause__ = exc
    conn = open_db(cfg.sqlite_path)
    try:
        job = run_book(conn, cfg, "b1", [FakeStage("chunk", boom=boom)], now=NOW)
    finally:
        conn.close()
    assert job.state == jobs.WAITING_KEY and job.stopped_reason == CREDIT_MESSAGE
    assert job.resumable_at is None
    assert jobs.can_continue(job, NOW) == (True, "")


def test_the_worker_pauses_and_continue_wakes_it(base, sessions, keys):
    keys.save("alice", ALICE)
    library(base, "alice", "b1")
    enqueue(sessions, "alice", "b1")
    worker = make_worker(base, sessions, [GeminiStage(status=lambda key: 402)], keys=keys)
    done = worker.run_once()
    assert (done.state, done.outcome) == ("paused", "credit_exhausted")
    assert done.error == CREDIT_MESSAGE
    row = book_job(base, "alice", "b1")
    assert row.state == jobs.WAITING_KEY and row.stopped_reason == CREDIT_MESSAGE
    assert keys.status("alice")["last_error"] is None  # the key itself is fine
    assert keys.providers("alice")["gemini_paid_hint"] is True  # only paid plans prepay
    enqueue(sessions, "alice", "b1")  # Continue
    with sessions() as db:
        assert db.get(Job, done.id).state == "queued"
    worker = make_worker(base, sessions, [GeminiStage()], keys=keys)
    assert worker.run_once().state == "done"
    assert book_job(base, "alice", "b1").state == "done"


def test_no_hint_from_a_402_once_the_paid_plan_is_on(sessions, keys):
    keys.save("alice", FAKE_KEY)
    keys.record_credit_refusal("alice", LITE)
    assert keys.providers("alice")["gemini_paid_hint"] is True
    keys.set_gemini_paid("alice", True)
    assert keys.providers("alice")["gemini_paid_hint"] is False
    assert keys.providers("bob")["gemini_paid_hint"] is False


def test_migration_0012_adds_and_drops_credit_refusals(tmp_path):
    from alembic import command
    from lore.central import db as central
    from lore.central.migrate import alembic_config

    url = f"sqlite:///{tmp_path / 'c.db'}"
    central.upgrade(url)
    engine = central.make_engine(url)
    with engine.connect() as conn:
        assert "credit_refusals" in {c["name"] for c in sa.inspect(conn).get_columns("usage")}
    engine.dispose()
    command.downgrade(alembic_config(url), "0011")
    engine = central.make_engine(url)
    with engine.connect() as conn:
        assert "credit_refusals" not in {c["name"] for c in
                                         sa.inspect(conn).get_columns("usage")}
    engine.dispose()
