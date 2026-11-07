"""Each user's model-provider keys: sealed at rest, checked once, used only for them.

One key per (user, provider): Gemini, Anthropic (Claude) or OpenAI. The user's
`active_provider` decides which one their questions and jobs use
(`credentials_for`), with their chosen model(s).

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
from lore.central.models import ApiKey, Usage, User
from lore.inference.key_scope import KeyRequired
from lore.inference.providers import roles
from lore.inference.providers.base import ANTHROPIC, GEMINI, OPENAI, PROVIDERS

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
# refused here. Claude keys start "sk-ant-", OpenAI keys "sk-" (sk-proj-...,
# sk-svcacct-..., or older sk-...). The live check is the real test.
_KEY_SHAPE = re.compile(r"[A-Za-z0-9_\-.]{30,128}")
_KEY_SHAPES = {
    GEMINI: _KEY_SHAPE,
    ANTHROPIC: re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,256}"),
    OPENAI: re.compile(r"sk-[A-Za-z0-9_\-]{20,256}"),
}


class KeyConfigError(AuthConfigError):
    """The master key is missing or malformed: multi mode does not start."""


class KeyDecryptError(Exception):
    """A sealed key did not open. Never carries key material."""

    def __init__(self, message: str = "the stored key could not be decrypted") -> None:
        super().__init__(message)


class KeyFormatError(ValueError):
    """The pasted text is not shaped like a key for that provider. Never echoes it."""


class ProviderChoiceError(ValueError):
    """A provider or model the user may not switch to (not connected, unknown)."""


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


def _aad(user_id: str, provider: str = PROVIDER) -> bytes:
    # Gemini rows keep the associated data they were sealed with.
    return f"lore/api_keys/{provider}/{user_id}".encode()


def seal(master: MasterKeys, user_id: str, plaintext: str,
         provider: str = PROVIDER) -> Sealed:
    nonce = secrets.token_bytes(NONCE_BYTES)
    ciphertext = AESGCM(master.current).encrypt(nonce, plaintext.encode(),
                                                _aad(user_id, provider))
    return Sealed(ciphertext, nonce, master.version)


def unseal(master: MasterKeys, user_id: str, sealed: Sealed,
           provider: str = PROVIDER) -> str:
    key = master.for_version(sealed.key_version)
    if key is None:
        raise KeyDecryptError("the stored key was sealed with a master key this"
                              f" server does not have (set {ENV_MASTER_OLD})")
    try:
        return AESGCM(key).decrypt(sealed.nonce, sealed.ciphertext,
                                   _aad(user_id, provider)).decode()
    except (InvalidTag, ValueError):
        raise KeyDecryptError() from None


def normalize_key(text: object, provider: str = PROVIDER) -> str:
    """The key without surrounding whitespace, or KeyFormatError."""
    key = text.strip() if isinstance(text, str) else ""
    shape = _KEY_SHAPES.get(provider)
    if shape is None or not shape.fullmatch(key):
        raise KeyFormatError(f"that doesn't look like a {provider} API key")
    return key


def rotate_all(sessions: sessionmaker[Session], master: MasterKeys) -> int:
    """Re-seal every key not already under the current master key; the count."""
    done = 0
    with sessions() as db:
        rows = db.scalars(sa.select(ApiKey).where(
            ApiKey.key_version != master.version)).all()
        for row in rows:
            plaintext = unseal(master, row.user_id,
                               Sealed(row.ciphertext, row.nonce, row.key_version),
                               row.provider)
            fresh = seal(master, row.user_id, plaintext, row.provider)
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


class CheckResult(NamedTuple):
    status: str               # CHECK_OK, CHECK_INVALID or CHECK_UNAVAILABLE
    models: list[str]         # what the key may use (empty when unknown)


class AnthropicKeyChecker:
    """One models-list request (no generation, nothing billed).

    401/403 mean the key is refused; any other failure means "could not
    tell". The models offered are the three LORE supports that the key's
    list shows (all three if the list names none of them: it may be paged
    or filtered, and the first real request tells). Only the exception's
    type is ever logged.
    """

    def __init__(self, client_factory: Callable[[str], object] | None = None) -> None:
        self._factory = client_factory

    def _client(self, key: str):
        if self._factory is not None:
            return self._factory(key)
        import anthropic
        return anthropic.Anthropic(api_key=key, max_retries=0, timeout=10.0)

    def check(self, key: str) -> CheckResult:
        import anthropic
        try:
            ids = [m.id for m in self._client(key).models.list(limit=100)]
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
            return CheckResult(CHECK_INVALID, [])
        except Exception as exc:  # noqa: BLE001 - any other failure: could not tell
            log.warning("Claude key check failed (%s)", type(exc).__name__)
            return CheckResult(CHECK_UNAVAILABLE, list(roles.ANTHROPIC_MODELS))
        offered = [m for m in roles.ANTHROPIC_MODELS if m in ids]
        return CheckResult(CHECK_OK, offered or list(roles.ANTHROPIC_MODELS))


class OpenAIKeyChecker:
    """One models-list request; the chat-capable ids are what the user picks from."""

    def __init__(self, client_factory: Callable[[str], object] | None = None) -> None:
        self._factory = client_factory

    def _client(self, key: str):
        if self._factory is not None:
            return self._factory(key)
        import openai
        return openai.OpenAI(api_key=key, max_retries=0, timeout=10.0)

    def check(self, key: str) -> CheckResult:
        import openai
        try:
            ids = [m.id for m in self._client(key).models.list()]
        except (openai.AuthenticationError, openai.PermissionDeniedError):
            return CheckResult(CHECK_INVALID, [])
        except Exception as exc:  # noqa: BLE001
            log.warning("OpenAI key check failed (%s)", type(exc).__name__)
            return CheckResult(CHECK_UNAVAILABLE, [])
        return CheckResult(CHECK_OK, roles.openai_chat_models(ids))


# --- the service the web app and the worker use --------------------------------------

@dataclass(frozen=True)
class SaveOutcome:
    stored: bool
    check: str
    models: tuple[str, ...] = ()


@dataclass(frozen=True)
class Credentials:
    """What one unit of work needs: the provider, its key and the model(s)."""

    provider: str
    key: str = field(repr=False)
    model: str | None = None
    fast_model: str | None = None
    paid_tier: bool = False  # Gemini: the user's "I'm on a paid Gemini plan"


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def default_model(provider: str, models: list[str] | None) -> str | None:
    """The suggested model when a key is first saved."""
    if provider == ANTHROPIC:
        return roles.ANTHROPIC_DEFAULT
    if provider == OPENAI:
        return roles.suggest_openai_model(list(models or []))
    return None


def _cost(provider: str, model: str, tokens_in: int, tokens_out: int,
          day: str | None = None) -> float | None:
    if provider == ANTHROPIC:
        return roles.anthropic_cost(model, tokens_in, tokens_out)
    if provider == GEMINI:
        # Tokens are recorded only on a paid Gemini plan: a free-tier day (no
        # tokens) has no cost to show, not a cost of $0. A model without a
        # verified price (roles.GEMINI_PRICES) shows tokens only.
        if not (tokens_in or tokens_out):
            return None
        return roles.gemini_cost(model, tokens_in, tokens_out, day)
    return None  # OpenAI: no price list LORE can read


class KeyService:
    """Keys, provider choice, usage and the pause that follows a rejected key.

    `credentials_for(user_id)` is THE factory input: the web ask path and the
    worker call it and bind the result (lore.inference.key_scope) for one unit
    of work; nothing else decrypts. `key_for` is the active provider's key.
    """

    def __init__(self, sessions: sessionmaker[Session], master: MasterKeys,
                 checker: KeyChecker | None = None,
                 clock: Callable[[], datetime] | None = None,
                 checkers: Mapping[str, object] | None = None) -> None:
        self.sessions = sessions
        self._master = master
        self._checkers: dict[str, object] = {
            GEMINI: checker or GeminiKeyChecker(),
            ANTHROPIC: AnthropicKeyChecker(),
            OPENAI: OpenAIKeyChecker(),
        }
        self._checkers.update(checkers or {})
        self._now = clock or (lambda: datetime.now(timezone.utc))

    def __repr__(self) -> str:
        return "KeyService(...)"

    # keys

    def active_provider(self, user_id: str) -> str:
        with self.sessions() as db:
            user = db.get(User, user_id)
        return user.active_provider if user is not None and user.active_provider else GEMINI

    def _row(self, db: Session, user_id: str, provider: str | None = None):
        if provider is None:
            user = db.get(User, user_id)
            provider = (user.active_provider if user is not None else None) or GEMINI
        return db.get(ApiKey, (user_id, provider))

    def status(self, user_id: str, provider: str | None = None) -> dict:
        """The active provider's key (or `provider`'s), never the key itself."""
        with self.sessions() as db:
            active = self._active(db, user_id)
            row = self._row(db, user_id, provider or active)
        which = provider or active
        if row is None:
            return {"provider": which, "present": False, "last4": None, "added_at": None,
                    "last_ok_at": None, "last_error": None}
        return {"provider": which, "present": True, "last4": row.last4,
                "added_at": _iso(row.added_at), "last_ok_at": _iso(row.last_ok_at),
                "last_error": row.last_error}

    def _active(self, db: Session, user_id: str) -> str:
        user = db.get(User, user_id)
        return (user.active_provider if user is not None else None) or GEMINI

    def providers(self, user_id: str) -> dict:
        """Every provider: connected or not, last4, models, the choice, which is active."""
        with self.sessions() as db:
            active = self._active(db, user_id)
            user = db.get(User, user_id)
            rows = {r.provider: r for r in db.scalars(
                sa.select(ApiKey).where(ApiKey.user_id == user_id))}
        out = []
        for provider in PROVIDERS:
            row = rows.get(provider)
            out.append({
                "provider": provider,
                "connected": row is not None,
                "active": provider == active,
                "last4": row.last4 if row else None,
                "added_at": _iso(row.added_at) if row else None,
                "last_ok_at": _iso(row.last_ok_at) if row else None,
                "last_error": row.last_error if row else None,
                "model": row.model if row else None,
                "fast_model": row.fast_model if row else None,
                "models": list(row.models or []) if row else [],
                "paid_tier": bool(row.paid_tier) if row and provider == GEMINI else False,
            })
        gemini = rows.get(GEMINI)
        paid = bool(gemini is not None and gemini.paid_tier)
        return {"active": active, "providers": out,
                "daily_token_cap": user.daily_token_cap if user is not None else None,
                "gemini_paid": paid,
                "gemini_paid_hint": (gemini is not None and not paid
                                     and self.paid_hint(user_id))}

    def has_key(self, user_id: str) -> bool:
        """True when the ACTIVE provider has a key."""
        with self.sessions() as db:
            return self._row(db, user_id) is not None

    def _check(self, provider: str, key: str) -> CheckResult:
        checker = self._checkers[provider]
        try:
            result = checker.check(key)
        except Exception as exc:  # noqa: BLE001 - a checker must not raise; if it does,
            # "could not tell", and only the exception's type is logged.
            log.warning("the %s key check failed (%s)", provider, type(exc).__name__)
            return CheckResult(CHECK_UNAVAILABLE, [])
        if isinstance(result, CheckResult):
            return result
        if isinstance(result, tuple) and len(result) == 2:
            return CheckResult(*result)
        return CheckResult(str(result), [])  # Gemini's checker: a status only

    def save(self, user_id: str, text: object, provider: str = GEMINI) -> SaveOutcome:
        """Check the key once; store it (sealed) unless the provider refused it.

        A key that could not be checked (quota, overload, network) is stored
        with `last_error = unchecked`: the next real request tells. The first
        key a user saves, or a key for a provider when the active one has no
        key, becomes the active provider. Saving a working key wakes the
        user's paused jobs.
        """
        if provider not in PROVIDERS:
            raise KeyFormatError("unknown provider")
        key = normalize_key(text, provider)  # KeyFormatError before any request
        result = self._check(provider, key)
        if result.status == CHECK_INVALID:
            return SaveOutcome(False, result.status)
        now = self._now()
        sealed = seal(self._master, user_id, key, provider)
        with self.sessions() as db:
            row = db.get(ApiKey, (user_id, provider))
            if row is None:
                row = ApiKey(user_id=user_id, provider=provider)
                db.add(row)
            row.ciphertext, row.nonce, row.key_version = sealed
            row.last4, row.added_at = key[-4:], now
            row.last_ok_at = now if result.status == CHECK_OK else None
            row.last_error = None if result.status == CHECK_OK else ERR_UNCHECKED
            if result.models or row.models is None:
                row.models = list(result.models)
            if row.model is None or (row.models and row.model not in row.models
                                     and provider == OPENAI):
                row.model = default_model(provider, row.models)
            user = db.get(User, user_id)
            if user is not None:
                current = db.get(ApiKey, (user_id, user.active_provider or GEMINI))
                if current is None or current is row:
                    user.active_provider = provider
            db.flush()
            queue.resume_user(db, user_id)  # commits
        return SaveOutcome(True, result.status, tuple(result.models))

    def remove(self, user_id: str, provider: str | None = None) -> bool:
        """Remove one provider's key (the active one when none is named). When
        it was the active provider, another connected one becomes active."""
        with self.sessions() as db:
            row = self._row(db, user_id, provider)
            if row is None:
                return False
            removed = row.provider
            db.delete(row)
            user = db.get(User, user_id)
            if user is not None and (user.active_provider or GEMINI) == removed:
                rest = db.scalars(sa.select(ApiKey.provider).where(
                    ApiKey.user_id == user_id, ApiKey.provider != removed)).all()
                if rest:
                    user.active_provider = next(p for p in PROVIDERS if p in rest)
            db.commit()
        return True

    def choose(self, user_id: str, provider: str, model: str | None = None,
               fast_model: str | None = None) -> dict:
        """Switch the active provider and/or its model(s). ProviderChoiceError
        for a provider without a key or a model it does not offer."""
        if provider not in PROVIDERS:
            raise ProviderChoiceError("unknown provider")
        with self.sessions() as db:
            row = db.get(ApiKey, (user_id, provider))
            if row is None:
                raise ProviderChoiceError("connect that provider's key first")
            if provider == ANTHROPIC:
                allowed = set(roles.ANTHROPIC_MODELS)
            elif provider == OPENAI:
                allowed = set(row.models or [])
            else:
                allowed = set()
            for value in (model, fast_model):
                if value and value not in allowed:
                    raise ProviderChoiceError("that model isn't offered for this key")
            if model:
                row.model = model
            if fast_model is not None:
                row.fast_model = fast_model or None
            user = db.get(User, user_id)
            if user is not None:
                user.active_provider = provider
            queue.resume_user(db, user_id)  # commits
        return self.providers(user_id)

    def set_gemini_paid(self, user_id: str, paid: bool) -> dict:
        """The user's "I'm on a paid Gemini plan" switch, on their Gemini key.
        ProviderChoiceError without a Gemini key. Never switches the active
        provider; LORE never turns it on by itself (see `paid_hint`)."""
        if not isinstance(paid, bool):
            raise ProviderChoiceError("say on or off")
        with self.sessions() as db:
            row = db.get(ApiKey, (user_id, GEMINI))
            if row is None:
                raise ProviderChoiceError("connect a Gemini key first")
            row.paid_tier = paid
            # Turning the free-tier cap off can let a user's waiting jobs go on.
            queue.resume_user(db, user_id)  # commits
        return self.providers(user_id)

    def paid_hint(self, user_id: str) -> bool:
        """True when the user's Gemini key seems to be on a paid plan: today
        (Pacific) some Gemini model took more requests than its free-tier
        daily count and Google never answered 429. Only a hint: the switch
        stays the user's (`set_gemini_paid`)."""
        from lore.inference.gemini import limits_for
        day = self._day()
        with self.sessions() as db:
            rows = db.execute(sa.select(Usage.model, Usage.requests, Usage.quota_refusals)
                              .where(Usage.user_id == user_id, Usage.day == day,
                                     Usage.provider == GEMINI)).all()
        if any(refusals for _m, _r, refusals in rows):
            return False
        return any(requests > limits_for(model).rpd for model, requests, _ in rows)

    def credentials_for(self, user_id: str) -> Credentials:
        """The active provider, its plaintext key and the chosen model(s), for
        one unit of work; KeyRequired if that provider has no key."""
        with self.sessions() as db:
            row = self._row(db, user_id)
            if row is None:
                raise KeyRequired()
            provider, model, fast = row.provider, row.model, row.fast_model
            paid = provider == GEMINI and bool(row.paid_tier)
            sealed = Sealed(row.ciphertext, row.nonce, row.key_version)
        key = unseal(self._master, user_id, sealed, provider)
        if provider == ANTHROPIC and model not in roles.ANTHROPIC_MODELS:
            model = roles.ANTHROPIC_DEFAULT
        return Credentials(provider, key, model, fast, paid)

    def key_for(self, user_id: str) -> str:
        """The active provider's plaintext key; KeyRequired if none."""
        return self.credentials_for(user_id).key

    def mark_rejected(self, user_id: str, provider: str | None = None) -> None:
        """The provider refused the key: say so on the key, and pause the
        user's queued jobs (they resume when a working key is saved)."""
        with self.sessions() as db:
            row = self._row(db, user_id, provider)
            if row is not None:
                row.last_error = ERR_REJECTED
            queue.pause_user(db, user_id)  # commits

    def mark_ok(self, user_id: str, provider: str | None = None) -> None:
        with self.sessions() as db:
            row = self._row(db, user_id, provider)
            if row is not None:
                row.last_ok_at, row.last_error = self._now(), None
                db.commit()

    # usage

    def _day(self) -> str:
        from lore.orchestrator.gemini_budget import today
        return today(self._now())

    def _bump(self, user_id: str, provider: str, model: str, requests: int,
              tokens_in: int, tokens_out: int, refusals: int = 0) -> None:
        day = self._day()
        with self.sessions() as db:
            for _ in range(2):
                hit = db.execute(
                    sa.update(Usage)
                    .where(Usage.user_id == user_id, Usage.day == day,
                           Usage.model == model)
                    .values(requests=Usage.requests + requests,
                            input_tokens=Usage.input_tokens + tokens_in,
                            output_tokens=Usage.output_tokens + tokens_out,
                            quota_refusals=Usage.quota_refusals + refusals)
                    .execution_options(synchronize_session=False))
                if hit.rowcount:
                    db.commit()
                    return
                db.add(Usage(user_id=user_id, day=day, model=model, provider=provider,
                             requests=requests, input_tokens=tokens_in,
                             output_tokens=tokens_out, quota_refusals=refusals))
                try:
                    db.commit()
                    return
                except IntegrityError:  # another process inserted it first
                    db.rollback()

    def record_request(self, user_id: str, model: str, provider: str = GEMINI) -> None:
        """One more request today on `model` for this user (central `usage`).

        A failed write is logged and swallowed: this count is for display;
        the Gemini budget itself is guarded by the ledger in the user's lore.db.
        """
        try:
            self._bump(user_id, provider, model, 1, 0, 0)
        except Exception:  # noqa: BLE001 - see the docstring
            log.exception("could not record a request in the usage table")

    def record_tokens(self, user_id: str, provider: str, model: str,
                      tokens_in: int, tokens_out: int) -> None:
        """One Claude or OpenAI response's token counts (central `usage`)."""
        try:
            self._bump(user_id, provider, model, 0, int(tokens_in), int(tokens_out))
        except Exception:  # noqa: BLE001 - display and the cap; never fails a request
            log.exception("could not record tokens in the usage table")

    def record_quota_refusal(self, user_id: str, model: str) -> None:
        """Gemini answered 429 on `model` for this user (central `usage`)."""
        try:
            self._bump(user_id, GEMINI, model, 0, 0, 0, refusals=1)
        except Exception:  # noqa: BLE001 - only the paid-plan hint reads it
            log.exception("could not record a quota refusal in the usage table")

    def usage_today(self, user_id: str) -> dict:
        day = self._day()
        with self.sessions() as db:
            rows = db.execute(sa.select(Usage.model, Usage.requests).where(
                Usage.user_id == user_id, Usage.day == day)).all()
        return {"day": day, "requests": sum(r for _, r in rows),
                "limit": DAILY_ALLOWANCE,
                "models": {m: r for m, r in sorted(rows)},
                "gemini_paid": self.gemini_paid(user_id)}

    def gemini_paid(self, user_id: str) -> bool:
        """The user's "I'm on a paid Gemini plan" switch (False with no Gemini key)."""
        with self.sessions() as db:
            row = db.get(ApiKey, (user_id, GEMINI))
            return bool(row is not None and row.paid_tier)

    def tokens_today(self, user_id: str) -> int:
        day = self._day()
        with self.sessions() as db:
            total = db.execute(sa.select(
                sa.func.coalesce(sa.func.sum(Usage.input_tokens + Usage.output_tokens), 0))
                .where(Usage.user_id == user_id, Usage.day == day)).scalar_one()
        return int(total)

    def usage_summary(self, user_id: str) -> dict:
        """Requests and tokens today and this month (Pacific), per model, with
        an estimated cost where a price is known; and the daily token cap."""
        day = self._day()
        month = day[:7]
        with self.sessions() as db:
            rows = db.execute(sa.select(
                Usage.day, Usage.provider, Usage.model, Usage.requests,
                Usage.input_tokens, Usage.output_tokens).where(
                Usage.user_id == user_id, Usage.day.like(f"{month}-%"))).all()
            user = db.get(User, user_id)

        def total(selected) -> dict:
            models: dict[str, dict] = {}
            costs: dict[str, float | None] = {}
            for d, provider, model, requests, tin, tout in selected:
                entry = models.setdefault(model, {"provider": provider, "requests": 0,
                                                  "input_tokens": 0, "output_tokens": 0})
                entry["requests"] += requests
                entry["input_tokens"] += tin
                entry["output_tokens"] += tout
                # Priced per day: a price can change on a given day (Gemini).
                c = _cost(provider, model, tin, tout, d)
                if c is not None:
                    costs[model] = (costs.get(model) or 0.0) + c
                else:
                    costs.setdefault(model, None)
            cost, priced = 0.0, False
            for model, entry in models.items():
                c = costs.get(model)
                entry["estimated_cost_usd"] = None if c is None else round(c, 4)
                if c is not None:
                    cost, priced = cost + c, True
            return {"requests": sum(e["requests"] for e in models.values()),
                    "input_tokens": sum(e["input_tokens"] for e in models.values()),
                    "output_tokens": sum(e["output_tokens"] for e in models.values()),
                    "estimated_cost_usd": round(cost, 4) if priced else None,
                    "models": dict(sorted(models.items()))}

        today_rows = [r for r in rows if r[0] == day]
        today = total(today_rows)
        cap = user.daily_token_cap if user is not None else None
        return {"day": day, "month": month, "today": today, "month_total": total(rows),
                "daily_token_cap": cap,
                "cap_reached": bool(cap) and
                today["input_tokens"] + today["output_tokens"] >= cap,
                "requests_limit": DAILY_ALLOWANCE}

    def set_token_cap(self, user_id: str, cap: int | None) -> None:
        """The user's own daily token cap; None (or 0) turns it off."""
        if cap is not None and (not isinstance(cap, int) or cap < 0):
            raise ValueError("a token cap is a whole number of tokens")
        with self.sessions() as db:
            user = db.get(User, user_id)
            if user is not None:
                user.daily_token_cap = cap or None
                db.commit()

    def over_cap(self, user_id: str) -> bool:
        with self.sessions() as db:
            user = db.get(User, user_id)
            cap = user.daily_token_cap if user is not None else None
        return bool(cap) and self.tokens_today(user_id) >= cap

    def usage_by_user(self) -> dict[str, int]:
        """Today's requests per user (the admin's users list)."""
        day = self._day()
        with self.sessions() as db:
            rows = db.execute(sa.select(Usage.user_id, sa.func.sum(Usage.requests))
                              .where(Usage.day == day).group_by(Usage.user_id)).all()
        return {u: int(n) for u, n in rows}

    def scope(self, user_id: str, on_request: Callable[[str], None] | None = None):
        """A KeyScope for one unit of work on the user's active provider (the
        key, model choice, usage counting and the token cap), or KeyRequired /
        KeyDecryptError. The caller binds it (key_scope.bound)."""
        from lore.inference.key_scope import KeyScope
        creds = self.credentials_for(user_id)

        def request(model: str) -> None:
            if on_request is not None:
                on_request(model)
            self.record_request(user_id, model, creds.provider)

        def usage(provider: str, model: str, tokens_in: int, tokens_out: int) -> None:
            self.record_tokens(user_id, provider, model, tokens_in, tokens_out)

        def refusal(model: str) -> None:
            self.record_quota_refusal(user_id, model)

        gemini = creds.provider == GEMINI
        paid = gemini and creds.paid_tier
        # A free-tier Gemini key records requests only, exactly as before; a
        # paid one is metered like Claude and OpenAI (tokens, cost, the cap).
        return KeyScope(user_id, creds.key, request, provider=creds.provider,
                        model=creds.model, fast_model=creds.fast_model,
                        on_usage=usage if (paid or not gemini) else None,
                        over_cap=lambda: self.over_cap(user_id),
                        paid_tier=paid, on_refusal=refusal if gemini else None)
