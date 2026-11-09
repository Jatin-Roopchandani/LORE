import { useRef, useState } from "react";
import { DEEP, POSITION, UI } from "../messages";
import { DEPTHS } from "../depth";
import type { Depth } from "../types";

// `placeholder`: what the empty box says ("Ask about your books…" by default; "Ask a follow-up…" inside a chat).
// `spoilerGuard`: the reading progress holds something back, so the box offers "Show spoilers for this question": the
// next question alone is answered from every book (`onAsk(q, true)`), and the toggle is off again once it is sent.
// `onStop`: while an answer streams (`busy`) the Ask button is Stop, and Esc in the box does the same. It returns the
// stopped question, which the box holds again (unless something new has been typed) so it can be asked again.
export function AskBox({ onAsk, busy, depth = "auto", onDepth, placeholder = UI.askPlaceholder, spoilerGuard = false, onStop }:
  { onAsk: (q: string, spoilers?: boolean) => void; busy: boolean; depth?: Depth; onDepth?: (d: Depth) => void; placeholder?: string;
    spoilerGuard?: boolean; onStop?: () => string | null }) {
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
        ? <button type="button" className="stop" onClick={stop} title={UI.stopHint} aria-label={UI.stopHint} aria-keyshortcuts="Escape">
            <span className="stop-icon" aria-hidden="true" />{UI.stop}
          </button>
        : <button type="submit" disabled={busy || !q.trim()}>{busy ? "Answering…" : UI.ask}</button>}
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
