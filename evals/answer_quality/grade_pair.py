"""Blind packet for a paired run, and its score once graded.

    .venv/bin/python evals/answer_quality/grade_pair.py packet --run r4 \\
        --arms control full_answerstrue --set dev --out <dir>
    .venv/bin/python evals/answer_quality/grade_pair.py score --out <dir>

`packet` writes <dir>/packet.json (each question's two answers as A and B,
order shuffled per question) and <dir>/sealed/key.json. The grader is given
packet.json, GRADING.md and the question set, never the sealed directory,
and writes <dir>/grades.json:
    [{"id", "A": {"facts_covered", "facts_total", "false_claims",
                  "duplicates", "verdict", ...}, "B": {...}}, ...]

`score` unseals and reports, per arm, key facts, false claims, duplicates
and verdicts, plus the paired view the decision rests on: per question,
which arm covered more facts, and a two-sided sign test over the questions
that differ. A gain is real at p < 0.05, not on totals alone.
"""
from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from math import comb
from pathlib import Path

HERE = Path(__file__).resolve().parent
SETS = {"dev": HERE / "questions.json", "heldout": HERE / "heldout.json"}


def _answers(path: Path) -> dict[str, dict]:
    return {json.loads(l)["id"]: json.loads(l)
            for l in path.read_text().splitlines() if l.strip()}


def packet(run: str, arms: list[str], qset: str, out: Path, seed: int,
           answer_only: bool = False) -> None:
    runs = [_answers(HERE / "runs" / f"{run}.{a}.jsonl") for a in arms]
    rng = random.Random(seed)
    items, key = [], {}
    for q in json.loads(SETS[qset].read_text()):
        if not all(q["id"] in r for r in runs):
            continue
        order = [0, 1]
        rng.shuffle(order)
        key[q["id"]] = {"A": arms[order[0]], "B": arms[order[1]]}
        item = {"id": q["id"], "question": q["question"]}
        for label, a in zip("AB", order):
            r = runs[a][q["id"]]
            if answer_only:
                item[label] = {"answer": r.get("answer")}
            else:
                item[label] = {"answer": r.get("answer"),
                               "claims": [{"text": c["text"], "quote": c.get("quote")}
                                          for c in r.get("claims", [])]}
        items.append(item)
    (out / "sealed").mkdir(parents=True, exist_ok=True)
    (out / "packet.json").write_text(json.dumps(items, indent=1, ensure_ascii=False))
    (out / "sealed" / "key.json").write_text(json.dumps(key, indent=1))
    print(f"{len(items)} questions -> {out / 'packet.json'}")


def sign_test(wins: int, losses: int) -> float:
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    return min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n)


def score(out: Path) -> None:
    key = json.loads((out / "sealed" / "key.json").read_text())
    grades = json.loads((out / "grades.json").read_text())
    arms = sorted({a for k in key.values() for a in k.values()})
    totals = {a: Counter() for a in arms}
    per_q = []
    for g in grades:
        by_arm = {key[g["id"]][label]: g[label] for label in "AB"}
        for a, x in by_arm.items():
            t = totals[a]
            t["facts"] += x["facts_covered"]
            t["of"] += x["facts_total"]
            t["false"] += x["false_claims"]
            t["dup"] += x["duplicates"]
            t[x["verdict"]] += 1
        per_q.append((g["id"], by_arm[arms[0]]["facts_covered"],
                      by_arm[arms[1]]["facts_covered"]))
    for a in arms:
        t = totals[a]
        print(f"{a:24s} facts {t['facts']}/{t['of']}  false {t['false']}  dup {t['dup']}  "
              f"correct/partly/refused/wrong {t['correct']}/{t['partly']}/"
              f"{t['refused']}/{t['wrong']}")
    prefs = Counter()
    for g in grades:
        p = g.get("preferred")
        if p in ("A", "B"):
            prefs[key[g["id"]][p]] += 1
        elif p == "tie":
            prefs["tie"] += 1
    if prefs:
        w, l = prefs[arms[1]], prefs[arms[0]]
        print(f"readability: {arms[1]} preferred {w}, {arms[0]} preferred {l}, tie {prefs['tie']};"
              f" sign test p = {sign_test(w, l):.3f}")
    wins = sum(b > a for _, a, b in per_q)
    losses = sum(b < a for _, a, b in per_q)
    print(f"\n{arms[1]} vs {arms[0]}, per question: better {wins}, worse {losses}, "
          f"same {len(per_q) - wins - losses}; sign test p = {sign_test(wins, losses):.3f}")
    print("changed:", ", ".join(f"{i} {a}->{b}" for i, a, b in per_q if a != b))


def styles(run: str, arms: list[str]) -> None:
    for a in arms:
        recs = list(_answers(HERE / "runs" / f"{run}.{a}.jsonl").values())
        c = Counter(r.get("answer_style", "claims") for r in recs)
        eligible = sum(1 for r in recs if len(r.get("claims", [])) >= 2)
        fell = sum(1 for r in recs if len(r.get("claims", [])) >= 2 and r.get("answer_style") != "prose")
        rate = f"{100 * fell // eligible}%" if eligible else "n/a"
        print(f"{a}: prose {c['prose']}, claims {c['claims']}; fallback {rate} of {eligible} eligible")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("packet")
    p.add_argument("--run", required=True)
    p.add_argument("--arms", nargs=2, required=True,
                   help="the two arm names as run_pair.py wrote them")
    p.add_argument("--set", dest="qset", choices=sorted(SETS), default="dev")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--answer-only", action="store_true",
                   help="show graders only each answer text, not its claims")
    s = sub.add_parser("score")
    s.add_argument("--out", type=Path, required=True)
    st = sub.add_parser("styles")
    st.add_argument("--run", required=True)
    st.add_argument("--arms", nargs=2, required=True)
    args = parser.parse_args()
    if args.cmd == "packet":
        packet(args.run, args.arms, args.qset, args.out, args.seed, args.answer_only)
    elif args.cmd == "styles":
        styles(args.run, args.arms)
    else:
        score(args.out)


if __name__ == "__main__":
    main()
