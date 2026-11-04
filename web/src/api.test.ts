import { adminApi, api, authApi, csrfHeaders, keyApi, nav, onKeyProblem, parseSse, streamAsk } from "./api";

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

test("ask sends the depth in its body, and leaves it out when not given", async () => {
  jsonFetch({}, 400);
  for await (const _ of streamAsk("who?", new AbortController().signal, "2", "deep")) { /* drain */ }
  expect(JSON.parse(lastCall()[1]!.body as string)).toEqual({ question: "who?", world: "2", depth: "deep" });
  for await (const _ of streamAsk("who?", new AbortController().signal, undefined, "focused")) { /* drain */ }
  expect(JSON.parse(lastCall()[1]!.body as string)).toEqual({ question: "who?", depth: "focused" });
});

test("the explorer reads hit the right paths, in the world", async () => {
  jsonFetch();
  await api.profile("a_zed", "2");
  expect(lastCall()[0]).toBe("/api/entity/a_zed/profile?world=2");
  await api.timeline("a_zed");
  expect(lastCall()[0]).toBe("/api/entity/a_zed/timeline");
  await api.relations("a_zed", "2");
  expect(lastCall()[0]).toBe("/api/entity/a_zed/relations?world=2");
  await api.edge("a_zed", "a_yan", "2");
  expect(lastCall()[0]).toBe("/api/edge?a=a_zed&b=a_yan&world=2");
  await api.search("zed q", "2");
  expect(lastCall()[0]).toBe("/api/search?q=zed%20q&limit=8&world=2");
  await api.path("a_zed", "a_yan", "all");
  expect(lastCall()[0]).toBe("/api/path?a=a_zed&b=a_yan&max=4&world=all");
  await api.passage("ba1:1:0", "2");
  expect(lastCall()[0]).toBe("/api/passage/ba1%3A1%3A0?world=2");
});

describe("the CSRF token", () => {
  const setCookie = (value: string) => { document.cookie = `lore_csrf=${value}; path=/`; };
  afterEach(() => { document.cookie = "lore_csrf=; path=/; max-age=0"; });

  test("is read from the lore_csrf cookie", () => {
    setCookie("abc123");
    expect(csrfHeaders()).toEqual({ "X-CSRF-Token": "abc123" });
  });

  test("nothing is sent when there is no cookie (local mode)", () => {
    expect(csrfHeaders()).toEqual({});
  });

  test("every mutating call sends it", async () => {
    setCookie("tok");
    jsonFetch({ started: true });
    await api.continueBook("b1");
    expect((lastCall()[1]!.headers as Record<string, string>)["X-CSRF-Token"]).toBe("tok");
    await api.moveBook("b1", "W", true);
    expect((lastCall()[1]!.headers as Record<string, string>)["X-CSRF-Token"]).toBe("tok");
    await api.createWorld("Z");
    expect((lastCall()[1]!.headers as Record<string, string>)["X-CSRF-Token"]).toBe("tok");
    await api.renameWorld(1, "Z");
    expect((lastCall()[1]!.headers as Record<string, string>)["X-CSRF-Token"]).toBe("tok");
    await api.deleteWorld(1);
    expect((lastCall()[1]!.headers as Record<string, string>)["X-CSRF-Token"]).toBe("tok");
    jsonFetch({}, 400);
    for await (const _ of streamAsk("who?", new AbortController().signal)) { /* drain */ }
    expect((lastCall()[1]!.headers as Record<string, string>)["X-CSRF-Token"]).toBe("tok");
  });

  test("reads do not carry it", async () => {
    setCookie("tok");
    jsonFetch();
    await api.worlds();
    expect(lastCall()[1]!.headers).not.toHaveProperty("X-CSRF-Token");
  });
});

describe("signed-out and strict errors", () => {
  afterEach(() => { document.head.innerHTML = ""; });

  test("a 401 in multi-user mode sends the browser to sign in", async () => {
    document.head.innerHTML = '<meta name="lore-multi" content="1">';
    const go = vi.spyOn(nav, "to").mockImplementation(() => {});
    jsonFetch({ detail: "sign in required" }, 401);
    const r = await api.worlds();
    expect(go).toHaveBeenCalledWith("/signin");
    expect(r).toEqual({ error: { code: "unauthorized", message: "" } });
  });

  test("local mode never redirects", async () => {
    const go = vi.spyOn(nav, "to").mockImplementation(() => {});
    jsonFetch({ detail: "x" }, 401);
    await api.worlds();
    expect(go).not.toHaveBeenCalled();
  });

  test("a 401 on a mutation and on the ask stream also signs out", async () => {
    document.head.innerHTML = '<meta name="lore-multi" content="1">';
    const go = vi.spyOn(nav, "to").mockImplementation(() => {});
    jsonFetch({ detail: "x" }, 401);
    await api.createWorld("w");
    await api.continueBook("b");
    const out = [];
    for await (const e of streamAsk("q", new AbortController().signal)) out.push(e.type);
    expect(go).toHaveBeenCalledTimes(3);
    expect(out).toEqual(["error"]);
  });

  test("a bare 403 from the admin API is an error, not data", async () => {
    jsonFetch({ detail: "admins only" }, 403);
    expect(await adminApi.users()).toEqual({ error: { code: "http_403", message: "admins only" } });
    expect(await adminApi.approve(1)).toEqual({ error: { code: "http_403", message: "admins only" } });
  });

  test("an {error} body keeps its own code", async () => {
    jsonFetch({ error: { code: "rate_limited", message: "slow" } }, 429);
    expect(await authApi.requestLink("a@b.co")).toEqual({ error: { code: "rate_limited", message: "slow" } });
  });

  test("the auth calls use the right paths and bodies", async () => {
    jsonFetch({});
    await authApi.requestLink("a@b.co", "INV");
    expect([lastCall()[0], lastCall()[1]!.method, JSON.parse(lastCall()[1]!.body as string)]).toEqual(["/auth/email", "POST", { email: "a@b.co", invite: "INV" }]);
    await authApi.requestLink("a@b.co");
    expect(JSON.parse(lastCall()[1]!.body as string)).toEqual({ email: "a@b.co" });
    await authApi.joinWaitlist("a@b.co", "Dracula", true);
    expect(JSON.parse(lastCall()[1]!.body as string)).toEqual({ email: "a@b.co", reading: "Dracula", consent: true, website: "" });
    await authApi.logout();
    expect([lastCall()[0], lastCall()[1]!.method]).toEqual(["/auth/logout", "POST"]);
    await adminApi.createInvite(7);
    expect(JSON.parse(lastCall()[1]!.body as string)).toEqual({ days: 7 });
  });
});

describe("the user's Gemini key", () => {
  afterEach(() => vi.unstubAllGlobals());

  test("the key calls use the right paths, verbs and bodies", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ present: false }))));
    await keyApi.get();
    await keyApi.save("AIzaKEY");
    await keyApi.remove();
    await keyApi.usage();
    const calls = (fetch as any).mock.calls.map(([url, init]: [string, RequestInit | undefined]) => [url, init?.method ?? "GET", init?.body]);
    expect(calls).toEqual([["/api/me/key", "GET", undefined], ["/api/me/key", "PUT", JSON.stringify({ key: "AIzaKEY" })],
      ["/api/me/key", "DELETE", undefined], ["/api/me/usage", "GET", undefined]]);
  });

  test("an ask answered key_required or key_invalid reports it", async () => {
    const seen: string[] = [];
    const stop = onKeyProblem((p) => seen.push(p));
    for (const code of ["key_required", "key_invalid", "busy"]) {
      vi.stubGlobal("fetch", vi.fn(async () => new Response(frame("error", { t: 0, error: { code, message: "" } }), { status: 200 })));
      for await (const _ of streamAsk("who?", new AbortController().signal)) { /* drain */ }
    }
    stop();
    expect(seen).toEqual(["key_required", "key_invalid"]);
  });

  test("a Continue or a move answered key_required reports it; the key routes themselves do not", async () => {
    const seen: string[] = [];
    const stop = onKeyProblem((p) => seen.push(p));
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: { code: "key_required", message: "" } }), { status: 409 })));
    await api.continueBook("b1");
    await api.moveBook("b1", 2);
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: { code: "key_invalid", message: "" } }), { status: 400 })));
    expect(await keyApi.save("AIzaBAD")).toEqual({ error: { code: "key_invalid", message: "" } });
    stop();
    expect(seen).toEqual(["key_required", "key_required"]);
  });
});
