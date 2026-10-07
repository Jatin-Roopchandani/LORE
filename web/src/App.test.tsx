import { render, screen } from "@testing-library/react";
import App from "./App";

vi.mock("./components/GraphCanvas", () => ({ GraphCanvas: () => <div data-testid="graph" /> }));
vi.mock("./api", () => ({
  api: {
    settings: () => Promise.resolve({ show_internals: true }),
    library: () => Promise.resolve({ books: [], note: null }),
    quota: () => Promise.resolve({ provider: "gemini", models: [], resets_at: "2026-10-08T07:00:00Z" }),
    mcp: () => Promise.resolve({ error: { code: "x", message: "x" } }),
  },
  isError: (x: unknown) => typeof x === "object" && x !== null && "error" in x,
  streamAsk: async function* () { /* never asked in this test */ },
}));

test("renders the top bar, the ask box and the graph pane", async () => {
  render(<App />);
  expect(screen.getByText("LORE")).toBeInTheDocument();
  expect(await screen.findByPlaceholderText("Ask about your books…")).toBeInTheDocument();
  expect(screen.getByTestId("graph")).toBeInTheDocument();
});
