"""Whose Gemini key a model call uses: the one place every client gets its key.

Local mode (the default) is unchanged: the key comes from the environment or
`~/.config/lore/gemini.env`, exactly as `resolve_api_key` always read it.

Multi mode (release step 3): each user brings their own key, stored encrypted
in the central database (lore.central.keys). A unit of work for one user (a
question in the web app, a job in the worker) decrypts that user's key and
BINDS it here for its duration:

    with key_scope.bound(KeyScope(user_id, key, on_request=...)):
        ...  # every GeminiClient built in here gets this user's key

Every construction site (query models, extraction, adjudication and the
first-person narrator, disambiguation, beats, codex) asks `gemini_api_key()`
for its key instead of reading the key file itself. In multi mode, or inside
any bound scope, the server's key file and environment are never consulted:
with no user key bound, `gemini_api_key()` raises KeyRequired.

The binding is a ContextVar, so it follows the work into `asyncio.run` and
into tasks created inside the scope, and never leaks to another request or
job. The plaintext key lives only in the scope object and in the client's
request header, never in a repr, a log line or an exception message.
"""
from __future__ import annotations

import contextlib
import os
from collections.abc import Callable, Iterator
from contextvars import ContextVar

from lore.inference.gemini import (
    GeminiError,
    default_key_file,
    resolve_api_key,
)

ENV_MULTI_USER = "LORE_MULTI_USER"  # same flag as lore.tenancy (not imported: no fastapi here)


class KeyRequired(GeminiError):
    """No Gemini key for this user. Never carries a key."""

    def __init__(self, message: str = "add your Gemini API key in Account settings"
                 " before asking or processing") -> None:
        super().__init__(message)


class KeyScope:
    """One user's key for one unit of work, plus what happened with it.

    `on_request(model)` is called once per HTTP attempt (the per-user usage
    count). `rejected` turns True when the provider refuses the key (Gemini:
    401/403, or a 400 that says API_KEY_INVALID; Claude and OpenAI: 401/403),
    whatever the calling code then does with the exception, so the web app
    and the worker can tell a bad key from any other failure.

    `provider` is the user's active provider (gemini, anthropic or openai);
    `model` and `fast_model` their chosen model(s), unused for Gemini, whose
    cascades come from the configuration. `on_usage(provider, model, input,
    output)` receives each Claude or OpenAI response's token counts, and
    `over_cap()` says whether the user's own daily token cap is reached.

    `paid_tier` (Gemini only): the user says their Gemini key is on a paid
    plan, so Gemini is metered like Claude and OpenAI (tokens through
    `on_usage`, the token cap) instead of LORE's free-tier ledger.
    `on_refusal(model)` is told of each 429 Gemini answers, `on_credit(model)`
    of each 402 (the key's prepaid credit is used up). `changed()` says the
    user has since switched provider or removed this key (a long job stops
    at its next safe point and resumes on the new choice).
    """

    __slots__ = ("user_id", "_key", "_on_request", "rejected", "provider",
                 "model", "fast_model", "_on_usage", "_over_cap", "paid_tier",
                 "_on_refusal", "_on_credit", "_changed", "base_url")

    def __init__(self, user_id: str, key: str | None,
                 on_request: Callable[[str], None] | None = None, *,
                 provider: str = "gemini", model: str | None = None,
                 fast_model: str | None = None,
                 on_usage: Callable[[str, str, int, int], None] | None = None,
                 over_cap: Callable[[], bool] | None = None,
                 paid_tier: bool = False,
                 on_refusal: Callable[[str], None] | None = None,
                 on_credit: Callable[[str], None] | None = None,
                 changed: Callable[[], bool] | None = None,
                 base_url: str | None = None) -> None:
        self.user_id = user_id
        # Self-host mode only: the admin's local model's address (the
        # server's AI provider for everyone; lore.central.keys). Never set
        # from anything a user sends.
        self.base_url = base_url
        self._key = key
        self._on_request = on_request
        self.rejected = False
        self.provider = provider
        self.model = model
        self.fast_model = fast_model
        self._on_usage = on_usage
        self._over_cap = over_cap
        self.paid_tier = paid_tier
        self._on_refusal = on_refusal
        self._on_credit = on_credit
        self._changed = changed

    @property
    def key(self) -> str | None:
        return self._key

    def __repr__(self) -> str:  # never the key
        state = "with a key" if self._key else "no key"
        return f"KeyScope(user_id={self.user_id!r}, {self.provider}, {state})"

    __str__ = __repr__

    def __reduce__(self):  # not picklable: a pickle would carry the key
        raise TypeError("a KeyScope is not picklable")


_BOUND: ContextVar[KeyScope | None] = ContextVar("lore_gemini_key", default=None)


@contextlib.contextmanager
def bound(scope: KeyScope) -> Iterator[KeyScope]:
    """Bind `scope` for the work inside the block (this context only)."""
    token = _BOUND.set(scope)
    try:
        yield scope
    finally:
        _BOUND.reset(token)


def current() -> KeyScope | None:
    return _BOUND.get()


def multi_user_enabled() -> bool:
    """LORE_MULTI_USER, or self-host mode (LORE_SELFHOST), which is built on it."""
    return any(os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")
               for name in (ENV_MULTI_USER, "LORE_SELFHOST"))


def server_key_forbidden() -> bool:
    """True when the server's own key (file or environment) must not be used."""
    return _BOUND.get() is not None or multi_user_enabled()


def gemini_api_key(explicit: str | None = None, *,
                   local: Callable[[], str | None] | None = None) -> str | None:
    """The key a new GeminiClient must use.

    `explicit` (a CLI's own resolved key) wins. Then a bound user's key. In
    multi mode, or in a bound scope with no key, KeyRequired: never the
    server's file. Otherwise (local mode) `local()`, by default today's
    `resolve_api_key` with the key file; it may return None (each caller words
    that refusal). Callers pass their own `local` so the lookup stays in their
    module, where it always was.
    """
    if explicit:
        return explicit
    scope = _BOUND.get()
    if scope is not None:
        if scope.key:
            return scope.key
        raise KeyRequired()
    if multi_user_enabled():
        raise KeyRequired()
    if local is not None:
        return local()
    return resolve_api_key(key_file=default_key_file())


def note_request(model: str) -> None:
    """Called by GeminiClient once per attempt; counts it for the bound user."""
    scope = _BOUND.get()
    if scope is not None and scope._on_request is not None:
        scope._on_request(model)


def note_usage(provider: str, model: str, input_tokens: int, output_tokens: int) -> None:
    """Called with each response's token counts (Claude, OpenAI; Gemini too,
    recorded only for a user on a paid Gemini plan: see KeyService.scope)."""
    scope = _BOUND.get()
    if scope is not None and scope._on_usage is not None:
        scope._on_usage(provider, model, input_tokens, output_tokens)


def note_quota_refusal(model: str) -> None:
    """Called by GeminiClient when Gemini answers 429 (out of allowance)."""
    scope = _BOUND.get()
    if scope is not None and scope._on_refusal is not None:
        scope._on_refusal(model)


def note_credit_exhausted(model: str) -> None:
    """Called by GeminiClient when Gemini answers 402 (prepaid credit used up)."""
    scope = _BOUND.get()
    if scope is not None and scope._on_credit is not None:
        scope._on_credit(model)


def provider_changed() -> bool:
    """True when the bound user switched provider (or removed the key in use)
    after this unit of work bound it."""
    scope = _BOUND.get()
    if scope is None or scope._changed is None:
        return False
    return bool(scope._changed())


def over_token_cap() -> bool:
    """True when the bound user's own daily token cap is reached."""
    scope = _BOUND.get()
    if scope is None or scope._over_cap is None:
        return False
    return bool(scope._over_cap())


def note_rejection() -> None:
    """Called by GeminiClient when Gemini refuses the key."""
    scope = _BOUND.get()
    if scope is not None:
        scope.rejected = True
