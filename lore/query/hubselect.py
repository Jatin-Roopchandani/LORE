"""Which of a hub's edges a graph search keeps, when it has more than the limit.

A template's `ORDER BY chapter ... LIMIT` keeps a hub's EARLIEST edges, so
for a central character the climax and the ending are cut systematically:
measured on a 1,235-passage novel, the protagonist's 1,750 edges cut at 250
kept passages 15-205 only, and `path_between` him and the antagonist
(250 rows, nearly all through intermediates) kept none of their 21 direct
edges, the "I killed him" edge of the climax among them.

So a search fetches up to FETCH_CAP rows and `select_rows` keeps `limit`:

1. pinned: for a two-entity search, the direct edges between the two
   (a direct edge is the shortest path); then edges whose predicate the
   sub-question asks about (`cued_predicates`: "kill" -> KILLS), most
   relevant first. At most PINNED_SHARE of the limit;
2. relevant: the rows whose passage is most similar to the sub-question
   (the same chunk vectors and cosine as the evidence ranking), up to
   RELEVANT_SHARE of the limit together with the pinned rows;
3. spread: the rest of the limit evenly over the story (timeline's
   coverage_order over the remaining rows in story order), so the beginning,
   the middle and the end are all represented.

The kept rows come back in the order they arrived (the template's story
order). Ties break on the row's content, so the selection is a function of
the rows, the scores and the question, never of storage order. Rows at or
under the limit are returned unchanged.
"""
from __future__ import annotations

import re
from typing import Callable, Iterable, Sequence

from lore.ingest.models import chunk_story_key
from lore.query.models import EdgeRow, TemplateCall
from lore.query.timeline import coverage_order

# Off: a search is the template's own `LIMIT`, earliest in the story first, as
# before this module (evals/answer_quality/run_pair.py arm `hub_select`).
HUB_SELECT = True
# Rows a search fetches before selection; bounded so a hub cannot explode it.
FETCH_CAP = 3000
PINNED_SHARE = 0.25
RELEVANT_SHARE = 0.5

# Words that ask about one curated predicate (lore/resolve/predicates.py), as
# word prefixes. For any book: the vocabulary is the graph's, not a story's.
PREDICATE_CUES: dict[str, tuple[str, ...]] = {
    "KILLS": ("kill", "slay", "slew", "slain", "murder", "dead", "death", "die", "died",
              "destroy", "assassin", "execut"),
    "LOVES": ("love", "lover", "marr", "wife", "husband", "court", "romanc", "sweetheart"),
    "OPPOSES": ("oppos", "enem", "fight", "fought", "battle", "confront", "against",
                "rival", "duel", "attack"),
    "MEMBER_OF": ("member", "join", "belong"),
    "ALLEGIANCE_TO": ("serve", "servant", "loyal", "allegian", "sworn", "obey"),
    "POSSESSES": ("own", "possess", "carr", "wield"),
    "BECOMES": ("become", "became", "transform"),
}
_WORD = re.compile(r"[a-z']+")


def cued_predicates(text: str) -> frozenset[str]:
    """The curated predicates a sub-question's words ask about."""
    words = _WORD.findall(text.lower())
    return frozenset(p for p, stems in PREDICATE_CUES.items()
                     if any(w.startswith(s) for w in words for s in stems))


def _content_key(row: EdgeRow) -> tuple:
    return (row.book_id, row.chapter_seq, chunk_story_key(row.chunk_id), row.predicate,
            row.predicate_raw, row.subject, row.object, row.confidence, row.evidence,
            row.subject_id, row.object_id)


def select_rows(rows: Sequence[EdgeRow], limit: int, *,
                scores: dict[str, float] | None = None,
                cued: Iterable[str] = (),
                pair: tuple | None = None) -> list[EdgeRow]:
    """`limit` of `rows` (see the module docstring), in their given order."""
    rows = list(rows)
    if limit <= 0:
        return []
    if len(rows) <= limit:
        return rows
    scores = scores or {}
    cued = frozenset(cued)

    def relevant(row: EdgeRow) -> tuple:
        return (row.chunk_id not in scores, -scores.get(row.chunk_id, 0.0), _content_key(row))

    chosen: list[int] = []
    taken: set[int] = set()

    def take(indices: Iterable[int], cap: int) -> None:
        for i in indices:
            if len(chosen) >= cap:
                return
            if i not in taken:
                taken.add(i)
                chosen.append(i)

    index = range(len(rows))
    pin_cap = max(1, int(limit * PINNED_SHARE))
    if pair is not None:
        a, b = ({pair[0]} if isinstance(pair[0], str) else set(pair[0]),
                {pair[1]} if isinstance(pair[1], str) else set(pair[1]))
        direct = [i for i in index
                  if (rows[i].subject_id in a and rows[i].object_id in b)
                  or (rows[i].subject_id in b and rows[i].object_id in a)]
        take(sorted(direct, key=lambda i: (rows[i].predicate not in cued, relevant(rows[i]))),
             pin_cap)
    if cued:
        take(sorted((i for i in index if rows[i].predicate in cued),
                    key=lambda i: relevant(rows[i])), pin_cap)
    if scores:
        take(sorted(index, key=lambda i: relevant(rows[i])),
             max(len(chosen), int(limit * RELEVANT_SHARE)))
    rest = sorted((i for i in index if i not in taken), key=lambda i: _content_key(rows[i]))
    take((rest[j] for j in coverage_order(len(rest))), limit)
    return [rows[i] for i in sorted(chosen)]


# Evidence ranking: a part's passages holding an edge BETWEEN two entities it
# names, with a predicate its words ask about ("how did he come to believe he
# killed him" -> Rand KILLS the Dark One), join its ranking by reciprocal-rank
# fusion (keyword.fused), at most CUE_HITS of them, the rarest predicate among
# them first (the specific fact over the 12 OPPOSES edges), then the most
# relevant. Off: rank by cosine alone (run_pair.py arm `cue_fusion`).
CUE_FUSION = True
CUE_HITS = 3


def cue_ranked(hits: Sequence[tuple[str, str]], scores: dict[str, float]) -> list[str]:
    """CUE_HITS chunk ids of (chunk_id, predicate) hits, best first."""
    pairs = set(hits)
    count: dict[str, int] = {}
    for _, predicate in pairs:
        count[predicate] = count.get(predicate, 0) + 1
    rarest: dict[str, int] = {}
    for chunk_id, predicate in pairs:
        rarest[chunk_id] = min(rarest.get(chunk_id, count[predicate]), count[predicate])
    return sorted(rarest, key=lambda c: (rarest[c], c not in scores, -scores.get(c, 0.0),
                                         chunk_story_key(c)))[:CUE_HITS]


# An entity an IDENTIFIED_AS edge ties to another (Ba'alzamon, whom the
# characters believe is the Dark One) is searched together with it: a search
# from the Dark One also walks from Ba'alzamon, so the fight the book tells
# under Ba'alzamon's name is found. Off for an eval arm.
SEARCH_IDENTITY_LINKS = True
MAX_LINKED_PER_SLOT = 2
_ENTITY_SLOTS = ("entity_id", "a_id", "b_id")


def _with_links(kuzu, entity_id: str) -> list[str]:
    """The entity, then up to MAX_LINKED_PER_SLOT entities it is identified with."""
    if not SEARCH_IDENTITY_LINKS:
        return [entity_id]
    from lore.query.seed import identified_with  # seed imports this module's callers
    try:
        linked = [i for i in identified_with(kuzu, [entity_id]) if i != entity_id]
    except Exception:  # noqa: BLE001 - a graph without the edge type: nothing linked
        linked = []
    return [entity_id, *linked[:MAX_LINKED_PER_SLOT]]


def identity_variants(kuzu, call: TemplateCall) -> list[TemplateCall]:
    """`call`, plus the same call with each entity slot swapped for an entity
    it is identified with (every combination, the original first). Two slots
    naming the same entity after a swap are skipped."""
    options = [(slot, _with_links(kuzu, str(call.slots[slot])))
               for slot in _ENTITY_SLOTS if slot in call.slots]
    variants = [dict(call.slots)]
    for slot, ids in options:
        variants = [{**v, slot: i} for v in variants for i in ids]
    out = []
    for slots in variants:
        if "a_id" in slots and "b_id" in slots and slots["a_id"] == slots["b_id"]:
            continue
        out.append(TemplateCall(call.template, slots))
    return out or [call]


def search(kuzu, call: TemplateCall, *, lance=None, vector: Sequence[float] | None = None,
           question: str = "",
           keep: Callable[[list[EdgeRow]], list[EdgeRow]] | None = None) -> list[EdgeRow]:
    """Run `call`, then keep its `limit` rows with `select_rows` (per book for
    across_books, whose limit is per book). `keep` filters the fetched rows
    first (a reading position, a world), so the limit counts rows the reader
    may see. With HUB_SELECT off: the template's own LIMIT, then `keep`."""
    from lore.query.subgraph import relevance, run_template  # subgraph imports timeline too

    if not HUB_SELECT:
        rows = run_template(kuzu, call)
        return keep(rows) if keep else rows
    limit = int(call.slots.get("limit", 0))
    rows: list[EdgeRow] = []
    seen: set[tuple] = set()
    variants = identity_variants(kuzu, call)
    for variant in variants:
        for row in run_template(kuzu, TemplateCall(variant.template,
                                                   {**variant.slots, "limit": max(limit, FETCH_CAP)})):
            key = _content_key(row)
            if key not in seen:
                seen.add(key)
                rows.append(row)
    if len(variants) > 1 and call.template != "across_books":
        # several variants: back into story order, as one template returns it
        rows.sort(key=lambda r: (r.book_id, r.chapter_seq, chunk_story_key(r.chunk_id),
                                 _content_key(r)))
    if keep:
        rows = keep(rows)
    groups: dict[str, list[EdgeRow]] = {}
    for row in rows:
        groups.setdefault(row.book_id if call.template == "across_books" else "", []).append(row)
    if all(len(g) <= limit for g in groups.values()):
        return rows
    scores = None
    if lance is not None and vector is not None:
        stored = lance.chunk_vectors(sorted({r.chunk_id for r in rows}))
        scores = relevance(list(vector), stored)
    pair = None
    if "a_id" in call.slots and "b_id" in call.slots:
        pair = (_with_links(kuzu, str(call.slots["a_id"])),
                _with_links(kuzu, str(call.slots["b_id"])))
    cued = cued_predicates(question)
    out: list[EdgeRow] = []
    for group in groups.values():
        out += select_rows(group, limit, scores=scores, cued=cued, pair=pair)
    return out
