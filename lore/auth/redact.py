"""Keep tokens out of logs.

Magic-link tokens, OAuth codes and state, and invite codes travel in query
strings, and uvicorn's access log prints the whole request target. This filter
blanks those values on `uvicorn.access` records before any handler sees them.
"""
from __future__ import annotations

import logging
import re

_SECRET_PARAMS = re.compile(r"([?&](?:token|code|state|invite)=)[^&\s\"]*")


def redact(text: str) -> str:
    return _SECRET_PARAMS.sub(r"\1[redacted]", text)


class RedactQuerySecrets(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) else a
                                for a in record.args)
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        return True


def install_log_redaction() -> None:
    log = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactQuerySecrets) for f in log.filters):
        log.addFilter(RedactQuerySecrets())
