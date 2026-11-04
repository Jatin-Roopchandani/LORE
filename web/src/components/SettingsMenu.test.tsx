import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { nav } from "../api";
import { AUTH } from "../messages";
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
