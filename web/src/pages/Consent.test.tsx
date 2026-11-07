import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { nav } from "../api";
import { CONSENT } from "../messages";
import { pageFor } from "../routing";
import { Consent } from "./Consent";

type Call = { url: string; method: string; body?: unknown; headers: Record<string, string> };
function serve(handler: (url: string, init: RequestInit) => { status?: number; body: unknown }) {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit = {}) => {
    calls.push({ url, method: init.method ?? "GET", body: init.body ? JSON.parse(init.body as string) : undefined,
      headers: (init.headers ?? {}) as Record<string, string> });
    const { status = 200, body } = handler(url, init);
    return new Response(JSON.stringify(body), { status });
  }));
  return calls;
}
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); document.cookie = "lore_csrf=; path=/; max-age=0"; });

const INFO = { client_name: "Claude", redirect_host: "claude.ai" };
const REDIRECT = "https://claude.ai/api/mcp/auth_callback?code=abc&state=s";

test("/oauth/consent is the consent page in multi mode only", () => {
  expect(pageFor("/oauth/consent", true)).toBe("consent");
  expect(pageFor("/oauth/consent", false)).toBe("app");
});

test("names the app, says what it can and can't do, and where Allow goes", async () => {
  serve(() => ({ body: INFO }));
  render(<Consent search="?req=signed.request" />);
  expect(await screen.findByRole("heading", { level: 1, name: CONSENT.title("Claude") })).toBeInTheDocument();
  for (const line of CONSENT.can) expect(screen.getByText(line)).toBeInTheDocument();
  expect(screen.getByText(CONSENT.cannot)).toBeInTheDocument();
  expect(screen.getByText(CONSENT.back("claude.ai"))).toBeInTheDocument();
});

test("Allow posts the answer with the CSRF header and goes where the server says", async () => {
  document.cookie = "lore_csrf=tok; path=/";
  const to = vi.spyOn(nav, "to").mockImplementation(() => {});
  const calls = serve((url) => (url.startsWith("/api/oauth/consent?") ? { body: INFO } : { body: { redirect: REDIRECT } }));
  render(<Consent search="?req=signed.request" />);
  await userEvent.click(await screen.findByRole("button", { name: CONSENT.allow }));
  const post = calls.find((c) => c.method === "POST")!;
  expect(post.url).toBe("/api/oauth/consent");
  expect(post.body).toEqual({ req: "signed.request", approve: true });
  expect(post.headers["X-CSRF-Token"]).toBe("tok");
  expect(to).toHaveBeenCalledWith(REDIRECT);
});

test("Cancel answers no", async () => {
  const to = vi.spyOn(nav, "to").mockImplementation(() => {});
  const calls = serve((url) => (url.startsWith("/api/oauth/consent?") ? { body: INFO }
    : { body: { redirect: "https://claude.ai/cb?error=access_denied" } }));
  render(<Consent search="?req=r" />);
  await userEvent.click(await screen.findByRole("button", { name: CONSENT.cancel }));
  expect(calls.find((c) => c.method === "POST")!.body).toEqual({ req: "r", approve: false });
  expect(to).toHaveBeenCalledWith("https://claude.ai/cb?error=access_denied");
});

test("an expired or tampered request says so and offers no Allow", async () => {
  serve(() => ({ status: 400, body: { error: { code: "invalid_request", message: "" } } }));
  render(<Consent search="?req=old" />);
  expect(await screen.findByText(CONSENT.expired)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: CONSENT.allow })).toBeNull();
});

test("no request at all is the expired message, with no call", async () => {
  const calls = serve(() => ({ body: INFO }));
  render(<Consent search="" />);
  expect(screen.getByText(CONSENT.expired)).toBeInTheDocument();
  expect(calls).toEqual([]);
});

test("a client name is shown as text, never as markup", async () => {
  serve(() => ({ body: { client_name: "<img src=x onerror=alert(1)>", redirect_host: "claude.ai" } }));
  const { container } = render(<Consent search="?req=r" />);
  expect(await screen.findByRole("heading", { level: 1, name: CONSENT.title("<img src=x onerror=alert(1)>") })).toBeInTheDocument();
  expect(container.querySelector("img")).toBeNull();
});
