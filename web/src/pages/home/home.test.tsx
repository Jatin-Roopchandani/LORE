import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Router from "../../Router";
import { HOME, LEGAL } from "../../messages";
import Home from "./Home";
import Legal from "./Legal";

type Call = { url: string; method: string; body: any };
function serve(answer: (url: string) => { status?: number; body?: unknown } | "offline") {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit = {}) => {
    calls.push({ url, method: init.method ?? "GET", body: init.body ? JSON.parse(init.body as string) : undefined });
    const a = answer(url);
    if (a === "offline") throw new TypeError("Failed to fetch");
    return new Response(JSON.stringify(a.body ?? {}), { status: a.status ?? 200 });
  }));
  return calls;
}
const motion = (reduced: boolean) => vi.stubGlobal("matchMedia", (q: string) => ({
  matches: q.includes("reduce") ? reduced : false, addEventListener() {}, removeEventListener() {},
}));
let scrollTo: ReturnType<typeof vi.fn>;
beforeEach(() => {
  scrollTo = vi.fn();
  vi.stubGlobal("scrollTo", scrollTo);
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => { cb(0); return 1; });
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); document.head.querySelector('meta[name="lore-signed-in"]')?.remove(); });

describe("the homepage", () => {
  test("draws every section in order, with the nav, the landmarks and the footer", () => {
    render(<Home signedIn={false} />);
    const nav = screen.getByRole("navigation", { name: HOME.navLabel });
    expect(within(nav).getByRole("link", { name: HOME.brandLabel })).toHaveAttribute("href", "#top");
    expect(within(nav).getByRole("link", { name: HOME.navHow })).toHaveAttribute("href", "#how");
    expect(within(nav).getByRole("link", { name: HOME.navBeta })).toHaveAttribute("href", "#beta");
    expect(within(nav).getByRole("link", { name: HOME.signIn })).toHaveAttribute("href", "/signin");
    expect(within(nav).getByRole("link", { name: HOME.requestInvite })).toHaveAttribute("href", "#beta");
    expect(screen.getByRole("main")).toBeInTheDocument();

    // Headings in order: one h1, then each section's h2 with its h3s under it.
    const headings = screen.getAllByRole("heading").map((h) => [Number(h.tagName[1]), h.textContent ?? ""] as const);
    expect(headings[0]).toEqual([1, `${HOME.headline}${HOME.headlineGlow}`]);
    const h2s = headings.filter(([level]) => level === 2).map(([, t]) => t);
    expect(h2s).toEqual([HOME.howTitle, HOME.whyTitle, HOME.betaTitle]);
    headings.reduce((prev, [level]) => { expect(level).toBeLessThanOrEqual(prev + 1); return level; }, 0 as number);

    expect(screen.getByText(HOME.lede)).toBeInTheDocument();
    expect(screen.getByRole("img", { name: HOME.heroGraphLabel })).toBeInTheDocument();
    expect(screen.getByText(HOME.heroQuestion)).toBeInTheDocument();
    for (const s of HOME.steps) expect(screen.getByText(s.h)).toBeInTheDocument();
    for (const f of HOME.features) expect(screen.getByRole("heading", { name: f.title })).toBeInTheDocument();
    for (const s of HOME.betaSteps) expect(screen.getByText(s.b, { selector: "b" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: HOME.writtenLabel })).toHaveTextContent(HOME.reading);

    const footer = screen.getByRole("contentinfo");
    expect(within(footer).getByText(HOME.copyright)).toBeInTheDocument();
    expect(within(footer).getByRole("link", { name: HOME.privacy })).toHaveAttribute("href", "/privacy");
    expect(within(footer).getByRole("link", { name: HOME.terms })).toHaveAttribute("href", "/terms");
    expect(within(footer).getByRole("link", { name: HOME.signIn })).toHaveAttribute("href", "/signin");
    expect(document.title).toBe(HOME.title);
  });

  test("a signed-in visitor gets Open LORE in place of Sign in and Request an invite", () => {
    render(<Home signedIn />);
    const nav = screen.getByRole("navigation", { name: HOME.navLabel });
    expect(within(nav).getByRole("link", { name: HOME.openLore })).toHaveAttribute("href", "/app");
    expect(within(nav).queryByRole("link", { name: HOME.signIn })).toBeNull();
    expect(within(nav).queryByRole("link", { name: HOME.requestInvite })).toBeNull();
    expect(screen.queryAllByRole("link", { name: HOME.requestInvite })).toHaveLength(0);
    expect(within(screen.getByRole("contentinfo")).getByRole("link", { name: HOME.openLore })).toHaveAttribute("href", "/app");
  });

  test("the server's signed-in mark is what decides, by default", () => {
    const meta = document.createElement("meta");
    meta.name = "lore-signed-in"; meta.content = "1";
    document.head.append(meta);
    render(<Home />);
    expect(screen.getAllByRole("link", { name: HOME.openLore }).length).toBeGreaterThan(0);
  });

  test("Request an invite scrolls to the form and puts the cursor in the email field", async () => {
    const into = vi.fn();
    Element.prototype.scrollIntoView = into;
    render(<Home signedIn={false} />);
    await userEvent.click(within(screen.getByRole("navigation")).getByRole("link", { name: HOME.requestInvite }));
    expect(into).toHaveBeenCalledWith({ behavior: "smooth", block: "start" });
    expect(screen.getByLabelText(HOME.emailLabel)).toHaveFocus();
  });
});

describe("the pinned answer scene", () => {
  function geometry(top: number) {
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 1000 });
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(5000);
    return vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ top } as DOMRect);
  }

  test("scrolling through it advances the step, and the change is read out", () => {
    const rect = geometry(0);
    render(<Home signedIn={false} />);
    const live = screen.getByTestId("how-live");
    expect(live).toHaveAttribute("aria-live", "polite");
    expect(live).toHaveTextContent(HOME.stepNow(1, 5, `${HOME.steps[0].n}. ${HOME.steps[0].h}`));
    rect.mockReturnValue({ top: -2500 } as DOMRect); // 2500 of 4000: step 4
    act(() => { window.dispatchEvent(new Event("scroll")); });
    expect(live).toHaveTextContent(HOME.stepNow(4, 5, `${HOME.steps[3].n}. ${HOME.steps[3].h}`));
    expect(screen.getByRole("button", { name: HOME.stepDot(4) })).toHaveAttribute("aria-current", "step");
    expect(screen.getByRole("button", { name: HOME.stepDot(1) })).not.toHaveAttribute("aria-current");
  });

  test("the step dots are labelled buttons that jump to their step; the arrow keys move between them", async () => {
    geometry(0);
    render(<Home signedIn={false} />);
    const dots = within(screen.getByRole("group", { name: HOME.stepsLabel })).getAllByRole("button");
    expect(dots.map((d) => d.getAttribute("aria-label"))).toEqual([1, 2, 3, 4, 5].map(HOME.stepDot));
    await userEvent.click(dots[2]); // step 3: the middle of its fifth of the 4000 px of travel
    expect(scrollTo).toHaveBeenLastCalledWith({ top: 4000 * (2.5 / 5), behavior: "smooth" });
    dots[0].focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(dots[1]).toHaveFocus();
    expect(scrollTo).toHaveBeenLastCalledWith({ top: 4000 * (1.5 / 5), behavior: "smooth" });
    await userEvent.keyboard("{End}");
    expect(dots[4]).toHaveFocus();
  });

  test("with reduced motion the dots jump without smooth scrolling, and the feature cards don't slide", async () => {
    motion(true);
    geometry(-1000);
    render(<Home signedIn={false} />);
    await userEvent.click(screen.getByRole("button", { name: HOME.stepDot(2) }));
    expect(scrollTo).toHaveBeenLastCalledWith(expect.objectContaining({ behavior: "instant" }));
    expect(screen.getByTestId("glide-track").style.transform).toBe("");
  });

  test("without reduced motion the feature track slides with the scroll", () => {
    motion(false);
    geometry(-2000);
    // The last card's right edge, from the track's left: 2400 px of cards in a 1400 px wide track.
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      return { top: -2000, left: 0, right: this.matches("article:last-child") ? 2400 : 0 } as DOMRect;
    });
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(1400);
    render(<Home signedIn={false} />);
    // progress 0.5 -> glide (0.5 - 0.08) / 0.84 = 0.5 of the 1000 px overflow
    expect(screen.getByTestId("glide-track").style.transform).toBe("translate3d(-500px,0,0)");
  });
});

describe("the waitlist form", () => {
  const fill = async (email: string, { consent = true, honeypot = "" } = {}) => {
    await userEvent.type(screen.getByLabelText(HOME.emailLabel), email);
    await userEvent.type(screen.getByLabelText(new RegExp(HOME.readingLabel.replace("?", "\\?"))), "Dune");
    if (honeypot) await userEvent.type(document.querySelector('input[name="website"]') as HTMLInputElement, honeypot);
    if (consent) await userEvent.click(screen.getByLabelText(HOME.consent));
    await userEvent.click(screen.getByRole("button", { name: HOME.submit }));
  };

  test("sends the email, the book, the consent and the empty honeypot, and says you're on the list", async () => {
    const calls = serve(() => ({ status: 202, body: { ok: true } }));
    render(<Home signedIn={false} />);
    await fill("  reader@example.org ");
    expect(calls).toEqual([{ url: "/api/waitlist", method: "POST", body: { email: "reader@example.org", reading: "Dune", consent: true, website: "" } }]);
    expect(await screen.findByRole("button", { name: HOME.done })).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent(HOME.done);
  });

  test("the honeypot goes too, when something fills it", async () => {
    const calls = serve(() => ({ status: 202, body: { ok: true } }));
    render(<Home signedIn={false} />);
    const hp = document.querySelector('input[name="website"]') as HTMLInputElement;
    expect(hp).toHaveAttribute("tabindex", "-1");
    expect(hp).toHaveAttribute("aria-hidden", "true");
    await fill("bot@example.org", { honeypot: "http://spam" });
    expect(calls[0].body.website).toBe("http://spam");
    expect(await screen.findByRole("button", { name: HOME.done })).toBeInTheDocument(); // 202 is always success
  });

  test("an address that isn't one is caught before anything is sent", async () => {
    const calls = serve(() => ({ status: 202 }));
    render(<Home signedIn={false} />);
    await fill("not-an-email");
    expect(calls).toHaveLength(0);
    expect(screen.getByRole("alert")).toHaveTextContent(HOME.invalidEmail);
    expect(screen.getByLabelText(HOME.emailLabel)).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText(HOME.emailLabel)).toHaveFocus();
  });

  test("the consent box must be ticked", async () => {
    const calls = serve(() => ({ status: 202 }));
    render(<Home signedIn={false} />);
    await fill("reader@example.org", { consent: false });
    expect(calls).toHaveLength(0);
    expect(screen.getByRole("alert")).toHaveTextContent(HOME.needConsent);
  });

  test("a rate limit gets a friendly message, and the form can be sent again", async () => {
    serve(() => ({ status: 429, body: { error: { code: "rate_limited", message: "slow down", retry_after: 60 } } }));
    render(<Home signedIn={false} />);
    await fill("reader@example.org");
    expect(await screen.findByRole("alert")).toHaveTextContent(HOME.rateLimited);
    expect(screen.getByRole("button", { name: HOME.submit })).toBeEnabled();
  });

  test("a bare 429 is a rate limit too", async () => {
    serve(() => ({ status: 429, body: { detail: "Too Many Requests" } }));
    render(<Home signedIn={false} />);
    await fill("reader@example.org");
    expect(await screen.findByRole("alert")).toHaveTextContent(HOME.rateLimited);
  });

  test("no network gets a friendly message", async () => {
    serve(() => "offline");
    render(<Home signedIn={false} />);
    await fill("reader@example.org");
    expect(await screen.findByRole("alert")).toHaveTextContent(HOME.offline);
  });

  test("the server refusing the address shows the address message", async () => {
    serve(() => ({ status: 422, body: { detail: [] } }));
    render(<Home signedIn={false} />);
    await fill("reader@example.org");
    expect(await screen.findByRole("alert")).toHaveTextContent(HOME.invalidEmail);
  });
});

describe("privacy and terms", () => {
  test("privacy says what is stored, where it goes, the cookies, deletion and whom to ask", () => {
    render(<Legal kind="privacy" signedIn={false} />);
    expect(screen.getByRole("heading", { level: 1, name: LEGAL.privacy.title })).toBeInTheDocument();
    for (const s of LEGAL.privacy.sections) expect(screen.getByRole("heading", { level: 2, name: s.h })).toBeInTheDocument();
    expect(screen.getByText(`${LEGAL.updatedLabel}`, { exact: false })).toHaveTextContent(LEGAL.updated);
    expect(screen.getByText(/30 days: your question/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: LEGAL.contact })).toHaveAttribute("href", `mailto:${LEGAL.contact}`);
    expect(document.title).toBe(LEGAL.privacy.docTitle);
    // The homepage's nav and footer, leading back to its sections.
    const nav = screen.getByRole("navigation", { name: HOME.navLabel });
    expect(within(nav).getByRole("link", { name: HOME.brandLabel })).toHaveAttribute("href", "/");
    expect(within(nav).getByRole("link", { name: HOME.requestInvite })).toHaveAttribute("href", "/#beta");
    expect(within(screen.getByRole("contentinfo")).getByRole("link", { name: HOME.terms })).toHaveAttribute("href", "/terms");
  });

  test("terms cover the beta, your books, your key, fair use and accounts", () => {
    render(<Legal kind="terms" signedIn />);
    expect(screen.getByRole("heading", { level: 1, name: LEGAL.terms.title })).toBeInTheDocument();
    for (const s of LEGAL.terms.sections) expect(screen.getByRole("heading", { level: 2, name: s.h })).toBeInTheDocument();
    expect(screen.getByText(/Upload only books you own, for your own personal use/)).toBeInTheDocument();
    expect(within(screen.getByRole("navigation")).getByRole("link", { name: HOME.openLore })).toHaveAttribute("href", "/app");
    expect(document.title).toBe(LEGAL.terms.docTitle);
  });

  test("the router draws them (and the homepage) in multi mode, from their own chunk", async () => {
    render(<Router pathname="/privacy" multi />);
    expect(await screen.findByRole("heading", { level: 1, name: LEGAL.privacy.title })).toBeInTheDocument();
  });
});

test("the router draws the homepage at / in multi mode", async () => {
  render(<Router pathname="/" multi />);
  expect(await screen.findByRole("heading", { level: 2, name: HOME.betaTitle })).toBeInTheDocument();
});
