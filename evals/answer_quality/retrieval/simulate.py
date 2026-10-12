"""Simulate evidence assembly offline (free: local Ollama + numpy, read-only).

Per dev question, using the baseline run's recorded sub-questions:
candidates = vector top-K for the question and each part, optional BM25
top-K per part, plus the baseline's own evidence chunks (a stand-in for the
graph's edge chunks). Each part ranks candidates by its score; the 36,000
char budget is filled round-robin across parts, as assemble_ranked does.
Reports how many of the 96 gold facts end up in the evidence.
"""
import asyncio, json, re, sqlite3, sys
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from lore.config import LoreConfig
from lore.db.lance_store import LanceStore
from lore.query.agent import make_query_embedder
from lore.query.seed import embed_question

STOP = set("""a an the and or but of to in on at by for from with as is are was were be been
has have had do does did this that these those it its he him his she her they them their
what who whom whose which when where why how each every one both into than then there
not no so novel novels book books""".split())
BUDGET = 36_000


async def main():
    cfg = LoreConfig.load(ROOT / "lore.toml")
    gold = [g for g in json.loads((ROOT / "evals/answer_quality/retrieval/gold.json").read_text()) if g.get("chunks")]
    base = {json.loads(l)["id"]: json.loads(l) for l in open(ROOT / "evals/answer_quality/runs/baseline.jsonl")}
    at = LanceStore(cfg.lancedb_dir)._db.open_table("chunks").to_arrow()
    ids = at.column("chunk_id").to_pylist()
    V = np.array(at.column("vector").to_pylist(), dtype=np.float32)
    sql = sqlite3.connect(f"file:{cfg.sqlite_path}?mode=ro", uri=True)
    text = dict(sql.execute("SELECT id, text FROM chunks"))
    mem = sqlite3.connect(":memory:")
    mem.execute("CREATE VIRTUAL TABLE c USING fts5(id UNINDEXED, text)")
    mem.executemany("INSERT INTO c VALUES (?, ?)", [(i, text[i]) for i in ids])
    emb = make_query_embedder(cfg)
    qv = {}

    async def cos(q):
        if q not in qv:
            v = np.array(await embed_question(q, emb.embed), dtype=np.float32)
            qv[q] = dict(zip(ids, (V @ v).tolist()))
        return qv[q]

    def bm(q):
        words = list(dict.fromkeys(w for w in re.findall(r"[a-z]+", q.lower()) if w not in STOP and len(w) > 2))
        if not words:
            return []
        return [r[0] for r in mem.execute("SELECT id FROM c WHERE c MATCH ? ORDER BY bm25(c)", (" OR ".join(words),))]

    def assemble(cands, lists):
        seen, kept, used, pos = set(), [], 0, [0] * len(lists)
        while True:
            moved = False
            for i, L in enumerate(lists):
                while pos[i] < len(L) and L[pos[i]] in seen:
                    pos[i] += 1
                if pos[i] < len(L):
                    c = L[pos[i]]; pos[i] += 1; moved = True
                    if kept and used + len(text[c]) > BUDGET:
                        return kept
                    seen.add(c); kept.append(c); used += len(text[c])
            if not moved:
                return kept

    configs = {
        "k5 cosine (today)": dict(k=5, bm=0, rank="cos"),
        "k30 cosine": dict(k=30, bm=0, rank="cos"),
        "k30 +bm25 cosine": dict(k=30, bm=30, rank="cos"),
        "k30 +bm25 rrf": dict(k=30, bm=30, rank="rrf"),
        "k20 +bm25 rrf": dict(k=20, bm=20, rank="rrf"),
    }
    results = {name: 0 for name in configs}
    hits = {name: set() for name in configs}
    try:
        for qid, r in base.items():
            parts = r.get("sub_questions") or [r["question"]]
            graph = [p["chunk_id"] for p in r.get("passages", [])]
            cs = {q: await cos(q) for q in [r["question"], *parts]}
            bms = {p: bm(p) for p in parts}
            vorder = {q: sorted(ids, key=lambda c: -cs[q][c]) for q in cs}
            for name, cf in configs.items():
                cands = set(graph)
                for q in cs:
                    cands |= set(vorder[q][:cf["k"]])
                for p in parts:
                    cands |= set(bms[p][:cf["bm"]])
                cands = [c for c in cands if c in text]
                lists = []
                for p in parts:
                    if cf["rank"] == "cos":
                        score = {c: cs[p][c] for c in cands}
                    else:
                        vr = {c: i + 1 for i, c in enumerate(sorted(cands, key=lambda c: -cs[p][c]))}
                        br = {c: i + 1 for i, c in enumerate(bms[p])}
                        score = {c: 1 / (60 + vr[c]) + (1 / (60 + br[c]) if c in br else 0) for c in cands}
                    lists.append(sorted(cands, key=lambda c: -score[c]))
                kept = set(assemble(cands, lists))
                for gi, g in enumerate(gold):
                    if g["id"] == qid and kept & set(g["chunks"]):
                        hits[name].add(gi)
    finally:
        await emb.aclose()
    n = len(gold)
    base_ev = sum(1 for g in gold if set(g["chunks"]) & {p["chunk_id"] for p in base[g["id"]].get("passages", [])})
    print(f"actual baseline evidence: {base_ev}/{n}")
    for name in configs:
        gained = len(hits[name] - hits["k5 cosine (today)"]); lost = len(hits["k5 cosine (today)"] - hits[name])
        print(f"{name:22s} {len(hits[name]):3d}/{n}   (+{gained} / -{lost} vs today)")


asyncio.run(main())
