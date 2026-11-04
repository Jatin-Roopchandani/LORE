"""Keep secrets out of logs.

Magic-link tokens, OAuth codes and state, and invite codes travel in query
strings, and uvicorn's access log prints the whole request target. This filter
blanks those values on `uvicorn.access` records before any handler sees them.

Gemini keys (release step 3) never belong in a log line either. Nothing in
LORE logs one, but as a second line of defence, multi mode installs a log
record factory that blanks anything shaped like a Google API key, and the key
bound for the current question or job whatever its shape, in every record's
message, arguments and exception text, from any logger.
"""
from __future__ import annotations

import logging
import re

_SECRET_PARAMS = re.compile(r"([?&](?:token|code|state|invite)=)[^&\s\"]*")
# Google API keys: "AIza" and 35 URL-safe characters.
_GOOGLE_KEY = re.compile(r"AIza[0-9A-Za-z_\-]{35}")
KEY_MASK = "[redacted key]"


def redact(text: str) -> str:
    return _SECRET_PARAMS.sub(r"\1[redacted]", text)


def redact_keys(text: str) -> str:
    """`text` with every Google-key-shaped string, and the bound user key, blanked."""
    from lore.inference import key_scope
    scope = key_scope.current()
    if scope is not None and scope.key:
        text = text.replace(scope.key, KEY_MASK)
    return _GOOGLE_KEY.sub(KEY_MASK, text)


class RedactQuerySecrets(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) else a
                                for a in record.args)
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        return True


def _scrub(record: logging.LogRecord) -> logging.LogRecord:
    """Format the record's message (and exception) now, redacted, so no
    handler formats the raw values later."""
    try:
        message = record.getMessage()
    except Exception:  # noqa: BLE001 - a broken format string: keep the template
        message = str(record.msg)
    cleaned = redact_keys(message)
    if cleaned != message:  # only then: some formatters read record.args
        record.msg, record.args = cleaned, None
    if record.exc_info and not record.exc_text:
        record.exc_text = redact_keys(logging.Formatter().formatException(record.exc_info))
    elif record.exc_text:
        record.exc_text = redact_keys(record.exc_text)
    if record.stack_info:
        record.stack_info = redact_keys(record.stack_info)
    return record


class _KeyRedactingFactory:
    def __init__(self, inner) -> None:
        self.inner = inner

    def __call__(self, *args, **kwargs) -> logging.LogRecord:
        return _scrub(self.inner(*args, **kwargs))


def install_log_redaction() -> None:
    log = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactQuerySecrets) for f in log.filters):
        log.addFilter(RedactQuerySecrets())
    install_key_redaction()


def install_key_redaction() -> None:
    """Every log record, from any logger, is scrubbed of API keys (idempotent)."""
    factory = logging.getLogRecordFactory()
    if not isinstance(factory, _KeyRedactingFactory):
        logging.setLogRecordFactory(_KeyRedactingFactory(factory))
