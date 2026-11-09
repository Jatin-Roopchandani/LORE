import { askReducer, initialState } from "./askReducer";
import { FOCUS_FROM, FOCUS_TARGET, focusGraph, highlightPart } from "./graphFocus";
import { deriveGraph, type GraphView } from "./graphView";
import { measureGraph } from "./graphMetrics";
import { replay } from "./fixtures/replay";
import { focusLayout } from "./layout";
import { FOCUS_KEY, loadFilters, loadFocus, saveFilters, saveFocus } from "./filters";
import golden from "./fixtures/small-answer-layout.golden.json";
import single from "./fixtures/single-part.json";
import twoPart from "./fixtures/two-part.json";
import ogier from "./fixtures/answer-graph-ogier.json";
import darkOne from "./fixtures/answer-graph-dark-one.json";
import battle from "./fixtures/answer-graph-battle.json";
import type { AskEvent, Edge, SubgraphData } from "./types";

const BIG = { ogier, "dark one": darkOne, battle } as Record<string, AskEvent[]>;
const opts = { hiddenBooks: new Set<string>(), hoverChunks: [], expansions: [] };
const whole = (events: AskEvent[], hideUncategorized = true) =>
  deriveGraph(events.reduce(askReducer, initialState("q")), { ...opts, hideUncategorized });
const PANE = { width: 954, height: 714 };

for (const [name, events] of Object.entries(BIG)) {
  test(`a big answer (${name}) is focused on its cited links and seeds, deterministically, at about 20-40 nodes`, () => {
    const all = whole(events);
    expect(all.nodes.length).toBeGreaterThanOrEqual(FOCUS_FROM);
    const a = focusGraph(all), b = focusGraph(all);
    expect(b).toEqual(a);
    expect(a.focus).toEqual({ total: all.nodes.length });
    expect(a.nodes.length).toBeGreaterThanOrEqual(20);
    expect(a.nodes.length).toBeLessThanOrEqual(40);
    const ids = new Set(a.nodes.map((n) => n.id)), links = new Set(a.links.map((l) => l.id));
    // Every cited link and both its ends, and every seed.
    for (const l of all.links.filter((x) => x.state === "cited")) {
      expect(links.has(l.id)).toBe(true);
      expect(ids.has(l.source) && ids.has(l.target)).toBe(true);
    }
    for (const n of all.nodes.filter((x) => x.seedPart !== null)) expect(ids.has(n.id)).toBe(true);
    // Only links between kept nodes; none floats loose that has a tie in the picture.
    for (const l of a.links) expect(ids.has(l.source) && ids.has(l.target)).toBe(true);
    const linked = new Set(a.links.flatMap((l) => [l.source, l.target]));
    for (const n of a.nodes) {
      const hasTie = all.links.some((l) => (l.source === n.id && ids.has(l.target)) || (l.target === n.id && ids.has(l.source)));
      if (hasTie) expect(linked.has(n.id)).toBe(true);
    }
  });
}

test("the focused view adds at most a few neighbours, and none past the target once the answer's own nodes fill it", () => {
  const all = whole(ogier as AskEvent[]);
  const anchoredIds = new Set([...all.nodes.filter((n) => n.seedPart !== null).map((n) => n.id),
    ...all.links.filter((l) => l.state === "cited").flatMap((l) => [l.source, l.target])]);
  const f = focusGraph(all);
  const extra = f.nodes.filter((n) => !anchoredIds.has(n.id)).length;
  expect(f.nodes.length).toBeLessThanOrEqual(Math.max(FOCUS_TARGET, anchoredIds.size));
  expect(extra).toBeLessThanOrEqual(2 * all.nodes.filter((n) => n.seedPart !== null).length);
});

test("the Ogier answer's focused view: no dot, name or cited label lost, and few label collisions", () => {
  const { view, positions } = replay(ogier as AskEvent[], "q", { hideUncategorized: true, focus: true });
  expect(view.focus).toBeDefined();
  const m = measureGraph(view, positions, PANE);
  expect([m.nodeOverlaps, m.labelOverlaps, m.labelOnNode, m.offCanvas]).toEqual([0, 0, 0, 0]);
  expect(m.citedLabelsDrawn).toBe(m.citedLabels);
  expect(m.edgeLabelOverlaps).toBeLessThanOrEqual(6);
  const full = replay(ogier as AskEvent[], "q", { hideUncategorized: true });
  expect(m.edgeLabelOverlaps).toBeLessThan(measureGraph(full.view, full.positions, PANE).edgeLabelOverlaps);
  expect(focusLayout(view)).toEqual(focusLayout(view));          // deterministic
});

test("a small answer is drawn exactly as before: never focused, laid out and sized as the golden file", () => {
  for (const [name, events] of Object.entries({ one: single, two: twoPart })) {
    const all = whole(events as AskEvent[], false);
    expect(focusGraph(all)).toBe(all);
    const { view, positions } = replay(events as AskEvent[], "q", { focus: true });
    expect(view.focus).toBeUndefined();
    const want = (golden as Record<string, { positions: Record<string, { x: number; y: number }> }>)[name];
    expect(Object.fromEntries(positions)).toEqual(want.positions);
  }
});

test("the library overview is never focused", () => {
  const all = whole(ogier as AskEvent[]);
  const overview: GraphView = { ...all, overview: true };
  expect(focusGraph(overview)).toBe(overview);
});

const row = (s: string, p: string, o: string, chunk = "c1"): Edge => ({ subject: s, predicate: p, predicate_raw: p, object: o, evidence: "",
  confidence: 1, book_id: "b1", chapter_seq: 1, chunk_id: chunk, subject_id: s, object_id: o });
const small: AskEvent[] = [
  { type: "parts", t: 0, sub_questions: ["a"] },
  { type: "seeds", t: 0, part: 0, entities: [{ id: "a", name: "A", kind: "Character", books: ["b1"], score: 0 }] },
  { type: "edges", t: 0, part: 0, template: "t", total: 6, edges: [
    row("a", "UNCATEGORIZED", "b"), row("a", "EXPERIENCES", "b"), row("a", "EXPERIENCES", "b", "c2"),
    row("a", "UNCATEGORIZED", "c"), row("a", "UNCATEGORIZED", "c", "c3"), row("d", "KILLS", "a")] },
];

test("uncategorized relationships are left out when hidden: a merged label keeps its named part, a pair with only those goes", () => {
  const shown = whole(small, false);
  expect(shown.links.find((l) => l.id === "a<->b")!.label).toBe("experiences ×2 +1");
  expect(shown.links.some((l) => l.id === "a<->c")).toBe(true);
  const hidden = whole(small, true);
  expect(hidden.links.find((l) => l.id === "a<->b")!.label).toBe("experiences ×2");
  expect(hidden.links.find((l) => l.id === "a<->b")!.count).toBe(2);
  expect(hidden.links.some((l) => l.id === "a<->c")).toBe(false);
  expect(hidden.nodes.some((n) => n.id === "c")).toBe(false);          // reached only by uncategorized links
  expect(hidden.hiddenUncategorized).toBe(3);
  expect(hidden.links.every((l) => !/uncategorized/.test(l.label))).toBe(true);
});

test("uncategorized predicates leave an expansion's labels too, and a link with only those goes", () => {
  const sub: SubgraphData = { truncated: false, depth: 1, nodes: [{ id: "a", name: "A", kind: "Character", books: [] }, { id: "e", name: "E", kind: "Place", books: [] },
    { id: "f", name: "F", kind: "Place", books: [] }], edges: [
    { subject: "A", subject_id: "a", object: "E", object_id: "e", count: 4, predicates: { UNCATEGORIZED: 3, LOCATED_IN: 1 } },
    { subject: "A", subject_id: "a", object: "F", object_id: "f", count: 2, predicates: { UNCATEGORIZED: 2 } }] };
  const state = small.reduce(askReducer, initialState("q"));
  const off = deriveGraph(state, { ...opts, expansions: [sub] });
  expect(off.links.find((l) => l.id === "a<->e")!.label).toBe("uncategorized ×3 +1");
  const on = deriveGraph(state, { ...opts, expansions: [sub], hideUncategorized: true });
  expect(on.links.find((l) => l.id === "a<->e")!.label).toBe("located in");
  expect(on.links.some((l) => l.id === "a<->f")).toBe(false);
});

test("the big answers' views carry no uncategorized label while hidden", () => {
  for (const events of Object.values(BIG)) {
    expect(whole(events, false).links.some((l) => /uncategorized/.test(l.label))).toBe(true);
    expect(whole(events, true).links.some((l) => /uncategorized/.test(l.label))).toBe(false);
  }
});

test("a section's part stands out: its seeds and links as they were, the rest dimmed and faint", () => {
  const all = focusGraph(whole(ogier as AskEvent[]));
  const state = (ogier as AskEvent[]).reduce(askReducer, initialState("q"));
  expect(highlightPart(all, null)).toBe(all);
  const seeds = state.seeds[1].map((s) => s.id);
  const h = highlightPart(all, 1, seeds);
  const inPart = new Set(seeds);
  for (const l of all.links) if (l.parts?.includes(1)) { inPart.add(l.source); inPart.add(l.target); }
  for (const n of h.nodes) expect(n.state).toBe(inPart.has(n.id) ? "shown" : "dim");
  for (const l of h.links) {
    const before = all.links.find((x) => x.id === l.id)!;
    expect(l.state).toBe(l.parts?.includes(1) ? before.state : "faint");
  }
  expect(h.nodes.map((n) => n.id)).toEqual(all.nodes.map((n) => n.id));    // nothing comes or goes
  expect(highlightPart(all, 42, [])).toBe(all);                            // a part with nothing in the view
});

test("the uncategorized chip and the focused/full choice are remembered", () => {
  localStorage.clear(); sessionStorage.clear();
  expect(loadFilters().uncategorized).toBeUndefined();
  saveFilters({ kinds: new Set(), books: new Set(), uncategorized: true });
  expect(loadFilters().uncategorized).toBe(true);
  saveFilters({ kinds: new Set(), books: new Set() });
  expect(loadFilters().uncategorized).toBeUndefined();
  expect(loadFocus()).toBe("focused");
  saveFocus("full");
  expect(sessionStorage.getItem(FOCUS_KEY)).toBe("full");
  expect(loadFocus()).toBe("full");
  saveFocus("focused");
  expect(loadFocus()).toBe("focused");
});
