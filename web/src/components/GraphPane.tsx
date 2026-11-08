import { useEffect, useMemo, useRef, useState } from "react";
import { api, isError } from "../api";
import { initialState, type AskState } from "../askReducer";
import { loadFilters, saveFilters, type Filters } from "../filters";
import { deriveGraph, unknownKinds, worldsIn, type KindMap } from "../graphView";
import { place, type Positions } from "../layout";
import { EXPLORE, STEP_LABEL, UI, WORLDS } from "../messages";
import type { Book, Depth, PathResult, SearchHit, SubgraphData, SubgraphLink } from "../types";
import { worldSlot } from "../worlds";
import { ConnectionView, type Connection } from "./ConnectionView";
import { EdgeView } from "./EdgeView";
import { EntityPanel } from "./EntityPanel";
import { EntitySearch } from "./EntitySearch";
import { FilterBar } from "./FilterBar";
import { GraphCanvas } from "./GraphCanvas";

interface Expansion { root: string; data: SubgraphData }
type Mode = "answer" | "library";
type Panel = { type: "entity"; id: string } | { type: "edge"; a: string; b: string };
// Ids per /api/entities request: keeps the URL well under the server's request-line limit.
const KIND_BATCH = 100;
const KIND_DELAY_MS = 150;
const BLANK = initialState();

// The path as a small subgraph, drawn like an expansion so its nodes and links join the graph.
function pathData(r: PathResult): SubgraphData {
  const edges: SubgraphLink[] = [];
  for (const h of r.hops) {
    for (const dir of ["out", "in"] as const) {
      const ps = h.predicates.filter((p) => p.direction === dir);
      if (!ps.length) continue;
      edges.push({ subject: "", object: "", subject_id: dir === "out" ? h.from : h.to, object_id: dir === "out" ? h.to : h.from,
        count: ps.reduce((a, p) => a + p.count, 0), predicates: Object.fromEntries(ps.map((p) => [p.predicate, p.count])) });
    }
  }
  return { nodes: r.nodes.map((n) => ({ id: n.id, name: n.name, kind: n.kind, books: [] })), edges, truncated: false, depth: r.hops.length };
}

// Keyed on the ask by its parent, so a new ask (even of the same text) starts with no expansions,
// no camera pin, no positions and no kinds, and late results for the old ask land nowhere.
// `overview` is the library overview (App fetches it once): the Library view, and the only view before any question.
// undefined = still loading; null = unavailable or empty.
// `world`: the selected world (an id as a string, or "all"), passed to every graph call.
// `onAsk`: sends a question through the ordinary ask path (the panel's one-click questions).
// `positionVersion`: bumped when the reading position changed. The expansions and the path were fetched under the old
// position, so they are dropped; the open panel refetches under the new one.
export function GraphPane({ state, hiddenBooks, hoverChunks, noBooks, books = [], overview, world, positionVersion = 0, onAsk = () => {} }:
  { state: AskState; hiddenBooks: Set<string>; hoverChunks: string[]; noBooks: boolean; books?: Book[];
    overview?: SubgraphData | null; world?: string; positionVersion?: number; onAsk?: (question: string, depth?: Depth) => void }) {
  const idle = state.status === "idle" && !state.question;
  // The person's choice of view; with none, an answer shows its graph and nothing shows the library.
  const [chosen, setChosen] = useState<Mode | null>(null);
  const mode: Mode = idle ? "library" : chosen ?? "answer";
  // Each view keeps its own expansions and layout, so toggling back restores it.
  const [expansions, setExpansions] = useState<Record<Mode, Expansion[]>>({ answer: [], library: [] });
  const [fitRequest, setFitRequest] = useState(0);
  const [panel, setPanel] = useState<Panel | null>(null);
  const [focus, setFocus] = useState<{ id: string | null; n: number }>({ id: null, n: 0 });
  const [connection, setConnection] = useState<Connection | null>(null);
  const [filters, setFilters] = useState<Filters>(() => loadFilters());
  const [kinds, setKinds] = useState<KindMap>({});
  const asked = useRef(new Set<string>());
  const positions = useRef<Record<Mode, Positions>>({ answer: new Map(), library: new Map() });
  const bookTitles = useMemo(() => new Map(books.map((b) => [b.id, b.title])), [books]);
  const graphed = useMemo(() => books.filter((b) => b.status === "graphed"), [books]);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  // A new world starts at its library: nothing of the old world's panel, path or expansions applies.
  const lastWorld = useRef(world);
  useEffect(() => {
    if (lastWorld.current === world) return;
    lastWorld.current = world;
    setChosen("library"); setPanel(null); setConnection(null);
    setExpansions({ answer: [], library: [] });
    positions.current = { answer: new Map(), library: new Map() };
  }, [world]);

  const lastPosition = useRef(positionVersion);
  useEffect(() => {
    if (lastPosition.current === positionVersion) return;
    lastPosition.current = positionVersion;
    setConnection(null);
    setExpansions({ answer: [], library: [] });
    positions.current = { answer: new Map(), library: new Map() };
  }, [positionVersion]);

  const libraryOverview = overview?.nodes.length ? overview : null;
  const shownOverview = mode === "library" ? libraryOverview : null;
  const selected = panel?.type === "entity" ? panel.id : null;
  const path = connection?.status === "found" && connection.result ? connection.result : null;
  const pathSub = useMemo(() => (path ? pathData(path) : null), [path]);
  const exps = useMemo(() => [...expansions[mode].map((e) => e.data), ...(pathSub ? [pathSub] : [])], [expansions, mode, pathSub]);
  const view = useMemo(() => deriveGraph(mode === "library" ? BLANK : state, { hiddenBooks, hoverChunks, expansions: exps, kinds,
    overview: shownOverview, selected, edge: panel?.type === "edge" ? { a: panel.a, b: panel.b } : null, filters,
    path: path ? { nodes: path.nodes.map((n) => n.id), hops: path.hops } : null }),
    [mode, state, hiddenBooks, hoverChunks, exps, kinds, shownOverview, selected, panel, filters, path]);
  const anchor = pathSub ? pathSub.nodes[0]?.id : expansions[mode].at(-1)?.root ?? null;
  // An expansion or a path pins the camera, and with it every node already on screen.
  const pinned = expansions[mode].length > 0 || !!pathSub;
  const pos = useMemo(() => {
    positions.current[mode] = place(positions.current[mode], view, anchor ?? null, !!view.overview, pinned);
    return positions.current[mode];
  }, [view, mode, anchor, pinned]);

  // Search edges name their ends but not their kinds: look those up, once per id per ask.
  const missing = unknownKinds(view).filter((id) => !asked.current.has(id) && !id.includes(","));
  const missingKey = missing.join(",");
  useEffect(() => {
    if (!missingKey) return;
    // Waits for staging to settle, so a burst of edge events makes one request, not one each.
    const timer = setTimeout(() => {
      const ids = missingKey.split(",");
      ids.forEach((id) => asked.current.add(id));
      for (let i = 0; i < ids.length; i += KIND_BATCH) {
        api.entities(ids.slice(i, i + KIND_BATCH), world).then((r) => {
          if (!mounted.current || isError(r) || !r.entities.length) return;
          setKinds((k) => ({ ...k, ...Object.fromEntries(r.entities.map((e) => [e.id, { kind: e.kind, books: e.books, world: e.world }])) }));
        });
      }
    }, KIND_DELAY_MS);
    return () => clearTimeout(timer);
  }, [missingKey, world]);

  // subgraph_by_id canonicalises the id, so the expansion's first node is the real root.
  const expand = (clicked: string) => (data: SubgraphData) =>
    setExpansions((e) => ({ ...e, [mode]: [...e[mode], { root: data.nodes[0]?.id ?? clicked, data }] }));
  // Brings an entity into the graph (with its neighbours) when it isn't there, then centres it.
  const latest = useRef(0);
  const reveal = (id: string, centre: boolean) => {
    const token = ++latest.current;
    if (view.nodes.some((n) => n.id === id)) { if (centre) setFocus((f) => ({ id, n: f.n + 1 })); return; }
    api.subgraph(id, world).then((r) => {
      if (!mounted.current || token !== latest.current || isError(r) || !r.nodes.length) return;
      expand(id)(r);
      if (centre) setFocus((f) => ({ id: r.nodes[0].id, n: f.n + 1 }));
    });
  };
  const openEntity = (id: string, centre = true) => { setPanel({ type: "entity", id }); reveal(id, centre); };
  const openEdge = (a: string, b: string) => {
    setPanel({ type: "edge", a, b });
    // The edge needs both ends in the graph to be seen.
    if (!view.nodes.some((n) => n.id === a) || !view.nodes.some((n) => n.id === b)) reveal(view.nodes.some((n) => n.id === a) ? b : a, false);
  };
  const connectToken = useRef(0);
  const connect = async (from: { id: string; name: string }, to: SearchHit) => {
    const target = { id: to.id, name: to.name };
    if (from.id === to.id) { setConnection({ from, to: target, status: "same" }); return; }
    const token = ++connectToken.current;
    setConnection({ from, to: target, status: "loading" });
    const r = await api.path(from.id, to.id, world);
    if (!mounted.current || token !== connectToken.current) return;
    setConnection({ from, to: target, result: isError(r) ? undefined : r,
      status: isError(r) ? "error" : r.found ? "found" : r.timed_out ? "timed_out" : "none" });
  };
  const clearPath = () => { connectToken.current++; setConnection(null); };
  const changeFilters = (f: Filters) => { setFilters(f); saveFilters(f); };

  const running = state.steps.find((s) => s.state === "running");
  const stage = mode === "library" ? (shownOverview ? UI.overviewStage : UI.graphIdle)
    : running ? `${STEP_LABEL[running.name]}…` : state.status === "done" ? UI.answeredIn(state.elapsed) : UI.graphIdle;
  // Several worlds drawn together: each is a cluster, marked on its nodes and listed here.
  const marked = worldsIn(view);
  const showHint = mode === "library" && overview === null && view.nodes.length === 0;
  const busy = state.status === "running";
  const seedIds = useMemo(() => new Set(state.seeds.flatMap((list) => list?.map((s) => s.id) ?? [])), [state.seeds]);
  return (
    <section className="pane graphpane" aria-label="Evidence graph">
      <div className="gbar">
        <span className="stage">{stage}</span>
        {!idle && (
          <div className="seg" role="group" aria-label={EXPLORE.modeLabel}>
            <button type="button" aria-pressed={mode === "answer"} onClick={() => setChosen("answer")}>{EXPLORE.modeAnswer}</button>
            <button type="button" aria-pressed={mode === "library"} onClick={() => setChosen("library")}>{EXPLORE.modeLibrary}</button>
          </div>
        )}
        {!noBooks && view.nodes.length > 0 && <button className="tbtn" type="button" onClick={() => setFitRequest((n) => n + 1)}>{UI.fitGraph}</button>}
        {mode === "answer" && view.shownEdges < view.totalEdges && <span>{UI.showingEdges(view.shownEdges, view.totalEdges)}</span>}
        <div className="legend">
          {mode === "answer" && (state.subQuestions.length > 1
            ? state.subQuestions.map((q, i) => <span key={i} title={q}><i className="ring" style={{ borderColor: `var(--part-${i % 4})` }} />{UI.legendPart(i + 1)}</span>)
            : <span><i className="ring" />{UI.legendStart}</span>)}
          {marked.length > 0 && <span className="wlegend" role="group" aria-label={WORLDS.legendLabel}>
            {marked.map((w) => <span key={w.id}><i className="wmark" style={{ borderColor: `var(--world-${worldSlot(w.id)})` }} />{w.name}</span>)}
          </span>}
          {UI.legendKinds.map(([token, label]) => <span key={token}><i style={{ background: `var(${token})` }} />{label}</span>)}
          {mode === "answer" && <span><i style={{ background: "var(--glow)", borderRadius: 2, height: 3, width: 14 }} />{UI.legendCited}</span>}
        </div>
      </div>
      <div className="gtools">
        <EntitySearch world={world} label={EXPLORE.searchLabel} placeholder={EXPLORE.searchPlaceholder} onPick={(h) => openEntity(h.id)} />
        <FilterBar filters={filters} onChange={changeFilters} books={graphed.map((b) => ({ id: b.id, title: b.title }))} hiddenCount={view.hiddenByFilters ?? 0} />
      </div>
      <div className="gwrap">
        {noBooks ? <p className="hint" style={{ padding: 16 }}>{UI.noBooks}</p>
          : showHint ? <p className="hint" style={{ padding: 16 }}>{UI.overviewHint}</p>
          : <GraphCanvas view={view} positions={pos} onNodeClick={(id) => openEntity(id, false)} onEdgeClick={openEdge}
            pinned={pinned} fitRequest={fitRequest} focusId={focus.id} focusRequest={focus.n} />}
        {(panel || connection) && (
          <div className="gside">
            {panel?.type === "entity" && <EntityPanel key={panel.id} id={panel.id} world={world} positionVersion={positionVersion} bookTitles={bookTitles} seed={seedIds.has(panel.id)}
              busy={busy} onClose={() => setPanel(null)} onSelect={(id) => openEntity(id)} onExpand={expand(panel.id)} onOpenEdge={openEdge}
              onAsk={onAsk} onConnect={connect} />}
            {panel?.type === "edge" && <EdgeView key={`${panel.a}|${panel.b}`} a={panel.a} b={panel.b} world={world} positionVersion={positionVersion} bookTitles={bookTitles} busy={busy}
              onClose={() => setPanel(null)} onSelect={(id) => openEntity(id)} onAsk={onAsk} />}
            {connection && <ConnectionView connection={connection} world={world} bookTitles={bookTitles} onSelect={(id) => openEntity(id)} onClear={clearPath} />}
          </div>
        )}
      </div>
    </section>
  );
}
