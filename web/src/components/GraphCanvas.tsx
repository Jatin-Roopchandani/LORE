import { useEffect, useRef } from "react";
import Graph from "graphology";
import Sigma from "sigma";
import type { GraphView } from "../graphView";
import type { Positions } from "../layout";

const KIND_VAR: Record<string, string> = { Character: "--k-character", Place: "--k-place", Faction: "--k-faction", Object: "--k-object" };

function palette() {
  const css = getComputedStyle(document.documentElement);
  const v = (name: string) => css.getPropertyValue(name).trim();
  return { v, ink: v("--ink"), muted: v("--muted"), edge: v("--edge"), faint: v("--edge-faint"), glow: v("--glow") };
}

// Sigma's default node program (circle) has no border, so a seed is marked by a larger disc in its part colour.
// While `pinned` is false the camera fits the whole layout on every change (staging and the answer).
// Once it turns true (an expansion) the current framing is frozen before new nodes land, so nodes
// already on screen stay put. Bumping `fitRequest` refits once and re-pins if still pinned.
export function GraphCanvas({ view, positions, onNodeClick, pinned = false, fitRequest = 0 }:
  { view: GraphView; positions: Positions; onNodeClick: (id: string) => void; pinned?: boolean; fitRequest?: number }) {
  const box = useRef<HTMLDivElement>(null);
  const sigma = useRef<Sigma | null>(null);
  const graph = useRef(new Graph({ multi: false, type: "directed" }));
  const click = useRef(onNodeClick);
  click.current = onNodeClick;
  const latest = useRef({ view, positions, pinned });
  latest.current = { view, positions, pinned };

  const paint = () => {
    const g = graph.current;
    const { view: v, positions: pos, pinned: pin } = latest.current;
    const p = palette();
    const s = sigma.current;
    if (s && pin && !s.getCustomBBox()) s.setCustomBBox(s.getBBox());
    if (s && !pin && s.getCustomBBox()) s.setCustomBBox(null);
    const keepNodes = new Set(v.nodes.map((n) => n.id));
    g.forEachNode((id) => { if (!keepNodes.has(id)) g.dropNode(id); });
    for (const n of v.nodes) {
      const at = pos.get(n.id) ?? { x: 500, y: 350 };
      const color = n.state === "dim" ? p.faint
        : n.seedPart !== null ? p.v(`--part-${n.seedPart % 4}`) : p.v(KIND_VAR[n.kind] ?? "--k-other");
      const attrs = { x: at.x, y: -at.y, size: n.seedPart !== null ? 10 : 7, label: n.label.replace(/^Mr\. /, ""), color,
        zIndex: n.state === "dim" ? 0 : 1 };
      if (g.hasNode(n.id)) g.mergeNodeAttributes(n.id, attrs); else g.addNode(n.id, attrs);
    }
    const keepEdges = new Set(v.links.map((l) => l.id));
    g.forEachEdge((id) => { if (!keepEdges.has(id)) g.dropEdge(id); });
    for (const l of v.links) {
      if (!g.hasNode(l.source) || !g.hasNode(l.target)) continue;
      const color = l.hot || l.state === "cited" ? p.glow
        : l.state === "shown" ? (l.part !== null ? p.v(`--part-${l.part % 4}`) : p.edge) : p.faint;
      const attrs = { type: "arrow", label: l.state === "faint" || l.state === "cut" ? "" : l.label, color,
        size: l.hot ? 4 : l.state === "cited" ? 2.6 : 1.4, zIndex: l.hot ? 3 : l.state === "cited" ? 2 : l.state === "shown" ? 1 : 0 };
      if (g.hasEdge(l.id)) g.mergeEdgeAttributes(l.id, attrs);
      else if (!g.hasDirectedEdge(l.source, l.target)) g.addEdgeWithKey(l.id, l.source, l.target, attrs);
    }
    sigma.current?.setSetting("labelColor", { color: p.ink });
    sigma.current?.setSetting("edgeLabelColor", { color: p.muted });
    sigma.current?.refresh();
  };

  useEffect(() => {
    const p = palette();
    sigma.current = new Sigma(graph.current, box.current!, {
      renderEdgeLabels: true, defaultEdgeType: "arrow", labelFont: "Public Sans", labelSize: 12, edgeLabelSize: 10,
      labelRenderedSizeThreshold: 0, labelColor: { color: p.ink }, edgeLabelColor: { color: p.muted }, zIndex: true,
      allowInvalidContainer: true,
    });
    sigma.current.on("clickNode", ({ node }) => click.current(node));
    paint();
    const obs = new MutationObserver(paint);
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    mq.addEventListener("change", paint);
    return () => { obs.disconnect(); mq.removeEventListener("change", paint); sigma.current?.kill(); sigma.current = null; };
    // paint reads everything through refs, so the mount-time closure stays current.
  }, []);
  useEffect(paint, [view, positions, pinned]);
  useEffect(() => {
    const s = sigma.current;
    if (!s || fitRequest === 0) return;
    s.setCustomBBox(null);
    s.getCamera().setState({ x: 0.5, y: 0.5, ratio: 1, angle: 0 });
    s.refresh();
    if (latest.current.pinned) s.setCustomBBox(s.getBBox());
  }, [fitRequest]);
  return <div ref={box} className="gcanvas" role="img" aria-label="Entities and relationships used for the answer" />;
}
