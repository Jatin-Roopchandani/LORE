"""Switching provider while a book processes (multi mode, the worker).

The worker binds the user's provider once per job. When the user switches
(PUT /api/me/provider) or removes the key in use, the running job stops at its
next safe point (JobCtl.spend_check, between items), is requeued, and resumes
where it stopped on the new provider; with a paid provider whose estimate for
the rest needs confirming, it waits for Continue instead. Offline."""
import pytest
import sqlalchemy as sa

from lore.central import keys as K
from lore.central.models import Job, Usage
from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.inference import key_scope
from lore.orchestrator import jobs
from lore.tenancy import config_for
from tests.auth_fixtures import FakeKeyChecker, master_keys
from tests.central_fixtures import central_sessions
from tests.test_worker import NOW, book_job, enqueue, library, make_worker

GEMINI_KEY = "AIzaALICEaliceALICEaliceALICEaliceALI0001"
CLAUDE_KEY = "sk-ant-api03-ALICEaliceALICEaliceALICEalice-0001"


class Checker:
    def __init__(self, models):
        self.models = models

    def check(self, key):
        return K.CheckResult(K.CHECK_OK, self.models)


@pytest.fixture
def base(tmp_path):
    return LoreConfig(data_dir=tmp_path / "root")


@pytest.fixture
def sessions(tmp_path):
    return central_sessions(tmp_path, users=("alice", "bob"))[0]


@pytest.fixture
def keys(sessions):
    return K.KeyService(sessions, master_keys(), FakeKeyChecker(), clock=lambda: NOW,
                        checkers={"anthropic": Checker(["claude-sonnet-5-5"])})


class ItemsStage:
    """Three items; each is a safe point (spend_check) then one "request" on the
    bound provider. Done items are remembered in the book's own database, as
    the real stages remember theirs, so a resumed run skips them."""

    name = "extract"

    def __init__(self, ran, after_first=None):
        self.ran, self._after_first = ran, after_first

    def total(self, ctx):
        return 3

    def run(self, ctx, job_ctl):
        ctx.conn.execute("CREATE TABLE IF NOT EXISTS items_done (n INTEGER PRIMARY KEY)")
        done = {r[0] for r in ctx.conn.execute("SELECT n FROM items_done")}
        for n in range(3):
            if n in done:
                continue
            job_ctl.spend_check()
            scope = key_scope.current()
            model = f"{scope.provider}-model"
            key_scope.note_request(model)
            key_scope.note_usage(scope.provider, model, 10, 1)
            self.ran.append((n, scope.provider))
            with ctx.conn:
                ctx.conn.execute("INSERT INTO items_done (n) VALUES (?)", (n,))
            job_ctl.advance(ctx.conn.execute("SELECT COUNT(*) FROM items_done").fetchone()[0])
            if n == 0 and self._after_first is not None:
                self._after_first()


def usage_by_provider(sessions):
    with sessions() as db:
        return {(r.provider, r.model): r.requests for r in db.scalars(sa.select(Usage))}


def test_a_switch_mid_job_resumes_on_the_new_provider_without_redoing_work(
        base, sessions, keys):
    keys.save("alice", GEMINI_KEY)
    keys.save("alice", CLAUDE_KEY, "anthropic")  # connected, not active
    keys.choose("alice", "gemini")
    library(base, "alice", "b1")  # no chunks: the rest needs no confirming
    job = enqueue(sessions, "alice", "b1")
    ran = []
    stage = ItemsStage(ran, after_first=lambda: keys.choose("alice", "anthropic"))
    worker = make_worker(base, sessions, [stage], keys=keys)

    first = worker.run_once()
    assert first.id == job.id and first.state == "queued"  # requeued, not finished
    row = book_job(base, "alice", "b1")
    assert row.state == jobs.IDLE and row.stage == "extract" and row.done_items == 1
    assert ran == [(0, "gemini")]

    stage._after_first = None
    second = worker.run_once()
    assert second.id == job.id and second.state == "done"
    assert ran == [(0, "gemini"), (1, "anthropic"), (2, "anthropic")]  # nothing redone
    assert book_job(base, "alice", "b1").state == "done"
    assert usage_by_provider(sessions) == {("gemini", "gemini-model"): 1,
                                           ("anthropic", "anthropic-model"): 2}


def test_removing_the_key_in_use_stops_the_job_too(base, sessions, keys):
    keys.save("alice", GEMINI_KEY)
    keys.save("alice", CLAUDE_KEY, "anthropic")
    library(base, "alice", "b1")
    enqueue(sessions, "alice", "b1")
    ran = []
    stage = ItemsStage(ran, after_first=lambda: keys.remove("alice", "gemini"))
    worker = make_worker(base, sessions, [stage], keys=keys)
    assert worker.run_once().state == "queued"
    stage._after_first = None
    assert worker.run_once().state == "done"
    assert ran == [(0, "gemini"), (1, "anthropic"), (2, "anthropic")]


def test_no_switch_runs_straight_through(base, sessions, keys):
    keys.save("alice", GEMINI_KEY)
    library(base, "alice", "b1")
    enqueue(sessions, "alice", "b1")
    ran = []
    done = make_worker(base, sessions, [ItemsStage(ran)], keys=keys).run_once()
    assert done.state == "done" and [p for _, p in ran] == ["gemini"] * 3


def test_a_switch_to_a_paid_provider_asks_before_the_rest(base, sessions, keys):
    keys.save("alice", GEMINI_KEY)
    keys.save("alice", CLAUDE_KEY, "anthropic")
    keys.choose("alice", "gemini")
    library(base, "alice", "b1")
    conn = open_db(config_for(base, "alice").sqlite_path)
    conn.executemany("INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text)"
                     " VALUES (?, 'b1', 1, 1, ?, 't')", [(f"c{i}", i) for i in range(3000)])
    conn.commit()
    conn.close()
    job = enqueue(sessions, "alice", "b1")
    ran = []
    stage = ItemsStage(ran, after_first=lambda: keys.choose("alice", "anthropic"))
    worker = make_worker(base, sessions, [stage], keys=keys)
    paused = worker.run_once()
    assert (paused.state, paused.outcome) == ("paused", "needs_confirm")
    row = book_job(base, "alice", "b1")
    assert row.state == jobs.IDLE and "Claude" in row.stopped_reason
    assert "$" in row.stopped_reason and "estimate" in row.stopped_reason
    assert jobs.can_continue(row, NOW)[0]
    enqueue(sessions, "alice", "b1")  # Continue confirms
    stage._after_first = None
    assert worker.run_once().state == "done"
    assert [p for _, p in ran] == ["gemini", "anthropic", "anthropic"]
    with sessions() as db:
        assert db.get(Job, job.id).state == "done"


def test_the_running_job_says_which_provider_it_uses(base, sessions, keys):
    keys.save("alice", CLAUDE_KEY, "anthropic")
    library(base, "alice", "b1")
    enqueue(sessions, "alice", "b1")
    seen = []

    class Peek(ItemsStage):
        def run(self, ctx, job_ctl):
            seen.append(jobs.provider_of(ctx.conn, ctx.book_id))

    make_worker(base, sessions, [Peek([])], keys=keys).run_once()
    assert seen == ["anthropic"]


def test_the_library_lists_the_provider_of_a_running_job(tmp_path):
    from fastapi.testclient import TestClient  # noqa: F401 - the app's own test client
    from lore.web.app import _job_out
    conn = open_db(tmp_path / "lore.db")
    conn.execute("INSERT INTO books (id, title, file_path) VALUES ('b1', 'B', 'x')")
    conn.commit()
    jobs.claim_job(conn, "b1", now=NOW)
    jobs.set_provider(conn, "b1", "openai")
    out = _job_out(jobs.get_job(conn, "b1"), NOW, jobs.provider_of(conn, "b1"))
    assert out["provider"] == "openai"
    assert _job_out(jobs.get_job(conn, "b1"), NOW)["provider"] is None
    conn.close()
