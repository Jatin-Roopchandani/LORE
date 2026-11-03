/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { themeCss } from "./src/themes";

// Every theme becomes a [data-theme] block in the page head, so the theme chosen before first paint (see the small
// script in index.html) styles the very first frame.
const themes = { name: "lore-themes", transformIndexHtml: () => [{ tag: "style", attrs: { id: "lore-themes" }, children: themeCss(), injectTo: "head-prepend" as const }] };

export default defineConfig({
  plugins: [react(), themes],
  build: { outDir: "../lore/web/static", emptyOutDir: true },
  server: { port: 5173, proxy: { "/api": { target: "http://127.0.0.1:8766", changeOrigin: true } } },
  test: { environment: "jsdom", globals: true, setupFiles: ["src/setupTests.ts"], exclude: ["e2e/**", "node_modules/**"] },
});
