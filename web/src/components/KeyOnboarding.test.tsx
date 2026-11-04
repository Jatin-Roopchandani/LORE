import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { KEYS } from "../messages";
import { KeyOnboarding } from "./KeyOnboarding";

const KEY = "AIzaFAKEfakeFAKEfakeFAKEfakeFAKEfake123";

function serve(status: number, body: object) {
  const calls: { method: string; body?: string }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (_url: string, init: RequestInit = {}) => {
    calls.push({ method: init.method ?? "GET", body: init.body as string | undefined });
    return new Response(JSON.stringify(body), { status });
  }));
  return calls;
}
afterEach(() => vi.unstubAllGlobals());

async function paste(key = KEY) {
  await userEvent.type(screen.getByLabelText(KEYS.fieldLabel), key);
  await userEvent.click(screen.getByRole("button", { name: KEYS.save }));
}

test("the key field hides what is typed and the button waits for a key", () => {
  render(<KeyOnboarding reason="missing" onDone={() => {}} />);
  expect(screen.getByLabelText(KEYS.fieldLabel)).toHaveAttribute("type", "password");
  expect(screen.getByLabelText(KEYS.fieldLabel)).toHaveAttribute("autocomplete", "off");
  expect(screen.getByRole("button", { name: KEYS.save })).toBeDisabled();
});

test("a key Gemini accepts finishes onboarding", async () => {
  const calls = serve(200, { present: true, last4: "e123", check: "ok", last_error: null });
  const done = vi.fn();
  render(<KeyOnboarding reason="missing" onDone={done} />);
  await paste();
  expect(done).toHaveBeenCalled();
  expect(calls[0]).toEqual({ method: "PUT", body: JSON.stringify({ key: KEY }) });
});

test.each([["key_invalid", KEYS.errors.key_invalid], ["key_format", KEYS.errors.key_format],
  ["rate_limited", KEYS.errors.rate_limited]])("%s shows a friendly message and stays", async (code, text) => {
  serve(code === "rate_limited" ? 429 : 400, { error: { code, message: "server words" } });
  const done = vi.fn();
  render(<KeyOnboarding reason="missing" onDone={done} />);
  await paste();
  expect(await screen.findByRole("alert")).toHaveTextContent(text);
  expect(done).not.toHaveBeenCalled();
  if (code !== "rate_limited") expect(screen.getByLabelText(KEYS.fieldLabel)).toHaveValue("");
});

test("a key saved without a check shows the warning before going on", async () => {
  serve(200, { present: true, last4: "e123", check: "unavailable", warning: "Saved, but not confirmed.", last_error: "unchecked" });
  const done = vi.fn();
  render(<KeyOnboarding reason="missing" onDone={done} />);
  await paste();
  expect(await screen.findByRole("status")).toHaveTextContent("Saved, but not confirmed.");
  expect(done).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: KEYS.continue }));
  expect(done).toHaveBeenCalled();
});
