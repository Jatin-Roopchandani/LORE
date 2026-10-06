"""Run the 26 acceptance questions through lore_search and record every answer.

    .venv/bin/python evals/answer_quality/run_eval.py --label baseline
    .venv/bin/python evals/answer_quality/run_eval.py --set heldout --label heldout-baseline

Writes one JSON line per question to evals/answer_quality/runs/<label>.jsonl
(gitignored). Resumable: a re-run with the same label skips questions
already recorded, because long runs here get interrupted by session breaks.

Each question spends roughly six Gemini requests (decompose, select,
synthesise per part, check), all recorded in the shared daily ledger, and
embeds through Ollama -- which must be running, CPU-only on this machine:

    CUDA_VISIBLE_DEVICES=-1 OLLAMA_VULKAN=0 ~/.local/ollama/bin/ollama serve

Answers are graded separately, blind, against the verified answers in
questions.json (copied from the Phase 3 specs). This script only collects.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lore.config import LoreConfig  # noqa: E402
from lore.mcp import tools  # noqa: E402

HERE = Path(__file__).resolve().parent
SETS = {"dev": HERE / "questions.json", "heldout": HERE / "heldout.json"}
RUNS = HERE / "runs"


def _done(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {json.loads(line)["id"] for line in path.read_text().splitlines()
            if line.strip()}


async def run(label: str, only: list[str] | None, qset: str) -> None:
    config = LoreConfig.load(ROOT / "lore.toml")
    questions = json.loads(SETS[qset].read_text())
    if only:
        questions = [q for q in questions if q["id"] in only]
    RUNS.mkdir(exist_ok=True)
    out = RUNS / f"{label}.jsonl"
    done = _done(out)
    for q in questions:
        if q["id"] in done:
            print(f"{q['id']}: already recorded, skipped", flush=True)
            continue
        started = time.monotonic()
        result = await tools.lore_search(config, q["question"])
        # A transient Gemini refusal (overload 503, a cooling model) is not a
        # quality signal: in the noise-floor run one cost a whole question's
        # facts. Retry it, after a pause, before recording.
        for attempt in range(2):
            err = result.get("error")
            if not err or err["code"] != "upstream_unavailable":
                break
            print(f"{q['id']}: upstream_unavailable, retrying in 60s", flush=True)
            await asyncio.sleep(60)
            result = await tools.lore_search(config, q["question"])
        record = {"id": q["id"], "question": q["question"],
                  "seconds": round(time.monotonic() - started, 1), **result}
        with out.open("a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        err = result.get("error")
        print(f"{q['id']}: {err['code'] if err else result.get('mode')}"
              f" | {len(result.get('claims', []))} claims"
              f" | {record['seconds']}s", flush=True)
        if err and err["code"] == "budget_exhausted":
            print("Gemini daily budget spent; stopping. Re-run tomorrow to resume.")
            return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--label", required=True,
                        help="run name, e.g. baseline or round-1")
    parser.add_argument("--set", dest="qset", choices=sorted(SETS),
                        default="dev",
                        help="dev (26, used to decide changes) or heldout (16,"
                             " milestones only -- see GRADING.md)")
    parser.add_argument("--only", nargs="*",
                        help="question ids to run (default: all 26)")
    args = parser.parse_args()
    asyncio.run(run(args.label, args.only, args.qset))


if __name__ == "__main__":
    main()
