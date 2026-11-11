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
import { FAQ, POSTS } from "../src/pages/blog/content";

const BLOG_CHUNK = "src/pages/blog/Blog.tsx";
// path -> file name under prerender/, and each page's lazy chunk (Router.tsx), whose CSS must be in the head so the
// pre-rendered HTML is styled at first paint.
export const PAGES: Record<string, { name: string; chunk: string }> = {
  "/": { name: "home", chunk: "src/pages/home/Home.tsx" },
  "/privacy": { name: "privacy", chunk: "src/pages/home/Legal.tsx" },
  "/terms": { name: "terms", chunk: "src/pages/home/Legal.tsx" },
  "/blog": { name: "blog", chunk: BLOG_CHUNK },
  "/faq": { name: "faq", chunk: BLOG_CHUNK },
  ...Object.fromEntries(POSTS.map((p) => [`/blog/${p.slug}`, { name: `blog/${p.slug}`, chunk: BLOG_CHUNK }])),
};

// What the server needs to know of the written pages (lore/web/public_pages.py): titles and descriptions for the
// head, dates for the sitemap, and the FAQ's questions and plain answers for its FAQPage JSON-LD.
// Block ends become spaces; inline tags (links, emphasis) just go, so no space lands before punctuation.
const plain = (html: string) => html.replace(/<\/(p|li|h\d|blockquote|pre)>/g, " ").replace(/<[^>]+>/g, "").replace(/&quot;/g, '"').replace(/&#39;/g, "'")
  .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();
export const blogIndex = () => ({
  posts: POSTS.map(({ slug, title, description, date }) => ({ slug, title, description, date })),
  faq: { title: FAQ.title, description: FAQ.description, date: FAQ.date,
    items: FAQ.items.map((i) => ({ q: i.q, a: plain(i.html) })) },
});

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
  mkdirSync(resolve(out, "blog"), { recursive: true });
  writeFileSync(resolve(out, "blog.json"), JSON.stringify(blogIndex(), null, 1));
  for (const [path, { name, chunk }] of Object.entries(PAGES)) {
    const html = await render(path);
    writeFileSync(resolve(out, `${name}.html`), html);
    writeFileSync(resolve(out, `${name}.head.html`), headFor(manifest, chunk));
    console.log(`prerendered ${path} -> prerender/${name}.html (${html.length} bytes)`);
  }
}
