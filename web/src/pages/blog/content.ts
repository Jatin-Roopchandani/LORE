/// <reference types="vite/client" />
import { marked } from "marked";

// The blog and the FAQ: Markdown files in web/content, each with a small front matter block (title, description,
// date). Bundled into the blog's own chunk, rendered the same way when pre-rendered (scripts/prerender.tsx) and in
// the browser, so the page hydrates without a difference.
export interface Meta { title: string; description: string; date: string }
export interface Post extends Meta { slug: string; html: string }
export interface FaqItem { q: string; html: string }
export interface Faq extends Meta { items: FaqItem[] }

const FRONT = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/;

export function parse(src: string): { meta: Meta; body: string } {
  const m = FRONT.exec(src);
  const fields: Record<string, string> = {};
  for (const line of (m?.[1] ?? "").split(/\r?\n/)) {
    const at = line.indexOf(":");
    if (at > 0) fields[line.slice(0, at).trim()] = line.slice(at + 1).trim();
  }
  for (const key of ["title", "description", "date"]) {
    if (!fields[key]) throw new Error(`front matter needs a ${key}`);
  }
  return { meta: { title: fields.title, description: fields.description, date: fields.date },
    body: m ? src.slice(m[0].length) : src };
}

export const toHtml = (md: string) => marked.parse(md, { async: false, gfm: true }) as string;

const slugOf = (path: string) => path.replace(/^.*\//, "").replace(/\.md$/, "");

export function postsFrom(files: Record<string, string>): Post[] {
  return Object.entries(files)
    .map(([path, src]) => { const { meta, body } = parse(src); return { ...meta, slug: slugOf(path), html: toHtml(body) }; })
    .sort((a, b) => (a.date === b.date ? a.title.localeCompare(b.title) : b.date.localeCompare(a.date)));
}

// The FAQ: each "## question" heading and the paragraphs under it.
export function faqFrom(src: string): Faq {
  const { meta, body } = parse(src);
  const items = body.split(/^## /m).slice(1).map((part) => {
    const nl = part.indexOf("\n");
    return { q: part.slice(0, nl).trim(), html: toHtml(part.slice(nl + 1).trim()) };
  });
  return { ...meta, items };
}

const files = import.meta.glob("../../../content/blog/*.md", { query: "?raw", import: "default", eager: true }) as
  Record<string, string>;
const faqFile = import.meta.glob("../../../content/faq.md", { query: "?raw", import: "default", eager: true }) as
  Record<string, string>;

export const POSTS: Post[] = postsFrom(files);
export const FAQ: Faq = faqFrom(Object.values(faqFile)[0]);

// "2026-11-11" -> "11 November 2026", the same wherever it renders (no time zone involved).
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
  "November", "December"];
export function longDate(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  return `${d} ${MONTHS[m - 1]} ${y}`;
}
