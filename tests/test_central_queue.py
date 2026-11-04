"""The job queue: enqueue, claim, heartbeat, finish, stale requeue."""
import threading
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from lore.central import db as central
from lore.central import queue
from lore.central.models import Job, User
from tests.central_fixtures import central_sessions

T0 = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path):
    sessions, url = central_sessions(tmp_path, users=("alice", "bob", "carol"))
    return sessions, url


@pytest.fixture
def session(env):
    with env[0]() as s:
        yield s


def test_enqueue_creates_a_queued_job(session):
    job, created = queue.enqueue(session, "alice", "b1", "process")
    assert created and (job.state, job.attempts, job.user_id) == ("queued", 0, "alice")


def test_enqueue_twice_finds_the_live_job(session):
    first, _ = queue.enqueue(session, "alice", "b1", "process")
    again, created = queue.enqueue(session, "alice", "b1", "process")
    assert not created and again.id == first.id
    queue.claim_next(session, "w1")  # still live once claimed
    assert queue.enqueue(session, "alice", "b1", "process")[1] is False


def test_enqueue_after_the_job_finished_makes_a_new_one(session):
    first, _ = queue.enqueue(session, "alice", "b1", "process")
    queue.claim_next(session, "w1")
    queue.finish(session, first.id, "w1", outcome="done")
    second, created = queue.enqueue(session, "alice", "b1", "process")
    assert created and second.id != first.id


def test_enqueue_refuses_unknown_users_and_kinds(session):
    with pytest.raises(queue.UnknownUser):
        queue.enqueue(session, "mallory", "b1", "process")
    with pytest.raises(ValueError):
        queue.enqueue(session, "alice", "b1", "format-disk")


def test_claim_takes_the_oldest_and_stamps_it(session):
    queue.enqueue(session, "alice", "b1", "process")
    queue.enqueue(session, "bob", "b2", "process")
    job = queue.claim_next(session, "w1", now=T0)
    assert (job.book_id, job.state, job.claimed_by, job.attempts) == (
        "b1", "claimed", "w1", 1)
    assert job.claimed_at == T0 and job.heartbeat_at == T0


def test_claim_on_an_empty_queue_is_none(session):
    assert queue.claim_next(session, "w1") is None


def test_a_user_has_one_claimed_job_at_a_time(session):
    queue.enqueue(session, "alice", "b1", "process")
    queue.enqueue(session, "alice", "b2", "process")
    queue.enqueue(session, "bob", "b3", "process")
    first = queue.claim_next(session, "w1")
    second = queue.claim_next(session, "w2")
    assert (first.book_id, second.book_id) == ("b1", "b3")  # alice's b2 waits
    assert queue.claim_next(session, "w3") is None
    queue.finish(session, first.id, "w1", outcome="done")
    third = queue.claim_next(session, "w3")
    assert third.book_id == "b2"


def test_the_database_refuses_a_second_claim_for_a_user(env):
    """The index is the backstop when two workers race past the check."""
    sessions, _ = env
    with sessions() as s:
        queue.enqueue(s, "alice", "b1", "process")
        queue.enqueue(s, "alice", "b2", "process")
        queue.claim_next(s, "w1")
        with pytest.raises(IntegrityError):
            s.execute(sa.update(Job).where(Job.book_id == "b2").values(state="claimed"))
            s.commit()


def test_workers_racing_never_share_a_job(env):
    sessions, url = env
    with sessions() as s:
        for i in range(12):
            s.add(User(id=f"u{i}", email=f"u{i}@example.org"))
        s.commit()
        for i in range(12):
            queue.enqueue(s, f"u{i}", "b", "process")
    claimed: list[tuple[str, int]] = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def work(name):
        engine = central.make_engine(url)  # each worker is its own connection
        with central.make_session_factory(engine)() as s:
            barrier.wait()
            while (job := queue.claim_next(s, name)) is not None:
                with lock:
                    claimed.append((name, job.id))
        engine.dispose()

    threads = [threading.Thread(target=work, args=(f"w{i}",)) for i in range(4)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    ids = [job_id for _, job_id in claimed]
    assert len(ids) == 12 and len(set(ids)) == 12


def test_workers_racing_for_one_users_jobs_get_one_between_them(env):
    sessions, url = env
    with sessions() as s:
        for i in range(6):
            queue.enqueue(s, "alice", f"b{i}", "process")
    wins: list[int] = []
    barrier = threading.Barrier(4)

    def work(name):
        engine = central.make_engine(url)
        with central.make_session_factory(engine)() as s:
            barrier.wait()
            job = queue.claim_next(s, name)
            if job is not None:
                wins.append(job.id)
        engine.dispose()

    threads = [threading.Thread(target=work, args=(f"w{i}",)) for i in range(4)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert len(wins) == 1


def test_heartbeat_and_finish_belong_to_the_claimant(session):
    queue.enqueue(session, "alice", "b1", "process")
    job = queue.claim_next(session, "w1", now=T0)
    later = T0 + timedelta(seconds=30)
    assert queue.heartbeat(session, job.id, "w1", now=later) is True
    assert queue.heartbeat(session, job.id, "w2", now=later) is False
    assert queue.finish(session, job.id, "w2", outcome="done") is False
    assert queue.finish(session, job.id, "w1", outcome="failed", failed=True,
                        error="boom", now=later) is True
    session.expire_all()
    done = session.get(Job, job.id)
    assert (done.state, done.outcome, done.error, done.finished_at) == (
        "failed", "failed", "boom", later)


def test_stale_claims_are_requeued_and_fresh_ones_are_not(session):
    queue.enqueue(session, "alice", "b1", "process")
    queue.enqueue(session, "bob", "b2", "process")
    dead = queue.claim_next(session, "w1", now=T0)
    alive = queue.claim_next(session, "w2", now=T0)
    queue.heartbeat(session, alive.id, "w2", now=T0 + timedelta(seconds=170))
    now = T0 + timedelta(seconds=200)
    assert queue.requeue_stale(session, now=now) == [dead.id]
    session.expire_all()
    assert session.get(Job, dead.id).state == "queued"
    assert session.get(Job, dead.id).claimed_by is None
    assert session.get(Job, alive.id).state == "claimed"
    # ...and the requeued job can be claimed again, counting the attempt.
    again = queue.claim_next(session, "w3", now=now)
    assert again.id == dead.id and again.attempts == 2


def test_a_job_that_keeps_dying_is_failed(session):
    queue.enqueue(session, "alice", "b1", "process")
    now = T0
    for _ in range(queue.MAX_ATTEMPTS):
        assert queue.claim_next(session, "w", now=now) is not None
        now += timedelta(seconds=300)
        assert len(queue.requeue_stale(session, now=now)) == 1
    session.expire_all()
    job = session.scalars(sa.select(Job)).one()
    assert (job.state, job.outcome) == ("failed", "abandoned")
    assert queue.claim_next(session, "w", now=now) is None
