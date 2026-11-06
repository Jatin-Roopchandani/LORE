/// <reference types="vitest" />
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import { themeCss } from "./src/themes";

// Every theme becomes a [data-theme] block in a stylesheet linked first in the page head, so the theme chosen before
// first paint (public/theme-boot.js) styles the very first frame. A file, not an inline <style>: the
// Content-Security-Policy allows styles from the site only (lore/web/headers.py). Built, it is assets/themes-<hash>.css;
// in dev the server answers /themes.css.
const THEMES_CSS = "themes.css";
let building = false;
const themes: Plugin = {
  name: "lore-themes",
  configResolved(config) { building = config.command === "build"; },
  configureServer(server) {
    server.middlewares.use(`/${THEMES_CSS}`, (_req, res) => { res.setHeader("Content-Type", "text/css"); res.end(themeCss()); });
  },
  buildStart() {
    if (building) this.emitFile({ type: "asset", name: THEMES_CSS, source: themeCss() });
  },
  transformIndexHtml: {
    order: "post",
    handler(_html, ctx) {
      const named = (f: object) => { const a = f as { type?: string; names?: string[]; name?: string }; return a.type === "asset" && (a.names ?? [a.name]).includes(THEMES_CSS); };
      const built = ctx.bundle ? Object.values(ctx.bundle).find(named) : undefined;
      const href = built ? `/${built.fileName}` : `/${THEMES_CSS}`;
      return [{ tag: "link", attrs: { rel: "stylesheet", id: "lore-themes", href }, injectTo: "head-prepend" as const }];
    },
  },
};

export default defineConfig({
  plugins: [react(), themes],
  // Fonts are never inlined as data: URIs (Vite inlines files under 4 KB), so a CSP of font-src 'self' loads them all.
  build: { outDir: "../lore/web/static", emptyOutDir: true,
    assetsInlineLimit: (file: string) => (/\.(woff2?|ttf|otf)$/.test(file) ? false : undefined) },
  server: { port: 5173, proxy: { "/api": { target: "http://127.0.0.1:8766", changeOrigin: true } } },
  test: { environment: "jsdom", globals: true, setupFiles: ["src/setupTests.ts"], exclude: ["e2e/**", "node_modules/**"] },
});
