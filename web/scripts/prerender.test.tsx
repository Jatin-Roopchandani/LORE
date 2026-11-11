// @vitest-environment node
// The public pages pre-render with no document or window, as `npm run build` does.
import { HOME, LEGAL } from "../src/messages";
import { headFor, render } from "./prerender";

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
