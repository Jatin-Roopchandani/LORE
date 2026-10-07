/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: { outDir: "../lore/web/static", emptyOutDir: true },
  server: { port: 5173, proxy: { "/api": { target: "http://127.0.0.1:8766", changeOrigin: true } } },
  test: { environment: "jsdom", globals: true, setupFiles: ["src/setupTests.ts"], exclude: ["e2e/**", "node_modules/**"] },
});
