import type { useAsk } from "../useAsk";
import { errorMessage, UI } from "../messages";
import { Claims } from "./Claims";
import { Prose } from "./Prose";
import { HistoryBar } from "./HistoryBar";
import { HowPanel } from "./HowPanel";
import { AskBox } from "./AskBox";
import { Sections } from "./Sections";
import { StepList } from "./StepList";
import { Snip } from "./SettingsMenu";
import { exampleQuestions } from "../examples";
import type { SubgraphData } from "../types";

const NOT_FOUND = "not found: ";
// The backend joins notes with "; " and names each missing part as "not found: <part>".
const missingParts = (note: string | null | undefined) =>
  (note ?? "").split("; ").filter((n) => n.startsWith(NOT_FOUND)).map((n) => n.slice(NOT_FOUND.length));

export function ChatPane({ ask, showInternals, resetsAt, onHoverChunks, overview }:
  { ask: ReturnType<typeof useAsk>; showInternals: boolean; resetsAt?: string; onHoverChunks: (chunks: string[]) => void;
    overview?: SubgraphData | null }) {
  const { state } = ask;
  const r = state.result;
  const err = state.error ? errorMessage(state.error, { showInternals, resetsAt }) : null;
  const notFound = missingParts(r?.note);
  // A deep answer: the plan or a section has arrived, or the result says so. (A deep question that fell back
  // to the ordinary path has a plan step but no plan, so it renders as before.)
  const sectioned = state.plan !== null || state.sections.length > 0 || r?.answer_style === "sectioned";
  const examples = state.question ? [] : exampleQuestions(overview);
  return (
    <section className="pane chat" aria-label="Conversation">
      <HistoryBar items={ask.history} current={ask.current} onPick={ask.replay} />
      <div className="thread" aria-live="polite">
        {!state.question && (
          <div className="empty">
            <p>{UI.emptyIntro}</p>
            {examples.length > 0 && (
              <ul className="examples" aria-label={UI.examplesLabel}>
                {examples.map((q) => <li key={q}><button className="example" type="button" onClick={() => ask.ask(q)}>{q}</button></li>)}
              </ul>
            )}
          </div>
        )}
        {state.question && <div className="q">{state.question}</div>}
        {state.question && (
          <div className="a">
            <StepList state={state} />
            {err && (
              <div className="note" role="alert">
                <p>{err.text}</p>
                {err.command && <Snip text={err.command} />}
                {err.retry && <button className="tbtn" type="button" onClick={ask.retry}>Retry</button>}
              </div>
            )}
            {r && r.claims.length === 0 && <p className="note">{UI.refusal}</p>}
            {sectioned && <Sections key={ask.run} state={state} onHoverChunks={onHoverChunks} />}
            {!sectioned && r && r.claims.length > 0 && r.answer_style === "prose" && (r.prose?.length ?? 0) > 0 ? (
              <>
                <Prose key={ask.run} result={r} onHoverChunks={onHoverChunks} />
                <details className="evidence">
                  <summary>{UI.evidence(r.claims.length)}</summary>
                  <Claims key={ask.run} result={r} onHoverChunks={onHoverChunks} />
                </details>
              </>
            ) : !sectioned && r && r.claims.length > 0 && (
              <>
                {(r.note ?? "").includes("written answer did not pass verification") && <p className="note">{UI.proseFallback}</p>}
                <Claims key={ask.run} result={r} onHoverChunks={onHoverChunks} />
              </>
            )}
            {notFound.length > 0 && <p className="note">{UI.partial(notFound)}</p>}
            <HowPanel state={state} showInternals={showInternals} />
          </div>
        )}
      </div>
      <AskBox onAsk={ask.ask} busy={ask.busy} depth={ask.depth} onDepth={ask.setDepth} />
    </section>
  );
}
