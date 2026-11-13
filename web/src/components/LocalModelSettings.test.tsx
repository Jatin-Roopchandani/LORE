import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LOCAL_MODEL } from "../messages";
import { LocalModelSettings } from "./LocalModelSettings";

// A fake local server for GET/PUT/DELETE /api/local-model and POST /api/local-model/test.
const toml = { provider: "gemini", model: null, fast_model: null, base_url: null };
const fresh = { active: { ...toml, source: "lore.toml" }, saved: null, file: toml, default_base_url: "http://localhost:8080/v1" };
type Call = { url: string; method: string; body?: Record<string, unknown> };
function serve(test: unknown = { ok: true, model: "qwen", seconds: 2.5 }, start: unknown = fresh) {
  const calls: Call[] = [];
  let state = start as typeof fresh;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = init.method ?? "GET";
    const body = init.body ? JSON.parse(init.body as string) : undefined;
    calls.push({ url, method, body });
    const json = (b: unknown, status = 200) => new Response(JSON.stringify(b), { status });
    if (url === "/api/local-model/test") return json(test);
    if (url === "/api/local-model" && method === "PUT") {
      const saved = { provider: "openai_compatible", model: body.model, fast_model: null, base_url: body.base_url,
        has_key: !!body.key };
      state = { ...state, active: { ...saved, source: "saved" }, saved } as unknown as typeof fresh;
      return json(state);
    }
    if (url === "/api/local-model" && method === "DELETE") { state = fresh; return json(state); }
    if (url === "/api/local-model") return json(state);
    return json({ error: { code: "not_found", message: "" } }, 404);
  }));
  return calls;
}
afterEach(() => { vi.unstubAllGlobals(); });

async function fill(model = "qwen", key = "") {
  render(<LocalModelSettings />);
  const address = await screen.findByLabelText(LOCAL_MODEL.addressLabel);
  expect(address).toHaveValue("http://localhost:8080/v1");  // the default address
  await userEvent.type(screen.getByLabelText(LOCAL_MODEL.modelLabel), model);
  if (key) await userEvent.type(screen.getByLabelText(LOCAL_MODEL.keyLabel), key);
}

test("says what runs now (lore.toml) and offers the Local model with an optional key", async () => {
  serve();
  render(<LocalModelSettings />);
  expect(await screen.findByTestId("local-active")).toHaveTextContent("Gemini");
  expect(screen.getByTestId("local-active")).toHaveTextContent(LOCAL_MODEL.source["lore.toml"]);
  expect(screen.getByLabelText(LOCAL_MODEL.keyLabel)).not.toBeRequired();
  expect(screen.getByRole("button", { name: LOCAL_MODEL.test })).toBeDisabled();  // no model named yet
});

test("Test connection: one call, and the success shown", async () => {
  const calls = serve({ ok: true, model: "qwen", seconds: 2.5 });
  await fill("qwen", "sekret");
  await userEvent.click(screen.getByRole("button", { name: LOCAL_MODEL.test }));
  expect(await screen.findByText(LOCAL_MODEL.testOk("qwen", 2.5))).toBeInTheDocument();
  const test = calls.find((c) => c.url === "/api/local-model/test")!;
  expect(test.body).toEqual({ base_url: "http://localhost:8080/v1", model: "qwen", key: "sekret" });
  expect(calls.some((c) => c.method === "PUT")).toBe(false);  // testing saves nothing
});

test("Test connection: the server's error in plain words", async () => {
  serve({ ok: false, message: "Couldn't reach a server at http://localhost:8080/v1. Check the address and that the server is running." });
  await fill();
  await userEvent.click(screen.getByRole("button", { name: LOCAL_MODEL.test }));
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(LOCAL_MODEL.testFailed);
  expect(alert).toHaveTextContent("Couldn't reach a server at http://localhost:8080/v1");
});

test("Use this model saves it as the active provider; Stop using it goes back to lore.toml", async () => {
  const calls = serve();
  await fill("qwen", "sekret");
  await userEvent.click(screen.getByRole("button", { name: LOCAL_MODEL.save }));
  expect(await screen.findByText(LOCAL_MODEL.saved)).toBeInTheDocument();
  expect(calls.find((c) => c.method === "PUT")!.body).toEqual({ base_url: "http://localhost:8080/v1", model: "qwen", key: "sekret" });
  const active = screen.getByTestId("local-active");
  expect(active).toHaveTextContent("Local model · qwen · at http://localhost:8080/v1");
  expect(active).toHaveTextContent(LOCAL_MODEL.source.saved);
  expect(screen.getByLabelText(LOCAL_MODEL.keyLabel)).toHaveValue("");  // the key is never shown back
  expect(screen.getByText(LOCAL_MODEL.keyKept, { exact: false })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: LOCAL_MODEL.remove }));
  expect(await screen.findByText(LOCAL_MODEL.removed)).toBeInTheDocument();
  expect(within(screen.getByTestId("local-active")).getByText(/Gemini/)).toBeInTheDocument();
});
