import { api } from "../api";
import { EXPLORE } from "../messages";
import type { Depth, EdgeDetail } from "../types";
import { AskButtons } from "./EntityPanel";
import { QuoteChip } from "./QuoteChip";
import { useLoad } from "./useLoad";

/** Everything stored about how two entities are related: each predicate, with its evidence in story order. */
export function EdgeView({ a, b, world, bookTitles, busy = false, onClose, onSelect, onAsk }:
  { a: string; b: string; world?: string; bookTitles?: Map<string, string>; busy?: boolean; onClose: () => void;
    onSelect: (id: string) => void; onAsk: (question: string, depth?: Depth) => void }) {
  const e = useLoad<EdgeDetail>(() => api.edge(a, b, world), [a, b, world]);
  return (
    <aside className="side-card" aria-label={EXPLORE.edgeLabel} onKeyDown={(ev) => { if (ev.key === "Escape") onClose(); }}>
      <button className="close" type="button" aria-label={EXPLORE.close} onClick={onClose}>×</button>
      {e.status === "loading" && <p className="hint">{EXPLORE.loading}</p>}
      {e.status === "error" && <p className="note">{e.message}</p>}
      {e.status === "ok" && (<>
        <span className="kind">{EXPLORE.edgeLabel}</span>
        <h3>
          <button className="linkbtn" type="button" aria-label={EXPLORE.openEntity(e.data.a.name)} onClick={() => onSelect(e.data.a.id)}>{e.data.a.name}</button>
          {" ↔ "}
          <button className="linkbtn" type="button" aria-label={EXPLORE.openEntity(e.data.b.name)} onClick={() => onSelect(e.data.b.id)}>{e.data.b.name}</button>
        </h3>
        {e.data.predicates.length > 0 && (
          <span className="preds">{e.data.predicates.map((p) => <span key={`${p.predicate}:${p.direction}`} className="pred">{EXPLORE.predicateLabel(p)}</span>)}</span>
        )}
        {e.data.order_note && <p className="hint">{e.data.order_note}</p>}
        {e.data.relationships.length === 0 ? <p className="hint">{EXPLORE.noEdges}</p> : (
          <ul className="plain rels">
            {e.data.relationships.map((r, i) => (
              <li key={i}>
                <strong>{EXPLORE.edgeStatement(r.subject.name, r.predicate, r.object.name)}</strong>
                <span className="mono">{r.position_label}</span>
                <div className="ev"><span className="qtext">{`“${r.quote}”`}</span>{" "}
                  <QuoteChip chunkId={r.chunk_id} quote={r.quote} bookId={r.book_id} chapter={r.chapter} world={world} bookTitles={bookTitles} /></div>
              </li>
            ))}
          </ul>
        )}
        <AskButtons busy={busy} onAsk={onAsk} questions={[{ label: EXPLORE.relationship(e.data.a.name, e.data.b.name) }]} />
      </>)}
    </aside>
  );
}
