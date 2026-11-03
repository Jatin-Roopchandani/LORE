import { Fragment, useId, useState } from "react";
import type { AskState, DeepStepView, StepView } from "../askReducer";
import { STEP_LABEL, STEPS_DEEP } from "../messages";
import { loadOpen, saveOpen, SPLIT_KEY, STEPS_KEY } from "../stepsPref";

const DOT = { pending: "○", running: "●", done: "✓", skipped: "–", failed: "✕" } as const;

type Timed = { state: string; detail: string | null; startedAt: number | null; doneAt: number | null };

function timing(s: Timed): string {
  // A section that failed or was not written says so: it never shows a duration as if it had worked.
  if (s.state === "failed") return STEPS_DEEP.failed;
  if (s.state === "skipped") return s.detail ?? STEPS_DEEP.skipped;
  if (s.state !== "done") return "";
  const secs = s.startedAt !== null && s.doneAt !== null ? `${(s.doneAt - s.startedAt).toFixed(1)}s` : null;
  return [s.detail, secs].filter(Boolean).join(" · ");
}

// The label is cut with an ellipsis when it is long (its full text is the tooltip); the timing never wraps.
function Row({ cls, state, label, time }: { cls?: string; state: StepView["state"]; label: string; time: string }) {
  return (
    <li className={[state === "running" ? "now" : state, cls].filter(Boolean).join(" ")}>
      <span className="dot" aria-hidden="true">{DOT[state]}</span>
      <span className="lbl" title={label}>{label}</span>
      <span className="t">{time}</span>
    </li>
  );
}

interface PartLine { question: string; part: number | null; lenses: { part: number; question: string }[] }

// The question's parts as the plan had them: a part, or an interpretive part with the lenses it was looked at
// through (`lens_of` says which plan part each retrieval part is a lens of; the other parts keep the plan's order).
function partLines(state: AskState): PartLine[] {
  const lines: PartLine[] = [];
  let plan = -1;
  state.subQuestions.forEach((question, part) => {
    const of = state.lensOf[part];
    if (of === null || of === undefined) {
      plan = Math.max(plan + 1, 0);
      lines.push({ question, part, lenses: [] });
      return;
    }
    plan = of;
    const last = lines[lines.length - 1];
    if (last && last.part === null && last.question === (state.planQuestions[of] ?? "")) last.lenses.push({ part, question });
    else lines.push({ question: state.planQuestions[of] ?? question, part: null, lenses: [{ part, question }] });
  });
  return lines;
}

function Split({ state }: { state: AskState }) {
  const id = useId();
  const [open, setOpen] = useState(() => loadOpen(SPLIT_KEY, true));
  const lines = partLines(state);
  const hasLenses = lines.some((l) => l.lenses.length > 0);
  const sectionRow = (part: number | null): DeepStepView | undefined =>
    part === null ? undefined : state.deepSteps.find((d) => d.kind === "section" && d.part === part);
  // A part's own search detail, until its section has a status of its own to show.
  const detail = (part: number) => {
    const seeds = state.seeds[part]?.length ?? 0;
    const rels = state.edges[part]?.total ?? 0;
    return seeds > 0 || rels > 0 ? STEPS_DEEP.lensDetail(seeds, rels) : "";
  };
  const line = (part: number, question: string, cls: string) => {
    const row = sectionRow(part);
    return (
      <li key={part} className={[cls, row ? row.state : ""].filter(Boolean).join(" ")}>
        {row && <span className="mark" aria-hidden="true">{DOT[row.state]}</span>}
        <span className={`${cls}-q`} title={question}>{question}</span>
        <span className="t">{row ? timing(row) || detail(part) : detail(part)}</span>
      </li>
    );
  };
  const toggle = (
    <button type="button" className="split-toggle" aria-expanded={open} aria-controls={id}
      onClick={() => { setOpen(!open); saveOpen(SPLIT_KEY, !open); }}>
      {STEPS_DEEP.split(state.subQuestions.length)}
    </button>
  );
  return (
    <li className="split">
      {toggle}
      {open && (
        <ul id={id} aria-label={STEPS_DEEP.partsLabel}>
          {lines.map((l, i) => l.lenses.length === 0 && l.part !== null
            ? line(l.part, l.question, "part")
            : (
              <li key={`g${i}`} className="part">
                <span className="part-q" title={l.question}>{l.question}</span>
                <div className="lens-block">
                  <span className="lens-label">{STEPS_DEEP.lensLabel}</span>
                  <ul>{l.lenses.map((x) => line(x.part, x.question, "lens"))}</ul>
                </div>
              </li>
            ))}
        </ul>
      )}
      {hasLenses && null}
    </li>
  );
}

export function StepList({ state }: { state: AskState }) {
  const panel = useId();
  const [pref, setPref] = useState(() => loadOpen(STEPS_KEY, false));
  const running = state.status === "running";
  // While the answer is being made the steps are open; once it has landed they fold into one line, and the viewer's
  // last choice (remembered here) says whether that line is open.
  const open = running || pref;
  const deep = state.deepSteps.length > 0;
  // Sections that are lenses of an interpretive part are shown on their lens lines, not twice.
  const lensRun = state.lensOf.some((x) => x !== null && x !== undefined);
  // In a deep answer the per-part rows (seeds, search, ...) that never ran are left out; if the answer
  // fell back to the ordinary path they run, and show.
  const generic = state.steps.filter((s) => !deep || s.name === "split" || (s.state !== "pending" && s.state !== "skipped"));
  const heading = (d: DeepStepView) => {
    const h = state.sections.find((x) => x.index === d.part)?.heading ?? state.plan?.sections.find((x) => x.index === d.part)?.heading ?? null;
    return h;
  };
  const row = (s: StepView) => (
    <Fragment key={s.name}>
      <Row state={s.state} label={STEP_LABEL[s.name]} time={timing(s)} />
      {s.name === "split" && showSplit && <Split state={state} />}
    </Fragment>
  );
  // The parts list: for a plan of sections (topical) the section rows are the structure, so it is left out.
  const topical = deep && !lensRun;
  const showSplit = !topical && (state.subQuestions.length > 1 || lensRun);
  const rest = generic.filter((s) => s.name !== "split");
  const sections = state.sections.filter((x) => x.kind === "section" && x.state === "done").length
    || (state.result?.sections ?? []).filter((x) => x.kind === "section").length;
  const parts = state.subQuestions.length || state.result?.sub_questions.length || 1;
  const list = (
    <ol className="steps" id={panel}>
      {generic.filter((s) => s.name === "split").map(row)}
      {state.deepSteps.filter((d) => !(lensRun && d.kind === "section")).map((d) => d.kind === "plan"
        ? <Row key="plan" state={d.state} label={STEPS_DEEP.plan} time={timing(d)} />
        : <Row key={`section-${d.part}`} cls="section-step" state={d.state} label={STEPS_DEEP.section((d.part ?? 0) + 1, heading(d))} time={timing(d)} />)}
      {rest.map(row)}
    </ol>
  );
  if (running) return list;
  return (
    <div className="steps-panel">
      <button type="button" className="steps-toggle" aria-expanded={open} aria-controls={panel}
        onClick={() => { setPref(!pref); saveOpen(STEPS_KEY, !pref); }}>
        {STEPS_DEEP.summary(parts, sections, Math.round(state.elapsed))}
      </button>
      {open && list}
    </div>
  );
}
