"""Each user's Gemini key: sealed at rest, checked once, used only for them.

Encryption: AES-256-GCM (the `cryptography` package). The master key is
`LORE_KEY_ENCRYPTION_KEY` (32 random bytes, base64; `lore admin
generate-master-key` prints one). Every seal uses a fresh 96-bit nonce, and
the associated data binds the ciphertext to the user id, so a row copied to
another user's id does not open. A tampered byte, the wrong user or the wrong
master key all fail the same way: KeyDecryptError, with a fixed message.

Rotation: each row records `key_version`, a short fingerprint of the master
key that sealed it. To rotate, set the new key as LORE_KEY_ENCRYPTION_KEY and
the previous one as LORE_KEY_ENCRYPTION_KEY_OLD, run `lore admin rotate-keys`
(re-seals every row under the new key), then drop the OLD variable.

The plaintext key exists only in memory while a client uses it: it is never
logged, never in an exception message or repr, never in an API response
(only its last four characters are shown back).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import NamedTuple, Protocol

import sqlalchemy as sa
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from lore.auth.settings import AuthConfigError
from lore.central import queue
from lore.central.models import ApiKey, Usage
from lore.inference.key_scope import KeyRequired

log = logging.getLogger("lore.keys")

ENV_MASTER = "LORE_KEY_ENCRYPTION_KEY"
ENV_MASTER_OLD = "LORE_KEY_ENCRYPTION_KEY_OLD"
PROVIDER = "gemini"
MASTER_BYTES = 32
NONCE_BYTES = 12

# The free tier, roughly: shown as "used today / about 1,000". The Lite
# models give 500 a day each (lore.inference.gemini.MODEL_LIMITS).
DAILY_ALLOWANCE = 1000

# What the key check said (KeyChecker).
CHECK_OK = "ok"                    # Gemini accepted the key
CHECK_INVALID = "invalid"          # Gemini refused it: not stored
CHECK_UNAVAILABLE = "unavailable"  # quota, overload or network: stored, with a warning

# ApiKey.last_error codes (fixed strings, never provider text).
ERR_REJECTED = "rejected"    # Gemini refused the key during a question or a job
ERR_UNCHECKED = "unchecked"  # saved without a successful check

# A Gemini key today is "AIza" plus 35 URL-safe characters; the rule is kept
# a little looser (any 30-128 URL-safe characters) so a new key format is not
# refused here. The live check is the real test.
_KEY_SHAPE = re.compile(r"[A-Za-z0-9_\-.]{30,128}")


class KeyConfigError(AuthConfigError):
    """The master key is missing or malformed: multi mode does not start."""


class KeyDecryptError(Exception):
    """A sealed key did not open. Never carries key material."""

    def __init__(self, message: str = "the stored key could not be decrypted") -> None:
        super().__init__(message)


class KeyFormatError(ValueError):
    """The pasted text is not shaped like a Gemini key. Never echoes it."""


def _fingerprint(key: bytes) -> str:
    return hashlib.sha256(b"lore-master-key-id\x00" + key).hexdigest()[:12]


def _decode(name: str, text: str) -> bytes:
    try:
        raw = base64.b64decode(text.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise KeyConfigError(f"{name} is not base64") from None
    if len(raw) != MASTER_BYTES:
        raise KeyConfigError(f"{name} must decode to {MASTER_BYTES} bytes"
                             f" (got {len(raw)})")
    return raw


def generate_master_key() -> str:
    return base64.b64encode(secrets.token_bytes(MASTER_BYTES)).decode()


@dataclass(frozen=True)
class MasterKeys:
    """The current master key and, during a rotation, the previous one."""

    current: bytes = field(repr=False)
    old: bytes | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        for value in (self.current, self.old):
            if value is not None and len(value) != MASTER_BYTES:
                raise KeyConfigError(f"a master key is {MASTER_BYTES} bytes")
        if self.old is not None and self.old == self.current:
            raise KeyConfigError(f"{ENV_MASTER_OLD} must differ from {ENV_MASTER}")

    @property
    def version(self) -> str:
        return _fingerprint(self.current)

    def for_version(self, version: str) -> bytes | None:
        for key in (self.current, self.old):
            if key is not None and _fingerprint(key) == version:
                return key
        return None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "MasterKeys":
        env = os.environ if environ is None else environ
        current = env.get(ENV_MASTER, "").strip()
        if not current:
            raise KeyConfigError(
                f"multi-user mode needs {ENV_MASTER} (32 random bytes, base64;"
                " `lore admin generate-master-key` prints one)")
        old = env.get(ENV_MASTER_OLD, "").strip()
        return cls(_decode(ENV_MASTER, current),
                   _decode(ENV_MASTER_OLD, old) if old else None)


class Sealed(NamedTuple):
    ciphertext: bytes
    nonce: bytes
    key_version: str


def _aad(user_id: str) -> bytes:
    return f"lore/api_keys/{PROVIDER}/{user_id}".encode()


def seal(master: MasterKeys, user_id: str, plaintext: str) -> Sealed:
    nonce = secrets.token_bytes(NONCE_BYTES)
    ciphertext = AESGCM(master.current).encrypt(nonce, plaintext.encode(), _aad(user_id))
    return Sealed(ciphertext, nonce, master.version)


def unseal(master: MasterKeys, user_id: str, sealed: Sealed) -> str:
    key = master.for_version(sealed.key_version)
    if key is None:
        raise KeyDecryptError("the stored key was sealed with a master key this"
                              f" server does not have (set {ENV_MASTER_OLD})")
    try:
        return AESGCM(key).decrypt(sealed.nonce, sealed.ciphertext,
                                   _aad(user_id)).decode()
    except (InvalidTag, ValueError):
        raise KeyDecryptError() from None


def normalize_key(text: object) -> str:
    """The key without surrounding whitespace, or KeyFormatError."""
    key = text.strip() if isinstance(text, str) else ""
    if not _KEY_SHAPE.fullmatch(key):
        raise KeyFormatError("that doesn't look like a Gemini API key")
    return key


def rotate_all(sessions: sessionmaker[Session], master: MasterKeys) -> int:
    """Re-seal every key not already under the current master key; the count."""
    done = 0
    with sessions() as db:
        rows = db.scalars(sa.select(ApiKey).where(
            ApiKey.key_version != master.version)).all()
        for row in rows:
            plaintext = unseal(master, row.user_id,
                               Sealed(row.ciphertext, row.nonce, row.key_version))
            fresh = seal(master, row.user_id, plaintext)
            row.ciphertext, row.nonce, row.key_version = fresh
            done += 1
        db.commit()
    return done


# --- checking a key ----------------------------------------------------------------

class KeyChecker(Protocol):
    def check(self, key: str) -> str:
        """CHECK_OK, CHECK_INVALID or CHECK_UNAVAILABLE. Must not raise."""


class GeminiKeyChecker:
    """One cheap request: list one model. Spends no generation quota.

    Gemini answers a bad, expired or revoked key with 400 (API_KEY_INVALID),
    401 or 403; anything else that is not 200 (429, 5xx, no network) means
    "could not tell". Only the status is looked at: the response text and
    the exception are never logged (the request carries the key).
    """

    URL = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1"

    def __init__(self, transport=None, timeout: float = 10.0) -> None:
        self._transport, self._timeout = transport, timeout

    def check(self, key: str) -> str:
        import httpx
        try:
            with httpx.Client(transport=self._transport, timeout=self._timeout) as http:
                response = http.get(self.URL, headers={"x-goog-api-key": key})
        except Exception as exc:  # noqa: BLE001 - any failure is "could not tell"
            log.warning("Gemini key check could not reach Gemini (%s)", type(exc).__name__)
            return CHECK_UNAVAILABLE
        if response.status_code == 200:
            return CHECK_OK
        if response.status_code in (400, 401, 403):
            return CHECK_INVALID
        log.warning("Gemini key check: HTTP %s", response.status_code)
        return CHECK_UNAVAILABLE


# --- the service the web app and the worker use --------------------------------------

@dataclass(frozen=True)
class SaveOutcome:
    stored: bool
    check: str


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


class KeyService:
    """Keys, usage and the pause that follows a rejected key, for one server.

    `key_for(user_id)` is THE factory input: the web ask path and the worker
    call it and bind the result (lore.inference.key_scope) for one unit of
    work; nothing else decrypts.
    """

    def __init__(self, sessions: sessionmaker[Session], master: MasterKeys,
                 checker: KeyChecker | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.sessions = sessions
        self._master = master
        self._checker = checker or GeminiKeyChecker()
        self._now = clock or (lambda: datetime.now(timezone.utc))

    def __repr__(self) -> str:
        return "KeyService(...)"

    # keys

    def status(self, user_id: str) -> dict:
        with self.sessions() as db:
            row = db.get(ApiKey, user_id)
        if row is None:
            return {"present": False, "last4": None, "added_at": None,
                    "last_ok_at": None, "last_error": None}
        return {"present": True, "last4": row.last4, "added_at": _iso(row.added_at),
                "last_ok_at": _iso(row.last_ok_at), "last_error": row.last_error}

    def has_key(self, user_id: str) -> bool:
        with self.sessions() as db:
            return db.get(ApiKey, user_id) is not None

    def save(self, user_id: str, text: object) -> SaveOutcome:
        """Check the key once; store it (sealed) unless Gemini refused it.

        A key that could not be checked (quota, overload, network) is stored
        with `last_error = unchecked`: the next real request tells. Saving a
        working key wakes the user's paused jobs.
        """
        key = normalize_key(text)  # KeyFormatError before any request
        result = self._checker.check(key)
        if result == CHECK_INVALID:
            return SaveOutcome(False, result)
        now = self._now()
        sealed = seal(self._master, user_id, key)
        with self.sessions() as db:
            row = db.get(ApiKey, user_id)
            if row is None:
                row = ApiKey(user_id=user_id, provider=PROVIDER)
                db.add(row)
            row.ciphertext, row.nonce, row.key_version = sealed
            row.last4, row.added_at = key[-4:], now
            row.last_ok_at = now if result == CHECK_OK else None
            row.last_error = None if result == CHECK_OK else ERR_UNCHECKED
            queue.resume_user(db, user_id)  # commits
        return SaveOutcome(True, result)

    def remove(self, user_id: str) -> bool:
        with self.sessions() as db:
            row = db.get(ApiKey, user_id)
            if row is None:
                return False
            db.delete(row)
            db.commit()
        return True

    def key_for(self, user_id: str) -> str:
        """The user's plaintext key, for one unit of work; KeyRequired if none."""
        with self.sessions() as db:
            row = db.get(ApiKey, user_id)
        if row is None:
            raise KeyRequired()
        return unseal(self._master, user_id,
                      Sealed(row.ciphertext, row.nonce, row.key_version))

    def mark_rejected(self, user_id: str) -> None:
        """Gemini refused the key: say so on the key, and pause the user's
        queued jobs (they resume when a working key is saved)."""
        with self.sessions() as db:
            row = db.get(ApiKey, user_id)
            if row is not None:
                row.last_error = ERR_REJECTED
            queue.pause_user(db, user_id)  # commits

    def mark_ok(self, user_id: str) -> None:
        with self.sessions() as db:
            row = db.get(ApiKey, user_id)
            if row is not None:
                row.last_ok_at, row.last_error = self._now(), None
                db.commit()

    # usage

    def _day(self) -> str:
        from lore.orchestrator.gemini_budget import today
        return today(self._now())

    def record_request(self, user_id: str, model: str) -> None:
        """One more request today on `model` for this user (central `usage`).

        A failed write is logged and swallowed: this count is for display;
        the budget itself is guarded by the ledger in the user's lore.db.
        """
        day = self._day()
        try:
            with self.sessions() as db:
                for _ in range(2):
                    hit = db.execute(
                        sa.update(Usage)
                        .where(Usage.user_id == user_id, Usage.day == day,
                               Usage.model == model)
                        .values(requests=Usage.requests + 1)
                        .execution_options(synchronize_session=False))
                    if hit.rowcount:
                        db.commit()
                        return
                    db.add(Usage(user_id=user_id, day=day, model=model, requests=1))
                    try:
                        db.commit()
                        return
                    except IntegrityError:  # another process inserted it first
                        db.rollback()
        except Exception:  # noqa: BLE001 - see the docstring
            log.exception("could not record a request in the usage table")

    def usage_today(self, user_id: str) -> dict:
        day = self._day()
        with self.sessions() as db:
            rows = db.execute(sa.select(Usage.model, Usage.requests).where(
                Usage.user_id == user_id, Usage.day == day)).all()
        return {"day": day, "requests": sum(r for _, r in rows),
                "limit": DAILY_ALLOWANCE,
                "models": {m: r for m, r in sorted(rows)}}

    def usage_by_user(self) -> dict[str, int]:
        """Today's requests per user (the admin's users list)."""
        day = self._day()
        with self.sessions() as db:
            rows = db.execute(sa.select(Usage.user_id, sa.func.sum(Usage.requests))
                              .where(Usage.day == day).group_by(Usage.user_id)).all()
        return {u: int(n) for u, n in rows}
