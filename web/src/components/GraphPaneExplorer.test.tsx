import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { api } from "../api";
import { askReducer, initialState } from "../askReducer";
import { FILTERS_KEY } from "../filters";
import type { GraphView } from "../graphView";
import { EXPLORE, UI } from "../messages";
import type { AskEvent, Book, Edge, SubgraphData } from "../types";
import { GraphPane } from "./GraphPane";

interface CanvasProps { view: GraphView; onNodeClick: (id: string) => void; onEdgeClick?: (a: string, b: string) => void; focusId?: string | null; focusRequest?: number }
const canvas: CanvasProps[] = [];
const last = () => canvas.at(-1)!;
const ids = () => last().view.nodes.map((n) => n.id).sort();
vi.mock("./GraphCanvas", () => ({ GraphCanvas: (p: CanvasProps) => { canvas.push(p); return <div data-testid="graph" />; } }));
vi.mock("../api", () => ({
  api: { entities: vi.fn(), profile: vi.fn(), timeline: vi.fn(), relations: vi.fn(), entity: vi.fn(), passage: vi.fn(), subgraph: vi.fn(), search: vi.fn(), path: vi.fn(), edge: vi.fn() },
  isError: (x: unknown) => typeof x === "object" && x !== null && "error" in x,
}));
const m = (f: unknown) => f as ReturnType<typeof vi.fn>;

const node = (id: string, name: string, kind: string, books: string[]) => ({ id, name, kind, books });
const overview: SubgraphData = { truncated: false, depth: 0,
  nodes: [node("h", "Hope", "Character", ["b1"]), node("w", "Wren", "Character", ["b2"]), node("p", "Pike Marsh", "Place", ["b1"])],
  edges: [{ subject: "Hope", subject_id: "h", object: "Wren", object_id: "w", count: 1, predicates: { KNOWS: 1 } },
    { subject: "Hope", subject_id: "h", object: "Pike Marsh", object_id: "p", count: 1, predicates: { LIVES_IN: 1 } }] };
const edge = (s: string, o: string, chunk: string): Edge => ({ subject: s, predicate: "KNOWS", predicate_raw: "", object: o, evidence: "", confidence: 1,
  book_id: "b1", chapter_seq: 1, chunk_id: chunk, subject_id: s, object_id: o });
const events: AskEvent[] = [
  { type: "parts", t: 0, sub_questions: ["a"] },
  { type: "seeds", t: 0, part: 0, entities: [{ id: "q", name: "Quill", kind: "Character", books: ["b1"], score: 0 }] },
  { type: "edges", t: 0, part: 0, template: "neighbours", total: 1, edges: [edge("q", "r", "c1")] },
];
const answered = events.reduce(askReducer, initialState("who is Quill?"));
const books: Book[] = [{ id: "b1", title: "Alpha One", author: null, status: "graphed", chunk_count: 1, entity_count: 1 },
  { id: "b2", title: "Alpha Two", author: null, status: "graphed", chunk_count: 1, entity_count: 1 }];
const base = { hiddenBooks: new Set<string>(), hoverChunks: [] as string[], noBooks: false, books, overview, world: "1" };

beforeEach(() => {
  canvas.length = 0; localStorage.clear();
  Object.values(api).forEach((f) => m(f).mockReset());
  m(api.entities).mockResolvedValue({ entities: [{ id: "r", name: "Rook", kind: "Place", books: ["b1"] }] });
  m(api.profile).mockImplementation((id: string) => Promise.resolve({ id, name: id === "h" ? "Hope" : id === "w" ? "Wren" : id, kind: "Character", books: ["b1"], summary: "S.", aspects: [], counts: { beats: 0, relations: 0, chapters: 0 } }));
  m(api.timeline).mockResolvedValue({ beats: [], order_note: null });
  m(api.relations).mockResolvedValue({ relations: [], order_note: null });
  m(api.search).mockResolvedValue({ results: [] });
});

const seg = () => screen.getByRole("group", { name: EXPLORE.modeLabel });

test("before any question only the Library is shown, with no toggle", () => {
  render(<GraphPane state={initialState()} {...base} />);
  expect(screen.queryByRole("group", { name: EXPLORE.modeLabel })).toBeNull();
  expect(ids()).toEqual(["h", "p", "w"]);
  expect(screen.getByText(UI.overviewStage)).toBeInTheDocument();
});

test("an answer opens on its graph; Library shows the overview and This answer restores the answer graph", async () => {
  render(<GraphPane state={answered} {...base} />);
  expect(ids()).toEqual(["q", "r"]);
  expect(within(seg()).getByRole("button", { name: EXPLORE.modeAnswer })).toHaveAttribute("aria-pressed", "true");
  await userEvent.click(within(seg()).getByRole("button", { name: EXPLORE.modeLibrary }));
  expect(ids()).toEqual(["h", "p", "w"]);
  expect(within(seg()).getByRole("button", { name: EXPLORE.modeLibrary })).toHaveAttribute("aria-pressed", "true");
  await userEvent.click(within(seg()).getByRole("button", { name: EXPLORE.modeAnswer }));
  expect(ids()).toEqual(["q", "r"]);
});

test("switching the world selects the Library", async () => {
  const { rerender } = render(<GraphPane state={answered} {...base} />);
  expect(ids()).toEqual(["q", "r"]);
  rerender(<GraphPane state={answered} {...base} world="2" />);
  await waitFor(() => expect(ids()).toEqual(["h", "p", "w"]));
  expect(within(seg()).getByRole("button", { name: EXPLORE.modeLibrary })).toHaveAttribute("aria-pressed", "true");
});

test("clicking a node opens its panel (profile fetched in the world) without moving the camera", async () => {
  render(<GraphPane state={initialState()} {...base} />);
  act(() => last().onNodeClick("h"));
  expect(await screen.findByRole("heading", { name: "Hope" })).toBeInTheDocument();
  expect(api.profile).toHaveBeenCalledWith("h", "1");
  expect(last().view.nodes.find((n) => n.id === "h")!.selected).toBe(true);
  expect(last().focusRequest ?? 0).toBe(0);
});

test("search: picking a node already in the graph opens its panel and centres it", async () => {
  m(api.search).mockResolvedValue({ results: [{ id: "w", name: "Wren", kind: "Character", books: ["b2"], matched_alias: null }] });
  render(<GraphPane state={initialState()} {...base} />);
  await userEvent.type(screen.getByRole("combobox", { name: EXPLORE.searchLabel }), "wre");
  await userEvent.click(await screen.findByRole("option", { name: /Wren/ }));
  expect(await screen.findByRole("heading", { name: "Wren" })).toBeInTheDocument();
  expect(api.search).toHaveBeenCalledWith("wre", "1");
  expect(last().focusId).toBe("w");
  expect(last().focusRequest).toBe(1);
  expect(api.subgraph).not.toHaveBeenCalled();
});

test("search: an entity that isn't in the graph is added with its neighbours, then centred", async () => {
  m(api.search).mockResolvedValue({ results: [{ id: "z", name: "Zed", kind: "Character", books: ["b1"], matched_alias: null }] });
  m(api.subgraph).mockResolvedValue({ truncated: false, depth: 1, nodes: [node("z", "Zed", "Character", ["b1"]), node("h", "Hope", "Character", ["b1"])],
    edges: [{ subject: "Zed", subject_id: "z", object: "Hope", object_id: "h", count: 1, predicates: { KNOWS: 1 } }] });
  render(<GraphPane state={initialState()} {...base} />);
  await userEvent.type(screen.getByRole("combobox", { name: EXPLORE.searchLabel }), "ze");
  await userEvent.click(await screen.findByRole("option", { name: /Zed/ }));
  await waitFor(() => expect(ids()).toEqual(["h", "p", "w", "z"]));
  expect(api.subgraph).toHaveBeenCalledWith("z", "1");
  await waitFor(() => expect(last().focusId).toBe("z"));
  expect(last().focusRequest).toBe(1);
});

test("clicking an edge opens the edge view and highlights that link", async () => {
  m(api.edge).mockResolvedValue({ a: { id: "h", name: "Hope", kind: "Character" }, b: { id: "w", name: "Wren", kind: "Character" }, order_note: null,
    predicates: [{ predicate: "KNOWS", count: 1, direction: "out" }],
    relationships: [{ subject: { id: "h", name: "Hope", kind: "Character" }, object: { id: "w", name: "Wren", kind: "Character" }, predicate: "KNOWS", predicate_raw: "knew",
      quote: "Hope knew Wren", chunk_id: "b1:1:0", book_id: "b1", book_title: "Alpha One", chapter: 1, position_label: "Alpha One, chapter 1" }] });
  render(<GraphPane state={initialState()} {...base} />);
  act(() => last().onEdgeClick!("h", "w"));
  expect(await screen.findByText("Hope knows Wren")).toBeInTheDocument();
  expect(api.edge).toHaveBeenCalledWith("h", "w", "1");
  expect(last().view.links.find((l) => l.picked)!.id).toBe("h<->w");
});

test("a relationship opened from the panel shows its edge view", async () => {
  m(api.relations).mockResolvedValue({ order_note: null, relations: [{ other: { id: "w", name: "Wren", kind: "Character" }, predicates: [{ predicate: "KNOWS", count: 1, direction: "out" }],
    evidence: [], first_chapter: null, last_chapter: null }] });
  m(api.edge).mockResolvedValue({ a: { id: "h", name: "Hope", kind: "Character" }, b: { id: "w", name: "Wren", kind: "Character" }, order_note: null, predicates: [], relationships: [] });
  render(<GraphPane state={initialState()} {...base} />);
  act(() => last().onNodeClick("h"));
  await userEvent.click(await screen.findByRole("tab", { name: "Relationships" }));
  await userEvent.click(await screen.findByRole("button", { name: EXPLORE.showEdge("Hope", "Wren") }));
  expect(await screen.findByRole("complementary", { name: EXPLORE.edgeLabel })).toBeInTheDocument();
  expect(last().view.links.find((l) => l.picked)!.id).toBe("h<->w");
});

async function connectHopeTo(name: string) {
  m(api.search).mockResolvedValue({ results: [{ id: "x", name, kind: "Character", books: ["b1"], matched_alias: null }] });
  act(() => last().onNodeClick("h"));
  await userEvent.click(await screen.findByRole("tab", { name: "Relationships" }));
  await userEvent.click(screen.getByRole("button", { name: EXPLORE.connectLabel }));
  await userEvent.type(screen.getByRole("combobox", { name: EXPLORE.connectPick }), "x");
  await userEvent.click(await screen.findByRole("option", { name: new RegExp(name) }));
}

test("Connect highlights the path in the graph and lists each hop with its evidence; Clear path removes it", async () => {
  m(api.path).mockResolvedValue({ found: true, timed_out: false,
    nodes: [{ id: "h", name: "Hope", kind: "Character" }, { id: "m", name: "Mira", kind: "Character" }, { id: "x", name: "Xan", kind: "Character" }],
    hops: [{ from: "h", to: "m", predicates: [{ predicate: "KNOWS", count: 1, direction: "out" }], evidence: [{ quote: "Hope knew Mira well", chunk_id: "b1:1:0", book_id: "b1", chapter: 1 }] },
      { from: "m", to: "x", predicates: [{ predicate: "LEADS_TO", count: 2, direction: "in" }], evidence: [{ quote: "Xan led Mira away", chunk_id: "b1:2:0", book_id: "b1", chapter: 2 }] }] });
  render(<GraphPane state={initialState()} {...base} />);
  await connectHopeTo("Xan");
  const conn = await screen.findByRole("region", { name: EXPLORE.pathLabel });
  expect(api.path).toHaveBeenCalledWith("h", "x", "1");
  expect(within(conn).getByText(EXPLORE.pathHops(2))).toBeInTheDocument();
  expect(within(conn).getByText(EXPLORE.hop(1))).toBeInTheDocument();
  expect(within(conn).getByText("“Hope knew Mira well”")).toBeInTheDocument();
  expect(within(conn).getByText("“Xan led Mira away”")).toBeInTheDocument();
  expect(within(conn).getByText("← leads to ×2")).toBeInTheDocument();
  expect(ids()).toEqual(["h", "m", "p", "w", "x"]); // the path's nodes join the graph
  expect(last().view.nodes.filter((n) => n.onPath).map((n) => n.id).sort()).toEqual(["h", "m", "x"]);
  expect(last().view.links.filter((l) => l.onPath).map((l) => l.id).sort()).toEqual(["h<->m", "m<->x"]);
  await userEvent.click(within(conn).getByRole("button", { name: EXPLORE.clearPath }));
  expect(screen.queryByRole("region", { name: EXPLORE.pathLabel })).toBeNull();
  expect(last().view.nodes.some((n) => n.onPath)).toBe(false);
  expect(ids()).toEqual(["h", "p", "w"]);
});

test("Connect with no path says so and highlights nothing", async () => {
  m(api.path).mockResolvedValue({ found: false, timed_out: false, nodes: [], hops: [] });
  render(<GraphPane state={initialState()} {...base} />);
  await connectHopeTo("Xan");
  expect(await screen.findByText(EXPLORE.pathNone("Hope", "Xan"))).toBeInTheDocument();
  expect(last().view.nodes.some((n) => n.onPath)).toBe(false);
  expect(ids()).toEqual(["h", "p", "w"]);
});

test("Connect that timed out says to try a closer pair", async () => {
  m(api.path).mockResolvedValue({ found: false, timed_out: true, nodes: [], hops: [] });
  render(<GraphPane state={initialState()} {...base} />);
  await connectHopeTo("Xan");
  expect(await screen.findByText(EXPLORE.pathTimedOut)).toBeInTheDocument();
});

test("kind filters hide nodes and their edges, and persist across a remount", async () => {
  const { unmount } = render(<GraphPane state={initialState()} {...base} />);
  expect(last().view.links).toHaveLength(2);
  await userEvent.click(screen.getByRole("button", { name: EXPLORE.filters }));
  await userEvent.click(within(screen.getByRole("group", { name: EXPLORE.kindsLabel })).getByRole("button", { name: "Place" }));
  expect(ids()).toEqual(["h", "w"]);
  expect(last().view.links.map((l) => l.id)).toEqual(["h<->w"]);
  expect(JSON.parse(localStorage.getItem(FILTERS_KEY)!).kinds).toEqual(["Place"]);
  unmount();
  render(<GraphPane state={initialState()} {...base} />);
  expect(ids()).toEqual(["h", "w"]);
  await userEvent.click(screen.getByRole("button", { name: EXPLORE.filtersActive(1) }));
  expect(within(screen.getByRole("group", { name: EXPLORE.kindsLabel })).getByRole("button", { name: "Place" })).toHaveAttribute("aria-pressed", "false");
  await userEvent.click(screen.getByRole("button", { name: EXPLORE.resetFilters }));
  expect(ids()).toEqual(["h", "p", "w"]);
});

test("book filters hide the nodes only in those books", async () => {
  render(<GraphPane state={initialState()} {...base} />);
  await userEvent.click(screen.getByRole("button", { name: EXPLORE.filters }));
  await userEvent.click(within(screen.getByRole("group", { name: EXPLORE.booksLabel })).getByRole("button", { name: "Alpha Two" }));
  expect(ids()).toEqual(["h", "p"]);
  expect(last().view.links.map((l) => l.id)).toEqual(["h<->p"]);
  expect(JSON.parse(localStorage.getItem(FILTERS_KEY)!).books).toEqual(["b2"]);
});

test("a one-click question from the panel goes through onAsk, with depth for Deep dive", async () => {
  const onAsk = vi.fn();
  render(<GraphPane state={initialState()} {...base} onAsk={onAsk} />);
  act(() => last().onNodeClick("h"));
  await userEvent.click(await screen.findByRole("tab", { name: "Ask" }));
  await userEvent.click(screen.getByRole("button", { name: "Deep dive on Hope" }));
  expect(onAsk).toHaveBeenCalledWith("Deep dive on Hope", "deep");
});
