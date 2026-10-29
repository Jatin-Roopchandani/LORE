"""Answer every question with two arms, paired: same question, same model.

    .venv/bin/python evals/answer_quality/run_pair.py --label r4 \\
        --arm control --arm candidate_k=20

Round 3 compared arms run back to back, and the dev set landed on one
Gemini model and the held-out set on another: a model difference passed
for a change's effect. Here each question is asked of both arms one after
the other on ONE model, and the model alternates between questions
(MODELS), so each arm sees each model equally often and both models'
daily caps are spent evenly. Which arm goes first alternates too.

An arm is `control` (the code as committed) or comma-separated overrides
of module settings, `name=value`, from ARM_SETTINGS -- e.g.
`candidate_k=20,keyword_k=20`. Each answer goes to
runs/<label>.<arm>.jsonl in run_eval.py's format, so recall.py and the
grading packets read them unchanged. Resumable per arm.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE))

from run_eval import RUNS, SETS, _done, _real_models, _traced  # noqa: E402

from lore.config import LoreConfig  # noqa: E402
from lore.mcp import tools  # noqa: E402

MODELS = ("gemini-3.1-flash-lite", "gemini-3.5-flash-lite")
# What an arm may override: name -> (module, attribute, type).
ARM_SETTINGS = {
    "candidate_k": ("lore.query.agent", "CANDIDATE_K", int),
    "keyword_k": ("lore.query.agent", "KEYWORD_K", int),
    "evidence_chars": ("lore.query.agent", "GEMINI_EVIDENCE_CHARS", int),
    "max_claims": ("lore.query.synthesize", "MAX_CLAIMS", int),
    "no_restate": ("lore.query.synthesize", "NO_RESTATE",
                   lambda v: v.lower() in ("1", "true", "yes", "on")),
    "attribute_speech": ("lore.query.synthesize", "ATTRIBUTE_SPEECH",
                         lambda v: v.lower() in ("1", "true", "yes", "on")),
    "strict_links": ("lore.query.synthesize", "STRICT_LINKS",
                     lambda v: v.lower() in ("1", "true", "yes", "on")),
    "thematic_parts": ("lore.query.decompose", "THEMATIC_PARTS",
                       lambda v: v.lower() in ("1", "true", "yes", "on")),
    "role_coverage": ("lore.query.synthesize", "ROLE_COVERAGE",
                      lambda v: v.lower() in ("1", "true", "yes", "on")),
    "full_answers": ("lore.query.synthesize", "FULL_ANSWERS",
                     lambda v: v.lower() in ("1", "true", "yes", "on")),
    "prose_answers": ("lore.query.write", "PROSE_ANSWERS",
                      lambda v: v.lower() in ("1", "true", "yes", "on")),
    # Story order, own words and gaps in the writer and the check.
    "story_order": ("lore.query.write", "STORY_ORDER",
                    lambda v: v.lower() in ("1", "true", "yes", "on")),
    # Labelled, checked readings of what actions show (write.READINGS).
    "readings": ("lore.query.write", "READINGS",
                 lambda v: v.lower() in ("1", "true", "yes", "on")),
    # The story timeline of the question's entities (lore/query/timeline.py).
    "timeline": ("lore.query.timeline", "TIMELINE",
                 lambda v: v.lower() in ("1", "true", "yes", "on")),
}


def parse_arm(spec: str) -> dict[tuple[str, str], object]:
    if spec == "control":
        return {}
    out = {}
    for item in spec.split(","):
        name, _, value = item.partition("=")
        if name not in ARM_SETTINGS:
            raise SystemExit(f"unknown arm setting {name!r}; known: {sorted(ARM_SETTINGS)}")
        module, attr, kind = ARM_SETTINGS[name]
        out[(module, attr)] = kind(value)
    return out


class _Applied:
    """Set an arm's overrides for the duration of one question."""

    def __init__(self, overrides):
        self._overrides, self._saved = overrides, {}

    def __enter__(self):
        import importlib
        for (module, attr), value in self._overrides.items():
            mod = importlib.import_module(module)
            self._saved[(mod, attr)] = getattr(mod, attr)
            setattr(mod, attr, value)

    def __exit__(self, *exc):
        for (mod, attr), value in self._saved.items():
            setattr(mod, attr, value)


def _pinned(config, model: str):
    return config.model_copy(update={
        step: getattr(config, step).model_copy(update={"models": [model]})
        for step in ("selection", "synthesis")})


async def ask(config, question: str, overrides, trace: list | None):
    with _Applied(overrides):
        models = _real_models(config)
        if trace is not None:
            models = _traced(models, trace)
        result = await tools.lore_search(config, question, models=models)
        for _ in range(2):  # transient refusals are not a quality signal
            err = result.get("error")
            if not err or err["code"] != "upstream_unavailable":
                break
            await asyncio.sleep(60)
            models = _real_models(config)
            if trace is not None:
                trace.clear()
                models = _traced(models, trace)
            result = await tools.lore_search(config, question, models=models)
    return result


async def run(label: str, arms: list[str], qset: str, only, trace: bool) -> None:
    base = LoreConfig.load(HERE.parents[1] / "lore.toml")
    questions = json.loads(SETS[qset].read_text())
    if only:
        questions = [q for q in questions if q["id"] in only]
    RUNS.mkdir(exist_ok=True)
    names = [a.replace("=", "").replace(",", "+") for a in arms]
    overrides = [parse_arm(a) for a in arms]
    outs = [RUNS / f"{label}.{n}.jsonl" for n in names]
    done = [_done(o) for o in outs]
    for i, q in enumerate(questions):
        model = MODELS[i % len(MODELS)]
        config = _pinned(base, model)
        order = [0, 1] if (i // len(MODELS)) % 2 == 0 else [1, 0]
        for a in order:
            if q["id"] in done[a]:
                continue
            calls: list[dict] | None = [] if trace else None
            started = time.monotonic()
            result = await ask(config, q["question"], overrides[a], calls)
            record = {"id": q["id"], "question": q["question"], "arm": arms[a],
                      "pinned_model": model,
                      "seconds": round(time.monotonic() - started, 1), **result}
            with outs[a].open("a") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            if trace:
                with outs[a].with_suffix(".trace.jsonl").open("a") as f:
                    f.write(json.dumps({"id": q["id"], "calls": calls},
                                       ensure_ascii=False) + "\n")
            err = result.get("error")
            print(f"{q['id']} [{names[a]}] {model}: "
                  f"{err['code'] if err else result.get('mode')}"
                  f" | {len(result.get('claims', []))} claims"
                  f" | {record['seconds']}s", flush=True)
            if err and err["code"] == "budget_exhausted":
                print("Gemini daily budget spent; stopping. Re-run tomorrow to resume.")
                return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--label", required=True)
    parser.add_argument("--arm", action="append", required=True,
                        help="control, or overrides like candidate_k=20 (twice)")
    parser.add_argument("--set", dest="qset", choices=sorted(SETS), default="dev")
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    if len(args.arm) != 2:
        raise SystemExit("give exactly two --arm")
    asyncio.run(run(args.label, args.arm, args.qset, args.only, args.trace))


if __name__ == "__main__":
    main()
