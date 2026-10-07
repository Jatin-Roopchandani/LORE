import { useState } from "react";
import { UI } from "../messages";

export function AskBox({ onAsk, busy }: { onAsk: (q: string) => void; busy: boolean }) {
  const [q, setQ] = useState("");
  return (
    <form className="ask" onSubmit={(e) => { e.preventDefault(); const v = q.trim(); if (v && !busy) { onAsk(v); setQ(""); } }}>
      <input id="ask-input" autoComplete="off" maxLength={2000} placeholder={UI.askPlaceholder} aria-label="Question" value={q} onChange={(e) => setQ(e.target.value)} />
      <button type="submit" disabled={busy || !q.trim()}>{busy ? "Answering…" : "Ask"}</button>
    </form>
  );
}
