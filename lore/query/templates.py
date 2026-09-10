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

_RETURN = (
    "RETURN a.name AS subject, r.predicate AS predicate, "
    "r.predicate_raw AS predicate_raw, b.name AS object, "
    "r.evidence AS evidence, r.confidence AS confidence, "
    "r.book_id AS book_id, r.chapter_seq AS chapter_seq, "
    "r.chunk_id AS chunk_id "
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
            "MATCH (a:Entity)-[:REL]-(m:Entity)-[r:REL]->(b:Entity) "
            "WHERE a.id = $entity_id AND m.id <> a.id "
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
            "MATCH (a:Entity)-[:REL]-(m:Entity)-[r:REL]-(b:Entity) "
            "WHERE a.id = $a_id AND b.id = $b_id "
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
            "MATCH (a:Entity)-[r:REL]-(b:Entity) "
            "WHERE a.id IN [$a_id, $b_id] "
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
        cypher=(
            "MATCH (a:Entity)-[r:REL]-(b:Entity) "
            "WHERE a.id = $entity_id "
            + _RETURN + "ORDER BY r.book_id, r.chapter_seq LIMIT $limit"
        ),
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
            "MATCH (a:Entity)-[r:REL]-(b:Entity) "
            "WHERE a.id = $entity_id AND r.chapter_seq >= $lo "
            "AND r.chapter_seq <= $hi "
            + _RETURN + "ORDER BY r.chapter_seq LIMIT $limit"
        ),
        slots=("entity_id", "lo", "hi", "limit"),
        description=(
            "One named thing's involvements within a range of chapters. "
            "Use when the question is about a period: early on, later, "
            "by the end, in the first half."
        ),
    ),
}
