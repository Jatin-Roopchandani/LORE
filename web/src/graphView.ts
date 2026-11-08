import type { AskState } from "./askReducer";
import { EDGE_LABEL, WORLDS } from "./messages";
import { edgeKey, type Edge, type SubgraphData, type WorldRef } from "./types";

export type NodeState = "shown" | "dim";
export type LinkState = "shown" | "cut" | "cited" | "faint";
// `world`: set only in the all-worlds view, where each world is its own cluster.
export interface ViewNode { id: string; label: string; kind: string; books: string[]; seedPart: number | null; state: NodeState; expanded: boolean; world?: WorldRef;
  // The entity open in the panel, and a node on the highlighted path.
  selected?: boolean; onPath?: boolean }
// `both`: the pair has relationships in each direction, merged into one link drawn without an arrowhead.
export interface ViewLink { id: string; source: string; target: string; label: string; count: number; chunkIds: string[];
  part: number | null; state: LinkState; hot: boolean; books: string[]; both: boolean;
  // Every part whose search found this pair (the answer's own links only; `part` is the first of them).
  parts?: number[];
  // The link the edge view shows, and a link on the highlighted path.
  picked?: boolean; onPath?: boolean }
// `overview`: the library overview is drawn (before any question); its own links are faint, and their
// labels show only when zoomed in or on the node in focus (GraphCanvas, edgeLabel.ts).
// `hiddenByFilters`: how many nodes the kind and book filters removed.
// `hiddenUncategorized`: how many relationships were left out as "uncategorized" (the extractor's catch-all).
// `focus`: set on a big answer's focused view (graphFocus.ts): how many nodes the full view has.
export interface GraphView { nodes: ViewNode[]; links: ViewLink[]; shownEdges: number; totalEdges: number; overview?: boolean; hiddenByFilters?: number;
  hiddenUncategorized?: number; focus?: { total: number } }
export type KindMap = Record<string, { kind: string; books: string[]; world?: WorldRef }>;
export interface ViewOptions { hiddenBooks: Set<string>; hoverChunks: string[]; expansions: SubgraphData[]; kinds?: KindMap;
  // The library overview, drawn before any question: like an expansion, but it marks no node expanded.
  overview?: SubgraphData | null;
  // The entity open in the panel; the pair whose edge view is open; the path being shown (node ids, and its hops' ends).
  selected?: string | null; edge?: { a: string; b: string } | null; path?: { nodes: string[]; hops: { from: string; to: string }[] } | null;
  // Kinds and books to hide: their nodes go, and so do the links to them. The selected node and the path stay.
  filters?: { kinds: ReadonlySet<string>; books: ReadonlySet<string> };
  // Leave out "uncategorized" relationships (the answer view hides them unless the filter chip shows them): a pair
  // with only those has no link, and a merged label keeps just its named relationships.
  hideUncategorized?: boolean }

/** The extractor's catch-all predicate: a relationship it could not name. */
export const UNCATEGORIZED = "UNCATEGORIZED";

const pretty = (p: string) => p.toLowerCase().replace(/_/g, " ");
const pairKey = (a: string, b: string) => (a < b ? `${a}<->${b}` : `${b}<->${a}`);
const ranked = (counts: Map<string, number>) => [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
const topOf = (counts: Map<string, number>) => ranked(counts)[0]?.[0] ?? "";

// One direction of a pair: its relationships by name, most common first.
type Way = { counts: Map<string, number>; n: number };
// The relationships one way: the most common by name with its own count, and "+k" for the k others
// ("opposes ×3 +2": three OPPOSES and two other relationships). The edge view lists them all.
function wayLabel(w: Way): string {
  const [name, n] = ranked(w.counts)[0] ?? ["", 0];
  return EDGE_LABEL.top(EDGE_LABEL.one(pretty(name), n), w.n - n);
}
// One label for a pair: one way as above; both ways with the same single relationship "interacts with ×8 · ×3";
// otherwise each way's label, "employs / works for ×2".
function pairLabel(fwd: Way, back: Way | null): string {
  if (!back) return wayLabel(fwd);
  if (fwd.counts.size === 1 && back.counts.size === 1 && topOf(fwd.counts) === topOf(back.counts))
    return EDGE_LABEL.mutual(pretty(topOf(fwd.counts)), fwd.n, back.n);
  return EDGE_LABEL.ways(wayLabel(fwd), wayLabel(back));
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
    const n = nodes.get(key) ?? { id: key, label, kind, books: [], seedPart: null, state: "shown" as NodeState, expanded: false, selected: false, onPath: false };
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
  const groups = new Map<string, { source: string; target: string; rows: Edge[]; part: number | null; parts: number[] }>();
  const hideUnc = !!options.hideUncategorized;
  let hiddenUncategorized = 0;
  state.edges.forEach((pe, part) => pe?.edges.forEach((e) => {
    if (hideUnc && e.predicate === UNCATEGORIZED) { hiddenUncategorized++; return; }
    const source = touch(e.subject_id, e.subject, undefined, [e.book_id]).id;
    const target = touch(e.object_id, e.object, undefined, [e.book_id]).id;
    const id = pairKey(source, target);
    const g = groups.get(id) ?? { source, target, rows: [], part, parts: [] };
    g.rows.push(e);
    if (!g.parts.includes(part)) g.parts.push(part);
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
    const dir = (fromSource: boolean): Way | null => {
      const rows = g.rows.filter((r) => ((r.subject_id || r.subject) === g.source) === fromSource);
      const counts = new Map<string, number>();
      rows.forEach((r) => counts.set(r.predicate, (counts.get(r.predicate) ?? 0) + 1));
      return rows.length ? { counts, n: rows.length } : null;
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
      count: g.rows.length, chunkIds, part: g.part, parts: g.parts, state: linkState,
      hot: chunkIds.some((c) => options.hoverChunks.includes(c)), books, both: !!(fwd && back), picked: false, onPath: false });
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
      let count = l.count;
      for (const [p, n] of Object.entries(l.predicates)) {
        if (hideUnc && p === UNCATEGORIZED) { count -= n; continue; }
        into.set(p, n);
      }
      if (count <= 0 || into.size === 0) continue;   // only uncategorized that way: no link that way
      if (forward) x.nf = count; else x.nb = count;
      extra.set(id, x);
    }
  }
  for (const [id, x] of extra) {
    if (!x.nf && !x.nb) continue;
    const fwd = x.nf ? { counts: x.fwd, n: x.nf } : null;
    const back = x.nb ? { counts: x.back, n: x.nb } : null;
    const quiet = x.quiet && !!options.overview;
    links.push({ id, source: x.source, target: x.target, label: pairLabel((fwd ?? back)!, fwd && back),
      count: x.nf + x.nb, chunkIds: [], part: null, state: quiet ? "faint" : "shown", hot: false, books: [], both: !!(fwd && back), picked: false, onPath: false });
  }

  for (const [id, k] of Object.entries(options.kinds ?? {})) {
    const n = nodes.get(id);
    if (!n) continue;
    if (n.kind === "Other" && k.kind) n.kind = k.kind;
    for (const b of k.books) if (!n.books.includes(b)) n.books.push(b);
    if (k.world && !n.world) n.world = k.world;
  }

  for (const n of nodes.values()) n.state = allHidden(n.books) ? "dim" : "shown";

  // The highlights: the open entity, the open edge and the path (a link's id is its pair, whichever way it points).
  const pathNodes = new Set(options.path?.nodes ?? []);
  const pathPairs = new Set((options.path?.hops ?? []).map((h) => pairKey(h.from, h.to)));
  const edgePair = options.edge ? pairKey(options.edge.a, options.edge.b) : null;
  for (const n of nodes.values()) { n.selected = n.id === options.selected; n.onPath = pathNodes.has(n.id); }
  for (const l of links) { l.picked = l.id === edgePair; l.onPath = pathPairs.has(l.id); }

  // The filters: a node is hidden by its kind, or when every book it is in is hidden.
  let shownNodes = [...nodes.values()], shownLinks = links, hiddenByFilters = 0;
  const f = options.filters;
  if (f && (f.kinds.size > 0 || f.books.size > 0)) {
    const keep = (n: ViewNode) => n.selected || n.onPath
      || !(f.kinds.has(n.kind) || (n.books.length > 0 && n.books.every((b) => f.books.has(b))));
    shownNodes = shownNodes.filter(keep);
    hiddenByFilters = nodes.size - shownNodes.length;
    const ids = new Set(shownNodes.map((n) => n.id));
    shownLinks = links.filter((l) => ids.has(l.source) && ids.has(l.target)
      && (l.picked || l.onPath || !(l.books.length > 0 && l.books.every((b) => f.books.has(b)))));
  }
  return {
    nodes: shownNodes, links: shownLinks, overview: !!options.overview, hiddenByFilters, hiddenUncategorized,
    shownEdges: state.edges.reduce((a, pe) => a + (pe?.edges.length ?? 0), 0),
    totalEdges: state.edges.reduce((a, pe) => a + (pe?.total ?? 0), 0),
  };
}
