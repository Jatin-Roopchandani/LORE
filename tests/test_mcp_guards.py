from pathlib import Path

import pytest

from lore.db.kuzu_store import KuzuStore
from lore.mcp.guards import ROW_CAP, CypherRejected, run_guarded, validate_cypher


@pytest.fixture
def live_kuzu(tmp_path: Path):
    """A small, real (not mocked) Kuzu store with a handful of Entity nodes.

    Built the same way tests/test_query_templates.py's `graph` fixture and
    tests/test_loader.py's `_setup` build theirs: a plain KuzuStore over
    tmp_path, populated with parameterized CREATE statements -- there is no
    Kuzu-specific helper in tests/fixtures.py (it only holds make_epub and
    make_pdf), so this follows the existing direct-KuzuStore convention
    rather than inventing a parallel fixture mechanism.
    """
    store = KuzuStore(tmp_path / "kuzu")
    for i in range(5):
        store.execute(
            "CREATE (:Entity {id: $id, name: $name, kind: 'Character'})",
            {"id": f"e{i}", "name": f"Entity {i}"},
        )
    yield store
    store.close()


@pytest.mark.parametrize("query", [
    "MATCH (e:Entity) RETURN e.name LIMIT 5",
    "match (e:Entity) return e.name",
    "  OPTIONAL MATCH (a:Entity)-[r:REL]->(b:Entity) RETURN a.name, b.name",
    "WITH 1 AS x RETURN x",
    "UNWIND [1, 2, 3] AS n RETURN n",
    "RETURN 1",
    "CALL show_tables() RETURN *",
    "CALL table_info('REL') RETURN *",
    "CALL db_version() RETURN *",
])
def test_read_queries_are_allowed(query):
    validate_cypher(query)


# These two are the reason this module exists. Both SUCCEED against a
# read-only Kuzu connection; only this guard stops them.
def test_copy_to_a_file_is_rejected():
    with pytest.raises(CypherRejected) as excinfo:
        validate_cypher("COPY (MATCH (e:Entity) RETURN e.id, e.name) TO '/tmp/leak.csv'")
    assert "COPY" in str(excinfo.value)


def test_load_from_a_file_is_rejected():
    with pytest.raises(CypherRejected) as excinfo:
        validate_cypher("LOAD FROM '/etc/hostname' (file_format='csv') RETURN *")
    assert "LOAD" in str(excinfo.value)


@pytest.mark.parametrize("query", [
    "CREATE (n:Entity {id: 'x'})",
    "MATCH (e:Entity) SET e.name = 'x'",
    "MATCH (e:Entity) DETACH DELETE e",
    "MERGE (n:Entity {id: 'x'})",
    "DROP TABLE Entity",
    "ALTER TABLE REL ADD junk STRING",
    "ATTACH '/tmp/other.kz' AS other (dbtype kuzu)",
    "INSTALL httpfs",
    "EXPORT DATABASE '/tmp/dump'",
    "IMPORT DATABASE '/tmp/dump'",
])
def test_write_and_io_statements_are_rejected(query):
    with pytest.raises(CypherRejected):
        validate_cypher(query)


def test_an_unlisted_call_is_rejected():
    with pytest.raises(CypherRejected) as excinfo:
        validate_cypher("CALL some_other_procedure() RETURN *")
    assert "some_other_procedure" in str(excinfo.value)


# Fix round 1: the allowlist was checked only when the query's FIRST token
# was CALL. Kuzu fully supports mid-query CALL, so anything opening with
# MATCH/WITH/UNWIND/RETURN/OPTIONAL MATCH reached an arbitrary, unlisted
# procedure without ever touching _ALLOWED_CALLS. Verified against a real
# read-only Kuzu 0.11.3 store before this fix existed: this exact query
# executed cleanly and returned [1, '16'] -- validate_cypher returned None
# (allowed it), it did not raise or error.
def test_a_mid_query_call_to_an_unlisted_builtin_is_rejected():
    with pytest.raises(CypherRejected) as excinfo:
        validate_cypher("MATCH (e:Entity) CALL current_setting('threads') RETURN *")
    assert "current_setting" in str(excinfo.value)


# The headline case: READ_CSV_SERIAL is Kuzu's table function for reading
# a CSV file from disk. Reaching it through a mid-query CALL is the
# CALL-shaped equivalent of the LOAD FROM primitive this whole module
# exists to stop -- not a style violation. Before this fix,
# validate_cypher returned None (allowed) for this query too.
#
# NOTE: READ_CSV_SERIAL is also in _BANNED (fix round 2, remedy B), so this
# test alone would still pass even if the CALL allowlist scan (remedy A)
# were reverted entirely -- it is caught by the whole-word ban before the
# CALL scan ever runs. See test_a_mid_query_call_to_an_unbanned_file_reader
# below for the test that exercises remedy A specifically.
def test_a_mid_query_call_to_a_file_reader_is_rejected():
    with pytest.raises(CypherRejected):
        validate_cypher(
            "MATCH (e:Entity) CALL READ_CSV_SERIAL('/etc/hostname') RETURN *"
        )


# Fix round 2: this is the companion the note above asks for. `glob` names
# a file-reading table function in some Kuzu builds but is NOT in _BANNED
# (banning a word that short and common would be its own usability
# disaster), so this can only be caught by the CALL allowlist scan --
# proving remedy A does the work on its own, not remedy B.
def test_a_mid_query_call_to_an_unbanned_file_reader_is_rejected():
    with pytest.raises(CypherRejected) as excinfo:
        validate_cypher("MATCH (e:Entity) CALL glob('/etc/*') RETURN *")
    assert "glob" in str(excinfo.value)


# Fix round 2, Minor 4: the fail-closed test below (CALL with no name) used
# `CALL ()`, a shape Kuzu itself cannot parse -- so it never tested the
# routes that genuinely execute past a naive `CALL\s+(\w+)` regex. The
# reviewer verified by execution that Kuzu 0.11.3 DOES parse and run both
# of these; only this guard's optional-capture-group design (name resolves
# to None, which is rejected) stops them.
def test_a_backtick_quoted_call_name_is_rejected():
    # Kuzu-parseable: a backtick-quoted identifier is a valid procedure
    # name in Cypher. `CALL `x`(...)` runs `x` just like `CALL x(...)`.
    with pytest.raises(CypherRejected):
        validate_cypher("MATCH (e:Entity) CALL `current_setting`('threads') RETURN *")


def test_a_comment_as_the_call_separator_is_rejected():
    # Kuzu-parseable: `/* ... */` is a valid (zero-width, semantically)
    # separator between CALL and the procedure name.
    with pytest.raises(CypherRejected):
        validate_cypher("MATCH (e:Entity) CALL/*x*/current_setting('threads') RETURN *")


# Fix round 3: the round-2 version of this test used a SUBSTRING match
# ("CALLread_csv_serial", no separator at all) to exercise the belt. Round
# 3 deliberately narrowed the belt from a substring test to a whole-word
# test (see the belt's own comment for why: the substring form rejected
# "Recalled to Life"), and that same no-separator input is no longer
# caught by anything -- see test_a_call_fused_to_its_name_is_not_caught_
# but_is_unparseable_by_kuzu below. This test is adjusted to an input the
# WORD-based belt genuinely still catches: `_WORD` is ASCII-only
# ([A-Za-z_][A-Za-z_0-9]*), so it tokenizes 'CALL' as a standalone word
# when it's immediately followed by a non-ASCII letter -- but Python's
# `\b` uses Unicode `\w` semantics by default, under which that same
# non-ASCII letter IS a word character, so \bCALL\b does not consider this
# a word boundary and _CALL_TOKEN finds no match at all. That is a real,
# reproducible divergence between an ASCII-only tokenizer and a
# Unicode-aware \b in the CURRENT code -- not a hypothetical -- and it is
# exactly the kind of drift the belt exists to catch.
def test_the_structural_fail_closed_belt_catches_a_standalone_call_the_token_scan_misses():
    with pytest.raises(CypherRejected) as excinfo:
        validate_cypher("MATCH (e:Entity) WHERE e.name = 'CALLé' RETURN e.id")
    assert "CALL" in str(excinfo.value)


def test_a_call_fused_to_its_name_is_not_caught_but_is_unparseable_by_kuzu():
    # Documents the belt's known gap (see its comment): CALLread_csv_serial
    # with no separator at all tokenizes as ONE word, CALLREAD_CSV_SERIAL,
    # under `_WORD`'s rules -- not as the word CALL plus a name -- so
    # neither `\bCALL\b` nor the word-based belt fires, and this call is
    # ALLOWED by validate_cypher. That is only safe because Kuzu's own
    # parser refuses this exact no-separator shape (verified against
    # 0.11.3); this module does not independently guarantee it, hence this
    # test exists to make that dependency explicit rather than silent.
    validate_cypher("CALLread_csv_serial('/etc/hostname')")


@pytest.mark.parametrize("query", [
    "WITH 1 AS x CALL some_other_procedure() RETURN x",
    "UNWIND [1, 2, 3] AS n CALL some_other_procedure() RETURN n",
    "RETURN 1 CALL some_other_procedure()",
    "OPTIONAL MATCH (a:Entity)-[:REL]->(b:Entity) CALL some_other_procedure() RETURN a.name",
])
def test_the_call_bypass_is_closed_from_every_allowed_opening(query):
    # The pre-fix bypass worked no matter which allowed keyword opened the
    # query -- each opening reached the same unguarded code path
    # independently, so one passing example would not have proven the fix.
    with pytest.raises(CypherRejected) as excinfo:
        validate_cypher(query)
    assert "some_other_procedure" in str(excinfo.value)


def test_a_mid_query_allowlisted_call_still_works():
    # Closing the bypass must not ban mid-query CALL outright -- that would
    # be the easy fix, and it would break this legitimate, Kuzu-supported
    # query shape for a procedure that IS on the allowlist.
    validate_cypher("MATCH (e:Entity) CALL table_info('Entity') RETURN *")


@pytest.mark.parametrize("query", [
    "RETURN read_csv_serial('/etc/hostname')",
    "RETURN read_csv_parallel('/etc/hostname')",
    "RETURN read_parquet('/etc/hostname')",
    "RETURN read_npy('/etc/hostname')",
    "RETURN json_scan('/etc/hostname')",
])
def test_file_reader_table_functions_are_banned_as_whole_words(query):
    # Defence in depth (remedy B): banned outright, not only reachable
    # through the CALL allowlist, so the control does not depend on CALL
    # being the only syntactic route to a file reader.
    with pytest.raises(CypherRejected):
        validate_cypher(query)


def test_a_call_with_no_extractable_procedure_name_is_rejected():
    # Fail closed: if the name after CALL can't be parsed out, the query
    # is rejected rather than the allowlist check silently finding nothing
    # to compare and letting the query through. NOTE: `CALL ()` is a shape
    # Kuzu itself cannot parse -- see test_a_backtick_quoted_call_name_is_
    # rejected and test_a_comment_as_the_call_separator_is_rejected above
    # for the two shapes that genuinely execute in Kuzu 0.11.3 and are
    # caught the same way.
    with pytest.raises(CypherRejected) as excinfo:
        validate_cypher("MATCH (e:Entity) CALL () RETURN *")
    assert "CALL" in str(excinfo.value)


# Fix round 2, Important 1: 'CALL' is matched as a whole word everywhere,
# so a perfectly ordinary, Kuzu-executable query merely mentioning "call"
# is rejected too -- this graph holds books, and "The Call of the Wild" is
# one. That is fail-closed, not a bug, but the message has to say so the
# same way the _BANNED rejection already does for 'Set', or a user reading
# it will think the guard is broken.
@pytest.mark.parametrize("query", [
    "MATCH (e:Entity) WHERE e.name = 'Call of the Wild' RETURN e.id",
    "MATCH (e:Entity) WHERE e.name = 'The Call' RETURN e.id",
    "MATCH (e:Entity) WHERE e.name CONTAINS 'call' RETURN e.id",
    "MATCH (e:Entity) RETURN e.id AS call",
    "MATCH (e:Entity) RETURN e.id // CALL current_setting",
])
def test_the_word_call_in_ordinary_text_is_rejected_with_an_honest_message(query):
    with pytest.raises(CypherRejected) as excinfo:
        validate_cypher(query)
    message = str(excinfo.value).lower()
    assert "call" in message
    assert "whole word" in message
    assert "string literal" in message or "comment" in message


# Fix round 3: round 2's structural fail-closed belt (Minor 3) tested for
# the SUBSTRING 'CALL' rather than the whole word, so it rejected any word
# merely CONTAINING those four letters -- recalled, called, recall,
# scallop -- with the belt's message, not the honest CALL message above.
# "Recalled to Life" is the real title of Book 1 of Charles Dickens's A
# Tale of Two Cities: this graph holds books, so ordinary literary names
# hit this. These three are the coordinator's own verified false positives
# against the round-2 code (commit 4f5c769); they must be ALLOWED.
@pytest.mark.parametrize("query", [
    "MATCH (e:Entity) WHERE e.name = 'Recalled to Life' RETURN e.id",
    "MATCH (e:Entity) WHERE e.name CONTAINS 'called' RETURN e.id",
    "MATCH (e:Entity) WHERE e.name = 'Recall' RETURN e.id",
])
def test_ordinary_words_that_merely_contain_call_are_allowed(query):
    validate_cypher(query)


def test_two_statements_are_rejected():
    with pytest.raises(CypherRejected) as excinfo:
        validate_cypher("MATCH (e:Entity) RETURN e.name; DROP TABLE Entity")
    assert "one statement" in str(excinfo.value).lower()


def test_a_trailing_semicolon_is_fine():
    validate_cypher("MATCH (e:Entity) RETURN e.name;")


def test_an_empty_query_is_rejected():
    with pytest.raises(CypherRejected):
        validate_cypher("   ")


# Review Focus 1: a banned token inside a legitimate string literal.
# This is a REAL question -- 'Set' could be a character name -- and the
# guard rejects it on purpose. The message must make that obvious, so the
# user can tell a guard from a bug.
def test_a_banned_word_in_a_string_literal_is_rejected_with_a_clear_reason():
    with pytest.raises(CypherRejected) as excinfo:
        validate_cypher("MATCH (e:Entity) WHERE e.name = 'Set' RETURN e.id")
    message = str(excinfo.value)
    assert "SET" in message
    assert "string literal" in message.lower() or "even inside" in message.lower()


def test_row_cap_truncates_and_says_so(live_kuzu):
    columns, rows, truncated = run_guarded(
        live_kuzu, "MATCH (a:Entity), (b:Entity) RETURN a.id, b.id", row_cap=10
    )
    assert columns == ["a.id", "b.id"]
    assert len(rows) == 10
    assert truncated is True


def test_a_small_result_is_not_truncated(live_kuzu):
    columns, rows, truncated = run_guarded(
        live_kuzu, "MATCH (e:Entity) RETURN e.id LIMIT 3"
    )
    assert len(rows) <= 3
    assert truncated is False


# The COPY/LOAD tests above prove validate_cypher raises -- but that alone
# doesn't prove the guard runs BEFORE Kuzu ever sees the statement. Prove
# it here: point COPY TO at a real path and assert no file is created,
# which would be impossible if Kuzu had executed the statement first.
def test_copy_to_is_rejected_before_kuzu_ever_sees_it(tmp_path, live_kuzu):
    target = tmp_path / "leak.csv"
    with pytest.raises(CypherRejected):
        run_guarded(
            live_kuzu,
            f"COPY (MATCH (e:Entity) RETURN e.id, e.name) TO '{target}'",
        )
    assert not target.exists()


# Fix round 2, Minor 4: nothing asserted that run_guarded closes the
# QueryResult -- the row-cap test above passes identically whether or not
# `finally: result.close()` is even present. Use a fake QueryResult that
# records close() calls, so a future edit that drops the try/finally (or
# moves the close off the exception path) fails a test instead of just
# leaking handles in production.
class _FakeResult:
    def __init__(self, columns, rows, raise_at=None):
        self._columns = columns
        self._rows = list(rows)
        self._i = 0
        self._raise_at = raise_at
        self.closed = False

    def get_column_names(self):
        return self._columns

    def has_next(self):
        return self._i < len(self._rows)

    def get_next(self):
        if self._raise_at is not None and self._i == self._raise_at:
            raise RuntimeError("boom")
        row = self._rows[self._i]
        self._i += 1
        return row

    def close(self):
        self.closed = True


class _FakeKuzu:
    def __init__(self, result):
        self._result = result

    def execute(self, query, params=None):
        return self._result


def test_run_guarded_closes_the_result_on_the_normal_path():
    result = _FakeResult(["e.id"], [["a"], ["b"]])
    run_guarded(_FakeKuzu(result), "MATCH (e:Entity) RETURN e.id")
    assert result.closed is True


def test_run_guarded_closes_the_result_on_the_truncation_path():
    result = _FakeResult(["e.id"], [["a"], ["b"], ["c"]])
    _, _, truncated = run_guarded(
        _FakeKuzu(result), "MATCH (e:Entity) RETURN e.id", row_cap=2
    )
    assert truncated is True
    assert result.closed is True


def test_run_guarded_closes_the_result_when_reading_raises():
    result = _FakeResult(["e.id"], [["a"], ["b"]], raise_at=1)
    with pytest.raises(RuntimeError):
        run_guarded(_FakeKuzu(result), "MATCH (e:Entity) RETURN e.id")
    assert result.closed is True
