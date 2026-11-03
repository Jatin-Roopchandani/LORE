import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeMenu } from "./ThemeMenu";
import { applyTheme } from "../theme";

beforeEach(() => { localStorage.clear(); applyTheme("system"); localStorage.clear(); });
const open = async () => { render(<ThemeMenu />); await userEvent.click(screen.getByRole("button", { name: "Theme" })); };

test("the button opens a menu: System first, then a Dark group, then a Light group", async () => {
  await open();
  const menu = screen.getByRole("menu", { name: "Choose a theme" });
  const names = within(menu).getAllByRole("menuitemradio").map((e) => e.textContent);
  expect(names[0]).toMatch(/^System/);
  expect(within(menu).getAllByRole("group").map((g) => g.getAttribute("aria-label"))).toEqual(["Dark", "Light"]);
  const dark = within(within(menu).getByRole("group", { name: "Dark" })).getAllByRole("menuitemradio").map((e) => e.textContent);
  const light = within(within(menu).getByRole("group", { name: "Light" })).getAllByRole("menuitemradio").map((e) => e.textContent);
  expect(dark).toContain("Tokyo Night"); expect(dark).toContain("Neon"); expect(dark).toContain("High Contrast Dark");
  expect(light).toEqual(["Light", "Catppuccin Latte", "Solarized Light", "Gruvbox Light", "Paper", "Rosé Pine Dawn"]);
  expect(names).toHaveLength(1 + dark.length + light.length);
});

test("every entry shows a swatch", async () => {
  await open();
  for (const item of screen.getAllByRole("menuitemradio")) expect(item.querySelector(".theme-swatch")).not.toBeNull();
});

test("choosing applies the theme to <html>, persists it, closes the menu and refocuses the button", async () => {
  await open();
  await userEvent.click(screen.getByRole("menuitemradio", { name: "Dracula" }));
  expect(document.documentElement.dataset.theme).toBe("dracula");
  expect(localStorage.getItem("lore-theme-v2")).toBe("dracula");
  expect(screen.queryByRole("menu")).toBeNull();
  expect(screen.getByRole("button", { name: "Theme" })).toHaveFocus();
});

test("keyboard: opening focuses the current entry, arrows move, Enter picks, Escape closes", async () => {
  applyTheme("nord");
  render(<ThemeMenu />);
  const button = screen.getByRole("button", { name: "Theme" });
  button.focus();
  await userEvent.keyboard("{Enter}");
  expect(screen.getByRole("menuitemradio", { name: "Nord" })).toHaveFocus();
  expect(screen.getByRole("menuitemradio", { name: "Nord" })).toHaveAttribute("aria-checked", "true");
  await userEvent.keyboard("{ArrowDown}");
  expect(screen.getByRole("menuitemradio", { name: "Catppuccin Mocha" })).toHaveFocus();
  await userEvent.keyboard("{ArrowUp}{ArrowUp}");
  expect(screen.getByRole("menuitemradio", { name: "Dracula" })).toHaveFocus();
  await userEvent.keyboard("{Home}");
  expect(screen.getByRole("menuitemradio", { name: /^System/ })).toHaveFocus();
  await userEvent.keyboard("{ArrowDown}{Enter}");
  expect(document.documentElement.dataset.theme).toBe("dark");
  await userEvent.keyboard("{Enter}");
  await userEvent.keyboard("{Escape}");
  expect(screen.queryByRole("menu")).toBeNull();
  expect(button).toHaveFocus();
});

test("choosing System removes the attribute", async () => {
  applyTheme("monokai");
  await open();
  await userEvent.click(screen.getByRole("menuitemradio", { name: /^System/ }));
  expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
});
