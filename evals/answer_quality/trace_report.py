"""Where do claims go? A free report over a run_eval/run_pair --trace file.

    .venv/bin/python evals/answer_quality/trace_report.py --run trace-dev

For every synthesis call: claims proposed, cut by MAX_CLAIMS, dropped by
the quote check (and why), then for the answer: lost to the near-duplicate
collapse and to the faithfulness check. Replays the real verify_claims on
the passages the call was shown (parsed from its prompt), so the numbers
are what the pipeline did, not an estimate.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from lore.query.models import Claim, Passage  # noqa: E402
from lore.query.synthesize import MAX_CLAIMS, Synthesis, verify_claims  # noqa: E402

_HEADER = re.compile(r"^\[(\d+)\] \(", re.M)


def shown(prompt: str, passages: dict[int, Passage]) -> list[Passage]:
    return [passages[int(n)] for n in _HEADER.findall(prompt) if int(n) in passages]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", required=True, help="label, or label.arm for run_pair")
    args = parser.parse_args()
    runs = HERE / "runs"
    answers = {json.loads(l)["id"]: json.loads(l)
               for l in (runs / f"{args.run}.jsonl").read_text().splitlines() if l.strip()}
    traces = [json.loads(l) for l in (runs / f"{args.run}.trace.jsonl").read_text().splitlines()
              if l.strip()]

    total = Counter()
    reasons = Counter()
    rows = []
    for t in traces:
        a = answers[t["id"]]
        passages = {p["n"]: Passage(n=p["n"], chunk_id=p["chunk_id"], book_id=p.get("book_id", ""),
                                    chapter_seq=p.get("chapter_seq", 0), text=p["text"])
                    for p in a.get("passages", [])}
        q = Counter()
        for call in t["calls"]:
            if call["kind"] == "check":
                verdicts = [j.get("verdict") for j in call["reply"].get("judgements", [])]
                q["check_dropped"] += sum(v == "not_supported" for v in verdicts)
                continue
            if call["kind"] != "synthesize":
                continue
            reply = Synthesis.model_validate(call["reply"])
            q["calls"] += 1
            q["proposed"] += len(reply.claims)
            q["cap_cut"] += max(0, len(reply.claims) - MAX_CLAIMS)
            q["said_cannot"] += not reply.can_answer
            capped = reply.model_copy(update={"claims": reply.claims[:MAX_CLAIMS]})
            kept, dropped = verify_claims(capped, shown(call["prompt"], passages))
            q["verified"] += len(kept)
            for _, why in dropped:
                reasons[why] += 1
            q["quote_dropped"] += len(dropped)
        q["final"] = len(a.get("claims", []))
        q["collapsed"] = max(0, q["verified"] - q["check_dropped"] - q["final"])
        total.update(q)
        rows.append((t["id"], q))

    print(f"{len(rows)} answers, {total['calls']} synthesis calls")
    print(f"  proposed {total['proposed']}  (cut by MAX_CLAIMS {total['cap_cut']})")
    print(f"  quote check dropped {total['quote_dropped']}: {dict(reasons)}")
    print(f"  verified {total['verified']}")
    print(f"  near-duplicate collapse removed ~{total['collapsed']}")
    print(f"  faithfulness check dropped {total['check_dropped']}")
    print(f"  final claims {total['final']}  ({total['final'] / max(1, len(rows)):.1f} per answer)")
    print(f"  synthesis calls answering can_answer=false: {total['said_cannot']}")
    print("\n id    calls proposed quote-drop verified collapsed check-drop final")
    for i, q in rows:
        print(f" {i:5s} {q['calls']:5d} {q['proposed']:8d} {q['quote_dropped']:10d} {q['verified']:8d}"
              f" {q['collapsed']:9d} {q['check_dropped']:10d} {q['final']:5d}")


if __name__ == "__main__":
    main()
