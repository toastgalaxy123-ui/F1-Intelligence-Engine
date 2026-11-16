import { readFileSync } from "node:fs";
import { join } from "node:path";
import { defineConfig, devices } from "@playwright/test";

const stage = process.env.F1RI_STAGE_DIR;
if (!stage) throw new Error("Set F1RI_STAGE_DIR to a prepared staging directory.");
const spki = readFileSync(join(stage, "edge-spki.txt"), "utf8").trim();
export default defineConfig({
  testDir: "./tests-private",
  outputDir: "./test-results-private",
  workers: 1,
  timeout: 100_000,
  expect: { timeout: 22_000 },
  reporter: process.env.F1RI_PRIVATE_BROWSER_REPORT ?
    [["list"], ["json", { outputFile: process.env.F1RI_PRIVATE_BROWSER_REPORT }]] : "list",
  use: { ...devices["Desktop Chrome"], channel: "chrome", baseURL: process.env.F1RI_PRIVATE_ORIGIN || "https://localhost:8443",
    viewport: { width: 1440, height: 1000 }, actionTimeout: 18_000, ignoreHTTPSErrors: true,
    launchOptions: { args: [`--ignore-certificate-errors-spki-list=${spki}`] }, trace: "off", video: "off" },
});
