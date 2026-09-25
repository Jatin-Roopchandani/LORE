"""What a client may send to `cypher_query`, and what it gets back.

Kuzu's read-only mode protects the DATABASE, not the FILESYSTEM. Measured
on a connection opened read_only=True, against this project's real graph:

    CREATE / SET / DETACH DELETE   -> refused by the engine
    COPY (MATCH ...) TO '<path>'   -> ALLOWED, wrote the file
    LOAD FROM '<path>' (...)       -> ALLOWED, returned the file as rows

So the read-only connection is the backstop and this module is the actual
control. Without it, `cypher_query` reachable over HTTP is an arbitrary
file read and write primitive. Do not delete the banned-token check on the
theory that "read-only already blocks writes" -- it does not block COPY TO
or LOAD FROM, and that is exactly the hole this module closes.

The rules fail closed. A rejected legitimate query is a clear error the
user can read; an accepted malicious one is not recoverable.
"""
from __future__ import annotations

import re

ROW_CAP = 200
TIMEOUT_MS = 5000
# TIMEOUT_MS is applied where the connection is made (Task 5's stores.py),
# not here -- run_guarded does not set it. It is insurance for a larger
# library, not a demonstrated control: it could not be made to fire on
# this corpus (560 entities is too small to build a slow query, and
# Kuzu's optimizer folds count(*) over a triple product into arithmetic,
# answered in 30ms). The row cap below is the real resource control,
# because Kuzu streams rows lazily -- see run_guarded's docstring.

_ALLOWED_OPENINGS = ("OPTIONAL MATCH", "MATCH", "RETURN", "WITH", "UNWIND", "CALL")
_ALLOWED_CALLS = frozenset({"show_tables", "table_info", "db_version"})
_BANNED = (
    "COPY", "LOAD", "EXPORT", "IMPORT", "ATTACH", "INSTALL",
    "CREATE", "MERGE", "SET", "DELETE", "DROP", "ALTER",
)
_WORD = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
_CALL_NAME = re.compile(r"^CALL\s+([A-Za-z_][A-Za-z_0-9]*)", re.IGNORECASE)


class CypherRejected(ValueError):
    """The query did not pass the read-only guard."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def validate_cypher(query: str) -> None:
    """Raise CypherRejected unless `query` is a single read-only statement."""
    text = query.strip().rstrip(";").strip()
    if not text:
        raise CypherRejected("empty query")

    if ";" in text:
        raise CypherRejected(
            "only one statement per query: a ';' joining two statements is"
            " rejected, because vetting the first would say nothing about the"
            " second"
        )

    # Whole words, INCLUDING inside string literals. A query about a
    # character named 'Set' is rejected on purpose: the alternative is
    # trusting a hand-written string-literal parser to agree with Kuzu's,
    # and a disagreement there is exactly the gap an injection uses.
    words = {w.upper() for w in _WORD.findall(text)}
    for banned in _BANNED:
        if banned in words:
            raise CypherRejected(
                f"'{banned}' is not allowed in a cypher_query. This is matched"
                " as a whole word even inside a string literal, so a legitimate"
                f" query mentioning '{banned.title()}' is rejected too -- use"
                " the `entity` or `subgraph` tool for that instead."
            )

    upper = text.upper()
    opening = next((o for o in _ALLOWED_OPENINGS if upper.startswith(o)), None)
    if opening is None:
        first = text.split(None, 1)[0]
        raise CypherRejected(
            f"a query must begin with one of"
            f" {', '.join(_ALLOWED_OPENINGS)} -- got '{first}'"
        )

    if opening == "CALL":
        match = _CALL_NAME.match(text)
        name = match.group(1) if match else ""
        if name not in _ALLOWED_CALLS:
            raise CypherRejected(
                f"CALL {name or '?'} is not allowed; the permitted procedures"
                f" are {', '.join(sorted(_ALLOWED_CALLS))}"
            )


def run_guarded(kuzu, query: str, row_cap: int = ROW_CAP):
    """Validate, run, and read at most `row_cap` rows.

    Returns (columns, rows, truncated).

    The row cap is the real resource control, not the timeout: Kuzu
    streams, so `MATCH (a:Entity),(b:Entity),(c:Entity) RETURN ...` --
    175,616,000 rows on the live graph -- returns its first rows in 60ms,
    and reading 200 then stopping costs nothing.
    """
    validate_cypher(query)
    result = kuzu.execute(query)
    columns = list(result.get_column_names())
    rows: list[list] = []
    while result.has_next():
        if len(rows) >= row_cap:
            return columns, rows, True
        rows.append(list(result.get_next()))
    return columns, rows, False
