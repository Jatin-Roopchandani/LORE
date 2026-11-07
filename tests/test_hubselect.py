"""Question-aware, story-spanning selection of a hub's edges (lore/query/hubselect.py)."""
from lore.query.hubselect import cued_predicates, select_rows
from lore.query.models import EdgeRow


def _row(seq: int, predicate: str = "INTERACTS_WITH", subject: str = "Hero",
         obj: str = "Other", sid: str = "h", oid: str = "o") -> EdgeRow:
    return EdgeRow(subject=subject, predicate=predicate, predicate_raw=predicate, object=obj,
                   evidence=f"ev {seq}", confidence=0.9, book_id="b", chapter_seq=1,
                   chunk_id=f"b:1:{seq}", subject_id=sid, object_id=oid)


def _hub(n: int = 400) -> list[EdgeRow]:
    return [_row(i) for i in range(1, n + 1)]


def test_under_the_limit_rows_are_unchanged() -> None:
    rows = _hub(10)
    assert select_rows(rows, 10) == rows


def test_selection_spans_the_whole_story_not_its_start() -> None:
    kept = select_rows(_hub(), 40)
    seqs = [int(r.chunk_id.rsplit(":", 1)[1]) for r in kept]
    assert len(kept) == 40
    assert seqs == sorted(seqs)          # still story order
    assert seqs[0] == 1 and seqs[-1] == 400  # beginning and end both kept
    assert max(b - a for a, b in zip(seqs, seqs[1:])) <= 400 // 20


def test_the_most_relevant_passages_are_kept() -> None:
    rows = _hub()
    scores = {r.chunk_id: 0.5 for r in rows} | {"b:1:377": 0.9, "b:1:212": 0.8}
    kept = {r.chunk_id for r in select_rows(rows, 20, scores=scores)}
    assert {"b:1:377", "b:1:212"} <= kept


def test_a_predicate_the_question_asks_about_is_pinned() -> None:
    rows = _hub()
    rows[385] = _row(386, "KILLS", obj="Villain", oid="v")
    assert cued_predicates("how did he come to believe that he killed him") == {"KILLS"}
    scores = {r.chunk_id: 0.9 for r in rows if r.predicate != "KILLS"}
    kept = select_rows(rows, 20, scores=scores, cued=cued_predicates("who killed him"))
    assert any(r.predicate == "KILLS" for r in kept)


def test_direct_edges_between_the_pair_come_first() -> None:
    rows = _hub()  # all h -> o: paths through intermediates, say
    rows = [_row(i, sid="h", oid=f"m{i}") for i in range(1, 400)]
    rows.append(_row(399, "KILLS", obj="Villain", sid="h", oid="v"))
    rows.append(_row(5, "OPPOSES", subject="Villain", obj="Hero", sid="v", oid="h"))
    kept = select_rows(rows, 10, pair=("h", "v"))
    direct = [r for r in kept if {r.subject_id, r.object_id} == {"h", "v"}]
    assert len(direct) == 2


def _hub_graph(path):
    """Hero with 300 early edges to minor people and, late, a KILLS edge to the
    Villain; Villain linked to every minor person too (paths through them)."""
    from lore.db.kuzu_store import KuzuStore

    store = KuzuStore(path)
    people = [("h", "Hero"), ("v", "Villain")] + [(f"m{i}", f"M{i}") for i in range(150)]
    for eid, name in people:
        store.execute("CREATE (:Entity {id: $id, name: $n, kind: 'Character'})",
                      {"id": eid, "n": name})

    def edge(s, o, p, seq, ev="ev"):
        store.execute(
            "MATCH (a:Entity), (b:Entity) WHERE a.id = $s AND b.id = $o "
            "CREATE (a)-[:REL {predicate: $p, predicate_raw: $p, evidence: $ev, confidence: 0.9,"
            " chunk_id: $c, book_id: 'b', chapter_seq: 1, run_id: 'r'}]->(b)",
            {"s": s, "o": o, "p": p, "c": f"b:1:{seq}", "ev": ev})

    for i in range(150):
        edge("h", f"m{i}", "INTERACTS_WITH", i + 1)
        edge(f"m{i}", "v", "ALLEGIANCE_TO", i + 1)
        edge("h", f"m{i}", "OPPOSES", i + 200)
    edge("h", "v", "KILLS", 1186, "I killed him.")
    return store


def test_search_keeps_a_hubs_late_climax_edge(tmp_path) -> None:
    from lore.query import hubselect

    store = _hub_graph(tmp_path / "kuzu")
    from lore.query.models import TemplateCall
    calls = [TemplateCall("neighbours", {"entity_id": "h", "limit": 40}),
             TemplateCall("path_between", {"a_id": "h", "b_id": "v", "limit": 40})]
    for call in calls:
        kept = hubselect.search(store, call, question="how did he come to believe he killed him")
        assert len(kept) == 40, call.template
        assert any(r.predicate == "KILLS" and r.chunk_id == "b:1:1186" for r in kept), call
    hubselect.HUB_SELECT = False
    try:
        old = hubselect.search(store, calls[1], question="how did he kill him")
        assert not any(r.chunk_id == "b:1:1186" for r in old)  # the bug, as it was
    finally:
        hubselect.HUB_SELECT = True
    store.close()


def test_cue_ranked_puts_the_rare_specific_fact_first() -> None:
    from lore.query.hubselect import CUE_HITS, cue_ranked

    hits = [(f"b:1:{i}", "OPPOSES") for i in range(10)] + [("b:1:1186", "KILLS")]
    scores = {f"b:1:{i}": 0.8 for i in range(10)} | {"b:1:1186": 0.7}
    ranked = cue_ranked(hits, scores)
    assert ranked[0] == "b:1:1186" and len(ranked) == CUE_HITS
    assert cue_ranked([], scores) == []


def test_selection_is_deterministic_whatever_the_row_order() -> None:
    rows = _hub()
    scores = {r.chunk_id: (i * 37 % 101) / 101 for i, r in enumerate(rows)}
    a = select_rows(rows, 30, scores=scores, cued={"KILLS"})
    b = select_rows(list(reversed(rows)), 30, scores=scores, cued={"KILLS"})
    assert sorted(map(repr, a)) == sorted(map(repr, b))
