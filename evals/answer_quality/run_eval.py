"""Run the dev (or held-out) questions through lore_search and record every answer.

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
SETS = {"dev": HERE / "questions.json", "heldout": HERE / "heldout.json",
        "arc": HERE / "arc.json"}
RUNS = HERE / "runs"


def _done(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {json.loads(line)["id"] for line in path.read_text().splitlines()
            if line.strip()}


def _real_models(config):
    from lore.inference.gemini import default_key_file, resolve_api_key
    from lore.orchestrator.gemini_budget import recorder_for
    from lore.query.agent import make_query_models

    return make_query_models(
        config, api_key=resolve_api_key(key_file=default_key_file()),
        recorder=recorder_for(config.sqlite_path))


def _traced(models, sink: list):
    """The same models, with every generate call's prompt and reply kept.

    For diagnosis: an answer records only the claims that survived, so
    whether the model proposed few claims or code dropped them (quote not
    found, near-duplicate, faithfulness check) is otherwise invisible.
    """
    import dataclasses

    def wrap(kind, generate):
        if generate is None:
            return None

        async def traced(prompt, schema):
            reply = await generate(prompt, schema)
            sink.append({"kind": kind, "prompt": prompt,
                         "reply": reply.model_dump(mode="json")})
            return reply
        return traced

    return dataclasses.replace(
        models, select=wrap("select", models.select),
        synthesize=wrap("synthesize", models.synthesize),
        decompose=wrap("decompose", models.decompose),
        check=wrap("check", models.check),
        write=wrap("write", models.write))


def _evidence_only_models(config):
    """The real query models with synthesis and the faithfulness check stubbed.

    Decomposition and template selection still run (they decide the
    evidence), but no answer is written: about half the Gemini requests,
    and a deterministic target -- whether the gold chunks reached the
    evidence (retrieval/recall.py) -- instead of a noisy graded answer.
    """
    import dataclasses

    from lore.query.synthesize import Synthesis

    models = _real_models(config)

    async def no_answer(prompt, schema):
        return Synthesis(claims=[], can_answer=False)

    return dataclasses.replace(models, synthesize=no_answer, check=None)


def _retrieval(candidate_k: int | None, keyword_k: int | None) -> dict:
    """Override the agent's retrieval settings for this run, and return the
    settings in force, so a comparison runs every arm from the same code."""
    from lore.query import agent
    if candidate_k is not None:
        agent.CANDIDATE_K = candidate_k
    if keyword_k is not None:
        agent.KEYWORD_K = keyword_k
    return {"candidate_k": agent.CANDIDATE_K, "keyword_k": agent.KEYWORD_K}


async def run(label: str, only: list[str] | None, qset: str,
              evidence_only: bool = False, candidate_k: int | None = None,
              keyword_k: int | None = None,
              gemini_model: str | None = None, trace: bool = False) -> None:
    config = LoreConfig.load(ROOT / "lore.toml")
    if gemini_model:
        # One model for selection and synthesis, no cascade: arms compared
        # on different models differ by the model, not the change.
        config = config.model_copy(update={
            step: getattr(config, step).model_copy(update={"models": [gemini_model]})
            for step in ("selection", "synthesis")})
    retrieval = _retrieval(candidate_k, keyword_k)
    print(f"retrieval: {retrieval}", flush=True)
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

        calls: list[dict] = []

        async def ask():
            calls.clear()
            if evidence_only:
                models = _evidence_only_models(config)
            elif trace:
                models = _real_models(config)
            else:
                return await tools.lore_search(config, q["question"])
            if trace:
                models = _traced(models, calls)
            return await tools.lore_search(config, q["question"], models=models)

        result = await ask()
        # A transient Gemini refusal (overload 503, a cooling model) is not a
        # quality signal: in the noise-floor run one cost a whole question's
        # facts. Retry it, after a pause, before recording.
        for attempt in range(2):
            err = result.get("error")
            if not err or err["code"] != "upstream_unavailable":
                break
            print(f"{q['id']}: upstream_unavailable, retrying in 60s", flush=True)
            await asyncio.sleep(60)
            result = await ask()
        record = {"id": q["id"], "question": q["question"],
                  "seconds": round(time.monotonic() - started, 1),
                  "retrieval": retrieval, **result}
        with out.open("a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        if trace:
            with (RUNS / f"{label}.trace.jsonl").open("a") as f:
                f.write(json.dumps({"id": q["id"], "calls": calls},
                                   ensure_ascii=False) + "\n")
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
                        help="dev (56, used to decide changes), heldout (16,"
                             " milestones only) or arc (16, timeline/arc"
                             " questions, never tuned on -- see GRADING.md)")
    parser.add_argument("--evidence-only", action="store_true",
                        help="stop before writing answers; record only the"
                             " evidence (for retrieval/recall.py --run)")
    parser.add_argument("--candidate-k", type=int,
                        help="vector candidates per sub-question (default:"
                             " the agent's CANDIDATE_K)")
    parser.add_argument("--keyword-k", type=int,
                        help="keyword candidates per sub-question, 0 = off"
                             " (default: the agent's KEYWORD_K)")
    parser.add_argument("--gemini-model",
                        help="pin selection and synthesis to this one model"
                             " (default: the configured cascade)")
    parser.add_argument("--trace", action="store_true",
                        help="also keep every Gemini prompt and reply, in"
                             " runs/<label>.trace.jsonl")
    parser.add_argument("--only", nargs="*",
                        help="question ids to run (default: the whole set)")
    args = parser.parse_args()
    asyncio.run(run(args.label, args.only, args.qset, args.evidence_only,
                    args.candidate_k, args.keyword_k, args.gemini_model,
                    args.trace))


if __name__ == "__main__":
    main()
