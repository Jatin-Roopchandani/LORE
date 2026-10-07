import { deriveGraph } from "./graphView";
import { askReducer, initialState } from "./askReducer";
import two from "./fixtures/two-part.json";
import type { AskEvent, Edge } from "./types";

const opts = { hiddenBooks: new Set<string>(), hoverChunk: null, expansions: [] };
const edge = (s: string, o: string, chunk: string, book = "b1", p = "KILLS"): Edge => ({ subject: s.toUpperCase(), predicate: p,
  predicate_raw: p, object: o.toUpperCase(), evidence: "", confidence: 1, book_id: book, chapter_seq: 1, chunk_id: chunk, subject_id: s, object_id: o });

function stateWith(events: AskEvent[]) { return events.reduce(askReducer, initialState("q")); }
const base: AskEvent[] = [
  { type: "parts", t: 0, sub_questions: ["a", "b"] },
  { type: "seeds", t: 0, part: 0, entities: [{ id: "h", name: "Hope", kind: "Character", books: ["b1"], score: 0 }] },
  { type: "seeds", t: 0, part: 1, entities: [{ id: "h", name: "Hope", kind: "Character", books: ["b1"], score: 0 }, { id: "d", name: "Drebber", kind: "Character", books: ["b1"], score: 0 }] },
  { type: "edges", t: 0, part: 0, template: "neighbours", total: 400, edges: [edge("h", "d", "c1"), edge("h", "d", "c2", "b1", "OPPOSES"), edge("h", "x", "c3", "b2")] },
];

test("groups edges by pair with the top predicate and count", () => {
  const v = deriveGraph(stateWith(base), opts);
  const hd = v.links.find((l) => l.source === "h" && l.target === "d")!;
  expect(hd.count).toBe(2);
  expect(hd.label).toMatch(/×2$/);
  expect(v.totalEdges).toBe(400);
  expect(v.shownEdges).toBe(3);
});

test("seed colour follows the first part that found it", () => {
  const v = deriveGraph(stateWith(base), opts);
  expect(v.nodes.find((n) => n.id === "h")!.seedPart).toBe(0);
  expect(v.nodes.find((n) => n.id === "d")!.seedPart).toBe(1);
});

test("edges whose passage missed the evidence cut are cut", () => {
  const v = deriveGraph(stateWith([...base, { type: "evidence", t: 0, chunk_ids: ["c1"], edge_keys: ["h|KILLS|d|c1"] }]), opts);
  expect(v.links.find((l) => l.target === "d")!.state).toBe("shown");
  expect(v.links.find((l) => l.target === "x")!.state).toBe("cut");
});

test("after the answer, cited links glow and the rest fade; hover marks hot", () => {
  const result = { answer: "", mode: "graph", template: null, note: null, model: null, selection_model: null, sub_questions: ["a", "b"],
    claims: [{ text: "t", passage: 1, quote: "q" }], passages: [{ n: 1, chunk_id: "c2", book_id: "b1", book_title: null, chapter_seq: 1, text: "" }],
    edges: [], seeds: [] };
  const s = stateWith([...base, { type: "answer", t: 1, result }]);
  const v = deriveGraph(s, { ...opts, hoverChunk: "c2" });
  const hd = v.links.find((l) => l.target === "d")!;
  expect(hd.state).toBe("cited");
  expect(hd.hot).toBe(true);
  expect(v.links.find((l) => l.target === "x")!.state).toBe("faint");
});

test("hiding a book dims its nodes and links", () => {
  const v = deriveGraph(stateWith(base), { ...opts, hiddenBooks: new Set(["b2"]) });
  expect(v.links.find((l) => l.target === "x")!.state).toBe("faint");
  expect(v.nodes.find((n) => n.id === "h")!.state).toBe("shown");
});

test("a recorded answer derives without throwing", () => {
  const v = deriveGraph((two as AskEvent[]).reduce(askReducer, initialState("q")), opts);
  expect(v.nodes.length).toBeGreaterThan(1);
  expect(v.links.some((l) => l.state === "cited")).toBe(true);
});
