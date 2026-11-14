"""Self-host mode (`LORE_SELFHOST=1`): the multi-user server, run by its owner.

On top of multi-user mode (it implies LORE_MULTI_USER), for one household or
team on their own machine (docker-compose.yml at the repository root):

- password sign-in, and a first-run screen that creates the admin
  (lore.auth.firstrun, lore.auth.selfhost);
- the admin's AI provider for everyone, including a local model on an
  OpenAI-compatible server, which only the admin may point at an address
  (lore.central.keys; the hosted site still refuses that provider);
- no outside services: no OAuth app, no mail, no waitlist, no public pages;
- secrets made on first start and kept in the data volume (`ensure_secrets`),
  so `docker compose up -d` needs no .env. Any value in the environment wins.
"""
from __future__ import annotations

import base64
import os
import secrets
from collections.abc import Mapping, MutableMapping
from pathlib import Path

ENV_SELFHOST = "LORE_SELFHOST"
ENV_ALLOWED_HOSTS = "LORE_ALLOWED_HOSTS"
# Where the generated secrets live: in the data root, readable by its owner only.
SECRETS_FILE = "secrets.env"
# The address LORE is opened at when LORE_BASE_URL is not set (docker-compose.yml).
DEFAULT_BASE_URL = "http://localhost:8080"

_SESSION = "LORE_SESSION_SECRET"
_CSRF = "LORE_CSRF_SECRET"
_MASTER = "LORE_KEY_ENCRYPTION_KEY"
SECRET_NAMES = (_SESSION, _CSRF, _MASTER)


def _on(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def selfhost_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return _on(env.get(ENV_SELFHOST))


def _generate() -> dict[str, str]:
    return {
        _SESSION: secrets.token_urlsafe(48),
        _CSRF: secrets.token_urlsafe(48),
        # The master key for sealed API keys: 32 random bytes, base64
        # (lore.central.keys.MasterKeys).
        _MASTER: base64.b64encode(secrets.token_bytes(32)).decode(),
    }


def _parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() in SECRET_NAMES and value.strip():
            out[name.strip()] = value.strip()
    return out


def _read_or_create(path: Path) -> dict[str, str]:
    """The file's secrets, creating the file first if there is none.

    Atomic across processes (the web app and the worker start together on
    one volume): the new file is written in full under a temporary name, then
    hard-linked to its real name, which fails if another process got there
    first; then everyone reads the one file that won.
    """
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                f.write("# Made by LORE on first start. Keep it secret, keep it with the data:\n"
                        "# without LORE_KEY_ENCRYPTION_KEY the saved API keys cannot be opened.\n")
                f.writelines(f"{k}={v}\n" for k, v in _generate().items())
            try:
                os.link(tmp, path)
            except FileExistsError:
                pass  # another process made it first: use theirs
        finally:
            tmp.unlink(missing_ok=True)
    found = _parse(path.read_text())
    missing = [n for n in SECRET_NAMES if n not in found]
    if missing:
        raise RuntimeError(f"{path} is missing {', '.join(missing)}; remove the file to"
                           " make new secrets (saved API keys then need saving again)")
    return found


def ensure_secrets(data_root: Path, environ: Mapping[str, str] | None = None
                   ) -> dict[str, str]:
    """The session, CSRF and key-encryption secrets: each from the environment
    when set there, else from `<data_root>/secrets.env`, made on first use."""
    env = os.environ if environ is None else environ
    given = {n: env.get(n, "").strip() for n in SECRET_NAMES}
    if all(given.values()):
        return given
    stored = _read_or_create(Path(data_root) / SECRETS_FILE)
    return {n: given[n] or stored[n] for n in SECRET_NAMES}


def apply_secrets(data_root: Path,
                  environ: MutableMapping[str, str] | None = None) -> None:
    """Put the secrets in the environment where they are missing (self-host
    mode only), for AuthSettings and MasterKeys to read as usual."""
    env = os.environ if environ is None else environ
    if not selfhost_enabled(env):
        return
    for name, value in ensure_secrets(data_root, env).items():
        if not env.get(name, "").strip():
            env[name] = value


def allowed_hosts_from_env(environ: Mapping[str, str] | None = None) -> set[str]:
    """LORE_ALLOWED_HOSTS: extra host names (or name:port) the server answers,
    comma-separated; `*` answers any."""
    env = os.environ if environ is None else environ
    raw = env.get(ENV_ALLOWED_HOSTS, "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}
