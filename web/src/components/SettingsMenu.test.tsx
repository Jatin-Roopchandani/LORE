import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { nav } from "../api";
import { AUTH, KEYS } from "../messages";
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

// --- the Gemini key in the account menu ---

function serveKey(key: object, usage = { day: "2026-11-04", requests: 12, limit: 1000, models: {} }) {
  const calls: { url: string; method: string; body?: string }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit = {}) => {
    calls.push({ url, method: init.method ?? "GET", body: init.body as string | undefined });
    if (url === "/api/me") return new Response(JSON.stringify({ user: { id: "u", email: "me@example.org", name: null, role: "user" } }));
    if (url === "/api/me/usage") return new Response(JSON.stringify(usage));
    if (url === "/api/me/key" && init.method === "PUT") {
      key = { present: true, last4: "WXYZ", added_at: null, last_ok_at: null, last_error: null };
      return new Response(JSON.stringify({ ...key, check: "ok" }));
    }
    if (url === "/api/me/key" && init.method === "DELETE") return new Response(JSON.stringify({ deleted: true }));
    if (url === "/api/me/key") return new Response(JSON.stringify(key));
    return new Response(JSON.stringify({ ok: true }));
  }));
  return calls;
}
const saved = { present: true, last4: "1234", added_at: "2026-11-04T12:00:00+00:00", last_ok_at: "2026-11-04T12:00:00+00:00", last_error: null };

test("the account menu shows the key's last four characters and today's requests, never the key", async () => {
  markMulti();
  serveKey(saved);
  render(<SettingsMenu showInternals={false} />);
  await userEvent.click(screen.getByRole("button", { name: "Settings" }));
  expect(await screen.findByText(KEYS.endingIn("1234"))).toBeInTheDocument();
  expect(screen.getByText(KEYS.usage(12, 1000))).toBeInTheDocument();
  expect(screen.getByText("Requests used today: 12 / about 1,000")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: KEYS.replace })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: KEYS.remove })).toBeInTheDocument();
});

test("Replace checks and saves a new key, then shows its last four", async () => {
  markMulti();
  const calls = serveKey(saved);
  render(<SettingsMenu showInternals={false} />);
  await userEvent.click(screen.getByRole("button", { name: "Settings" }));
  await userEvent.click(await screen.findByRole("button", { name: KEYS.replace }));
  await userEvent.type(screen.getByLabelText(KEYS.fieldLabel), "AIzaNEWnewNEWnewNEWnewNEWnewNEWnewWXYZ");
  await userEvent.click(screen.getByRole("button", { name: KEYS.save }));
  expect(await screen.findByText(KEYS.endingIn("WXYZ"))).toBeInTheDocument();
  const put = calls.find((c) => c.method === "PUT");
  expect(put?.url).toBe("/api/me/key");
  expect(JSON.parse(put!.body!)).toEqual({ key: "AIzaNEWnewNEWnewNEWnewNEWnewNEWnewWXYZ" });
});

test("Remove asks first, then deletes the key", async () => {
  markMulti();
  const calls = serveKey(saved);
  render(<SettingsMenu showInternals={false} />);
  await userEvent.click(screen.getByRole("button", { name: "Settings" }));
  await userEvent.click(await screen.findByRole("button", { name: KEYS.remove }));
  expect(calls.some((c) => c.method === "DELETE")).toBe(false);
  expect(screen.getByText(KEYS.removeConfirm)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: KEYS.removeYes }));
  expect(calls.some((c) => c.url === "/api/me/key" && c.method === "DELETE")).toBe(true);
});

test("a refused key is flagged in the menu", async () => {
  markMulti();
  serveKey({ ...saved, last_error: "rejected" });
  render(<SettingsMenu showInternals={false} />);
  await userEvent.click(screen.getByRole("button", { name: "Settings" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(KEYS.needsReplacing);
});
