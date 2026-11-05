import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "./App";
import { HISTORY, QUICK, UI } from "./messages";

const overview = vi.fn((_w?: string) => Promise.resolve({ nodes: [{ id: "h", name: "Holmes", kind: "Character", books: [] }], edges: [], truncated: false, depth: 0 }));
vi.mock("./components/GraphCanvas", () => ({ GraphCanvas: () => <div data-testid="graph" /> }));
const worlds = [{ id: 1, name: "Alpha Saga", books: [] }, { id: 2, name: "Beta Realm", books: [] }];
vi.mock("./api", () => ({
  api: {
    settings: () => Promise.resolve({ show_internals: true }), library: () => Promise.resolve({ books: [], note: null }), worlds: () => Promise.resolve(worlds),
    quota: () => Promise.resolve({ provider: "gemini", models: [], resets_at: "2026-10-08T07:00:00Z" }), mcp: () => Promise.resolve({ error: { code: "x", message: "x" } }),
    entities: () => Promise.resolve({ entities: [] }), overview: (w?: string) => overview(w),
    readingPosition: () => Promise.resolve({ error: { code: "x", message: "x" } }),
  },
  isError: (x: unknown) => typeof x === "object" && x !== null && "error" in x,
  streamAsk: async function* () { yield { type: "error", t: 0, error: { code: "interrupted", message: "" } }; },
}));
// Reduced motion is on (replays are instant); the narrow-screen query follows `narrow`.
Object.defineProperty(window, "matchMedia", { value: (q: string) => ({ matches: q.includes("max-width") ? false : true, addEventListener() {}, removeEventListener() {} }) });

const answer = { type: "answer", t: 1, result: { answer: "", mode: "graph", template: null, note: null, model: null, selection_model: null,
  sub_questions: [], claims: [], passages: [], edges: [], seeds: [] } };
const item = (id: string, question: string, world: string) => ({ id, question, askedAt: new Date().toISOString(), world,
  events: [{ type: "parts", t: 0, sub_questions: [question] }, answer] });

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("lore-history-v1", JSON.stringify([item("h1", "Who is Yan?", "2"), item("h2", "Where is the marsh?", "1")]));
  localStorage.setItem("lore-world-v1", "1");
});

const openHistory = async () => { await userEvent.click(await screen.findByRole("button", { name: HISTORY.open })); await screen.findByRole("dialog", { name: HISTORY.title }); };

test("history is closed at first and opens as an overlay drawer; the layout never changes", async () => {
  render(<App />);
  await screen.findByPlaceholderText(UI.askPlaceholder);
  expect(screen.queryByRole("dialog", { name: HISTORY.title })).toBeNull();
  const app = document.querySelector(".app")!, panes = document.querySelector(".panes")!;
  const before = [app.className, panes.className];
  await openHistory();
  expect([app.className, panes.className]).toEqual(before);
  expect(panes.className).toBe("panes");
  expect(document.querySelector(".hist-backdrop")!.parentElement).toBe(panes);
  expect(screen.getByRole("button", { name: HISTORY.open })).toHaveAttribute("aria-expanded", "true");
  expect(localStorage.getItem("lore-history-open-v1")).toBeNull();
});

test("Escape closes the drawer and focus goes back to the History button", async () => {
  render(<App />);
  await screen.findByPlaceholderText(UI.askPlaceholder);
  const button = screen.getByRole("button", { name: HISTORY.open });
  button.focus();
  await userEvent.click(button);
  const drawer = await screen.findByRole("dialog", { name: HISTORY.title });
  expect(within(drawer).getByRole("button", { name: "Who is Yan?" })).toBeInTheDocument();
  await userEvent.keyboard("{Escape}");
  expect(screen.queryByRole("dialog", { name: HISTORY.title })).toBeNull();
  expect(button).toHaveFocus();
});

test("a click outside closes the drawer; picking an entry closes it too", async () => {
  render(<App />);
  await openHistory();
  await userEvent.click(document.querySelector(".hist-backdrop")!);
  expect(screen.queryByRole("dialog", { name: HISTORY.title })).toBeNull();
  await openHistory();
  await userEvent.click(screen.getByRole("button", { name: "Where is the marsh?" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: HISTORY.title })).toBeNull());
});

test("the header button that manages books reads Books", async () => {
  render(<App />);
  await screen.findByPlaceholderText(UI.askPlaceholder);
  expect(screen.getByRole("button", { name: /^Books$/ })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /^Library$/ })).toBeNull();
});

const quick = () => screen.findByRole("dialog", { name: QUICK.title });

test("Ctrl+K opens the quick switcher, filters, moves with arrows and Enter replays (switching world)", async () => {
  render(<App />);
  expect(await screen.findByRole("combobox", { name: "World" })).toHaveValue("1");
  await userEvent.keyboard("{Control>}k{/Control}");
  const box = await quick();
  const input = within(box).getByRole("combobox", { name: QUICK.search });
  expect(input).toHaveFocus();
  expect(within(box).getAllByRole("option")).toHaveLength(2);
  await userEvent.type(input, "who");
  const opts = within(box).getAllByRole("option");
  expect(opts.map((o) => o.textContent)).toEqual([QUICK.ask("who"), "Who is Yan?"]);
  expect(input).toHaveAttribute("aria-activedescendant", opts[0].id);
  await userEvent.keyboard("{ArrowDown}");
  expect(input).toHaveAttribute("aria-activedescendant", opts[1].id);
  expect(opts[1]).toHaveAttribute("aria-selected", "true");
  await userEvent.keyboard("{Enter}");
  await waitFor(() => expect(screen.queryByRole("dialog", { name: QUICK.title })).toBeNull());
  await waitFor(() => expect(screen.getByRole("combobox", { name: "World" })).toHaveValue("2"));
});

test("Escape closes the quick switcher", async () => {
  render(<App />);
  await screen.findByPlaceholderText(UI.askPlaceholder);
  await userEvent.keyboard("{Control>}k{/Control}");
  await quick();
  await userEvent.keyboard("{Escape}");
  expect(screen.queryByRole("dialog", { name: QUICK.title })).toBeNull();
});

test("Cmd+K works too", async () => {
  render(<App />);
  await screen.findByPlaceholderText(UI.askPlaceholder);
  await userEvent.keyboard("{Meta>}k{/Meta}");
  expect(await quick()).toBeInTheDocument();
});

test("clicking an entry replays it and switches the world when it was asked elsewhere", async () => {
  render(<App />);
  await openHistory();
  expect(await screen.findByRole("combobox", { name: "World" })).toHaveValue("1");
  await userEvent.click(screen.getByRole("button", { name: "Who is Yan?" }));
  await waitFor(() => expect(screen.getByRole("combobox", { name: "World" })).toHaveValue("2"));
  await openHistory();
  expect(screen.getByRole("button", { name: "Who is Yan?" })).toHaveAttribute("aria-current", "true");
  expect(localStorage.getItem("lore-world-v1")).toBe("2");
});

test("New question clears the answer, returns the graph to the library and focuses the ask box", async () => {
  render(<App />);
  await openHistory();
  await userEvent.click(await screen.findByRole("button", { name: "Where is the marsh?" }));
  expect(await screen.findByRole("group", { name: "Graph view" })).toBeInTheDocument();
  await openHistory(); // an answer: the This answer | Library toggle
  expect(screen.queryByText(UI.emptyIntro)).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: HISTORY.newQuestion }));
  expect(await screen.findByText(UI.emptyIntro)).toBeInTheDocument();
  expect(screen.queryByRole("group", { name: "Graph view" })).toBeNull(); // nothing asked: the library alone
  expect(screen.getByText(UI.overviewStage)).toBeInTheDocument();
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Question" })).toHaveFocus());
});

test("deleting an entry removes it from the list and from storage; Clear history asks first", async () => {
  render(<App />);
  await openHistory();
  await screen.findByRole("button", { name: "Who is Yan?" });
  await userEvent.click(screen.getByRole("button", { name: HISTORY.delete("Who is Yan?") }));
  expect(screen.queryByRole("button", { name: "Who is Yan?" })).toBeNull();
  await waitFor(() => expect(JSON.parse(localStorage.getItem("lore-history-v1")!).map((h: { id: string }) => h.id)).toEqual(["h2"]));
  await userEvent.click(screen.getByRole("button", { name: HISTORY.clear }));
  expect(screen.getByRole("button", { name: "Where is the marsh?" })).toBeInTheDocument(); // not cleared yet
  await userEvent.click(screen.getByRole("button", { name: HISTORY.clearYes }));
  expect(screen.queryByRole("button", { name: "Where is the marsh?" })).toBeNull();
  expect(screen.getByText(HISTORY.empty)).toBeInTheDocument();
  await waitFor(() => expect(JSON.parse(localStorage.getItem("lore-history-v1")!)).toEqual([]));
});
