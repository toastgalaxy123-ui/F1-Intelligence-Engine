import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  workers: 1,
  timeout: 120_000,
  expect: { timeout: 20_000 },
  reporter: process.env.F1RI_UI_REPORT ? [["list"], ["json", { outputFile: process.env.F1RI_UI_REPORT }]] : "list",
  use: {
    ...devices["Desktop Chrome"],
    viewport: { width: 1440, height: 1000 },
    channel: process.env.F1RI_BROWSER_CHANNEL || "chrome",
    baseURL: process.env.F1RI_WEB_URL || "http://127.0.0.1:3000",
    acceptDownloads: true,
    trace: "retain-on-failure",
  },
});
