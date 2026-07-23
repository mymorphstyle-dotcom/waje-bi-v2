import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/frontend",
  fullyParallel: true,
  // A cold Next dev server must finish compiling the customer bundle before the
  // suite fans out. Concurrent first-page loads can otherwise exercise only the
  // server-rendered recovery shell while Turbopack is still building its chunks.
  workers: 1,
  use: {
    baseURL: "http://127.0.0.1:3000",
    browserName: "chromium",
    colorScheme: "dark",
    locale: "zh-CN",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "npm run dev",
    url: "http://127.0.0.1:3000",
    reuseExistingServer: true,
    timeout: 120_000,
  },
});
