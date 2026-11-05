import type { GraphView } from "./graphView";

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
export function place(previous: Positions, view: GraphView, anchor: string | null = null, overview = false): Positions {
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
