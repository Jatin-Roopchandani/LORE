import { act, render, screen } from "@testing-library/react";
import { LibraryPanel } from "./LibraryPanel";

const gemini = { used_today: 120, limit: 500, answer_reserve: 50 };
const job = (o: object) => ({ stage: "extract", state: "running", done: 3, total: 10, stopped_reason: null, resumable_at: null,
  can_continue: false, continue_reason: "", ...o });
const book = (id: string, j: object | null, status = "graphed") => ({ id, title: `Book ${id}`, author: null, status, series: null, series_index: null, job: j });
let status: { books: object[]; gemini: object };
let posts: string[];
let postReply: () => { status: number; body: object };

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
  vi.setSystemTime(new Date("2026-10-28T10:00:00Z"));
  posts = [];
  postReply = () => ({ status: 202, body: { started: true } });
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: { method?: string }) => {
    if (init?.method === "POST") { posts.push(url); const r = postReply(); return { status: r.status, ok: r.status < 400, json: async () => r.body }; }
    return { status: 200, ok: true, json: async () => status };
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
  const gets = () => (fetch as ReturnType<typeof vi.fn>).mock.calls.filter((c) => c[1]?.method !== "POST").length;
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
  const gets = () => (fetch as ReturnType<typeof vi.fn>).mock.calls.filter((c) => c[1]?.method !== "POST").length;
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
  const gets = () => (fetch as ReturnType<typeof vi.fn>).mock.calls.filter((c) => c[1]?.method !== "POST").length;
  await act(async () => { screen.getByRole("button", { name: "Continue Book a" }).click(); });
  await tick(3000 * 12);
  const settled = gets();
  await tick(9000);
  expect(gets()).toBe(settled);
});
