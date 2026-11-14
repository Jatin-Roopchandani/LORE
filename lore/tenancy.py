"""Tenancy: who is asking, and where that user's library lives.

One data directory per user, `<root>/users/<user_id>`, holding that user's
SQLite, Kuzu graph, LanceDB vectors, inbox and caches (release spec §2). The
single choke point is `config_for`: every request resolves its user, then
builds a `LoreConfig` whose `data_dir` is that user's directory, and every
store is opened from that config. No code path builds a data path from
request input; the only thing a request contributes is a user id, and that id
is validated here before it becomes a directory name.

Two modes:
- Local (the default): one fixed local user whose library is the configured
  `data_dir` itself, exactly as before tenancy existed.
- Multi (`LORE_MULTI_USER=1`): the user comes from the request through
  `resolve_user`, the one hook step 2 (auth) fills in.
"""
from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, Request

from lore.config import LoreConfig

ENV_MULTI_USER = "LORE_MULTI_USER"

# The local user: its library is the base config's data_dir, not a directory
# under users/. The id is reserved, so no multi-mode user can collide with it.
LOCAL_USER_ID = "local"

# A user id becomes a directory name, so only these shapes are accepted: a
# canonical lowercase UUID, or a slug of lowercase letters, digits, '-' and
# '_' that starts and ends with a letter or digit. Dots, slashes, whitespace,
# NULs and everything else (so "..", "a/b", "~") are refused, never cleaned.
_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?")


class InvalidUserId(ValueError):
    """The id is not a UUID or a safe slug (or is the reserved local id)."""


@dataclass(frozen=True)
class CurrentUser:
    id: str
    is_local: bool = False


LOCAL_USER = CurrentUser(LOCAL_USER_ID, is_local=True)


def validate_user_id(user_id: object) -> str:
    """The id unchanged if it is safe as a directory name; else InvalidUserId."""
    if not isinstance(user_id, str):
        raise InvalidUserId("a user id is a string")
    if user_id == LOCAL_USER_ID:
        raise InvalidUserId(f"{LOCAL_USER_ID!r} is reserved for local mode")
    try:
        if str(uuid.UUID(user_id)) == user_id:
            return user_id
    except ValueError:
        pass
    if _SLUG.fullmatch(user_id) is None:
        raise InvalidUserId(f"not a UUID or a safe slug: {user_id!r}")
    return user_id


def user_data_dir(root: Path, user_id: str, *, seed: bool = False) -> Path:
    """`<root>/users/<user_id>`, created on first use.

    `seed`: when the directory does not exist yet, it starts as a copy of the
    sample library (`<root>/sample`, lore.sample) if there is one. Only the
    web app's requests ask for it (a new account's first request); the
    worker and the hosted MCP never do. A directory that exists, empty or
    not, is never seeded, and the copy is renamed into place whole."""
    validate_user_id(user_id)
    users = Path(root) / "users"
    path = users / user_id
    # Belt and braces: the validated id cannot climb out, but check the
    # resolved path anyway, so a symlinked entry cannot either.
    if path.resolve().parent != users.resolve():
        raise InvalidUserId(f"user directory escapes the users root: {user_id!r}")
    if seed and not path.exists():
        from lore import sample
        sample.seed_new_library(Path(root), path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_for(base: LoreConfig, user_id: str, *, seed: bool = False) -> LoreConfig:
    """A copy of `base` whose library is `user_id`'s; everything else inherited.

    `base.data_dir` is the data root. The local user's library IS that root
    (single-user setups keep working unchanged, and are never seeded);
    anyone else's is `<root>/users/<id>`.
    """
    if user_id == LOCAL_USER_ID:
        return base
    return base.model_copy(update={
        "data_dir": user_data_dir(base.data_dir, user_id, seed=seed)})


def multi_user_enabled() -> bool:
    """LORE_MULTI_USER, or self-host mode (LORE_SELFHOST), which is built on it."""
    return any(os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")
               for name in (ENV_MULTI_USER, "LORE_SELFHOST"))


def resolve_user(request: Request) -> CurrentUser:
    """THE hook: who is making this request.

    Local mode returns the fixed local user, whatever the request says.
    Multi mode must name a user from the request: the `lore_session` cookie,
    resolved through the central database (`_user_from_session`). Anything
    else is 401. Tests may install their own resolver through
    `create_app(resolve_user=...)`; no test header exists in this module, so
    none can be switched on in production.
    """
    if not multi_user_enabled():
        return LOCAL_USER
    return require_session_user(request)


def require_session_user(request: Request) -> CurrentUser:
    """The signed-in user, or 401 (the multi-mode resolver)."""
    user = _user_from_session(request)
    if user is None:
        raise HTTPException(401, "sign in required")
    return user


def _user_from_session(request: Request) -> CurrentUser | None:
    """The signed-in user (cookie -> session row -> active central user), or
    None when the request is not signed in. See lore.auth.current."""
    from lore.auth.current import session_user  # auth is multi-mode only
    return session_user(request)
