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
# THE definition of a word in this module. The banned-token scan and the
# CALL scan below both use this one tokenizer, so they cannot disagree about
# where a word starts or ends -- the disagreement that produced three
# successive over-rejections (round 2: a substring test rejected 'Recalled
# to Life'; round 3: `_WORD` vs `\b` rejected '1CALL'; round 4 found that
# the assigned replacement for round 3 would have reopened a real bypass,
# see `_call_keyword_names`).
#
# How `_WORD` compares with Kuzu's own lexer -- each line measured on
# read-only Kuzu 0.11.3, not inferred:
#
#   * A DIGIT is a word edge for both. A Kuzu identifier may not start with
#     a digit, so a digit run ends the token before it, exactly as
#     `_WORD`'s first-character class ([A-Za-z_], no digits) does:
#
#       MATCH (e:Entity) WHERE e.n = 1CALL current_setting('threads') RETURN *
#
#     EXECUTES -- the '1' acts as whitespace and `current_setting` runs. This
#     agreement is load-bearing: a tokenizer that did not split here would
#     miss a real CALL.
#   * An ASCII letter, digit or underscore AFTER a letter continues the
#     token for both: `CALLread_csv_serial(...)` is one identifier to
#     `_WORD` and does NOT parse in Kuzu.
#   * A NON-ASCII letter is where they differ, and `_WORD` is the stricter.
#     Kuzu accepts non-ASCII letters inside unquoted identifiers
#     (`RETURN e.name AS café`, `AS CALLé`, `AS éCALL` all
#     execute), so to Kuzu `CALLé` is one identifier, not the keyword.
#     `_WORD` is ASCII-only, so it splits there and reports the word CALL.
#     The guard therefore rejects some inputs Kuzu would never read as a
#     call. That is a deliberate fail-closed over-rejection -- the safe
#     direction -- not agreement with Kuzu.
_WORD = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
# The procedure name after a CALL keyword: optional whitespace, then an
# identifier, both optional so that a CALL whose name cannot be extracted
# (`CALL `x``, `CALL/*c*/f()`, and -- an over-rejection, see `_WORD` --
# `CALL` + a non-ASCII letter) yields None and
# fails closed rather than going unchecked. Deliberately NOT IGNORECASE:
# [A-Za-z_] already covers both cases, and under IGNORECASE Python folds
# U+0130 'I-with-dot', U+0131 dotless 'i' and U+017F long 's' onto ASCII
# letters, which would widen this class for no benefit.
_NAME_AFTER_CALL = re.compile(r"\s*([A-Za-z_][A-Za-z_0-9]*)?")


def _call_keyword_names(text: str) -> list[str | None]:
    r"""The procedure name following every CALL keyword in `text`, in order.

    A CALL is found wherever `_WORD` tokenizes the standalone word CALL --
    every occurrence, not only a leading one. Kuzu fully supports mid-query
    CALL (`MATCH (e) CALL read_csv_serial(...) RETURN *` executes cleanly
    against a read-only 0.11.3 store), so checking the allowlist only when
    the *whole query* opens with CALL let any query that opened with
    MATCH/WITH/UNWIND/RETURN/OPTIONAL MATCH reach an arbitrary, unlisted
    procedure -- including the file readers banned above -- without ever
    consulting `_ALLOWED_CALLS`.

    Fix round 4 replaced a `\bCALL\b` scan plus a `_WORD`-based
    "structural belt" with this single tokenizer, because those two
    matchers disagreed and the disagreement was exploitable, not merely
    cosmetic. `\b` treats a digit as a word character, so `\bCALL\b`
    never matched `1CALL` and the belt was the only thing catching it --
    and the belt fired only when the `\b` scan found NOTHING at all, so

        MATCH (e:Entity) CALL table_info('Entity')
        WHERE e.n = 1CALL current_setting('threads') RETURN *

    passed the guard entirely (the allowlisted `table_info` suppressed the
    belt) and EXECUTES on read-only 0.11.3, reaching the unlisted
    `current_setting`. Verified against Kuzu, not reasoned about. One
    tokenizer has no second opinion to be played off against, and every
    CALL it finds is checked on its own.

    None means "a CALL whose procedure name this guard could not read",
    which the caller must reject.
    """
    names: list[str | None] = []
    for token in _WORD.finditer(text):
        if token.group(0).upper() != "CALL":
            continue
        names.append(_NAME_AFTER_CALL.match(text, token.end()).group(1))
    return names


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

    # Every CALL in the query is checked, not just a leading one, and each
    # on its own -- see `_call_keyword_names` for the verified bypass that a
    # position-only check, and a two-matcher check, each let through.
    # 'CALL' is found as a whole word EVERYWHERE, the same as the banned
    # words above: inside a string literal ('Call of the Wild'), a property
    # alias (`AS call`), or a comment (`// CALL ...`). That means a
    # perfectly ordinary query about a book with "Call" in the title is
    # rejected too -- this graph holds books, so that will happen -- and the
    # message below says so explicitly, for the same reason the banned-word
    # message does: so a user can tell a guard from a bug.
    #
    # A word edge here is `_WORD`'s (see its comment for how that compares
    # with Kuzu). A digit next to CALL ends the token for Kuzu too, so
    # '1CALL' IS the keyword and must be checked -- not over-caution:
    # `WHERE e.n = 1CALL current_setting('threads')` executes on a
    # read-only connection. A non-ASCII letter next to CALL also ends the
    # token for `_WORD`, but NOT for Kuzu, which reads 'CALL' + an accented
    # letter as one identifier; rejecting that is a deliberate fail-closed
    # over-rejection, not a keyword Kuzu would run. Ordinary words that
    # merely CONTAIN the letters ('Recalled to Life', 'called', 'Recall')
    # are a single different token and are not touched.
    #
    # What is NOT covered: a CALL fused to an ASCII procedure name with no
    # separator at all, `CALLread_csv_serial(...)`, which is one `_WORD`
    # token (CALLREAD_CSV_SERIAL) and so not the keyword. That gap is safe
    # only because Kuzu's own parser refuses every no-separator form tried
    # against 0.11.3; this module does not independently guarantee it. A
    # future Kuzu that tokenizes more leniently would need this revisited --
    # coverage of that one shape depends on the engine, not on this module.
    for name in _call_keyword_names(text):
        if name is None or name not in _ALLOWED_CALLS:
            raise CypherRejected(
                f"CALL {name or '?'} is not allowed; the permitted procedures"
                f" are {', '.join(sorted(_ALLOWED_CALLS))}. 'CALL' is matched"
                " as a whole word even inside a string literal, property"
                " name, alias, or comment, so a legitimate query merely"
                " mentioning 'call' (e.g. a book titled 'The Call of the"
                " Wild') is rejected too. A digit touching the word counts"
                " as a word edge, because Kuzu reads '1CALL' as the number 1"
                " followed by CALL, so '1CALL' is rejected as well. This"
                " guard also rejects 'call' joined to an accented letter, to"
                " stay on the safe side, even though Kuzu would not read it"
                " as a call -- use the `entity` or `subgraph` tool for that"
                " instead."
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
