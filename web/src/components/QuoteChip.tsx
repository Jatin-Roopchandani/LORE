import { useState } from "react";
import { api, isError } from "../api";
import { highlightQuote } from "../highlight";
import { EXPLORE } from "../messages";
import type { PassageText } from "../types";

type Opened = { status: "loading" } | { status: "error" } | { status: "ok"; passage: PassageText };

/** A chip that opens the passage a quote comes from (read from /api/passage, with the quote marked). */
export function QuoteChip({ chunkId, quote, bookId, chapter, world, bookTitles }:
  { chunkId: string; quote: string; bookId: string; chapter: number; world?: string; bookTitles?: Map<string, string> }) {
  const [opened, setOpened] = useState<Opened | null>(null);
  const [open, setOpen] = useState(false);
  const title = bookTitles?.get(bookId) ?? bookId;
  const toggle = () => {
    if (open) { setOpen(false); return; }
    setOpen(true);
    if (opened?.status === "ok") return;
    setOpened({ status: "loading" });
    api.passage(chunkId, world).then((r) => setOpened(isError(r) ? { status: "error" } : { status: "ok", passage: r }));
  };
  return (
    <>
      <button className="cite" type="button" aria-expanded={open} aria-label={EXPLORE.chipLabel(title, chapter)} onClick={toggle}>
        {EXPLORE.chip(title, chapter)}
      </button>
      {open && (
        <blockquote className="passage" role="region" aria-label={EXPLORE.chipLabel(title, chapter)}>
          {opened?.status === "ok" ? (<>
            <span className="src">{EXPLORE.passageSource(opened.passage.book_title, opened.passage.chapter)}</span>
            {highlightQuote(opened.passage.text, quote).map((x, j) => x.mark ? <mark key={j}>{x.text}</mark> : <span key={j}>{x.text}</span>)}
          </>) : opened?.status === "error" ? <span className="note">{EXPLORE.passageFailed}</span>
            : <span className="src">{EXPLORE.passageLoading}</span>}
        </blockquote>
      )}
    </>
  );
}
