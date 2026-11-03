// Themes as data. Each theme is a flat map of CSS variable name to colour; `themeCss()` turns the whole table into
// `[data-theme="id"]` blocks (injected into index.html at build time, see vite.config.ts), so choosing a theme is just
// setting one attribute on <html> and everything (graph, chips, headings, panels) follows through var(--token).
// Most schemes are written as a short "spec" (the scheme's published base colours); the rest is derived:
// soft tints by mixing, the six entity-kind colours by hue with their lightness tuned so kinds differ in luminance.

export type Group = "dark" | "light";
export type Tokens = Record<string, string>;
export interface Theme { id: string; name: string; group: Group; tokens: Tokens }

// ---- colour maths --------------------------------------------------------------------------------------------
const hex2rgb = (h: string): [number, number, number] => {
  const n = parseInt(h.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
};
const rgb2hex = (c: number[]) => "#" + c.map((x) => Math.round(Math.max(0, Math.min(255, x))).toString(16).padStart(2, "0")).join("").toUpperCase();
export function mix(a: string, b: string, t: number): string {
  const x = hex2rgb(a), y = hex2rgb(b);
  return rgb2hex(x.map((v, i) => v + (y[i] - v) * t));
}
export function luminance(h: string): number {
  const [r, g, b] = hex2rgb(h).map((v) => { const s = v / 255; return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4; });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
export function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((p, q) => q - p);
  return (hi + 0.05) / (lo + 0.05);
}
function hsl(h: string): [number, number, number] {
  const [r, g, b] = hex2rgb(h).map((v) => v / 255);
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2, d = mx - mn;
  if (d === 0) return [0, 0, l];
  const s = d / (1 - Math.abs(2 * l - 1));
  const hue = mx === r ? ((g - b) / d) % 6 : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return [(hue * 60 + 360) % 360, s, l];
}
function fromHsl(h: number, s: number, l: number): string {
  const c = (1 - Math.abs(2 * l - 1)) * s, x = c * (1 - Math.abs(((h / 60) % 2) - 1)), m = l - c / 2;
  const [r, g, b] = h < 60 ? [c, x, 0] : h < 120 ? [x, c, 0] : h < 180 ? [0, c, x] : h < 240 ? [0, x, c] : h < 300 ? [x, 0, c] : [c, 0, x];
  return rgb2hex([(r + m) * 255, (g + m) * 255, (b + m) * 255]);
}
// Same hue and saturation, lightness searched until the relative luminance reaches the target.
function withLuminance(color: string, target: number): string {
  const [h, s] = hsl(color);
  let lo = 0, hi = 1;
  for (let i = 0; i < 28; i++) {
    const mid = (lo + hi) / 2;
    if (luminance(fromHsl(h, s, mid)) < target) lo = mid; else hi = mid;
  }
  return fromHsl(h, s, (lo + hi) / 2);
}

// ---- derivation ----------------------------------------------------------------------------------------------
interface Spec {
  bg: string; panel: string; sunk: string; ink: string; muted: string; line: string;
  accent: string; glow: string; ok: string; warn: string; danger: string; edge: string;
  red: string; orange: string; yellow: string; green: string; cyan: string; blue: string; purple: string; pink: string;
}
const KINDS = ["character", "place", "faction", "object", "concept", "event"] as const;

type Hues = Record<(typeof KINDS)[number], string>;
// Kinds read as different on the canvas by lightness as well as hue: spread them evenly over the luminance range
// where a node still shows against the background (contrast about 2.4), brightest hue at the bright end.
function kindColours(group: Group, bg: string, hue: Hues): Tokens {
  const lb = luminance(bg);
  const lo = group === "dark" ? 2.5 * (lb + 0.05) - 0.05 : 0.04, hi = group === "dark" ? 0.72 : (lb + 0.05) / 2.4 - 0.05;
  const order = [...KINDS].sort((a, b) => luminance(hue[a]) - luminance(hue[b]));
  const kinds: Tokens = {};
  order.forEach((k, i) => { kinds[`--k-${k}`] = withLuminance(hue[k], lo + ((hi - lo) * i) / (KINDS.length - 1)); });
  return kinds;
}

function derive(group: Group, s: Spec): Tokens {
  const dark = group === "dark";
  const kinds = kindColours(group, s.bg, { character: s.blue, place: s.green, faction: s.purple, object: s.orange, concept: s.yellow, event: s.pink });
  const soft = dark ? 0.2 : 0.16;
  return {
    "--bg": s.bg, "--panel": s.panel, "--sunk": s.sunk, "--ink": s.ink, "--muted": s.muted, "--line": s.line,
    "--accent": s.accent, "--accent-soft": mix(s.bg, s.accent, soft), "--glow": s.glow, "--glow-soft": mix(s.bg, s.glow, soft),
    "--ok": s.ok, "--warn": s.warn, "--danger": s.danger,
    "--part-0": s.accent, "--part-1": s.purple, "--part-2": s.green, "--part-3": s.orange,
    ...kinds, "--k-other": s.edge,
    "--edge": s.edge, "--edge-faint": mix(s.bg, s.edge, 0.35),
    "--world-0": s.orange, "--world-1": s.cyan, "--world-2": s.purple, "--world-3": s.green, "--world-4": s.pink, "--world-5": s.blue,
  };
}
const scheme = (id: string, name: string, group: Group, s: Spec): Theme => ({ id, name, group, tokens: derive(group, s) });

// Today's two themes, kept literally except the six kind colours: those are re-tuned (same hues) so kinds differ in
// luminance by at least 0.05, which the old palettes did not guarantee.
const LIGHT0: Tokens = {
  "--bg": "#ECEFF2", "--panel": "#F8FAFB", "--sunk": "#E3E8EC", "--ink": "#17212B", "--muted": "#5A6774", "--line": "#D3DAE0",
  "--accent": "#1E5AA0", "--accent-soft": "#DCE7F4", "--glow": "#B97808", "--glow-soft": "#F6E7C8", "--ok": "#2E7D4F", "--warn": "#9A5B00", "--danger": "#B3261E",
  "--part-0": "#1E5AA0", "--part-1": "#8A4FA3", "--part-2": "#2E7D4F", "--part-3": "#B0662A",
  "--k-character": "#2F6DB5", "--k-place": "#3D8B6E", "--k-faction": "#8A4FA3", "--k-object": "#B0662A", "--k-concept": "#6B6F1E", "--k-event": "#B8456B", "--k-other": "#6C7A87",
  "--edge": "#9AA7B3", "--edge-faint": "#CBD3DA",
  "--world-0": "#C2410C", "--world-1": "#0E7490", "--world-2": "#7C3AED", "--world-3": "#4D7C0F", "--world-4": "#BE185D", "--world-5": "#1D4ED8",
};
const DARK0: Tokens = {
  "--bg": "#0E141A", "--panel": "#151D25", "--sunk": "#1B252F", "--ink": "#E3E9EF", "--muted": "#8C99A6", "--line": "#26323D",
  "--accent": "#78AEEA", "--accent-soft": "#1D3047", "--glow": "#EBAA3C", "--glow-soft": "#3A2D14", "--ok": "#6CC596", "--warn": "#E3A44B", "--danger": "#F2867E",
  "--part-0": "#78AEEA", "--part-1": "#BC8AD3", "--part-2": "#6CC596", "--part-3": "#E19A5C",
  "--k-character": "#6FA4E4", "--k-place": "#6CC1A0", "--k-faction": "#BC8AD3", "--k-object": "#E19A5C", "--k-concept": "#C2C66A", "--k-event": "#E58AA8", "--k-other": "#93A2B0",
  "--edge": "#5E6C79", "--edge-faint": "#2C3843",
  "--world-0": "#FB923C", "--world-1": "#22D3EE", "--world-2": "#A78BFA", "--world-3": "#A3E635", "--world-4": "#F472B6", "--world-5": "#60A5FA",
};

const retuned = (t: Tokens, group: Group): Tokens => ({
  ...t,
  ...kindColours(group, t["--bg"], { character: t["--k-character"], place: t["--k-place"], faction: t["--k-faction"], object: t["--k-object"], concept: t["--k-concept"], event: t["--k-event"] }),
});
const LIGHT = retuned(LIGHT0, "light"), DARK = retuned(DARK0, "dark");

export const THEMES: Theme[] = [
  { id: "light", name: "Light", group: "light", tokens: LIGHT },
  { id: "dark", name: "Dark", group: "dark", tokens: DARK },
  scheme("tokyo-night", "Tokyo Night", "dark", {
    bg: "#1A1B26", panel: "#1F2335", sunk: "#16161E", ink: "#C0CAF5", muted: "#8F98C4", line: "#2F334D",
    accent: "#7AA2F7", glow: "#E0AF68", ok: "#9ECE6A", warn: "#E0AF68", danger: "#F7768E", edge: "#565F89",
    red: "#F7768E", orange: "#FF9E64", yellow: "#E0AF68", green: "#9ECE6A", cyan: "#7DCFFF", blue: "#7AA2F7", purple: "#BB9AF7", pink: "#FF7EB6" }),
  // Synthwave. Hot pink and cyan are kept for accent and glow only; text stays a soft lavender so it passes contrast.
  scheme("neon", "Neon", "dark", {
    bg: "#0D0221", panel: "#150634", sunk: "#090118", ink: "#F3E8FF", muted: "#AE9ED3", line: "#34145F",
    accent: "#FF3DCB", glow: "#00E5FF", ok: "#3DE8A0", warn: "#FFD24A", danger: "#FF5577", edge: "#6A4FA0",
    red: "#FF4D6D", orange: "#FF9A3C", yellow: "#FFE14A", green: "#3DE8A0", cyan: "#00E5FF", blue: "#5C7CFF", purple: "#B967FF", pink: "#FF3DCB" }),
  scheme("dracula", "Dracula", "dark", {
    bg: "#282A36", panel: "#2F3243", sunk: "#21222C", ink: "#F8F8F2", muted: "#A9B2D6", line: "#44475A",
    accent: "#BD93F9", glow: "#F1FA8C", ok: "#50FA7B", warn: "#FFB86C", danger: "#FF5555", edge: "#6272A4",
    red: "#FF5555", orange: "#FFB86C", yellow: "#F1FA8C", green: "#50FA7B", cyan: "#8BE9FD", blue: "#8294E8", purple: "#BD93F9", pink: "#FF79C6" }),
  scheme("nord", "Nord", "dark", {
    bg: "#2E3440", panel: "#3B4252", sunk: "#272C36", ink: "#ECEFF4", muted: "#A9B4C8", line: "#4C566A",
    accent: "#88C0D0", glow: "#EBCB8B", ok: "#A3BE8C", warn: "#D08770", danger: "#BF616A", edge: "#616E88",
    red: "#BF616A", orange: "#D08770", yellow: "#EBCB8B", green: "#A3BE8C", cyan: "#8FBCBB", blue: "#81A1C1", purple: "#B48EAD", pink: "#D58DB8" }),
  scheme("catppuccin-mocha", "Catppuccin Mocha", "dark", {
    bg: "#1E1E2E", panel: "#27273A", sunk: "#181825", ink: "#CDD6F4", muted: "#A6ADC8", line: "#3A3C52",
    accent: "#89B4FA", glow: "#F9E2AF", ok: "#A6E3A1", warn: "#FAB387", danger: "#F38BA8", edge: "#6C7086",
    red: "#F38BA8", orange: "#FAB387", yellow: "#F9E2AF", green: "#A6E3A1", cyan: "#94E2D5", blue: "#89B4FA", purple: "#CBA6F7", pink: "#F5C2E7" }),
  scheme("gruvbox-dark", "Gruvbox Dark", "dark", {
    bg: "#282828", panel: "#32302F", sunk: "#1D2021", ink: "#EBDBB2", muted: "#A89984", line: "#504945",
    accent: "#83A598", glow: "#FABD2F", ok: "#B8BB26", warn: "#FE8019", danger: "#FB4934", edge: "#7C6F64",
    red: "#FB4934", orange: "#FE8019", yellow: "#FABD2F", green: "#B8BB26", cyan: "#8EC07C", blue: "#83A598", purple: "#D3869B", pink: "#F08CAE" }),
  scheme("one-dark", "One Dark", "dark", {
    bg: "#282C34", panel: "#2C313A", sunk: "#21252B", ink: "#ABB2BF", muted: "#8E96A6", line: "#3E4451",
    accent: "#61AFEF", glow: "#E5C07B", ok: "#98C379", warn: "#D19A66", danger: "#E06C75", edge: "#5C6370",
    red: "#E06C75", orange: "#D19A66", yellow: "#E5C07B", green: "#98C379", cyan: "#56B6C2", blue: "#61AFEF", purple: "#C678DD", pink: "#E68FB0" }),
  scheme("rose-pine", "Rosé Pine", "dark", {
    bg: "#191724", panel: "#1F1D2E", sunk: "#16141F", ink: "#E0DEF4", muted: "#908CAA", line: "#2A273F",
    accent: "#C4A7E7", glow: "#F6C177", ok: "#9CCFD8", warn: "#F6C177", danger: "#EB6F92", edge: "#6E6A86",
    red: "#EB6F92", orange: "#EA9A97", yellow: "#F6C177", green: "#7FBF9E", cyan: "#9CCFD8", blue: "#5AA0C0", purple: "#C4A7E7", pink: "#EBB4C8" }),
  scheme("kanagawa", "Kanagawa", "dark", {
    bg: "#1F1F28", panel: "#2A2A37", sunk: "#16161D", ink: "#DCD7BA", muted: "#A6A69C", line: "#363646",
    accent: "#7E9CD8", glow: "#E6C384", ok: "#98BB6C", warn: "#FFA066", danger: "#E46876", edge: "#54546D",
    red: "#E46876", orange: "#FFA066", yellow: "#E6C384", green: "#98BB6C", cyan: "#7FB4CA", blue: "#7E9CD8", purple: "#957FB8", pink: "#D27E99" }),
  scheme("everforest-dark", "Everforest Dark", "dark", {
    bg: "#2D353B", panel: "#343F44", sunk: "#232A2E", ink: "#D3C6AA", muted: "#9DA9A0", line: "#475258",
    accent: "#A7C080", glow: "#DBBC7F", ok: "#A7C080", warn: "#E69875", danger: "#E67E80", edge: "#5D6B66",
    red: "#E67E80", orange: "#E69875", yellow: "#DBBC7F", green: "#A7C080", cyan: "#83C092", blue: "#7FBBB3", purple: "#D699B6", pink: "#E6A0C0" }),
  scheme("monokai", "Monokai", "dark", {
    bg: "#272822", panel: "#31322C", sunk: "#1E1F1C", ink: "#F8F8F2", muted: "#A39E86", line: "#49483E",
    accent: "#66D9EF", glow: "#E6DB74", ok: "#A6E22E", warn: "#FD971F", danger: "#F92672", edge: "#75715E",
    red: "#F92672", orange: "#FD971F", yellow: "#E6DB74", green: "#A6E22E", cyan: "#66D9EF", blue: "#7A9CF5", purple: "#AE81FF", pink: "#FF6AA8" }),
  scheme("solarized-dark", "Solarized Dark", "dark", {
    bg: "#002B36", panel: "#073642", sunk: "#00212B", ink: "#A4B3B3", muted: "#8DA0A2", line: "#124A58",
    accent: "#2E96E0", glow: "#C99700", ok: "#859900", warn: "#CB4B16", danger: "#DC322F", edge: "#586E75",
    red: "#DC322F", orange: "#CB4B16", yellow: "#B58900", green: "#859900", cyan: "#2AA198", blue: "#268BD2", purple: "#6C71C4", pink: "#D33682" }),
  scheme("high-contrast-dark", "High Contrast Dark", "dark", {
    bg: "#000000", panel: "#0A0A0A", sunk: "#000000", ink: "#FFFFFF", muted: "#C8C8C8", line: "#6B6B6B",
    accent: "#4DC3FF", glow: "#FFD700", ok: "#5DFF8A", warn: "#FFB347", danger: "#FF6B6B", edge: "#9A9A9A",
    red: "#FF6B6B", orange: "#FFA040", yellow: "#FFE600", green: "#5DFF8A", cyan: "#4DFFFF", blue: "#5AA2FF", purple: "#C88BFF", pink: "#FF7AC8" }),
  scheme("catppuccin-latte", "Catppuccin Latte", "light", {
    bg: "#EFF1F5", panel: "#FBFBFD", sunk: "#E6E9EF", ink: "#4C4F69", muted: "#5C5F77", line: "#CCD0DA",
    accent: "#1E66F5", glow: "#DF8E1D", ok: "#40A02B", warn: "#C25A00", danger: "#D20F39", edge: "#9CA0B0",
    red: "#D20F39", orange: "#FE640B", yellow: "#DF8E1D", green: "#40A02B", cyan: "#179299", blue: "#1E66F5", purple: "#8839EF", pink: "#EA76CB" }),
  scheme("solarized-light", "Solarized Light", "light", {
    bg: "#FDF6E3", panel: "#FFFBF0", sunk: "#EEE8D5", ink: "#3F555C", muted: "#586E75", line: "#DDD6C1",
    accent: "#1F7FC0", glow: "#B58900", ok: "#6D7D00", warn: "#CB4B16", danger: "#DC322F", edge: "#93A1A1",
    red: "#DC322F", orange: "#CB4B16", yellow: "#B58900", green: "#859900", cyan: "#2AA198", blue: "#268BD2", purple: "#6C71C4", pink: "#D33682" }),
  scheme("gruvbox-light", "Gruvbox Light", "light", {
    bg: "#FBF1C7", panel: "#FDF6D8", sunk: "#EBDBB2", ink: "#3C3836", muted: "#665C54", line: "#D5C4A1",
    accent: "#076678", glow: "#B57614", ok: "#79740E", warn: "#AF3A03", danger: "#9D0006", edge: "#A89984",
    red: "#CC241D", orange: "#D65D0E", yellow: "#B57614", green: "#98971A", cyan: "#689D6A", blue: "#458588", purple: "#B16286", pink: "#D3869B" }),
  // A warm sepia for long reading: low glare, brown ink.
  scheme("paper", "Paper", "light", {
    bg: "#F4ECD8", panel: "#FBF6E9", sunk: "#E9DFC6", ink: "#3B3226", muted: "#625540", line: "#D8CBAB",
    accent: "#8C4A2F", glow: "#B5791A", ok: "#5A7A3A", warn: "#A8570F", danger: "#A32D24", edge: "#A99C7E",
    red: "#A32D24", orange: "#B8631C", yellow: "#A8821A", green: "#5A7A3A", cyan: "#2F7A78", blue: "#2F5F8F", purple: "#7A4F8A", pink: "#A8456A" }),
  scheme("rose-pine-dawn", "Rosé Pine Dawn", "light", {
    bg: "#FAF4ED", panel: "#FFFAF3", sunk: "#F2E9E1", ink: "#575279", muted: "#635F7E", line: "#DFDAD9",
    accent: "#286983", glow: "#EA9D34", ok: "#4F8A5F", warn: "#B86E1A", danger: "#B4637A", edge: "#9893A5",
    red: "#B4637A", orange: "#D7827E", yellow: "#EA9D34", green: "#5B8F6A", cyan: "#56949F", blue: "#286983", purple: "#907AA9", pink: "#C9728F" }),
];

export const DEFAULT_THEME = "light";                // the values in theme.css :root; also the base of "system" when the OS is light
export const THEME_IDS = ["system", ...THEMES.map((t) => t.id)];
export const themeById = (id: string): Theme | undefined => THEMES.find((t) => t.id === id);

const block = (selector: string, group: Group, tokens: Tokens) =>
  `${selector}{${Object.entries(tokens).map(([k, v]) => `${k}:${v}`).join(";")};color-scheme:${group}}`;

// One CSS block per theme, plus the dark theme for "system" when the OS prefers dark and no theme is chosen.
export function themeCss(): string {
  const dark = themeById("dark")!;
  return [
    ...THEMES.map((t) => block(`:root[data-theme="${t.id}"]`, t.group, t.tokens)),
    `@media (prefers-color-scheme: dark){${block(":root:not([data-theme])", "dark", dark.tokens)}}`,
  ].join("\n");
}
