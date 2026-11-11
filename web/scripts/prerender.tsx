// Pre-renders the public pages (/, /privacy, /terms) to HTML after `vite build`, so a crawler that runs no script
// (most AI search crawlers) reads the same page a visitor sees. The server puts each page's HTML inside #root and
// main.tsx hydrates it. Rendered signed out; a signed-in visitor's page re-renders on the client.
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Writable } from "node:stream";
import { StrictMode } from "react";
import { renderToPipeableStream } from "react-dom/server";
import Router from "../src/Router";

export const PAGES: Record<string, string> = { "/": "home", "/privacy": "privacy", "/terms": "terms" };
// Each page's lazy chunk (Router.tsx), whose CSS must be in the head so the pre-rendered HTML is styled at first paint.
const CHUNKS: Record<string, string> = { home: "src/pages/home/Home.tsx", privacy: "src/pages/home/Legal.tsx",
  terms: "src/pages/home/Legal.tsx" };

type Manifest = Record<string, { file: string; css?: string[]; imports?: string[] }>;

// The chunk's stylesheets and its scripts to preload, walking its static imports (the entry's own are in index.html).
export function headFor(manifest: Manifest, chunk: string): string {
  const css = new Set<string>(), js = new Set<string>(), seen = new Set<string>();
  const walk = (key: string) => {
    if (seen.has(key) || key === "index.html" || !manifest[key]) return;
    seen.add(key);
    js.add(manifest[key].file);
    manifest[key].css?.forEach((c) => css.add(c));
    manifest[key].imports?.forEach(walk);
  };
  walk(chunk);
  return [...[...css].map((c) => `<link rel="stylesheet" href="/${c}">`),
          ...[...js].map((f) => `<link rel="modulepreload" href="/${f}">`)].join("");
}

export function render(pathname: string): Promise<string> {
  return new Promise((done, fail) => {
    let html = "";
    const sink = new Writable({ write(chunk, _enc, next) { html += chunk.toString(); next(); } });
    sink.on("finish", () => done(html));
    const stream = renderToPipeableStream(<StrictMode><Router pathname={pathname} multi /></StrictMode>, {
      onAllReady() { stream.pipe(sink); },
      onShellError: fail,
      onError: fail,
    });
  });
}

const out = resolve(dirname(fileURLToPath(import.meta.url)), "../../lore/web/static/prerender");
if (process.argv.includes("--write")) {
  mkdirSync(out, { recursive: true });
  const manifest = JSON.parse(readFileSync(resolve(out, "../.vite/manifest.json"), "utf-8")) as Manifest;
  for (const [path, name] of Object.entries(PAGES)) {
    const html = await render(path);
    writeFileSync(resolve(out, `${name}.html`), html);
    writeFileSync(resolve(out, `${name}.head.html`), headFor(manifest, CHUNKS[name]));
    console.log(`prerendered ${path} -> prerender/${name}.html (${html.length} bytes)`);
  }
}
