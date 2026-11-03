// @ts-expect-error -- node built-ins have no types in this project
import { readFileSync } from "node:fs";
import { THEMES, contrast, luminance, themeCss } from "./themes";

const css: string = readFileSync("src/theme.css", "utf8").replace(/\/\*[\s\S]*?\*\//g, "");   // vitest runs from web/
const rootBlock = css.match(/:root\s*\{([^}]*)\}/)![1];
// Every colour variable in the default theme: adding one there without a value in every theme must fail.
const TOKENS = [...rootBlock.matchAll(/(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{6})\b/g)].map((m) => m[1]);
const KINDS = ["character", "place", "faction", "object", "concept", "event"].map((k) => `--k-${k}`);

test("the token list is read from theme.css and includes the app's colours", () => {
  for (const t of ["--bg", "--panel", "--sunk", "--ink", "--muted", "--line", "--accent", "--accent-soft", "--glow", "--glow-soft", "--ok", "--warn", "--danger",
    "--edge", "--edge-faint", "--part-0", "--part-3", "--k-other", "--world-0", "--world-5", ...KINDS]) expect(TOKENS).toContain(t);
});

test("there are the 18 named themes plus Light and Dark, with unique ids", () => {
  expect(THEMES).toHaveLength(20);
  expect(new Set(THEMES.map((t) => t.id)).size).toBe(20);
  expect(THEMES.filter((t) => t.group === "dark")).toHaveLength(14);
  expect(THEMES.filter((t) => t.group === "light")).toHaveLength(6);
});

test("the light theme is the :root default in theme.css", () => {
  const light = THEMES.find((t) => t.id === "light")!;
  for (const m of rootBlock.matchAll(/(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{6})\b/g)) expect(light.tokens[m[1]].toLowerCase()).toBe(m[2].toLowerCase());
});

describe.each(THEMES.map((t) => [t.name, t] as const))("%s", (_name, theme) => {
  test("defines every token used in theme.css, as a hex colour, and nothing unknown", () => {
    for (const k of TOKENS) expect(theme.tokens[k], k).toMatch(/^#[0-9A-Fa-f]{6}$/);
    expect(Object.keys(theme.tokens).filter((k) => !TOKENS.includes(k))).toEqual([]);
  });
  test("text is readable: ink on bg and panel, muted on bg at least 4.5", () => {
    const t = theme.tokens;
    expect(contrast(t["--ink"], t["--bg"])).toBeGreaterThanOrEqual(4.5);
    expect(contrast(t["--ink"], t["--panel"])).toBeGreaterThanOrEqual(4.5);
    expect(contrast(t["--muted"], t["--bg"])).toBeGreaterThanOrEqual(4.5);
  });
  test("accent on bg at least 3", () => {
    expect(contrast(theme.tokens["--accent"], theme.tokens["--bg"])).toBeGreaterThanOrEqual(3);
  });
  test("the six kind colours differ in luminance by at least 0.05 pairwise", () => {
    const l = KINDS.map((k) => luminance(theme.tokens[k])).sort((a, b) => a - b);
    for (let i = 1; i < l.length; i++) expect(l[i] - l[i - 1]).toBeGreaterThanOrEqual(0.05 - 1e-3);
  });
});

test("generated css has a block per theme and the OS-dark default for system", () => {
  const out = themeCss();
  for (const t of THEMES) expect(out).toContain(`:root[data-theme="${t.id}"]{`);
  expect(out).toMatch(/@media \(prefers-color-scheme: dark\)\{:root:not\(\[data-theme\]\)\{--bg:#0E141A/);
  expect(out).toContain("color-scheme:dark");
});
