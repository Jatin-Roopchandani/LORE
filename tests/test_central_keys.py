"""Each user's Gemini key, encrypted at rest (release step 3, task 1).

AES-256-GCM with the user id as associated data: a ciphertext only opens for
the user it was sealed for, a flipped byte fails, and rotation re-seals every
key under the new master key. All offline."""
import base64
import os
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from lore.central import keys as K
from lore.central.models import ApiKey, Usage
from tests.central_fixtures import central_sessions

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
FAKE_KEY = "AIzaFAKEfakeFAKEfakeFAKEfakeFAKEfake123"  # shaped like a key, not one
OTHER_KEY = "AIzaOTHERotherOTHERotherOTHERother45678"


def master(seed: int = 1) -> bytes:
    return bytes([seed]) * 32


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


@pytest.fixture
def sessions(tmp_path):
    s, _ = central_sessions(tmp_path, users=("alice", "bob"))
    return s


class Checker:
    """A fake key check: no network. `result` is what Gemini 'said'."""

    def __init__(self, result: str = K.CHECK_OK) -> None:
        self.result, self.seen = result, []

    def check(self, key: str) -> str:
        self.seen.append(key)
        return self.result


def service(sessions, *, keys=None, checker=None, now=NOW):
    return K.KeyService(sessions, keys or K.MasterKeys(master(1)),
                        checker or Checker(), clock=lambda: now)


# --- the cipher ------------------------------------------------------------------

def test_a_key_round_trips():
    mk = K.MasterKeys(master(1))
    sealed = K.seal(mk, "alice", FAKE_KEY)
    assert FAKE_KEY.encode() not in sealed.ciphertext
    assert len(sealed.nonce) == 12
    assert K.unseal(mk, "alice", sealed) == FAKE_KEY


def test_each_seal_uses_a_fresh_nonce():
    mk = K.MasterKeys(master(1))
    a, b = K.seal(mk, "alice", FAKE_KEY), K.seal(mk, "alice", FAKE_KEY)
    assert a.nonce != b.nonce and a.ciphertext != b.ciphertext


def test_a_ciphertext_does_not_open_for_another_user():
    mk = K.MasterKeys(master(1))
    sealed = K.seal(mk, "alice", FAKE_KEY)
    with pytest.raises(K.KeyDecryptError):
        K.unseal(mk, "bob", sealed)


@pytest.mark.parametrize("field", ["ciphertext", "nonce"])
def test_a_tampered_byte_fails(field):
    mk = K.MasterKeys(master(1))
    sealed = K.seal(mk, "alice", FAKE_KEY)
    raw = bytearray(getattr(sealed, field))
    raw[0] ^= 0x01
    with pytest.raises(K.KeyDecryptError) as err:
        K.unseal(mk, "alice", sealed._replace(**{field: bytes(raw)}))
    assert FAKE_KEY not in str(err.value) and FAKE_KEY not in repr(err.value)


def test_the_wrong_master_key_fails():
    sealed = K.seal(K.MasterKeys(master(1)), "alice", FAKE_KEY)
    with pytest.raises(K.KeyDecryptError):
        K.unseal(K.MasterKeys(master(2)), "alice", sealed)


def test_the_old_master_key_still_opens_its_own_version():
    sealed = K.seal(K.MasterKeys(master(1)), "alice", FAKE_KEY)
    rotated = K.MasterKeys(master(2), old=master(1))
    assert sealed.key_version != rotated.version
    assert K.unseal(rotated, "alice", sealed) == FAKE_KEY


def test_master_keys_come_from_the_environment():
    env = {K.ENV_MASTER: b64(master(3)), K.ENV_MASTER_OLD: b64(master(4))}
    mk = K.MasterKeys.from_env(env)
    assert mk.current == master(3) and mk.old == master(4)
    assert K.MasterKeys.from_env({K.ENV_MASTER: b64(master(3))}).old is None


@pytest.mark.parametrize("env", [
    {},
    {K.ENV_MASTER: ""},
    {K.ENV_MASTER: "not base64 at all!"},
    {K.ENV_MASTER: b64(b"x" * 16)},                     # 16 bytes, not 32
    {K.ENV_MASTER: b64(master(1)), K.ENV_MASTER_OLD: b64(b"y" * 31)},
    {K.ENV_MASTER: b64(master(1)), K.ENV_MASTER_OLD: b64(master(1))},  # same key
])
def test_a_missing_or_malformed_master_key_is_refused(env):
    from lore.auth.settings import AuthConfigError
    with pytest.raises(K.KeyConfigError) as err:
        K.MasterKeys.from_env(env)
    assert isinstance(err.value, AuthConfigError)  # `lore ui` reports it like the rest


def test_reprs_hide_the_master_keys():
    mk = K.MasterKeys(master(5), old=master(6))
    assert b64(master(5)) not in repr(mk) and repr(master(5)) not in repr(mk)
    assert repr(master(6)) not in repr(mk)


def test_generate_makes_a_usable_master_key():
    text = K.generate_master_key()
    assert len(base64.b64decode(text)) == 32
    K.MasterKeys.from_env({K.ENV_MASTER: text})


# --- storage -----------------------------------------------------------------------

def test_a_saved_key_is_stored_encrypted_with_its_last_four(sessions):
    svc = service(sessions)
    outcome = svc.save("alice", FAKE_KEY)
    assert outcome.stored and outcome.check == K.CHECK_OK
    with sessions() as db:
        row = db.get(ApiKey, ("alice", "gemini"))
        assert row.provider == "gemini" and row.last4 == FAKE_KEY[-4:]
        assert FAKE_KEY.encode() not in row.ciphertext
        assert row.added_at == NOW and row.last_ok_at == NOW and row.last_error is None
        assert row.key_version == K.MasterKeys(master(1)).version
    assert svc.key_for("alice") == FAKE_KEY


def test_the_status_never_carries_the_key(sessions):
    svc = service(sessions)
    assert svc.status("alice") == {"provider": "gemini", "present": False,
                                   "last4": None, "added_at": None,
                                   "last_ok_at": None, "last_error": None}
    svc.save("alice", FAKE_KEY)
    st = svc.status("alice")
    assert st["present"] and st["last4"] == FAKE_KEY[-4:]
    assert FAKE_KEY not in repr(st)


def test_a_user_without_a_key_gets_key_required(sessions):
    from lore.inference.key_scope import KeyRequired
    with pytest.raises(KeyRequired):
        service(sessions).key_for("alice")


def test_users_keys_are_separate(sessions):
    svc = service(sessions)
    svc.save("alice", FAKE_KEY)
    svc.save("bob", OTHER_KEY)
    assert svc.key_for("alice") == FAKE_KEY and svc.key_for("bob") == OTHER_KEY


def test_a_ciphertext_moved_to_another_user_does_not_open(sessions):
    svc = service(sessions)
    svc.save("alice", FAKE_KEY)
    svc.save("bob", OTHER_KEY)
    with sessions() as db:  # an attacker with DB write access swaps the rows
        a = db.get(ApiKey, ("alice", "gemini"))
        b = db.get(ApiKey, ("bob", "gemini"))
        a.ciphertext, a.nonce, b.ciphertext, b.nonce = b.ciphertext, b.nonce, a.ciphertext, a.nonce
        db.commit()
    with pytest.raises(K.KeyDecryptError):
        svc.key_for("alice")


def test_replacing_a_key_overwrites_the_row(sessions):
    svc = service(sessions)
    svc.save("alice", FAKE_KEY)
    svc.save("alice", OTHER_KEY)
    assert svc.key_for("alice") == OTHER_KEY
    with sessions() as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(ApiKey)) == 1


def test_an_invalid_key_is_not_stored(sessions):
    svc = service(sessions, checker=Checker(K.CHECK_INVALID))
    outcome = svc.save("alice", FAKE_KEY)
    assert not outcome.stored and outcome.check == K.CHECK_INVALID
    assert svc.status("alice")["present"] is False


def test_a_key_that_could_not_be_checked_is_stored_with_a_warning(sessions):
    svc = service(sessions, checker=Checker(K.CHECK_UNAVAILABLE))
    outcome = svc.save("alice", FAKE_KEY)
    assert outcome.stored and outcome.check == K.CHECK_UNAVAILABLE
    st = svc.status("alice")
    assert st["present"] and st["last_ok_at"] is None and st["last_error"] == K.ERR_UNCHECKED


@pytest.mark.parametrize("bad", ["", "   ", "short", "AIza with spaces in the middle ok",
                                 "x" * 300, "AIza\nFAKEfakeFAKEfakeFAKEfakeFAKEfake1234"])
def test_a_malformed_key_is_refused_before_any_check(sessions, bad):
    checker = Checker()
    with pytest.raises(K.KeyFormatError) as err:
        service(sessions, checker=checker).save("alice", bad)
    assert checker.seen == []
    assert bad.strip() == "" or bad not in str(err.value)


def test_surrounding_whitespace_is_trimmed(sessions):
    svc = service(sessions)
    svc.save("alice", f"  {FAKE_KEY}\n")
    assert svc.key_for("alice") == FAKE_KEY


def test_remove_deletes_the_key(sessions):
    svc = service(sessions)
    svc.save("alice", FAKE_KEY)
    assert svc.remove("alice") is True
    assert svc.status("alice")["present"] is False
    assert svc.remove("alice") is False


def test_mark_rejected_records_the_error_and_a_new_key_clears_it(sessions):
    svc = service(sessions)
    svc.save("alice", FAKE_KEY)
    svc.mark_rejected("alice")
    assert svc.status("alice")["last_error"] == K.ERR_REJECTED
    svc.save("alice", OTHER_KEY)
    assert svc.status("alice")["last_error"] is None


def test_rotation_reseals_every_key_under_the_new_master(sessions):
    service(sessions, keys=K.MasterKeys(master(1))).save("alice", FAKE_KEY)
    service(sessions, keys=K.MasterKeys(master(1))).save("bob", OTHER_KEY)
    rotated = K.MasterKeys(master(2), old=master(1))
    assert K.rotate_all(sessions, rotated) == 2
    with sessions() as db:
        assert {r.key_version for r in db.scalars(sa.select(ApiKey))} == {rotated.version}
    # The old master key is no longer needed.
    after = service(sessions, keys=K.MasterKeys(master(2)))
    assert after.key_for("alice") == FAKE_KEY and after.key_for("bob") == OTHER_KEY
    assert K.rotate_all(sessions, rotated) == 0  # nothing left to re-seal


def test_rotation_without_the_old_key_reports_what_it_could_not_open(sessions):
    service(sessions, keys=K.MasterKeys(master(1))).save("alice", FAKE_KEY)
    with pytest.raises(K.KeyDecryptError):
        K.rotate_all(sessions, K.MasterKeys(master(2)))


def test_deleting_a_user_deletes_their_key(sessions):
    from lore.central.models import User
    service(sessions).save("alice", FAKE_KEY)
    with sessions() as db:
        db.delete(db.get(User, "alice"))
        db.commit()
        assert db.get(ApiKey, ("alice", "gemini")) is None


# --- usage ------------------------------------------------------------------------

def test_requests_are_counted_per_user_day_and_model(sessions):
    svc = service(sessions)
    for _ in range(3):
        svc.record_request("alice", "gemini-3.1-flash-lite")
    svc.record_request("alice", "gemini-3.8-flash")
    svc.record_request("bob", "gemini-3.1-flash-lite")
    with sessions() as db:
        rows = {(r.user_id, r.day, r.model): r.requests for r in db.scalars(sa.select(Usage))}
    day = "2026-11-04"  # midday UTC is still the 4th in Pacific time
    assert rows == {("alice", day, "gemini-3.1-flash-lite"): 3,
                    ("alice", day, "gemini-3.8-flash"): 1,
                    ("bob", day, "gemini-3.1-flash-lite"): 1}
    assert svc.usage_today("alice")["requests"] == 4
    assert svc.usage_today("bob")["requests"] == 1
    assert svc.usage_today("alice")["limit"] == K.DAILY_ALLOWANCE


def test_the_usage_day_is_googles_pacific_day(sessions):
    late = datetime(2026, 11, 5, 6, 0, tzinfo=timezone.utc)  # 22:00 on the 4th, Pacific
    svc = service(sessions, now=late)
    svc.record_request("alice", "m")
    with sessions() as db:
        assert db.scalar(sa.select(Usage.day)) == "2026-11-04"


# --- the CLI ---------------------------------------------------------------------

def test_lore_admin_rotate_keys(sessions, tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from lore.cli import app
    service(sessions, keys=K.MasterKeys(master(1))).save("alice", FAKE_KEY)
    monkeypatch.setenv("LORE_DATABASE_URL", f"sqlite:///{tmp_path / 'central.db'}")
    monkeypatch.setenv(K.ENV_MASTER, b64(master(2)))
    monkeypatch.setenv(K.ENV_MASTER_OLD, b64(master(1)))
    result = CliRunner().invoke(app, ["admin", "rotate-keys"])
    assert result.exit_code == 0, result.output
    assert "1" in result.output
    assert FAKE_KEY not in result.output
    assert service(sessions, keys=K.MasterKeys(master(2))).key_for("alice") == FAKE_KEY


def test_lore_admin_rotate_keys_refuses_without_a_master_key(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from lore.cli import app
    monkeypatch.setenv("LORE_DATABASE_URL", f"sqlite:///{tmp_path / 'central.db'}")
    monkeypatch.delenv(K.ENV_MASTER, raising=False)
    result = CliRunner().invoke(app, ["admin", "rotate-keys"])
    assert result.exit_code == 2
