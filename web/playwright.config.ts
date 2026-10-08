import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "e2e",
  timeout: 240_000,
  use: { baseURL: "http://127.0.0.1:8766", viewport: { width: 1280, height: 800 } },
});
