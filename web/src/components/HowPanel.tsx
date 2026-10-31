import type { AskState } from "../askReducer";
import { DEEP } from "../messages";

export function HowPanel({ state, showInternals }: { state: AskState; showInternals: boolean }) {
  const r = state.result;
  if (!r) return null;
  const byN = new Map(r.passages.map((p) => [p.n, p.chunk_id]));
  const cited = new Set(r.claims.map((c) => byN.get(c.passage)).filter(Boolean));
  const seeds = [...new Map(state.seeds.flat().map((s) => [s.id, s.name])).values()];
  const relationships = state.edges.reduce((a, pe) => a + (pe?.total ?? 0), 0);
  const parts = state.subQuestions.length ? state.subQuestions : r.sub_questions;
  return (
    <details className="how">
      <summary>How this was answered</summary>
      <dl>
        <dt>Parts</dt><dd>{parts.map((s, i) => <div key={i}>{s}</div>)}</dd>
        <dt>Graph search</dt><dd><code>{r.template ?? "none"}</code>{` · ${relationships} relationships`}</dd>
        <dt>Starting entities</dt><dd>{seeds.join(", ") || "none"}</dd>
        <dt>Passages read</dt><dd>{`${r.passages.length} (${r.claims.length} claims cite ${cited.size})`}</dd>
        {(r.requests ?? 0) > 0 && <><dt>{DEEP.requests}</dt><dd>{DEEP.requestCount(r.requests!)}</dd></>}
        {showInternals && <><dt>Models</dt><dd><code>{r.selection_model ?? "none"}</code> chose the search; <code>{r.model ?? "none"}</code> wrote the answer</dd></>}
      </dl>
    </details>
  );
}
