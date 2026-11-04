"""The worker runs each job with ITS user's own Gemini key (release step 3).

Fake stages build real GeminiClients through the key factory against a stubbed
transport (no network). A key Gemini refuses pauses that user's jobs, and only
theirs; a user without a key is paused too; budgets are per user."""
import httpx
import pytest
import sqlalchemy as sa

from lore.central import keys as K
from lore.central.models import ApiKey, Job, Usage
from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.inference import gemini
from lore.inference.gemini import GeminiClient, GeminiError
from lore.inference.key_scope import gemini_api_key
from lore.orchestrator import jobs
from lore.orchestrator.gemini_budget import calls_today, record_call, recorder_for, today
from lore.tenancy import config_for
from tests.auth_fixtures import FakeKeyChecker, master_keys
from tests.central_fixtures import central_sessions
from tests.test_worker import NOW, book_job, enqueue, library, make_worker

ALICE = "AIzaALICEaliceALICEaliceALICEaliceALI0001"
BOB = "AIzaBOBbobBOBbobBOBbobBOBbobBOBbobBOBb0002"


@pytest.fixture
def base(tmp_path):
    return LoreConfig(data_dir=tmp_path / "root")


@pytest.fixture
def sessions(tmp_path):
    return central_sessions(tmp_path, users=("alice", "bob"))[0]


@pytest.fixture
def keys(sessions):
    return K.KeyService(sessions, master_keys(), FakeKeyChecker(), clock=lambda: NOW)


class GeminiStage:
    """A stage that spends one Gemini request through the key factory, as the
    real stages do, and records which key it was given."""

    def __init__(self, name="extract", status=lambda key: 200, seen=None):
        self.name, self._status, self.seen = name, status, seen if seen is not None else []

    def total(self, ctx):
        return 1

    def run(self, ctx, job_ctl):
        import asyncio
        key = gemini_api_key()
        self.seen.append((ctx.config.data_dir.name, key))

        def reply(request):
            code = self._status(request.headers["x-goog-api-key"])
            if code == 200:
                return httpx.Response(200, json={"candidates": [
                    {"content": {"parts": [{"text": "ok"}]}}]})
            return httpx.Response(code, json={"error": {}})

        async def go():
            client = GeminiClient(key, models=["gemini-3.1-flash-lite"],
                                  transport=httpx.MockTransport(reply),
                                  recorder=recorder_for(ctx.config.sqlite_path))
            try:
                await client.generate("q")
            finally:
                await client.aclose()

        try:
            asyncio.run(go())
        except GeminiError as exc:  # a stage words its own failure
            raise RuntimeError(f"extraction stopped: {exc}") from exc
        job_ctl.advance(1)


def states(sessions):
    with sessions() as db:
        return {(j.user_id, j.book_id): j.state for j in db.scalars(sa.select(Job))}


def test_each_job_builds_its_models_with_its_own_users_key(base, sessions, keys):
    keys.save("alice", ALICE)
    keys.save("bob", BOB)
    library(base, "alice", "b1")
    library(base, "bob", "b1")
    enqueue(sessions, "alice", "b1")
    enqueue(sessions, "bob", "b1")
    stage = GeminiStage()
    worker = make_worker(base, sessions, [stage], keys=keys)
    worker.run_once()
    worker.run_once()
    assert stage.seen == [("alice", ALICE), ("bob", BOB)]
    assert states(sessions) == {("alice", "b1"): "done", ("bob", "b1"): "done"}


def test_requests_land_in_each_users_own_ledger_and_usage(base, sessions, keys):
    keys.save("alice", ALICE)
    keys.save("bob", BOB)
    library(base, "alice", "b1", "b2")
    library(base, "bob", "b1")
    for user, book in (("alice", "b1"), ("alice", "b2"), ("bob", "b1")):
        enqueue(sessions, user, book)
    worker = make_worker(base, sessions, [GeminiStage()], keys=keys)
    for _ in range(3):
        worker.run_once()
    day = today()
    for user, expected in (("alice", 2), ("bob", 1)):
        conn = open_db(config_for(base, user).sqlite_path)
        try:  # the per-user budget: the ledger in the user's own lore.db
            assert calls_today(conn, "gemini-3.1-flash-lite", day) == expected
        finally:
            conn.close()
    with sessions() as db:
        usage = {r.user_id: r.requests for r in db.scalars(sa.select(Usage))}
    assert usage == {"alice": 2, "bob": 1}


def test_two_users_budgets_are_independent(base, monkeypatch):
    from lore.mcp.tools import _check_synthesis_budget
    cfg = base.model_copy(update={"synthesis": base.synthesis.model_copy(
        update={"provider": "gemini"})})
    alice, bob = config_for(cfg, "alice"), config_for(cfg, "bob")
    assert alice.sqlite_path != bob.sqlite_path  # config_for points each at their own lore.db
    conn = open_db(alice.sqlite_path)
    try:
        for model in cfg.synthesis.models:
            for _ in range(gemini.limits_for(model).rpd):
                record_call(conn, model, today())
    finally:
        conn.close()
    assert _check_synthesis_budget(alice) is not None  # alice has spent hers
    assert _check_synthesis_budget(bob) is None        # bob's is untouched


def test_a_refused_key_pauses_that_users_jobs_only(base, sessions, keys):
    keys.save("alice", ALICE)
    keys.save("bob", BOB)
    library(base, "alice", "b1", "b2")
    library(base, "bob", "b1")
    enqueue(sessions, "alice", "b1")
    enqueue(sessions, "alice", "b2")
    enqueue(sessions, "bob", "b1")
    stage = GeminiStage(status=lambda key: 401 if key == ALICE else 200)
    worker = make_worker(base, sessions, [stage], keys=keys)
    worker.run_once()  # alice's b1: Gemini refuses her key
    assert states(sessions) == {("alice", "b1"): "paused", ("alice", "b2"): "paused",
                                ("bob", "b1"): "queued"}
    row = book_job(base, "alice", "b1")
    assert row.state == jobs.WAITING_KEY
    assert "Replace it in Account settings" in row.stopped_reason
    assert "Traceback" not in row.stopped_reason and ALICE not in row.stopped_reason
    assert "gemini.env" not in row.stopped_reason
    assert jobs.can_continue(row, NOW)[0] is True  # paused, not failed
    with sessions() as db:
        assert db.get(ApiKey, "alice").last_error == K.ERR_REJECTED
        assert db.get(ApiKey, "bob").last_error is None
        job = db.scalars(sa.select(Job).where(Job.user_id == "alice",
                                              Job.book_id == "b1")).one()
        assert job.outcome == "key_invalid" and job.claimed_by is None
    worker.run_once()  # bob is not held up
    assert states(sessions)[("bob", "b1")] == "done"
    assert worker.run_once() is None  # alice's jobs wait for a new key


def test_a_new_key_resumes_the_paused_jobs_where_they_stopped(base, sessions, keys):
    keys.save("alice", ALICE)
    library(base, "alice", "b1")
    enqueue(sessions, "alice", "b1")
    good = {"key": BOB}
    stage = GeminiStage(status=lambda key: 200 if key == good["key"] else 403)
    worker = make_worker(base, sessions, [stage], keys=keys)
    worker.run_once()
    assert states(sessions) == {("alice", "b1"): "paused"}
    keys.save("alice", BOB)  # the user replaces the key
    assert states(sessions) == {("alice", "b1"): "queued"}
    worker.run_once()
    assert states(sessions) == {("alice", "b1"): "done"}
    assert book_job(base, "alice", "b1").state == "done"


def test_a_user_without_a_key_is_paused_untouched(base, sessions, keys):
    keys.save("bob", BOB)
    library(base, "alice", "b1")
    library(base, "bob", "b1")
    enqueue(sessions, "alice", "b1")
    enqueue(sessions, "bob", "b1")
    stage = GeminiStage()
    worker = make_worker(base, sessions, [stage], keys=keys)
    worker.run_once()
    assert states(sessions) == {("alice", "b1"): "paused", ("bob", "b1"): "queued"}
    assert book_job(base, "alice", "b1") is None  # nothing ran, nothing claimed
    with sessions() as db:
        assert db.scalars(sa.select(Job.outcome).where(Job.user_id == "alice")).one() \
            == "key_required"
    worker.run_once()
    assert stage.seen == [("bob", BOB)]


def test_without_a_key_store_a_job_never_falls_back_to_the_servers_key(
        base, sessions, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSERVERenvSERVERenvSERVERenvSERVE6789")
    path = tmp_path / ".config" / "lore" / "gemini.env"
    path.parent.mkdir(parents=True)
    path.write_text("GEMINI_API_KEY=AIzaSERVERfileSERVERfileSERVERfile12345\n")
    path.chmod(0o600)
    reads = []
    monkeypatch.setattr(gemini, "_key_from_file", lambda p: reads.append(p))
    library(base, "alice", "b1")
    enqueue(sessions, "alice", "b1")
    stage = GeminiStage()
    make_worker(base, sessions, [stage]).run_once()  # no keys=
    assert stage.seen == []  # KeyRequired before any client
    assert book_job(base, "alice", "b1").state == jobs.WAITING_KEY
    assert states(sessions) == {("alice", "b1"): "paused"}
    assert reads == []


def test_a_key_that_no_longer_decrypts_pauses_as_invalid(base, sessions, keys):
    keys.save("alice", ALICE)
    library(base, "alice", "b1")
    enqueue(sessions, "alice", "b1")
    other = K.KeyService(sessions, K.MasterKeys(b"\x07" * 32), FakeKeyChecker(),
                         clock=lambda: NOW)  # the master key was lost
    make_worker(base, sessions, [GeminiStage()], keys=other).run_once()
    assert states(sessions) == {("alice", "b1"): "paused"}
    with sessions() as db:
        assert db.get(ApiKey, "alice").last_error == K.ERR_REJECTED
