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
    # Defence in depth. Kuzu's built-in file-reading table functions --
    # verified callable via a mid-query CALL past the allowlist check below
    # before this list was added -- are the CALL-based equivalent of
    # LOAD FROM: an arbitrary file read. The CALL scan below already blocks
    # them by name, but the module's whole premise is that the token guard
    # does not depend on one syntactic route reaching a file reader, so
    # they are banned outright too.
    #
    # The over-rejection cost of these five entries specifically is small:
    # _WORD tokenizes on underscore boundaries the same as letter boundaries,
    # so "Json Scan" (a space, two separate words: JSON and SCAN) does NOT
    # match the single banned token JSON_SCAN and is allowed through -- only
    # the literal underscored spelling ('json_scan') is caught, which is
    # unlikely to appear in ordinary prose about a book. The real usability
    # cost of this fix is the CALL whole-word check below, which does
    # reject a book titled "The Call of the Wild" -- see that rejection's
    # message for why that trade-off is accepted.
    "READ_CSV_SERIAL", "READ_CSV_PARALLEL", "READ_PARQUET", "READ_NPY",
    "JSON_SCAN",
)
_WORD = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
# Matches every CALL in the query, not only a leading one. Kuzu fully
# supports mid-query CALL (`MATCH (e) CALL read_csv_serial(...) RETURN *`
# executes cleanly against a read-only 0.11.3 store), so checking the
# allowlist only when the *whole query* opens with CALL let any query that
# opened with MATCH/WITH/UNWIND/RETURN/OPTIONAL MATCH reach an arbitrary,
# unlisted procedure -- including the file readers above -- without ever
# consulting _ALLOWED_CALLS. The group is optional so a CALL with no
# extractable name (e.g. `CALL (weird)`, `CALL /*x*/ f()`) still matches
# and falls through to the "name is None" branch below, rather than the
# regex silently finding nothing and the CALL going unchecked.
_CALL_TOKEN = re.compile(r"\bCALL\b\s*([A-Za-z_][A-Za-z_0-9]*)?", re.IGNORECASE)


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

    # Every CALL in the query is checked, not just a leading one -- see
    # _CALL_TOKEN's comment for why a position-only check is not enough.
    # 'CALL' is matched as a whole word EVERYWHERE, the same as the banned
    # words above: inside a string literal ('Call of the Wild'), a
    # property alias (`AS call`), or a comment (`// CALL ...`). That means
    # a perfectly ordinary query about a book with "Call" in the title is
    # rejected too -- this graph holds books, so that will happen -- and
    # the message below says so explicitly, for the same reason the
    # banned-word message does: so a user can tell a guard from a bug.
    call_matches = list(_CALL_TOKEN.finditer(text))
    for call_match in call_matches:
        name = call_match.group(1)
        if name is None or name not in _ALLOWED_CALLS:
            raise CypherRejected(
                f"CALL {name or '?'} is not allowed; the permitted procedures"
                f" are {', '.join(sorted(_ALLOWED_CALLS))}. 'CALL' is matched"
                " as a whole word even inside a string literal, property"
                " name, alias, or comment, so a legitimate query merely"
                " mentioning 'call' (e.g. a book titled 'The Call of the"
                " Wild') is rejected too -- use the `entity` or `subgraph`"
                " tool for that instead."
            )

    # Fail-closed belt, for if `_CALL_TOKEN`'s notion of a CALL "word" ever
    # diverges from `_WORD`'s. Fix round 3: this used to test the
    # SUBSTRING 'CALL' in the uppercased text, which also fires on
    # RECALLED, CALLED, RECALL, and SCALLOP -- rejecting ordinary literary
    # names ("Recalled to Life" is Book 1 of A Tale of Two Cities) with the
    # same dishonest-looking message round 2 exists to fix. Test the WORD
    # 'CALL' instead, via the same `words` set the banned-token scan above
    # already builds from `_WORD` -- so this only fires when 'CALL' is its
    # own token, not a substring of a longer one.
    #
    # This does NOT cover a CALL fused directly to its procedure name with
    # no separator at all, e.g. `CALLread_csv_serial(...)` -- `_WORD`
    # reads that as one token, CALLREAD_CSV_SERIAL, not as the word CALL,
    # so neither `\bCALL\b` above nor this word check fires on it. That
    # gap is safe only because Kuzu's own parser refuses every
    # no-separator form tried against 0.11.3; this module does not
    # independently guarantee it. A future Kuzu that tokenizes more
    # leniently would need this revisited -- coverage of that one shape
    # depends on the engine, not on this module.
    if "CALL" in words and not call_matches:
        raise CypherRejected(
            "'CALL' appears in the query as a bare keyword that this guard"
            " could not resolve into a procedure name; rejected to fail"
            " closed rather than let an unrecognized shape through"
            " unchecked"
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
    # This runs inside a long-lived HTTP server process, so an open
    # QueryResult on the truncation path (or on an exception mid-read)
    # would leak a handle per request rather than per query. Close on
    # every path.
    try:
        columns = list(result.get_column_names())
        rows: list[list] = []
        while result.has_next():
            if len(rows) >= row_cap:
                return columns, rows, True
            rows.append(list(result.get_next()))
        return columns, rows, False
    finally:
        result.close()
