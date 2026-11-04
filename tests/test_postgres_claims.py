"""The job claim and the single-use invite on a real Postgres.

SQLite serialises every write, so its tests cannot show the Postgres path:
`FOR UPDATE SKIP LOCKED`, the partial unique index as the per-user backstop,
and a guarded UPDATE that two sessions race for. Opt-in; needs docker:
    LORE_TEST_POSTGRES=1 pytest -m postgres tests/test_postgres_claims.py
"""
import threading
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from lore.auth import accounts
from lore.auth.tokens import hash_token
from lore.central import db as central
from lore.central import queue
from lore.central.models import CLAIMED, QUEUED, Identity, Invite, Job, User
from tests.postgres_fixtures import postgres_url, requires_postgres

pytestmark = [pytest.mark.postgres, requires_postgres]
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def sessions():
    with postgres_url() as url:
        engine = central.make_engine(url)
        yield central.make_session_factory(engine)
        engine.dispose()


@pytest.fixture(autouse=True)
def clean(sessions):
    yield
    with sessions() as s:
        s.execute(sa.text("TRUNCATE jobs, identities, invites, users CASCADE"))
        s.commit()


def add_users(sessions, *ids):
    with sessions() as s:
        for uid in ids:
            s.add(User(id=uid, email=f"{uid}@example.org"))
        s.commit()


def run_threads(n, target):
    results, errors = [None] * n, []

    def wrap(i):
        try:
            results[i] = target(i)
        except Exception as exc:  # surfaced below, not swallowed by the thread
            errors.append(exc)

    threads = [threading.Thread(target=wrap, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    return results


def test_racing_workers_each_claim_a_different_job_exactly_once(sessions):
    users = [f"u{i}" for i in range(8)]
    add_users(sessions, *users)
    with sessions() as s:
        for u in users:
            queue.enqueue(s, u, "b1", "process")

    def work(i):
        got = []
        with sessions() as s:
            while (job := queue.claim_next(s, f"w{i}", now=NOW)) is not None:
                got.append(job.id)
        return got

    claimed = [j for part in run_threads(4, work) for j in part]
    assert len(claimed) == 8 and len(set(claimed)) == 8  # none twice, none lost
    with sessions() as s:
        assert s.scalar(sa.select(sa.func.count()).select_from(Job).where(
            Job.state == CLAIMED)) == 8


def test_one_user_never_has_two_claims_however_the_workers_race(sessions):
    add_users(sessions, "solo")
    with sessions() as s:
        for book in ("b1", "b2", "b3"):
            queue.enqueue(s, "solo", book, "process")

    def work(i):
        with sessions() as s:
            job = queue.claim_next(s, f"w{i}", now=NOW)
            return job.id if job else None

    results = run_threads(6, work)
    assert len([r for r in results if r is not None]) == 1
    with sessions() as s:
        states = sorted(j.state for j in s.scalars(sa.select(Job)))
    assert states == [CLAIMED, QUEUED, QUEUED]


def test_a_row_locked_by_another_worker_is_skipped_not_waited_for(sessions):
    add_users(sessions, "ua", "ub")
    with sessions() as s:
        first, _ = queue.enqueue(s, "ua", "b1", "process")
        second, _ = queue.enqueue(s, "ub", "b1", "process")
    holder = sessions()
    holder.scalars(sa.select(Job).where(Job.id == first.id).with_for_update()).all()
    try:
        out = {}

        def claim():
            with sessions() as s:
                job = queue.claim_next(s, "w2", now=NOW)
                out["id"] = job.id if job else None

        t = threading.Thread(target=claim)
        t.start()
        t.join(timeout=15)
        assert not t.is_alive(), "claim_next blocked on a locked row: SKIP LOCKED is not in effect"
        assert out["id"] == second.id
    finally:
        holder.rollback()
        holder.close()


def test_two_sign_ins_racing_for_one_invite_have_one_winner(sessions):
    with sessions() as s:
        code = accounts.create_invite(s, None, NOW)
        s.commit()

    def sign_in(i):
        with sessions() as s:
            result = accounts.sign_in_with_identity(
                s, "google", f"sub-{i}", f"racer{i}@example.org", None,
                hash_token(code), True, NOW)
            if result.outcome is accounts.Outcome.OK:
                s.commit()
            else:
                s.rollback()
            return result.outcome

    outcomes = run_threads(5, sign_in)
    assert outcomes.count(accounts.Outcome.OK) == 1
    assert outcomes.count(accounts.Outcome.NEEDS_INVITE) == 4
    with sessions() as s:
        assert s.scalar(sa.select(sa.func.count()).select_from(User)) == 1
        assert s.get(Invite, hash_token(code)).used_by is not None
        assert s.scalar(sa.select(sa.func.count()).select_from(Identity)) == 1
