import { api, parseSse, streamAsk } from "./api";

const frame = (event: string, data: object) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

test("parses complete frames and keeps the partial tail", () => {
  const text = frame("parts", { t: 0.1, sub_questions: ["a"] }) + "event: step\ndata: {\"t\":";
  const { events, rest } = parseSse(text);
  expect(events).toEqual([{ type: "parts", t: 0.1, sub_questions: ["a"] }]);
  expect(rest).toBe("event: step\ndata: {\"t\":");
});

test("handles CRLF line endings from SSE servers", () => {
  const { events, rest } = parseSse("event: parts\r\ndata: {\"t\":0,\"sub_questions\":[\"a\"]}\r\n\r\n");
  expect(events).toEqual([{ type: "parts", t: 0, sub_questions: ["a"] }]);
  expect(rest).toBe("");
});

test("streamAsk yields events across chunk boundaries", async () => {
  const body = frame("parts", { t: 0, sub_questions: ["a"] }) + frame("answer", { t: 1, result: { answer: "x" } });
  const bytes = new TextEncoder().encode(body);
  const chunks = [bytes.slice(0, 17), bytes.slice(17, 60), bytes.slice(60)];
  const stream = new ReadableStream({ start(c) { chunks.forEach((x) => c.enqueue(x)); c.close(); } });
  vi.stubGlobal("fetch", vi.fn(async () => new Response(stream, { status: 200, headers: { "content-type": "text/event-stream" } })));
  const out = [];
  for await (const e of streamAsk("who?", new AbortController().signal)) out.push(e.type);
  expect(out).toEqual(["parts", "answer"]);
  const [, init] = (fetch as any).mock.calls[0];
  expect(init.method).toBe("POST");
  expect(init.headers["Content-Type"]).toBe("application/json");
});

test("a 400 becomes an error event", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "a question is 1-2000 characters" }), { status: 400 })));
  const out = [];
  for await (const e of streamAsk("", new AbortController().signal)) out.push(e);
  expect(out).toEqual([{ type: "error", t: 0, error: { code: "bad_request", message: "a question is 1-2000 characters" } }]);
});

test("a dropped connection ends with an interrupted error", async () => {
  let sent = false;
  const stream = new ReadableStream({
    pull(c) {
      if (!sent) { sent = true; c.enqueue(new TextEncoder().encode(frame("parts", { t: 0, sub_questions: [] }))); }
      else c.error(new TypeError("network"));
    },
  });
  vi.stubGlobal("fetch", vi.fn(async () => new Response(stream, { status: 200 })));
  const out = [];
  for await (const e of streamAsk("q", new AbortController().signal)) out.push(e.type === "error" ? e.error.code : e.type);
  expect(out).toEqual(["parts", "interrupted"]);
});

const jsonFetch = (body: unknown = {}, status = 200) =>
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body), { status })));
const lastCall = () => (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls.at(-1) as [string, RequestInit | undefined];

test("every graph call passes the selected world", async () => {
  jsonFetch();
  await api.overview("2");
  expect(lastCall()[0]).toBe("/api/overview?limit=40&world=2");
  await api.subgraph("a b", "all");
  expect(lastCall()[0]).toBe("/api/subgraph?id=a%20b&depth=1&world=all");
  await api.entity("e1", "1");
  expect(lastCall()[0]).toBe("/api/entity?id=e1&world=1");
  await api.entities(["x", "y"], "1");
  expect(lastCall()[0]).toBe("/api/entities?ids=x%2Cy&world=1");
  await api.library("1");
  expect(lastCall()[0]).toBe("/api/library?world=1");
});

test("without a world the calls are as before", async () => {
  jsonFetch();
  await api.overview();
  expect(lastCall()[0]).toBe("/api/overview?limit=40");
  await api.library();
  expect(lastCall()[0]).toBe("/api/library");
});

test("ask sends the world in its body", async () => {
  jsonFetch({}, 400);
  for await (const _ of streamAsk("who?", new AbortController().signal, "2")) { /* drain */ }
  expect(JSON.parse(lastCall()[1]!.body as string)).toEqual({ question: "who?", world: "2" });
});

test("world calls use the right verbs and bodies", async () => {
  jsonFetch({ started: true });
  await api.moveBook("b 1", "New World", true);
  expect(lastCall()[0]).toBe("/api/books/b%201/move");
  expect(lastCall()[1]!.method).toBe("POST");
  expect(JSON.parse(lastCall()[1]!.body as string)).toEqual({ world: "New World", create: true });
  await api.renameWorld(3, "X");
  expect([lastCall()[0], lastCall()[1]!.method]).toEqual(["/api/worlds/3", "PATCH"]);
  await api.deleteWorld(3);
  expect([lastCall()[0], lastCall()[1]!.method]).toEqual(["/api/worlds/3", "DELETE"]);
  await api.createWorld("Z");
  expect([lastCall()[0], lastCall()[1]!.method]).toEqual(["/api/worlds", "POST"]);
});
