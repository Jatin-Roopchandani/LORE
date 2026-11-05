import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Router from "../Router";
import { ADMIN, AUTH, HOME, LEGAL } from "../messages";
import { Admin } from "./Admin";
import { SignIn } from "./SignIn";
import { Waitlist } from "./Waitlist";

type Handler = (url: string, init?: RequestInit) => { status?: number; body: unknown };
function serve(handler: Handler) {
  const calls: { url: string; method: string; body: any; headers: Record<string, string> }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit = {}) => {
    calls.push({ url, method: init.method ?? "GET", body: init.body ? JSON.parse(init.body as string) : undefined, headers: (init.headers ?? {}) as Record<string, string> });
    const { status = 200, body } = handler(url, init);
    return new Response(JSON.stringify(body), { status });
  }));
  return calls;
}
afterEach(() => { vi.unstubAllGlobals(); document.cookie = "lore_csrf=; path=/; max-age=0"; });

const PROVIDERS = { providers: ["github", "google"], email: true, invite_only: true };

describe("SignIn", () => {
  test("offers the configured providers and an email form, and a way to request an invite", async () => {
    serve(() => ({ body: PROVIDERS }));
    render(<SignIn search="" />);
    expect(await screen.findByRole("link", { name: AUTH.withGoogle })).toHaveAttribute("href", "/auth/google/start");
    expect(screen.getByRole("link", { name: AUTH.withGithub })).toHaveAttribute("href", "/auth/github/start");
    expect(screen.getByRole("button", { name: AUTH.sendLink })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: AUTH.requestInvite })).toHaveAttribute("href", "/waitlist");
  });

  test("shows only the providers the server has configured", async () => {
    serve(() => ({ body: { ...PROVIDERS, providers: ["github"] } }));
    render(<SignIn search="" />);
    await screen.findByRole("link", { name: AUTH.withGithub });
    expect(screen.queryByRole("link", { name: AUTH.withGoogle })).toBeNull();
  });

  test("an invite in the address is carried to the provider and to the emailed link", async () => {
    const calls = serve((url) => url === "/auth/email" ? { status: 202, body: { sent: true } } : { body: PROVIDERS });
    render(<SignIn search="?invite=abc_DEF-123" />);
    expect(await screen.findByRole("link", { name: AUTH.withGoogle })).toHaveAttribute("href", "/auth/google/start?invite=abc_DEF-123");
    expect(screen.getByText(AUTH.invited)).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText(AUTH.emailLabel), "me@example.org");
    await userEvent.click(screen.getByRole("button", { name: AUTH.sendLink }));
    expect(calls.find((c) => c.url === "/auth/email")!.body).toEqual({ email: "me@example.org", invite: "abc_DEF-123" });
  });

  test("asking for a link says the same thing whoever asks", async () => {
    serve((url) => url === "/auth/email" ? { status: 202, body: { sent: true } } : { body: PROVIDERS });
    render(<SignIn search="" />);
    await userEvent.type(screen.getByLabelText(AUTH.emailLabel), "me@example.org");
    await userEvent.click(screen.getByRole("button", { name: AUTH.sendLink }));
    expect(await screen.findByText(AUTH.linkSent)).toBeInTheDocument();
  });

  test("rate limits and bad addresses show their own messages", async () => {
    serve((url) => url === "/auth/email" ? { status: 429, body: { error: { code: "rate_limited", message: "x" } } } : { body: PROVIDERS });
    render(<SignIn search="" />);
    await userEvent.type(screen.getByLabelText(AUTH.emailLabel), "me@example.org");
    await userEvent.click(screen.getByRole("button", { name: AUTH.sendLink }));
    expect(await screen.findByText(AUTH.rateLimited)).toBeInTheDocument();
  });

  test("an error code from the server shows a fixed message, never the raw text", async () => {
    serve(() => ({ body: PROVIDERS }));
    render(<SignIn search="?error=email_unverified" />);
    expect(await screen.findByText(AUTH.errors.email_unverified)).toBeInTheDocument();
  });

  test("an unknown error code falls back to the generic message and shows nothing from the address", async () => {
    serve(() => ({ body: PROVIDERS }));
    render(<SignIn search="?error=%3Cb%3Eevil" />);
    expect(await screen.findByText(AUTH.errors.oauth_failed)).toBeInTheDocument();
    expect(screen.queryByText(/evil/)).toBeNull();
  });

  test("the sign-in call is sent without a CSRF header when there is no session (none exists yet)", async () => {
    const calls = serve((url) => url === "/auth/email" ? { status: 202, body: { sent: true } } : { body: PROVIDERS });
    render(<SignIn search="" />);
    await userEvent.type(screen.getByLabelText(AUTH.emailLabel), "me@example.org");
    await userEvent.click(screen.getByRole("button", { name: AUTH.sendLink }));
    expect(calls.find((c) => c.url === "/auth/email")!.headers["X-CSRF-Token"]).toBeUndefined();
  });
});

describe("Waitlist", () => {
  test("submits the email, reading and consent, with the honeypot empty, and thanks the person", async () => {
    const calls = serve(() => ({ status: 202, body: { ok: true } }));
    render(<Waitlist />);
    const submit = screen.getByRole("button", { name: AUTH.submit });
    expect(submit).toBeDisabled(); // no consent yet
    await userEvent.type(screen.getByLabelText(AUTH.emailLabel), "me@example.org");
    await userEvent.type(screen.getByLabelText(AUTH.readingLabel), "Dracula");
    await userEvent.click(screen.getByLabelText(AUTH.consentLabel));
    await userEvent.click(submit);
    expect(await screen.findByText(AUTH.thanks)).toBeInTheDocument();
    expect(calls[0]).toMatchObject({ url: "/api/waitlist", method: "POST",
      body: { email: "me@example.org", reading: "Dracula", consent: true, website: "" } });
  });

  test("the honeypot field is hidden from people and from assistive technology", () => {
    serve(() => ({ status: 202, body: { ok: true } }));
    const { container } = render(<Waitlist />);
    const hp = container.querySelector('input[name="website"]')!;
    expect(hp.closest(".hp")).toHaveAttribute("aria-hidden", "true");
    expect(hp).toHaveAttribute("tabindex", "-1");
  });

  test("thanks even when the server says something else (it always means 202, but never reveal more)", async () => {
    serve(() => ({ status: 500, body: { detail: "boom" } }));
    render(<Waitlist />);
    await userEvent.type(screen.getByLabelText(AUTH.emailLabel), "me@example.org");
    await userEvent.click(screen.getByLabelText(AUTH.consentLabel));
    await userEvent.click(screen.getByRole("button", { name: AUTH.submit }));
    expect(await screen.findByText(AUTH.thanks)).toBeInTheDocument();
  });
});

describe("Admin", () => {
  const entry = (id: number, email: string, status = "pending") => ({ id, email, reading: "Dracula", status, created_at: "2026-11-04T10:00:00+00:00", invited_at: null });
  const users = { users: [{ id: "u1", email: "root@example.org", name: "Root", role: "admin", status: "active", created_at: "2026-11-01T00:00:00+00:00" }] };

  test("lists the waitlist and the users", async () => {
    serve((url) => url === "/api/admin/waitlist" ? { body: { entries: [entry(1, "a@example.org"), entry(2, "b@example.org", "declined")] } } : { body: users });
    render(<Admin />);
    const table = await screen.findByRole("table", { name: ADMIN.waitlist });
    expect(within(table).getByText("a@example.org")).toBeInTheDocument();
    expect(within(table).getByText(ADMIN.status.declined)).toBeInTheDocument();
    expect(within(screen.getByRole("table", { name: ADMIN.users })).getByText("root@example.org")).toBeInTheDocument();
  });

  test("approving calls the API with the CSRF token and reloads", async () => {
    document.cookie = "lore_csrf=tok123; path=/";
    let approved = false;
    const calls = serve((url) => {
      if (url === "/api/admin/waitlist/1/approve") { approved = true; return { body: { invited: true, entry: entry(1, "a@example.org", "invited") } }; }
      if (url === "/api/admin/waitlist") return { body: { entries: [entry(1, "a@example.org", approved ? "invited" : "pending")] } };
      return { body: users };
    });
    render(<Admin />);
    await userEvent.click(await screen.findByRole("button", { name: ADMIN.approve }));
    expect(await screen.findByText(ADMIN.approved("a@example.org"))).toBeInTheDocument();
    const post = calls.find((c) => c.url.endsWith("/approve"))!;
    expect(post.method).toBe("POST");
    expect(post.headers["X-CSRF-Token"]).toBe("tok123");
    await waitFor(() => expect(screen.queryByRole("button", { name: ADMIN.approve })).toBeNull());
  });

  test("declining", async () => {
    const calls = serve((url) => url.endsWith("/decline") ? { body: { declined: true, entry: entry(1, "a@example.org", "declined") } }
      : url === "/api/admin/waitlist" ? { body: { entries: [entry(1, "a@example.org")] } } : { body: users });
    render(<Admin />);
    await userEvent.click(await screen.findByRole("button", { name: ADMIN.decline }));
    expect(await screen.findByText(ADMIN.declined("a@example.org"))).toBeInTheDocument();
    expect(calls.some((c) => c.url === "/api/admin/waitlist/1/decline" && c.method === "POST")).toBe(true);
  });

  test("a failed approval says nothing was changed", async () => {
    serve((url) => url.endsWith("/approve") ? { status: 502, body: { error: { code: "mail_failed", message: "x" } } }
      : url === "/api/admin/waitlist" ? { body: { entries: [entry(1, "a@example.org")] } } : { body: users });
    render(<Admin />);
    await userEvent.click(await screen.findByRole("button", { name: ADMIN.approve }));
    expect(await screen.findByText(ADMIN.actionFailed)).toBeInTheDocument();
  });

  test("creates an invite and shows the code and link once", async () => {
    const calls = serve((url) => url === "/api/admin/invites" ? { status: 201, body: { code: "CODE123", link: "https://lore.example/signin?invite=CODE123", expires_at: "x" } }
      : url === "/api/admin/waitlist" ? { body: { entries: [] } } : { body: users });
    render(<Admin />);
    await userEvent.click(await screen.findByRole("button", { name: ADMIN.createInvite }));
    expect(await screen.findByText("CODE123")).toBeInTheDocument();
    expect(screen.getByText("https://lore.example/signin?invite=CODE123")).toBeInTheDocument();
    expect(calls.find((c) => c.url === "/api/admin/invites")!.body).toEqual({ days: 14 });
  });

  test("a non-admin sees only that the page is for administrators", async () => {
    serve(() => ({ status: 403, body: { detail: "admins only" } }));
    render(<Admin />);
    expect(await screen.findByText(ADMIN.forbidden)).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
  });

  test("lists reported answers and opens a report's trace, each call collapsible", async () => {
    const report = { id: 5, request_id: "req-9", note: "wrong book", created_at: "2026-11-04T10:00:00+00:00", status: "open",
      user_id: "u2", email: "reader@example.org", question: "Who met Watson?", trace: true };
    const trace = { request_id: "req-9", user_id: "u2", created_at: "x", question: "Who met Watson?", world: null, depth: null,
      decomposition: { parts: { sub_questions: ["who?"] } }, steps: [], error: null, timings: null, model_usage: null,
      calls: [{ stage: "synthesize", model: "flash", prompt: "THE PROMPT", reply: "THE REPLY", latency_ms: 12, ok: true, error: null, attempts: [] }],
      answer: { answer: "Holmes met Watson." } };
    serve((url) => url === "/api/admin/reports" ? { body: { reports: [report] } }
      : url === "/api/admin/traces/req-9" ? { body: trace }
      : url === "/api/admin/waitlist" ? { body: { entries: [] } } : { body: users });
    render(<Admin />);
    const table = await screen.findByRole("table", { name: ADMIN.reports });
    expect(within(table).getByText("wrong book")).toBeInTheDocument();
    await userEvent.click(within(table).getByRole("button", { name: ADMIN.openTrace }));
    const view = await screen.findByRole("region", { name: ADMIN.traceTitle });
    expect(within(view).getByText(ADMIN.traceCalls(1))).toBeInTheDocument();
    expect(within(view).getByText("1. synthesize · flash · 12 ms")).toBeInTheDocument();
    expect(within(view).getByText("THE PROMPT")).toBeInTheDocument();
    expect(within(view).getByText("Holmes met Watson.")).toBeInTheDocument();
  });

  test("an empty waitlist says so", async () => {
    serve((url) => url === "/api/admin/waitlist" ? { body: { entries: [] } } : { body: users });
    render(<Admin />);
    expect(await screen.findByText(ADMIN.noRequests)).toBeInTheDocument();
  });
});

describe("Router", () => {
  test("multi mode draws the page for the address; the homepage links to sign in and the waitlist form", async () => {
    render(<Router pathname="/" multi />);
    const nav = await screen.findByRole("navigation", { name: HOME.navLabel });
    expect(within(nav).getByRole("link", { name: HOME.signIn })).toHaveAttribute("href", "/signin");
    expect(within(nav).getByRole("link", { name: HOME.requestInvite })).toHaveAttribute("href", "#beta");
  });

  test("privacy and terms exist", async () => {
    render(<Router pathname="/privacy" multi />);
    expect(await screen.findByRole("heading", { level: 1, name: LEGAL.privacy.title })).toBeInTheDocument();
    render(<Router pathname="/terms" multi />);
    expect(await screen.findByRole("heading", { level: 1, name: LEGAL.terms.title })).toBeInTheDocument();
  });

  test("an unknown address says there is nothing there", () => {
    render(<Router pathname="/nope" multi />);
    expect(screen.getByText(AUTH.notFound)).toBeInTheDocument();
  });
});
