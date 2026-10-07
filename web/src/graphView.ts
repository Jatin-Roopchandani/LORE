import type { AskState } from "./askReducer";
import { edgeKey, type Edge, type SubgraphData } from "./types";

export type NodeState = "shown" | "dim";
export type LinkState = "shown" | "cut" | "cited" | "faint";
export interface ViewNode { id: string; label: string; kind: string; books: string[]; seedPart: number | null; state: NodeState; expanded: boolean }
export interface ViewLink { id: string; source: string; target: string; label: string; count: number; chunkIds: string[];
  part: number | null; state: LinkState; hot: boolean; books: string[] }
export interface GraphView { nodes: ViewNode[]; links: ViewLink[]; shownEdges: number; totalEdges: number }
export interface ViewOptions { hiddenBooks: Set<string>; hoverChunk: string | null; expansions: SubgraphData[] }

const pretty = (p: string) => p.toLowerCase().replace(/_/g, " ");

export function deriveGraph(state: AskState, options: ViewOptions): GraphView {
  const nodes = new Map<string, ViewNode>();
  const touch = (id: string, label: string, kind = "Other", books: string[] = []) => {
    const key = id || label;
    const n = nodes.get(key) ?? { id: key, label, kind, books: [], seedPart: null, state: "shown" as NodeState, expanded: false };
    if (n.kind === "Other" && kind !== "Other") n.kind = kind;
    for (const b of books) if (!n.books.includes(b)) n.books.push(b);
    nodes.set(key, n);
    return n;
  };
  state.seeds.forEach((list, part) => list?.forEach((s) => {
    const n = touch(s.id, s.name, s.kind, s.books);
    if (n.seedPart === null) n.seedPart = part;
  }));

  const groups = new Map<string, { source: string; target: string; rows: Edge[]; part: number | null }>();
  state.edges.forEach((pe, part) => pe?.edges.forEach((e) => {
    const source = touch(e.subject_id, e.subject, undefined, [e.book_id]).id;
    const target = touch(e.object_id, e.object, undefined, [e.book_id]).id;
    const id = `${source}->${target}`;
    const g = groups.get(id) ?? { source, target, rows: [], part };
    g.rows.push(e);
    groups.set(id, g);
  }));

  const cited = new Set<string>();
  if (state.result) {
    const byN = new Map(state.result.passages.map((p) => [p.n, p.chunk_id]));
    for (const c of state.result.claims) { const chunk = byN.get(c.passage); if (chunk) cited.add(chunk); }
  }
  const kept = state.evidence ? new Set(state.evidence.edgeKeys) : null;
  const hidden = options.hiddenBooks;
  const allHidden = (books: string[]) => books.length > 0 && books.every((b) => hidden.has(b));

  const links: ViewLink[] = [];
  for (const [id, g] of groups) {
    const counts = new Map<string, number>();
    g.rows.forEach((r) => counts.set(r.predicate, (counts.get(r.predicate) ?? 0) + 1));
    const top = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))[0][0];
    const chunkIds = [...new Set(g.rows.map((r) => r.chunk_id))];
    const books = [...new Set(g.rows.map((r) => r.book_id))];
    let linkState: LinkState = "shown";
    if (state.result) linkState = chunkIds.some((c) => cited.has(c)) ? "cited" : "faint";
    else if (kept) linkState = g.rows.some((r) => kept.has(edgeKey(r))) ? "shown" : "cut";
    if (allHidden(books)) linkState = "faint";
    links.push({ id, source: g.source, target: g.target, label: pretty(top) + (g.rows.length > 1 ? ` ×${g.rows.length}` : ""),
      count: g.rows.length, chunkIds, part: g.part, state: linkState,
      hot: options.hoverChunk !== null && chunkIds.includes(options.hoverChunk), books });
  }

  for (const sub of options.expansions) {
    for (const n of sub.nodes) touch(n.id, n.name, n.kind, n.books);
    const root = sub.nodes[0]?.id;
    if (root && nodes.has(root)) nodes.get(root)!.expanded = true;
    for (const l of sub.edges) {
      const id = `${l.subject_id}->${l.object_id}`;
      if (groups.has(id) || links.some((x) => x.id === id)) continue;
      const top = Object.entries(l.predicates).sort((a, b) => b[1] - a[1])[0]?.[0] ?? "";
      links.push({ id, source: l.subject_id, target: l.object_id, label: pretty(top) + (l.count > 1 ? ` ×${l.count}` : ""),
        count: l.count, chunkIds: [], part: null, state: "shown", hot: false, books: [] });
    }
  }

  for (const n of nodes.values()) n.state = allHidden(n.books) ? "dim" : "shown";
  return {
    nodes: [...nodes.values()], links,
    shownEdges: state.edges.reduce((a, pe) => a + (pe?.edges.length ?? 0), 0),
    totalEdges: state.edges.reduce((a, pe) => a + (pe?.total ?? 0), 0),
  };
}
