"""Scene setup: the passage just before a scene the evidence opens mid-flow.

SCENE_SETUP (2026-11-09, spec order-and-setup). Measured on "how did Watson meet
Holmes": retrieval found the meeting, whose passage opens "On the day he
decided to move ...", but not the passage before it, which says why he had to
move. A question about events is answered better with the scene's setup.

So, for an event question (`event_question`), each of the best-ranked evidence
passages that opens mid-flow -- its first sentence refers back ("On the day
...", "That evening ...", "The decision ...", "He ...") or it is the earliest
of a run of consecutive evidence passages -- brings the IMMEDIATELY PRECEDING
passage of its chapter as context (`setups`). At most MAX_SETUP per answer,
within the evidence budget (the least relevant ranked passages make room),
never across a chapter, never past the reader's position or outside the
reader's books. The synthesis prompt says which passage sets up which, and a
claim from it still quotes it like any other. Off gives the code before it.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Sequence

from lore.query import visible as visible_module

SCENE_SETUP = True
MAX_SETUP = 2
# Only the best-ranked passages bring their setup: one ranked lower is less
# likely to be the scene the answer tells.
SETUP_TOP = 6

# A question about what happened: how, why or when something was done, what
# happened or led to it, who did it.
_EVENT_QUESTION = re.compile(
    r"\b(?:how|why|when|where)\s+(?:did|does|do|had|has|have|was|were|is|are|could|would)\b"
    r"|\bwhat\s+(?:happen\w*|led|made|caused|did)\b"
    r"|\bwho\s+(?!is\b|are\b|was\b|were\b)[a-z]+ed\b"
    r"|\b(?:describe|tell\s+me\s+about|recount)\s+(?:the\s+|how\b)", re.I)

# A first sentence that leans on what the story told just before it.
_MID_FLOW = re.compile(
    r"^\W*(?:"
    r"(?:on|by|from|at|in|until|till|after|before)\s+(?:the|that|this)\s+(?:very\s+)?"
    r"(?:same\s+)?(?:day|night|evening|morning|afternoon|moment|time|occasion|hour|week)\b"
    r"|(?:that|this|the\s+next|the\s+following|next)\s+"
    r"(?:day|night|evening|morning|afternoon|week|moment|time)\b"
    r"|(?:after|afterwards|afterward|then|thereupon|meanwhile|so|thus|hence|accordingly"
    r"|having|still|nevertheless|however|but|and)\b"
    r"|the\s+(?:decision|plan|idea|news|matter|affair|incident|result|same|latter|former)\b"
    r")", re.I)
_LEAD = re.compile(r"\S.*?(?:[.!?](?=\s|$)|$)", re.S)


def event_question(question: str) -> bool:
    """Whether the question asks about events: how, why or when something was
    done, what happened, who did something."""
    return bool(_EVENT_QUESTION.search(question or ""))


def mid_flow(text: str) -> bool:
    """Whether a passage's first sentence refers back to what came before it."""
    from lore.query.write import leans_back  # write.py imports nothing from here
    first = _LEAD.match((text or "").lstrip())
    lead = first.group(0) if first else ""
    return bool(_MID_FLOW.search(lead)) or leans_back(lead)


def preceding_chunk(conn: sqlite3.Connection, chunk_id: str) -> tuple[str, str, int, str] | None:
    """(chunk id, book id, chapter, text) of the chunk just before `chunk_id` in
    its chapter, or None at a chapter's start (or for an unknown chunk)."""
    row = conn.execute(
        "SELECT p.id, p.book_id, p.chapter_seq, p.text FROM chunks a JOIN chunks p"
        " ON p.book_id = a.book_id AND p.chapter_seq = a.chapter_seq AND p.seq < a.seq"
        " WHERE a.id = ? ORDER BY p.seq DESC LIMIT 1", (chunk_id,)).fetchone()
    return (row[0], row[1], int(row[2]), row[3]) if row else None


def next_chunk_id(conn: sqlite3.Connection, chunk_id: str) -> str | None:
    """The chunk just after `chunk_id` in its chapter, or None."""
    row = conn.execute(
        "SELECT n.id FROM chunks a JOIN chunks n"
        " ON n.book_id = a.book_id AND n.chapter_seq = a.chapter_seq AND n.seq > a.seq"
        " WHERE a.id = ? ORDER BY n.seq ASC LIMIT 1", (chunk_id,)).fetchone()
    return row[0] if row else None


def setups(conn: sqlite3.Connection, ranked: Sequence[tuple], held: set[str], *,
           visible=None, limit: int | None = None, top: int | None = None,
           is_boilerplate=None) -> list[tuple[tuple, str]]:
    """(setup item, anchor chunk id) pairs, at most `limit`: for each of the
    first `top` `ranked` items (chunk id, book id, chapter, text), most relevant
    first, that opens mid-flow (`mid_flow`) or is the earliest of a run of
    consecutive held passages, the chunk just before it in its chapter, when
    that chunk is not `held` already (or taken as another's setup), is visible
    at the reading position, and is story (not boilerplate)."""
    limit = MAX_SETUP if limit is None else limit
    top = SETUP_TOP if top is None else top
    out: list[tuple[tuple, str]] = []
    taken = set(held)
    for chunk_id, book_id, chapter, text in list(ranked)[:top]:
        if len(out) >= limit:
            break
        before = preceding_chunk(conn, chunk_id)
        if before is None or before[0] in taken:
            continue
        run_start = next_chunk_id(conn, chunk_id) in held
        if not (mid_flow(text) or run_start):
            continue
        if before[1] != book_id or before[2] != chapter:
            continue  # never across a chapter (or book)
        if not visible_module.sees(visible, before[1], before[2]):
            continue
        if is_boilerplate is not None and is_boilerplate(before[3]):
            continue
        out.append((before, chunk_id))
        taken.add(before[0])
    return out
