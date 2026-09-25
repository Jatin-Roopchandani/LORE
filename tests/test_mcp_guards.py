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
