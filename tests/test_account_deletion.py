"""Admin controls and account deletion (release step 6, task 4).

The cascade test fills every central table (and the library directory) for
two users, deletes one, and checks that every row and file of theirs is
gone, that the other is untouched, that the waitlist is anonymised, and that
an interrupted deletion finishes when it is run again."""
from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from lore import tracing
from lore.auth import ratelimit
from lore.auth import sessions as auth_sessions
from lore.auth.accounts import create_invite
from lore.auth.tokens import hash_token
from lore.central import erase, queue
from lore.central import history as H
from lore.central import traces as T
from lore.central.models import (
    AdminEvent, ApiKey, AuthSession, Chat, Identity, InflightSlot, Invite, Job,
    LoginToken, Message, RateHit, Report, Trace, Usage, User, Waitlist,
)
from lore.config import LoreConfig
from lore.tenancy import config_for
from tests.auth_fixtures import BASE_URL, NOW, Clock, FakeMailer, give_key, make_app, sign_in
from tests.central_fixtures import central_sessions

ALICE = str(uuid.UUID(int=1))
BOB = str(uuid.UUID(int=2))
ROOT = str(uuid.UUID(int=3))

# Every table that holds a user's rows, with how to find them.
OWNED = {
    "sessions": (AuthSession, lambda uid, email: AuthSession.user_id == uid),
    "identities": (Identity, lambda uid, email: Identity.user_id == uid),
    "api_keys": (ApiKey, lambda uid, email: ApiKey.user_id == uid),
    "chats": (Chat, lambda uid, email: Chat.user_id == uid),
    "messages": (Message, lambda uid, email: Message.chat_id.in_(
        sa.select(Chat.id).where(Chat.user_id == uid))),
    "traces": (Trace, lambda uid, email: Trace.user_id == uid),
    "reports": (Report, lambda uid, email: Report.user_id == uid),
    "usage": (Usage, lambda uid, email: Usage.user_id == uid),
    "jobs": (Job, lambda uid, email: Job.user_id == uid),
    "inflight_slots": (InflightSlot, lambda uid, email: InflightSlot.user_id == uid),
    "rate_hits": (RateHit, lambda uid, email: RateHit.key.in_(
        [ratelimit._key(k, uid) for k in erase.USER_RATE_KINDS])),
    "login_tokens": (LoginToken, lambda uid, email: LoginToken.email == email),
    "waitlist": (Waitlist, lambda uid, email: Waitlist.email == email),
    "invites_used": (Invite, lambda uid, email: Invite.used_by == uid),
    "users": (User, lambda uid, email: User.id == uid),
}


def email_of(uid):
    return {ALICE: "alice@example.org", BOB: "bob@example.org", ROOT: "root@example.org"}[uid]


@pytest.fixture
def central(tmp_path):
    sessions, _ = central_sessions(tmp_path, users=())
    with sessions() as db:
        for uid in (ALICE, BOB, ROOT):
            db.add(User(id=uid, email=email_of(uid), role="admin" if uid == ROOT else "user"))
        db.commit()
    return sessions


@pytest.fixture
def root(tmp_path):
    return LoreConfig(data_dir=tmp_path / "root")


@pytest.fixture
def app(root, central):
    return make_app(root, central, mailer=FakeMailer())


def populate(central, root: LoreConfig, uid: str) -> Path:
    """One of everything for `uid`, and a library with files in it."""
    email = email_of(uid)
    give_key(central, uid)
    with central() as db:
        auth_sessions.create_session(db, uid, NOW)
        db.add(Identity(provider="email", provider_user_id=email, user_id=uid))
        chat, _ = H.create_chat(db, uid, title="t", now=NOW)
        H.append_message(db, uid, chat.id, question="q", answer={"result": {}}, depth=None,
                         world=None, request_id=f"rid-{uid}", now=NOW)
        t = tracing.Trace(request_id=f"rid-{uid}")
        t.finish(answer={"answer": "a"})
        T.save(db, t, user_id=uid, question="q", chat_id=chat.id, now=NOW)
        db.add(Report(user_id=uid, request_id=f"rid-{uid}", note="n", status="open"))
        db.add(Usage(user_id=uid, day="2026-11-04", model="m", requests=3))
        queue.enqueue(db, uid, "book1", "process")
        db.add(InflightSlot(kind=f"ask:user:{uid}", slot=0, holder="h", user_id=uid,
                            expires_at=NOW + timedelta(minutes=5)))
        ratelimit.allow(db, "ask", uid, 10, NOW)
        ratelimit.allow(db, "upload", uid, 10, NOW)
        db.add(LoginToken(token_hash=hash_token(f"tok-{uid}"), email=email,
                          expires_at=NOW + timedelta(minutes=5)))
        db.add(Waitlist(email=email, reading="Dune", status="invited"))
        code = create_invite(db, ROOT, NOW, email=email)
        db.get(Invite, hash_token(code)).used_by = uid
        db.add(AdminEvent(admin_id=ROOT, action="enable", target_user_id=uid,
                          at=NOW - timedelta(days=1)))
        db.commit()
    lib = config_for(root, uid).data_dir
    (lib / "kuzu").mkdir()
    (lib / "kuzu" / "catalog.kz").write_bytes(b"graph")
    (lib / "lore.db").write_bytes(b"sqlite")
    return lib


def counts(central, uid):
    email = email_of(uid)
    with central() as db:
        return {name: db.scalar(sa.select(sa.func.count()).select_from(model).where(where(uid, email)))
                for name, (model, where) in OWNED.items()}


# --- the cascade --------------------------------------------------------------------

def test_deleting_an_account_removes_every_row_and_file_and_nothing_else(app, central, root):
    alice_lib, bob_lib = populate(central, root, ALICE), populate(central, root, BOB)
    before_bob = counts(central, BOB)
    assert all(before_bob.values()), before_bob  # the fixture really fills every table
    assert all(counts(central, ALICE).values())

    admin = sign_in(app, central, ROOT)
    r = admin.delete(f"/api/admin/users/{ALICE}")
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": True, "library_removed": True}

    assert counts(central, ALICE) == dict.fromkeys(OWNED, 0)
    assert not alice_lib.exists()
    assert counts(central, BOB) == before_bob
    assert (bob_lib / "kuzu" / "catalog.kz").read_bytes() == b"graph"
    with central() as db:
        # The waitlist row is kept, anonymised; the invite is kept, unbound.
        anon = db.scalars(sa.select(Waitlist).where(Waitlist.email == erase.ANONYMISED)).all()
        assert len(anon) == 1 and anon[0].reading is None
        assert db.scalar(sa.select(sa.func.count()).select_from(Invite)
                         .where(Invite.email == email_of(ALICE))) == 0
        # The admin log names the action and the id, nothing else.
        events = db.scalars(sa.select(AdminEvent).where(AdminEvent.action == "delete_user")).all()
        assert [(e.admin_id, e.target_user_id) for e in events] == [(ROOT, ALICE)]


def test_deleting_is_idempotent(app, central, root):
    populate(central, root, ALICE)
    admin = sign_in(app, central, ROOT)
    assert admin.delete(f"/api/admin/users/{ALICE}").status_code == 200
    # The row is gone, so a second admin call is 404, and the function itself
    # is a no-op that still succeeds.
    assert admin.delete(f"/api/admin/users/{ALICE}").status_code == 404
    done = erase.erase_user(central, root.data_dir, ALICE, now=NOW)
    assert done.library_removed and set(done.rows.values()) == {0}


def test_an_interrupted_deletion_finishes_when_run_again(app, central, root, monkeypatch):
    lib = populate(central, root, ALICE)
    real = erase.remove_library

    def crash(root_, uid):
        raise OSError("disk went away")
    monkeypatch.setattr(erase, "remove_library", crash)
    with pytest.raises(OSError):
        erase.erase_user(central, root.data_dir, ALICE, now=NOW)
    with central() as db:  # locked: deleting, signed out, but not yet gone
        assert db.get(User, ALICE).status == "deleting"
        assert db.scalar(sa.select(sa.func.count()).select_from(AuthSession)
                         .where(AuthSession.user_id == ALICE)) == 0
    assert lib.exists()
    monkeypatch.setattr(erase, "remove_library", real)
    admin = sign_in(app, central, ROOT)
    assert admin.delete(f"/api/admin/users/{ALICE}").status_code == 200
    assert counts(central, ALICE) == dict.fromkeys(OWNED, 0)
    assert not lib.exists()


def test_a_running_job_keeps_the_library_until_the_worker_is_done(app, central, root):
    lib = populate(central, root, ALICE)
    with central() as db:
        job = queue.claim_next(db, "w1", now=NOW)
        assert job.user_id in (ALICE,)
    done = erase.erase_user(central, root.data_dir, ALICE, now=NOW)
    assert not done.library_removed and lib.exists()
    assert counts(central, ALICE)["users"] == 0
    # The worker, after its job, removes what is left.
    assert erase.finish_pending(central, root.data_dir, ALICE)
    assert not lib.exists()
    assert not erase.finish_pending(central, root.data_dir, BOB)  # an active user: nothing


def test_the_worker_finishes_a_deletion_after_its_job(central, root):
    from lore.worker import Worker
    from lore.central.keys import KeyService
    from tests.auth_fixtures import FakeKeyChecker, master_keys
    lib = populate(central, root, ALICE)
    keys = KeyService(central, master_keys(), FakeKeyChecker(), clock=Clock())

    def stages(cfg):  # the account is deleted while this job runs
        erase.erase_user(central, root.data_dir, ALICE, now=NOW)
        return []
    with central() as db:  # only alice's job is waiting
        db.execute(sa.delete(Job).where(Job.user_id != ALICE))
        db.commit()
    from lore.db.sqlite import open_db
    lib_cfg = config_for(root, ALICE)
    (lib_cfg.data_dir / "lore.db").unlink()
    conn = open_db(lib_cfg.sqlite_path)
    conn.execute("INSERT INTO books (id, title, source, file_path) VALUES ('book1', 'B', 'upload', 'x')")
    conn.commit()
    conn.close()
    Worker(root, central, keys=keys, make_stages=stages, clock=lambda: NOW).run_once()
    assert not lib.exists()


# --- self-service ----------------------------------------------------------------------

def test_a_user_deletes_their_own_account_with_a_typed_confirmation(app, central, root):
    lib = populate(central, root, ALICE)
    alice = sign_in(app, central, ALICE)
    r = alice.post("/api/me/delete", json={"confirm": "not my address"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "confirm_mismatch"
    assert lib.exists()
    r = alice.post("/api/me/delete", json={"confirm": " Alice@Example.org "})
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert any(c.startswith("lore_session=;") for c in r.headers.get_list("set-cookie"))
    assert counts(central, ALICE) == dict.fromkeys(OWNED, 0)
    assert not lib.exists()
    assert alice.get("/api/me").status_code == 401


def test_self_deletion_needs_csrf_and_a_session(app, central, root):
    populate(central, root, ALICE)
    alice = sign_in(app, central, ALICE)
    r = alice.post("/api/me/delete", json={"confirm": email_of(ALICE)},
                   headers={"X-CSRF-Token": "forged"})
    assert r.status_code == 403
    anon = TestClient(app, base_url=BASE_URL)
    assert anon.post("/api/me/delete", json={"confirm": email_of(ALICE)}).status_code == 401
    assert counts(central, ALICE)["users"] == 1


# --- admin controls ----------------------------------------------------------------------

def test_non_admins_get_403_on_every_control(app, central, root):
    populate(central, root, BOB)
    alice = sign_in(app, central, ALICE)
    for method, path in (("post", f"/api/admin/users/{BOB}/disable"),
                         ("post", f"/api/admin/users/{BOB}/enable"),
                         ("post", f"/api/admin/users/{BOB}/revoke-sessions"),
                         ("delete", f"/api/admin/users/{BOB}"),
                         ("get", "/api/admin/events")):
        assert getattr(alice, method)(path).status_code == 403, path
    assert counts(central, BOB)["users"] == 1 and counts(central, BOB)["sessions"] == 1


def test_disable_revokes_sessions_refuses_sign_in_and_pauses_jobs(app, central, root):
    populate(central, root, ALICE)
    alice = sign_in(app, central, ALICE)
    assert alice.get("/api/me").status_code == 200
    admin = sign_in(app, central, ROOT)
    r = admin.post(f"/api/admin/users/{ALICE}/disable")
    assert r.status_code == 200 and r.json()["user"]["status"] == "disabled"
    assert alice.get("/api/me").status_code == 401         # every session, at once
    assert counts(central, ALICE)["sessions"] == 0
    with central() as db:
        assert {j.state for j in db.scalars(sa.select(Job).where(Job.user_id == ALICE))} == {"paused"}
        # A worker would not claim a disabled user's job even if it were queued.
        db.execute(sa.update(Job).where(Job.user_id == ALICE).values(state="queued"))
        db.execute(sa.delete(Job).where(Job.user_id != ALICE))
        db.commit()
        assert queue.claim_next(db, "w1", now=NOW) is None
    # Sign-in is refused: a magic link is not even sent.
    mailer = FakeMailer()
    app2 = make_app(root, central, mailer=mailer)
    anon = TestClient(app2, base_url=BASE_URL)
    assert anon.post("/auth/email", json={"email": email_of(ALICE)}).status_code == 202
    assert mailer.sent == []
    # Re-enabled: jobs run again and sign-in works.
    r = admin.post(f"/api/admin/users/{ALICE}/enable")
    assert r.json()["user"]["status"] == "active"
    with central() as db:
        assert queue.claim_next(db, "w1", now=NOW) is not None
    anon.post("/auth/email", json={"email": email_of(ALICE)})
    assert len(mailer.sent) == 1


def test_revoke_one_users_sessions(app, central, root):
    populate(central, root, ALICE)
    alice, bob = sign_in(app, central, ALICE), sign_in(app, central, BOB)
    admin = sign_in(app, central, ROOT)
    r = admin.post(f"/api/admin/users/{ALICE}/revoke-sessions")
    assert r.status_code == 200 and r.json()["revoked"] == 2  # the fixture's and this one
    assert alice.get("/api/me").status_code == 401
    assert bob.get("/api/me").status_code == 200
    with central() as db:
        assert db.get(User, ALICE).status == "active"  # not disabled: may sign in again


def test_an_admin_cannot_disable_or_delete_themselves(app, central):
    admin = sign_in(app, central, ROOT)
    assert admin.post(f"/api/admin/users/{ROOT}/disable").status_code == 409
    assert admin.delete(f"/api/admin/users/{ROOT}").status_code == 409
    assert admin.post("/api/admin/users/nobody/disable").status_code == 404


def test_every_admin_action_is_logged(app, central, root):
    populate(central, root, ALICE)
    admin = sign_in(app, central, ROOT)
    admin.post(f"/api/admin/users/{ALICE}/disable")
    admin.post(f"/api/admin/users/{ALICE}/enable")
    admin.post(f"/api/admin/users/{ALICE}/revoke-sessions")
    admin.post("/api/admin/invites", json={"days": 3})
    admin.get(f"/api/admin/traces/rid-{ALICE}")
    r = admin.get("/api/admin/events")
    assert r.status_code == 200
    got = [(e["action"], e["target_user_id"]) for e in r.json()["events"]]
    assert got[:5] == [("view_trace", ALICE), ("create_invite", None),
                       ("revoke_sessions", ALICE), ("enable", ALICE), ("disable", ALICE)]
    assert all(e["admin"] == email_of(ROOT) for e in r.json()["events"][:5])
    assert email_of(ALICE) not in r.text
