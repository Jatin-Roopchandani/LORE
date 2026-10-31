import { useState } from "react";
import { highlightQuote } from "../highlight";
import { UI } from "../messages";
import type { ProseSentence, SearchResult } from "../types";

type Kind = "fact" | "reading" | "counter" | "limit";
// Answers saved before judgements folded into readings may still say "judgement"; it is a reading now.
const kindOf = (s: ProseSentence): Kind => {
  const k = s.kind as string | undefined;
  return k === "reading" || k === "judgement" ? "reading" : k === "counter" ? "counter" : k === "limit" ? "limit" : "fact";
};
const NOTE = { reading: UI.readingNote, counter: UI.counterNote, limit: UI.limitNote } as const;

// `prose` shows other sentences than the result's own (a section's, or the overview's), cited from the result's
// claims and passages. `showMissing` is off for those: the unanswered line belongs to the whole answer.
export function Prose({ result, prose: given, showMissing = true, onHoverChunks }:
  { result: SearchResult; prose?: ProseSentence[]; showMissing?: boolean; onHoverChunks: (chunks: string[]) => void }) {
  const [open, setOpen] = useState<string | null>(null);   // "<sentence>:<passage>"
  const byN = new Map(result.passages.map((p) => [p.n, p]));
  const prose = given ?? result.prose ?? [];
  // A change between facts, readings, the other side and limits starts a paragraph, so each run can carry its label.
  const paragraphs: { s: ProseSentence; i: number }[][] = [[]];
  prose.forEach((s, i) => {
    const last = paragraphs[paragraphs.length - 1];
    const changed = last.length > 0 && kindOf(last[last.length - 1].s) !== kindOf(s);
    if ((s.new_paragraph || changed) && last.length) paragraphs.push([]);
    paragraphs[paragraphs.length - 1].push({ s, i });
  });
  // Whole parts the passages do not answer, then aspects within a part: one line.
  const missing = showMissing ? [...(result.unanswered ?? []), ...(result.gaps ?? [])] : [];
  return (
    <div className="prose-answer">
      {paragraphs.map((para, pi) => (
        <div key={pi}>
          <p className={para[0] && kindOf(para[0].s) !== "fact" ? `prose ${kindOf(para[0].s)}` : "prose"}>
            {para[0] && kindOf(para[0].s) !== "fact" && (
              <span className="kind-label">{UI.kindLabel[kindOf(para[0].s) as Exclude<Kind, "fact">]}</span>
            )}
            {para.map(({ s, i }) => {
              const chunks = s.passages.map((n) => byN.get(n)?.chunk_id).filter(Boolean) as string[];
              const kind = kindOf(s);
              const note = kind === "fact" ? undefined : NOTE[kind];
              return (
                <span key={i} className={kind === "fact" ? "sentence" : `sentence ${kind}`}
                  title={note}
                  onMouseEnter={() => onHoverChunks(chunks)} onMouseLeave={() => onHoverChunks([])}>
                  {s.text}{" "}
                  {note && <span className="visually-hidden">{note}</span>}
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
      {missing.length > 0 && (
        <p className="unanswered">{`${UI.unanswered} ${missing.join("; ")}`}</p>
      )}
    </div>
  );
}
