// The history drawer and the Ctrl+K quick switcher open a whole chat thread from the server.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "./App";
import { CHAT, HISTORY, QUICK } from "./messages";
import type { ChatMessage, ChatSummary } from "./types";

vi.mock("./components/GraphCanvas", () => ({ GraphCanvas: () => <div data-testid="graph" /> }));
const result = (answer: string) => ({ answer, mode: "graph", template: null, note: null, model: null, selection_model: null,
  sub_questions: [], claims: [], passages: [], edges: [], seeds: [] });
const message = (id: number, question: string, extra: Partial<ChatMessage> = {}): ChatMessage => ({ id, role: "assistant", question,
  depth: "auto", world: "1", request_id: null, created_at: new Date().toISOString(), answer: { result: result(`Answer ${id}.`), steps: [] }, ...extra });
const chat: ChatSummary = { id: "c1", title: "Who is Ren Ashby?", world: "1", client_id: null, created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(), latest: message(2, "Where did he go?") };
const get = vi.fn((_id: string) => Promise.resolve({ chat, messages: [
  message(1, "Who is Ren Ashby?"), message(2, "Where did he go?", { standalone: "Where did Ren Ashby go?" })] }));
vi.mock("./chatsApi", async (importOriginal) => ({ ...(await importOriginal<typeof import("./chatsApi")>()),
  chatsApi: { list: () => Promise.resolve({ chats: [chat], next_offset: null }), get: (id: string) => get(id), create: vi.fn(), rename: vi.fn(),
    remove: () => Promise.resolve({ deleted: true }) } }));
vi.mock("./api", () => ({
  api: {
    settings: () => Promise.resolve({ show_internals: true }), library: () => Promise.resolve({ books: [], note: null }),
    worlds: () => Promise.resolve([{ id: 1, name: "Alpha Saga", books: [] }]),
    quota: () => Promise.resolve({ provider: "gemini", models: [], resets_at: "2026-10-08T07:00:00Z" }), mcp: () => Promise.resolve({ error: { code: "x", message: "x" } }),
    entities: () => Promise.resolve({ entities: [] }), overview: () => Promise.resolve({ nodes: [], edges: [], truncated: false, depth: 0 }),
    readingPosition: () => Promise.resolve({ error: { code: "x", message: "x" } }),
  },
  isError: (x: unknown) => typeof x === "object" && x !== null && "error" in x,
  onKeyProblem: () => () => {},
  streamAsk: async function* () { yield { type: "error", t: 0, error: { code: "interrupted", message: "" } }; },
}));
Object.defineProperty(window, "matchMedia", { value: (q: string) => ({ matches: !q.includes("max-width"), addEventListener() {}, removeEventListener() {} }) });

beforeEach(() => { localStorage.clear(); localStorage.setItem("lore-world-v1", "1"); get.mockClear(); });

const expectThread = async () => {
  await waitFor(() => expect([...document.querySelectorAll(".q")].map((q) => q.textContent)).toEqual(["Who is Ren Ashby?", "Where did he go?"]));
  await waitFor(() => expect(document.querySelector(".latest .understood")?.textContent).toBe(`${CHAT.understoodAs} Where did Ren Ashby go?`));
  expect(screen.getByPlaceholderText(CHAT.followupPlaceholder)).toBeInTheDocument();
};

test("the drawer lists a chat under its first question and opens the whole thread", async () => {
  render(<App />);
  await userEvent.click(await screen.findByRole("button", { name: HISTORY.open }));
  const drawer = await screen.findByRole("dialog", { name: HISTORY.title });
  await userEvent.click(await within(drawer).findByText("Who is Ren Ashby?"));
  await expectThread();
  expect(get).toHaveBeenCalledWith("c1");
});

test("the quick switcher opens the whole thread too", async () => {
  render(<App />);
  await screen.findByRole("button", { name: HISTORY.open });
  await waitFor(() => expect(localStorage.getItem("lore-history-v1")).toContain("c1"));
  await userEvent.keyboard("{Control>}k{/Control}");
  const box = await screen.findByRole("dialog", { name: QUICK.title });
  await userEvent.click(within(box).getByRole("option", { name: "Who is Ren Ashby?" }));
  await expectThread();
});
