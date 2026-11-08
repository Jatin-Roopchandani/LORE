import { askReducer, initialState, type AskState } from "../askReducer";
import { focusGraph } from "../graphFocus";
import { deriveGraph, type GraphView } from "../graphView";
import { place, type Positions } from "../layout";
import type { AskEvent } from "../types";

// An answer as the graph pane saw it: the events folded one by one, the layout updated after each
// (as GraphPane does in answer mode, before any expansion). Returns the final view and positions.
// `opts`: as the graph pane draws an answer now: "uncategorized" relationships left out, and a big answer focused.
// Without them, the view as it was before either (what the layout's own tests measure).
export function replay(events: readonly AskEvent[], question = "q", opts: { hideUncategorized?: boolean; focus?: boolean } = {}):
    { state: AskState; view: GraphView; positions: Positions } {
  let state = initialState(question);
  let positions: Positions = new Map();
  let view: GraphView = deriveGraph(state, { hiddenBooks: new Set(), hoverChunks: [], expansions: [] });
  for (const e of events) {
    state = askReducer(state, e);
    view = deriveGraph(state, { hiddenBooks: new Set(), hoverChunks: [], expansions: [], hideUncategorized: opts.hideUncategorized });
    if (opts.focus) view = focusGraph(view);
    positions = place(positions, view);
  }
  return { state, view, positions };
}
