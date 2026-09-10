import { defineConfig, devices } from "@playwright/test";
import path from "path";

declare const process: {
  env: Record<string, string | undefined>;
};

export default defineConfig({
  testDir: "./e2e",
  retries: 0,
  workers: 1,
  outputDir: process.env.EVIDENCE_DIR
    ? path.join(process.env.EVIDENCE_DIR, "playwright-results")
    : undefined,
  use: {
    baseURL: process.env.COMMONSBOOK_BASE_URL || process.env.APP_ORIGIN || "http://localhost:5173",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
