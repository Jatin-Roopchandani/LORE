import { applyTheme, loadTheme } from "./theme";

test("system removes data-theme; light and dark set it and persist", () => {
  applyTheme("dark");
  expect(document.documentElement.dataset.theme).toBe("dark");
  expect(loadTheme()).toBe("dark");
  applyTheme("system");
  expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  expect(loadTheme()).toBe("system");
});
