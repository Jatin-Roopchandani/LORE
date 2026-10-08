import { expect, test } from "@playwright/test";

declare const process: { env: Record<string, string | undefined> };

// Opt-in: spends ~6 Gemini requests. Needs `lore ui` running on 8766 (built
// frontend), Ollama running CPU-only, and LORE_E2E=1.
// Also the visual check: full-page screenshots go to LORE_SHOTS.
test.skip(!process.env.LORE_E2E, "set LORE_E2E=1 to run (spends Gemini requests)");

const shots = process.env.LORE_SHOTS ?? "test-results/shots";
const shot = (name: string) => `${shots}/${name}`;

test("ask, watch, read, explore", async ({ page, context }) => {
  await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
  await page.goto("/");
  await page.getByLabel("Question").fill("How is Jefferson Hope connected to John Ferrier?");
  await page.getByRole("button", { name: "Ask" }).click();
  await expect(page.locator(".steps li.now").first()).toBeVisible();
  await expect(page.getByText("How this was answered")).toBeVisible({ timeout: 150_000 });
  const cite = page.locator(".cite").first();
  await cite.click();
  await expect(page.locator(".passage mark").first()).toBeVisible();
  await expect(page.locator(".gcanvas canvas").first()).toBeVisible();
  if (process.env.LORE_STATE) await context.storageState({ path: process.env.LORE_STATE });
  await page.waitForTimeout(1000);
  await page.screenshot({ path: shot("light.png"), fullPage: true });

  // Dark: same page, same answer, no new request.
  await page.evaluate(() => { document.documentElement.dataset.theme = "dark"; });
  await page.waitForTimeout(500);
  await page.screenshot({ path: shot("dark.png"), fullPage: true });
  await page.evaluate(() => { document.documentElement.dataset.theme = "light"; });

  // Node card: click grid points over the canvas until an entity card opens.
  // Node labels are drawn on a 2D canvas; each label's left edge sits just right of its node, so
  // cluster the label pixels and aim a hover-then-click a little left of each cluster.
  const card = page.locator(".card");
  const box = (await page.locator(".gcanvas").boundingBox())!;
  const targets = await page.evaluate(() => {
    const c = document.querySelector<HTMLCanvasElement>(".gcanvas canvas.sigma-labels")!;
    const k = c.width / c.getBoundingClientRect().width;
    const { data, width, height } = c.getContext("2d")!.getImageData(0, 0, c.width, c.height);
    const cell = 4, cols = Math.ceil(width / cell), rows = Math.ceil(height / cell);
    const on = new Uint8Array(cols * rows);
    for (let y = 0; y < height; y++) for (let x = 0; x < width; x++)
      if (data[(y * width + x) * 4 + 3] > 0) on[Math.floor(y / cell) * cols + Math.floor(x / cell)] = 1;
    const seen = new Uint8Array(cols * rows);
    const out: { x: number; y: number }[] = [];
    for (let i = 0; i < on.length; i++) {
      if (!on[i] || seen[i]) continue;
      const stack = [i]; seen[i] = 1;
      let minC = cols, sumR = 0, n = 0;
      while (stack.length) {
        const j = stack.pop()!; const cc = j % cols, rr = Math.floor(j / cols);
        minC = Math.min(minC, cc); sumR += rr; n++;
        for (let dr = -1; dr <= 1; dr++) for (let dc = -2; dc <= 2; dc++) {
          const r2 = rr + dr, c2 = cc + dc;
          if (r2 < 0 || c2 < 0 || r2 >= rows || c2 >= cols) continue;
          const j2 = r2 * cols + c2;
          if (on[j2] && !seen[j2]) { seen[j2] = 1; stack.push(j2); }
        }
      }
      out.push({ x: (minC * cell) / k - 11, y: ((sumR / n) * cell + cell / 2) / k });
    }
    return out;
  });
  let opened = false;
  for (const t of targets) {
    await page.mouse.move(box.x + t.x - 2, box.y + t.y);
    await page.mouse.move(box.x + t.x, box.y + t.y);
    await page.waitForTimeout(80);
    await page.mouse.down();
    await page.mouse.up();
    await page.waitForTimeout(150);
    if ((opened = await card.isVisible())) break;
  }
  expect(opened, "a click on a node should open an entity card").toBe(true);
  await page.waitForTimeout(800);
  await page.screenshot({ path: shot("card.png"), fullPage: true });
  const expand = page.getByRole("button", { name: "Expand neighbours" });
  await expand.click();
  await expect(expand).toBeEnabled({ timeout: 30_000 });
  await page.waitForTimeout(1000);
  await page.screenshot({ path: shot("expanded.png"), fullPage: true });

  // Phone width.
  await page.setViewportSize({ width: 400, height: 860 });
  await page.waitForTimeout(800);
  await page.screenshot({ path: shot("phone.png"), fullPage: true });
});
