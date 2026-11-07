import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { nav } from "../api";
import { AUTH, PROVIDERS, USAGE } from "../messages";
import { SettingsMenu } from "./SettingsMenu";

function serve(me: { role: string }) {
  const calls: { url: string; method: string }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit = {}) => {
    calls.push({ url, method: init.method ?? "GET" });
    if (url === "/api/me") return new Response(JSON.stringify({ user: { id: "u", email: "me@example.org", name: null, ...me } }));
    return new Response(JSON.stringify({ ok: true }));
  }));
  return calls;
}
const markMulti = () => { document.head.innerHTML = '<meta name="lore-multi" content="1">'; };
afterEach(() => { vi.unstubAllGlobals(); document.head.innerHTML = ""; });

test("in multi-user mode the menu shows the account and signs out", async () => {
  markMulti();
  const calls = serve({ role: "user" });
  const go = vi.spyOn(nav, "to").mockImplementation(() => {});
  render(<SettingsMenu showInternals={false} />);
  await userEvent.click(screen.getByRole("button", { name: "Settings" }));
  expect(await screen.findByText("me@example.org")).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: AUTH.admin })).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: AUTH.signOut }));
  expect(calls.some((c) => c.url === "/auth/logout" && c.method === "POST")).toBe(true);
  expect(go).toHaveBeenCalledWith("/");
});

test("an admin also gets a link to the admin page", async () => {
  markMulti();
  serve({ role: "admin" });
  render(<SettingsMenu showInternals={false} />);
  await userEvent.click(screen.getByRole("button", { name: "Settings" }));
  expect(await screen.findByRole("link", { name: AUTH.admin })).toHaveAttribute("href", "/admin");
});

test("local mode shows no account section and makes no auth call", async () => {
  const calls = serve({ role: "user" });
  render(<SettingsMenu showInternals={false} />);
  await userEvent.click(screen.getByRole("button", { name: "Settings" }));
  expect(screen.queryByText(AUTH.signOut)).toBeNull();
  expect(calls).toEqual([]);
});

// --- AI providers and usage in the account menu (ProviderSettings, UsagePanel) ---

type Info = { provider: string; connected: boolean; active: boolean; last4: string | null; added_at: null; last_ok_at: null;
  last_error: string | null; model: string | null; fast_model: string | null; models: string[] };
const row = (provider: string, over: Partial<Info> = {}): Info => ({ provider, connected: false, active: false, last4: null,
  added_at: null, last_ok_at: null, last_error: null, model: null, fast_model: null, models: [], ...over });
const usageBody = { day: "2026-11-07", requests: 12, limit: 1000, models: {}, provider: "anthropic",
  tokens: { day: "2026-11-07", month: "2026-11", daily_token_cap: null, cap_reached: false, requests_limit: 1000,
    today: { requests: 3, input_tokens: 1000, output_tokens: 500, estimated_cost_usd: 0.007, models: {} },
    month_total: { requests: 9, input_tokens: 4000, output_tokens: 1000, estimated_cost_usd: 0.018, models: {} } } };

function serveProviders(providers: Info[]) {
  const calls: { url: string; method: string; body?: string }[] = [];
  let state = { active: providers.find((p) => p.active)?.provider ?? "gemini", providers, daily_token_cap: null };
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = init.method ?? "GET";
    calls.push({ url, method, body: init.body as string | undefined });
    if (url === "/api/me") return new Response(JSON.stringify({ user: { id: "u", email: "me@example.org", name: null, role: "user" } }));
    if (url === "/api/me/usage") return new Response(JSON.stringify(usageBody));
    if (url === "/api/me/providers") return new Response(JSON.stringify(state));
    if (url === "/api/me/provider" && method === "PUT") {
      const body = JSON.parse(init.body as string);
      state = { ...state, active: body.provider, providers: state.providers.map((p) => p.provider === body.provider
        ? { ...p, active: true, model: body.model ?? p.model } : { ...p, active: false }) };
      return new Response(JSON.stringify(state));
    }
    if (url.startsWith("/api/me/key") && method === "DELETE") return new Response(JSON.stringify({ deleted: true }));
    if (url === "/api/me/token-cap") return new Response(JSON.stringify({ ...usageBody.tokens, daily_token_cap: 5000 }));
    return new Response(JSON.stringify({ ok: true }));
  }));
  return calls;
}

const claude = row("anthropic", { connected: true, active: true, last4: "AAAA", model: "claude-sonnet-5-5",
  models: ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"] });
const gemini = row("gemini", { connected: true, last4: "1234" });

async function open() {
  markMulti();
  render(<SettingsMenu showInternals={false} />);
  await userEvent.click(screen.getByRole("button", { name: "Settings" }));
}

test("connected providers show their last four, which is in use, and the model; never a key", async () => {
  serveProviders([gemini, claude, row("openai")]);
  await open();
  expect(await screen.findByText(PROVIDERS.endingIn("AAAA"))).toBeInTheDocument();
  expect(screen.getByText(PROVIDERS.endingIn("1234"))).toBeInTheDocument();
  expect(screen.getByText(PROVIDERS.active)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: PROVIDERS.use("Gemini") })).toBeInTheDocument();
  expect(screen.getByLabelText(PROVIDERS.modelLabel)).toHaveValue("claude-sonnet-5-5");
  expect(screen.getByRole("button", { name: "OpenAI" })).toBeInTheDocument();  // add another
});

test("switching the active provider and the model", async () => {
  const calls = serveProviders([gemini, claude, row("openai")]);
  await open();
  await userEvent.selectOptions(await screen.findByLabelText(PROVIDERS.modelLabel), "claude-opus-5-5");
  const put = calls.find((c) => c.url === "/api/me/provider");
  expect(JSON.parse(put!.body!)).toEqual({ provider: "anthropic", model: "claude-opus-5-5" });
  await userEvent.click(screen.getByRole("button", { name: PROVIDERS.use("Gemini") }));
  expect(JSON.parse(calls.filter((c) => c.url === "/api/me/provider")[1].body!)).toEqual({ provider: "gemini" });
  expect(await screen.findByRole("button", { name: PROVIDERS.use("Claude") })).toBeInTheDocument();
});

test("Remove asks first, then deletes that provider's key", async () => {
  const calls = serveProviders([claude]);
  await open();
  await userEvent.click(await screen.findByRole("button", { name: PROVIDERS.remove }));
  expect(calls.some((c) => c.method === "DELETE")).toBe(false);
  expect(screen.getByText(PROVIDERS.removeConfirm("Claude"))).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: PROVIDERS.removeYes }));
  expect(calls.some((c) => c.url === "/api/me/key?provider=anthropic" && c.method === "DELETE")).toBe(true);
});

test("a refused key is flagged", async () => {
  serveProviders([{ ...claude, last_error: "rejected" }]);
  await open();
  expect(await screen.findByRole("alert")).toHaveTextContent(PROVIDERS.needsReplacing);
});

test("usage shows tokens and an estimated cost, and the cap can be set", async () => {
  const calls = serveProviders([claude]);
  await open();
  expect(await screen.findByText(/Today: 1,500 tokens/)).toBeInTheDocument();
  expect(screen.getByText(/This month: 5,000 tokens/)).toBeInTheDocument();
  expect(screen.getByText(/about \$0\.01 \(estimate\)/)).toBeInTheDocument();
  await userEvent.type(screen.getByLabelText(USAGE.capLabel), "5000");
  await userEvent.click(screen.getByRole("button", { name: USAGE.capSave }));
  const put = calls.find((c) => c.url === "/api/me/token-cap");
  expect(JSON.parse(put!.body!)).toEqual({ cap: 5000 });
});
