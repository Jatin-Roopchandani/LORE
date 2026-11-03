import { useEffect, useMemo, useRef, useState } from "react";
import type { HistoryItem } from "../history";
import { filterHistory } from "../historyView";
import { QUICK } from "../messages";

const MAX = 12;
type Option = { id: string; label: string; ask: boolean; item?: HistoryItem };

/** Ctrl/Cmd+K: a centred search over past questions. Enter replays the highlighted one, or asks what was typed. */
export function QuickSwitcher({ items, onReplay, onAsk, onClose }:
  { items: HistoryItem[]; onReplay: (id: string) => void; onAsk: (q: string) => void; onClose: () => void }) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const options = useMemo<Option[]>(() => {
    const typed = query.trim();
    const past = filterHistory(items, query).slice(0, MAX).map((h) => ({ id: `quick-opt-${h.id}`, label: h.question, ask: false, item: h }));
    return typed ? [{ id: "quick-opt-ask", label: QUICK.ask(typed), ask: true }, ...past] : past;
  }, [items, query]);
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    input.current?.focus();
    return () => opener?.focus?.();
  }, []);
  const choose = (o: Option | undefined) => {
    if (!o) return;
    if (o.ask) onAsk(query.trim()); else if (o.item) onReplay(o.item.id);
    onClose();
  };
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); onClose(); }
    else if (e.key === "ArrowDown" && options.length) { e.preventDefault(); setActive((a) => (a + 1) % options.length); }
    else if (e.key === "ArrowUp" && options.length) { e.preventDefault(); setActive((a) => (a - 1 + options.length) % options.length); }
    else if (e.key === "Enter") { e.preventDefault(); choose(options[Math.min(active, options.length - 1)]); }
    else if (e.key === "Tab") e.preventDefault(); // the box is the only stop
  };
  const cur = options[Math.min(active, options.length - 1)];
  return (
    <div className="quick-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="quick" role="dialog" aria-modal="true" aria-label={QUICK.title} onKeyDown={onKeyDown}>
        <input ref={input} type="text" role="combobox" aria-expanded="true" aria-controls="quick-list" aria-autocomplete="list"
          aria-activedescendant={cur?.id} aria-label={QUICK.search} placeholder={QUICK.placeholder} autoComplete="off"
          value={query} onChange={(e) => { setQuery(e.target.value); setActive(0); }} />
        <ul id="quick-list" role="listbox" aria-label={QUICK.list}>
          {options.map((o, i) => (
            <li key={o.id} id={o.id} role="option" aria-selected={o === cur} className={o.ask ? "ask" : undefined} title={o.label}
              onMouseEnter={() => setActive(i)} onMouseDown={(e) => e.preventDefault()} onClick={() => choose(o)}>{o.label}</li>
          ))}
        </ul>
        {options.length === 0 && <p className="hint" role="status">{QUICK.empty}</p>}
      </div>
    </div>
  );
}
