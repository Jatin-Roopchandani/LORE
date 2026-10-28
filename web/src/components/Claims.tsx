import { useState } from "react";
import { highlightQuote } from "../highlight";
import type { SearchResult } from "../types";

const short = (title: string | null) => (title ?? "Book").replace(/^(A|An|The) /, "").split(" ")[0];

export function Claims({ result, onHoverChunks }: { result: SearchResult; onHoverChunks: (chunks: string[]) => void }) {
  const [open, setOpen] = useState<number | null>(null);
  const byN = new Map(result.passages.map((p) => [p.n, p]));
  return (
    <ul className="claims">
      {result.claims.map((c, i) => {
        const p = byN.get(c.passage);
        return (
          <li key={i} className="claim" onMouseEnter={() => onHoverChunks(p ? [p.chunk_id] : [])} onMouseLeave={() => onHoverChunks([])}>
            <p>{c.text}
              {p && <button className="cite" type="button" aria-expanded={open === i} onClick={() => setOpen(open === i ? null : i)}>
                {`${short(p.book_title)} ch.${p.chapter_seq} · [${c.passage}]`}</button>}
            </p>
            {p && open === i && (
              <blockquote className="passage">
                <span className="src">{`${p.book_title ?? p.book_id} · chapter ${p.chapter_seq} · ${p.chunk_id}`}</span>
                {highlightQuote(p.text, c.quote).map((s, j) => s.mark ? <mark key={j}>{s.text}</mark> : <span key={j}>{s.text}</span>)}
              </blockquote>
            )}
          </li>
        );
      })}
    </ul>
  );
}
