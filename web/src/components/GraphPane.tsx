import { useEffect, useMemo, useRef, useState } from "react";
import { api, isError } from "../api";
import type { AskState } from "../askReducer";
import { deriveGraph, unknownKinds, type KindMap } from "../graphView";
import { place, type Positions } from "../layout";
import { STEP_LABEL, UI } from "../messages";
import type { SubgraphData } from "../types";
import { EntityCard } from "./EntityCard";
import { GraphCanvas } from "./GraphCanvas";

interface Expansion { root: string; data: SubgraphData }
// Ids per /api/entities request: keeps the URL well under the server's request-line limit.
const KIND_BATCH = 100;
const KIND_DELAY_MS = 150;

// Keyed on the ask by its parent, so a new ask (even of the same text) starts with no expansions,
// no camera pin, no positions and no kinds, and late results for the old ask land nowhere.
// `overview` is the library overview (App fetches it once): drawn only while no question has been asked.
// undefined = still loading; null = unavailable or empty.
export function GraphPane({ state, hiddenBooks, hoverChunk, noBooks, bookTitles, overview }:
  { state: AskState; hiddenBooks: Set<string>; hoverChunk: string | null; noBooks: boolean; bookTitles?: Map<string, string>;
    overview?: SubgraphData | null }) {
  const [expansions, setExpansions] = useState<Expansion[]>([]);
  const [fitRequest, setFitRequest] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [kinds, setKinds] = useState<KindMap>({});
  const asked = useRef(new Set<string>());
  const positions = useRef<Positions>(new Map());
  const idle = state.status === "idle" && !state.question;
  const shownOverview = idle && overview?.nodes.length ? overview : null;
  const view = useMemo(() => deriveGraph(state, { hiddenBooks, hoverChunk, expansions: expansions.map((e) => e.data), kinds,
    overview: shownOverview }), [state, hiddenBooks, hoverChunk, expansions, kinds, shownOverview]);
  const pos = useMemo(() => {
    positions.current = place(positions.current, view, expansions.at(-1)?.root ?? null, !!view.overview);
    return positions.current;
  }, [view, expansions]);

  // Search edges name their ends but not their kinds: look those up, once per id per ask.
  const missing = unknownKinds(view).filter((id) => !asked.current.has(id) && !id.includes(","));
  const missingKey = missing.join(",");
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    if (!missingKey) return;
    // Waits for staging to settle, so a burst of edge events makes one request, not one each.
    const timer = setTimeout(() => {
      const ids = missingKey.split(",");
      ids.forEach((id) => asked.current.add(id));
      for (let i = 0; i < ids.length; i += KIND_BATCH) {
        api.entities(ids.slice(i, i + KIND_BATCH)).then((r) => {
          if (!mounted.current || isError(r) || !r.entities.length) return;
          setKinds((k) => ({ ...k, ...Object.fromEntries(r.entities.map((e) => [e.id, { kind: e.kind, books: e.books }])) }));
        });
      }
    }, KIND_DELAY_MS);
    return () => clearTimeout(timer);
  }, [missingKey]);

  const running = state.steps.find((s) => s.state === "running");
  const stage = running ? `${STEP_LABEL[running.name]}…` : state.status === "done" ? UI.answeredIn(state.elapsed)
    : shownOverview ? UI.overviewStage : UI.graphIdle;
  const showHint = idle && overview === null && view.nodes.length === 0;
  // subgraph_by_id canonicalises the id, so the expansion's first node is the real root.
  const expand = (clicked: string) => (data: SubgraphData) =>
    setExpansions((e) => [...e, { root: data.nodes[0]?.id ?? clicked, data }]);
  return (
    <section className="pane graphpane" aria-label="Evidence graph">
      <div className="gbar">
        <span className="stage">{stage}</span>
        {!noBooks && view.nodes.length > 0 && <button className="tbtn" type="button" onClick={() => setFitRequest((n) => n + 1)}>{UI.fitGraph}</button>}
        {view.shownEdges < view.totalEdges && <span>{UI.showingEdges(view.shownEdges, view.totalEdges)}</span>}
        <div className="legend">
          {state.subQuestions.length > 1
            ? state.subQuestions.map((q, i) => <span key={i} title={q}><i className="ring" style={{ borderColor: `var(--part-${i % 4})` }} />{UI.legendPart(i + 1)}</span>)
            : !shownOverview && <span><i className="ring" />{UI.legendStart}</span>}
          {UI.legendKinds.map(([token, label]) => <span key={token}><i style={{ background: `var(${token})` }} />{label}</span>)}
          <span><i style={{ background: "var(--glow)", borderRadius: 2, height: 3, width: 14 }} />{UI.legendCited}</span>
        </div>
      </div>
      <div className="gwrap">
        {noBooks ? <p className="hint" style={{ padding: 16 }}>{UI.noBooks}</p>
          : showHint ? <p className="hint" style={{ padding: 16 }}>{UI.overviewHint}</p>
          : <GraphCanvas view={view} positions={pos} onNodeClick={setSelected} pinned={expansions.length > 0} fitRequest={fitRequest} />}
        {selected && <EntityCard key={selected} id={selected} onClose={() => setSelected(null)} onExpand={expand(selected)}
          answerEdges={state.edges.flatMap((pe) => pe?.edges ?? []).filter((e) => e.subject_id === selected || e.object_id === selected)}
          seed={state.seeds.some((list) => list?.some((s) => s.id === selected))} bookTitles={bookTitles} />}
      </div>
    </section>
  );
}
