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

A provider arm answers on another model provider: `provider=anthropic` (with
an optional `model=claude-opus-5-5`; Claude's default is claude-sonnet-5-5)
or `provider=openai,model=<id>`, with its key in ANTHROPIC_API_KEY /
OPENAI_API_KEY or ~/.config/lore/<provider>.env. It sets [lore.llm] for that
arm only, and may be combined with the module settings below. The pinned
Gemini model then applies to Gemini arms only.
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
    # Prose for plain factual answers too (write.PROSE_FACTUAL; off since round 7).
    "prose_factual": ("lore.query.write", "PROSE_FACTUAL",
                      lambda v: v.lower() in ("1", "true", "yes", "on")),
    # Every kept claim reaches the shown prose: a repair round, then code places
    # what is still uncited (write.COVERAGE).
    "coverage": ("lore.query.write", "COVERAGE",
                 lambda v: v.lower() in ("1", "true", "yes", "on")),
    # Readings only in labelled sentences: the fact rule, the check's
    # inference_in_fact and its repair (write.FACT_FIDELITY).
    "fact_fidelity": ("lore.query.write", "FACT_FIDELITY",
                      lambda v: v.lower() in ("1", "true", "yes", "on")),
    # A kept sentence that leans on one the check removed ("He ran ..."), or a
    # removed opening: one repair round, else the claim list (write.ORPHAN_REPAIR).
    "orphan_repair": ("lore.query.write", "ORPHAN_REPAIR",
                      lambda v: v.lower() in ("1", "true", "yes", "on")),
    # The writer writes for a reader: connected paragraphs, fuller sentences,
    # depth, no "The facts show", sentences that stand if the one before is cut,
    # and a section opening that names its subject (write.READER_PROSE).
    "reader_prose": ("lore.query.write", "READER_PROSE",
                     lambda v: v.lower() in ("1", "true", "yes", "on")),
    # The check judges an introduction or summary sentence against the union of
    # the claims it cites (synthesize.SUMMARY_CHECK).
    "summary_check": ("lore.query.synthesize", "SUMMARY_CHECK",
                      lambda v: v.lower() in ("1", "true", "yes", "on")),
    # An opening cites every claim it sums up: the writer is told so, and the check
    # judges the opening against all the unit's claims and names those supporting
    # it, which become its citations (write.OPENER_CITES).
    "opener_cites": ("lore.query.write", "OPENER_CITES",
                     lambda v: v.lower() in ("1", "true", "yes", "on")),
    # An opening still rejected gets one focused introduction call, checked once;
    # a gist with no opening takes its sections' first sentences (write.INTRO_REPAIR).
    "intro_repair": ("lore.query.write", "INTRO_REPAIR",
                     lambda v: v.lower() in ("1", "true", "yes", "on")),
    # A failed orphan repair keeps the prose: the repaired draft minus its rejects,
    # else the first draft minus rejects and orphans, the claim list last
    # (write.GENTLE_FALLBACK).
    "gentle_fallback": ("lore.query.write", "GENTLE_FALLBACK",
                        lambda v: v.lower() in ("1", "true", "yes", "on")),
    # Events in story order in narrative prose: the writer rule, then code moves a
    # sentence told after a later event back, else one repair round
    # (write.STORY_ORDER_PROSE).
    "story_order_prose": ("lore.query.write", "STORY_ORDER_PROSE",
                          lambda v: v.lower() in ("1", "true", "yes", "on")),
    # For an event question, the passage just before a cited passage that opens
    # mid-scene joins the evidence as its setup (scene.SCENE_SETUP).
    "scene_setup": ("lore.query.scene", "SCENE_SETUP",
                    lambda v: v.lower() in ("1", "true", "yes", "on")),
    # A written sentence keeps who did what to whom, and how often: the writer rule
    # and the sentence check (synthesize.ATTRIBUTION_FIDELITY).
    "attribution_fidelity": ("lore.query.synthesize", "ATTRIBUTION_FIDELITY",
                             lambda v: v.lower() in ("1", "true", "yes", "on")),
    # No shown sentence restates an earlier one; COVERAGE cites a claim a shown
    # sentence already says instead of placing it (synthesize.DEDUP_SENTENCES).
    "dedup_sentences": ("lore.query.synthesize", "DEDUP_SENTENCES",
                        lambda v: v.lower() in ("1", "true", "yes", "on")),
    # Story order, own words and gaps in the writer and the check.
    "story_order": ("lore.query.write", "STORY_ORDER",
                    lambda v: v.lower() in ("1", "true", "yes", "on")),
    # Labelled, checked readings of what actions show (write.READINGS).
    "readings": ("lore.query.write", "READINGS",
                 lambda v: v.lower() in ("1", "true", "yes", "on")),
    # Kinds, lenses and the interpretive answer (reading, counter-case, limits):
    # lore/query/decompose.py, interpret.py.
    "interpretive": ("lore.query.decompose", "INTERPRETIVE",
                     lambda v: v.lower() in ("1", "true", "yes", "on")),
    # The deep answer: an outline planned from the graph and codex, one section
    # per entry (lore/query/deep.py). `deep=true` also asks every question at
    # depth "deep"; `deep=false` is the code before it existed.
    "deep": ("lore.query.decompose", "DEEP",
             lambda v: v.lower() in ("1", "true", "yes", "on")),
    # The story timeline of the question's entities (lore/query/timeline.py).
    "timeline": ("lore.query.timeline", "TIMELINE",
                 lambda v: v.lower() in ("1", "true", "yes", "on")),
    # Recap answers: a question asking what happens over a stretch of the story is
    # answered from the stretch's beats, one section per stretch of chapters
    # (lore/query/recap.py). `recap=false` is the code before it existed.
    "recap": ("lore.query.decompose", "RECAP",
              lambda v: v.lower() in ("1", "true", "yes", "on")),
    # Follow-ups: a question with `history` (earlier turns, each {question,
    # answer}) is rewritten to stand alone in the decompose call, and the
    # rewrite drives the answer (lore/query/decompose.py). `followups=false`
    # ignores the history, as the code before it did. Questions without
    # history are unaffected by either arm.
    "followups": ("lore.query.decompose", "FOLLOWUPS",
                  lambda v: v.lower() in ("1", "true", "yes", "on")),
    # A hub's edges past the limit chosen by the question and spread over the
    # story, direct edges first for two entities (lore/query/hubselect.py).
    # `hub_select=false` is the template's own LIMIT, earliest edges first.
    "hub_select": ("lore.query.hubselect", "HUB_SELECT",
                   lambda v: v.lower() in ("1", "true", "yes", "on")),
    # Passages of an edge between two named entities whose predicate the
    # question asks about join the part's ranking (hubselect.CUE_FUSION).
    "cue_fusion": ("lore.query.hubselect", "CUE_FUSION",
                   lambda v: v.lower() in ("1", "true", "yes", "on")),
    # Vector-found seeds bring what an IDENTIFIED_AS edge ties them to, as
    # named seeds already do (seed.FOLLOW_IDENTITY_LINKS).
    # A template from an entity also walks from what IDENTIFIED_AS ties it to.
    # Multi-part factual answers: drop a later part's claim that cites the
    # same quote as an earlier part's; show a list answer in reading order.
    "cross_part_quote": ("lore.query.synthesize", "CROSS_PART_SAME_QUOTE",
                         lambda v: v.lower() in ("1", "true", "yes", "on")),
    "story_order_list": ("lore.query.synthesize", "STORY_ORDER_LIST",
                         lambda v: v.lower() in ("1", "true", "yes", "on")),
    # Later check rounds keep the first round's claim verdicts; the claim check
    # treats a quote as a pointer into its passage.
    "carry_verdicts": ("lore.query.synthesize", "CARRY_VERDICTS",
                       lambda v: v.lower() in ("1", "true", "yes", "on")),
    "quote_pointer": ("lore.query.synthesize", "QUOTE_POINTER",
                      lambda v: v.lower() in ("1", "true", "yes", "on")),
    "identity_search": ("lore.query.hubselect", "SEARCH_IDENTITY_LINKS",
                        lambda v: v.lower() in ("1", "true", "yes", "on")),
    "identity_links": ("lore.query.seed", "FOLLOW_IDENTITY_LINKS",
                       lambda v: v.lower() in ("1", "true", "yes", "on")),
}


# Arm settings that change [lore.llm] instead of a module attribute.
LLM_SETTINGS = {"provider": "provider", "model": "model", "fast_model": "fast_model"}
LLM = ("config", "llm")


def parse_arm(spec: str) -> dict[tuple[str, str], object]:
    if spec == "control":
        return {}
    out = {}
    for item in spec.split(","):
        name, _, value = item.partition("=")
        if name in LLM_SETTINGS:
            llm = dict(out.get(LLM, {}))
            llm[LLM_SETTINGS[name]] = value
            out[LLM] = llm
            continue
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
            if (module, attr) == LLM:
                continue  # applied to the config (with_llm), not a module
            mod = importlib.import_module(module)
            self._saved[(mod, attr)] = getattr(mod, attr)
            setattr(mod, attr, value)

    def __exit__(self, *exc):
        for (mod, attr), value in self._saved.items():
            setattr(mod, attr, value)


def with_llm(config, overrides):
    """The config with this arm's [lore.llm] (a provider arm), else as given."""
    llm = overrides.get(LLM)
    if not llm:
        return config
    return config.model_copy(update={"llm": config.llm.model_copy(update=llm)})


def _pinned(config, model: str):
    return config.model_copy(update={
        step: getattr(config, step).model_copy(update={"models": [model]})
        for step in ("selection", "synthesis")})


# The world every question is asked in (--world); None asks across all worlds,
# where deep answers do not run, so a run meant to mirror the UI names one.
WORLD: str | None = None


async def ask(config, question: str, overrides, trace: list | None, history=None):
    # The `deep` arm asks at depth "deep"; any other arm lets the decomposer decide.
    depth = "deep" if overrides.get(("lore.query.decompose", "DEEP")) is True else None
    kw = {"depth": depth} if depth else {}
    # Evals measure the whole story: a reading position saved in the library
    # (and moved while a run is going) must not change what an arm can see.
    kw["spoilers"] = True
    if WORLD is not None:
        kw["world"] = WORLD
    if history:  # a follow-up question in the set: its earlier turns
        kw["history"] = history
    config = with_llm(config, overrides)
    with _Applied(overrides):
        models = _real_models(config)
        if trace is not None:
            models = _traced(models, trace)
        result = await tools.lore_search(config, question, models=models, **kw)
        for _ in range(2):  # transient refusals are not a quality signal
            err = result.get("error")
            if not err or err["code"] != "upstream_unavailable":
                break
            await asyncio.sleep(60)
            models = _real_models(config)
            if trace is not None:
                trace.clear()
                models = _traced(models, trace)
            result = await tools.lore_search(config, question, models=models, **kw)
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
            result = await ask(config, q["question"], overrides[a], calls, q.get("history"))
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
    parser.add_argument("--world", help="ask every question in this world (as the UI does)")
    args = parser.parse_args()
    if len(args.arm) != 2:
        raise SystemExit("give exactly two --arm")
    global WORLD
    WORLD = args.world
    asyncio.run(run(args.label, args.arm, args.qset, args.only, args.trace))


if __name__ == "__main__":
    main()
