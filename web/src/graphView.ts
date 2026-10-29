import type { AskState } from "./askReducer";
import { WORLDS } from "./messages";
import { edgeKey, type Edge, type SubgraphData, type WorldRef } from "./types";

export type NodeState = "shown" | "dim";
export type LinkState = "shown" | "cut" | "cited" | "faint";
// `world`: set only in the all-worlds view, where each world is its own cluster.
export interface ViewNode { id: string; label: string; kind: string; books: string[]; seedPart: number | null; state: NodeState; expanded: boolean; world?: WorldRef }
// `both`: the pair has relationships in each direction, merged into one link drawn without an arrowhead.
export interface ViewLink { id: string; source: string; target: string; label: string; count: number; chunkIds: string[];
  part: number | null; state: LinkState; hot: boolean; books: string[]; both: boolean }
// `overview`: the library overview is drawn (before any question); its own links are faint and unlabelled.
export interface GraphView { nodes: ViewNode[]; links: ViewLink[]; shownEdges: number; totalEdges: number; overview?: boolean }
export type KindMap = Record<string, { kind: string; books: string[]; world?: WorldRef }>;
export interface ViewOptions { hiddenBooks: Set<string>; hoverChunks: string[]; expansions: SubgraphData[]; kinds?: KindMap;
  // The library overview, drawn before any question: like an expansion, but it marks no node expanded.
  overview?: SubgraphData | null }

const pretty = (p: string) => p.toLowerCase().replace(/_/g, " ");
const pairKey = (a: string, b: string) => (a < b ? `${a}<->${b}` : `${b}<->${a}`);
const topOf = (counts: Map<string, number>) =>
  [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))[0]?.[0] ?? "";
const times = (n: number) => (n > 1 ? ` ×${n}` : "");

// One label for a pair: "knows ×2" one way; "interacts with ×8 · ×3" both ways with the same
// top relationship; "employs / works for ×2" when each way has its own.
function pairLabel(fwd: { top: string; n: number }, back: { top: string; n: number } | null): string {
  if (!back) return pretty(fwd.top) + times(fwd.n);
  if (fwd.top === back.top) return `${pretty(fwd.top)} ×${fwd.n} · ×${back.n}`;
  return `${pretty(fwd.top)}${times(fwd.n)} / ${pretty(back.top)}${times(back.n)}`;
}

/** A node's name as drawn: in the all-worlds view it names its world too, so same-named characters tell apart. */
export const nodeLabel = (n: ViewNode) => (n.world ? WORLDS.nodeLabel(n.label, n.world.name) : n.label);

/** The worlds present in a view, in id order: what the all-worlds legend lists. */
export const worldsIn = (view: GraphView): WorldRef[] =>
  [...new Map(view.nodes.flatMap((n) => (n.world ? [[n.world.id, n.world] as const] : []))).values()].sort((a, b) => a.id - b.id);

/** The node ids in a view whose kind is not known yet: what to ask /api/entities for. */
export const unknownKinds = (view: GraphView) => view.nodes.filter((n) => n.kind === "Other").map((n) => n.id);

export function deriveGraph(state: AskState, options: ViewOptions): GraphView {
  const nodes = new Map<string, ViewNode>();
  const touch = (id: string, label: string, kind = "Other", books: string[] = [], world?: WorldRef) => {
    const key = id || label;
    const n = nodes.get(key) ?? { id: key, label, kind, books: [], seedPart: null, state: "shown" as NodeState, expanded: false };
    if (n.kind === "Other" && kind !== "Other") n.kind = kind;
    for (const b of books) if (!n.books.includes(b)) n.books.push(b);
    if (world && !n.world) n.world = world;
    nodes.set(key, n);
    return n;
  };
  state.seeds.forEach((list, part) => list?.forEach((s) => {
    const n = touch(s.id, s.name, s.kind, s.books);
    if (n.seedPart === null) n.seedPart = part;
  }));

  // Grouped by unordered pair: a→b and b→a become one link.
  const groups = new Map<string, { source: string; target: string; rows: Edge[]; part: number | null }>();
  state.edges.forEach((pe, part) => pe?.edges.forEach((e) => {
    const source = touch(e.subject_id, e.subject, undefined, [e.book_id]).id;
    const target = touch(e.object_id, e.object, undefined, [e.book_id]).id;
    const id = pairKey(source, target);
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
    const dir = (fromSource: boolean) => {
      const rows = g.rows.filter((r) => ((r.subject_id || r.subject) === g.source) === fromSource);
      const counts = new Map<string, number>();
      rows.forEach((r) => counts.set(r.predicate, (counts.get(r.predicate) ?? 0) + 1));
      return rows.length ? { top: topOf(counts), n: rows.length } : null;
    };
    const fwd = dir(true), back = dir(false);
    const chunkIds = [...new Set(g.rows.map((r) => r.chunk_id))];
    const books = [...new Set(g.rows.map((r) => r.book_id))];
    // Over the union of both directions, so the merged link takes the stronger state (cited > shown > cut > faint).
    let linkState: LinkState = "shown";
    if (state.result) linkState = chunkIds.some((c) => cited.has(c)) ? "cited" : "faint";
    else if (kept) linkState = g.rows.some((r) => kept.has(edgeKey(r))) ? "shown" : "cut";
    if (allHidden(books)) linkState = "faint";
    links.push({ id, source: g.source, target: g.target, label: pairLabel((fwd ?? back)!, fwd && back),
      count: g.rows.length, chunkIds, part: g.part, state: linkState,
      hot: chunkIds.some((c) => options.hoverChunks.includes(c)), books, both: !!(fwd && back) });
  }

  const extra = new Map<string, { source: string; target: string; fwd: Map<string, number>; back: Map<string, number>; nf: number; nb: number; quiet: boolean }>();
  const subs = options.overview ? [options.overview, ...options.expansions] : options.expansions;
  for (const sub of subs) {
    for (const n of sub.nodes) touch(n.id, n.name, n.kind, n.books, n.world);
    const root = sub.nodes[0]?.id;
    if (sub !== options.overview && root && nodes.has(root)) nodes.get(root)!.expanded = true;
    for (const l of sub.edges) {
      const id = pairKey(l.subject_id, l.object_id);
      if (groups.has(id)) continue; // the search's link wins
      const x = extra.get(id) ?? { source: l.subject_id, target: l.object_id, fwd: new Map(), back: new Map(), nf: 0, nb: 0, quiet: true };
      // Quiet only while no expansion has drawn this pair: an expansion's links keep labels and normal styling.
      if (sub !== options.overview) x.quiet = false;
      extra.set(id, x);
      const forward = l.subject_id === x.source;
      if (forward ? x.nf : x.nb) continue; // the same link from an earlier expansion
      const into = forward ? x.fwd : x.back;
      for (const [p, n] of Object.entries(l.predicates)) into.set(p, n);
      if (forward) x.nf = l.count; else x.nb = l.count;
      extra.set(id, x);
    }
  }
  for (const [id, x] of extra) {
    const fwd = x.nf ? { top: topOf(x.fwd), n: x.nf } : null;
    const back = x.nb ? { top: topOf(x.back), n: x.nb } : null;
    const quiet = x.quiet && !!options.overview;
    links.push({ id, source: x.source, target: x.target, label: quiet ? "" : pairLabel((fwd ?? back)!, fwd && back),
      count: x.nf + x.nb, chunkIds: [], part: null, state: quiet ? "faint" : "shown", hot: false, books: [], both: !!(fwd && back) });
  }

  for (const [id, k] of Object.entries(options.kinds ?? {})) {
    const n = nodes.get(id);
    if (!n) continue;
    if (n.kind === "Other" && k.kind) n.kind = k.kind;
    for (const b of k.books) if (!n.books.includes(b)) n.books.push(b);
    if (k.world && !n.world) n.world = k.world;
  }

  for (const n of nodes.values()) n.state = allHidden(n.books) ? "dim" : "shown";
  return {
    nodes: [...nodes.values()], links, overview: !!options.overview,
    shownEdges: state.edges.reduce((a, pe) => a + (pe?.edges.length ?? 0), 0),
    totalEdges: state.edges.reduce((a, pe) => a + (pe?.total ?? 0), 0),
  };
}
