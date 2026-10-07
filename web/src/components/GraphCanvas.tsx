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
export function GraphCanvas({ view, positions, onNodeClick }: { view: GraphView; positions: Positions; onNodeClick: (id: string) => void }) {
  const box = useRef<HTMLDivElement>(null);
  const sigma = useRef<Sigma | null>(null);
  const graph = useRef(new Graph({ multi: false, type: "directed" }));
  const click = useRef(onNodeClick);
  click.current = onNodeClick;
  const latest = useRef({ view, positions });
  latest.current = { view, positions };

  const paint = () => {
    const g = graph.current;
    const { view: v, positions: pos } = latest.current;
    const p = palette();
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
  useEffect(paint, [view, positions]);
  return <div ref={box} className="gcanvas" role="img" aria-label="Entities and relationships used for the answer" />;
}
