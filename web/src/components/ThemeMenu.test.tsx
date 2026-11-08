import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeGrid } from "./ThemeMenu";
import { applyTheme } from "../theme";
import { THEMES } from "../themes";

beforeEach(() => { localStorage.clear(); applyTheme("system"); localStorage.clear(); });

test("the grid offers every theme: System, Light and Dark first, then the dark and the light themes", () => {
  render(<ThemeGrid />);
  const grid = screen.getByRole("radiogroup", { name: "Theme" });
  const names = within(grid).getAllByRole("radio").map((e) => e.querySelector(".theme-name")!.textContent);
  expect(names.slice(0, 3)).toEqual(["System", "Light", "Dark"]);
  expect(within(grid).getAllByRole("group").map((g) => g.getAttribute("aria-label"))).toEqual(
    ["System, light and dark", "Dark themes", "Light themes"]);
  const light = within(within(grid).getByRole("group", { name: "Light themes" })).getAllByRole("radio")
    .map((e) => e.querySelector(".theme-name")!.textContent);
  expect(light).toEqual(["Catppuccin Latte", "Solarized Light", "Gruvbox Light", "Paper", "Rosé Pine Dawn"]);
  expect(names).toHaveLength(1 + THEMES.length);
  expect(new Set(names).size).toBe(names.length);
});

test("every card shows a preview", () => {
  render(<ThemeGrid />);
  for (const item of screen.getAllByRole("radio")) expect(item.querySelector(".theme-swatch.large")).not.toBeNull();
});

test("choosing applies the theme to <html> and persists it", async () => {
  const onChange = vi.fn();
  render(<ThemeGrid onChange={onChange} />);
  await userEvent.click(screen.getByRole("radio", { name: /Dracula/ }));
  expect(document.documentElement.dataset.theme).toBe("dracula");
  expect(localStorage.getItem("lore-theme-v2")).toBe("dracula");
  expect(screen.getByRole("radio", { name: /Dracula/ })).toHaveAttribute("aria-checked", "true");
  expect(onChange).toHaveBeenCalledWith("dracula");
});

test("keyboard: only the current card is in the tab order, arrows move and choose, Home and End jump", async () => {
  applyTheme("nord");
  render(<ThemeGrid />);
  const nord = screen.getByRole("radio", { name: /Nord/ });
  expect(nord).toHaveAttribute("tabindex", "0");
  expect(screen.getByRole("radio", { name: /Dracula/ })).toHaveAttribute("tabindex", "-1");
  nord.focus();
  await userEvent.keyboard("{Home}");
  expect(screen.getByRole("radio", { name: /^System/ })).toHaveFocus();
  expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  await userEvent.keyboard("{ArrowRight}");
  expect(screen.getByRole("radio", { name: /^Light/ })).toHaveFocus();
  expect(document.documentElement.dataset.theme).toBe("light");
  await userEvent.keyboard("{ArrowDown}");
  expect(document.documentElement.dataset.theme).toBe("dark");
  await userEvent.keyboard("{ArrowUp}{ArrowUp}{ArrowUp}");
  expect(document.documentElement.dataset.theme).toBe(THEMES.filter((t) => t.group === "light").at(-1)!.id);
  await userEvent.keyboard("{End}");
  expect(screen.getByRole("radio", { name: /Rosé Pine Dawn/ })).toHaveFocus();
});

test("choosing System removes the attribute", async () => {
  applyTheme("monokai");
  render(<ThemeGrid />);
  await userEvent.click(screen.getByRole("radio", { name: /^System/ }));
  expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
});
