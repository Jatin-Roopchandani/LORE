import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { api } from "../api";
import { EXPLORE, POSITION } from "../messages";
import { EntityPanel } from "./EntityPanel";

vi.mock("../api", () => ({
  api: {
    profile: vi.fn(), timeline: vi.fn(), relations: vi.fn(), entity: vi.fn(), passage: vi.fn(), subgraph: vi.fn(), search: vi.fn(), path: vi.fn(),
  },
  isError: (x: unknown) => typeof x === "object" && x !== null && "error" in x,
}));
const m = (f: unknown) => f as ReturnType<typeof vi.fn>;

const profile = { id: "a_zed", name: "Zed Quill", kind: "Character", books: ["ba1"], summary: "A traveller who crosses the marsh.",
  aspects: [{ aspect: "nature", statements: [{ text: "Zed is patient.", quote: "Zed waited by the water for hours", chunk_id: "ba1:1:0", book_id: "ba1", chapter: 1 }] }],
  counts: { beats: 2, relations: 1, chapters: 3 } };
const timeline = { order_note: "Relative story order is unknown between: Alpha One; Alpha Two.", beats: [
  { kind: "action", text: "Zed crosses the marsh.", quote: "Zed crossed the marsh at dawn", chunk_id: "ba1:1:0", book_id: "ba1", book_title: "Alpha One",
    chapter: 1, position_label: "Alpha One, chapter 1", others: [{ id: "a_yan", name: "Yan Vale" }, { id: null, name: "A stranger" }] },
  { kind: "meeting", text: "Zed meets Yan.", quote: "Yan met Zed at the gate", chunk_id: "ba1:2:0", book_id: "ba1", book_title: "Alpha One",
    chapter: 2, position_label: "Alpha One, chapter 2", others: [] },
  { kind: "action", text: "Zed leaves.", quote: "Zed left the island", chunk_id: "ba2:1:0", book_id: "ba2", book_title: "Alpha Two",
    chapter: 1, position_label: "Alpha Two, chapter 1", others: [] }] };
const relations = { order_note: null, relations: [{ other: { id: "a_yan", name: "Yan Vale", kind: "Character" },
  predicates: [{ predicate: "INTERACTS_WITH", count: 2, direction: "out" }],
  evidence: [{ quote: "Yan met Zed at the gate", chunk_id: "ba1:2:0", book_id: "ba1", chapter: 2 }],
  first_chapter: { book_id: "ba1", book_title: "Alpha One", chapter: 1 }, last_chapter: { book_id: "ba2", book_title: "Alpha Two", chapter: 2 } }] };

const props = () => ({ id: "a_zed", world: "1", bookTitles: new Map([["ba1", "Alpha One"], ["ba2", "Alpha Two"]]),
  onClose: vi.fn(), onSelect: vi.fn(), onExpand: vi.fn(), onOpenEdge: vi.fn(), onAsk: vi.fn(), onConnect: vi.fn() });

beforeEach(() => {
  Object.values(api).forEach((f) => m(f).mockReset());
  m(api.profile).mockResolvedValue(profile);
  m(api.timeline).mockResolvedValue(timeline);
  m(api.relations).mockResolvedValue(relations);
  m(api.passage).mockResolvedValue({ chunk_id: "ba1:1:0", book_id: "ba1", book_title: "Alpha One", chapter: 1, position_label: "x",
    text: "At first light Zed waited by the water for hours, then went on." });
});

test("loads the profile in the selected world: summary, aspects, counts", async () => {
  render(<EntityPanel {...props()} />);
  expect(await screen.findByRole("heading", { name: "Zed Quill" })).toBeInTheDocument();
  expect(api.profile).toHaveBeenCalledWith("a_zed", "1");
  expect(screen.getByText("A traveller who crosses the marsh.")).toBeInTheDocument();
  expect(screen.getByText("Nature")).toBeInTheDocument();
  expect(screen.getByText("Zed is patient.")).toBeInTheDocument();
  expect(screen.getByText(EXPLORE.counts(profile.counts))).toBeInTheDocument();
});

test("a quote chip opens the passage from /api/passage with the quote marked", async () => {
  render(<EntityPanel {...props()} />);
  await userEvent.click(await screen.findByRole("button", { name: EXPLORE.chipLabel("Alpha One", 1) }));
  expect(await screen.findByText(/At first light/, { selector: "span" })).toBeInTheDocument();
  expect(api.passage).toHaveBeenCalledWith("ba1:1:0", "1");
  expect(screen.getByText("Zed waited by the water for hours", { selector: "mark" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: EXPLORE.chipLabel("Alpha One", 1) })); // closes again
  expect(screen.queryByText(/At first light/)).toBeNull();
});

test("a passage that can't be loaded says so", async () => {
  m(api.passage).mockResolvedValue({ error: { code: "not_found", message: "no such chunk" } });
  render(<EntityPanel {...props()} />);
  await userEvent.click(await screen.findByRole("button", { name: EXPLORE.chipLabel("Alpha One", 1) }));
  expect(await screen.findByText(EXPLORE.passageFailed)).toBeInTheDocument();
});

test("an entity with no profile says so and shows the raw facts from the graph", async () => {
  m(api.profile).mockResolvedValue({ ...profile, summary: null, aspects: [] });
  m(api.entity).mockResolvedValue({ id: "a_zed", name: "Zed Quill", kind: "Character", aliases: [], books: ["ba1"], edges_in: [], edges_out_total: 1, edges_in_total: 0, truncated: false,
    edges_out: [{ subject: "Zed Quill", subject_id: "a_zed", predicate: "KNOWS", predicate_raw: "knew", object: "Yan Vale", object_id: "a_yan", evidence: "Zed knew Yan well",
      confidence: 1, book_id: "ba1", chapter_seq: 4, chunk_id: "ba1:4:0" }] });
  render(<EntityPanel {...props()} />);
  expect(await screen.findByText(EXPLORE.noProfile)).toBeInTheDocument();
  expect(await screen.findByText(EXPLORE.edgeStatement("Zed Quill", "KNOWS", "Yan Vale"))).toBeInTheDocument();
  expect(api.entity).toHaveBeenCalledWith("a_zed", "1");
});

test("the timeline groups beats by book and chapter, with a kind label and the order note", async () => {
  render(<EntityPanel {...props()} />);
  await userEvent.click(await screen.findByRole("tab", { name: "Timeline" }));
  expect(await screen.findByRole("heading", { name: "Alpha One" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Alpha Two" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Chapter 2" })).toBeInTheDocument();
  expect(screen.getByText("Meeting")).toBeInTheDocument();
  expect(screen.getByText(timeline.order_note)).toBeInTheDocument();
  expect(api.timeline).toHaveBeenCalledWith("a_zed", "1");
});

test("a quote chip in the timeline opens its passage", async () => {
  m(api.passage).mockResolvedValue({ chunk_id: "ba2:1:0", book_id: "ba2", book_title: "Alpha Two", chapter: 1, position_label: "x", text: "Then Zed left the island for good." });
  render(<EntityPanel {...props()} />);
  await userEvent.click(await screen.findByRole("tab", { name: "Timeline" }));
  await userEvent.click(await screen.findByRole("button", { name: EXPLORE.chipLabel("Alpha Two", 1) }));
  expect(await screen.findByText("Zed left the island", { selector: "mark" })).toBeInTheDocument();
  expect(api.passage).toHaveBeenCalledWith("ba2:1:0", "1");
});

test("clicking an entity named in a beat opens it; an unlinked name is plain text", async () => {
  const p = props();
  render(<EntityPanel {...p} />);
  await userEvent.click(await screen.findByRole("tab", { name: "Timeline" }));
  await userEvent.click(await screen.findByRole("button", { name: EXPLORE.openEntity("Yan Vale") }));
  expect(p.onSelect).toHaveBeenCalledWith("a_yan");
  expect(screen.getByText(/A stranger/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: EXPLORE.openEntity("A stranger") })).toBeNull();
});

test("relationships list predicates, chapters and evidence; clicking one opens the edge view", async () => {
  const p = props();
  render(<EntityPanel {...p} />);
  await userEvent.click(await screen.findByRole("tab", { name: "Relationships" }));
  const row = await screen.findByRole("button", { name: EXPLORE.showEdge("Zed Quill", "Yan Vale") });
  expect(within(row).getByText("interacts with ×2 →")).toBeInTheDocument();
  expect(within(row).getByText("Chapter 1 – Chapter 2")).toBeInTheDocument();
  expect(screen.getByText("“Yan met Zed at the gate”")).toBeInTheDocument();
  await userEvent.click(row);
  expect(p.onOpenEdge).toHaveBeenCalledWith("a_zed", "a_yan");
});

test("one-click questions submit the question; Deep dive adds depth deep and says it costs allowance", async () => {
  const p = props();
  render(<EntityPanel {...p} />);
  await userEvent.click(await screen.findByRole("tab", { name: "Ask" }));
  const deep = screen.getByRole("button", { name: "Deep dive on Zed Quill" });
  expect(deep).toHaveAttribute("title", EXPLORE.askCostDeep);
  await userEvent.click(deep);
  expect(p.onAsk).toHaveBeenLastCalledWith("Deep dive on Zed Quill", "deep");
  const change = screen.getByRole("button", { name: "How does Zed Quill change across the story?" });
  expect(change).toHaveAttribute("title", EXPLORE.askCost);
  await userEvent.click(change);
  expect(p.onAsk).toHaveBeenLastCalledWith("How does Zed Quill change across the story?", undefined);
  await userEvent.click(screen.getByRole("button", { name: "What role does Zed Quill play?" }));
  expect(p.onAsk).toHaveBeenLastCalledWith("What role does Zed Quill play?", undefined);
});

test("one-click questions are off while an answer is being written", async () => {
  render(<EntityPanel {...props()} busy />);
  await userEvent.click(await screen.findByRole("tab", { name: "Ask" }));
  expect(screen.getByRole("button", { name: "Deep dive on Zed Quill" })).toBeDisabled();
});

test("Connect to... picks a second entity through search and hands both to the pane", async () => {
  const p = props();
  m(api.search).mockResolvedValue({ results: [{ id: "a_wick", name: "Wick Orrin", kind: "Character", books: ["ba1"], matched_alias: null }] });
  render(<EntityPanel {...p} />);
  await userEvent.click(await screen.findByRole("tab", { name: "Relationships" }));
  await userEvent.click(screen.getByRole("button", { name: EXPLORE.connectLabel }));
  await userEvent.type(screen.getByRole("combobox", { name: EXPLORE.connectPick }), "wick");
  await userEvent.click(await screen.findByRole("option", { name: /Wick Orrin/ }));
  expect(p.onConnect).toHaveBeenCalledWith({ id: "a_zed", name: "Zed Quill" }, expect.objectContaining({ id: "a_wick" }));
});

test("Expand neighbours loads the neighbours; a reply for an entity the panel left is dropped", async () => {
  const p = props();
  let land: (v: unknown) => void = () => {};
  m(api.subgraph).mockImplementation(() => new Promise((r) => { land = r; }));
  const { rerender } = render(<EntityPanel {...p} />);
  await userEvent.click(await screen.findByRole("button", { name: EXPLORE.expand }));
  rerender(<EntityPanel {...p} id="a_yan" />);
  land({ nodes: [], edges: [], truncated: false, depth: 1 });
  await new Promise((r) => setTimeout(r, 20));
  expect(p.onExpand).not.toHaveBeenCalled();
});

test("Escape closes the panel and the tabs are a labelled tablist", async () => {
  const p = props();
  render(<EntityPanel {...p} />);
  expect(await screen.findByRole("tablist", { name: EXPLORE.tabsLabel })).toBeInTheDocument();
  const tab = screen.getByRole("tab", { name: "Profile" });
  tab.focus();
  await userEvent.keyboard("{ArrowRight}");
  expect(screen.getByRole("tab", { name: "Timeline" })).toHaveAttribute("aria-selected", "true");
  await userEvent.keyboard("{Escape}");
  expect(p.onClose).toHaveBeenCalled();
  await waitFor(() => expect(api.timeline).toHaveBeenCalled());
});

test("moving the reading position refetches the profile and the open tab", async () => {
  const p = props();
  const { rerender } = render(<EntityPanel {...p} positionVersion={0} />);
  await screen.findByText("Zed Quill");
  await userEvent.click(screen.getByRole("tab", { name: EXPLORE.tabs.timeline }));
  await waitFor(() => expect(m(api.timeline)).toHaveBeenCalledTimes(1));
  expect(m(api.profile)).toHaveBeenCalledTimes(1);
  rerender(<EntityPanel {...p} positionVersion={1} />);
  await waitFor(() => expect(m(api.profile)).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(m(api.timeline).mock.calls.length).toBeGreaterThanOrEqual(2));
});

test("under a reading position the whole-story summary is replaced by a hint and the statements read so far", async () => {
  m(api.profile).mockResolvedValue({ ...profile, summary: null, summary_hidden: true });
  render(<EntityPanel {...props()} />);
  expect(await screen.findByText(POSITION.summaryHidden)).toBeInTheDocument();
  expect(screen.queryByText(profile.summary)).toBeNull();
  expect(screen.getByText(/Zed is patient\./)).toBeInTheDocument();
});
