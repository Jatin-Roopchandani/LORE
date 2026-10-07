import { parseSse, streamAsk } from "./api";

const frame = (event: string, data: object) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

test("parses complete frames and keeps the partial tail", () => {
  const text = frame("parts", { t: 0.1, sub_questions: ["a"] }) + "event: step\ndata: {\"t\":";
  const { events, rest } = parseSse(text);
  expect(events).toEqual([{ type: "parts", t: 0.1, sub_questions: ["a"] }]);
  expect(rest).toBe("event: step\ndata: {\"t\":");
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
  const stream = new ReadableStream({ start(c) { c.enqueue(new TextEncoder().encode(frame("parts", { t: 0, sub_questions: [] }))); c.error(new TypeError("network")); } });
  vi.stubGlobal("fetch", vi.fn(async () => new Response(stream, { status: 200 })));
  const out = [];
  for await (const e of streamAsk("q", new AbortController().signal)) out.push(e.type === "error" ? e.error.code : e.type);
  expect(out).toEqual(["parts", "interrupted"]);
});
