import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "./App";

const quota = vi.fn(() => Promise.resolve({ provider: "gemini", models: [], resets_at: "2026-10-08T07:00:00Z" }));
const overview = vi.fn((_world?: string) => Promise.resolve({ nodes: [{ id: "h", name: "Holmes", kind: "Character", books: [] }], edges: [], truncated: false, depth: 0 }));
vi.mock("./components/GraphCanvas", () => ({ GraphCanvas: () => <div data-testid="graph" /> }));
const library = vi.fn((_world?: string) => Promise.resolve({ books: [], note: null }));
const worlds = [{ id: 1, name: "Alpha Saga", books: [] }, { id: 2, name: "Beta Realm", books: [] }];
const asked: (string | undefined)[] = [];
vi.mock("./api", () => ({
  api: {
    settings: () => Promise.resolve({ show_internals: true }),
    library: (w?: string) => library(w),
    worlds: () => Promise.resolve(worlds),
    quota: () => quota(),
    mcp: () => Promise.resolve({ error: { code: "x", message: "x" } }),
    entities: () => Promise.resolve({ entities: [] }),
    overview: (w?: string) => overview(w),
  },
  isError: (x: unknown) => typeof x === "object" && x !== null && "error" in x,
  // The stream drops before any answer.
  streamAsk: async function* (_q: string, _signal: AbortSignal, world?: string) {
    asked.push(world);
    yield { type: "error", t: 0, error: { code: "interrupted", message: "" } };
  },
}));
Object.defineProperty(window, "matchMedia", { value: () => ({ matches: true, addEventListener() {}, removeEventListener() {} }) });

beforeEach(() => { localStorage.clear(); asked.length = 0; });

test("renders the top bar, the ask box and the graph pane", async () => {
  render(<App />);
  expect(screen.getByText("LORE")).toBeInTheDocument();
  expect(await screen.findByPlaceholderText("Ask about your books…")).toBeInTheDocument();
  expect(screen.getByTestId("graph")).toBeInTheDocument();
});

test("an interrupted stream refreshes the quota", async () => {
  render(<App />);
  await waitFor(() => expect(quota).toHaveBeenCalled());
  const before = quota.mock.calls.length;
  await userEvent.type(await screen.findByPlaceholderText("Ask about your books…"), "who?{Enter}");
  expect(await screen.findByText(/connection dropped/)).toBeInTheDocument();
  await waitFor(() => expect(quota.mock.calls.length).toBeGreaterThanOrEqual(before + 2));
});

test("the overview is fetched once and offers example questions in the empty chat", async () => {
  overview.mockClear();
  render(<App />);
  expect(await screen.findByRole("button", { name: "Tell me about Holmes" })).toBeInTheDocument();
  expect(screen.getByText("Library overview · your most-connected characters, places and groups")).toBeInTheDocument();
  expect(overview).toHaveBeenCalledTimes(1);
});

test("the world selector is remembered, refetches the graph and the books, and asks go to that world", async () => {
  overview.mockClear(); library.mockClear();
  const { unmount } = render(<App />);
  const select = await screen.findByRole("combobox", { name: "World" });
  await waitFor(() => expect(overview).toHaveBeenCalledWith("all"));
  await userEvent.selectOptions(select, "Beta Realm");
  await waitFor(() => expect(overview).toHaveBeenLastCalledWith("2"));
  expect(library).toHaveBeenLastCalledWith("2");
  expect(localStorage.getItem("lore-world-v1")).toBe("2");
  await userEvent.type(await screen.findByPlaceholderText("Ask about your books…"), "who?{Enter}");
  await waitFor(() => expect(asked).toEqual(["2"]));
  unmount();
  // A new visit starts in the remembered world, without first fetching the others.
  overview.mockClear();
  render(<App />);
  expect(await screen.findByRole("combobox", { name: "World" })).toHaveValue("2");
  await waitFor(() => expect(overview).toHaveBeenCalled());
  expect(overview.mock.calls.map((c) => c[0])).toEqual(["2"]);
});

test("a remembered world that no longer exists falls back to all worlds", async () => {
  localStorage.setItem("lore-world-v1", "9");
  overview.mockClear();
  render(<App />);
  await waitFor(() => expect(screen.getByRole("combobox", { name: "World" })).toHaveValue("all"));
  await waitFor(() => expect(overview).toHaveBeenLastCalledWith("all"));
});

test("replaying a question asked in another world switches the selector to it", async () => {
  localStorage.setItem("lore-world-v1", "1");
  const item = { id: "h1", question: "Who is Yan?", askedAt: "2026-10-29T00:00:00Z", world: "2", events: [
    { type: "parts", t: 0, sub_questions: ["Who is Yan?"] },
    { type: "answer", t: 1, result: { answer: "", mode: "graph", template: null, note: null, model: null, selection_model: null,
      sub_questions: [], claims: [], passages: [], edges: [], seeds: [] } }] };
  localStorage.setItem("lore-history-v1", JSON.stringify([item]));
  render(<App />);
  expect(await screen.findByRole("combobox", { name: "World" })).toHaveValue("1");
  await userEvent.click(screen.getByRole("button", { name: "Who is Yan?" }));
  await waitFor(() => expect(screen.getByRole("combobox", { name: "World" })).toHaveValue("2"));
  expect(localStorage.getItem("lore-world-v1")).toBe("2");
  await waitFor(() => expect(overview).toHaveBeenLastCalledWith("2"));
});
