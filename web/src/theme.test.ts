import { applyTheme, loadTheme } from "./theme";

beforeEach(() => { localStorage.clear(); applyTheme("system"); localStorage.clear(); });

test("system removes data-theme; a theme sets it and persists under lore-theme-v2", () => {
  applyTheme("dark");
  expect(document.documentElement.dataset.theme).toBe("dark");
  expect(localStorage.getItem("lore-theme-v2")).toBe("dark");
  expect(loadTheme()).toBe("dark");
  applyTheme("tokyo-night");
  expect(document.documentElement.dataset.theme).toBe("tokyo-night");
  expect(loadTheme()).toBe("tokyo-night");
  applyTheme("system");
  expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  expect(loadTheme()).toBe("system");
});

test.each(["light", "dark", "system"])("the old lore-theme value %s migrates to lore-theme-v2", (old) => {
  localStorage.setItem("lore-theme", old);
  expect(loadTheme()).toBe(old);
  expect(localStorage.getItem("lore-theme-v2")).toBe(old);
});

test("an unknown stored id falls back to system; v2 wins over the old key", () => {
  localStorage.setItem("lore-theme-v2", "no-such-theme");
  expect(loadTheme()).toBe("system");
  localStorage.setItem("lore-theme-v2", "nord");
  localStorage.setItem("lore-theme", "dark");
  expect(loadTheme()).toBe("nord");
});

test("applying an unknown id behaves as system", () => {
  applyTheme("dracula");
  applyTheme("bogus");
  expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
});
