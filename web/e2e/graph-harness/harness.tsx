// A test page, never built into the app (vite builds index.html only): the real GraphPane drawing a recorded
// answer, fed its events one by one as the app would. Served by `vite --port 18766` for the layout
// screenshots and the in-browser legibility metric (e2e/graph-layout.spec.ts).
// ?fx=<fixture>&theme=<id>&view=focused|full (a big answer's view; default focused)&section=<n> (a section brought forward)
import "@fontsource/public-sans/400.css";
import "@fontsource/public-sans/500.css";
import "@fontsource/public-sans/600.css";
import "../../src/theme.css";
import { useEffect, useState } from "react";
import { flushSync } from "react-dom";
import { createRoot } from "react-dom/client";
import { askReducer, initialState, type AskState } from "../../src/askReducer";
import { GraphPane } from "../../src/components/GraphPane";
import { measureGraph, screenPositions } from "../../src/graphMetrics";
import { replay } from "../../src/fixtures/replay";
import type { AskEvent } from "../../src/types";
import two from "../../src/fixtures/two-part.json";
import one from "../../src/fixtures/single-part.json";
import dark from "../../src/fixtures/answer-graph-dark-one.json";
import battle from "../../src/fixtures/answer-graph-battle.json";
import ogier from "../../src/fixtures/answer-graph-ogier.json";
import { NO_FILTERS, saveFilters, saveFocus } from "../../src/filters";

const FIXTURES: Record<string, AskEvent[]> = { one: one as AskEvent[], two: two as AskEvent[], dark: dark as AskEvent[], battle: battle as AskEvent[], ogier: ogier as AskEvent[] };
const q = new URLSearchParams(location.search);
const events = FIXTURES[q.get("fx") ?? "dark"];
document.documentElement.setAttribute("data-theme", q.get("theme") ?? "light");
// view=before: as before the focused view, whole and with the "uncategorized" relationships shown.
const before = q.get("view") === "before";
const full = before || q.get("view") === "full";
saveFocus(full ? "full" : "focused");
saveFilters({ ...NO_FILTERS, ...(before ? { uncategorized: true } : {}) });
const section = q.get("section") === null ? null : Number(q.get("section"));
// No server behind this page: every API call fails, as with an unreachable server.
window.fetch = async () => new Response(JSON.stringify({ error: { code: "offline", message: "harness" } }), { status: 503, headers: { "Content-Type": "application/json" } });

declare global { interface Window { __lore: { ready: boolean; metrics: (ratio?: number, focus?: string) => unknown;
  point: (label: string) => { x: number; y: number } | null } } }

function Harness() {
  const [state, setState] = useState<AskState>(() => initialState("q"));
  useEffect(() => {
    let s = initialState("q");
    for (const e of events) { s = askReducer(s, e); const next = s; flushSync(() => setState(next)); }
    window.__lore.ready = true;
  }, []);
  return (
    <div className="app">
      <div className="harness-top">LORE</div>
      <div className="panes">
        <section className="pane chat" aria-label="Conversation" />
        <GraphPane state={state} hiddenBooks={new Set()} hoverChunks={[]} noBooks={false} section={section} />
      </div>
    </div>
  );
}

const ctx = document.createElement("canvas").getContext("2d")!;
const measure = (text: string, px: number) => { ctx.font = `500 ${px}px Public Sans`; return ctx.measureText(text).width; };
const canvasBox = () => document.querySelector(".gcanvas")!.getBoundingClientRect();
window.__lore = {
  ready: false,
  // The legibility metric at the fitted zoom (or `ratio`), optionally with the node named `focus` hovered.
  metrics: (ratio = 1, focus) => {
    const box = canvasBox();
    const { view, positions } = replay(events, "q", { hideUncategorized: !before, focus: !full });
    const f = new Set(view.nodes.filter((n) => n.label === focus).map((n) => n.id));
    return { viewport: { width: box.width, height: box.height }, ...measureGraph(view, positions, { width: box.width, height: box.height }, { measure, ratio, focus: f }) };
  },
  // Where the node named `label` is drawn, in page px.
  point: (label) => {
    const box = canvasBox();
    const { view, positions } = replay(events, "q", { hideUncategorized: !before, focus: !full });
    const n = view.nodes.find((x) => x.label === label);
    const at = n && screenPositions(view, positions, { width: box.width, height: box.height }, measure).at.get(n.id);
    return at ? { x: box.left + at.x, y: box.top + at.y } : null;
  },
};
// The canvas draws names in Public Sans: load it first, as the app has by the time an answer arrives.
Promise.all([document.fonts.load('400 12px "Public Sans"'), document.fonts.load('500 12px "Public Sans"')])
  .finally(() => createRoot(document.getElementById("root")!).render(<Harness />));
