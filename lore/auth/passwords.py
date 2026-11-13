"""Passwords for self-host mode: argon2id hashes (argon2-cffi), never the password.

`verify_password` takes the same time whether or not the account exists (a
missing hash is checked against a dummy one), so a sign-in cannot be used to
learn which addresses have accounts by timing.
"""
from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_CHARS = 8
MAX_CHARS = 256  # argon2 takes any length; a cap keeps a request cheap

# argon2-cffi's defaults (RFC 9106 "low memory": 64 MiB, 3 passes).
_hasher = PasswordHasher()
_DUMMY = _hasher.hash("lore-no-such-account")


class PasswordPolicyError(ValueError):
    """The new password is too short or too long. Never carries the password."""


def check_policy(password: object) -> str:
    """The password if it may be set, else PasswordPolicyError."""
    if not isinstance(password, str) or len(password) < MIN_CHARS:
        raise PasswordPolicyError(f"Use at least {MIN_CHARS} characters.")
    if len(password) > MAX_CHARS:
        raise PasswordPolicyError(f"Use at most {MAX_CHARS} characters.")
    if not password.strip():
        raise PasswordPolicyError("A password can't be only spaces.")
    return password


def hash_password(password: str) -> str:
    return _hasher.hash(check_policy(password))


def verify_password(stored: str | None, password: object) -> bool:
    """True when `password` matches `stored`. A missing hash (no account, or
    an account with no password) is False, after the same work."""
    if not isinstance(password, str) or not password or len(password) > MAX_CHARS:
        password = "\x00"
    try:
        return _hasher.verify(stored or _DUMMY, password) and stored is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
