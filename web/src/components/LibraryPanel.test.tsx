import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LibraryPanel } from "./LibraryPanel";

const gemini = { used_today: 120, limit: 500, answer_reserve: 50 };
const job = (o: object) => ({ stage: "extract", state: "running", done: 3, total: 10, stopped_reason: null, resumable_at: null,
  can_continue: false, continue_reason: "", ...o });
const book = (id: string, j: object | null, status = "graphed", world: string | null = "Alpha Saga", series: string | null = null, series_index: number | null = null) =>
  ({ id, title: `Book ${id}`, author: null, status, world, series, series_index, job: j });
const worldRow = (id: number, name: string, books: object[] = []) => ({ id, name, books });
let status: { books: object[]; gemini: object };
let worlds: object[];
let posts: string[];
let sent: { url: string; method: string; body: unknown }[];
let postReply: (url: string) => { status: number; body: object };

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
  vi.setSystemTime(new Date("2026-10-28T10:00:00Z"));
  posts = [];
  sent = [];
  worlds = [worldRow(1, "Alpha Saga"), worldRow(2, "Beta Realm")];
  postReply = () => ({ status: 202, body: { started: true } });
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: { method?: string; body?: string }) => {
    if (init?.method && init.method !== "GET") {
      if (init.method === "POST") posts.push(url);
      sent.push({ url, method: init.method, body: init.body ? JSON.parse(init.body) : undefined });
      const r = postReply(url);
      return { status: r.status, ok: r.status < 400, json: async () => r.body };
    }
    return { status: 200, ok: true, json: async () => (url === "/api/worlds" ? worlds : status) };
  }));
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

const open = async () => { render(<LibraryPanel open onClose={() => {}} showInternals={false} />); await act(async () => {}); };
const tick = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

test("paused: Continue is disabled until the time passes, then the timer enables it", async () => {
  status = { gemini, books: [book("a", job({ state: "waiting_quota", stopped_reason: "Daily limit reached", resumable_at: "2026-10-28T10:20:00Z", can_continue: false }))] };
  await open();
  expect(screen.getByText("Daily limit reached")).toBeInTheDocument();
  expect(screen.getByText(/Continues after \d\d:\d\d \(your time\) — in 0h 20m/)).toBeInTheDocument();
  const btn = screen.getByRole("button", { name: "Continue Book a" });
  expect(btn).toBeDisabled();
  await tick(30000 * 41);
  expect(screen.getByRole("button", { name: "Continue Book a" })).toBeEnabled();
});

test("processing shows the stage and an accessible progress bar", async () => {
  status = { gemini, books: [book("a", job({}), "ingested")] };
  await open();
  expect(screen.getByText("Reading passages")).toBeInTheDocument();
  const bar = screen.getByRole("progressbar");
  expect(bar).toHaveAttribute("aria-valuenow", "3");
  expect(bar).toHaveAttribute("aria-valuemax", "10");
  expect(screen.getByText("3 / 10")).toBeInTheDocument();
  expect(screen.getByText(/Question allowance today: 120 of 500 used · 50 kept/)).toBeInTheDocument();
  vi.clearAllTimers();
});

test("failed book shows the error and Retry", async () => {
  status = { gemini, books: [book("a", job({ state: "failed", stopped_reason: "Disk full" }), "ingested")] };
  await open();
  expect(screen.getByText("Disk full")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Retry Book a" })).toBeEnabled();
});

test("an unprocessed book shows Process; a graphed one is Ready", async () => {
  status = { gemini, books: [book("a", null, "ingested"), book("b", null)] };
  await open();
  expect(screen.getByRole("button", { name: "Process Book a" })).toBeInTheDocument();
  expect(screen.getByText("Ready")).toBeInTheDocument();
});

test("Continue POSTs, then polls every 3s until no job is running", async () => {
  status = { gemini, books: [book("a", job({ state: "waiting_quota", can_continue: true }))] };
  await open();
  const gets = () => (fetch as ReturnType<typeof vi.fn>).mock.calls.filter((c) => c[0] === "/api/books").length;
  status = { gemini, books: [book("a", job({}))] };
  await act(async () => { screen.getByRole("button", { name: "Continue Book a" }).click(); });
  expect(posts).toEqual(["/api/books/a/continue"]);
  const after = gets();
  await tick(3000);
  expect(gets()).toBe(after + 1);
  status = { gemini, books: [book("a", job({ state: "done" }))] };
  await tick(3000);
  const settled = gets();
  await tick(9000);
  expect(gets()).toBe(settled);
  expect(screen.getByText("Ready")).toBeInTheDocument();
});

test("a 409 message is shown", async () => {
  status = { gemini, books: [book("a", job({ state: "failed", stopped_reason: "x" }))] };
  postReply = () => ({ status: 409, body: { error: { code: "quota", message: "The daily limit is not reset yet." } } });
  await open();
  await act(async () => { screen.getByRole("button", { name: "Retry Book a" }).click(); });
  expect(screen.getByText("The daily limit is not reset yet.")).toBeInTheDocument();
});

test("after Continue, polling continues even if the first GET still shows paused", async () => {
  const paused = job({ state: "waiting_quota", can_continue: true });
  status = { gemini, books: [book("a", paused)] };
  await open();
  const gets = () => (fetch as ReturnType<typeof vi.fn>).mock.calls.filter((c) => c[0] === "/api/books").length;
  await act(async () => { screen.getByRole("button", { name: "Continue Book a" }).click(); });
  const after = gets();
  await tick(3000); // still paused: the worker has not written yet
  expect(gets()).toBe(after + 1);
  await tick(3000);
  expect(gets()).toBe(after + 2);
  status = { gemini, books: [book("a", job({}))] };
  await tick(3000);
  status = { gemini, books: [book("a", job({ state: "done" }))] };
  await tick(3000);
  const settled = gets();
  await tick(9000);
  expect(gets()).toBe(settled);
});

test("the forced polling gives up after about ten cycles", async () => {
  status = { gemini, books: [book("a", job({ state: "waiting_quota", can_continue: true }))] };
  await open();
  const gets = () => (fetch as ReturnType<typeof vi.fn>).mock.calls.filter((c) => c[0] === "/api/books").length;
  await act(async () => { screen.getByRole("button", { name: "Continue Book a" }).click(); });
  await tick(3000 * 12);
  const settled = gets();
  await tick(9000);
  expect(gets()).toBe(settled);
});

// --- worlds ----------------------------------------------------------------

const click = async (el: HTMLElement) => { await act(async () => { el.click(); }); };

test("books are grouped by world, each header listing its series", async () => {
  worlds = [worldRow(1, "Alpha Saga"), worldRow(2, "Beta Realm"), worldRow(3, "Empty Isle")];
  status = { gemini, books: [book("a1", null, "graphed", "Alpha Saga", "The Alpha Books", 1), book("a2", null, "graphed", "Alpha Saga", "The Alpha Books", 2),
    book("b1", null, "graphed", "Beta Realm")] };
  await open();
  const alpha = screen.getByRole("group", { name: "World Alpha Saga" });
  expect(within(alpha).getByText("Series: The Alpha Books")).toBeInTheDocument();
  expect(within(alpha).getAllByRole("listitem").map((li) => li.querySelector("strong")?.textContent)).toEqual(["Book a1", "Book a2"]);
  const beta = screen.getByRole("group", { name: "World Beta Realm" });
  expect(within(beta).getAllByRole("listitem")).toHaveLength(1);
  expect(within(beta).queryByText(/Series:/)).toBeNull();
  expect(within(screen.getByRole("group", { name: "World Empty Isle" })).getByText("No books in this world yet.")).toBeInTheDocument();
});

test("Move to world: pick an existing world, confirm, POST, then poll like Continue", async () => {
  status = { gemini, books: [book("a", null)] };
  await open();
  const gets = () => (fetch as ReturnType<typeof vi.fn>).mock.calls.filter((c) => c[0] === "/api/books").length;
  await click(screen.getByRole("button", { name: "Move Book a to another world" }));
  const pick = screen.getByRole("combobox", { name: "World to move Book a to" });
  expect(within(pick).queryByRole("option", { name: "Alpha Saga" })).toBeNull(); // its own world is not offered
  await userEvent.selectOptions(pick, "Beta Realm");
  expect(screen.getByText(/Move “Book a” to the world “Beta Realm”\?/)).toBeInTheDocument();
  expect(posts).toEqual([]);                       // nothing happens before the confirmation
  status = { gemini, books: [book("a", job({ stage: "remove", total: 1, done: 0 }))] };
  await click(screen.getByRole("button", { name: "Confirm move" }));
  expect(sent.at(-1)).toEqual({ url: "/api/books/a/move", method: "POST", body: { world: 2, create: false } });
  const after = gets();
  expect(screen.getByText("Taking it out of the graph")).toBeInTheDocument();
  await tick(3000);
  expect(gets()).toBe(after + 1);
  status = { gemini, books: [book("a", job({ state: "done" }), "graphed", "Beta Realm")] };
  await tick(3000);
  const settled = gets();
  await tick(9000);
  expect(gets()).toBe(settled);
  expect(within(screen.getByRole("group", { name: "World Beta Realm" })).getByText("Book a")).toBeInTheDocument();
});

test("Move to world: a new world is created by the same request", async () => {
  status = { gemini, books: [book("a", null)] };
  await open();
  await click(screen.getByRole("button", { name: "Move Book a to another world" }));
  await userEvent.selectOptions(screen.getByRole("combobox", { name: "World to move Book a to" }), "New world…");
  expect(screen.getByRole("button", { name: "Confirm move" })).toBeDisabled();       // a name is needed
  await userEvent.type(screen.getByRole("textbox", { name: "New world name" }), "  Gamma Isles ");
  expect(screen.getByText(/to a new world named “Gamma Isles”\?/)).toBeInTheDocument();
  await click(screen.getByRole("button", { name: "Confirm move" }));
  expect(sent.at(-1)).toEqual({ url: "/api/books/a/move", method: "POST", body: { world: "Gamma Isles", create: true } });
});

test("Cancel closes the move picker without a request", async () => {
  status = { gemini, books: [book("a", null)] };
  await open();
  await click(screen.getByRole("button", { name: "Move Book a to another world" }));
  await click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.queryByRole("combobox", { name: "World to move Book a to" })).toBeNull();
  expect(sent).toEqual([]);
});

test("a refused move shows the server's message", async () => {
  status = { gemini, books: [book("a", null)] };
  postReply = () => ({ status: 409, body: { error: { code: "busy", message: "another book is being processed" } } });
  await open();
  await click(screen.getByRole("button", { name: "Move Book a to another world" }));
  await click(screen.getByRole("button", { name: "Confirm move" }));
  expect(screen.getByText("another book is being processed")).toBeInTheDocument();
});

test("a book being processed cannot be moved", async () => {
  status = { gemini, books: [book("a", job({}), "ingested")] };
  await open();
  expect(screen.getByRole("button", { name: "Move Book a to another world" })).toBeDisabled();
  vi.clearAllTimers();
});

test("an interrupted move offers Continue with the move's stage names", async () => {
  status = { gemini, books: [book("a", job({ stage: "reassign", state: "idle", can_continue: true, stopped_reason: "interrupted" }))] };
  await open();
  expect(screen.getByRole("button", { name: "Continue Book a" })).toBeEnabled();
  await click(screen.getByRole("button", { name: "Continue Book a" }));
  expect(posts).toEqual(["/api/books/a/continue"]);
});

test("rename a world", async () => {
  status = { gemini, books: [book("a", null)] };
  await open();
  await click(screen.getByRole("button", { name: "Rename world Alpha Saga" }));
  const input = screen.getByRole("textbox", { name: "New name for world Alpha Saga" });
  await userEvent.clear(input);
  await userEvent.type(input, "Alpha Lands");
  postReply = () => ({ status: 200, body: { id: 1, name: "Alpha Lands" } });
  await click(screen.getByRole("button", { name: "Save" }));
  expect(sent.at(-1)).toEqual({ url: "/api/worlds/1", method: "PATCH", body: { name: "Alpha Lands" } });
  expect(screen.queryByRole("textbox", { name: "New name for world Alpha Saga" })).toBeNull();
});

test("a rename the server refuses keeps the form open and shows why", async () => {
  status = { gemini, books: [book("a", null)] };
  postReply = () => ({ status: 409, body: { error: { code: "exists", message: "a world named 'Beta Realm' already exists" } } });
  await open();
  await click(screen.getByRole("button", { name: "Rename world Alpha Saga" }));
  const input = screen.getByRole("textbox", { name: "New name for world Alpha Saga" });
  await userEvent.clear(input);
  await userEvent.type(input, "Beta Realm");
  await click(screen.getByRole("button", { name: "Save" }));
  expect(screen.getByText("a world named 'Beta Realm' already exists")).toBeInTheDocument();
  expect(input).toBeInTheDocument();
});

test("only an empty world can be deleted, after a confirmation", async () => {
  worlds = [worldRow(1, "Alpha Saga"), worldRow(3, "Empty Isle")];
  status = { gemini, books: [book("a", null)] };
  await open();
  expect(screen.queryByRole("button", { name: "Delete world Alpha Saga" })).toBeNull();
  await click(screen.getByRole("button", { name: "Delete world Empty Isle" }));
  expect(screen.getByText("Delete the empty world “Empty Isle”?")).toBeInTheDocument();
  expect(sent).toEqual([]);
  postReply = () => ({ status: 200, body: { deleted: true } });
  await click(screen.getByRole("button", { name: "Delete world" }));
  expect(sent.at(-1)).toEqual({ url: "/api/worlds/3", method: "DELETE", body: undefined });
});

test("changes tell the page so it can refetch the worlds and the graph", async () => {
  worlds = [worldRow(1, "Alpha Saga"), worldRow(3, "Empty Isle")];
  status = { gemini, books: [book("a", null)] };
  const changed = vi.fn();
  render(<LibraryPanel open onClose={() => {}} showInternals={false} onChange={changed} />);
  await act(async () => {});
  postReply = () => ({ status: 200, body: { deleted: true } });
  await click(screen.getByRole("button", { name: "Delete world Empty Isle" }));
  await click(screen.getByRole("button", { name: "Delete world" }));
  expect(changed).toHaveBeenCalledTimes(1);
});
