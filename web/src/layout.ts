import type { GraphView } from "./graphView";
import { degrees, drawnLabel, estimateWidth, isLarge, LABEL_GAP, NODE_LABEL_PX, nodeSizes } from "./graphSizing";
import { contentBounds, measureGraph, sigmaScale } from "./graphMetrics";

export type Positions = Map<string, { x: number; y: number }>;
const CX = 500, CY = 350;
// Label box in layout units, for the overview's label-collision pass.
const LABEL_W = 150, LABEL_H = 24;
const SPIRAL = 60, GOLDEN = Math.PI * (3 - Math.sqrt(5));

function hash(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  h ^= h >>> 16; h = Math.imul(h, 2246822507); h ^= h >>> 13; h = Math.imul(h, 3266489909); h ^= h >>> 16;
  return (h >>> 0) / 4294967296;
}

// Force-directed relaxation (Fruchterman-Reingold), deterministic from the starting positions: links pull
// their ends to about IDEAL apart, every pair pushes apart, and a weak pull to the centre keeps unlinked
// entities near the picture instead of drifting off; pairs further apart than REACH do not push. Distances count horizontal gaps at half weight, because
// labels run to the right of their nodes and need more room side by side than stacked.
const IDEAL = 70, STEPS = 300, REACH2 = (3 * IDEAL) ** 2;
function relax(pos: Positions, ids: string[], links: GraphView["links"]): void {
  const index = new Map(ids.map((id, i) => [id, i]));
  const xs = ids.map((id) => pos.get(id)!.x), ys = ids.map((id) => pos.get(id)!.y);
  const edges: [number, number][] = [];
  const seen = new Set<string>();
  for (const l of links) {
    const a = index.get(l.source), b = index.get(l.target);
    if (a === undefined || b === undefined || a === b) continue;
    const key = a < b ? `${a}:${b}` : `${b}:${a}`;
    if (!seen.has(key)) { seen.add(key); edges.push([a, b]); }
  }
  const n = ids.length, k2 = IDEAL * IDEAL;
  for (let step = 0; step < STEPS; step++) {
    const heat = 40 * (1 - step / STEPS) + 1;
    const fx = new Float64Array(n), fy = new Float64Array(n);
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
      let dx = (xs[i] - xs[j]) * 0.5, dy = ys[i] - ys[j];
      let d2 = dx * dx + dy * dy;
      if (d2 < 1e-6) { dx = hash(ids[i] + ids[j]) - 0.5; dy = 0.5 - hash(ids[j] + ids[i]); d2 = dx * dx + dy * dy || 1; }
      if (d2 > REACH2) continue;                            // far pairs don't push: a cluster can't fling loners off
      const f = k2 / d2;                                    // (k^2 / d) along the unit vector
      fx[i] += dx * f; fy[i] += dy * f; fx[j] -= dx * f; fy[j] -= dy * f;
    }
    for (const [a, b] of edges) {
      const dx = xs[a] - xs[b], dy = ys[a] - ys[b], d = Math.hypot(dx, dy) || 1;
      const f = d / IDEAL;                                  // (d^2 / k) along the unit vector
      fx[a] -= dx * f; fy[a] -= dy * f; fx[b] += dx * f; fy[b] += dy * f;
    }
    for (let i = 0; i < n; i++) {
      fx[i] -= (xs[i] - CX) * 0.25; fy[i] -= (ys[i] - CY) * 0.25;
      const m = Math.hypot(fx[i], fy[i]);
      if (m > 0) { const s = Math.min(m, heat) / m; xs[i] += fx[i] * s; ys[i] += fy[i] * s; }
    }
  }
  ids.forEach((id, i) => pos.set(id, { x: xs[i], y: ys[i] }));
}

// `overview`: the library overview, started on a spiral and relaxed by links (only then; answer graphs hang off their seeds).
// `pinned`: the camera is pinned (an expansion or a path is drawn), so nodes on screen must stay put.
// A large answer graph (graphSizing.isLarge) that is not pinned is laid out whole by density (denseLayout) each
// time new nodes arrive or its cited links change (the answer lands); hiding nodes with a filter moves nothing, and
// they come back where they were. Anything else places only its new nodes and never moves the rest.
export function place(previous: Positions, view: GraphView, anchor: string | null = null, overview = false, pinned = false): Positions {
  if (!overview && !pinned && isLarge(view)) {
    const key = denseKey(view);
    const fresh = view.nodes.some((n) => !previous.has(n.id));
    if (!fresh && laidOut.get(previous) === key) return previous;
    const pos = view.focus ? focusLayout(view) : denseLayout(view);
    for (const [id, p] of previous) if (!pos.has(id)) pos.set(id, p);
    laidOut.set(pos, key);
    return pos;
  }
  const pos: Positions = new Map(previous);
  const fresh = view.nodes.filter((n) => !pos.has(n.id));
  const seeds = fresh.filter((n) => n.seedPart !== null);
  const totalSeeds = view.nodes.filter((n) => n.seedPart !== null).length;
  const radius = Math.max(220, 12 * totalSeeds);
  for (const n of seeds) {
    const a = (n.seedPart ?? 0) * 0.6 + 2 * Math.PI * hash(n.id);
    pos.set(n.id, { x: CX + radius * Math.cos(a), y: CY + radius * Math.sin(a) });
  }
  const neighbours = new Map<string, string[]>();
  for (const l of view.links) {
    neighbours.set(l.source, [...(neighbours.get(l.source) ?? []), l.target]);
    neighbours.set(l.target, [...(neighbours.get(l.target) ?? []), l.source]);
  }
  const others = fresh.filter((n) => n.seedPart === null);
  if (overview && pos.size === 0 && others.length > 0) {
    // Nothing to hang them on (the library overview): a sunflower spiral in view order, so the
    // most-connected nodes, which come first, sit nearest the centre and the rest spread evenly.
    others.forEach((n, i) => {
      const r = SPIRAL * Math.sqrt(i + 0.5), a = i * GOLDEN;
      pos.set(n.id, { x: CX + r * Math.cos(a), y: CY + r * Math.sin(a) });
    });
    // The spiral alone ignores who is linked to whom, so every edge crosses the picture: relax it so
    // linked entities sit together and clusters separate.
    relax(pos, others.map((n) => n.id), view.links);
  }
  for (const n of others) {
    if (pos.has(n.id)) continue;
    const near = (neighbours.get(n.id) ?? []).find((m) => pos.has(m)) ?? anchor ?? null;
    const c = (near && pos.get(near)) || { x: CX, y: CY };
    const a = 2 * Math.PI * hash(n.id);
    pos.set(n.id, { x: c.x + 140 * Math.cos(a), y: c.y + 140 * Math.sin(a) });
  }
  const movable = new Set(fresh.map((n) => n.id));
  const ids = [...pos.keys()];
  for (let it = 0; it < 60; it++) {
    for (const id of movable) {
      const p = pos.get(id)!;
      for (const other of ids) {
        if (other === id) continue;
        const q = pos.get(other)!;
        let dx = p.x - q.x, dy = p.y - q.y;
        let d = Math.hypot(dx, dy);
        if (d === 0) { dx = hash(id + other) - 0.5; dy = 0.5 - hash(other + id); d = Math.hypot(dx, dy) || 1; }
        if (d < 70) { const push = (70 - d) / 2 + 0.5; p.x += (dx / d) * push; p.y += (dy / d) * push; }
        // The overview labels every node, and labels run to the right of their node: two nodes on
        // nearly the same row closer than a label's width are pushed apart vertically.
        if (overview && Math.abs(dx) < LABEL_W && Math.abs(dy) < LABEL_H) {
          const push = (LABEL_H - Math.abs(dy)) / 2 + 0.5;
          p.y += (dy >= 0 ? 1 : -1) * push;
        }
      }
    }
  }
  return pos;
}

// ---- Dense answer graphs ---------------------------------------------------------------------------------------
// A big answer (60-100+ entities, a hub with dozens of links) hung off its seeds piles its names on top of each
// other. It is laid out instead in screen pixels for the graph pane's usual size (REF_W x REF_H, the pane at
// 1440x900), so what is kept apart here is what the fitted camera shows apart:
//  1. Forces over every node (Fruchterman-Reingold): the ideal link length is the side of an equal share of the
//     pane, so spacing grows with the square root of the number of nodes per pane and the whole picture fills
//     about one pane; a cited link is longer by its label's width, so the label has room. Hubs push harder (by
//     the square root of their links), so two hubs keep their satellite rings apart, and a node linked only to
//     a hub ends up on a ring round it. Pairs more than four link lengths apart do not push, and a pull to the centre
//     holds unlinked nodes near the picture: nothing is flung off.
//  2. Then each node takes the free spot nearest where the forces put it (the ends of cited links first, then the
//     most-linked): free when the node's dot and its whole name, plus a margin, touch no dot, name or kept-clear
//     stretch already placed, and no linked node is closer than a visible link. A cited link needs a stretch clear
//     of both dots and of the name running along it as long as its label, and keeps that stretch clear.
//  3. If the packed picture comes out bigger than the pane (the camera would shrink it and names would meet again),
//     it is packed again from the force positions drawn in, up to four times, keeping the best fit.
// Deterministic (no randomness, a fixed number of steps) and bounded: about 100 ms for 100 nodes.
const REF_W = 950, REF_H = 710, ASPECT = REF_W / REF_H, REF = { width: REF_W, height: REF_H };
const MX = 8, MY = 3;              // the margin round each node's box, horizontally and vertically
const LINK_GAP = 16;                        // the shortest visible link between two linked nodes' rims
const CORRIDOR = (NODE_LABEL_PX + 4) / 2;   // half the height of an edge label's pill
const laidOut = new WeakMap<Positions, string>();
const pairOf = (a: string, b: string) => (a < b ? `${a}\n${b}` : `${b}\n${a}`);

// What, besides new nodes, calls for a fresh dense layout: the cited links (their labels get room).
const denseKey = (view: GraphView) => view.links.filter((l) => l.state === "cited").map((l) => l.id).sort().join("\n");

interface Foot { left: number; right: number; half: number }
type Rect = { x0: number; y0: number; x1: number; y1: number };

// Free-spot lookup over placed boxes, on a coarse grid.
class Occupancy {
  private cells = new Map<number, Rect[]>();
  private static C = 48;
  private static key = (i: number, j: number) => (i + 4096) * 8192 + (j + 4096);
  free(r: Rect): boolean {
    const c = Occupancy.C;
    for (let i = Math.floor(r.x0 / c), i1 = Math.floor(r.x1 / c); i <= i1; i++)
      for (let j = Math.floor(r.y0 / c), j1 = Math.floor(r.y1 / c); j <= j1; j++) {
        const list = this.cells.get(Occupancy.key(i, j));
        if (list) for (const o of list) if (r.x0 < o.x1 && o.x0 < r.x1 && r.y0 < o.y1 && o.y0 < r.y1) return false;
      }
    return true;
  }
  add(r: Rect): void {
    const c = Occupancy.C;
    for (let i = Math.floor(r.x0 / c), i1 = Math.floor(r.x1 / c); i <= i1; i++)
      for (let j = Math.floor(r.y0 / c), j1 = Math.floor(r.y1 / c); j <= j1; j++) {
        const k = Occupancy.key(i, j), list = this.cells.get(k);
        if (list) list.push(r); else this.cells.set(k, [r]);
      }
  }
}

const rectAt = (x: number, y: number, f: Foot): Rect => ({ x0: x - f.left, y0: y - f.half, x1: x + f.right, y1: y + f.half });

// The free spot nearest (x, y) that `ok` accepts, searched outward on a sunflower spiral stretched to the pane's
// shape. Past `limit` px it gives up and keeps (x, y).
function nearestFree(occ: Occupancy, x: number, y: number, f: Foot, ok: (x: number, y: number) => boolean, limit = 2400): { x: number; y: number } {
  const step = 4, sx = Math.sqrt(ASPECT), sy = 1 / sx;
  for (let k = 0; ; k++) {
    const r = step * Math.sqrt(k);
    if (r > limit) return { x, y };
    const a = k * GOLDEN;
    const px = x + r * Math.cos(a) * sx, py = y + r * Math.sin(a) * sy;
    if (occ.free({ x0: px - f.left, y0: py - f.half, x1: px + f.right, y1: py + f.half }) && ok(px, py)) return { x: px, y: py };
  }
}


// A big answer's focused view (graphFocus.ts) is small enough to lay out several ways: the density layout at a few
// link lengths, keeping the one whose labels collide least at the fitted zoom (graphMetrics). Where some thirty cited
// labels share a picture, which one that is swings with small changes, so picking beats any single setting.
// Deterministic, and about 5 x 20 ms for 30-odd nodes.
export const FOCUS_SPREADS = [0.9, 0.65, 0.8, 1.05, 1.2];
export function focusLayout(view: GraphView, measure: (text: string, px: number) => number = estimateWidth): Positions {
  let best: Positions | null = null, bestCost = Infinity;
  for (const spread of FOCUS_SPREADS) {
    const pos = denseLayout(view, measure, spread);
    const m = measureGraph(view, pos, REF, { measure });
    const cost = 5 * m.nodeOverlaps + 3 * (m.labelOverlaps + m.labelOnNode + m.offCanvas) + 2 * m.edgeLabelOverlaps
      + 4 * (m.citedLabels - m.citedLabelsDrawn);
    if (cost < bestCost) { best = pos; bestCost = cost; }
  }
  return best!;
}

// `spread`: the link length as a share of the side of each node's equal share of the pane.
export function denseLayout(view: GraphView, measure: (text: string, px: number) => number = estimateWidth, spread = 0.9): Positions {
  const sizes = nodeSizes(view);
  const deg = degrees(view);
  // Packing order: the ends of cited links first (so their labels' stretches are kept clear early), then by links.
  const lit = new Set(view.links.filter((l) => l.state === "cited" || l.onPath || l.picked).flatMap((l) => [l.source, l.target]));
  const ids = [...view.nodes.map((n) => n.id)].sort((a, b) => Number(lit.has(b)) - Number(lit.has(a))
    || (deg.get(b) ?? 0) - (deg.get(a) ?? 0) || (a < b ? -1 : a > b ? 1 : 0));
  const index = new Map(ids.map((id, i) => [id, i]));
  const n = ids.length;
  const foot = new Map<string, Foot>();
  for (const v of view.nodes) {
    const r = sizes.get(v.id) ?? 8;
    foot.set(v.id, { left: r + MX, right: r + LABEL_GAP + measure(drawnLabel(v), NODE_LABEL_PX) + MX, half: Math.max(r, NODE_LABEL_PX / 2 + 2) + MY });
  }
  const labelW = new Map(view.nodes.map((v) => [v.id, measure(drawnLabel(v), NODE_LABEL_PX)]));
  // How much the fitted camera (which fits dots and names, GraphCanvas) would scale the picture in the reference pane.
  const fit = (pos: Positions) => {
    const items = ids.map((id) => ({ ...pos.get(id)!, r: sizes.get(id) ?? 8, w: labelW.get(id)! }));
    return sigmaScale(contentBounds(items, REF), REF);
  };
  const near: Set<number>[] = ids.map(() => new Set<number>());
  const room = new Map<string, number>();     // a cited link's ends keep its label's width apart
  const springs: [number, number, number][] = [];
  const seen = new Set<string>();
  const ideal = spread * Math.sqrt((REF_W * REF_H) / Math.max(n, 1));
  for (const l of view.links) {
    const a = index.get(l.source), b = index.get(l.target);
    if (a === undefined || b === undefined || a === b) continue;
    near[a].add(b); near[b].add(a);
    const key = pairOf(l.source, l.target);
    if (seen.has(key)) continue;
    seen.add(key);
    if (l.state === "cited" || l.onPath || l.picked) room.set(key, measure(l.label, NODE_LABEL_PX) + 32);
    springs.push([a, b, ideal + (room.get(key) ?? 0)]);
  }
  // 1. Forces, from a sunflower start in link order (hubs at the centre).
  const xs = ids.map((_, i) => CX + ideal * 0.7 * Math.sqrt(i + 0.5) * Math.cos(i * GOLDEN) * Math.sqrt(ASPECT));
  const ys = ids.map((_, i) => CY + ideal * 0.7 * Math.sqrt(i + 0.5) * Math.sin(i * GOLDEN) / Math.sqrt(ASPECT));
  const weight = ids.map((id) => Math.sqrt(1 + (deg.get(id) ?? 0)));
  const k2 = ideal * ideal, reach2 = (4 * ideal) ** 2, steps = 300;
  for (let step = 0; step < steps; step++) {
    const heat = ideal * (1 - step / steps) + 1;
    const fx = new Float64Array(n), fy = new Float64Array(n);
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
      // Names run to the right, so a horizontal gap counts for less than a vertical one.
      let dx = (xs[i] - xs[j]) / Math.sqrt(ASPECT), dy = (ys[i] - ys[j]) * Math.sqrt(ASPECT);
      let d2 = dx * dx + dy * dy;
      if (d2 < 1e-6) { dx = hash(ids[i] + ids[j]) - 0.5; dy = 0.5 - hash(ids[j] + ids[i]); d2 = dx * dx + dy * dy || 1; }
      if (d2 > reach2) continue;
      const f = (k2 * weight[i] * weight[j]) / d2;
      fx[i] += dx * f; fy[i] += dy * f; fx[j] -= dx * f; fy[j] -= dy * f;
    }
    for (const [a, b, len] of springs) {
      const dx = xs[a] - xs[b], dy = ys[a] - ys[b], d = Math.hypot(dx, dy) || 1;
      const f = d / len;
      fx[a] -= dx * f; fy[a] -= dy * f; fx[b] += dx * f; fy[b] += dy * f;
    }
    for (let i = 0; i < n; i++) {
      fx[i] -= (xs[i] - CX) * 0.3; fy[i] -= (ys[i] - CY) * 0.3 * ASPECT;
      const m = Math.hypot(fx[i], fy[i]);
      if (m > 0) { const s = Math.min(m, heat) / m; xs[i] += fx[i] * s; ys[i] += fy[i] * s; }
    }
  }
  // 2. Pack, most-linked first. When the packed picture is larger than the pane (the fitted camera would shrink it
  // and the names would collide again), pack again from the force positions drawn closer together.
  // The stretch of the link between a and b that is clear of both dots and of the name running from its left end
  // (names sit right of their dots), measured in px from the left end along the link.
  const clear = (a: { x: number; y: number }, ida: string, b: { x: number; y: number }, idb: string) => {
    const [start, sid, end, eid] = a.x <= b.x ? [a, ida, b, idb] as const : [b, idb, a, ida] as const;
    const d = Math.hypot(end.x - start.x, end.y - start.y) || 1;
    const ux = (end.x - start.x) / d, uy = (end.y - start.y) / d;
    // As the canvas reserves a name for edge labels (GraphCanvas: 10 px and 8% over the measured width).
    const from = (sizes.get(sid) ?? 8) + (10 + labelW.get(sid)! * 1.08) * Math.abs(ux) + 4;
    return { start, ux, uy, from, to: d - (sizes.get(eid) ?? 8) - 4 };
  };
  const pack = (squeeze: number, from: (i: number) => { x: number; y: number } = (i) => ({ x: xs[i], y: ys[i] })): Positions => {
    const occ = new Occupancy();
    const pos: Positions = new Map();
    ids.forEach((id, i) => {
      const r = sizes.get(id) ?? 8;
      const linked = [...near[i]].map((j) => ids[j]).filter((m) => pos.has(m));
      const ok = (x: number, y: number) => linked.every((m) => {
        const q = pos.get(m)!, need = room.get(pairOf(id, m));
        if (!need) return Math.hypot(x - q.x, y - q.y) >= r + (sizes.get(m) ?? 8) + LINK_GAP;
        const c = clear({ x, y }, id, q, m);
        return c.to - c.from >= need;
      });
      const t = from(i);
      const at = nearestFree(occ, CX + (t.x - CX) * squeeze, CY + (t.y - CY) * squeeze, foot.get(id)!, ok);
      pos.set(id, at);
      occ.add(rectAt(at.x, at.y, foot.get(id)!));
      // A cited link whose ends are both down keeps the stretch its label sits on clear: small squares along
      // the middle of its clear stretch, which nodes placed later avoid.
      for (const m of linked) {
        const need = room.get(pairOf(id, m));
        if (!need) continue;
        const c = clear(at, id, pos.get(m)!, m);
        const mid = (c.from + c.to) / 2;
        for (let u = mid - (need - 8) / 2; u <= mid + (need - 8) / 2 + 1e-9; u += 6) {
          const cx = c.start.x + c.ux * u, cy = c.start.y + c.uy * u;
          occ.add({ x0: cx - CORRIDOR, y0: cy - CORRIDOR, x1: cx + CORRIDOR, y1: cy + CORRIDOR });
        }
      }
    });
    return pos;
  };
  let squeeze = 1, best = pack(1), bestFit = fit(best);
  for (let round = 0, f = bestFit; round < 4 && bestFit < 0.97; round++) {
    squeeze *= Math.max(0.7, Math.min(0.95, f));
    const next = pack(squeeze);
    f = fit(next);
    if (f > bestFit) { best = next; bestFit = f; }
  }
  const pos = best;
  return pos;
}
