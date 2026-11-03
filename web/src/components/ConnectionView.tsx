import { EXPLORE } from "../messages";
import type { PathResult } from "../types";
import { QuoteChip } from "./QuoteChip";

export interface Connection { from: { id: string; name: string }; to: { id: string; name: string };
  status: "loading" | "found" | "none" | "timed_out" | "error" | "same"; result?: PathResult }

/** The path between two entities: one step per hop, each with its evidence. The path is also lit in the graph. */
export function ConnectionView({ connection, world, bookTitles, onSelect, onClear }:
  { connection: Connection; world?: string; bookTitles?: Map<string, string>; onSelect: (id: string) => void; onClear: () => void }) {
  const { from, to, status, result } = connection;
  const name = (id: string) => result?.nodes.find((n) => n.id === id)?.name ?? id;
  return (
    <section className="side-card" aria-label={EXPLORE.pathLabel}>
      <h3>{EXPLORE.pathTitle(from.name, to.name)}</h3>
      {status === "loading" && <p className="hint" role="status">{EXPLORE.connecting}</p>}
      {status === "none" && <p className="note" role="status">{EXPLORE.pathNone(from.name, to.name)}</p>}
      {status === "timed_out" && <p className="note" role="status">{EXPLORE.pathTimedOut}</p>}
      {status === "error" && <p className="note" role="status">{EXPLORE.pathFailed}</p>}
      {status === "same" && <p className="note" role="status">{EXPLORE.pathSame}</p>}
      {status === "found" && result && (<>
        <span className="mono">{EXPLORE.pathHops(result.hops.length)}</span>
        <ol className="plain hops">
          {result.hops.map((h, i) => (
            <li key={i}>
              <span className="beat-kind">{EXPLORE.hop(i + 1)}</span>{" "}
              <button className="linkbtn" type="button" aria-label={EXPLORE.openEntity(name(h.from))} onClick={() => onSelect(h.from)}>{name(h.from)}</button>
              <span className="preds">{h.predicates.map((p) => <span key={`${p.predicate}:${p.direction}`} className="pred">{EXPLORE.predicateLabel(p)}</span>)}</span>
              <button className="linkbtn" type="button" aria-label={EXPLORE.openEntity(name(h.to))} onClick={() => onSelect(h.to)}>{name(h.to)}</button>
              {h.evidence.map((q) => (
                <p key={`${q.chunk_id}:${q.quote}`} className="ev"><span className="qtext">{`“${q.quote}”`}</span>{" "}
                  <QuoteChip chunkId={q.chunk_id} quote={q.quote} bookId={q.book_id} chapter={q.chapter} world={world} bookTitles={bookTitles} /></p>
              ))}
            </li>
          ))}
        </ol>
      </>)}
      <button className="btn" type="button" onClick={onClear}>{EXPLORE.clearPath}</button>
    </section>
  );
}
