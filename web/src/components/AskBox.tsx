import { useState } from "react";
import { DEEP, UI } from "../messages";
import { DEPTHS } from "../depth";
import type { Depth } from "../types";

export function AskBox({ onAsk, busy, depth = "auto", onDepth }: { onAsk: (q: string) => void; busy: boolean; depth?: Depth; onDepth?: (d: Depth) => void }) {
  const [q, setQ] = useState("");
  return (
    <form className="ask" onSubmit={(e) => { e.preventDefault(); const v = q.trim(); if (v && !busy) { onAsk(v); setQ(""); } }}>
      <input id="ask-input" autoComplete="off" maxLength={2000} placeholder={UI.askPlaceholder} aria-label="Question" value={q} onChange={(e) => setQ(e.target.value)} />
      <button type="submit" disabled={busy || !q.trim()}>{busy ? "Answering…" : "Ask"}</button>
      {onDepth && (
        <div className="ask-opts">
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
        </div>
      )}
    </form>
  );
}
