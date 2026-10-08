import { render, waitFor } from "@testing-library/react";
import { askReducer, initialState } from "../askReducer";
import type { GraphView } from "../graphView";
import type { AskEvent, Edge } from "../types";
import { GraphPane } from "./GraphPane";

const views: GraphView[] = [];
const entities = vi.fn((ids: string[]) => Promise.resolve({ entities: ids.filter((i) => i === "x").map((id) => ({ id, name: "X", kind: "Place", books: ["b1"] })) }));
vi.mock("./GraphCanvas", () => ({ GraphCanvas: ({ view }: { view: GraphView }) => { views.push(view); return <div />; } }));
vi.mock("../api", () => ({
  api: { entities: (ids: string[]) => entities(ids) },
  isError: (x: unknown) => typeof x === "object" && x !== null && "error" in x,
}));

const edge = (s: string, o: string, chunk: string): Edge => ({ subject: s, predicate: "KNOWS", predicate_raw: "", object: o, evidence: "",
  confidence: 1, book_id: "b1", chapter_seq: 1, chunk_id: chunk, subject_id: s, object_id: o });
const events: AskEvent[] = [
  { type: "parts", t: 0, sub_questions: ["a"] },
  { type: "seeds", t: 0, part: 0, entities: [{ id: "h", name: "Hope", kind: "Character", books: ["b1"], score: 0 }] },
  { type: "edges", t: 0, part: 0, template: "neighbours", total: 2, edges: [edge("h", "x", "c1"), edge("h", "y", "c2")] },
];

test("node kinds the search did not carry are fetched once, in one batch, and applied", async () => {
  const state = events.reduce(askReducer, initialState("q"));
  const props = { hiddenBooks: new Set<string>(), hoverChunk: null, noBooks: false };
  const { rerender } = render(<GraphPane state={state} {...props} />);
  await waitFor(() => expect(views.at(-1)!.nodes.find((n) => n.id === "x")!.kind).toBe("Place"));
  rerender(<GraphPane state={state} {...props} hoverChunk="c1" />);
  await new Promise((r) => setTimeout(r, 250));
  expect(entities).toHaveBeenCalledTimes(1);
  expect(entities.mock.calls[0][0].sort()).toEqual(["x", "y"]);
  expect(views.at(-1)!.nodes.find((n) => n.id === "y")!.kind).toBe("Other"); // unknown to the graph: stays Other
});
