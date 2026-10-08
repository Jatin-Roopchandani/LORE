import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TopBar } from "./TopBar";
import { applyTheme } from "../theme";

vi.mock("../api", () => ({
  api: { quota: () => Promise.resolve({ error: { code: "x", message: "x" } }), mcp: () => Promise.resolve({ error: { code: "x", message: "x" } }) },
  isError: () => true,
}));

const bar = () => render(<TopBar books={[]} hiddenBooks={new Set()} onBooksChange={() => {}} showInternals={false} refreshKey={0} />);
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
  expect(screen.getByText(`lore ingest 'it'\\''s.epub'`)).toBeInTheDocument();
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
