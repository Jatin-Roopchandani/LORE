import { memo, useEffect, useMemo, useRef, useState } from "react";
import type { useAsk } from "../useAsk";
import { foldEvents, type AskState } from "../askReducer";
import type { ThreadEntry } from "../history";
import { CHAT, errorMessage, POSITION, SETTINGS, UI } from "../messages";
import { hrefFor } from "../settings";
import { Claims } from "./Claims";
import { Prose } from "./Prose";
import { HowPanel } from "./HowPanel";
import { ReportAnswer } from "./ReportAnswer";
import { AskBox } from "./AskBox";
import { Sections } from "./Sections";
import { ShortAnswer } from "./ShortAnswer";
import { StepList } from "./StepList";
import { Snip } from "./Snip";
import { exampleQuestions } from "../examples";
import type { AskEvent, SubgraphData } from "../types";

const NOT_FOUND = "not found: ";
// The backend joins notes with "; " and names each missing part as "not found: <part>".
const missingParts = (note: string | null | undefined) =>
  (note ?? "").split("; ").filter((n) => n.startsWith(NOT_FOUND)).map((n) => n.slice(NOT_FOUND.length));

// At most this many answers are drawn per thread (the latest included); "show earlier" draws more.
export const MAX_SHOWN = 20;

type AskApi = ReturnType<typeof useAsk>;
type Hover = (chunks: string[]) => void;

// `onSection`: the latest answer's section being read (a deep answer), for the graph. `limited`: the reading progress
// holds something back, so the ask box offers "Show spoilers for this question".
export function ChatPane({ ask, limited = false, showInternals, resetsAt, onHoverChunks, overview, onSection }:
  { ask: Pick<AskApi, "state" | "run" | "ask" | "retry" | "busy" | "depth" | "setDepth"> & Partial<Pick<AskApi, "thread" | "clear">>;
    limited?: boolean;
    showInternals: boolean; resetsAt?: string; onHoverChunks: Hover; overview?: SubgraphData | null; onSection?: (index: number | null) => void }) {
  const { state } = ask;
  const thread = ask.thread ?? [];
  const inChat = Boolean(state.question) || thread.length > 0;
  const examples = inChat ? [] : exampleQuestions(overview);
  // A follow-up starts at the top of the view, under the thread above it.
  const latest = useRef<HTMLDivElement>(null);
  useEffect(() => { if (thread.length) latest.current?.scrollIntoView?.({ block: "start" }); }, [ask.run, thread.length]);
  return (
    <section className="pane chat" aria-label="Conversation">
      {inChat && ask.clear && (
        <div className="chat-bar">
          <button className="tbtn" type="button" title={CHAT.newChatHint} onClick={ask.clear}>{CHAT.newChat}</button>
        </div>
      )}
      <div className="thread" aria-live="polite">
        {!inChat && (
          <div className="empty">
            <p>{UI.emptyIntro}</p>
            {examples.length > 0 && (
              <ul className="examples" aria-label={UI.examplesLabel}>
                {examples.map((q) => <li key={q}><button className="example" type="button" onClick={() => ask.ask(q)}>{q}</button></li>)}
              </ul>
            )}
          </div>
        )}
        {thread.length > 0 && <Earlier key={thread[0].key} thread={thread} showInternals={showInternals} onHoverChunks={onHoverChunks} />}
        {state.question && (
          <div className="turn latest" ref={latest}>
            <div className="q">{state.question}</div>
            {state.standalone && <Understood text={state.standalone} />}
            <div className="a">
              <StepList state={state} />
              <AnswerBody state={state} run={ask.run} showInternals={showInternals} resetsAt={resetsAt} onHoverChunks={onHoverChunks} onRetry={ask.retry} onSection={onSection} />
              <HowPanel state={state} showInternals={showInternals} />
            </div>
          </div>
        )}
      </div>
      <AskBox onAsk={(q, spoilers) => (spoilers ? ask.ask(q, undefined, true) : ask.ask(q))} busy={ask.busy} depth={ask.depth} onDepth={ask.setDepth}
        spoilerGuard={limited}
        placeholder={inChat ? CHAT.followupPlaceholder : UI.askPlaceholder} />
    </section>
  );
}

function Understood({ text }: { text: string }) {
  return <p className="understood"><span className="understood-label">{CHAT.understoodAs}</span> {text}</p>;
}

// The earlier questions and answers of the chat, oldest first: the last MAX_SHOWN - 1 of them, and more on request.
function Earlier({ thread, showInternals, onHoverChunks }: { thread: ThreadEntry[]; showInternals: boolean; onHoverChunks: Hover }) {
  const [limit, setLimit] = useState(MAX_SHOWN - 1);
  const hidden = Math.max(0, thread.length - limit);
  return (
    <section className="earlier" aria-label={CHAT.earlierLabel}>
      {hidden > 0 && (
        <button className="tbtn show-earlier" type="button" onClick={() => setLimit((n) => n + MAX_SHOWN)}>
          {CHAT.showEarlier(Math.min(hidden, MAX_SHOWN))}
        </button>
      )}
      {thread.slice(hidden).map((entry) => <EarlierTurn key={entry.key} entry={entry} showInternals={showInternals} onHoverChunks={onHoverChunks} />)}
    </section>
  );
}

// One line of the answer, for a collapsed turn: its short answer, else its text, else what went wrong.
function preview(events: AskEvent[], showInternals: boolean): string {
  for (const e of events) {
    if (e.type === "answer") {
      const short = (e.result.short_answer ?? []).map((p) => p.text).join(" ");
      const prose = (e.result.prose ?? []).map((p) => p.text).join(" ");
      return (short || prose || e.result.answer || UI.refusal).replace(/\s+/g, " ").trim();
    }
    if (e.type === "error") return errorMessage(e.error, { showInternals }).text;
  }
  return CHAT.noAnswer;
}

// An earlier question and its answer. Collapsed it is the question and one line of the answer, and folds no events;
// expanded it draws the answer as it was shown (citations included), without the live parts (steps, retry).
const EarlierTurn = memo(function EarlierTurn({ entry, showInternals, onHoverChunks }:
  { entry: ThreadEntry; showInternals: boolean; onHoverChunks: Hover }) {
  const [open, setOpen] = useState(false);
  const state = useMemo(() => (open ? foldEvents(entry.question, entry.events) : null), [open, entry]);
  return (
    <article className={`turn earlier-turn${open ? " open" : ""}`}>
      <div className="q">{entry.question}</div>
      {entry.standalone && <Understood text={entry.standalone} />}
      <button className="turn-toggle" type="button" aria-expanded={open}
        aria-label={open ? CHAT.collapse(entry.question) : CHAT.expand(entry.question)} onClick={() => setOpen((o) => !o)}>
        <span className="caret" aria-hidden="true">{open ? "\u25be" : "\u25b8"}</span>
        {!open && <span className="turn-preview">{preview(entry.events, showInternals)}</span>}
      </button>
      {state && <div className="a"><AnswerBody state={state} run={0} showInternals={showInternals} onHoverChunks={onHoverChunks} /></div>}
    </article>
  );
});

// The answer itself: the error, the short answer, the sections, prose or claims, and the notes under them.
function AnswerBody({ state, run, showInternals, resetsAt, onHoverChunks, onRetry, onSection }:
  { state: AskState; run: number; showInternals: boolean; resetsAt?: string; onHoverChunks: Hover; onRetry?: () => void;
    onSection?: (index: number | null) => void }) {
  const r = state.result;
  const err = state.error ? errorMessage(state.error, { showInternals, resetsAt }) : null;
  const notFound = missingParts(r?.note);
  // A deep answer: once the answer has landed, the result says so (a deep question that fell back to the
  // ordinary path has a plan but a prose or claims answer, and must show that). Until then the stream does:
  // a plan or a written section, unless every planned section has come back empty (the ordinary path is next).
  const written = state.sections.some((s) => s.state !== "empty");
  const abandoned = state.plan !== null && state.sections.length > 0 && !written;
  const sectioned = r ? r.answer_style === "sectioned" : written || (state.plan !== null && !abandoned);
  return (
    <>
      {err && (
        <div className="note" role="alert">
          <p>{err.text}</p>
          {err.command && <Snip text={err.command} />}
          {err.settings && <a className="tbtn" href={hrefFor(err.settings)}>{SETTINGS.openTab(SETTINGS.tabs[err.settings])}</a>}
          {err.retry && onRetry && <button className="tbtn" type="button" onClick={onRetry}>Retry</button>}
        </div>
      )}
      {r && r.claims.length === 0 && <p className="note">{UI.refusal}</p>}
      <ShortAnswer state={state} onHoverChunks={onHoverChunks} />
      {r?.position_note && <p className="position-note" role="note" aria-label={POSITION.noteLabel}>{r.position_note}</p>}
      {sectioned && <Sections key={run} state={state} onHoverChunks={onHoverChunks} onSection={onSection} />}
      {!sectioned && r && r.claims.length > 0 && r.answer_style === "prose" && (r.prose?.length ?? 0) > 0 ? (
        <>
          <Prose key={run} result={r} onHoverChunks={onHoverChunks} />
          <details className="evidence">
            <summary>{UI.evidence(r.claims.length)}</summary>
            <Claims key={run} result={r} onHoverChunks={onHoverChunks} />
          </details>
        </>
      ) : !sectioned && r && r.claims.length > 0 && (
        <>
          {(r.note ?? "").includes("written answer did not pass verification") && <p className="note">{UI.proseFallback}</p>}
          <Claims key={run} result={r} onHoverChunks={onHoverChunks} />
        </>
      )}
      {notFound.length > 0 && <p className="note">{UI.partial(notFound)}</p>}
      {state.status === "done" && state.requestId && <ReportAnswer key={state.requestId} requestId={state.requestId} />}
    </>
  );
}
