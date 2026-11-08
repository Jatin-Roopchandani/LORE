import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TopBar } from "./TopBar";
import { applyTheme } from "../theme";

vi.mock("../api", () => ({
  api: { quota: () => Promise.resolve({ error: { code: "x", message: "x" } }), mcp: () => Promise.resolve({ error: { code: "x", message: "x" } }),
    readingPosition: () => Promise.resolve({ error: { code: "x", message: "x" } }),
    books: () => Promise.resolve({ error: { code: "x", message: "x" } }), worlds: () => Promise.resolve({ error: { code: "x", message: "x" } }) },
  authApi: { me: () => Promise.resolve({ error: { code: "x", message: "x" } }), logout: () => Promise.resolve({ ok: true }),
    providers: () => Promise.resolve({ error: { code: "x", message: "x" } }) },
  nav: { to: () => {} },
  isError: (x: unknown) => typeof x === "object" && x !== null && "error" in x,
}));

const worlds = [
  { id: 1, name: "Alpha Saga", books: [{ id: "a1", title: "Alpha One", series: "Alpha Saga", series_index: 1 }] },
  { id: 2, name: "Beta Realm", books: [] },
];
const bar = (extra = {}) => render(<TopBar books={[]} hiddenBooks={new Set()} onBooksChange={() => {}} showInternals={false} refreshKey={0}
  worlds={worlds} world="all" onWorldChange={() => {}} {...extra} />);
beforeEach(() => applyTheme("system"));

test("add dialog: zone focused on open, keyboard opens picker, Escape returns focus", async () => {
  bar();
  const add = screen.getByRole("button", { name: /Add book/ });
  await userEvent.click(add);
  const zone = screen.getByRole("button", { name: /Drop an EPUB or PDF/ });
  expect(zone).toHaveFocus();
  const input = document.getElementById("add-file") as HTMLInputElement;
  const click = vi.spyOn(input, "click");
  await userEvent.keyboard("{Enter}");
  expect(click).toHaveBeenCalled();
  await userEvent.keyboard("{Escape}");
  expect(add).toHaveFocus();
});

test("dialog shows a safely quoted command", async () => {
  bar();
  await userEvent.click(screen.getByRole("button", { name: /Add book/ }));
  const input = document.getElementById("add-file") as HTMLInputElement;
  await userEvent.upload(input, new File(["x"], "it's.epub"));
  expect(screen.getByText(`lore ingest 'it'\\''s.epub' --world 'it'\\''s'`)).toBeInTheDocument();
});

test("⚙ opens the Settings popup, whose Appearance tab sets the theme; Escape returns focus to ⚙", async () => {
  bar();
  expect(screen.queryByRole("button", { name: "Theme" })).toBeNull();  // the old dropdown is gone: themes live in Settings
  await userEvent.click(screen.getByRole("button", { name: "Settings" }));
  expect(screen.getByRole("dialog", { name: "Settings" })).toHaveAttribute("aria-modal", "true");
  await userEvent.click(screen.getByRole("tab", { name: "Appearance" }));
  await userEvent.click(screen.getByRole("radio", { name: /Neon/ }));
  expect(document.documentElement.dataset.theme).toBe("neon");
  await userEvent.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.getByRole("button", { name: "Settings" })).toHaveFocus();
});

test("local mode has no account menu; multi-user mode's has Settings and Sign out", async () => {
  const local = bar();
  expect(screen.queryByRole("button", { name: "Account menu" })).toBeNull();
  local.unmount();
  document.head.innerHTML = '<meta name="lore-multi" content="1">';
  try {
    render(<TopBar books={[]} hiddenBooks={new Set()} onBooksChange={() => {}} showInternals={false} refreshKey={0} />);
    await userEvent.click(screen.getByRole("button", { name: "Account menu" }));
    const menu = screen.getByRole("menu", { name: "Account menu" });
    expect(within(menu).getAllByRole("menuitem").map((m) => m.textContent)).toEqual(["Settings", "Sign out"]);
    await userEvent.click(within(menu).getByRole("menuitem", { name: "Settings" }));
    expect(screen.getByRole("dialog", { name: "Settings" })).toBeInTheDocument();
    expect(window.location.hash).toBe("#settings/account");
  } finally {
    document.head.innerHTML = "";
    history.replaceState(null, "", window.location.pathname);
  }
});

test("the add button uses a plain plus sign", () => {
  bar();
  expect(screen.getByRole("button", { name: /Add book/ }).textContent).toBe("+ Add book");
});

test("the world selector lists each world and All worlds, labelled, and reports a choice", async () => {
  const onWorldChange = vi.fn();
  bar({ onWorldChange, world: "2" });
  const select = screen.getByRole("combobox", { name: "World" });
  expect(select).toHaveValue("2");
  expect(Array.from((select as HTMLSelectElement).options).map((o) => [o.value, o.textContent])).toEqual(
    [["all", "All worlds"], ["1", "Alpha Saga"], ["2", "Beta Realm"]]);
  await userEvent.selectOptions(select, "Alpha Saga");
  expect(onWorldChange).toHaveBeenCalledWith("1");
});

const addFile = async (name: string, extra = {}) => {
  bar(extra);
  await userEvent.click(screen.getByRole("button", { name: /Add book/ }));
  await userEvent.upload(document.getElementById("add-file") as HTMLInputElement, new File(["x"], name));
};

test("add dialog: a file named after an existing world defaults to that world and says so in the command", async () => {
  await addFile("Alpha_Saga_3.epub");
  expect(screen.getByRole("combobox", { name: "World for this book" })).toHaveValue("1");
  expect(screen.getByText("lore ingest 'Alpha_Saga_3.epub' --world 'Alpha Saga'")).toBeInTheDocument();
  expect(screen.queryByRole("textbox", { name: "New world name" })).toBeNull();
});

test("add dialog: an unfamiliar file defaults to a new world named after it, which can be renamed", async () => {
  await addFile("Gamma_Isles.epub");
  expect(screen.getByRole("combobox", { name: "World for this book" })).toHaveValue("new");
  expect(screen.getByText("lore ingest 'Gamma_Isles.epub' --world 'Gamma Isles'")).toBeInTheDocument();
  const name = screen.getByRole("textbox", { name: "New world name" });
  await userEvent.clear(name);
  await userEvent.type(name, "Gamma Lands");
  expect(screen.getByText("lore ingest 'Gamma_Isles.epub' --world 'Gamma Lands'")).toBeInTheDocument();
});

test("add dialog: choosing another world changes the command", async () => {
  await addFile("Gamma_Isles.epub");
  await userEvent.selectOptions(screen.getByRole("combobox", { name: "World for this book" }), "Beta Realm");
  expect(screen.getByText("lore ingest 'Gamma_Isles.epub' --world 'Beta Realm'")).toBeInTheDocument();
});

test("after an upload the page refetches, and the success link opens the Library", async () => {
  const sent: { onload?: () => void; status?: number; responseText?: string }[] = [];
  vi.stubGlobal("XMLHttpRequest", class {
    upload = { onprogress: null }; status = 0; responseText = ""; onload: (() => void) | null = null;
    constructor() { sent.push(this as never); }
    open() {} setRequestHeader() {} send() {} abort() {}
  });
  const onLibraryChange = vi.fn();
  await addFile("Gamma_Isles.epub", { onLibraryChange });
  await userEvent.click(screen.getByRole("button", { name: "Upload" }));
  const xhr = sent[0];
  xhr.status = 202;
  xhr.responseText = JSON.stringify({ book_id: "b9", title: "Gamma", world: { id: 3, name: "Gamma Isles" }, filename: "Gamma_Isles.epub",
    processing: { state: "processing" } });
  await act(async () => xhr.onload!());
  expect(onLibraryChange).toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: "Open it in the Library" }));
  expect(screen.getByRole("dialog", { name: "Library" })).toBeInTheDocument();
  expect(screen.queryByRole("dialog", { name: "Add a book" })).toBeNull();
  vi.unstubAllGlobals();
});
