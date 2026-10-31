import { Fragment } from "react";
import type { AskState, DeepStepView, StepView } from "../askReducer";
import { STEP_LABEL, STEPS_DEEP } from "../messages";

const DOT = { pending: "○", running: "●", done: "✓", skipped: "–" } as const;

function timing(s: { state: string; detail: string | null; startedAt: number | null; doneAt: number | null }): string {
  if (s.state !== "done") return "";
  const secs = s.startedAt !== null && s.doneAt !== null ? `${(s.doneAt - s.startedAt).toFixed(1)}s` : null;
  return [s.detail, secs].filter(Boolean).join(" · ");
}

function Row({ cls, state, label, time }: { cls?: string; state: StepView["state"]; label: string; time: string }) {
  return (
    <li className={[state === "running" ? "now" : state, cls].filter(Boolean).join(" ")}>
      <span className="dot" aria-hidden="true">{DOT[state]}</span>
      <span>{label}</span>
      <span className="t">{time}</span>
    </li>
  );
}

// The parts that are lenses of an interpretive part, grouped under that part (`lens_of` says which).
function LensGroups({ state }: { state: AskState }) {
  const groups = new Map<number, number[]>();
  state.lensOf.forEach((plan, part) => { if (plan !== null && plan !== undefined) groups.set(plan, [...(groups.get(plan) ?? []), part]); });
  if (!groups.size) return null;
  return (
    <li className="lens-groups">
      <ul aria-label={STEPS_DEEP.partsLabel}>
        {[...groups].map(([plan, parts]) => (
          <li key={plan} className="lens-group">
            <span className="lens-of">{STEPS_DEEP.group(state.planQuestions[plan] ?? state.subQuestions[parts[0]] ?? "")}</span>
            <ul>
              {parts.map((part) => {
                const seeds = state.seeds[part]?.length ?? 0;
                const rels = state.edges[part]?.total ?? 0;
                return (
                  <li key={part} className="lens">
                    <span>{state.subQuestions[part]}</span>
                    {(seeds > 0 || rels > 0) && <span className="t">{STEPS_DEEP.lensDetail(seeds, rels)}</span>}
                  </li>
                );
              })}
            </ul>
          </li>
        ))}
      </ul>
    </li>
  );
}

export function StepList({ state }: { state: AskState }) {
  const deep = state.deepSteps.length > 0;
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
      {s.name === "search" && <LensGroups state={state} />}
    </Fragment>
  );
  const rest = generic.filter((s) => s.name !== "split");
  return (
    <ol className="steps">
      {generic.filter((s) => s.name === "split").map(row)}
      {state.deepSteps.map((d) => d.kind === "plan"
        ? <Row key="plan" state={d.state} label={STEPS_DEEP.plan} time={timing(d)} />
        : <Row key={`section-${d.part}`} cls="section-step" state={d.state} label={STEPS_DEEP.section((d.part ?? 0) + 1, heading(d))} time={timing(d)} />)}
      {rest.map(row)}
      {!rest.some((s) => s.name === "search") && <LensGroups state={state} />}
    </ol>
  );
}
