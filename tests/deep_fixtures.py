"""The synthetic worlds library (tests/worlds_fixtures.py) with more in it: a
place, a faction and an idea beside the two characters, and codex entries, all
on real SQLite, Kuzu and LanceDB. Used by the deep-answer tests and by the
golden that pins DEEP off."""
from __future__ import annotations

from pathlib import Path

from lore.codex import store as codex_store
from lore.codex.store import CodexEntry, Statement
from lore.db.kuzu_store import KuzuStore
from lore.db.lance_store import LanceStore
from lore.db.sqlite import open_db
from lore.worlds import list_worlds
from tests.worlds_fixtures import ALPHA, MARK, QUESTION_VECTOR, build_library

# (entity id, name, kind, books)
MORE = [("a_gate", "Iron Gate", "Place", ["ba1", "ba2"]),
        ("a_guild", "Ledger Guild", "Faction", ["ba1", "ba2"]),
        ("a_road", "Long Road", "Concept", ["ba1", "ba2"])]
# (subject, object, book, chunk, chapter): every one involves a_zed
MORE_EDGES = [("a_zed", "a_gate", "ba1", "ba1:1:0", 1),
              ("a_zed", "a_gate", "ba1", "ba1:2:0", 2),
              ("a_zed", "a_guild", "ba1", "ba1:2:0", 2),
              ("a_guild", "a_gate", "ba2", "ba2:1:0", 1),
              ("a_zed", "a_road", "ba2", "ba2:2:0", 2),
              ("a_yan", "a_road", "ba2", "ba2:2:0", 2)]
QUOTE_A = "walked on with Yan Vale"
QUOTE_B = "the traveller Zed Quill"
QUOTE_C = "through the"


def add_world_content(config) -> None:
    """Three more entities (and edges) in Alpha Saga."""
    graph = KuzuStore(config.kuzu_dir / "graph")
    for entity_id, name, kind, books in MORE:
        graph.execute("CREATE (:Entity {id: $id, name: $n, kind: $k, books: $b,"
                      " aliases: []})", {"id": entity_id, "n": name, "k": kind, "b": books})
    for subject, obj, book_id, chunk_id, chapter in MORE_EDGES:
        graph.execute(
            "MATCH (s:Entity {id: $s}), (o:Entity {id: $o}) CREATE (s)-[:REL {predicate:"
            " 'INTERACTS_WITH', predicate_raw: 'reached', evidence: $ev, confidence: 0.9,"
            " chunk_id: $c, book_id: $b, chapter_seq: $ch, run_id: 'r'}]->(o)",
            {"s": subject, "o": obj, "ev": f"{MARK[book_id]} {subject} reached {obj}",
             "c": chunk_id, "b": book_id, "ch": chapter})
    graph.close()


def add_codex(config, entity_ids=("a_zed", "a_gate")) -> dict[str, list[str]]:
    """A codex entry for each of `entity_ids` (those the fixture knows), with
    its statements also in the Lance codex table. Returns statement ids."""
    conn = open_db(config.sqlite_path)
    world = next(w.id for w in list_worlds(conn) if w.name == ALPHA)
    spec = {
        "a_zed": ("Zed Quill", "Character", "A traveller who walks the roads.", [
            ("nature", "Zed is a traveller.", QUOTE_B, "ba1:1:0"),
            ("history", "Zed walked on with Yan.", QUOTE_A, "ba1:2:0"),
            ("role", "Zed crosses the hills.", QUOTE_C, "ba2:1:0"),
            ("change", "Zed walked on again later.", QUOTE_A, "ba2:2:0")]),
        "a_gate": ("Iron Gate", "Place", "A gate on the road.", [
            ("nature", "The gate stands on the road.", QUOTE_C, "ba1:1:0"),
            ("history", "The gate was passed by travellers.", QUOTE_B, "ba2:1:0")]),
        "a_guild": ("Ledger Guild", "Faction", "A guild.", [
            ("nature", "The guild keeps ledgers.", QUOTE_C, "ba1:2:0")])}
    lance = LanceStore(config.lancedb_dir)
    out: dict[str, list[str]] = {}
    for entity_id in entity_ids:
        name, kind, summary, items = spec[entity_id]
        entry = CodexEntry(
            entity_id=entity_id, world_id=world, name=name, kind=kind, summary=summary,
            prompt_hash="p", evidence_hash="v",
            statements=tuple(Statement(aspect=a, text=t, quote=q, chunk_id=c)
                             for a, t, q, c in items))
        ids = codex_store.save_entry(conn, entry)
        lance.upsert_codex([{"id": i, "vector": QUESTION_VECTOR} for i in ids])
        out[entity_id] = ids
    conn.close()
    return out


def build_deep_library(tmp_path: Path, *, codex: bool = True, **kw):
    config = build_library(tmp_path, **kw)
    add_world_content(config)
    if codex:
        add_codex(config)
    return config


# --- scripted models ------------------------------------------------------------

import re  # noqa: E402

from lore.query.agent import QueryModels  # noqa: E402
from lore.query.decompose import Decomposition, PartSpec  # noqa: E402
from lore.query.select import TemplateChoice  # noqa: E402
from lore.query.synthesize import Faithfulness, Judgement, Synthesis  # noqa: E402
from lore.query.write import Prose, Sentence  # noqa: E402

QUESTION = "Tell me about Zed Quill and the road."


class Scripted:
    """Scripted QueryModels for the deep tests: every call is recorded, every
    claims call quotes the first passage it is shown, and the check supports
    everything it is shown unless `reject` names text in a sentence or claim.

    `planner` is the reply to the planner call (a callable gets the prompt),
    `fail` is a set of substrings: a claims or writer prompt containing one
    raises."""

    def __init__(self, decomposition=None, *, planner=None, fail=(), reject=(),
                 write_kinds=False, tagged=False):
        self.decomposition = decomposition or Decomposition(
            sub_questions=[PartSpec(question=QUESTION)], depth="focused")
        self.planner = planner
        self.fail = tuple(fail)
        self.reject = tuple(reject)
        self.calls: list[tuple[str, str]] = []   # (kind, prompt)
        self.write_kinds = write_kinds
        # Each written sentence starts with its section's question, so a test can
        # tell which section wrote (and rejects) what.
        self.tagged = tagged

    def prompts(self, kind):
        return [p for k, p in self.calls if k == kind]

    async def embed(self, texts):
        return [QUESTION_VECTOR for _ in texts]

    async def decompose(self, prompt, schema):
        return self.decomposition

    async def select(self, prompt, schema):
        return TemplateChoice(template="neighbours", entities=[1])

    async def generate(self, prompt, schema):
        try:
            from lore.query.deep import DeepPlan
        except ImportError:  # the golden was written before the module existed
            DeepPlan = None
        if schema is DeepPlan:
            self.calls.append(("plan", prompt))
            return self.planner(prompt) if callable(self.planner) else self.planner
        if schema is Synthesis:
            self.calls.append(("claims", prompt))
            if any(f in prompt for f in self.fail):
                raise RuntimeError("claims call failed")
            numbers = [int(n) for n in re.findall(r"^\[(\d+)\]", prompt, re.M)]
            quotes = [QUOTE_A, QUOTE_B, QUOTE_C]
            claims = []
            for i, n in enumerate(numbers[:3]):
                text = _passage_text(prompt, n)
                quote = next((q for q in quotes if q in text), None)
                if quote:
                    claims.append({"text": f"Zed Quill: {quote} (fact {i}).", "passage": n,
                                   "quote": quote})
            return Synthesis(can_answer=True, claims=claims)
        if "sentences" in schema.model_fields:
            self.calls.append(("write", prompt))
            if any(f in prompt for f in self.fail):
                raise RuntimeError("writer failed")
            count = len(re.findall(r"^\d+\. ", prompt.split("Facts:")[-1], re.M))
            tag = (re.search(r"^Question: (.*)$", prompt, re.M).group(1)[:40] + " "
                   if self.tagged else "")
            sentences = [Sentence(text=f"{tag}sentence {i + 1}." if tag else f"Sentence {i + 1}.",
                                  claims=[i + 1]) for i in range(count)]
            if self.write_kinds and count >= 2:
                sentences.append(Sentence(text=f"{tag}the evidence suggests a pattern.",
                                          claims=[1, count], kind="reading"))
            return Prose(sentences=sentences)
        self.calls.append(("check", prompt))
        n_claims = prompt.count("\n   cites passage")
        sentence_lines = re.findall(r"^\d+\. sentence(?: \(\w+\))?: (.*)$", prompt, re.M)
        return Faithfulness(
            judgements=[Judgement(claim=i, verdict="supported") for i in range(1, n_claims + 1)],
            sentence_judgements=[
                Judgement(claim=i, verdict=("not_supported" if any(r in line for r in self.reject)
                                            else "supported"))
                for i, line in enumerate(sentence_lines, start=1)])

    def models(self, **kw):
        async def aclose():
            return None
        return QueryModels(self.embed, self.select, self.generate, lambda: "writer", aclose,
                           lambda: "chooser", decompose=self.decompose, check=self.generate,
                           write=self.generate, **kw)


def _passage_text(prompt: str, n: int) -> str:
    match = re.search(rf"^\[{n}\][^\n]*\n(.*?)(?:\n\n\[|\Z)", prompt, re.M | re.S)
    return match.group(1) if match else ""


async def run_scenario(config, scripted, question=QUESTION, **kw):
    """(answer, events) for `question` in Alpha Saga."""
    from lore.mcp.stores import open_stores
    from lore.query.agent import answer_question
    events: list[dict] = []
    with open_stores(config) as s:
        answer = await answer_question(s.conn, s.kuzu, s.lance, question, scripted.models(),
                                       world=ALPHA, on_event=events.append, **kw)
    return answer, events


def snapshot(answer, events, scripted) -> dict:
    """Everything the golden pins: events, prompts and the answer."""
    from lore.query.events import json_safe
    return json_safe({
        "events": events,
        "prompts": [list(c) for c in scripted.calls],
        "answer": {"text": answer.text, "mode": answer.mode, "note": answer.note,
                   "sub_questions": list(answer.sub_questions), "style": answer.answer_style,
                   "claims": [c.model_dump() for c in answer.claims], "passages": [p.chunk_id for p in answer.passages],
                   "prose": answer.prose}})
