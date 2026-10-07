import type { StepView } from "../askReducer";
import { STEP_LABEL } from "../messages";

const DOT = { pending: "○", running: "●", done: "✓", skipped: "–" } as const;

function timing(s: StepView): string {
  if (s.state !== "done") return "";
  const secs = s.startedAt !== null && s.doneAt !== null ? `${(s.doneAt - s.startedAt).toFixed(1)}s` : null;
  return [s.detail, secs].filter(Boolean).join(" · ");
}

export function StepList({ steps }: { steps: StepView[] }) {
  return (
    <ol className="steps">
      {steps.map((s) => (
        <li key={s.name} className={s.state === "running" ? "now" : s.state}>
          <span className="dot" aria-hidden="true">{DOT[s.state]}</span>
          <span>{STEP_LABEL[s.name]}</span>
          <span className="t">{timing(s)}</span>
        </li>
      ))}
    </ol>
  );
}
