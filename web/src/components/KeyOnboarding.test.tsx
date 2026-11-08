import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ONBOARD, PROVIDERS } from "../messages";
import { KeyOnboarding } from "./KeyOnboarding";

const GEMINI_KEY = "AIzaFAKEfakeFAKEfakeFAKEfakeFAKEfake123";
const CLAUDE_KEY = "sk-ant-api03-FAKEfakeFAKEfakeFAKEfakeFAKEfake-AAAA";

const providersBody = (provider: string, model: string | null, models: string[], extra: object = {}) => ({
  active: provider, daily_token_cap: null,
  providers: [{ provider, connected: true, active: true, last4: "AAAA", added_at: null, last_ok_at: null, last_error: null,
    model, fast_model: null, models, ...extra }] });

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

const radio = (name: string) => screen.getByRole("radio", { name: new RegExp(`^${name}`) });

// Step 1 → step 2 for one provider.
async function choose(name: string) {
  await userEvent.click(radio(name));
  await userEvent.click(screen.getByRole("button", { name: ONBOARD.continue }));
  await screen.findByRole("heading", { name: ONBOARD.keyTitle(name) });
}
async function paste(name: string, key: string) {
  await userEvent.type(screen.getByLabelText(PROVIDERS.fieldLabel(name)), key);
  await userEvent.click(screen.getByRole("button", { name: ONBOARD.check }));
}

test("step 1: three provider cards in a radio group, each with its pitch; Gemini is free to start", () => {
  render(<KeyOnboarding reason="missing" onDone={() => {}} />);
  expect(screen.getByRole("heading", { name: PROVIDERS.connectTitle })).toBeInTheDocument();
  expect(screen.getByText(ONBOARD.stepOf(1, 2))).toBeInTheDocument();
  expect(screen.getByRole("radiogroup", { name: ONBOARD.providersLabel })).toBeInTheDocument();
  expect(screen.getAllByRole("radio")).toHaveLength(3);
  expect(radio("Gemini")).toBeChecked(); // the free one is chosen to start with
  for (const id of ["gemini", "anthropic", "openai"]) expect(screen.getByText(ONBOARD.pitch[id])).toBeInTheDocument();
  expect(screen.getByText(ONBOARD.freeBadge)).toBeInTheDocument();
  // No key field and no prices until a provider is chosen.
  expect(screen.queryByLabelText(PROVIDERS.fieldLabel("Gemini"))).toBeNull();
  expect(screen.queryByText("Claude Opus 5.5")).toBeNull();
  expect(screen.getByText(ONBOARD.footer)).toBeInTheDocument();
});

test("the arrow keys move the choice and Enter opens that provider's key step", async () => {
  render(<KeyOnboarding reason="missing" onDone={() => {}} />);
  radio("Gemini").focus();
  await userEvent.keyboard("{ArrowRight}");
  expect(radio("Claude")).toBeChecked();
  expect(radio("Claude")).toHaveFocus();
  await userEvent.keyboard("{ArrowRight}");
  expect(radio("OpenAI")).toBeChecked();
  await userEvent.keyboard("{ArrowLeft}");
  expect(radio("Claude")).toBeChecked();
  await userEvent.keyboard("{Enter}"); // submits step 1
  expect(await screen.findByRole("heading", { name: ONBOARD.keyTitle("Claude") })).toHaveFocus();
  expect(screen.getByText(ONBOARD.stepOf(2, 2))).toBeInTheDocument();
});

test("step 2: one hidden paste field with show/hide, the console link in a new tab, and a way back", async () => {
  render(<KeyOnboarding reason="missing" onDone={() => {}} />);
  await choose("Claude");
  const field = screen.getByLabelText(PROVIDERS.fieldLabel("Claude"));
  expect(field).toHaveAttribute("type", "password");
  expect(field).toHaveAttribute("autocomplete", "off");
  expect(screen.getByRole("button", { name: ONBOARD.check })).toBeDisabled();
  await userEvent.click(screen.getByRole("button", { name: ONBOARD.showKey }));
  expect(field).toHaveAttribute("type", "text");
  await userEvent.click(screen.getByRole("button", { name: ONBOARD.hideKey }));
  expect(field).toHaveAttribute("type", "password");
  const link = screen.getByRole("link", { name: new RegExp(ONBOARD.getKeyAt.anthropic) });
  expect(link).toHaveAttribute("href", "https://console.anthropic.com/settings/keys");
  expect(link).toHaveAttribute("target", "_blank");
  expect(link).toHaveAttribute("rel", "noopener noreferrer");
  expect(screen.queryAllByLabelText(/API key$/)).toHaveLength(1); // the other providers' fields are not there
  await userEvent.click(screen.getByRole("button", { name: ONBOARD.changeProvider }));
  expect(await screen.findByRole("heading", { name: PROVIDERS.connectTitle })).toHaveFocus();
  expect(radio("Claude")).toBeChecked();
});

test.each([["Gemini", "https://aistudio.google.com/app/apikey"], ["OpenAI", "https://platform.openai.com/api-keys"]])(
  "%s's key step links to its own console", async (name, url) => {
    render(<KeyOnboarding reason="missing" onDone={() => {}} />);
    await choose(name);
    expect(screen.getByRole("link", { name: /Get a key at/ })).toHaveAttribute("href", url);
  });

test("prices and the tokens explanation stay behind See prices", async () => {
  render(<KeyOnboarding reason="missing" onDone={() => {}} />);
  await choose("Claude");
  const opus = screen.getByText("Claude Opus 5.5");
  expect(opus).not.toBeVisible();
  expect(screen.getByText(PROVIDERS.priceExplain)).not.toBeVisible();
  await userEvent.click(screen.getByText(ONBOARD.seePrices));
  expect(opus).toBeVisible();
  expect(screen.getByText("$4 in · $20 out per million tokens")).toBeVisible();
  expect(screen.getByText("$0.10 in · $0.50 out per million tokens")).toBeVisible();
  expect(screen.getByText(PROVIDERS.priceExplain)).toBeVisible();
  expect(screen.getByText(/Estimate, not a measurement/)).toBeVisible();
});

test("a Claude key that checks out shows the model picker with Sonnet 5.5 chosen, then Start reading", async () => {
  const models = ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"];
  const calls = serve({ status: 200, body: { present: true, last4: "AAAA", check: "ok", models, active: "anthropic" } },
    providersBody("anthropic", "claude-sonnet-5-5", models));
  const done = vi.fn();
  render(<KeyOnboarding reason="missing" onDone={done} />);
  await choose("Claude");
  await paste("Claude", CLAUDE_KEY);
  expect(calls[0]).toEqual({ url: "/api/me/key", method: "PUT", body: JSON.stringify({ provider: "anthropic", key: CLAUDE_KEY }) });
  expect(await screen.findByRole("status")).toHaveTextContent(ONBOARD.connected("Claude"));
  expect(await screen.findByLabelText(PROVIDERS.modelLabel)).toHaveValue("claude-sonnet-5-5");
  expect(screen.getByLabelText(PROVIDERS.fastModelLabel)).toBeInTheDocument();
  expect(screen.queryByLabelText(PROVIDERS.fieldLabel("Claude"))).toBeNull();
  expect(done).not.toHaveBeenCalled();
  await userEvent.selectOptions(screen.getByLabelText(PROVIDERS.modelLabel), "claude-haiku-5-5");
  expect(JSON.parse(calls.find((c) => c.url === "/api/me/provider")!.body!)).toEqual(
    { provider: "anthropic", model: "claude-haiku-5-5" });
  await userEvent.click(screen.getByRole("button", { name: PROVIDERS.startReading }));
  expect(done).toHaveBeenCalled();
});

test("a Gemini key that checks out offers the paid-plan switch, then Start reading", async () => {
  const calls = serve({ status: 200, body: { present: true, last4: "e123", check: "ok", last_error: null } },
    providersBody("gemini", null, [], { paid_tier: false }));
  const done = vi.fn();
  render(<KeyOnboarding reason="missing" onDone={done} />);
  await choose("Gemini");
  await paste("Gemini", GEMINI_KEY);
  expect(calls[0]).toEqual({ url: "/api/me/key", method: "PUT", body: JSON.stringify({ provider: "gemini", key: GEMINI_KEY }) });
  expect(await screen.findByRole("status")).toHaveTextContent(ONBOARD.connected("Gemini"));
  const sw = await screen.findByRole("switch", { name: PROVIDERS.geminiPaid });
  expect(sw).not.toBeChecked();
  await userEvent.click(sw);
  expect(JSON.parse(calls.find((c) => c.url === "/api/me/provider")!.body!)).toEqual({ gemini_paid: true });
  await userEvent.click(screen.getByRole("button", { name: PROVIDERS.startReading }));
  expect(done).toHaveBeenCalled();
});

test.each([["key_invalid", PROVIDERS.errors.key_invalid], ["key_format", PROVIDERS.errors.key_format],
  ["rate_limited", PROVIDERS.errors.rate_limited]])("%s shows a friendly message and stays", async (code, text) => {
  serve({ status: code === "rate_limited" ? 429 : 400, body: { error: { code, message: "server words" } } });
  const done = vi.fn();
  render(<KeyOnboarding reason="missing" onDone={done} />);
  await choose("OpenAI");
  await paste("OpenAI", "sk-proj-FAKEfakeFAKEfakeFAKEfakeFAKE");
  expect(await screen.findByRole("alert")).toHaveTextContent(text);
  expect(screen.queryByText("server words")).toBeNull();
  expect(done).not.toHaveBeenCalled();
  const field = screen.getByLabelText(PROVIDERS.fieldLabel("OpenAI"));
  expect(field).toHaveAttribute("aria-invalid", "true");
  expect(field).toHaveValue(code === "rate_limited" ? "sk-proj-FAKEfakeFAKEfakeFAKEfakeFAKE" : "");
});

test("a key saved without a check shows the warning before going on", async () => {
  serve({ status: 200, body: { present: true, last4: "e123", check: "unavailable", warning: "Saved, but not confirmed.", last_error: "unchecked" } },
    providersBody("gemini", null, []));
  const done = vi.fn();
  render(<KeyOnboarding reason="missing" onDone={done} />);
  await choose("Gemini");
  await paste("Gemini", GEMINI_KEY);
  expect(await screen.findByRole("status")).toHaveTextContent("Saved, but not confirmed.");
  expect(done).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: PROVIDERS.startReading }));
  expect(done).toHaveBeenCalled();
});

test("the replace variant says why and starts on the provider that refused the key", async () => {
  serve({ status: 200, body: {} }, { active: "anthropic", daily_token_cap: null, providers: [
    { provider: "anthropic", connected: true, active: true, last4: "AAAA", added_at: null, last_ok_at: null,
      last_error: "rejected", model: "claude-sonnet-5-5", fast_model: null, models: [] }] });
  render(<KeyOnboarding reason="invalid" onDone={() => {}} />);
  expect(screen.getByRole("heading", { name: PROVIDERS.replaceTitle })).toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent(PROVIDERS.rejected);
  await waitFor(() => expect(radio("Claude")).toBeChecked());
});
