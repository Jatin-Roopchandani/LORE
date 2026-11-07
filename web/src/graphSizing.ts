import { nodeLabel, type GraphView, type ViewNode } from "./graphView";

// How big the canvas draws things, in screen pixels at the fitted zoom (sigma sizes items by the screen).
// Shared by GraphCanvas (what it draws), the layout (what it keeps apart) and graphMetrics (what it measures).
export const NODE_LABEL_PX = 12;
// Sigma draws a node's name this far right of its dot's rim.
export const LABEL_GAP = 3;

/** A node's name as the canvas draws it. */
export const drawnLabel = (n: ViewNode) => nodeLabel({ ...n, label: n.label.replace(/^Mr\. /, "") });

// Public Sans averages about 0.56 em a character; the layout has no canvas to measure with.
export const estimateWidth = (text: string, px = NODE_LABEL_PX) => Math.ceil(text.length * px * 0.56);

// An answer graph counts as large from this many nodes: it is laid out by density (layout.ts) and its
// nodes are sized by their links. Smaller answers keep the layout and sizes they always had.
export const LARGE_GRAPH = 40;
export const isLarge = (view: GraphView) => !view.overview && view.nodes.length >= LARGE_GRAPH;

/** Each node's links, counting a pair once. */
export function degrees(view: GraphView): Map<string, number> {
  const d = new Map<string, number>();
  for (const l of view.links) if (l.source !== l.target) for (const e of [l.source, l.target]) d.set(e, (d.get(e) ?? 0) + 1);
  return d;
}

// Node radius in screen pixels. The overview grows plain nodes with their links (7 to 12), so the main cast
// stands out; a small answer draws seeds and highlighted nodes at 11 and the rest at 7. A large answer grows
// every node with its links (a hub up to 18), so the hubs read at a glance and nothing shrinks to a dot.
export function nodeSizes(view: GraphView): Map<string, number> {
  const large = isLarge(view);
  const degree = view.overview || large ? degrees(view) : new Map<string, number>();
  const maxDegree = Math.max(1, ...degree.values());
  const out = new Map<string, number>();
  for (const n of view.nodes) {
    const seed = n.seedPart !== null, marked = !!n.selected || !!n.onPath;
    const share = Math.sqrt((degree.get(n.id) ?? 0) / maxDegree);
    out.set(n.id, large ? (seed || marked ? 10 : 8) + 8 * share : seed || marked ? 11 : 7 + 5 * share);
  }
  return out;
}

// The order the canvas hands edges to sigma, which draws their labels in that order, each placed clear of those
// before it. A large graph puts the highlighted, hovered and cited first, so they get the best spots.
const RANK = { cited: 2, shown: 3, cut: 4, faint: 4 } as const;
export function edgeOrder(view: GraphView): GraphView["links"] {
  if (!isLarge(view)) return view.links;
  const rank = (l: GraphView["links"][number]) => (l.onPath || l.picked ? 0 : l.hot ? 1 : RANK[l.state]);
  return view.links.map((l, i) => ({ l, i })).sort((a, b) => rank(a.l) - rank(b.l) || a.i - b.i).map((x) => x.l);
}
