// Release step 6 in the browser: deleting one's account, the admin's user controls, friendly errors that quote a
// reference, and a 429 from /api/ask shown in its own words.
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { nav, streamAsk } from "./api";
import { ACCOUNT_DELETE, ADMIN, ADMIN_USERS, errorMessage } from "./messages";
import { SettingsDialog } from "./components/SettingsDialog";
import { openSettings } from "./settings";
import { Admin } from "./pages/Admin";

type Call = { url: string; method: string; body: any };
function serve(handler: (url: string, method: string) => { status?: number; body: unknown }) {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = init.method ?? "GET";
    calls.push({ url, method, body: init.body ? JSON.parse(init.body as string) : undefined });
    const { status = 200, body } = handler(url, method);
    return new Response(JSON.stringify(body), { status });
  }));
  return calls;
}
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); document.head.innerHTML = ""; });

test("Delete my account needs the email typed, then posts it and leaves", async () => {
  document.head.innerHTML = '<meta name="lore-multi" content="1">';
  const calls = serve((url) => url === "/api/me"
    ? { body: { user: { id: "u", email: "me@example.org", name: null, role: "user" } } }
    : { body: { deleted: true } });
  const go = vi.spyOn(nav, "to").mockImplementation(() => {});
  render(<SettingsDialog />);
  act(() => openSettings("data"));
  await userEvent.click(await screen.findByRole("button", { name: ACCOUNT_DELETE.open }));
  const confirm = screen.getByRole("button", { name: ACCOUNT_DELETE.confirm });
  expect(confirm).toBeDisabled();
  const field = screen.getByLabelText(ACCOUNT_DELETE.confirmLabel("me@example.org"));
  await userEvent.type(field, "someone@else.org");
  expect(confirm).toBeDisabled();
  await userEvent.clear(field);
  await userEvent.type(field, "Me@Example.org");
  await userEvent.click(confirm);
  const post = calls.find((c) => c.url === "/api/me/delete")!;
  expect(post.method).toBe("POST");
  expect(post.body).toEqual({ confirm: "Me@Example.org" });
  expect(go).toHaveBeenCalledWith("/");
});

test("the admin disables, signs out and deletes a user, and sees the log", async () => {
  let status = "active";
  const calls = serve((url, method) => {
    if (url === "/api/admin/waitlist") return { body: { entries: [] } };
    if (url === "/api/admin/users") return { body: { users: [{ id: "u2", email: "bob@example.org", name: null, role: "user", status, created_at: "2026-11-01T00:00:00+00:00" }] } };
    if (url === "/api/admin/reports") return { body: { reports: [] } };
    if (url === "/api/admin/events") return { body: { events: [{ id: 1, at: "2026-11-05T10:00:00+00:00", admin_id: "r", admin: "root@example.org", action: "disable", target_user_id: "u2", detail: null }] } };
    if (url.endsWith("/disable")) { status = "disabled"; return { body: { user: { status } } }; }
    if (url.endsWith("/revoke-sessions")) return { body: { revoked: 1 } };
    if (method === "DELETE") return { body: { deleted: true } };
    return { body: {} };
  });
  render(<Admin />);
  const users = await screen.findByRole("table", { name: ADMIN.users });
  await userEvent.click(within(users).getByRole("button", { name: ADMIN_USERS.disable }));
  expect(await screen.findByText(ADMIN_USERS.disabled("bob@example.org"))).toBeInTheDocument();
  expect(await within(screen.getByRole("table", { name: ADMIN.users })).findByRole("button", { name: ADMIN_USERS.enable })).toBeInTheDocument();
  await userEvent.click(within(screen.getByRole("table", { name: ADMIN.users })).getByRole("button", { name: ADMIN_USERS.revoke }));
  await userEvent.click(within(screen.getByRole("table", { name: ADMIN.users })).getByRole("button", { name: ADMIN_USERS.remove }));
  expect(calls.some((c) => c.method === "DELETE")).toBe(false);  // a confirmation first
  await userEvent.click(screen.getByRole("button", { name: ADMIN_USERS.yes }));
  expect(calls.find((c) => c.method === "DELETE")!.url).toBe("/api/admin/users/u2");
  expect(calls.some((c) => c.url === "/api/admin/users/u2/revoke-sessions" && c.method === "POST")).toBe(true);
  expect(within(screen.getByRole("table", { name: ADMIN_USERS.events })).getByText("disable")).toBeInTheDocument();
});

test("an unknown error quotes the request id; a friendly one is not quoted twice", () => {
  expect(errorMessage({ code: "weird", message: "", request_id: "rid-1" }, { showInternals: false }).text)
    .toBe("Something went wrong. Reference: rid-1");
  expect(errorMessage({ code: "internal", message: "Something went wrong. Reference: rid-1", request_id: "rid-1" }, { showInternals: false }).text)
    .toBe("Something went wrong. Reference: rid-1");
  expect(errorMessage({ code: "unreachable", message: "" }, { showInternals: false }).text).not.toContain("lore ui");
});

test("a 429 from /api/ask becomes its own error, with retry", async () => {
  serve(() => ({ status: 429, body: { error: { code: "rate_limited", message: "You can ask 6 questions a minute. Please wait a moment.", retry_after: 30, request_id: "r9" }, retry_after: 30 } }));
  const events = [];
  for await (const e of streamAsk("q", new AbortController().signal)) events.push(e);
  expect(events).toHaveLength(1);
  const e = events[0] as { type: "error"; error: { code: string; message: string; request_id?: string } };
  expect(e.error.code).toBe("rate_limited");
  expect(e.error.request_id).toBe("r9");
  expect(errorMessage(e.error, { showInternals: false })).toMatchObject({ text: "You can ask 6 questions a minute. Please wait a moment.", retry: true });
});
