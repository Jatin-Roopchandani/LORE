import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LibraryPanel } from "./LibraryPanel";
import { LIBRARY, STAGE_LABEL } from "../messages";

const gemini = { used_today: 120, limit: 500, answer_reserve: 50 };
const job = (o: object) => ({ stage: "extract", state: "running", done: 3, total: 10, stopped_reason: null, resumable_at: null,
  can_continue: false, continue_reason: "", ...o });
const book = (id: string, j: object | null, status = "graphed", world: string | null = "Alpha Saga", series: string | null = null, series_index: number | null = null) =>
  ({ id, title: `Book ${id}`, author: null, status, world, series, series_index, job: j });
const worldRow = (id: number, name: string, books: object[] = []) => ({ id, name, books });
let status: { books: object[]; gemini: object; usage?: object; sample?: boolean };
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

test("footer, Gemini free tier: today's requests (as before)", async () => {
  status = { gemini, usage: { mode: "free", provider: "gemini" }, books: [book("a", null)] };
  await open();
  expect(screen.getByText(/Question allowance today: 120 of 500 used · 50 kept/)).toBeInTheDocument();
  vi.clearAllTimers();
});

test("footer, Claude: today's tokens, the estimated cost and the cap; no Gemini allowance", async () => {
  status = { gemini, books: [book("a", null)],
    usage: { mode: "metered", provider: "anthropic", tokens_today: 1234567, cost_usd: 4.2, daily_token_cap: 5000000 } };
  await open();
  expect(screen.getByText("Claude · today: 1,234,567 tokens (about $4.20) · daily cap 5,000,000")).toBeInTheDocument();
  expect(screen.queryByText(/allowance/)).toBeNull();
  vi.clearAllTimers();
});

test("footer, Gemini paid or OpenAI: tokens, and no cost where no price is known", async () => {
  status = { gemini, books: [book("a", null)],
    usage: { mode: "metered", provider: "openai", tokens_today: 900, cost_usd: null, daily_token_cap: null } };
  await open();
  expect(screen.getByText("OpenAI · today: 900 tokens")).toBeInTheDocument();
  vi.clearAllTimers();
});

test("footer, local mode on a billed provider: no token count kept", async () => {
  status = { gemini, books: [book("a", null)],
    usage: { mode: "metered", provider: "gemini", tokens_today: null, cost_usd: null, daily_token_cap: null } };
  await open();
  expect(screen.getByText("Gemini · billed to your own account")).toBeInTheDocument();
  vi.clearAllTimers();
});

test("a running step with no real count shows an indeterminate bar, not a frozen 0 / 1", async () => {
  status = { gemini, books: [book("a", job({ stage: "load", done: 0, total: 1 }), "ingested")] };
  await open();
  expect(screen.getByText("Building the graph")).toBeInTheDocument();
  const bar = screen.getByRole("progressbar");
  expect(bar).toHaveClass("indeterminate");
  expect(bar).not.toHaveAttribute("aria-valuenow");
  expect(screen.getByText(LIBRARY.slowStep)).toBeInTheDocument();
  expect(screen.queryByText("0 / 1")).toBeNull();
  vi.clearAllTimers();
});

test("the load's real counts show as a bar and a grouped count", async () => {
  status = { gemini, books: [book("a", job({ stage: "load", done: 340, total: 2900 }), "ingested")] };
  await open();
  expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "340");
  expect(screen.getByText("340 / 2,900")).toBeInTheDocument();
  vi.clearAllTimers();
});

const plan = {
  step: 4, of: 6, seconds_left: 3000, unknown_left: false,
  steps: [
    { stage: "extract", state: "done", seconds_left: 0 }, { stage: "load", state: "done", seconds_left: 0 },
    { stage: "beats", state: "done", seconds_left: 0 },
    { stage: "codex", state: "current", done: 120, total: 2400, seconds_left: 2700 },
    { stage: "disambiguate", state: "upcoming", seconds_left: 300 },
    { stage: "ready", state: "upcoming", seconds_left: null },
  ],
};

test("a running book shows where it is in the whole pipeline, collapsed, with time left", async () => {
  status = { gemini, books: [book("a", job({ stage: "codex", done: 120, total: 2400, plan }), "ingested")] };
  await open();
  const summary = screen.getByText("Step 4 of 6 · Writing entity profiles · ~50 min left");
  const details = summary.closest("details")!;
  expect(details).not.toHaveAttribute("open");
  await userEvent.click(summary);
  expect(details).toHaveAttribute("open");
  const items = within(screen.getByRole("list", { name: LIBRARY.stepsLabel })).getAllByRole("listitem");
  expect(items.map((li) => li.className)).toEqual(["step done", "step done", "step done", "step current", "step upcoming", "step upcoming"]);
  expect(items[3]).toHaveAttribute("aria-current", "step");
  expect(within(items[3]).getByText("120 / 2,400")).toBeInTheDocument();
  expect(within(items[3]).getByText("~45 min")).toBeInTheDocument();
  expect(within(items[0]).getByText(LIBRARY.stepDone)).toBeInTheDocument();
  expect(within(items[4]).getByText("~5 min")).toBeInTheDocument();
  expect(within(items[5]).getByText("a few minutes")).toBeInTheDocument();  // nothing measured: no invented precision
  vi.clearAllTimers();
});

test("the total says when some step has no estimate, and no list without a plan", async () => {
  status = { gemini, books: [
    book("a", job({ plan: { ...plan, seconds_left: 5400, unknown_left: true } }), "ingested"),
    book("b", job({}), "ingested")] };
  await open();
  expect(screen.getByText("Step 4 of 6 · Writing entity profiles · ~1.5 h left (plus a few minutes)")).toBeInTheDocument();
  expect(screen.getAllByText(/^Step \d of \d/)).toHaveLength(1);
  vi.clearAllTimers();
});

test("durations are rounded to friendly values", () => {
  expect(LIBRARY.duration(null)).toBe("a few minutes");
  expect(LIBRARY.duration(20)).toBe("under a minute");
  expect(LIBRARY.duration(70)).toBe("~1 min");
  expect(LIBRARY.duration(7 * 60 + 20)).toBe("~7 min");
  expect(LIBRARY.duration(23 * 60)).toBe("~25 min");
  expect(LIBRARY.duration(58 * 60)).toBe("~1 h");
  expect(LIBRARY.duration(100 * 60)).toBe("~1.5 h");
  expect(LIBRARY.left(null, true)).toBeNull();
});

test("a running job names the provider it runs on", async () => {
  status = { gemini, books: [book("a", job({ provider: "anthropic" }), "ingested")] };
  await open();
  expect(screen.getByText("Reading passages · Claude")).toBeInTheDocument();
  vi.clearAllTimers();
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

test("the codex stage has its own label and progress bar, beside remove and reassign", async () => {
  status = { gemini, books: [book("a", job({ stage: "codex", done: 4, total: 9 }), "ingested")] };
  await open();
  expect(screen.getByText("Writing entity profiles")).toBeInTheDocument();
  expect(screen.getByRole("progressbar", { name: "Writing entity profiles progress" })).toHaveAttribute("aria-valuenow", "4");
  expect(STAGE_LABEL.codex).toBe("Writing entity profiles");
  expect(STAGE_LABEL.remove).toBe("Taking it out of the graph");
  expect(STAGE_LABEL.reassign).toBe("Changing its world");
  vi.clearAllTimers();
});

test("paused for the Gemini key: the reason shows and Continue is enabled at once", async () => {
  status = { gemini, books: [book("a", job({ state: "waiting_key", stopped_reason: "Paused: Gemini refused your API key. Replace it in Account settings, then this book continues.",
    can_continue: true }))] };
  await open();
  expect(screen.getByText(/Gemini refused your API key/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Continue Book a" })).toBeEnabled();
});

test("opened at a book just uploaded: that row is marked and the panel keeps polling until it runs", async () => {
  status = { gemini, books: [book("a", null, "graphed"), book("new1", null, "chunked")] };
  render(<LibraryPanel open onClose={() => {}} showInternals={false} focusBook="new1" />);
  await act(async () => {});
  const rows = screen.getAllByRole("listitem");
  const marked = rows.filter((r) => r.getAttribute("aria-current") === "true");
  expect(marked).toHaveLength(1);
  expect(within(marked[0]).getByText("Book new1")).toBeInTheDocument();
  const gets = () => (fetch as unknown as { mock: { calls: [string, { method?: string }?][] } }).mock.calls
    .filter(([url, init]) => url === "/api/books" && !init?.method).length;
  const before = gets();
  status = { gemini, books: [book("a", null, "graphed"), book("new1", job({ stage: "chunk", done: 0, total: 1 }), "chunked")] };
  await tick(3000);
  expect(gets()).toBeGreaterThan(before);
  expect(screen.getByText("Splitting into passages")).toBeInTheDocument();
  vi.clearAllTimers();
});

test("an empty library on a server with the sample offers it; one click adds it and tells the page", async () => {
  status = { gemini, books: [], sample: true };
  worlds = [];
  const changed = vi.fn();
  postReply = (url) => {
    if (url === "/api/library/sample") status = { gemini, books: [book("s", null, "graphed", "Sherlock Holmes")], sample: false };
    return { status: 201, body: { added: true, book: { id: "s", title: "Book s", world: "Sherlock Holmes" } } };
  };
  render(<LibraryPanel open onClose={() => {}} showInternals={false} onChange={changed} />);
  await act(async () => {});
  expect(screen.getByText(LIBRARY.sampleLead)).toBeInTheDocument();
  await userEvent.setup({ advanceTimers: vi.advanceTimersByTime }).click(screen.getByRole("button", { name: LIBRARY.addSample }));
  await act(async () => {});
  expect(posts).toContain("/api/library/sample");
  expect(screen.getByText("Book s")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: LIBRARY.addSample })).toBeNull();
  expect(changed).toHaveBeenCalled();
});

test("the sample is not offered when the server has none, or once the library has a book", async () => {
  status = { gemini, books: [], sample: false };
  worlds = [];
  await open();
  expect(screen.queryByRole("button", { name: LIBRARY.addSample })).toBeNull();
});

test("a refused sample shows why and keeps the library as it was", async () => {
  status = { gemini, books: [], sample: true };
  worlds = [];
  postReply = () => ({ status: 409, body: { error: { code: "busy", message: "a book is being added or processed" } } });
  await open();
  await userEvent.setup({ advanceTimers: vi.advanceTimersByTime }).click(screen.getByRole("button", { name: LIBRARY.addSample }));
  await act(async () => {});
  expect(screen.getByRole("alert")).toHaveTextContent("a book is being added or processed");
});

test("the sample book carries a Sample tag; other books don't", async () => {
  status = { gemini, books: [{ ...book("s", null), is_sample: true }, book("a", null)] };
  await open();
  const tags = screen.getAllByText(LIBRARY.sampleTag);
  expect(tags).toHaveLength(1);
  expect(tags[0].closest("li, .lib-row, div")?.textContent).toContain("Book s");
  expect(tags[0]).toHaveAttribute("title", LIBRARY.sampleTagHint);
});

test("a book is deleted only after a confirmation that names it and says it can't be undone", async () => {
  status = { gemini, books: [book("a", job({ state: "done" })), book("b", null)] };
  const changed = vi.fn();
  render(<LibraryPanel open onClose={() => {}} showInternals={false} onChange={changed} />);
  await act(async () => {});
  await click(screen.getByRole("button", { name: LIBRARY.deleteBookLabel("Book a") }));
  expect(screen.getByText(LIBRARY.deleteBookConfirm("Book a"))).toBeInTheDocument();
  expect(LIBRARY.deleteBookConfirm("Book a")).toMatch(/can't be undone.*upload it and process it again/);
  expect(sent).toEqual([]);
  // Cancel sends nothing.
  await click(screen.getByRole("button", { name: LIBRARY.deleteBookCancel }));
  expect(sent).toEqual([]);
  await click(screen.getByRole("button", { name: LIBRARY.deleteBookLabel("Book a") }));
  postReply = () => ({ status: 200, body: { deleted: true, book: { id: "a", title: "Book a" }, world: "Alpha Saga", world_removed: false } });
  status = { gemini, books: [book("b", null)] };
  await click(screen.getByRole("button", { name: LIBRARY.deleteBookConfirmButton }));
  expect(sent.at(-1)).toEqual({ url: "/api/books/a", method: "DELETE", body: undefined });
  // The list refetched without it, and the page was told (worlds, graph and reading progress refetch).
  expect(screen.queryByText("Book a")).toBeNull();
  expect(screen.getByText("Book b")).toBeInTheDocument();
  expect(changed).toHaveBeenCalledTimes(1);
});

test("a processing book can't be deleted, and says why", async () => {
  status = { gemini, books: [book("a", job({})), book("q", null, "ingested", "Alpha Saga"), book("r", job({ state: "done" }))] };
  (status.books[1] as { processing?: boolean }).processing = true; // queued for the worker
  await open();
  expect(screen.getByRole("button", { name: LIBRARY.deleteBookLabel("Book a") })).toBeDisabled();
  expect(screen.getByRole("button", { name: LIBRARY.deleteBookLabel("Book q") })).toBeDisabled();
  expect(screen.getByRole("button", { name: LIBRARY.deleteBookLabel("Book r") })).toBeEnabled();
  expect(screen.getAllByText(LIBRARY.deleteBookBusy)).toHaveLength(2);
  vi.clearAllTimers();
});

test("a refused delete shows the reason on the row and keeps the book", async () => {
  status = { gemini, books: [book("a", job({ state: "done" }))] };
  const changed = vi.fn();
  render(<LibraryPanel open onClose={() => {}} showInternals={false} onChange={changed} />);
  await act(async () => {});
  postReply = () => ({ status: 409, body: { error: { code: "busy", message: "a book is being added or processed in your library; try again when it is done" } } });
  await click(screen.getByRole("button", { name: LIBRARY.deleteBookLabel("Book a") }));
  await click(screen.getByRole("button", { name: LIBRARY.deleteBookConfirmButton }));
  expect(screen.getByRole("alert")).toHaveTextContent("a book is being added or processed");
  expect(screen.getByText("Book a")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: LIBRARY.deleteBookLabel("Book a") })).toBeEnabled();
  expect(changed).not.toHaveBeenCalled();
});
