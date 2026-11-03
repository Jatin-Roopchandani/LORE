vi.mock("sigma", () => ({ default: class {} }));
vi.mock("@sigma/node-border", () => ({ createNodeBorderProgram: () => class {} }));
import { palette } from "./GraphCanvas";
import { themeById } from "../themes";

// jsdom has no stylesheet cascade for our generated blocks, so emulate getComputedStyle from the theme's tokens
// the way the browser resolves them from [data-theme].
test("palette() reads the selected theme's colours from the computed style", () => {
  const spy = vi.spyOn(window, "getComputedStyle").mockImplementation(() => {
    const id = document.documentElement.dataset.theme ?? "light";
    const tokens = themeById(id)!.tokens;
    return { getPropertyValue: (n: string) => ` ${tokens[n] ?? ""} ` } as unknown as CSSStyleDeclaration;
  });
  try {
    document.documentElement.dataset.theme = "tokyo-night";
    const p = palette();
    const t = themeById("tokyo-night")!.tokens;
    expect([p.ink, p.muted, p.edge, p.faint, p.glow, p.panel]).toEqual([t["--ink"], t["--muted"], t["--edge"], t["--edge-faint"], t["--glow"], t["--panel"]]);
    expect(p.v("--accent-soft")).toBe(t["--accent-soft"]);
    document.documentElement.dataset.theme = "paper";
    expect(palette().ink).toBe(themeById("paper")!.tokens["--ink"]);
    expect(palette().ink).not.toBe(p.ink);
  } finally { spy.mockRestore(); document.documentElement.removeAttribute("data-theme"); }
});
