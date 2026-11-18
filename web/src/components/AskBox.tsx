import { useRef, useState } from "react";
import { DEEP, POSITION, PROCESSING, UI } from "../messages";
import { DEPTHS } from "../depth";
import { askCount } from "../bookStatus";
import type { Depth, LibraryBook } from "../types";

// `placeholder`: what the empty box says ("Ask about your books…" by default; "Ask a follow-up…" inside a chat).
// `spoilerGuard`: the reading progress holds something back, so the box offers "Show spoilers for this question": the
// next question alone is answered from every book (`onAsk(q, true)`), and the toggle is off again once it is sent.
// `onStop`: while an answer streams (`busy`) the Ask button is Stop, and Esc in the box does the same. It returns the
// stopped question, which the box holds again (unless something new has been typed) so it can be asked again.
// `finishing`: the answer has all arrived and is only still being drawn, so there is nothing to stop: the button is
// "Show answer" instead, and it (or Esc) draws the answer whole at once.
// `pending`: books in the current world (or any, under all worlds) still being processed: a note under the box says
// questions about them will work once they're ready. It never stops a question.
export function AskBox({ onAsk, busy, depth = "auto", onDepth, placeholder = UI.askPlaceholder, spoilerGuard = false, onStop,
  finishing = false, pending = [] }:
  { onAsk: (q: string, spoilers?: boolean) => void; busy: boolean; depth?: Depth; onDepth?: (d: Depth) => void; placeholder?: string;
    spoilerGuard?: boolean; onStop?: () => string | null; finishing?: boolean; pending?: LibraryBook[] }) {
  const [q, setQ] = useState("");
  const [spoilers, setSpoilers] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const showing = spoilerGuard && spoilers;
  const stoppable = busy && onStop !== undefined;
  const stop = () => {
    const stopped = onStop?.() ?? null;
    if (stopped && !q.trim()) setQ(stopped);
    // The Stop button gives way to Ask: the box keeps the focus, ready to ask again.
    input.current?.focus();
  };
  return (
    <form className="ask" onSubmit={(e) => {
      e.preventDefault(); const v = q.trim();
      if (v && !busy) { if (showing) onAsk(v, true); else onAsk(v); setQ(""); setSpoilers(false); }
    }} onKeyDown={(e) => {
      if (e.key === "Escape" && stoppable) { e.preventDefault(); stop(); }
    }}>
      <input id="ask-input" ref={input} autoComplete="off" maxLength={2000} placeholder={placeholder} aria-label="Question" value={q}
        onChange={(e) => setQ(e.target.value)} aria-keyshortcuts={stoppable ? "Escape" : undefined} />
      {stoppable
        // Two elements, not one whose type changes: a Stop click that turns the button into Ask (type submit) before the
        // browser finishes the click would submit the form and ask the stopped question again.
        ? (finishing
          ? <button key="show" type="button" className="stop finishing" onClick={stop} title={UI.showAnswerHint}
              aria-label={UI.showAnswerHint} aria-keyshortcuts="Escape">{UI.showAnswer}</button>
          : <button key="stop" type="button" className="stop" onClick={stop} title={UI.stopHint} aria-label={UI.stopHint} aria-keyshortcuts="Escape">
              <span className="stop-icon" aria-hidden="true" />{UI.stop}
            </button>)
        : <button key="ask" type="submit" disabled={busy || !q.trim()}>{busy ? "Answering…" : UI.ask}</button>}
      {pending.length > 0 && <PendingNote books={pending} />}
      {(onDepth || spoilerGuard) && (
        <div className="ask-opts">
          {onDepth && (<>
            <fieldset className="depth" aria-describedby="depth-hint">
              <legend>{DEEP.toggleLabel}</legend>
              {DEPTHS.map((d) => (
                <label key={d} className={depth === d ? "on" : undefined}>
                  <input type="radio" name="depth" value={d} checked={depth === d} onChange={() => onDepth(d)} />
                  <span>{DEEP.options[d]}</span>
                </label>
              ))}
            </fieldset>
            <span id="depth-hint" className="depth-hint">{depth === "deep" ? DEEP.costNote : DEEP.toggleHint[depth]}</span>
          </>)}
          {spoilerGuard && (
            <label className={`spoiler-toggle${showing ? " on" : ""}`}>
              <input type="checkbox" role="switch" checked={showing} aria-describedby={showing ? "spoiler-hint" : undefined}
                onChange={(e) => setSpoilers(e.target.checked)} />
              <span>{POSITION.spoilerToggle}</span>
            </label>
          )}
          {showing && <span id="spoiler-hint" className="spoiler-hint" role="status">{POSITION.spoilerOn}</span>}
        </div>
      )}
    </form>
  );
}

const started = (b: LibraryBook) => b.job !== null || b.processing === true;

function PendingNote({ books }: { books: LibraryBook[] }) {
  if (books.length === 1) {
    const b = books[0];
    return (
      <p className="ask-pending" role="note" aria-label={PROCESSING.noteLabel}>
        <em>{b.title}</em> {started(b) ? PROCESSING.askOne(askCount(b)) : PROCESSING.askOneNotStarted}
      </p>
    );
  }
  return (
    <p className="ask-pending" role="note" aria-label={PROCESSING.noteLabel}>
      {PROCESSING.askMany(books.length)}{" "}
      {books.map((b, i) => {
        const count = askCount(b);
        return <span key={b.id}>{i > 0 && ", "}<em>{b.title}</em>{count && ` (${count})`}</span>;
      })}.
    </p>
  );
}
