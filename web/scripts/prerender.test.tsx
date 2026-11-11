// @vitest-environment node
// The public pages pre-render with no document or window, as `npm run build` does.
import { HOME, LEGAL } from "../src/messages";
import { FAQ, POSTS } from "../src/pages/blog/content";
import { PAGES, blogIndex, headFor, render } from "./prerender";

test("the homepage, privacy and terms render to their text with no browser", async () => {
  const home = await render("/");
  expect(home).toContain(HOME.headline);
  expect(home).toContain(HOME.howTitle);
  expect(home).toContain(HOME.features[3].title);
  expect(home.startsWith("<!--$-->")).toBe(true); // the Suspense boundary main.tsx hydrates
  expect(home).toContain(HOME.signIn); // rendered signed out
  expect(await render("/privacy")).toContain(LEGAL.privacy.lede);
  expect(await render("/terms")).toContain(LEGAL.terms.title);
});

test("a page's head links its chunk's CSS and preloads its scripts, never the entry's", () => {
  const manifest = {
    "index.html": { file: "assets/index.js", css: ["assets/index.css"] },
    "src/pages/home/Home.tsx": { file: "assets/Home.js", imports: ["index.html", "_Chrome.js"] },
    "_Chrome.js": { file: "assets/Chrome.js", css: ["assets/Chrome.css"], imports: ["index.html"] },
  };
  expect(headFor(manifest, "src/pages/home/Home.tsx")).toBe(
    '<link rel="stylesheet" href="/assets/Chrome.css">'
    + '<link rel="modulepreload" href="/assets/Home.js"><link rel="modulepreload" href="/assets/Chrome.js">');
});

test("the blog, every post and the FAQ pre-render, and the index the server reads lists them", async () => {
  expect(Object.keys(PAGES)).toEqual(expect.arrayContaining(["/blog", "/faq", ...POSTS.map((p) => `/blog/${p.slug}`)]));
  expect(await render(`/blog/${POSTS[0].slug}`)).toContain(POSTS[0].title);
  expect(await render("/faq")).toContain(FAQ.items[0].q);
  const index = blogIndex();
  expect(index.posts.map((p) => p.slug)).toEqual(POSTS.map((p) => p.slug));
  // plain answers for the FAQPage JSON-LD: no tags, and no space left before punctuation by a removed link
  for (const item of index.faq.items) {
    expect(item.a).not.toMatch(/[<>]/);
    expect(item.a).not.toMatch(/ [.,;:]/);
  }
});
