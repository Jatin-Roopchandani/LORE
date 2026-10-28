import { useState } from "react";
import { highlightQuote } from "../highlight";
import type { SearchResult } from "../types";

export function Prose({ result, onHoverChunks }: { result: SearchResult; onHoverChunks: (chunks: string[]) => void }) {
  const [open, setOpen] = useState<string | null>(null);   // "<sentence>:<passage>"
  const byN = new Map(result.passages.map((p) => [p.n, p]));
  const prose = result.prose ?? [];
  const paragraphs: { s: (typeof prose)[number]; i: number }[][] = [[]];
  prose.forEach((s, i) => {
    if (s.new_paragraph && paragraphs[paragraphs.length - 1].length) paragraphs.push([]);
    paragraphs[paragraphs.length - 1].push({ s, i });
  });
  return (
    <div className="prose-answer">
      {paragraphs.map((para, pi) => (
        <div key={pi}>
          <p className="prose">
            {para.map(({ s, i }) => {
              const chunks = s.passages.map((n) => byN.get(n)?.chunk_id).filter(Boolean) as string[];
              return (
                <span key={i} className="sentence" onMouseEnter={() => onHoverChunks(chunks)} onMouseLeave={() => onHoverChunks([])}>
                  {s.text}{" "}
                  {s.passages.map((n) => (
                    <button key={n} className="cite" type="button" aria-expanded={open === `${i}:${n}`}
                      onClick={() => setOpen(open === `${i}:${n}` ? null : `${i}:${n}`)}>{`[${n}]`}</button>
                  ))}{" "}
                </span>
              );
            })}
          </p>
          {para.map(({ s, i }) => s.passages.map((n) => {
            if (open !== `${i}:${n}`) return null;
            const p = byN.get(n);
            if (!p) return null;
            const quote = s.claims.map((k) => result.claims[k - 1]).find((c) => c && c.passage === n)?.quote ?? "";
            return (
              <blockquote key={`${i}:${n}`} className="passage">
                <span className="src">{`${p.book_title ?? p.book_id} · chapter ${p.chapter_seq} · ${p.chunk_id}`}</span>
                {highlightQuote(p.text, quote).map((x, j) => x.mark ? <mark key={j}>{x.text}</mark> : <span key={j}>{x.text}</span>)}
              </blockquote>
            );
          }))}
        </div>
      ))}
    </div>
  );
}
