import type { HistoryItem } from "../history";

export function HistoryBar({ items, current, onPick }: { items: HistoryItem[]; current: string | null; onPick: (id: string) => void }) {
  if (!items.length) return null;
  return (
    <nav className="history" aria-label="Earlier questions">
      {items.map((h) => <button key={h.id} type="button" title={h.question} aria-current={h.id === current} onClick={() => onPick(h.id)}>{h.question}</button>)}
    </nav>
  );
}
