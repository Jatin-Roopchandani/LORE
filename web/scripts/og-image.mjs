// Draws public/og.png (1200 x 630), the link-preview image for the public pages, by photographing the real homepage
// hero: the build's own page, served here exactly as the server serves it to a signed-out visitor (the pre-rendered
// HTML in #root, its stylesheet in the head), with motion reduced so the graph is drawn in full. Run from web/ after
// `npm run build`:
//   node scripts/og-image.mjs
// Then build again, so the new image is copied into the built site. It uses the Playwright Chromium already installed
// for the e2e tests; nothing is fetched from the network.
import { chromium } from "@playwright/test";
import { createServer } from "node:http";
import { existsSync, readFileSync } from "node:fs";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

const STATIC = fileURLToPath(new URL("../../lore/web/static/", import.meta.url));
const OUT = fileURLToPath(new URL("../public/og.png", import.meta.url));
const TYPES = { ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".png": "image/png",
  ".woff2": "font/woff2", ".woff": "font/woff", ".json": "application/json" };

if (!existsSync(join(STATIC, "prerender/home.html"))) {
  console.error("No pre-rendered homepage: run `npm run build` first.");
  process.exit(1);
}

// The homepage as lore/web/app.py serves it to a signed-out visitor (lore/web/public_pages.with_prerender).
const home = () => readFileSync(join(STATIC, "index.html"), "utf-8")
  .replace("</head>", `<meta name="lore-multi" content="1">${readFileSync(join(STATIC, "prerender/home.head.html"), "utf-8")}</head>`)
  .replace(/<div id="?root"?>\s*<\/div>/, `<div id="root">${readFileSync(join(STATIC, "prerender/home.html"), "utf-8")}</div>`);

const server = createServer((req, res) => {
  const path = decodeURIComponent((req.url ?? "/").split("?")[0]);
  if (path === "/") { res.writeHead(200, { "content-type": "text/html" }); res.end(home()); return; }
  const file = join(STATIC, normalize(path).replace(/^(\.\.[/\\])+/, ""));
  if (!file.startsWith(STATIC) || !existsSync(file)) { res.writeHead(404); res.end(); return; }
  res.writeHead(200, { "content-type": TYPES[extname(file)] ?? "application/octet-stream" });
  res.end(readFileSync(file));
});
await new Promise((ok) => server.listen(0, "127.0.0.1", ok));
const { port } = server.address();

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1200, height: 630 }, deviceScaleFactor: 1,
  reducedMotion: "reduce", colorScheme: "dark" });
await page.goto(`http://127.0.0.1:${port}/`, { waitUntil: "networkidle" });
await page.evaluate(() => document.fonts.ready);
// A still image has nothing to scroll.
await page.addStyleTag({ content: ".h-scroll-cue{display:none!important}" });
await page.waitForTimeout(1000);
await page.screenshot({ path: OUT });
await browser.close();
server.close();
console.log(`wrote ${OUT}`);
