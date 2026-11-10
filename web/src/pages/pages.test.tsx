import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Router from "../Router";
import { ADMIN, AUTH, HOME, LEGAL, EMAIL_SUGGEST } from "../messages";
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

const SENT = { status: 202, body: { sent: true } };
const ask = (url: string) => url === "/auth/email";
async function requestLink(address = "me@example.org", user: Pick<typeof userEvent, "type" | "click"> = userEvent) {
  await user.type(screen.getByLabelText(AUTH.emailLabel), address);
  await user.click(screen.getByRole("button", { name: AUTH.sendLink }));
}

describe("SignIn", () => {
  test("offers the configured providers in Google's button style, the email form, and a way to request an invite", async () => {
    serve(() => ({ body: PROVIDERS }));
    render(<SignIn search="" />);
    const google = await screen.findByRole("link", { name: AUTH.withGoogle });
    expect(google).toHaveAttribute("href", "/auth/google/start");
    expect(google.querySelector("svg")).not.toBeNull(); // the G, inline
    expect(screen.getByRole("link", { name: AUTH.withGithub })).toHaveAttribute("href", "/auth/github/start");
    expect(screen.getByText(AUTH.or)).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: AUTH.signInTitle })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: AUTH.sendLink })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: AUTH.requestInvite })).toHaveAttribute("href", "/waitlist");
    expect(screen.getByRole("link", { name: AUTH.termsLink })).toHaveAttribute("href", "/terms");
    expect(screen.getByRole("link", { name: AUTH.privacyLink })).toHaveAttribute("href", "/privacy");
  });

  test("shows only the providers the server has configured", async () => {
    serve(() => ({ body: { ...PROVIDERS, providers: ["github"] } }));
    render(<SignIn search="" />);
    await screen.findByRole("link", { name: AUTH.withGithub });
    expect(screen.queryByRole("link", { name: AUTH.withGoogle })).toBeNull();
  });

  test("with no provider configured there are no provider buttons and no divider", async () => {
    const calls = serve(() => ({ body: { ...PROVIDERS, providers: [] } }));
    render(<SignIn search="" />);
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(screen.queryByRole("link", { name: /Continue with/ })).toBeNull();
    expect(screen.queryByText(AUTH.or)).toBeNull();
    expect(screen.getByRole("button", { name: AUTH.sendLink })).toBeInTheDocument();
  });

  test("an invite in the address shows the banner and is carried to the provider and to the emailed link", async () => {
    const calls = serve((url) => ask(url) ? SENT : { body: PROVIDERS });
    render(<SignIn search="?invite=abc_DEF-123" />);
    expect(await screen.findByRole("link", { name: AUTH.withGoogle })).toHaveAttribute("href", "/auth/google/start?invite=abc_DEF-123");
    expect(screen.getByText(AUTH.invitedTitle)).toBeInTheDocument();
    expect(screen.getByText(AUTH.invited)).toBeInTheDocument();
    await requestLink();
    expect(calls.find((c) => ask(c.url))!.body).toEqual({ email: "me@example.org", invite: "abc_DEF-123" });
  });

  test("no invite, no banner", async () => {
    serve(() => ({ body: PROVIDERS }));
    render(<SignIn search="" />);
    await screen.findByRole("link", { name: AUTH.withGoogle });
    expect(screen.queryByText(AUTH.invitedTitle)).toBeNull();
  });

  test("while the link is being sent the button is busy and says so", async () => {
    let answer: (r: Response) => void = () => {};
    vi.stubGlobal("fetch", vi.fn((url: string) => ask(url)
      ? new Promise<Response>((done) => { answer = done; })
      : Promise.resolve(new Response(JSON.stringify(PROVIDERS)))));
    render(<SignIn search="" />);
    await requestLink();
    const busy = await screen.findByRole("button", { name: AUTH.sending });
    expect(busy).toBeDisabled();
    expect(busy).toHaveAttribute("aria-busy", "true");
    answer(new Response(JSON.stringify({ sent: true }), { status: 202 }));
    expect(await screen.findByRole("heading", { name: AUTH.checkInbox })).toBeInTheDocument();
  });

  test("asking for a link says the same thing whoever asks: check your inbox, naming the address", async () => {
    serve((url) => ask(url) ? SENT : { body: PROVIDERS });
    render(<SignIn search="" />);
    await requestLink("  me@example.org ");
    const heading = await screen.findByRole("heading", { level: 1, name: AUTH.checkInbox });
    expect(heading).toHaveFocus();
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent(`${AUTH.sentBefore}me@example.org${AUTH.sentAfter}`);
    expect(screen.queryByLabelText(AUTH.emailLabel)).toBeNull(); // the form is gone
    expect(screen.queryByRole("link", { name: AUTH.withGoogle })).toBeNull();
  });

  describe("after the link is sent", () => {
    beforeEach(() => { vi.useFakeTimers({ shouldAdvanceTime: true }); });
    afterEach(() => { vi.useRealTimers(); });

    test("counts down before offering to resend, then resends and counts down again", async () => {
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      const calls = serve((url) => ask(url) ? SENT : { body: PROVIDERS });
      render(<SignIn search="?invite=INV" />);
      await requestLink("me@example.org", user);
      await screen.findByRole("heading", { name: AUTH.checkInbox });
      expect(screen.getByText(AUTH.didntGet)).toBeInTheDocument();
      expect(screen.getByText(AUTH.resendIn(30))).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: AUTH.resend })).toBeNull();
      act(() => { vi.advanceTimersByTime(10_000); });
      expect(screen.getByText(AUTH.resendIn(20))).toBeInTheDocument();
      act(() => { vi.advanceTimersByTime(20_000); });
      const resend = screen.getByRole("button", { name: AUTH.resend });
      expect(screen.queryByText(/Resend in/)).toBeNull();
      await user.click(resend);
      expect(await screen.findByText(AUTH.resent)).toBeInTheDocument();
      const asks = calls.filter((c) => ask(c.url));
      expect(asks).toHaveLength(2);
      expect(asks[1].body).toEqual({ email: "me@example.org", invite: "INV" });
      expect(screen.getByText(AUTH.resendIn(30))).toBeInTheDocument();
    });

    test("a refused resend shows why, and the resend button stays", async () => {
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      let n = 0;
      serve((url) => ask(url) ? (n++ ? { status: 429, body: { error: { code: "rate_limited", message: "x" } } } : SENT) : { body: PROVIDERS });
      render(<SignIn search="" />);
      await requestLink("me@example.org", user);
      await screen.findByRole("heading", { name: AUTH.checkInbox });
      act(() => { vi.advanceTimersByTime(30_000); });
      await user.click(screen.getByRole("button", { name: AUTH.resend }));
      expect(await screen.findByRole("alert")).toHaveTextContent(AUTH.rateLimited);
      expect(screen.getByRole("button", { name: AUTH.resend })).toBeEnabled();
    });

    test("'Use a different email' goes back to an empty, focused field", async () => {
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      serve((url) => ask(url) ? SENT : { body: PROVIDERS });
      render(<SignIn search="" />);
      await requestLink("me@example.org", user);
      await screen.findByRole("heading", { name: AUTH.checkInbox });
      await user.click(screen.getByRole("button", { name: AUTH.differentEmail }));
      const field = screen.getByLabelText(AUTH.emailLabel);
      expect(field).toHaveValue("");
      expect(field).toHaveFocus();
      expect(screen.getByRole("heading", { name: AUTH.signInTitle })).toBeInTheDocument();
      act(() => { vi.advanceTimersByTime(30_000); }); // the old countdown is gone, nothing ticks into the form
      expect(screen.queryByText(/Resend/)).toBeNull();
    });
  });

  test("an address that isn't one is caught before anything is sent", async () => {
    const calls = serve(() => ({ body: PROVIDERS }));
    render(<SignIn search="" />);
    await userEvent.click(screen.getByRole("button", { name: AUTH.sendLink }));
    const field = screen.getByLabelText(AUTH.emailLabel);
    expect(screen.getByRole("alert")).toHaveTextContent(AUTH.emailMissing);
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(field).toHaveFocus();
    await userEvent.type(field, "not-an-address");
    expect(screen.queryByRole("alert")).toBeNull(); // typing clears it
    await userEvent.click(screen.getByRole("button", { name: AUTH.sendLink }));
    expect(screen.getByRole("alert")).toHaveTextContent(AUTH.invalidEmail);
    expect(field).toHaveAccessibleDescription(AUTH.invalidEmail);
    expect(calls.some((c) => ask(c.url))).toBe(false);
  });

  test.each([
    ["invalid_email", 400, AUTH.invalidEmail],
    ["rate_limited", 429, AUTH.rateLimited],
    ["mail_failed", 502, AUTH.sendFailed],
  ])("the server's %s shows its own message and keeps the form", async (code, status, message) => {
    serve((url) => ask(url) ? { status, body: { error: { code, message: "x" } } } : { body: PROVIDERS });
    render(<SignIn search="" />);
    await requestLink();
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.getByLabelText(AUTH.emailLabel)).toHaveValue("me@example.org");
    expect(screen.getByRole("button", { name: AUTH.sendLink })).toBeEnabled();
  });

  test.each(Object.keys(AUTH.errors))("the server's ?error=%s shows its fixed message in an alert", async (code) => {
    serve(() => ({ body: PROVIDERS }));
    render(<SignIn search={`?error=${code}`} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(AUTH.errors[code]);
  });

  test("an unknown error code falls back to the generic message and shows nothing from the address", async () => {
    serve(() => ({ body: PROVIDERS }));
    render(<SignIn search="?error=%3Cb%3Eevil" />);
    expect(await screen.findByText(AUTH.errors.oauth_failed)).toBeInTheDocument();
    expect(screen.queryByText(/evil/)).toBeNull();
  });

  test("the sign-in call is sent without a CSRF header when there is no session (none exists yet)", async () => {
    const calls = serve((url) => ask(url) ? SENT : { body: PROVIDERS });
    render(<SignIn search="" />);
    await requestLink();
    await screen.findByRole("heading", { name: AUTH.checkInbox });
    expect(calls.find((c) => ask(c.url))!.headers["X-CSRF-Token"]).toBeUndefined();
  });

  test("the brand panel carries the logo link home, the promise and the spoiler-free graph", async () => {
    serve(() => ({ body: PROVIDERS }));
    const { container } = render(<SignIn search="" />);
    await screen.findByRole("link", { name: AUTH.withGoogle });
    expect(screen.getAllByRole("link", { name: AUTH.homeLabel })[0]).toHaveAttribute("href", "/");
    expect(screen.getByText(AUTH.brandLineGlow)).toBeInTheDocument();
    const art = container.querySelector(".au-art")!;
    expect(art).toHaveAttribute("aria-hidden", "true");
    expect(art.querySelectorAll("line").length).toBeGreaterThan(10);
    expect(container.querySelector("[style]")).toBeNull(); // nothing the CSP would refuse
  });
});

describe("Waitlist", () => {
  test("submits the email, reading and consent, with the honeypot empty, and thanks the person", async () => {
    const calls = serve(() => ({ status: 202, body: { ok: true } }));
    render(<Waitlist />);
    expect(screen.getByRole("heading", { level: 1, name: AUTH.waitlistTitle })).toBeInTheDocument();
    const submit = screen.getByRole("button", { name: AUTH.submit });
    expect(screen.queryByRole("checkbox")).toBeNull(); // sending is the consent; the line says what it allows
    expect(screen.getByText(AUTH.consentLine)).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText(AUTH.emailLabel), "me@example.org");
    await userEvent.type(screen.getByLabelText(AUTH.readingLabel), "Dracula");
    await userEvent.click(submit);
    expect(await screen.findByRole("status")).toHaveTextContent(AUTH.thanks);
    // the invite may land in spam: say where it comes from and where to look
    expect(screen.getByText(AUTH.spamHint)).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: AUTH.waitlistDone })).toHaveFocus();
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
    await userEvent.click(screen.getByRole("button", { name: AUTH.submit }));
    expect(await screen.findByText(AUTH.thanks)).toBeInTheDocument();
  });

  test("uses the same split frame, and links back to sign in", () => {
    serve(() => ({ status: 202, body: { ok: true } }));
    const { container } = render(<Waitlist />);
    expect(container.querySelector(".au .au-brand")).not.toBeNull();
    expect(screen.getByRole("link", { name: AUTH.signIn })).toHaveAttribute("href", "/signin");
    expect(screen.getByRole("link", { name: AUTH.termsLink })).toHaveAttribute("href", "/terms");
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

describe("email typo hints", () => {
  test("the waitlist suggests @gmail.com for @gmai.com, and one click fixes it", async () => {
    const calls = serve(() => ({ status: 202, body: { ok: true } }));
    render(<Waitlist />);
    await userEvent.type(screen.getByLabelText(AUTH.emailLabel), "shadow@gmai.com");
    expect(screen.getByText("shadow@gmail.com")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: EMAIL_SUGGEST.use }));
    expect(screen.getByLabelText(AUTH.emailLabel)).toHaveValue("shadow@gmail.com");
    expect(screen.queryByRole("button", { name: EMAIL_SUGGEST.use })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: AUTH.submit }));
    expect(calls[0].body.email).toBe("shadow@gmail.com");
  });

  test("no hint for a normal or unusual-but-real domain", async () => {
    serve(() => ({ status: 202, body: { ok: true } }));
    render(<Waitlist />);
    await userEvent.type(screen.getByLabelText(AUTH.emailLabel), "me@mail.jiit.ac.in");
    expect(screen.queryByRole("button", { name: EMAIL_SUGGEST.use })).toBeNull();
  });
});
