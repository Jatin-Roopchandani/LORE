"""Vector-retrieval recall for the dev set's gold chunks. Free: local Ollama
embeddings + LanceDB, no Gemini. Read-only on the library.

For each key fact's gold chunk(s): its best rank in the top-100 vector
results for the question and for each of the baseline's sub-questions
(embedded exactly as the agent does, QUERY_PREFIX once), and whether the
baseline's final evidence actually contained it.
"""
import asyncio, json, sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lore.config import LoreConfig
from lore.db.lance_store import LanceStore
from lore.query.agent import make_query_embedder
from lore.query.seed import embed_question

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
TOP = 100


async def main():
    cfg = LoreConfig.load(ROOT / "lore.toml")
    gold = json.loads((HERE / "gold.json").read_text())
    base = {json.loads(l)["id"]: json.loads(l)
            for l in open(ROOT / "evals/answer_quality/runs/baseline.jsonl")}
    lance = LanceStore(cfg.lancedb_dir)
    emb = make_query_embedder(cfg)
    ranks_cache: dict[str, dict[str, int]] = {}
    try:
        async def ranks_for(text):
            if text not in ranks_cache:
                vec = await embed_question(text, emb.embed)
                rows = lance.search_chunks(vec, limit=TOP)
                ranks_cache[text] = {r["chunk_id"]: i + 1 for i, r in enumerate(rows)}
            return ranks_cache[text]

        out = []
        for g in gold:
            if not g.get("chunks"):
                continue
            r = base[g["id"]]
            queries = [r["question"], *r.get("sub_questions", [])]
            best = None
            for q in queries:
                rk = await ranks_for(q)
                for c in g["chunks"]:
                    if c in rk and (best is None or rk[c] < best):
                        best = rk[c]
            in_evidence = any(p["chunk_id"] in g["chunks"] for p in r.get("passages", []))
            out.append({"id": g["id"], "fact": g["fact"][:70], "best_rank": best,
                        "in_baseline_evidence": in_evidence})
    finally:
        await emb.aclose()

    (ROOT / "evals/answer_quality/runs/recall.json").write_text(json.dumps(out, indent=1))
    n = len(out)
    print(f"{n} located facts")
    for k in (5, 10, 20, 30, 50, 100):
        hit = sum(1 for o in out if o["best_rank"] and o["best_rank"] <= k)
        print(f"  vector recall@{k:<3}: {hit}/{n} ({100*hit//n}%)")
    ev = sum(o["in_baseline_evidence"] for o in out)
    print(f"  gold chunk in the baseline's actual evidence: {ev}/{n} ({100*ev//n}%)")
    miss = [o for o in out if not o["in_baseline_evidence"]]
    print(f"\nnot in evidence ({len(miss)}), best vector rank:")
    for o in miss:
        print(f"  {o['id']:4s} rank {str(o['best_rank']):>4}  {o['fact']}")


asyncio.run(main())
