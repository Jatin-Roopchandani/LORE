import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TopBar } from "./TopBar";
import { applyTheme } from "../theme";

vi.mock("../api", () => ({
  api: { quota: () => Promise.resolve({ error: { code: "x", message: "x" } }), mcp: () => Promise.resolve({ error: { code: "x", message: "x" } }) },
  isError: () => true,
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

test("settings reflects the theme toggled with the top bar button", async () => {
  bar();
  await userEvent.click(screen.getByRole("button", { name: "Switch light or dark" }));
  await userEvent.click(screen.getByRole("button", { name: "Settings" }));
  expect(screen.getByRole("button", { name: "Dark" })).toHaveAttribute("aria-pressed", "true");
  await userEvent.keyboard("{Escape}");
  expect(screen.getByRole("button", { name: "Settings" })).toHaveFocus();
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
