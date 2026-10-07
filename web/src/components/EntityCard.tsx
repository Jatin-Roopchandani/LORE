import { useEffect, useState } from "react";
import { api, isError } from "../api";
import { errorMessage } from "../messages";
import type { ApiError, EntityCardData, SubgraphData } from "../types";

// Entity and subgraph errors never name a provider, so internals stay off here.
const say = (e: ApiError) => errorMessage(e, { showInternals: false }).text;
const pretty = (p: string) => p.toLowerCase().replace(/_/g, " ");

export function EntityCard({ id, onClose, onExpand }: { id: string; onClose: () => void; onExpand: (d: SubgraphData) => void }) {
  const [card, setCard] = useState<EntityCardData | null>(null);
  const [problem, setProblem] = useState("");
  const [expanding, setExpanding] = useState(false);
  useEffect(() => {
    let live = true;
    setCard(null); setProblem("");
    api.entity(id).then((r) => { if (!live) return; if (isError(r)) setProblem(say(r.error)); else setCard(r); });
    return () => { live = false; };
  }, [id]);
  const expand = async () => {
    setExpanding(true);
    const r = await api.subgraph(id);
    setExpanding(false);
    if (isError(r)) setProblem(say(r.error)); else onExpand(r);
  };
  const rels = card ? [...new Set([...card.edges_out.map((e) => `${pretty(e.predicate)} → ${e.object}`),
    ...card.edges_in.map((e) => `${e.subject} → ${pretty(e.predicate)}`)])].slice(0, 8) : [];
  return (
    <aside className="card" aria-label="Entity">
      <button className="close" type="button" aria-label="Close" onClick={onClose}>×</button>
      {problem && <p className="note">{problem}</p>}
      {card && (<>
        <span className="kind">{card.kind}</span>
        <h3>{card.name}</h3>
        {card.aliases.length > 0 && <span className="mono">{`Also: ${card.aliases.slice(0, 5).join(", ")}`}</span>}
        <ul>{rels.map((r) => <li key={r}>{r}</li>)}</ul>
        <span className="mono">{`${card.edges_out_total + card.edges_in_total} relationships in the graph`}</span>
        <button className="btn" type="button" disabled={expanding} onClick={expand}>{expanding ? "Expanding…" : "Expand neighbours"}</button>
      </>)}
    </aside>
  );
}
