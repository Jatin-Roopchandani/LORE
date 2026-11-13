import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Router from "../Router";
import { nav } from "../api";
import { ServerProviderSettings } from "../components/ServerProviderSettings";
import { ChangePassword } from "../components/settings/AccountTab";
import { ADMIN, SELFHOST } from "../messages";
import { pageFor } from "../routing";
import { AddPerson, Admin } from "./Admin";
import { PasswordSignIn } from "./PasswordSignIn";
import { SetPassword } from "./SetPassword";
import { Setup } from "./Setup";

type Call = { url: string; method: string; body: any };
type Handler = (url: string, method: string, body: any) => { status?: number; body: unknown };
function serve(handler: Handler) {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = init.method ?? "GET";
    const body = init.body ? JSON.parse(init.body as string) : undefined;
    calls.push({ url, method, body });
    const { status = 200, body: out } = handler(url, method, body);
    return new Response(JSON.stringify(out), { status });
  }));
  return calls;
}
let went: string[] = [];
beforeEach(() => { went = []; vi.spyOn(nav, "to").mockImplementation((p: string) => { went.push(p); }); });
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

const err = (code: string, status = 400, message = "") => ({ status, body: { error: { code, message } } });

describe("routing", () => {
  test("the setup and set-password addresses have pages of their own", () => {
    expect(pageFor("/setup", true)).toBe("setup");
    expect(pageFor("/set-password", true)).toBe("setpassword");
  });

  test("a self-hosted server's sign-in page asks for a password, not a mailed link", async () => {
    serve(() => ({ body: {} }));
    render(<Router pathname="/signin" multi selfhost />);
    expect(screen.getByLabelText(SELFHOST.passwordLabel)).toBeInTheDocument();
    expect(screen.queryByText(/request an invite/i)).toBeNull();
  });
});

describe("first-run setup", () => {
  async function fill(password = "a long password", confirm = password, email = "owner@example.org") {
    render(<Setup />);
    await userEvent.type(screen.getByLabelText(SELFHOST.emailLabel), email);
    await userEvent.type(screen.getByLabelText(SELFHOST.passwordLabel), password);
    await userEvent.type(screen.getByLabelText(SELFHOST.confirmLabel), confirm);
    await userEvent.click(screen.getByRole("button", { name: SELFHOST.createAdmin }));
  }

  test("creates the admin and opens the app", async () => {
    const calls = serve(() => ({ status: 201, body: { ok: true, redirect: "/app" } }));
    await fill();
    await waitFor(() => expect(went).toEqual(["/app"]));
    expect(calls[0]).toMatchObject({ url: "/api/setup", method: "POST",
      body: { email: "owner@example.org", password: "a long password", confirm: "a long password" } });
    expect(screen.getByRole("heading", { level: 1, name: SELFHOST.setupTitle })).toBeInTheDocument();
  });

  test("checks the passwords match and are long enough before sending anything", async () => {
    const calls = serve(() => ({ body: {} }));
    await fill("a long password", "a different one");
    expect(await screen.findByRole("alert")).toHaveTextContent(SELFHOST.mismatch);
    await userEvent.clear(screen.getByLabelText(SELFHOST.passwordLabel));
    await userEvent.type(screen.getByLabelText(SELFHOST.passwordLabel), "short");
    await userEvent.clear(screen.getByLabelText(SELFHOST.confirmLabel));
    await userEvent.type(screen.getByLabelText(SELFHOST.confirmLabel), "short");
    await userEvent.click(screen.getByRole("button", { name: SELFHOST.createAdmin }));
    expect(await screen.findByRole("alert")).toHaveTextContent(SELFHOST.tooShort);
    expect(calls).toEqual([]);
  });

  test("when someone else got there first, it says so and offers sign-in", async () => {
    serve(() => err("setup_done", 409));
    await fill();
    expect(await screen.findByRole("heading", { name: SELFHOST.setupDone })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: SELFHOST.goSignIn })).toHaveAttribute("href", "/signin");
    expect(went).toEqual([]);
  });
});

describe("password sign-in", () => {
  async function signIn(password = "my password") {
    render(<PasswordSignIn />);
    await userEvent.type(screen.getByLabelText(SELFHOST.emailLabel), "me@example.org");
    await userEvent.type(screen.getByLabelText(SELFHOST.passwordLabel), password);
    await userEvent.click(screen.getByRole("button", { name: SELFHOST.signInButton }));
  }

  test("signs in and goes where the server says", async () => {
    const calls = serve(() => ({ body: { ok: true, redirect: "/app" } }));
    await signIn();
    await waitFor(() => expect(went).toEqual(["/app"]));
    expect(calls[0]).toMatchObject({ url: "/auth/password", body: { email: "me@example.org", password: "my password" } });
  });

  test("a wrong password says so and clears the field", async () => {
    serve(() => err("invalid_credentials", 401));
    await signIn();
    expect(await screen.findByRole("alert")).toHaveTextContent(SELFHOST.invalid);
    expect(screen.getByLabelText(SELFHOST.passwordLabel)).toHaveValue("");
  });

  test("too many tries says to wait", async () => {
    serve(() => err("rate_limited", 429));
    await signIn();
    expect(await screen.findByRole("alert")).toHaveTextContent(SELFHOST.rateLimited);
  });
});

describe("set-password link", () => {
  test("names the account, then saves the password and signs in", async () => {
    const calls = serve((_url, method) => method === "GET" ? { body: { email: "carol@example.org" } }
      : { body: { ok: true, redirect: "/app" } });
    render(<SetPassword search="?code=abc123abc123abc123" />);
    expect(await screen.findByText(SELFHOST.setLead("carol@example.org"))).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText(SELFHOST.newPasswordLabel), "carols password");
    await userEvent.type(screen.getByLabelText(SELFHOST.confirmLabel), "carols password");
    await userEvent.click(screen.getByRole("button", { name: SELFHOST.setButton }));
    await waitFor(() => expect(went).toEqual(["/app"]));
    expect(calls[1]).toMatchObject({ url: "/auth/password/link", body: { code: "abc123abc123abc123", password: "carols password" } });
  });

  test("a used or expired link says to ask for a new one", async () => {
    serve(() => err("link_invalid", 404));
    render(<SetPassword search="?code=abc123abc123abc123" />);
    expect(await screen.findByRole("alert")).toHaveTextContent(SELFHOST.linkInvalid);
  });
});

describe("the admin adds a person", () => {
  test("with a password", async () => {
    const onAdded = vi.fn();
    const calls = serve(() => ({ status: 201, body: { user: { id: "u1", email: "bob@example.org", name: null, role: "user",
      status: "active", created_at: "2026-11-13T00:00:00Z", has_password: true } } }));
    render(<AddPerson onAdded={onAdded} />);
    await userEvent.type(screen.getByLabelText(SELFHOST.personEmail), "bob@example.org");
    await userEvent.type(screen.getByLabelText(SELFHOST.personPassword), "bobs password");
    await userEvent.click(screen.getByRole("button", { name: SELFHOST.add }));
    expect(await screen.findByText(SELFHOST.added("bob@example.org"))).toBeInTheDocument();
    expect(calls[0]).toMatchObject({ url: "/api/admin/users", method: "POST", body: { email: "bob@example.org", password: "bobs password" } });
    expect(onAdded).toHaveBeenCalled();
  });

  test("without a password: a set-password link to copy, on this browser's own address", async () => {
    serve(() => ({ status: 201, body: { user: { id: "u2", email: "carol@example.org", name: null, role: "user",
      status: "active", created_at: "2026-11-13T00:00:00Z", has_password: false },
      link: { code: "CODE123", link: "http://localhost:8080/set-password?code=CODE123", expires_at: "x" } } }));
    render(<AddPerson onAdded={() => {}} />);
    await userEvent.type(screen.getByLabelText(SELFHOST.personEmail), "carol@example.org");
    await userEvent.click(screen.getByRole("button", { name: SELFHOST.add }));
    expect(await screen.findByText(SELFHOST.addedWithLink("carol@example.org"))).toBeInTheDocument();
    expect(screen.getByText(`${window.location.origin}/set-password?code=CODE123`)).toBeInTheDocument();
  });

  test("an address that already has an account is refused in words", async () => {
    serve(() => err("exists", 409));
    render(<AddPerson onAdded={() => {}} />);
    await userEvent.type(screen.getByLabelText(SELFHOST.personEmail), "alice@example.org");
    await userEvent.click(screen.getByRole("button", { name: SELFHOST.add }));
    expect(await screen.findByRole("alert")).toHaveTextContent(SELFHOST.exists);
  });

  test("the self-hosted admin page has people and the provider, and no waitlist", async () => {
    serve((url) => {
      if (url === "/api/admin/provider") return { body: { provider: null } };
      if (url === "/api/admin/users") return { body: { users: [{ id: "u2", email: "carol@example.org", name: null, role: "user",
        status: "active", created_at: "2026-11-13T00:00:00Z", has_password: false }] } };
      if (url === "/api/admin/waitlist") return { body: { entries: [] } };
      return { body: url.includes("events") ? { events: [] } : { reports: [] } };
    });
    render(<Admin selfhost />);
    expect(await screen.findByRole("heading", { name: SELFHOST.providerTitle })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: SELFHOST.people })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: ADMIN.waitlist })).toBeNull();
    expect(await screen.findByText(`(${SELFHOST.noPassword})`, { exact: false })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: SELFHOST.resetPassword })).toBeInTheDocument();
  });
});

describe("the AI provider for everyone", () => {
  test("a local model: address, model, Test connection, then Save", async () => {
    let saved: unknown = null;
    const calls = serve((url, method, body) => {
      if (url === "/api/admin/provider/test") return { body: { ok: true, model: body.model, seconds: 4.2 } };
      if (url === "/api/admin/provider" && method === "PUT") {
        saved = { provider: "openai_compatible", model: body.model, fast_model: null, base_url: body.base_url,
          has_key: false, last4: null, updated_at: "2026-11-13T00:00:00Z" };
        return { body: { provider: saved } };
      }
      return { body: { provider: saved } };
    });
    render(<ServerProviderSettings />);
    expect(await screen.findByTestId("server-provider-now")).toHaveTextContent(SELFHOST.providerNone);
    expect(screen.getByLabelText(SELFHOST.addressLabel)).toHaveValue("http://host.docker.internal:8080/v1");
    await userEvent.type(screen.getByLabelText(SELFHOST.modelLabel), "qwen3-30b");
    await userEvent.click(screen.getByRole("button", { name: SELFHOST.test }));
    expect(await screen.findByText(SELFHOST.testOk("qwen3-30b", 4.2))).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: SELFHOST.save }));
    expect(await screen.findByText(SELFHOST.saved)).toBeInTheDocument();
    expect(screen.getByTestId("server-provider-now")).toHaveTextContent("Local model (qwen3-30b)");
    const put = calls.find((c) => c.method === "PUT")!;
    expect(put.body).toEqual({ provider: "openai_compatible", model: "qwen3-30b", base_url: "http://host.docker.internal:8080/v1" });
  });

  test("a failed test says why", async () => {
    serve((url) => url.endsWith("/test") ? { body: { ok: false, message: "Couldn't reach a server." } } : { body: { provider: null } });
    render(<ServerProviderSettings />);
    await userEvent.type(await screen.findByLabelText(SELFHOST.modelLabel), "m");
    await userEvent.click(screen.getByRole("button", { name: SELFHOST.test }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't reach a server.");
  });

  test("a cloud provider needs a key, which is never shown back", async () => {
    const calls = serve((_url, method, body) => method === "PUT"
      ? { body: { provider: { provider: body.provider, model: null, fast_model: null, base_url: null, has_key: true, last4: "1234",
          updated_at: "t" } } }
      : { body: { provider: null } });
    render(<ServerProviderSettings />);
    await userEvent.selectOptions(await screen.findByLabelText(SELFHOST.providerLabel), "gemini");
    expect(screen.queryByLabelText(SELFHOST.addressLabel)).toBeNull();
    expect(screen.queryByRole("button", { name: SELFHOST.test })).toBeNull();
    expect(screen.getByRole("button", { name: SELFHOST.save })).toBeDisabled();
    await userEvent.type(screen.getByLabelText(SELFHOST.keyLabel), "AIzaSECRETsecretSECRETsecret1234");
    await userEvent.click(screen.getByRole("button", { name: SELFHOST.save }));
    expect(await screen.findByText(SELFHOST.keyKept("1234"))).toBeInTheDocument();
    expect(screen.getByLabelText(SELFHOST.keyLabel)).toHaveValue("");
    expect(calls.find((c) => c.method === "PUT")!.body).toEqual({ provider: "gemini", key: "AIzaSECRETsecretSECRETsecret1234" });
  });
});

describe("changing my password", () => {
  test("sends the current and the new one, and confirms", async () => {
    const calls = serve(() => ({ body: { ok: true } }));
    render(<ChangePassword />);
    await userEvent.type(screen.getByLabelText(SELFHOST.currentPasswordLabel), "old password");
    await userEvent.type(screen.getByLabelText(SELFHOST.newPasswordLabel), "new password!");
    await userEvent.type(screen.getByLabelText(SELFHOST.confirmLabel), "new password!");
    await userEvent.click(screen.getByRole("button", { name: SELFHOST.changePassword }));
    expect(await screen.findByText(SELFHOST.changed)).toBeInTheDocument();
    expect(calls[0]).toMatchObject({ url: "/api/me/password", body: { current: "old password", password: "new password!" } });
  });

  test("a wrong current password says so", async () => {
    serve(() => err("wrong_password"));
    render(<ChangePassword />);
    await userEvent.type(screen.getByLabelText(SELFHOST.currentPasswordLabel), "nope nope");
    await userEvent.type(screen.getByLabelText(SELFHOST.newPasswordLabel), "new password!");
    await userEvent.type(screen.getByLabelText(SELFHOST.confirmLabel), "new password!");
    await userEvent.click(screen.getByRole("button", { name: SELFHOST.changePassword }));
    expect(await screen.findByRole("alert")).toHaveTextContent(SELFHOST.wrongCurrent);
  });
});
