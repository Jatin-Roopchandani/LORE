import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "./App";

const quota = vi.fn(() => Promise.resolve({ provider: "gemini", models: [], resets_at: "2026-10-08T07:00:00Z" }));
const overview = vi.fn(() => Promise.resolve({ nodes: [{ id: "h", name: "Holmes", kind: "Character", books: [] }], edges: [], truncated: false, depth: 0 }));
vi.mock("./components/GraphCanvas", () => ({ GraphCanvas: () => <div data-testid="graph" /> }));
vi.mock("./api", () => ({
  api: {
    settings: () => Promise.resolve({ show_internals: true }),
    library: () => Promise.resolve({ books: [], note: null }),
    quota: () => quota(),
    mcp: () => Promise.resolve({ error: { code: "x", message: "x" } }),
    entities: () => Promise.resolve({ entities: [] }),
    overview: () => overview(),
  },
  isError: (x: unknown) => typeof x === "object" && x !== null && "error" in x,
  // The stream drops before any answer.
  streamAsk: async function* () { yield { type: "error", t: 0, error: { code: "interrupted", message: "" } }; },
}));
Object.defineProperty(window, "matchMedia", { value: () => ({ matches: true, addEventListener() {}, removeEventListener() {} }) });

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
