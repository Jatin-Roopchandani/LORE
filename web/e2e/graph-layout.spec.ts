import { expect, test } from "@playwright/test";

declare const process: { env: Record<string, string | undefined> };

// Opt-in, no server or model needed: the graph harness (e2e/graph-harness) served by `npx vite --port 18766`.
// Screenshots of recorded answers' graphs at 1440x900 in a light and a dark theme, with the legibility metric
// (graphMetrics.ts) measured in the page: screenshots go to LORE_GRAPH_SHOTS as <tag>-<fixture>-<theme>.png, and
// each metric is printed as a METRICS line.
const dir = process.env.LORE_GRAPH_SHOTS;
test.skip(!dir, "set LORE_GRAPH_SHOTS=<dir> (and serve the harness on 18766)");
const base = process.env.LORE_HARNESS ?? "http://127.0.0.1:18766";
const tag = process.env.LORE_GRAPH_TAG ?? "shot";

// A big answer is drawn focused by default; `full` is the whole graph (the header's "Show all").
for (const fx of ["ogier", "dark", "battle", "two"]) for (const theme of ["light", "nord"]) for (const view of ["focused", "full", "before"]) {
  if (fx === "two" && view !== "focused") continue;   // a small answer has one view
  test(`${fx} ${view} in ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto(`${base}/e2e/graph-harness/index.html?fx=${fx}&theme=${theme}&view=${view}`);
    await expect.poll(() => page.evaluate(() => window.__lore?.ready), { timeout: 150_000 }).toBe(true);
    await page.waitForTimeout(1200);
    await page.screenshot({ path: `${dir}/${tag}-${fx}-${view}-${theme}.png` });
    const metrics = await page.evaluate(() => window.__lore.metrics(1));
    console.log(`METRICS ${tag} ${fx} ${view} ${theme} ${JSON.stringify(metrics)}`);
  });
}

// A deep answer's section brought forward (as when it is picked in the contents, or scrolled to).
for (const theme of ["light", "nord"]) {
  test(`ogier section 1 in ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto(`${base}/e2e/graph-harness/index.html?fx=ogier&theme=${theme}&section=1`);
    await expect.poll(() => page.evaluate(() => window.__lore?.ready), { timeout: 150_000 }).toBe(true);
    await page.waitForTimeout(1200);
    await page.screenshot({ path: `${dir}/${tag}-ogier-section1-${theme}.png` });
  });
}

// The hub in focus (pointer on The Dark One), and zoomed in on it: its edges come forward with their labels.
for (const theme of ["light", "nord"]) {
  test(`dark one focus and zoom in ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto(`${base}/e2e/graph-harness/index.html?fx=dark&theme=${theme}`);
    await expect.poll(() => page.evaluate(() => window.__lore?.ready), { timeout: 150_000 }).toBe(true);
    await page.waitForTimeout(1200);
    const hub = await page.evaluate(() => window.__lore.point("The Dark One"));
    expect(hub).not.toBeNull();
    await page.mouse.move(hub!.x, hub!.y);
    await page.waitForTimeout(500);
    await page.screenshot({ path: `${dir}/${tag}-dark-${theme}-hover.png` });
    console.log(`METRICS ${tag} dark-hover ${theme} ${JSON.stringify(await page.evaluate(() => window.__lore.metrics(1, "The Dark One")))}`);
    for (let i = 0; i < 4; i++) { await page.mouse.wheel(0, -240); await page.waitForTimeout(250); }
    await page.mouse.move(hub!.x + 300, hub!.y + 300);
    await page.waitForTimeout(800);
    await page.screenshot({ path: `${dir}/${tag}-dark-${theme}-zoom.png` });
  });
}
