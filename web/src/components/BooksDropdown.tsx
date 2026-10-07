import { useEffect, useRef, useState } from "react";
import type { Book } from "../types";

export function BooksDropdown({ books, hiddenBooks, onChange }: { books: Book[]; hiddenBooks: Set<string>; onChange: (s: Set<string>) => void }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const graphed = books.filter((b) => b.status === "graphed");
  const on = graphed.filter((b) => !hiddenBooks.has(b.id));
  const label = on.length === graphed.length ? `All books (${graphed.length})` : on.length === 0 ? "No books"
    : on.length === 1 ? on[0].title : `${on.length} of ${graphed.length} books`;
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") { setOpen(false); trigger.current?.focus(); } };
    document.addEventListener("mousedown", close); document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", close); document.removeEventListener("keydown", esc); };
  }, [open]);
  const shown = books.filter((b) => !q || `${b.title} ${b.author ?? ""}`.toLowerCase().includes(q.toLowerCase()));
  const toggle = (id: string, checked: boolean) => { const next = new Set(hiddenBooks); checked ? next.delete(id) : next.add(id); onChange(next); };
  return (
    <div className="pop" ref={root}>
      <button ref={trigger} className="tbtn" type="button" aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span>{label}</span><span className="caret" aria-hidden="true">▾</span>
      </button>
      {open && (
        <div className="menu books-menu">
          <input type="search" placeholder="Filter books…" aria-label="Filter books" value={q} onChange={(e) => setQ(e.target.value)} autoFocus />
          <div className="linkrow">
            <button type="button" onClick={() => onChange(new Set())}>Select all</button>
            <button type="button" onClick={() => onChange(new Set(graphed.map((b) => b.id)))}>Clear</button>
          </div>
          <ul className="booklist">
            {shown.map((b) => {
              const ok = b.status === "graphed";
              return (
                <li key={b.id}><label className={ok ? "" : "off"}>
                  <input type="checkbox" disabled={!ok} checked={ok && !hiddenBooks.has(b.id)} onChange={(e) => toggle(b.id, e.target.checked)} />
                  <span>{b.title}<span className="meta">{[b.author, ok ? `${b.entity_count ?? "?"} entities` : "not graphed yet"].filter(Boolean).join(" · ")}</span></span>
                </label></li>
              );
            })}
            {!shown.length && <li className="hint">No book matches “{q}”.</li>}
          </ul>
          <p className="hint">Only graphed books can be searched. The rest are still processing.</p>
        </div>
      )}
    </div>
  );
}
