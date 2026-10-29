import { errorMessage, UI } from "./messages";

const now = new Date("2026-10-07T20:40:00-07:00");
const resetsAt = "2026-10-08T00:00:00-07:00";

test("budget names Gemini and the time left when internals show", () => {
  const m = errorMessage({ code: "budget_exhausted", message: "" }, { showInternals: true, resetsAt, now });
  expect(m.text).toBe("Today's Gemini requests are used up. They reset at midnight Pacific (in about 3h 20m).");
});

test("budget hides the provider when internals are off", () => {
  const m = errorMessage({ code: "budget_exhausted", message: "" }, { showInternals: false, resetsAt, now });
  expect(m.text).not.toMatch(/Gemini/);
});

test("overload offers a retry; Ollama gives the start command only with internals", () => {
  const busy = errorMessage({ code: "upstream_unavailable", message: "" }, { showInternals: true });
  expect(busy.retry).toBe(true);
  const err = { code: "upstream_unavailable", message: "Ollama at http://localhost:11434 failed: connection refused" };
  const o = errorMessage(err, { showInternals: true });
  expect(o.command).toContain("CUDA_VISIBLE_DEVICES=-1");
  expect(o.text).toBe("LORE can't reach Ollama. Start it, then ask again.");
  const hidden = errorMessage(err, { showInternals: false });
  expect(hidden.command).toBeUndefined();
  expect(hidden.text).not.toMatch(/Ollama/);
  expect(hidden.retry).toBe(true);
});

test("a plain upstream_unavailable gets the busy text and no command", () => {
  const m = errorMessage({ code: "upstream_unavailable", message: "503 overloaded" }, { showInternals: true });
  expect(m.text).toContain("Gemini is busy");
  expect(m.command).toBeUndefined();
});

test("unknown codes fall back to the server's message", () => {
  expect(errorMessage({ code: "weird", message: "it broke" }, { showInternals: true }).text).toBe("it broke");
});

test("forbidden explains the address and offers no retry", () => {
  const m = errorMessage({ code: "forbidden", message: "" }, { showInternals: true });
  expect(m.text).toBe("This page isn't allowed to talk to LORE. Open it from the address lore ui printed.");
  expect(m.retry).toBeUndefined();
});

test("the judgement note says it is an arguable case, not a statement from the book", () => {
  expect(UI.judgementNote).toBe("A judgement weighing the cited facts: an arguable case, not a statement from the book.");
});

test("the reading note says it is an interpretation, not a statement from the book", () => {
  expect(UI.readingNote).toBe("A reading of the cited facts, not a direct statement from the book.");
});
