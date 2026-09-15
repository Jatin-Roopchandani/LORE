import re
from pathlib import Path

import pytest

from lore.db.kuzu_store import KuzuStore
from lore.query.models import EdgeRow, TemplateCall
from lore.query.subgraph import run_template
from lore.query.templates import ROW_FIELDS, TEMPLATES

WRITE_WORDS = ("CREATE", "MERGE", "SET ", "DELETE", "DROP", "ALTER", "COPY")


def test_every_template_is_read_only() -> None:
    for name, template in TEMPLATES.items():
        upper = template.cypher.upper()
        for word in WRITE_WORDS:
            assert word not in upper, f"{name} contains {word!r}"


def test_no_template_interpolates_a_value() -> None:
    # Spec 2.2: every slot binds as a $parameter. An f-string brace or a
    # %s in a template is a value reaching Cypher as text.
    for name, template in TEMPLATES.items():
        assert "{" not in template.cypher, f"{name} has a brace"
        assert "%s" not in template.cypher, f"{name} has a %s"


def test_every_declared_slot_appears_as_a_bound_parameter() -> None:
    for name, template in TEMPLATES.items():
        for slot in template.slots:
            assert f"${slot}" in template.cypher, f"{name} never binds ${slot}"


def test_every_parameter_in_the_cypher_is_a_declared_slot() -> None:
    # The inverse: an unbound $param raises at execution time, and a 3B
    # model filling slots would never know to supply it.
    for name, template in TEMPLATES.items():
        found = set(re.findall(r"\$(\w+)", template.cypher))
        assert found == set(template.slots), f"{name}: {found} != {set(template.slots)}"


def test_every_template_returns_the_uniform_row_shape() -> None:
    # Assembly never branches on which template ran, so every RETURN must
    # alias to the same field names in the same order.
    for name, template in TEMPLATES.items():
        for field in ROW_FIELDS:
            assert f"AS {field}" in template.cypher, f"{name} lacks AS {field}"


def test_row_fields_match_the_edge_row_dataclass() -> None:
    assert ROW_FIELDS == tuple(EdgeRow.__dataclass_fields__)


def test_every_template_is_bounded() -> None:
    # across_books bounds each book by slicing, not the whole result by
    # LIMIT (Task 8b): a global LIMIT could never reach a busy entity's
    # second book.
    for name, template in TEMPLATES.items():
        upper = template.cypher.upper()
        assert "LIMIT" in upper or "[1:$LIMIT]" in upper, f"{name} is unbounded"


def test_every_template_has_a_description_for_the_model() -> None:
    for name, template in TEMPLATES.items():
        assert len(template.description) > 20, f"{name} description too thin"
        assert template.name == name


# --- Task 3b: every row a template returns is a real directed edge -------
#
# The contract tests above read the Cypher as text. These run it against a
# real Kuzu database, because a template can pass every textual check and
# still alias the wrong node as subject or reverse an incoming edge.

_ENTITIES = {"a": "A", "b": "B", "m": "M", "n": "N", "x": "X", "y": "Y", "z": "Z"}

# (subject id, predicate, object id, chunk_id, book_id, chapter_seq)
_EDGES = [
    ("a", "KNOWS", "m", "c1", "b1", 1),          # a -> m -> b
    ("m", "KNOWS", "b", "c2", "b1", 2),
    ("b", "KNOWS", "n", "c3", "b1", 3),          # a <- n <- b
    ("n", "KNOWS", "a", "c4", "b1", 4),
    ("a", "OPPOSES", "b", "c5", "b2", 1),        # direct a - b
    ("x", "ALLEGIANCE_TO", "a", "c6", "b2", 2),  # incoming to a
    ("a", "KNOWS", "x", "c7", "b2", 3),          # exact parallel duplicates
    ("a", "KNOWS", "x", "c7", "b2", 3),
    ("m", "WORKS_FOR", "y", "c8", "b1", 5),      # beyond a neighbour, outgoing
    ("z", "OPPOSES", "m", "c9", "b1", 6),        # beyond a neighbour, incoming
    ("a", "KNOWS", "y", "c10", "b1", 7),         # a's third b1 fact (Task 8b)
]

_CALLS = {
    "neighbours": {"entity_id": "a", "limit": 100},
    "two_hop": {"entity_id": "a", "limit": 100},
    "path_between": {"a_id": "a", "b_id": "b", "limit": 100},
    "by_predicate": {"entity_id": "a", "predicate": "KNOWS", "limit": 100},
    "shared_neighbours": {"a_id": "a", "b_id": "b", "limit": 100},
    "across_books": {"entity_id": "a", "limit": 100},
    "chapter_window": {"entity_id": "a", "lo": 1, "hi": 4, "limit": 100},
}


def _fact(subject: str, predicate: str, obj: str, chunk_id: str) -> tuple:
    return (_ENTITIES[subject], predicate, _ENTITIES[obj], chunk_id)


@pytest.fixture
def graph(tmp_path: Path):
    store = KuzuStore(tmp_path / "kuzu")
    for entity_id, name in _ENTITIES.items():
        store.execute(
            "CREATE (:Entity {id: $id, name: $name, kind: 'Character'})",
            {"id": entity_id, "name": name},
        )
    for subject, predicate, obj, chunk_id, book_id, chapter_seq in _EDGES:
        store.execute(
            "MATCH (a:Entity), (b:Entity) WHERE a.id = $s AND b.id = $o "
            "CREATE (a)-[:REL {predicate: $p, predicate_raw: $p, evidence: 'ev', "
            "confidence: 0.9, chunk_id: $c, book_id: $book, chapter_seq: $ch, "
            "run_id: 'r1'}]->(b)",
            {"s": subject, "o": obj, "p": predicate, "c": chunk_id,
             "book": book_id, "ch": chapter_seq},
        )
    yield store
    store.close()


def _run(store, name: str, **slots):
    return run_template(store, TemplateCall(template=name,
                                            slots={**_CALLS[name], **slots}))


def test_the_fixture_calls_cover_every_template() -> None:
    assert set(_CALLS) == set(TEMPLATES)


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_every_row_is_a_real_directed_edge(graph, name: str) -> None:
    real = {
        tuple(row) for row in graph.execute(
            "MATCH (x:Entity)-[r:REL]->(y:Entity) "
            "RETURN x.name, r.predicate, y.name, r.chunk_id"
        )
    }
    rows = _run(graph, name)
    assert rows, f"{name} returned nothing, so 'every row is real' proves nothing"
    false = [(r.subject, r.predicate, r.object, r.chunk_id) for r in rows
             if (r.subject, r.predicate, r.object, r.chunk_id) not in real]
    assert not false, f"{name} returned edges the graph does not hold: {false}"


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_no_template_returns_a_row_twice(graph, name: str) -> None:
    rows = _run(graph, name)
    assert len(rows) == len(set(rows)), f"{name} repeated a row"


def test_path_between_returns_direct_edges_and_both_hops(graph) -> None:
    facts = {(r.subject, r.predicate, r.object, r.chunk_id)
             for r in _run(graph, "path_between")}
    assert facts == {
        _fact("a", "OPPOSES", "b", "c5"),   # direct
        _fact("a", "KNOWS", "m", "c1"),     # a -> m
        _fact("m", "KNOWS", "b", "c2"),     # m -> b
        _fact("b", "KNOWS", "n", "c3"),     # b -> n
        _fact("n", "KNOWS", "a", "c4"),     # n -> a
    }


def test_two_hop_is_one_step_beyond_the_neighbours(graph) -> None:
    rows = _run(graph, "two_hop")
    assert all("A" not in (r.subject, r.object) for r in rows)
    assert {(r.subject, r.predicate, r.object, r.chunk_id) for r in rows} == {
        _fact("m", "KNOWS", "b", "c2"),
        _fact("b", "KNOWS", "n", "c3"),
        _fact("m", "WORKS_FOR", "y", "c8"),
        _fact("z", "OPPOSES", "m", "c9"),
    }


def test_edge_centred_templates_keep_incoming_edges_the_right_way_round(graph) -> None:
    incoming = _fact("x", "ALLEGIANCE_TO", "a", "c6")
    for name in ("neighbours", "across_books", "shared_neighbours"):
        facts = {(r.subject, r.predicate, r.object, r.chunk_id)
                 for r in _run(graph, name)}
        assert incoming in facts, f"{name} lost or reversed an incoming edge"


def test_path_between_with_no_intermediate_still_returns_direct_edges(graph) -> None:
    # x links to a only directly, so the OPTIONAL MATCH finds no m.
    facts = {(r.subject, r.predicate, r.object, r.chunk_id)
             for r in _run(graph, "path_between", b_id="x")}
    assert facts == {_fact("x", "ALLEGIANCE_TO", "a", "c6"),
                     _fact("a", "KNOWS", "x", "c7")}


def test_ordered_templates_keep_their_order_under_distinct(graph) -> None:
    rows = _run(graph, "across_books")
    keys = [(r.book_id, r.chapter_seq) for r in rows]
    assert keys == sorted(keys)
    assert {r.book_id for r in rows} == {"b1", "b2"}
    window = _run(graph, "chapter_window")
    assert [r.chapter_seq for r in window] == sorted(r.chapter_seq for r in window)
    assert all(1 <= r.chapter_seq <= 4 for r in window)


# --- Task 8b: across_books gives every book its own `limit` ---------------
#
# In the fixture, A has 3 facts in b1 (chapters 1, 4, 7) and 3 in b2
# (chapters 1, 2, 3, one of them a parallel duplicate). M is in b1 only.
# A single global LIMIT in book order returned only b1 for any limit <= 2,
# which on the real graph meant 40 of Holmes's 59 A Study in Scarlet facts
# and none of The Sign of the Four's 122.

def _by_book(rows) -> dict[str, list]:
    books: dict[str, list] = {}
    for row in rows:
        books.setdefault(row.book_id, []).append(row)
    return books


@pytest.mark.parametrize("limit", [1, 2])
def test_across_books_limit_applies_per_book(graph, limit: int) -> None:
    everything = _by_book(_run(graph, "across_books", limit=100))
    capped = _by_book(_run(graph, "across_books", limit=limit))
    # Book one has more than `limit` facts, and book two still appears.
    assert len(everything["b1"]) > limit
    assert set(capped) == {"b1", "b2"}
    for book, rows in capped.items():
        assert len(rows) == min(limit, len(everything[book]))
        assert len(rows) == len(set(rows))
        # The EARLIEST chapters of that book are the ones kept.
        kept = [r.chapter_seq for r in rows]
        dropped = sorted(set(everything[book]) - set(rows),
                         key=lambda r: r.chapter_seq)
        assert kept == sorted(kept)
        assert all(max(kept) <= r.chapter_seq for r in dropped)


def test_across_books_for_a_single_book_entity_is_the_first_limit_by_chapter(graph) -> None:
    everything = _run(graph, "across_books", entity_id="m", limit=100)
    assert {r.book_id for r in everything} == {"b1"}
    rows = _run(graph, "across_books", entity_id="m", limit=2)
    assert [r.chapter_seq for r in rows] == [1, 2]
    assert {(r.subject, r.predicate, r.object, r.chunk_id) for r in rows} == {
        _fact("a", "KNOWS", "m", "c1"),
        _fact("m", "KNOWS", "b", "c2"),
    }


def test_across_books_under_a_per_book_limit_returns_only_real_edges(graph) -> None:
    real = {
        tuple(row) for row in graph.execute(
            "MATCH (x:Entity)-[r:REL]->(y:Entity) "
            "RETURN x.name, r.predicate, y.name, r.chunk_id, r.book_id, r.chapter_seq"
        )
    }
    rows = _run(graph, "across_books", limit=1)
    assert rows
    assert all((r.subject, r.predicate, r.object, r.chunk_id, r.book_id,
                r.chapter_seq) in real for r in rows)


def test_across_books_order_is_reliable_not_lucky(graph) -> None:
    # Kuzu 0.11.3 returns `RETURN DISTINCT ... ORDER BY` with no LIMIT out of
    # order about one run in four, so one ordered run proves little. Twenty
    # in a row would pass by luck well under 1% of the time.
    for _ in range(20):
        for limit in (1, 100):
            keys = [(r.book_id, r.chapter_seq)
                    for r in _run(graph, "across_books", limit=limit)]
            assert keys == sorted(keys)
