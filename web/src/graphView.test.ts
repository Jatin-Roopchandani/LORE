import { deriveGraph, unknownKinds } from "./graphView";
import { askReducer, initialState } from "./askReducer";
import two from "./fixtures/two-part.json";
import type { AskEvent, Edge, SubgraphData } from "./types";

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

const sub = (root: string, others: string[]) => ({
  nodes: [root, ...others].map((id) => ({ id, name: id, kind: "Character", books: ["b1"] })),
  edges: others.map((o) => ({ subject: root, subject_id: root, object: o, object_id: o, count: 1, predicates: { KNOWS: 1 } })),
  truncated: false, depth: 1, note: null,
} as any);

test("expansion marks root, adds part-null shown links, search link wins", () => {
  const v = deriveGraph(stateWith(base), { ...opts, expansions: [sub("h", ["d", "z"])] });
  expect(v.nodes.find((n) => n.id === "h")!.expanded).toBe(true);
  expect(v.nodes.find((n) => n.id === "z")!.expanded).toBe(false);
  expect(v.links.filter((l) => l.source === "h" && l.target === "d")).toHaveLength(1);
  expect(v.links.find((l) => l.target === "d")!.part).toBe(0);
  const hz = v.links.find((l) => l.target === "z")!;
  expect(hz.part).toBeNull();
  expect(hz.state).toBe("shown");
});

test("a node with every book hidden is dim", () => {
  const v = deriveGraph(stateWith(base), { ...opts, hiddenBooks: new Set(["b2"]) });
  expect(v.nodes.find((n) => n.id === "x")!.state).toBe("dim");
  expect(v.nodes.find((n) => n.id === "d")!.state).toBe("shown");
});

test("hidden book forces faint even on a cited link", () => {
  const result = { answer: "", mode: "graph", template: null, note: null, model: null, selection_model: null, sub_questions: ["a", "b"],
    claims: [{ text: "t", passage: 1, quote: "q" }], passages: [{ n: 1, chunk_id: "c3", book_id: "b2", book_title: null, chapter_seq: 1, text: "" }],
    edges: [], seeds: [] };
  const s = stateWith([...base, { type: "answer", t: 1, result }]);
  expect(deriveGraph(s, opts).links.find((l) => l.target === "x")!.state).toBe("cited");
  expect(deriveGraph(s, { ...opts, hiddenBooks: new Set(["b2"]) }).links.find((l) => l.target === "x")!.state).toBe("faint");
});

const answerFor = (chunk: string) => ({ answer: "", mode: "graph", template: null, note: null, model: null, selection_model: null,
  sub_questions: ["a"], claims: [{ text: "t", passage: 1, quote: "q" }],
  passages: [{ n: 1, chunk_id: chunk, book_id: "b1", book_title: null, chapter_seq: 1, text: "" }], edges: [], seeds: [] });
const pair: AskEvent[] = [
  { type: "parts", t: 0, sub_questions: ["a"] },
  { type: "edges", t: 0, part: 0, template: "neighbours", total: 5, edges: [
    edge("h", "f", "c1", "b1", "INTERACTS_WITH"), edge("h", "f", "c2", "b1", "INTERACTS_WITH"),
    edge("f", "h", "c3", "b2", "INTERACTS_WITH"), edge("h", "l", "c4", "b1", "LOVES"), edge("l", "h", "c5", "b1", "LOVED_BY")] },
];

test("a reciprocal pair merges into one undirected link with a combined label", () => {
  const v = deriveGraph(stateWith(pair), opts);
  const hf = v.links.filter((l) => [l.source, l.target].sort().join() === "f,h");
  expect(hf).toHaveLength(1);
  expect(hf[0].both).toBe(true);
  expect(hf[0].count).toBe(3);
  expect(hf[0].label).toBe("interacts with ×2 · ×1");
  expect(hf[0].chunkIds.sort()).toEqual(["c1", "c2", "c3"]);
  expect(hf[0].books.sort()).toEqual(["b1", "b2"]);
  expect(v.links.find((l) => l.source === "h" && l.target === "l")!.label).toBe("loves / loved by");
  expect(v.links).toHaveLength(2);
});

test("a merged pair is cited when either direction is cited", () => {
  const v = deriveGraph(stateWith([...pair, { type: "answer", t: 1, result: answerFor("c3") }]), opts);
  const hf = v.links.find((l) => [l.source, l.target].sort().join() === "f,h")!;
  expect(hf.state).toBe("cited");
  expect(v.links.find((l) => l.target === "l")!.state).toBe("faint");
});

test("a single-direction link keeps its arrow", () => {
  const v = deriveGraph(stateWith(base), opts);
  expect(v.links.every((l) => !l.both)).toBe(true);
});

test("expansion links in both directions merge too", () => {
  const s = { ...sub("h", ["z"]), edges: [
    { subject: "h", subject_id: "h", object: "z", object_id: "z", count: 2, predicates: { KNOWS: 2 } },
    { subject: "z", subject_id: "z", object: "h", object_id: "h", count: 1, predicates: { KNOWS: 1 } }] };
  const v = deriveGraph(stateWith(base), { ...opts, expansions: [s] });
  const hz = v.links.filter((l) => [l.source, l.target].sort().join() === "h,z");
  expect(hz).toHaveLength(1);
  expect(hz[0]).toMatchObject({ both: true, count: 3, label: "knows ×2 · ×1", state: "shown" });
});

test("a kinds map gives search-edge endpoints their kind and books", () => {
  const before = deriveGraph(stateWith(base), opts);
  expect(before.nodes.find((n) => n.id === "x")!.kind).toBe("Other");
  expect(unknownKinds(before)).toEqual(["x"]);
  const v = deriveGraph(stateWith(base), { ...opts, kinds: { x: { kind: "Place", books: ["b3"] }, h: { kind: "Object", books: [] } } });
  const x = v.nodes.find((n) => n.id === "x")!;
  expect(x.kind).toBe("Place");
  expect(x.books).toEqual(["b2", "b3"]);
  expect(v.nodes.find((n) => n.id === "h")!.kind).toBe("Character"); // a seed's own kind stands
  expect(unknownKinds(v)).toEqual([]);
});

test("an overview draws its nodes and links with their kinds and marks nothing expanded", () => {
  const overview: SubgraphData = { truncated: false, depth: 0, nodes: [
    { id: "h", name: "Holmes", kind: "Character", books: ["b1"] }, { id: "p", name: "Baker Street", kind: "Place", books: ["b1"] },
    { id: "w", name: "Watson", kind: "Character", books: ["b1"] }],
  edges: [{ subject: "Holmes", subject_id: "h", object: "Baker Street", object_id: "p", count: 3, predicates: { LIVES_AT: 3 } },
    { subject: "Watson", subject_id: "w", object: "Holmes", object_id: "h", count: 2, predicates: { KNOWS: 2 } }] };
  const v = deriveGraph(initialState(), { ...opts, overview });
  expect(v.nodes.map((n) => n.id)).toEqual(["h", "p", "w"]);
  expect(v.nodes.some((n) => n.expanded)).toBe(false);
  expect(v.nodes.find((n) => n.id === "p")!.kind).toBe("Place");
  expect(v.links.map((l) => l.id).sort()).toEqual(["h<->p", "h<->w"]);
  expect(v.overview).toBe(true);
  // Overview links are faint and unlabelled: 40 hubs with labelled links are a hairball.
  expect(v.links.every((l) => l.state === "faint" && l.label === "")).toBe(true);
  expect(unknownKinds(v)).toEqual([]);
  // An expansion on top of the overview still marks its own root.
  const more = deriveGraph(initialState(), { ...opts, overview, expansions: [{ truncated: false, depth: 1,
    nodes: [{ id: "w", name: "Watson", kind: "Character", books: ["b1"] }, { id: "m", name: "Mary", kind: "Character", books: ["b1"] }],
    edges: [{ subject: "Watson", subject_id: "w", object: "Mary", object_id: "m", count: 1, predicates: { MARRIES: 1 } }] }] });
  expect(more.nodes.filter((n) => n.expanded).map((n) => n.id)).toEqual(["w"]);
  expect(more.links).toHaveLength(3);
  // The expansion's links keep their labels and normal styling, even a pair the overview also drew.
  expect(more.links.find((l) => l.id === "m<->w")).toMatchObject({ state: "shown", label: "marries" });
  expect(more.links.find((l) => l.id === "h<->p")).toMatchObject({ state: "faint", label: "" });
  const again = deriveGraph(initialState(), { ...opts, overview, expansions: [{ truncated: false, depth: 1,
    nodes: [{ id: "w", name: "Watson", kind: "Character", books: ["b1"] }, { id: "h", name: "Holmes", kind: "Character", books: ["b1"] }],
    edges: [{ subject: "Watson", subject_id: "w", object: "Holmes", object_id: "h", count: 2, predicates: { KNOWS: 2 } }] }] });
  expect(again.links.find((l) => l.id === "h<->w")).toMatchObject({ state: "shown", label: "knows ×2" });
  // Without an overview nothing is quiet.
  expect(deriveGraph(stateWith(base), opts).overview).toBe(false);
});
