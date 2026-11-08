import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { nav } from "../api";
import { ACCOUNT_DELETE, AUTH, PROVIDERS, SETTINGS, UI, USAGE } from "../messages";
import { openSettings, TAB_KEY } from "../settings";
import { applyTheme } from "../theme";
import { THEMES } from "../themes";
import { SettingsDialog, type SettingsDialogProps } from "./SettingsDialog";

// ---- a fake server ------------------------------------------------------------------------------------------------

type Info = { provider: string; connected: boolean; active: boolean; last4: string | null; added_at: null; last_ok_at: null;
  last_error: string | null; model: string | null; fast_model: string | null; models: string[]; paid_tier?: boolean };
const row = (provider: string, over: Partial<Info> = {}): Info => ({ provider, connected: false, active: false, last4: null,
  added_at: null, last_ok_at: null, last_error: null, model: null, fast_model: null, models: [], ...over });
const claude = row("anthropic", { connected: true, active: true, last4: "AAAA", model: "claude-sonnet-5-5",
  models: ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"] });
const gemini = row("gemini", { connected: true, last4: "1234" });
const usageBody = { day: "2026-11-07", requests: 12, limit: 1000, models: {}, provider: "anthropic",
  tokens: { day: "2026-11-07", month: "2026-11", daily_token_cap: null, cap_reached: false, requests_limit: 1000,
    today: { requests: 3, input_tokens: 1000, output_tokens: 500, estimated_cost_usd: 0.007, models: {} },
    month_total: { requests: 9, input_tokens: 4000, output_tokens: 1000, estimated_cost_usd: 0.018, models: {} } } };
const now = Date.now();
const sessionsBody = { sessions: [
  { id: "aaaaaaaaaaaaaaaa", device: "Firefox on Linux", created_at: new Date(now - 864e5).toISOString(), last_active: new Date(now - 6e4 * 5).toISOString(), current: true },
  { id: "bbbbbbbbbbbbbbbb", device: "Safari on iPhone", created_at: new Date(now - 3 * 864e5).toISOString(), last_active: new Date(now - 2 * 864e5).toISOString(), current: false },
] };
const mcpBody = { stdio_command: "claude mcp add lore -- /opt/lore/bin/lore mcp", http_command: "LORE_DATA_DIR=/home/r/LORE/data /opt/lore/bin/lore mcp --http --port 8765",
  http_url: "http://127.0.0.1:8765/mcp", http_running: false, lore: "/opt/lore/bin/lore", env: { LORE_DATA_DIR: "/home/r/LORE/data" },
  claude_desktop: '{\n  "mcpServers": {\n    "lore": {\n      "command": "/opt/lore/bin/lore"\n    }\n  }\n}',
  claude_code: "claude mcp add --scope user lore -e LORE_DATA_DIR=/home/r/LORE/data -- /opt/lore/bin/lore mcp",
  claude_code_http: "claude mcp add --scope user --transport http lore http://127.0.0.1:8765/mcp",
  library: { data_dir: "/home/r/LORE/data", central_db: "/home/r/LORE/data/central.db" } };
const hostedBody = { hosted: true, base_url: "https://lore.example.org", mcp_url: "https://lore.example.org/mcp", max_tokens: 2, oauth: false };
type Tok = { id: string; name: string; prefix: string; created_at: string; last_used_at: string | null };
let mcpTokens: Tok[] = [];
let oauthOn = false;
let mcpApps: { id: string; client_name: string; created_at: string; last_used_at: string | null }[] = [];
beforeEach(() => {
  mcpTokens = [{ id: "t0", name: "Old laptop", prefix: "lore_ZzZzZzZ", created_at: new Date(now - 864e5).toISOString(), last_used_at: null }];
  oauthOn = false;
  mcpApps = [{ id: "g1", client_name: "Claude", created_at: new Date(now - 864e5).toISOString(), last_used_at: new Date(now - 6e4 * 10).toISOString() }];
});
const checkBody = { ok: true, lore_found: true, problem: null, library_found: true,
  tools: [{ name: "lore_search", summary: "Answer a question from the knowledge graph." }, { name: "entity", summary: "One entity's card." }] };
const quotaBody = { provider: "gemini", resets_at: "2026-11-08T08:00:00Z",
  models: [{ model: "gemini-flash", count: 40, limit: 500, remaining: 460 }, { model: "gemini-lite", count: 0, limit: 500, remaining: 500 }] };

type Call = { url: string; method: string; body?: string };
function serve({ role = "user", providers = [gemini, claude, row("openai")] as Info[], name = "Ada" as string | null,
  usage = usageBody as Record<string, unknown>, hint = false, quota = quotaBody as Record<string, unknown> } = {}) {
  const calls: Call[] = [];
  let state = { active: providers.find((p) => p.active)?.provider ?? "gemini", providers, daily_token_cap: null,
    gemini_paid: providers.some((p) => p.provider === "gemini" && p.paid_tier), gemini_paid_hint: hint };
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = init.method ?? "GET";
    calls.push({ url, method, body: init.body as string | undefined });
    const json = (b: unknown, status = 200) => new Response(JSON.stringify(b), { status });
    if (url === "/api/me") return json({ user: { id: "u", email: "me@example.org", name, role },
      identities: [{ provider: "email", created_at: new Date(now - 10 * 864e5).toISOString() }, { provider: "github", created_at: new Date(now - 864e5).toISOString() }] });
    if (url === "/auth/providers") return json({ providers: ["github", "google"], email: true, invite_only: true });
    if (url === "/api/me/sessions") return json(sessionsBody);
    if (url.startsWith("/api/me/sessions/") && method === "DELETE") return json({ revoked: 1 });
    if (url === "/api/me/sessions/sign-out-others") return json({ revoked: 1 });
    if (url === "/api/me/usage") return json(usage);
    if (url === "/api/me/providers") return json(state);
    if (url === "/api/me/provider" && method === "PUT") {
      const body = JSON.parse(init.body as string);
      if (body.provider === undefined && typeof body.gemini_paid === "boolean") {
        state = { ...state, gemini_paid: body.gemini_paid, providers: state.providers.map((p) => p.provider === "gemini"
          ? { ...p, paid_tier: body.gemini_paid } : p) };
        return json(state);
      }
      state = { ...state, active: body.provider, providers: state.providers.map((p) => p.provider === body.provider
        ? { ...p, active: true, model: body.model ?? p.model } : { ...p, active: false }) };
      return json(state);
    }
    if (url.startsWith("/api/me/key") && method === "DELETE") return json({ deleted: true });
    if (url === "/api/me/token-cap") return json({ ...usageBody.tokens, daily_token_cap: 5000 });
    if (url === "/api/me/export") return new Response(new Blob(["PK"]), { headers: { "content-disposition": 'attachment; filename="lore-data-2026-11-07.zip"' } });
    if (url === "/api/me/delete") return json({ deleted: true });
    const multiDoc = document.querySelector('meta[name="lore-multi"]') !== null;
    if (url === "/api/mcp") return json(multiDoc ? { ...hostedBody, oauth: oauthOn } : mcpBody);
    if (url === "/api/me/mcp-connections") return json({ connections: mcpApps });
    if (url.startsWith("/api/me/mcp-connections/") && method === "DELETE") {
      mcpApps = mcpApps.filter((a) => !url.endsWith(`/${a.id}`));
      return json({ revoked: true });
    }
    if (url === "/api/me/mcp-tokens" && method === "GET") return json({ tokens: mcpTokens, max_tokens: 2 });
    if (url === "/api/me/mcp-tokens" && method === "POST") {
      const made = { id: `t${mcpTokens.length + 1}`, name: JSON.parse(init.body as string).name, prefix: "lore_AbCdEfG",
        created_at: new Date(now).toISOString(), last_used_at: null };
      mcpTokens = [...mcpTokens, made];
      return json({ ...made, token: "lore_AbCdEfGsecretsecretsecret" }, 201);
    }
    if (url.startsWith("/api/me/mcp-tokens/") && method === "DELETE") {
      mcpTokens = mcpTokens.filter((t) => !url.endsWith(`/${t.id}`));
      return json({ revoked: true });
    }
    if (url === "/api/mcp/check") return json(checkBody);
    if (url === "/api/quota") return json(quota);
    return json({ ok: true });
  }));
  return calls;
}

const markMulti = () => { document.head.innerHTML = '<meta name="lore-multi" content="1">'; };
const clearHash = () => history.replaceState(null, "", window.location.pathname);
beforeEach(() => { clearHash(); sessionStorage.clear(); localStorage.clear(); applyTheme("system"); });
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); document.head.innerHTML = ""; clearHash();
  document.documentElement.removeAttribute("data-motion"); });

// The ⚙ button as the top bar has it, a stand-in for the reading-position button, and the popup.
function setup(props: SettingsDialogProps = {}) {
  render(<><button id="settings-button" type="button" onClick={() => openSettings()}>⚙</button>
    <header><button type="button" className="pos-btn">Position</button></header>
    <SettingsDialog {...props} /></>);
  return screen.getByRole("button", { name: "⚙" });
}
async function openAt(tab?: Parameters<typeof openSettings>[0], props: SettingsDialogProps = {}) {
  setup(props);
  act(() => openSettings(tab));
  return screen.findByRole("dialog", { name: SETTINGS.title });
}
const tabNames = () => screen.getAllByRole("tab").map((t) => t.textContent);
const panel = () => screen.getByRole("tabpanel");

// ---- opening and closing ------------------------------------------------------------------------------------------

test("the ⚙ button opens a modal dialog; Esc closes it and gives focus back", async () => {
  serve();
  const gear = setup();
  expect(screen.queryByRole("dialog")).toBeNull();
  await userEvent.click(gear);
  const dialog = screen.getByRole("dialog", { name: SETTINGS.title });
  expect(dialog).toHaveAttribute("aria-modal", "true");
  expect(window.location.hash).toBe("#settings");
  expect(screen.getByRole("tab", { name: SETTINGS.tabs.usage })).toHaveFocus();
  await userEvent.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(window.location.hash).toBe("");
  expect(gear).toHaveFocus();
});

test("the close button and a click outside close it too", async () => {
  serve();
  const gear = setup();
  await userEvent.click(gear);
  await userEvent.click(screen.getByRole("button", { name: SETTINGS.close }));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(gear).toHaveFocus();
  await userEvent.click(gear);
  await userEvent.pointer({ keys: "[MouseLeft]", target: document.querySelector(".settings-overlay")! });
  expect(screen.queryByRole("dialog")).toBeNull();
});

test("Tab stays inside the dialog: past the last control to the first, and back", async () => {
  serve();
  await openAt("appearance");
  const close = screen.getByRole("button", { name: SETTINGS.close });
  const last = screen.getByRole("radio", { name: SETTINGS.motionOptions.reduce });
  close.focus();
  await userEvent.tab({ shift: true });
  expect(screen.getByRole("dialog").contains(document.activeElement)).toBe(true);
  expect(close).not.toHaveFocus();
  last.focus();
  await userEvent.tab();
  expect(close).toHaveFocus();
  await userEvent.tab({ shift: true });
  expect(last).toHaveFocus();
});

// ---- tabs per mode ------------------------------------------------------------------------------------------------

test("local mode: Usage, Appearance, Reading, MCP connector and Your data; no account tabs", async () => {
  const calls = serve();
  await openAt();
  expect(tabNames()).toEqual(["Usage", "Appearance", "Reading", "MCP connector", "Your data"]);
  expect(calls.some((c) => c.url === "/api/me")).toBe(false);
});

test("multi-user mode: Account and AI providers first; Admin only for admins", async () => {
  markMulti();
  serve();
  await openAt();
  await waitFor(() => expect(tabNames()).toEqual(["Account", "AI providers", "Usage", "Appearance", "Reading", "MCP connector", "Your data"]));
  expect(screen.getByRole("tab", { name: "Account" })).toHaveAttribute("aria-selected", "true");
});

test("an admin also gets the Admin tab, which links to /admin", async () => {
  markMulti();
  serve({ role: "admin" });
  await openAt("admin");
  await waitFor(() => expect(tabNames()).toContain("Admin"));
  expect(screen.getByRole("tab", { name: "Admin" })).toHaveAttribute("aria-selected", "true");
  expect(within(panel()).getByRole("link", { name: SETTINGS.adminLink })).toHaveAttribute("href", "/admin");
});

// ---- deep links and the remembered tab ----------------------------------------------------------------------------

test("#settings/<tab> opens that tab, and a change of hash switches tabs", async () => {
  markMulti();
  serve();
  history.replaceState(null, "", "#settings/providers");
  setup();
  expect(await screen.findByRole("tab", { name: "AI providers" })).toHaveAttribute("aria-selected", "true");
  await act(async () => { history.replaceState(null, "", "#settings/mcp"); window.dispatchEvent(new HashChangeEvent("hashchange")); });
  expect(screen.getByRole("tab", { name: "MCP connector" })).toHaveAttribute("aria-selected", "true");
  expect(within(panel()).getByText(SETTINGS.mcpWhatHosted)).toBeInTheDocument();
});

test("a link to a tab that doesn't apply here opens the first tab instead", async () => {
  serve();
  history.replaceState(null, "", "#settings/account");
  setup();
  expect(await screen.findByRole("tab", { name: "Usage" })).toHaveAttribute("aria-selected", "true");
});

test("the last tab is remembered for the session", async () => {
  serve();
  const gear = setup();
  await userEvent.click(gear);
  await userEvent.click(screen.getByRole("tab", { name: "Reading" }));
  expect(window.location.hash).toBe("#settings/reading");
  await userEvent.keyboard("{Escape}");
  expect(sessionStorage.getItem(TAB_KEY)).toBe("reading");
  await userEvent.click(gear);
  expect(screen.getByRole("tab", { name: "Reading" })).toHaveAttribute("aria-selected", "true");
});

test("arrow keys move between tabs", async () => {
  serve();
  await openAt("usage");
  await userEvent.keyboard("{ArrowDown}");
  expect(screen.getByRole("tab", { name: "Appearance" })).toHaveAttribute("aria-selected", "true");
  await waitFor(() => expect(screen.getByRole("tab", { name: "Appearance" })).toHaveFocus());
  await userEvent.keyboard("{End}");
  expect(screen.getByRole("tab", { name: "Your data" })).toHaveAttribute("aria-selected", "true");
  await waitFor(() => expect(screen.getByRole("tab", { name: "Your data" })).toHaveFocus());
  await userEvent.keyboard("{ArrowDown}");
  expect(screen.getByRole("tab", { name: "Usage" })).toHaveAttribute("aria-selected", "true");
});

// ---- Account ------------------------------------------------------------------------------------------------------

test("Account: name and email, linked sign-in methods, and Connect for the others", async () => {
  markMulti();
  serve();
  await openAt("account");
  expect(await within(panel()).findByText("me@example.org")).toBeInTheDocument();
  const p = panel();
  expect(within(p).getByText("Ada")).toBeInTheDocument();
  expect(within(p).getByText("Email link")).toBeInTheDocument();
  expect(within(p).getByText("GitHub")).toBeInTheDocument();
  expect(await within(p).findByRole("link", { name: SETTINGS.connect("Google") })).toHaveAttribute("href", "/auth/google/start");
  expect(within(p).queryByRole("link", { name: SETTINGS.connect("GitHub") })).toBeNull();
});

test("Account: signed-in devices, signing one out, and all the others", async () => {
  markMulti();
  const calls = serve();
  await openAt("account");
  expect(await within(panel()).findByText("Firefox on Linux")).toBeInTheDocument();
  const p = panel();
  expect(within(p).getByText(SETTINGS.thisDevice)).toBeInTheDocument();
  expect(within(p).getByText(SETTINGS.lastActive("5 minutes ago"))).toBeInTheDocument();
  expect(within(p).queryByRole("button", { name: SETTINGS.revokeLabel("Firefox on Linux") })).toBeNull();  // not this one
  await userEvent.click(within(p).getByRole("button", { name: SETTINGS.revokeLabel("Safari on iPhone") }));
  expect(calls.some((c) => c.url === "/api/me/sessions/bbbbbbbbbbbbbbbb" && c.method === "DELETE")).toBe(true);
  await userEvent.click(within(p).getByRole("button", { name: SETTINGS.revokeOthers }));
  expect(calls.some((c) => c.url === "/api/me/sessions/sign-out-others" && c.method === "POST")).toBe(true);
  expect(await within(p).findByText(SETTINGS.revokedOthers(1))).toBeInTheDocument();
});

test("Account: Sign out", async () => {
  markMulti();
  const calls = serve();
  const go = vi.spyOn(nav, "to").mockImplementation(() => {});
  await openAt("account");
  await userEvent.click(await screen.findByRole("button", { name: AUTH.signOut }));
  expect(calls.some((c) => c.url === "/auth/logout" && c.method === "POST")).toBe(true);
  expect(go).toHaveBeenCalledWith("/");
});

// ---- AI providers (ProviderSettings) ------------------------------------------------------------------------------

test("AI providers: last four, which is in use, the model; never a key", async () => {
  markMulti();
  serve();
  await openAt("providers");
  expect(await screen.findByText(PROVIDERS.endingIn("AAAA"))).toBeInTheDocument();
  expect(screen.getByText(PROVIDERS.endingIn("1234"))).toBeInTheDocument();
  expect(screen.getByText(PROVIDERS.active)).toBeInTheDocument();
  expect(screen.getByLabelText(PROVIDERS.modelLabel)).toHaveValue("claude-sonnet-5-5");
  expect(screen.getByRole("button", { name: "OpenAI" })).toBeInTheDocument();  // add another
});

test("AI providers: Claude's prices say in and out per million tokens, with what that means", async () => {
  markMulti();
  serve();
  await openAt("providers");
  const picker = await screen.findByLabelText(PROVIDERS.modelLabel);
  expect(within(picker).getByRole("option", { name: "Claude Sonnet 5.5 ($2 in · $10 out per million tokens)" })).toBeInTheDocument();
  expect(screen.getByText(PROVIDERS.priceExplain)).toBeInTheDocument();
  expect(screen.queryByText(/\$2 \/ \$10/)).toBeNull();
});

test("AI providers: switching the active provider and the model", async () => {
  markMulti();
  const calls = serve();
  await openAt("providers");
  await userEvent.selectOptions(await screen.findByLabelText(PROVIDERS.modelLabel), "claude-opus-5-5");
  expect(JSON.parse(calls.find((c) => c.url === "/api/me/provider")!.body!)).toEqual({ provider: "anthropic", model: "claude-opus-5-5" });
  await userEvent.click(screen.getByRole("button", { name: PROVIDERS.use("Gemini") }));
  expect(JSON.parse(calls.filter((c) => c.url === "/api/me/provider")[1].body!)).toEqual({ provider: "gemini" });
  expect(await screen.findByRole("button", { name: PROVIDERS.use("Claude") })).toBeInTheDocument();
});

test("AI providers: Remove asks first, then deletes that provider's key; a refused key is flagged", async () => {
  markMulti();
  const calls = serve({ providers: [{ ...claude, last_error: "rejected" }] });
  await openAt("providers");
  expect(await screen.findByRole("alert")).toHaveTextContent(PROVIDERS.needsReplacing);
  await userEvent.click(screen.getByRole("button", { name: PROVIDERS.remove }));
  expect(calls.some((c) => c.method === "DELETE")).toBe(false);
  await userEvent.click(screen.getByRole("button", { name: PROVIDERS.removeYes }));
  expect(calls.some((c) => c.url === "/api/me/key?provider=anthropic" && c.method === "DELETE")).toBe(true);
});

test("AI providers: the paid Gemini plan switch is off by default and turns on through the API", async () => {
  markMulti();
  const calls = serve();
  await openAt("providers");
  const sw = await screen.findByRole("switch", { name: PROVIDERS.geminiPaid });
  expect(sw).not.toBeChecked();
  expect(screen.getByText(PROVIDERS.geminiPaidHelp)).toBeInTheDocument();
  expect(screen.getByText(PROVIDERS.cost.gemini)).toBeInTheDocument();
  await userEvent.click(sw);
  expect(JSON.parse(calls.find((c) => c.url === "/api/me/provider")!.body!)).toEqual({ gemini_paid: true });
  await waitFor(() => expect(screen.getByRole("switch", { name: PROVIDERS.geminiPaid })).toBeChecked());
  expect(screen.getByText(PROVIDERS.geminiPaidCost)).toBeInTheDocument();
  expect(screen.getByText(PROVIDERS.endingIn("AAAA"))).toBeInTheDocument();  // Claude still listed; nothing else switched
  await userEvent.click(screen.getByRole("switch", { name: PROVIDERS.geminiPaid }));
  expect(JSON.parse(calls.filter((c) => c.url === "/api/me/provider")[1].body!)).toEqual({ gemini_paid: false });
});

test("AI providers: no paid-plan switch on Claude or OpenAI", async () => {
  markMulti();
  serve({ providers: [claude] });
  await openAt("providers");
  await screen.findByText(PROVIDERS.endingIn("AAAA"));
  expect(screen.queryByRole("switch")).toBeNull();
});

test("AI providers: the paid-plan hint shows only when the server says so, and can be dismissed", async () => {
  markMulti();
  serve({ hint: true });
  await openAt("providers");
  expect(await screen.findByText(PROVIDERS.geminiPaidHint)).toBeInTheDocument();
  expect(screen.getByRole("switch", { name: PROVIDERS.geminiPaid })).not.toBeChecked();  // never switched for them
  await userEvent.click(screen.getByRole("button", { name: PROVIDERS.dismiss }));
  expect(screen.queryByText(PROVIDERS.geminiPaidHint)).toBeNull();
});

test("AI providers: a dismissed hint stays dismissed for the rest of the day", async () => {
  markMulti();
  localStorage.setItem("lore-gemini-paid-hint-dismissed-v1", new Date().toISOString().slice(0, 10));
  serve({ hint: true });
  await openAt("providers");
  await screen.findByRole("switch", { name: PROVIDERS.geminiPaid });
  expect(screen.queryByText(PROVIDERS.geminiPaidHint)).toBeNull();
});

test("AI providers: no hint when the condition is not met, or when the switch is already on", async () => {
  markMulti();
  serve({ hint: false });
  await openAt("providers");
  await screen.findByRole("switch", { name: PROVIDERS.geminiPaid });
  expect(screen.queryByText(PROVIDERS.geminiPaidHint)).toBeNull();
});

test("AI providers: the hint is not shown while the paid plan is on", async () => {
  markMulti();
  serve({ hint: true, providers: [{ ...gemini, active: true, paid_tier: true }] });
  await openAt("providers");
  expect(await screen.findByRole("switch", { name: PROVIDERS.geminiPaid })).toBeChecked();
  expect(screen.queryByText(PROVIDERS.geminiPaidHint)).toBeNull();
});

// ---- Usage --------------------------------------------------------------------------------------------------------

const geminiUsage = (paid: boolean) => ({ ...usageBody, provider: "gemini", requests: 960, gemini_paid: paid });

test("Usage (multi, Gemini free): the request count and the reset, no tokens or cap", async () => {
  markMulti();
  serve({ usage: geminiUsage(false) });
  await openAt("usage");
  expect(await screen.findByText(USAGE.requestsToday(960, 1000))).toBeInTheDocument();
  expect(screen.getByText(USAGE.resets)).toBeInTheDocument();
  expect(screen.queryByText(/Today: 1,500 tokens/)).toBeNull();
  expect(screen.queryByLabelText(USAGE.capLabel)).toBeNull();
});

test("Usage (multi, Gemini paid): tokens, an estimated cost and the cap, no request limit", async () => {
  markMulti();
  serve({ usage: geminiUsage(true) });
  await openAt("usage");
  expect(await screen.findByText(/Today: 1,500 tokens/)).toBeInTheDocument();
  expect(screen.getByText(new RegExp(USAGE.cost(0.007).replace(/[$().]/g, "\\$&")))).toBeInTheDocument();
  expect(screen.getByLabelText(USAGE.capLabel)).toBeInTheDocument();
  expect(screen.queryByText(USAGE.requestsToday(960, 1000))).toBeNull();
  expect(screen.queryByText(USAGE.resets)).toBeNull();
});


test("Usage (multi): tokens, an estimated cost, and the daily cap", async () => {
  markMulti();
  const calls = serve({ providers: [claude] });
  await openAt("usage");
  expect(await screen.findByText(/Today: 1,500 tokens/)).toBeInTheDocument();
  expect(screen.getByText(/This month: 5,000 tokens/)).toBeInTheDocument();
  await userEvent.type(screen.getByLabelText(USAGE.capLabel), "5000");
  await userEvent.click(screen.getByRole("button", { name: USAGE.capSave }));
  expect(JSON.parse(calls.find((c) => c.url === "/api/me/token-cap")!.body!)).toEqual({ cap: 5000 });
});

test("Usage (local): Gemini's daily budget, per model with internals on", async () => {
  serve();
  await openAt("usage", { showInternals: true });
  expect(await screen.findByText(SETTINGS.geminiToday(960, 1000))).toBeInTheDocument();
  expect(screen.getByText(SETTINGS.perModel("gemini-flash", 40, 500))).toBeInTheDocument();
  expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "4");
});

test("Usage (local, on Claude): billed, no Gemini budget", async () => {
  serve({ quota: { provider: "billed", billed_by: "anthropic", models: [], resets_at: "2026-11-08T08:00:00Z" } });
  await openAt("usage", { showInternals: true });
  expect(await screen.findByText(SETTINGS.billed("Claude"))).toBeInTheDocument();
  expect(screen.queryByRole("progressbar")).toBeNull();
});

test("Usage (local, internals off): no model names", async () => {
  serve();
  await openAt("usage", { showInternals: false });
  expect(await screen.findByText(SETTINGS.requestsToday(960, 1000))).toBeInTheDocument();
  expect(screen.queryByText(/gemini-flash/)).toBeNull();
});

// ---- Appearance ---------------------------------------------------------------------------------------------------

test("Appearance: the theme grid sets the theme; Reduce motion marks the page", async () => {
  serve();
  await openAt("appearance");
  const names = within(screen.getByRole("radiogroup", { name: SETTINGS.theme })).getAllByRole("radio")
    .map((r) => r.querySelector(".theme-name")!.textContent);
  expect(names.slice(0, 3)).toEqual(["System", "Light", "Dark"]);
  expect(names).toHaveLength(THEMES.length + 1);
  await userEvent.click(screen.getByRole("radio", { name: /Nord/ }));
  expect(document.documentElement.dataset.theme).toBe("nord");
  await userEvent.click(screen.getByRole("radio", { name: SETTINGS.motionOptions.reduce }));
  expect(document.documentElement.dataset.motion).toBe("reduce");
  expect(localStorage.getItem("lore-motion-v1")).toBe("reduce");
  await userEvent.click(screen.getByRole("radio", { name: SETTINGS.motionOptions.system }));
  expect(document.documentElement.hasAttribute("data-motion")).toBe(false);
});

// ---- Reading ------------------------------------------------------------------------------------------------------

const worlds = [{ id: 1, name: "Alpha Saga", books: [] }, { id: 2, name: "Beta Realm", books: [] }];

test("Reading: world, depth, steps and internals", async () => {
  serve();
  const onWorldChange = vi.fn(), onDepth = vi.fn(), onShowInternals = vi.fn();
  await openAt("reading", { worlds, world: "all", onWorldChange, depth: "auto", onDepth, showInternals: true, onShowInternals });
  await userEvent.selectOptions(screen.getByLabelText(SETTINGS.world), "Beta Realm");
  expect(onWorldChange).toHaveBeenCalledWith("2");
  expect(screen.getByRole("radio", { name: "Auto" })).toBeChecked();
  await userEvent.click(screen.getByRole("radio", { name: "Deep on" }));
  expect(onDepth).toHaveBeenCalledWith("deep");
  await userEvent.click(screen.getByRole("switch", { name: SETTINGS.steps }));
  expect(localStorage.getItem("lore-steps-open-v1")).toBe("open");
  const internals = screen.getByRole("switch", { name: SETTINGS.internals });
  expect(internals).toBeChecked();
  await userEvent.click(internals);
  expect(onShowInternals).toHaveBeenCalledWith(false);
  expect(screen.getByRole("button", { name: SETTINGS.positionOpen })).toBeDisabled();
});

test("Reading: the reading-position button closes Settings and opens the top bar's control", async () => {
  serve();
  await openAt("reading", { worlds, world: "1" });
  const pos = screen.getByRole("button", { name: "Position" });
  const clicked = vi.fn();
  pos.addEventListener("click", clicked);
  await userEvent.click(screen.getByRole("button", { name: SETTINGS.positionOpen }));
  expect(screen.queryByRole("dialog")).toBeNull();
  await waitFor(() => expect(clicked).toHaveBeenCalled());
  expect(pos).toHaveFocus();
});

test("Reading (multi): no internals switch", async () => {
  markMulti();
  serve();
  await openAt("reading");
  await screen.findByRole("tab", { name: "Account" });
  expect(screen.queryByRole("switch", { name: SETTINGS.internals })).toBeNull();
});

// ---- MCP connector ------------------------------------------------------------------------------------------------

test("MCP (local): the exact snippets with copy buttons, and Check lists the tools", async () => {
  const calls = serve();
  await openAt("mcp");
  expect(await screen.findByText(mcpBody.claude_code)).toBeInTheDocument();
  expect(screen.getByText(/"mcpServers"/)).toBeInTheDocument();
  expect(screen.getByText(mcpBody.http_command)).toBeInTheDocument();
  expect(screen.getByText(mcpBody.claude_code_http)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: `${SETTINGS.copy} ${SETTINGS.mcpDesktop}` })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: `${SETTINGS.copy} ${SETTINGS.mcpCode}` })).toBeInTheDocument();
  expect(screen.getByText(UI.mcpNotRunning)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: SETTINGS.mcpCheck }));
  expect(calls.some((c) => c.url === "/api/mcp/check")).toBe(true);
  expect(await screen.findByText(SETTINGS.mcpOk)).toBeInTheDocument();
  expect(screen.getByText(SETTINGS.mcpTools(2))).toBeInTheDocument();
  expect(screen.getByText("lore_search")).toBeInTheDocument();
});

test("MCP (multi): the server address, the token list and snippets with a placeholder; no coming-soon line", async () => {
  markMulti();
  serve();
  await openAt("mcp");
  expect(await within(panel()).findByText(hostedBody.mcp_url)).toBeInTheDocument();
  expect(await within(panel()).findByText("Old laptop")).toBeInTheDocument();
  expect(within(panel()).getByText(SETTINGS.mcpTokenPrefix("lore_ZzZzZzZ"))).toBeInTheDocument();
  expect(within(panel()).getByText(
    'claude mcp add --transport http lore https://lore.example.org/mcp --header "Authorization: Bearer <your token>"')).toBeInTheDocument();
  expect(panel().textContent).not.toMatch(/coming soon/i);
  expect(within(panel()).getByRole("button", { name: SETTINGS.mcpCheck })).toBeEnabled();
});

test("MCP (multi): Create token shows it once, fills the snippets, and lists it; Revoke removes it", async () => {
  markMulti();
  const calls = serve();
  await openAt("mcp");
  await within(panel()).findByText("Old laptop");
  await userEvent.type(within(panel()).getByLabelText(SETTINGS.mcpTokenName), "Work laptop");
  await userEvent.click(within(panel()).getByRole("button", { name: SETTINGS.mcpCreate }));
  expect(await within(panel()).findByText(SETTINGS.mcpTokenOnce)).toBeInTheDocument();
  expect(within(panel()).getByText("lore_AbCdEfGsecretsecretsecret")).toBeInTheDocument();
  expect(within(panel()).getByRole("button", { name: `${SETTINGS.copy} ${SETTINGS.mcpTokenLabel}` })).toBeInTheDocument();
  expect(within(panel()).getByText(
    'claude mcp add --transport http lore https://lore.example.org/mcp --header "Authorization: Bearer lore_AbCdEfGsecretsecretsecret"')).toBeInTheDocument();
  expect(await within(panel()).findByText("Work laptop")).toBeInTheDocument();
  const post = calls.find((c) => c.url === "/api/me/mcp-tokens" && c.method === "POST")!;
  expect(JSON.parse(post.body!)).toEqual({ name: "Work laptop" });
  // Two tokens is this fake server's cap: the form says so and is disabled.
  expect(within(panel()).getByText(SETTINGS.mcpTokenMax(2))).toBeInTheDocument();
  expect(within(panel()).getByRole("button", { name: SETTINGS.mcpCreate })).toBeDisabled();
  await userEvent.click(within(panel()).getByRole("button", { name: SETTINGS.mcpTokenRevokeLabel("Work laptop") }));
  await waitFor(() => expect(within(panel()).queryByText("Work laptop")).toBeNull());
  expect(within(panel()).queryByText(SETTINGS.mcpTokenOnce)).toBeNull();  // the revoked token's secret is gone too
  expect(calls.some((c) => c.url === "/api/me/mcp-tokens/t2" && c.method === "DELETE")).toBe(true);
});

test("MCP (multi): a token needs a name", async () => {
  markMulti();
  const calls = serve();
  await openAt("mcp");
  await within(panel()).findByText("Old laptop");
  await userEvent.click(within(panel()).getByRole("button", { name: SETTINGS.mcpCreate }));
  expect(within(panel()).getByRole("alert")).toHaveTextContent(SETTINGS.mcpTokenNameNeeded);
  expect(calls.some((c) => c.url === "/api/me/mcp-tokens" && c.method === "POST")).toBe(false);
});

test("MCP (multi, OAuth on): claude.ai steps and the connected apps, each disconnectable", async () => {
  markMulti();
  oauthOn = true;
  const calls = serve();
  await openAt("mcp");
  expect(await within(panel()).findByText(SETTINGS.mcpClaudeAiSteps[0])).toBeInTheDocument();
  expect(within(panel()).queryByText(SETTINGS.mcpClaudeAiOff)).toBeNull();
  const apps = await within(panel()).findByRole("button", { name: SETTINGS.mcpDisconnectLabel("Claude") });
  await userEvent.click(apps);
  expect(await within(panel()).findByText(SETTINGS.mcpConnectionsNone)).toBeInTheDocument();
  expect(calls.some((c) => c.url === "/api/me/mcp-connections/g1" && c.method === "DELETE")).toBe(true);
});

test("MCP (multi, OAuth off): says claude.ai needs https, and lists no connected apps", async () => {
  markMulti();
  const calls = serve();
  await openAt("mcp");
  expect(await within(panel()).findByText(SETTINGS.mcpClaudeAiOff)).toBeInTheDocument();
  expect(within(panel()).queryByText(SETTINGS.mcpConnections)).toBeNull();
  expect(calls.some((c) => c.url === "/api/me/mcp-connections")).toBe(false);
});

test("MCP (multi): Check lists the hosted tools", async () => {
  markMulti();
  serve();
  await openAt("mcp");
  await userEvent.click(within(panel()).getByRole("button", { name: SETTINGS.mcpCheck }));
  expect(await within(panel()).findByText(SETTINGS.mcpCheckHosted)).toBeInTheDocument();
});

// ---- Your data ----------------------------------------------------------------------------------------------------

test("Your data (multi): Download my data posts and saves the zip", async () => {
  markMulti();
  const calls = serve();
  const made = vi.fn(() => "blob:x");
  URL.createObjectURL = made;
  URL.revokeObjectURL = vi.fn();
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  await openAt("data");
  await userEvent.click(within(panel()).getByRole("button", { name: SETTINGS.download }));
  expect(calls.some((c) => c.url === "/api/me/export" && c.method === "POST")).toBe(true);
  await waitFor(() => expect(made).toHaveBeenCalled());
  expect(click).toHaveBeenCalled();
});

test("Your data (multi): Delete my account needs the email typed", async () => {
  markMulti();
  const calls = serve();
  const go = vi.spyOn(nav, "to").mockImplementation(() => {});
  await openAt("data");
  await userEvent.click(await within(panel()).findByRole("button", { name: ACCOUNT_DELETE.open }));
  const confirm = screen.getByRole("button", { name: ACCOUNT_DELETE.confirm });
  expect(confirm).toBeDisabled();
  await userEvent.type(screen.getByLabelText(ACCOUNT_DELETE.confirmLabel("me@example.org")), "me@example.org");
  await userEvent.click(confirm);
  expect(calls.some((c) => c.url === "/api/me/delete" && c.method === "POST")).toBe(true);
  expect(go).toHaveBeenCalledWith("/");
});

test("Your data (local): where the library lives and a backup command", async () => {
  serve();
  await openAt("data");
  expect(await screen.findByText("/home/r/LORE/data")).toBeInTheDocument();
  expect(screen.getByText("tar -czf ~/lore-backup-$(date +%F).tar.gz -C '/home/r/LORE' 'data'")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: SETTINGS.download })).toBeNull();
  expect(screen.queryByRole("button", { name: ACCOUNT_DELETE.open })).toBeNull();
});

// ---- nothing lost from the old menus ------------------------------------------------------------------------------

test("every control of the old ⚙ menu, theme menu and account menu is somewhere in Settings (multi, admin)", async () => {
  markMulti();
  serve({ role: "admin" });
  await openAt("account");
  const visit = async (tab: string) => { await userEvent.click(screen.getByRole("tab", { name: tab })); return panel(); };
  expect(await within(panel()).findByText("me@example.org")).toBeInTheDocument();            // the account's email
  expect(within(panel()).getByRole("button", { name: AUTH.signOut })).toBeInTheDocument();   // Sign out
  await waitFor(() => expect(tabNames()).toContain("Admin"));
  expect(within(await visit("Admin")).getByRole("link", { name: SETTINGS.adminLink })).toHaveAttribute("href", "/admin");
  expect(await within(await visit("AI providers")).findByText(PROVIDERS.endingIn("AAAA"))).toBeInTheDocument();  // ProviderSettings
  expect(await within(await visit("Usage")).findByText(/Today: 1,500 tokens/)).toBeInTheDocument();          // UsagePanel
  expect(within(await visit("Your data")).getByRole("button", { name: ACCOUNT_DELETE.open })).toBeInTheDocument();
  const themes = within(await visit("Appearance")).getAllByRole("radio").map((r) => r.getAttribute("data-id"));
  for (const t of ["system", ...THEMES.map((x) => x.id)]) expect(themes).toContain(t);                     // every theme
});

test("every control of the old local MCP panel is in Settings (local)", async () => {
  serve();
  await openAt("mcp", { showInternals: true });
  expect(await screen.findByText(mcpBody.claude_code)).toBeInTheDocument();  // the Claude Code command
  expect(screen.getByText(mcpBody.http_command)).toBeInTheDocument();        // the HTTP command
  expect(screen.getByText(mcpBody.http_url)).toBeInTheDocument();            // the HTTP address
  expect(screen.getByText(UI.mcpNotRunning)).toBeInTheDocument();            // the running status
});

test("Usage (local): nothing spent yet today", async () => {
  serve();
  const real = globalThis.fetch;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => url === "/api/quota"
    ? new Response(JSON.stringify({ provider: "gemini", models: [], resets_at: "2026-11-08T08:00:00Z" })) : real(url, init)));
  await openAt("usage", { showInternals: true });
  expect(await screen.findByText(SETTINGS.geminiNoneYet)).toBeInTheDocument();
});
