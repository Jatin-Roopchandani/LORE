"""Hand-written Cypher. The LLM picks one and fills its slots; it never
writes Cypher itself.

Read-only holds by construction because these are module constants, not
generated text — see tests/test_query_templates.py, which pins that.
Every slot binds as a $parameter (spec 2.2), and every template aliases
its RETURN to ROW_FIELDS so assembly never branches on which one ran.
"""
from __future__ import annotations

from lore.query.models import Template

MAX_EVIDENCE_CHARS = 12000

ROW_FIELDS: tuple[str, ...] = (
    "subject", "predicate", "predicate_raw", "object", "evidence",
    "confidence", "book_id", "chapter_seq", "chunk_id",
)

# Every template binds the edge as a DIRECTED (a)-[r:REL]->(b), so subject
# and object are always r's true endpoints. An undirected match with the
# queried entity bound to one side reverses every incoming edge, and
# aliasing a node r does not touch as subject invents an edge outright.
# DISTINCT at the source makes LIMIT count facts, not repeats.
_RETURN = (
    "RETURN DISTINCT a.name AS subject, r.predicate AS predicate, "
    "r.predicate_raw AS predicate_raw, b.name AS object, "
    "r.evidence AS evidence, r.confidence AS confidence, "
    "r.book_id AS book_id, r.chapter_seq AS chapter_seq, "
    "r.chunk_id AS chunk_id "
)


def _sized(field: str) -> str:
    """`field` prefixed by its zero-padded length, so a concatenation of
    several is injective: no separator a value could contain is needed."""
    return f"lpad(cast(size({field}) AS STRING), 10, '0') + {field}"


# One sort key per distinct fact: chapter first (zero-padded, so it sorts as
# a number), then every other field, length-prefixed so that two distinct
# facts can never share a key.
_FACT_KEY = " + ".join([
    "lpad(cast(chapter_seq AS STRING), 10, '0')",
    _sized("chunk_id"), _sized("predicate"), _sized("predicate_raw"),
    _sized("subject"), _sized("object"),
    _sized("cast(confidence AS STRING)"), _sized("evidence"),
])

# across_books: every book the entity appears in contributes up to $limit
# facts, its earliest chapters first. A single `ORDER BY book_id ... LIMIT`
# spent the whole budget on the first book: Holmes at 40 returned 40 rows of
# A Study in Scarlet and none of The Sign of the Four's 122.
#
# Measured on Kuzu 0.11.3, and why it is built this way:
# - a list slice `[1:n]` is 1-based and inclusive, and past the end it
#   simply stops, so `[1:$limit]` is "the first $limit";
# - `struct_pack(name := value)` builds each row (no `{...}` literal, which
#   the no-interpolation guard forbids in template text);
# - `list_sort` cannot sort a list of structs (it hits KU_UNREACHABLE), so
#   the rows are collected as structs but the ORDER is taken from a sorted
#   list of string keys, which does not depend on the order `collect` sees;
# - DISTINCT happens once, in `WITH DISTINCT` before the keys are built, so
#   the budget counts facts and exact parallel duplicates cannot use up a
#   book's share. Each fact sits in exactly one book's list, so nothing after
#   that can repeat a row;
# - the final RETURN is deliberately NOT `RETURN DISTINCT`: in 0.11.3,
#   `RETURN DISTINCT ... ORDER BY` with no LIMIT comes back out of order about
#   one run in four (82 of 300 on a plain MATCH, the same through this
#   pipeline), while plain `RETURN ... ORDER BY` was ordered 450 of 450. The
#   other templates are unaffected because their ORDER BY carries a LIMIT.
_ACROSS_BOOKS = (
    "MATCH (a:Entity)-[r:REL]->(b:Entity) "
    "WHERE a.id = $entity_id OR b.id = $entity_id "
    "WITH DISTINCT a.name AS subject, r.predicate AS predicate, "
    "r.predicate_raw AS predicate_raw, b.name AS object, "
    "r.evidence AS evidence, r.confidence AS confidence, "
    "r.book_id AS book_id, r.chapter_seq AS chapter_seq, "
    "r.chunk_id AS chunk_id "
    "WITH book_id, struct_pack(subject := subject, predicate := predicate, "
    "predicate_raw := predicate_raw, object := object, evidence := evidence, "
    "confidence := confidence, chapter_seq := chapter_seq, "
    "chunk_id := chunk_id, k := " + _FACT_KEY + ") AS fact "
    "WITH book_id, collect(fact) AS facts, "
    "list_sort(collect(fact.k))[1:$limit] AS kept "
    "UNWIND facts AS fact "
    "WITH book_id, fact, kept WHERE list_contains(kept, fact.k) "
    "RETURN fact.subject AS subject, fact.predicate AS predicate, "
    "fact.predicate_raw AS predicate_raw, fact.object AS object, "
    "fact.evidence AS evidence, fact.confidence AS confidence, "
    "book_id AS book_id, fact.chapter_seq AS chapter_seq, "
    "fact.chunk_id AS chunk_id "
    "ORDER BY book_id, chapter_seq"
)

TEMPLATES: dict[str, Template] = {
    "neighbours": Template(
        name="neighbours",
        cypher=(
            "MATCH (a:Entity)-[r:REL]->(b:Entity) "
            "WHERE a.id = $entity_id OR b.id = $entity_id "
            + _RETURN + "LIMIT $limit"
        ),
        slots=("entity_id", "limit"),
        description=(
            "Everything one named thing is directly involved in. Use for "
            "'who does X deal with', 'what happens to X', 'tell me about X'."
        ),
    ),
    "two_hop": Template(
        name="two_hop",
        cypher=(
            # The entity's neighbours first, then every edge that touches a
            # neighbour and not the entity itself, in its true direction.
            "MATCH (e:Entity)-[:REL]-(m:Entity) "
            "WHERE e.id = $entity_id AND m.id <> e.id "
            "WITH e, collect(DISTINCT m.id) AS mids "
            "MATCH (a:Entity)-[r:REL]->(b:Entity) "
            "WHERE (a.id IN mids OR b.id IN mids) "
            "AND a.id <> e.id AND b.id <> e.id "
            + _RETURN + "LIMIT $limit"
        ),
        slots=("entity_id", "limit"),
        description=(
            "The wider neighbourhood two steps out from one named thing. "
            "Use when the question is about indirect influence or "
            "consequences rather than direct dealings."
        ),
    ),
    "path_between": Template(
        name="path_between",
        cypher=(
            # Intermediates linked to both, collected under OPTIONAL MATCH so
            # a pair with only direct edges still returns them. Then: direct
            # a-b edges, and both hops a-m and m-b, each either direction.
            "MATCH (p:Entity), (q:Entity) WHERE p.id = $a_id AND q.id = $b_id "
            "OPTIONAL MATCH (p)-[:REL]-(m:Entity)-[:REL]-(q) "
            "WHERE m.id <> p.id AND m.id <> q.id "
            "WITH p, q, collect(DISTINCT m.id) AS mids "
            "MATCH (a:Entity)-[r:REL]->(b:Entity) "
            "WHERE (a.id = p.id AND b.id = q.id) "
            "OR (a.id = q.id AND b.id = p.id) "
            "OR ((a.id = p.id OR a.id = q.id) AND b.id IN mids) "
            "OR ((b.id = p.id OR b.id = q.id) AND a.id IN mids) "
            + _RETURN + "LIMIT $limit"
        ),
        slots=("a_id", "b_id", "limit"),
        description=(
            "How two named things are connected through something in "
            "between. Use for 'how are X and Y related', 'what links X to Y'."
        ),
    ),
    "by_predicate": Template(
        name="by_predicate",
        cypher=(
            "MATCH (a:Entity)-[r:REL]->(b:Entity) "
            "WHERE (a.id = $entity_id OR b.id = $entity_id) "
            "AND r.predicate = $predicate "
            + _RETURN + "LIMIT $limit"
        ),
        slots=("entity_id", "predicate", "limit"),
        description=(
            "One specific kind of relationship involving one named thing. "
            "Use when the question names the relationship: who opposes X, "
            "who works for X, what X is a member of."
        ),
    ),
    "shared_neighbours": Template(
        name="shared_neighbours",
        cypher=(
            "MATCH (a:Entity)-[r:REL]->(b:Entity) "
            "WHERE a.id IN [$a_id, $b_id] OR b.id IN [$a_id, $b_id] "
            + _RETURN + "LIMIT $limit"
        ),
        slots=("a_id", "b_id", "limit"),
        description=(
            "Everything two named things are each involved in, so their "
            "common ground can be compared. Use for 'what do X and Y have "
            "in common', 'how do X and Y differ'."
        ),
    ),
    "across_books": Template(
        name="across_books",
        # `limit` is PER BOOK here (Task 8b); see _ACROSS_BOOKS.
        cypher=_ACROSS_BOOKS,
        slots=("entity_id", "limit"),
        description=(
            "One named thing's involvements across every book it appears "
            "in, ordered by book then chapter. Use when the question "
            "compares books or asks how something differs between them."
        ),
    ),
    "chapter_window": Template(
        name="chapter_window",
        cypher=(
            "MATCH (a:Entity)-[r:REL]->(b:Entity) "
            "WHERE (a.id = $entity_id OR b.id = $entity_id) "
            "AND r.chapter_seq >= $lo AND r.chapter_seq <= $hi "
            + _RETURN + "ORDER BY chapter_seq LIMIT $limit"
        ),
        slots=("entity_id", "lo", "hi", "limit"),
        description=(
            "One named thing's involvements within a range of chapters. "
            "Use when the question is about a period: early on, later, "
            "by the end, in the first half."
        ),
    ),
}
