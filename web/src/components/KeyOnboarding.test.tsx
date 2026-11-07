import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PROVIDERS } from "../messages";
import { KeyOnboarding } from "./KeyOnboarding";

const GEMINI_KEY = "AIzaFAKEfakeFAKEfakeFAKEfakeFAKEfake123";
const CLAUDE_KEY = "sk-ant-api03-FAKEfakeFAKEfakeFAKEfakeFAKEfake-AAAA";

const providersBody = (provider: string, model: string | null, models: string[]) => ({
  active: provider, daily_token_cap: null,
  providers: [{ provider, connected: true, active: true, last4: "AAAA", added_at: null, last_ok_at: null, last_error: null,
    model, fast_model: null, models }] });

function serve(put: { status: number; body: object }, providers?: object) {
  const calls: { url: string; method: string; body?: string }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit = {}) => {
    calls.push({ url, method: init.method ?? "GET", body: init.body as string | undefined });
    if (url === "/api/me/providers") return new Response(JSON.stringify(providers ?? {}));
    if (url === "/api/me/provider") return new Response(JSON.stringify(providers ?? {}));
    return new Response(JSON.stringify(put.body), { status: put.status });
  }));
  return calls;
}
afterEach(() => vi.unstubAllGlobals());

const card = (name: string) => screen.getByRole("region", { name });

async function paste(name: string, key: string) {
  await userEvent.type(within(card(name)).getByLabelText(PROVIDERS.fieldLabel(name)), key);
  await userEvent.click(within(card(name)).getByRole("button", { name: PROVIDERS.save }));
}

test("three cards, each with its own key link and cost note; fields hide what is typed", () => {
  render(<KeyOnboarding reason="missing" onDone={() => {}} />);
  expect(screen.getByRole("heading", { name: PROVIDERS.connectTitle })).toBeInTheDocument();
  for (const [name, url] of [["Gemini", "https://aistudio.google.com/app/apikey"],
    ["Claude", "https://console.anthropic.com/settings/keys"], ["OpenAI", "https://platform.openai.com/api-keys"]]) {
    const field = within(card(name)).getByLabelText(PROVIDERS.fieldLabel(name));
    expect(field).toHaveAttribute("type", "password");
    expect(field).toHaveAttribute("autocomplete", "off");
    expect(within(card(name)).getByRole("link")).toHaveAttribute("href", url);
    expect(within(card(name)).getByRole("button", { name: PROVIDERS.save })).toBeDisabled();
  }
  expect(within(card("Gemini")).getByText(/about 1,000 requests a day/)).toBeInTheDocument();
  expect(within(card("Claude")).getByText(PROVIDERS.cost.anthropic)).toBeInTheDocument();
  expect(within(card("OpenAI")).getByText(PROVIDERS.cost.openai)).toBeInTheDocument();
  // Claude's price table and the marked estimate.
  expect(within(card("Claude")).getByText("Claude Opus 5.5")).toBeInTheDocument();
  expect(within(card("Claude")).getByText("$4 / $20")).toBeInTheDocument();
  expect(within(card("Claude")).getByText("$0.10 / $0.50")).toBeInTheDocument();
  expect(within(card("Claude")).getByText(/Estimate, not a measurement/)).toBeInTheDocument();
});

test("a Gemini key the server accepts finishes onboarding at once", async () => {
  const calls = serve({ status: 200, body: { present: true, last4: "e123", check: "ok", last_error: null } });
  const done = vi.fn();
  render(<KeyOnboarding reason="missing" onDone={done} />);
  await paste("Gemini", GEMINI_KEY);
  expect(done).toHaveBeenCalled();
  expect(calls[0]).toEqual({ url: "/api/me/key", method: "PUT", body: JSON.stringify({ provider: "gemini", key: GEMINI_KEY }) });
});

test("a Claude key shows the model picker with Sonnet 5.5 chosen, then continues", async () => {
  const models = ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"];
  const calls = serve({ status: 200, body: { present: true, last4: "AAAA", check: "ok", models, active: "anthropic" } },
    providersBody("anthropic", "claude-sonnet-5-5", models));
  const done = vi.fn();
  render(<KeyOnboarding reason="missing" onDone={done} />);
  await paste("Claude", CLAUDE_KEY);
  expect(await within(card("Claude")).findByLabelText(PROVIDERS.modelLabel)).toHaveValue("claude-sonnet-5-5");
  expect(done).not.toHaveBeenCalled();
  await userEvent.selectOptions(within(card("Claude")).getByLabelText(PROVIDERS.modelLabel), "claude-haiku-5-5");
  expect(JSON.parse(calls.find((c) => c.url === "/api/me/provider")!.body!)).toEqual(
    { provider: "anthropic", model: "claude-haiku-5-5" });
  await userEvent.click(within(card("Claude")).getByRole("button", { name: PROVIDERS.startReading }));
  expect(done).toHaveBeenCalled();
});

test.each([["key_invalid", PROVIDERS.errors.key_invalid], ["key_format", PROVIDERS.errors.key_format],
  ["rate_limited", PROVIDERS.errors.rate_limited]])("%s shows a friendly message and stays", async (code, text) => {
  serve({ status: code === "rate_limited" ? 429 : 400, body: { error: { code, message: "server words" } } });
  const done = vi.fn();
  render(<KeyOnboarding reason="missing" onDone={done} />);
  await paste("OpenAI", "sk-proj-FAKEfakeFAKEfakeFAKEfakeFAKE");
  expect(await within(card("OpenAI")).findByRole("alert")).toHaveTextContent(text);
  expect(done).not.toHaveBeenCalled();
  if (code !== "rate_limited") expect(within(card("OpenAI")).getByLabelText(PROVIDERS.fieldLabel("OpenAI"))).toHaveValue("");
});

test("a Gemini key saved without a check shows the warning before going on", async () => {
  serve({ status: 200, body: { present: true, last4: "e123", check: "unavailable", warning: "Saved, but not confirmed.", last_error: "unchecked" } },
    providersBody("gemini", null, []));
  const done = vi.fn();
  render(<KeyOnboarding reason="missing" onDone={done} />);
  await paste("Gemini", GEMINI_KEY);
  expect(await screen.findByRole("status")).toHaveTextContent("Saved, but not confirmed.");
  expect(done).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: PROVIDERS.startReading }));
  expect(done).toHaveBeenCalled();
});

test("the replace variant says why", () => {
  render(<KeyOnboarding reason="invalid" onDone={() => {}} />);
  expect(screen.getByRole("heading", { name: PROVIDERS.replaceTitle })).toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent(PROVIDERS.rejected);
});
