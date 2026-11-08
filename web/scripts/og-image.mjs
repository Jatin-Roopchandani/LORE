// Draws public/og.png (1200 x 630), the link-preview image for the public pages, from the homepage hero: the night
// sky, the wordmark, the headline and the chapters 1-2 constellation with its answer path lit. Run from web/:
//   node scripts/og-image.mjs
// It uses the bundled @fontsource fonts and the Playwright Chromium already installed for the e2e tests; nothing is
// fetched from the network.
import { chromium } from "@playwright/test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const font = (pkg, file) => `data:font/woff2;base64,${readFileSync(fileURLToPath(new URL(`../node_modules/@fontsource/${pkg}/files/${file}`, import.meta.url))).toString("base64")}`;
const faces = [
  ["Public Sans", 700, font("public-sans", "public-sans-latin-700-normal.woff2")],
  ["Public Sans", 500, font("public-sans", "public-sans-latin-500-normal.woff2")],
  ["Newsreader", 600, font("newsreader", "newsreader-latin-600-normal.woff2")],
].map(([f, w, src]) => `@font-face{font-family:"${f}";font-weight:${w};src:url(${src}) format("woff2")}`).join("");

const edges = [[150,250,120,110],[270,120,120,110],[270,120,480,110],[390,260,480,110],[150,250,70,370],[150,250,230,380],[150,250,40,290],[390,260,540,340],[150,250,540,340],[390,260,600,210],[600,210,640,90]];
const hot = [[150,250,270,120],[270,120,390,260],[390,260,150,250]];
const nodes = [[150,250,11,"#6FA4E4"],[270,120,8,"#6FA4E4"],[120,110,7,"#6CC1A0"],[480,110,7,"#6CC1A0"],[70,370,6,"#6CC1A0"],[230,380,6,"#6CC1A0"],[40,290,6,"#6FA4E4"],[540,340,8,"#6CC1A0"],[600,210,7,"#6FA4E4"],[640,90,6,"#BC8AD3"],[390,260,13,"#EBAA3C"]];
const labels = [[96,282,"John Watson"],[284,112,"Stamford"],[62,98,"Criterion Bar"],[420,96,"the hospital laboratory"],[82,374,"Afghanistan"],[242,384,"Battle of Maiwand"],[20,316,"Murray"],[554,344,"221B Baker Street"],[614,214,"Lestrade"],[600,78,"Scotland Yard"],[406,286,"Sherlock Holmes"]];
const tags = [[148,176,"meets again"],[338,180,"introduces"],[214,244,"shares rooms with"]];
const line = ([a, b, c, d], cls) => `<line class="${cls}" x1="${a}" y1="${b}" x2="${c}" y2="${d}"/>`;

const html = `<!doctype html><meta charset="utf-8"><style>${faces}
*{margin:0;box-sizing:border-box}
body{width:1200px;height:630px;overflow:hidden;color:#E3E9EF;font-family:"Public Sans";
  background:radial-gradient(900px 500px at 80% 35%,rgb(120 174 234 / .13),transparent 60%),radial-gradient(700px 420px at 5% 100%,rgb(235 170 60 / .09),transparent 60%),#0B1117}
.text{position:absolute;left:72px;top:70px;width:560px}
.brand{display:flex;align-items:center;gap:14px;margin-bottom:54px}
.brand b{font:600 34px/1 "Newsreader";letter-spacing:.06em}
.brand small{font:500 13px/1 "Public Sans";letter-spacing:.16em;text-transform:uppercase;color:#9AA7B4}
h1{font:700 58px/1.06 "Public Sans";letter-spacing:-.025em;margin-bottom:28px}
h1 span{color:#EBAA3C}
p{font:500 23px/1.45 "Public Sans";color:#9AA7B4}
svg.c{position:absolute;right:64px;top:150px;width:560px;overflow:visible}
.e{stroke:#2E3B48;stroke-width:1.3}.hot{stroke:#EBAA3C;stroke-width:2.6}
text{font:500 13px "Public Sans";fill:#C9D3DC}.tag{font-weight:700;font-size:11px;fill:#EBAA3C}
</style>
<div class="text">
  <div class="brand"><svg width="48" height="48" viewBox="0 0 64 64"><path d="M32 56 C24 51 14 50 6 52 V30 C14 28 24 29 32 34 C40 29 50 28 58 30 V52 C50 50 40 51 32 56 Z" fill="none" stroke="#78AEEA" stroke-width="3.4" stroke-linejoin="round"/><path d="M32 34 V56" stroke="#78AEEA" stroke-width="3.4"/><path d="M32 30 L16 14 M32 30 L32 8 M32 30 L48 14 M16 14 L32 8 L48 14" fill="none" stroke="#78AEEA" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><circle cx="32" cy="30" r="3.2" fill="#78AEEA"/><circle cx="16" cy="14" r="3.4" fill="#78AEEA"/><circle cx="32" cy="8" r="3.8" fill="#EBAA3C"/><circle cx="48" cy="14" r="3.4" fill="#78AEEA"/></svg><b>LORE</b><small>Reader</small></div>
  <h1>Every story is a web.<br><span>LORE draws it.</span></h1>
  <p>Ask about the books you own. Answers from the text, every sentence cited.</p>
</div>
<svg class="c" viewBox="0 0 680 430">
  ${edges.map((e) => line(e, "e")).join("")}${hot.map((e) => line(e, "hot")).join("")}
  ${nodes.map(([x, y, r, f]) => `<circle cx="${x}" cy="${y}" r="${r}" fill="${f}"/>`).join("")}
  ${labels.map(([x, y, t]) => `<text x="${x}" y="${y}">${t}</text>`).join("")}
  ${tags.map(([x, y, t]) => `<text class="tag" x="${x}" y="${y}">${t}</text>`).join("")}
</svg>`;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1200, height: 630 } });
await page.setContent(html);
await page.evaluate(() => document.fonts.ready);
await page.screenshot({ path: fileURLToPath(new URL("../public/og.png", import.meta.url)) });
await browser.close();
