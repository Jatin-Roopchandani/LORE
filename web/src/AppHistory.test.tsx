import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "./App";
import { HISTORY_OPEN_KEY } from "./historyPanel";
import { HISTORY, UI } from "./messages";

const narrow = { value: false };
const overview = vi.fn((_w?: string) => Promise.resolve({ nodes: [{ id: "h", name: "Holmes", kind: "Character", books: [] }], edges: [], truncated: false, depth: 0 }));
vi.mock("./components/GraphCanvas", () => ({ GraphCanvas: () => <div data-testid="graph" /> }));
const worlds = [{ id: 1, name: "Alpha Saga", books: [] }, { id: 2, name: "Beta Realm", books: [] }];
vi.mock("./api", () => ({
  api: {
    settings: () => Promise.resolve({ show_internals: true }), library: () => Promise.resolve({ books: [], note: null }), worlds: () => Promise.resolve(worlds),
    quota: () => Promise.resolve({ provider: "gemini", models: [], resets_at: "2026-10-08T07:00:00Z" }), mcp: () => Promise.resolve({ error: { code: "x", message: "x" } }),
    entities: () => Promise.resolve({ entities: [] }), overview: (w?: string) => overview(w),
  },
  isError: (x: unknown) => typeof x === "object" && x !== null && "error" in x,
  streamAsk: async function* () { yield { type: "error", t: 0, error: { code: "interrupted", message: "" } }; },
}));
// Reduced motion is on (replays are instant); the narrow-screen query follows `narrow`.
Object.defineProperty(window, "matchMedia", { value: (q: string) => ({ matches: q.includes("max-width") ? narrow.value : true, addEventListener() {}, removeEventListener() {} }) });

const answer = { type: "answer", t: 1, result: { answer: "", mode: "graph", template: null, note: null, model: null, selection_model: null,
  sub_questions: [], claims: [], passages: [], edges: [], seeds: [] } };
const item = (id: string, question: string, world: string) => ({ id, question, askedAt: new Date().toISOString(), world,
  events: [{ type: "parts", t: 0, sub_questions: [question] }, answer] });

beforeEach(() => {
  localStorage.clear(); narrow.value = false;
  localStorage.setItem("lore-history-v1", JSON.stringify([item("h1", "Who is Yan?", "2"), item("h2", "Where is the marsh?", "1")]));
  localStorage.setItem("lore-world-v1", "1");
});

test("on a wide screen history is a column; the History button hides it and the choice is remembered", async () => {
  const { unmount } = render(<App />);
  expect(await screen.findByRole("complementary", { name: HISTORY.title })).toBeInTheDocument();
  const button = screen.getByRole("button", { name: HISTORY.open });
  expect(button).toHaveAttribute("aria-expanded", "true");
  await userEvent.click(button);
  expect(screen.queryByRole("complementary", { name: HISTORY.title })).toBeNull();
  expect(localStorage.getItem(HISTORY_OPEN_KEY)).toBe("0");
  unmount();
  render(<App />);
  await screen.findByPlaceholderText(UI.askPlaceholder);
  expect(screen.queryByRole("complementary", { name: HISTORY.title })).toBeNull();
  expect(screen.getByRole("button", { name: HISTORY.open })).toHaveAttribute("aria-expanded", "false");
  await userEvent.click(screen.getByRole("button", { name: HISTORY.open }));
  expect(await screen.findByRole("complementary", { name: HISTORY.title })).toBeInTheDocument();
  expect(localStorage.getItem(HISTORY_OPEN_KEY)).toBe("1");
});

test("clicking an entry replays it and switches the world when it was asked elsewhere", async () => {
  render(<App />);
  expect(await screen.findByRole("combobox", { name: "World" })).toHaveValue("1");
  await userEvent.click(screen.getByRole("button", { name: "Who is Yan?" }));
  await waitFor(() => expect(screen.getByRole("combobox", { name: "World" })).toHaveValue("2"));
  expect(screen.getByRole("button", { name: "Who is Yan?" })).toHaveAttribute("aria-current", "true");
  expect(localStorage.getItem("lore-world-v1")).toBe("2");
});

test("New question clears the answer, returns the graph to the library and focuses the ask box", async () => {
  render(<App />);
  await userEvent.click(await screen.findByRole("button", { name: "Where is the marsh?" }));
  expect(await screen.findByRole("group", { name: "Graph view" })).toBeInTheDocument(); // an answer: the This answer | Library toggle
  expect(screen.queryByText(UI.emptyIntro)).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: HISTORY.newQuestion }));
  expect(await screen.findByText(UI.emptyIntro)).toBeInTheDocument();
  expect(screen.queryByRole("group", { name: "Graph view" })).toBeNull(); // nothing asked: the library alone
  expect(screen.getByText(UI.overviewStage)).toBeInTheDocument();
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Question" })).toHaveFocus());
});

test("deleting an entry removes it from the list and from storage; Clear history asks first", async () => {
  render(<App />);
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

test("on a narrow screen history is a drawer opened by the button, closed by Escape, and focus goes back", async () => {
  narrow.value = true;
  render(<App />);
  await screen.findByPlaceholderText(UI.askPlaceholder);
  expect(screen.queryByRole("dialog", { name: HISTORY.title })).toBeNull();
  const button = screen.getByRole("button", { name: HISTORY.open });
  button.focus();
  await userEvent.click(button);
  const drawer = await screen.findByRole("dialog", { name: HISTORY.title });
  expect(within(drawer).getByRole("button", { name: "Who is Yan?" })).toBeInTheDocument();
  await userEvent.keyboard("{Escape}");
  expect(screen.queryByRole("dialog", { name: HISTORY.title })).toBeNull();
  expect(button).toHaveFocus();
  // The drawer never changes what the wide layout remembers.
  expect(localStorage.getItem(HISTORY_OPEN_KEY)).toBeNull();
  // Replaying from the drawer closes it.
  await userEvent.click(button);
  await userEvent.click(await screen.findByRole("button", { name: "Where is the marsh?" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: HISTORY.title })).toBeNull());
});
