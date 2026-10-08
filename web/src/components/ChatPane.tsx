import type { useAsk } from "../useAsk";
import { errorMessage, UI } from "../messages";
import { Claims } from "./Claims";
import { HistoryBar } from "./HistoryBar";
import { HowPanel } from "./HowPanel";
import { AskBox } from "./AskBox";
import { StepList } from "./StepList";
import { Snip } from "./SettingsMenu";
import { exampleQuestions } from "../examples";
import type { SubgraphData } from "../types";

const NOT_FOUND = "not found: ";
// The backend joins notes with "; " and names each missing part as "not found: <part>".
const missingParts = (note: string | null | undefined) =>
  (note ?? "").split("; ").filter((n) => n.startsWith(NOT_FOUND)).map((n) => n.slice(NOT_FOUND.length));

export function ChatPane({ ask, showInternals, resetsAt, onHoverChunk, overview }:
  { ask: ReturnType<typeof useAsk>; showInternals: boolean; resetsAt?: string; onHoverChunk: (c: string | null) => void;
    overview?: SubgraphData | null }) {
  const { state } = ask;
  const r = state.result;
  const err = state.error ? errorMessage(state.error, { showInternals, resetsAt }) : null;
  const notFound = missingParts(r?.note);
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
            <StepList steps={state.steps} />
            {err && (
              <div className="note" role="alert">
                <p>{err.text}</p>
                {err.command && <Snip text={err.command} />}
                {err.retry && <button className="tbtn" type="button" onClick={ask.retry}>Retry</button>}
              </div>
            )}
            {r && r.claims.length === 0 && <p className="note">{UI.refusal}</p>}
            {r && r.claims.length > 0 && <Claims key={ask.run} result={r} onHoverChunk={onHoverChunk} />}
            {notFound.length > 0 && <p className="note">{UI.partial(notFound)}</p>}
            <HowPanel state={state} showInternals={showInternals} />
          </div>
        )}
      </div>
      <AskBox onAsk={ask.ask} busy={ask.busy} />
    </section>
  );
}
