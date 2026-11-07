import { errorMessage } from "./messages";
import { ago, applyMotion, hrefFor, loadMotion, parseHash, prefersReducedMotion } from "./settings";

test("parseHash: our hashes only; an unknown tab is the default tab", () => {
  expect(parseHash("#settings/providers")).toBe("providers");
  expect(parseHash("#settings")).toBe("");
  expect(parseHash("#settings/")).toBe("");
  expect(parseHash("#settings/nope")).toBe("");
  expect(parseHash("")).toBeNull();
  expect(parseHash("#other")).toBeNull();
  expect(parseHash("#settingsx")).toBeNull();
  expect(hrefFor("mcp")).toBe("#settings/mcp");
});

test("ago: minutes, hours, days, then a date", () => {
  const now = new Date("2026-11-07T12:00:00Z");
  expect(ago("2026-11-07T11:59:30Z", now)).toBe("just now");
  expect(ago("2026-11-07T11:55:00Z", now)).toBe("5 minutes ago");
  expect(ago("2026-11-07T09:00:00Z", now)).toBe("3 hours ago");
  expect(ago("2026-11-06T12:00:00Z", now)).toBe("yesterday");
  expect(ago("2026-11-01T12:00:00Z", now)).toBe("6 days ago");
  expect(ago("2026-08-01T12:00:00Z", now)).toBe("on Aug 1, 2026");
});

test("reduce motion: stored, marked on <html>, and honoured whatever the device says", () => {
  localStorage.clear();
  expect(loadMotion()).toBe("system");
  applyMotion("reduce");
  expect(loadMotion()).toBe("reduce");
  expect(prefersReducedMotion()).toBe(true);
  applyMotion("system");
  expect(document.documentElement.hasAttribute("data-motion")).toBe(false);
});

test("errors a Settings tab can fix link to it", () => {
  expect(errorMessage({ code: "key_required", message: "" }, { showInternals: false }).settings).toBe("providers");
  expect(errorMessage({ code: "key_invalid", message: "" }, { showInternals: false }).settings).toBe("providers");
  expect(errorMessage({ code: "budget_exhausted", message: "" }, { showInternals: false }).settings).toBe("usage");
  expect(errorMessage({ code: "busy", message: "" }, { showInternals: false }).settings).toBeUndefined();
});
