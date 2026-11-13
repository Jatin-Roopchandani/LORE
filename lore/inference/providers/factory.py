"""The one factory every model call site builds its model through.

`credentials(config)` says whose key and which provider a new model uses:

- a bound user scope (multi mode: a web question or a worker job) gives that
  user's ACTIVE provider, key and model choice. A user on Claude or OpenAI
  gets Claude or OpenAI and nothing else: never Gemini, never the server's
  key. With no key, KeyRequired.
- multi mode with nothing bound: KeyRequired (never the server's key).
- local mode: `[lore.llm] provider` (default "gemini"). Gemini resolves its
  key exactly as before (key_scope.gemini_api_key, the caller's own `local`
  lookup); Claude reads ANTHROPIC_API_KEY or ~/.config/lore/anthropic.env,
  OpenAI reads OPENAI_API_KEY or ~/.config/lore/openai.env (mode 600, one
  line `NAME=...`, like gemini.env).
- local mode, `provider = "openai_compatible"`: a self-hosted server at
  `[lore.llm] base_url`. Its key is optional (LORE_LLM_API_KEY or
  ~/.config/lore/llm.env); with none, `key` is NO_KEY, so every "is there a
  key?" check a call site makes passes, and the model sends no header.
  Refused in multi mode, bound user or not (MultiUserRefused).

`build(creds, config, site, gemini_models=...)` turns them into a TextModel.
Gemini is the very same `GeminiClient(key, models=..., recorder=...)` each
site always built, so its requests are byte-identical (tests/golden). The
`[lore.<step>] provider = "gemini"` settings keep meaning "this step runs on
the remote model"; which remote model is decided here.
"""
from __future__ import annotations

import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from lore.inference import key_scope
from lore.inference.gemini import GeminiClient, GeminiKeyFileError
from lore.inference.key_scope import KeyRequired
from lore.inference.providers import roles
from lore.inference.providers.base import (
    ANTHROPIC,
    GEMINI,
    LABELS,
    OPENAI,
    OPENAI_COMPATIBLE,
    TextModel,
)

KEY_VARIABLES = {ANTHROPIC: "ANTHROPIC_API_KEY", OPENAI: "OPENAI_API_KEY",
                 OPENAI_COMPATIBLE: "LORE_LLM_API_KEY"}
# The key file's name, when it is not the provider's: ~/.config/lore/llm.env.
_KEY_FILE_NAMES = {OPENAI_COMPATIBLE: "llm"}
# The key of an OpenAI-compatible server that needs none: truthy, so the call
# sites' key checks pass; the model never sends it (`build`).
NO_KEY = "no-key"


@dataclass(frozen=True)
class Credentials:
    provider: str
    key: str | None = field(repr=False)
    model: str | None = None
    fast_model: str | None = None
    base_url: str | None = None


def key_file(provider: str) -> Path:
    """`~/.config/lore/<provider>.env` (llm.env for openai_compatible), next to
    gemini.env."""
    name = _KEY_FILE_NAMES.get(provider, provider)
    return Path.home() / ".config" / "lore" / f"{name}.env"


def _key_from_file(path: Path, variable: str) -> str | None:
    if key_scope.server_key_forbidden():
        raise KeyRequired()
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    mode = stat.S_IMODE(info.st_mode)
    if mode & ~0o600:
        raise GeminiKeyFileError(
            f"refusing to read {path}: its mode is {oct(mode)[2:]}, which lets"
            f" someone other than its owner read it. Run `chmod 600 {path}`.")
    for line in path.read_text().splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == variable:
            value = value.strip().strip("'\"").strip()
            if value:
                return value
    return None


def local_key(provider: str, env: Mapping[str, str] | None = None) -> str | None:
    """A Claude, OpenAI or local-server key from the environment, then its key
    file (local mode)."""
    if key_scope.server_key_forbidden():
        raise KeyRequired()
    variable = KEY_VARIABLES[provider]
    value = ((os.environ if env is None else env).get(variable) or "").strip()
    return value or _key_from_file(key_file(provider), variable)


def active_provider(config) -> str:
    """The provider the current unit of work runs on."""
    scope = key_scope.current()
    if scope is not None:
        return scope.provider
    if key_scope.multi_user_enabled():
        return GEMINI
    return config.llm.provider


def processing_model(config) -> str | None:
    """The model a book's processing mostly runs on (the fast role), for the
    step list's timings: the bound user's choice, `[lore.llm]`, or Gemini's
    first extraction model."""
    provider = active_provider(config)
    if provider == GEMINI:
        return (config.extraction.gemini_models or [None])[0]
    scope = key_scope.current()
    if scope is not None:
        return scope.fast_model or scope.model
    return config.llm.fast_model or config.llm.model or None


def gemini_paid(config) -> bool:
    """True when the current unit of work's Gemini key is on a paid plan: the
    bound user's switch (multi mode), else `[lore.llm] gemini_paid` (local)."""
    scope = key_scope.current()
    if scope is not None:
        return scope.provider == GEMINI and bool(scope.paid_tier)
    if key_scope.multi_user_enabled():
        return False
    return config.llm.provider == GEMINI and bool(config.llm.gemini_paid)


def on_gemini(config) -> bool:
    """True when Gemini's free-tier ledger and answer reserve apply: Gemini,
    and not a paid Gemini plan (which is metered like Claude and OpenAI)."""
    return active_provider(config) == GEMINI and not gemini_paid(config)


def credentials(config, explicit: str | None = None, *,
                local: Callable[[], str | None] | None = None) -> Credentials:
    """Whose key and which provider. See the module docstring."""
    scope = key_scope.current()
    if OPENAI_COMPATIBLE in (config.llm.provider, getattr(scope, "provider", None)):
        from lore.inference.providers.openai_compatible import refuse_in_multi_mode
        refuse_in_multi_mode()
        if scope is not None and scope.provider == OPENAI_COMPATIBLE:
            # Never a user's choice (base.PROVIDERS); never from a bound user.
            from lore.inference.providers.openai_compatible import MultiUserRefused
            raise MultiUserRefused()
    if scope is not None and scope.provider != GEMINI:
        if not scope.key:
            raise KeyRequired()
        return Credentials(scope.provider, scope.key, scope.model, scope.fast_model)
    if scope is None and not key_scope.multi_user_enabled() \
            and config.llm.provider != GEMINI:
        provider = config.llm.provider
        key = local_key(provider)
        if provider == OPENAI_COMPATIBLE:
            return Credentials(provider, key or NO_KEY, config.llm.model or None,
                               config.llm.fast_model or None,
                               base_url=config.llm.base_url)
        if not key:
            raise ValueError(
                f'[lore.llm] provider = "{provider}" but no API key: set'
                f" {KEY_VARIABLES[provider]}, or put one line"
                f" `{KEY_VARIABLES[provider]}=...` in {key_file(provider)} with"
                ' mode 600. Or set provider = "gemini".')
        return Credentials(provider, key, config.llm.model or None,
                           config.llm.fast_model or None)
    return Credentials(GEMINI, key_scope.gemini_api_key(explicit, local=local))


def model_for(creds: Credentials, site: str) -> str:
    """The model id a site uses on Claude or OpenAI."""
    role = roles.SITES[site].role
    chosen = (creds.fast_model if role == roles.FAST and creds.fast_model else None) \
        or creds.model
    if creds.provider == ANTHROPIC:
        chosen = chosen or roles.ANTHROPIC_DEFAULT
        if chosen not in roles.ANTHROPIC_MODELS:
            raise ValueError(f"{chosen!r} is not one of the Claude models LORE offers"
                             f" ({', '.join(roles.ANTHROPIC_MODELS)})")
        return chosen
    if not chosen and creds.provider == OPENAI_COMPATIBLE:
        raise ValueError('[lore.llm] provider = "openai_compatible" needs model')
    if not chosen:
        raise ValueError("choose an OpenAI model first (LORE never assumes one)")
    return chosen


def effort_for(config, site: str) -> str:
    override = (config.llm.effort or {}).get(site)
    return override or roles.SITES[site].effort


def build(creds: Credentials, config, site: str, *,
          gemini_models, recorder=None) -> TextModel:
    """The TextModel for `site`. Gemini: exactly the client the site always built."""
    if site not in roles.SITES:
        raise ValueError(f"unknown model call site {site!r}")
    if creds.provider == GEMINI:
        return GeminiClient(creds.key, models=gemini_models, recorder=recorder)
    if creds.provider == ANTHROPIC:
        from lore.inference.providers.anthropic_model import AnthropicTextModel
        return AnthropicTextModel(creds.key, model=model_for(creds, site),
                                  effort=effort_for(config, site))
    if creds.provider == OPENAI:
        from lore.inference.providers.openai_model import OpenAITextModel
        return OpenAITextModel(creds.key, model=model_for(creds, site))
    if creds.provider == OPENAI_COMPATIBLE:
        from lore.inference.providers.openai_compatible import (
            OpenAICompatibleTextModel,
            refuse_in_multi_mode,
        )
        refuse_in_multi_mode()
        return OpenAICompatibleTextModel(
            None if creds.key == NO_KEY else creds.key,
            base_url=creds.base_url or config.llm.base_url,
            model=model_for(creds, site), concurrency=config.llm.concurrency)
    raise ValueError(f"unknown provider {creds.provider!r}")


def label(provider: str) -> str:
    return LABELS.get(provider, provider)
