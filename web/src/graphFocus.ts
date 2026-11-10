// A big answer's focused view: past FOCUS_FROM nodes the whole answer graph is too much to read (a deep answer
// brings 80-110 entities, hubs with spokes everywhere), so the answer view shows by default only what the answer
// rests on, and a header control switches to the full graph and back (GraphPane).
//
// What the focused view keeps:
//  - the links the answer cites (their evidence passage is cited), and the path, the open edge and any expansion
//    the reader asked for (links that came from no search part), with their ends;
//  - every part's seed entities, the open entity and the path's nodes;
//  - for context, a few of each seed's strongest neighbours (by their links among the answer's own), taken a round
//    at a time over the seeds until FOCUS_TARGET nodes or FOCUS_NEIGHBOURS each;
// and every link whose two ends are both kept. Deterministic: ties go by id. A smaller answer is drawn whole.
import type { GraphView, ViewLink } from "./graphView";
import { LARGE_GRAPH } from "./graphSizing";

export const FOCUS_FROM = LARGE_GRAPH;
export const FOCUS_TARGET = 32;
export const FOCUS_NEIGHBOURS = 2;

// A link the answer leans on, or one the reader brought in: always kept.
const anchored = (l: ViewLink) => l.state === "cited" || !!l.picked || !!l.onPath || l.chunkIds.length === 0;

/** Whether a view is big enough to be focused. */
export const focusable = (view: GraphView) => !view.overview && view.nodes.length >= FOCUS_FROM;

/** The focused view of a big answer graph (`focus.total` = the full view's nodes); any other view as it is. */
export function focusGraph(view: GraphView): GraphView {
  if (!focusable(view)) return view;
  const keep = new Set<string>();
  for (const n of view.nodes) if (n.seedPart !== null || n.selected || n.onPath || n.expanded) keep.add(n.id);
  for (const l of view.links) if (anchored(l)) { keep.add(l.source); keep.add(l.target); }

  // Degree within the answer's own links (not expansions), counting a pair once.
  const degree = new Map<string, number>();
  const near = new Map<string, string[]>();
  for (const l of view.links) {
    if (l.source === l.target) continue;
    for (const [a, b] of [[l.source, l.target], [l.target, l.source]]) {
      near.set(a, [...(near.get(a) ?? []), b]);
      if (l.chunkIds.length > 0) degree.set(a, (degree.get(a) ?? 0) + 1);
    }
  }
  const byStrength = (a: string, b: string) => (degree.get(b) ?? 0) - (degree.get(a) ?? 0) || (a < b ? -1 : a > b ? 1 : 0);
  // Seeds in part order, the best-linked first within a part.
  const seeds = view.nodes.filter((n) => n.seedPart !== null)
    .sort((a, b) => a.seedPart! - b.seedPart! || byStrength(a.id, b.id)).map((n) => n.id);
  const candidates = new Map(seeds.map((s) => [s, [...new Set(near.get(s) ?? [])].sort(byStrength)]));
  const pairs = new Set<string>();
  const pair = (a: string, b: string) => (a < b ? `${a}\n${b}` : `${b}\n${a}`);
  for (let round = 0; round < FOCUS_NEIGHBOURS && keep.size < FOCUS_TARGET; round++) {
    for (const s of seeds) {
      if (keep.size >= FOCUS_TARGET) break;
      const next = candidates.get(s)!.find((m) => !keep.has(m));
      if (next) { keep.add(next); pairs.add(pair(s, next)); }
    }
  }
  // The links drawn: the anchored ones and each neighbour's link to its seed. Every other link between kept nodes is
  // context the full view has; here it would only cross the picture. A kept node left with no link gets its
  // strongest one to another kept node, so nothing floats loose that has a tie in the picture.
  const links = view.links.filter((l) => keep.has(l.source) && keep.has(l.target) && (anchored(l) || pairs.has(pair(l.source, l.target))));
  const linked = new Set(links.flatMap((l) => [l.source, l.target]));
  const extra = new Map<string, ViewLink>();
  for (const l of view.links) {
    if (!keep.has(l.source) || !keep.has(l.target) || l.source === l.target || links.includes(l)) continue;
    for (const [a, b] of [[l.source, l.target], [l.target, l.source]]) {
      if (linked.has(a)) continue;
      const have = extra.get(a);
      if (!have || byStrength(b, have.source === a ? have.target : have.source) < 0) extra.set(a, l);
    }
  }
  for (const l of new Set(extra.values())) links.push(l);
  const order = new Map(view.links.map((l, i) => [l.id, i]));
  links.sort((a, b) => order.get(a.id)! - order.get(b.id)!);
  return { ...view, nodes: view.nodes.filter((n) => keep.has(n.id)), links, focus: { total: view.nodes.length } };
}

/** One part of the answer brought forward (a deep answer's section: its part has the section's index): the part's
 *  seeds (`seeds`) and the ends of the links its search found stay as they are; every other node is dimmed and every
 *  other link drawn faint (the open edge and the path excepted). Positions are untouched: apply after the layout.
 *  A part the view has nothing of leaves the view as it is. */
export function highlightPart(view: GraphView, part: number | null, seeds: readonly string[] = []): GraphView {
  if (part === null) return view;
  const inPart = (l: ViewLink) => !!l.parts?.includes(part);
  const ids = new Set(seeds);
  for (const l of view.links) if (inPart(l)) { ids.add(l.source); ids.add(l.target); }
  if (!view.nodes.some((n) => ids.has(n.id))) return view;
  return { ...view,
    nodes: view.nodes.map((n) => (ids.has(n.id) || n.selected || n.onPath ? n : { ...n, state: "dim" as const })),
    links: view.links.map((l) => (inPart(l) || l.picked || l.onPath ? l : { ...l, state: "faint" as const, hot: false })) };
}
