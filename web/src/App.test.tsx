import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "./App";
import { KEYS } from "./messages";

const quota = vi.fn(() => Promise.resolve({ provider: "gemini", models: [], resets_at: "2026-10-08T07:00:00Z" }));
const overview = vi.fn((_world?: string) => Promise.resolve({ nodes: [{ id: "h", name: "Holmes", kind: "Character", books: [] }], edges: [], truncated: false, depth: 0 }));
vi.mock("./components/GraphCanvas", () => ({ GraphCanvas: () => <div data-testid="graph" /> }));
const library = vi.fn((_world?: string) => Promise.resolve({ books: [], note: null }));
const worlds = [{ id: 1, name: "Alpha Saga", books: [] }, { id: 2, name: "Beta Realm", books: [] }];
const asked: (string | undefined)[] = [];
const me = vi.fn(() => Promise.resolve({ user: { id: "u", email: "me@example.org", name: null, role: "user" } }));
// The signed-in user's Gemini key (multi-user mode); the key-problem channel is the real one, so tests can fire it.
const keyStatus = vi.fn(() => Promise.resolve({ present: true, last4: "1234", added_at: null, last_ok_at: null, last_error: null } as object));
const keySave = vi.fn((_key: string) => Promise.resolve({ present: true, last4: "WXYZ", added_at: null, last_ok_at: null, last_error: null, check: "ok" } as object));
const keyListeners = new Set<(p: string) => void>();
const fireKeyProblem = (p: string) => keyListeners.forEach((l) => l(p));
let askError = "interrupted";
vi.mock("./api", () => ({
  authApi: { me: () => me() },
  keyApi: { get: () => keyStatus(), save: (k: string) => keySave(k), usage: () => Promise.resolve({ error: { code: "x", message: "" } }),
    remove: () => Promise.resolve({ deleted: true }) },
  onKeyProblem: (l: (p: string) => void) => { keyListeners.add(l); return () => { keyListeners.delete(l); }; },
  reportKeyProblem: (c: string) => fireKeyProblem(c),
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
  // The stream drops before any answer (or fails with `askError`, reporting a key problem as the real client does).
  streamAsk: async function* (_q: string, _signal: AbortSignal, world?: string) {
    asked.push(world);
    if (askError !== "interrupted") fireKeyProblem(askError);
    yield { type: "error", t: 0, error: { code: askError, message: "" } };
  },
}));
Object.defineProperty(window, "matchMedia", { value: () => ({ matches: true, addEventListener() {}, removeEventListener() {} }) });

beforeEach(() => { localStorage.clear(); asked.length = 0; me.mockClear(); keyStatus.mockClear(); keySave.mockClear(); askError = "interrupted"; });
afterEach(() => { document.head.innerHTML = ""; });

test("multi-user mode asks who is signed in once, which re-issues a lost CSRF cookie; local mode does not", async () => {
  const { unmount } = render(<App />);
  await screen.findByPlaceholderText("Ask about your books…");
  expect(me).not.toHaveBeenCalled();
  unmount();
  document.head.innerHTML = '<meta name="lore-multi" content="1">';
  render(<App />);
  await screen.findByPlaceholderText("Ask about your books…");
  expect(me).toHaveBeenCalledTimes(1);
});

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
  await userEvent.click(screen.getByRole("button", { name: "History" })); // the mocked narrow screen keeps history in a drawer
  await userEvent.click(screen.getByRole("button", { name: "Who is Yan?" }));
  await waitFor(() => expect(screen.getByRole("combobox", { name: "World" })).toHaveValue("2"));
  expect(localStorage.getItem("lore-world-v1")).toBe("2");
  await waitFor(() => expect(overview).toHaveBeenLastCalledWith("2"));
});

// --- each user's own Gemini key (multi-user mode) ---

const multi = () => { document.head.innerHTML = '<meta name="lore-multi" content="1">'; };
const noKey = { present: false, last4: null, added_at: null, last_ok_at: null, last_error: null };

test("a signed-in user without a key sees the onboarding screen, and saving one opens the app", async () => {
  multi();
  keyStatus.mockResolvedValueOnce(noKey);
  render(<App />);
  expect(await screen.findByRole("heading", { name: KEYS.onboardingTitle })).toBeInTheDocument();
  expect(screen.queryByPlaceholderText("Ask about your books…")).toBeNull();
  expect(screen.getByRole("link", { name: KEYS.getKey })).toHaveAttribute("href", "https://aistudio.google.com/app/apikey");
  expect(screen.getByText(/about 1,000 requests a day/)).toBeInTheDocument();
  expect(screen.getByText(/midnight Pacific/)).toBeInTheDocument();
  expect(screen.getByText(/sent to Gemini under your own key/)).toBeInTheDocument();
  await userEvent.type(screen.getByLabelText(KEYS.fieldLabel), "AIzaFAKEfakeFAKEfakeFAKEfakeFAKEfake123");
  await userEvent.click(screen.getByRole("button", { name: KEYS.save }));
  expect(keySave).toHaveBeenCalledWith("AIzaFAKEfakeFAKEfakeFAKEfakeFAKEfake123");
  expect(await screen.findByPlaceholderText("Ask about your books…")).toBeInTheDocument();
});

test("a user with a key goes straight to the app", async () => {
  multi();
  render(<App />);
  expect(await screen.findByPlaceholderText("Ask about your books…")).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: KEYS.onboardingTitle })).toBeNull();
  expect(keyStatus).toHaveBeenCalledTimes(1);
});

test("a key Gemini refused opens the replace screen", async () => {
  multi();
  keyStatus.mockResolvedValueOnce({ ...noKey, present: true, last4: "1234", last_error: "rejected" });
  render(<App />);
  expect(await screen.findByRole("heading", { name: KEYS.replaceTitle })).toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent(KEYS.rejected);
});

test.each([["key_required", KEYS.onboardingTitle], ["key_invalid", KEYS.replaceTitle]])(
  "an ask answered %s routes to the key screen", async (code, heading) => {
    multi();
    askError = code;
    render(<App />);
    await userEvent.type(await screen.findByPlaceholderText("Ask about your books…"), "who?{Enter}");
    expect(await screen.findByRole("heading", { name: heading })).toBeInTheDocument();
  });

test("local mode never asks about a key", async () => {
  render(<App />);
  await screen.findByPlaceholderText("Ask about your books…");
  expect(keyStatus).not.toHaveBeenCalled();
});
