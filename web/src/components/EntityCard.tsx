import { useEffect, useRef, useState } from "react";
import { api, isError } from "../api";
import { errorMessage } from "../messages";
import type { ApiError, Edge, EntityCardData, SubgraphData } from "../types";

// Entity and subgraph errors never name a provider, so internals stay off here.
const say = (e: ApiError) => errorMessage(e, { showInternals: false }).text;
const pretty = (p: string) => p.toLowerCase().replace(/_/g, " ");

export interface EntityCardProps {
  id: string; onClose: () => void; onExpand: (d: SubgraphData) => void;
  answerEdges?: Edge[]; seed?: boolean; bookTitles?: Map<string, string>; world?: string;
}

export function EntityCard({ id, onClose, onExpand, answerEdges = [], seed = false, bookTitles, world }: EntityCardProps) {
  const [card, setCard] = useState<EntityCardData | null>(null);
  const [problem, setProblem] = useState("");
  const [expanding, setExpanding] = useState(false);
  // The id an expand result must still match when it lands; null once the card is gone.
  const current = useRef<string | null>(id);
  useEffect(() => { current.current = id; return () => { current.current = null; }; }, [id]);
  useEffect(() => {
    let live = true;
    setCard(null); setProblem(""); setExpanding(false);
    api.entity(id, world).then((r) => { if (!live) return; if (isError(r)) setProblem(say(r.error)); else setCard(r); });
    return () => { live = false; };
  }, [id, world]);
  const expand = async () => {
    const asked = id;
    setExpanding(true);
    const r = await api.subgraph(asked, world);
    if (current.current !== asked) return; // closed, moved to another entity, or a new ask
    setExpanding(false);
    if (isError(r)) setProblem(say(r.error)); else onExpand(r);
  };
  const counts = new Map<string, number>();
  for (const e of answerEdges) {
    const other = e.subject_id === id ? e.object : e.subject;
    const k = `${pretty(e.predicate)} · ${other}`;
    counts.set(k, (counts.get(k) ?? 0) + 1);
  }
  const rels = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 6);
  const chapters = [...new Set(answerEdges.map((e) => e.chapter_seq))].sort((a, b) => a - b);
  const books = card ? card.books.map((b) => bookTitles?.get(b) ?? b).join(", ") : "";
  return (
    <aside className="card" aria-label="Entity">
      <button className="close" type="button" aria-label="Close" onClick={onClose}>×</button>
      {problem && <p className="note">{problem}</p>}
      {card && (<>
        <span className="kind">{card.kind}{seed ? " · starting entity" : ""}</span>
        <h3>{card.name}</h3>
        {card.world && <span className="mono">{card.world.name}</span>}
        <span className="mono">{books || "—"}</span>
        <div>
          <strong style={{ fontSize: 12 }}>In this answer</strong>
          <ul>{rels.length ? rels.map(([k, n]) => <li key={k}>{k}{n > 1 && <span className="mono">{` ×${n}`}</span>}</li>)
            : <li>No relationships in this answer</li>}</ul>
        </div>
        <span className="mono">{`Chapters: ${chapters.join(", ") || "—"}`}</span>
        <span className="mono">{`${card.edges_out_total + card.edges_in_total} relationships in the graph`}</span>
        <button className="btn" type="button" disabled={expanding} onClick={expand}>{expanding ? "Expanding…" : "Expand neighbours"}</button>
      </>)}
    </aside>
  );
}
