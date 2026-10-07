import { useEffect, useMemo, useRef, useState } from "react";
import type { AskState } from "../askReducer";
import { deriveGraph } from "../graphView";
import { place, type Positions } from "../layout";
import { STEP_LABEL, UI } from "../messages";
import type { SubgraphData } from "../types";
import { EntityCard } from "./EntityCard";
import { GraphCanvas } from "./GraphCanvas";

interface Expansion { root: string; data: SubgraphData }

export function GraphPane({ state, hiddenBooks, hoverChunk, noBooks, bookTitles }:
  { state: AskState; hiddenBooks: Set<string>; hoverChunk: string | null; noBooks: boolean; bookTitles?: Map<string, string> }) {
  const [expansions, setExpansions] = useState<Expansion[]>([]);
  const [fitRequest, setFitRequest] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const positions = useRef<Positions>(new Map());
  const lastQuestion = useRef(state.question);
  if (lastQuestion.current !== state.question) { lastQuestion.current = state.question; positions.current = new Map(); }
  useEffect(() => { setExpansions([]); setSelected(null); }, [state.question]);
  const view = useMemo(() => deriveGraph(state, { hiddenBooks, hoverChunk, expansions: expansions.map((e) => e.data) }),
    [state, hiddenBooks, hoverChunk, expansions]);
  const pos = useMemo(() => {
    positions.current = place(positions.current, view, expansions.at(-1)?.root ?? null);
    return positions.current;
  }, [view, expansions]);
  const running = state.steps.find((s) => s.state === "running");
  const stage = running ? `${STEP_LABEL[running.name]}…` : state.status === "done" ? UI.answeredIn(state.elapsed) : UI.graphIdle;
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
            ? state.subQuestions.map((q, i) => <span key={i} title={q}><i style={{ background: `var(--part-${i % 4})` }} />{`Part ${i + 1}`}</span>)
            : <span><i style={{ background: "var(--part-0)" }} />Starting entity</span>}
          <span><i style={{ background: "var(--k-character)" }} />Character</span>
          <span><i style={{ background: "var(--k-place)" }} />Place</span>
          <span><i style={{ background: "var(--k-faction)" }} />Group</span>
          <span><i style={{ background: "var(--k-object)" }} />Object</span>
          <span><i style={{ background: "var(--glow)", borderRadius: 2, height: 3, width: 14 }} />Cited</span>
        </div>
      </div>
      <div className="gwrap">
        {noBooks ? <p className="hint" style={{ padding: 16 }}>{UI.noBooks}</p>
          : <GraphCanvas view={view} positions={pos} onNodeClick={setSelected} pinned={expansions.length > 0} fitRequest={fitRequest} />}
        {selected && <EntityCard id={selected} onClose={() => setSelected(null)} onExpand={expand(selected)}
          answerEdges={state.edges.flatMap((pe) => pe?.edges ?? []).filter((e) => e.subject_id === selected || e.object_id === selected)}
          seed={state.seeds.some((list) => list?.some((s) => s.id === selected))} bookTitles={bookTitles} />}
      </div>
    </section>
  );
}
