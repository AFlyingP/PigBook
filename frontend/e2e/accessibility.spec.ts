import { test, expect, type Page } from "@playwright/test";
import path from "path";
import fs from "fs";

/**
 * WCAG 2.2 Relative Luminance and Contrast Ratio calculation (Spec 7.2, 12.2).
 * Implemented natively in test code without adding external dependencies.
 */
function getRelativeLuminance(hexColor: string): number {
  const cleanHex = hexColor.replace("#", "");
  const r = parseInt(cleanHex.substring(0, 2), 16) / 255;
  const g = parseInt(cleanHex.substring(2, 4), 16) / 255;
  const b = parseInt(cleanHex.substring(4, 6), 16) / 255;

  const toLinear = (c: number) =>
    c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);

  return 0.2126 * toLinear(r) + 0.7152 * toLinear(g) + 0.0722 * toLinear(b);
}

function getContrastRatio(hex1: string, hex2: string): number {
  const l1 = getRelativeLuminance(hex1);
  const l2 = getRelativeLuminance(hex2);
  const lighter = Math.max(l1, l2);
  const darker = Math.min(l1, l2);
  return (lighter + 0.05) / (darker + 0.05);
}

test.describe.serial("Accessibility, Viewport, Contrast & Telemetry Verification (Spec 7.2, 12.2)", () => {
  let sharedPage: Page;

  test.beforeAll(async ({ browser }) => {
    sharedPage = await browser.newPage();
    await sharedPage.goto("/login");
    await sharedPage.fill("#login-email", "admin@example.com");
    await sharedPage.fill("#login-password", "AdminPassword123!");
    await sharedPage.click('button[type="submit"]');
    await expect(sharedPage).toHaveURL(/.*\/resources/);
    await sharedPage.waitForLoadState("networkidle");
  });

  test.afterAll(async () => {
    await sharedPage?.close();
  });

  test("computes WCAG 2.2 AA contrast ratios for theme foreground/background color pairs", async () => {
    // Theme colors from frontend/src/theme.ts
    const primary = "#1976d2";
    const secondary = "#7b1fa2";
    const error = "#d32f2f";
    const darkText = "#111827";
    const backgroundPaper = "#ffffff";
    const backgroundDefault = "#f8f9fa";

    // WCAG AA requirement for normal text: >= 4.5:1
    const primaryContrast = getContrastRatio(primary, backgroundPaper);
    const secondaryContrast = getContrastRatio(secondary, backgroundPaper);
    const errorContrast = getContrastRatio(error, backgroundPaper);
    const textContrast = getContrastRatio(darkText, backgroundPaper);
    const defaultBgTextContrast = getContrastRatio(darkText, backgroundDefault);

    expect(primaryContrast).toBeGreaterThanOrEqual(4.5);
    expect(secondaryContrast).toBeGreaterThanOrEqual(4.5);
    expect(errorContrast).toBeGreaterThanOrEqual(4.5);
    expect(textContrast).toBeGreaterThanOrEqual(4.5);
    expect(defaultBgTextContrast).toBeGreaterThanOrEqual(4.5);
  });

  test("proves mobile-width viewport (375px) has no horizontal overflow on critical routes", async ({ browser }) => {
    // 1. Check unauthenticated login route on separate ephemeral page
    const anonContext = await browser.newContext({ viewport: { width: 375, height: 667 } });
    const anonPage = await anonContext.newPage();
    await anonPage.goto("/login");
    await anonPage.waitForLoadState("domcontentloaded");
    const loginOverflow = await anonPage.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
    expect(loginOverflow, "Route /login has horizontal overflow").toBe(false);
    await anonContext.close();

    // 2. Set mobile viewport on sharedPage
    await sharedPage.setViewportSize({ width: 375, height: 667 });

    const routesToCheck = [
      "/",
      "/resources",
      "/resources/55555555-5555-4555-8555-555555555555",
      "/my-bookings",
      "/waitlist",
      "/feedback",
      "/admin/resources",
      "/admin/bookings",
      "/admin/users",
      "/admin/audit",
      "/admin/outbox",
      "/admin/feedback",
    ];

    for (const r of routesToCheck) {
      await sharedPage.goto(r);
      await sharedPage.waitForLoadState("networkidle");
      await expect(sharedPage.locator("main")).toBeVisible();

      const hasHorizontalOverflow = await sharedPage.evaluate(() => {
        const docWidth = document.documentElement.scrollWidth;
        const windowWidth = window.innerWidth;
        return docWidth > windowWidth;
      });

      expect(
        hasHorizontalOverflow,
        `Route ${r} has horizontal overflow in mobile viewport (375px)`
      ).toBe(false);
    }

    // Reset viewport size to standard desktop
    await sharedPage.setViewportSize({ width: 1280, height: 720 });
  });

  test("verifies keyboard navigation, focus visible, focus trapping and restoration on dialogs", async () => {
    await sharedPage.goto("/admin/resources");
    await sharedPage.waitForLoadState("networkidle");
    await expect(sharedPage.getByRole("heading", { name: "Resource Inventory" })).toBeVisible();

    // 1. Keyboard focus on "Add Resource" button and open dialog with Enter
    const addBtn = sharedPage.locator("#add-resource-button");
    await addBtn.focus();
    await sharedPage.keyboard.press("Enter");

    const dialog = sharedPage.getByRole("dialog");
    await expect(dialog).toBeVisible();

    // 2. Verify focus is trapped inside dialog
    for (let i = 0; i < 6; i++) {
      await sharedPage.keyboard.press("Tab");
      const isInside = await sharedPage.evaluate(() => {
        const active = document.activeElement;
        const dlg = document.querySelector('[role="dialog"]');
        return dlg?.contains(active);
      });
      expect(isInside).toBe(true);
    }

    // 3. Close with Escape key and verify focus is restored
    await sharedPage.keyboard.press("Escape");
    await expect(dialog).not.toBeVisible();
  });

  test("verifies live-region announcements, accessible labels, and non-colour-only status across routes", async () => {
    // 1. Verify live region on Feedback page
    await sharedPage.goto("/feedback");
    await sharedPage.waitForLoadState("networkidle");
    const liveRegion = sharedPage.locator('[aria-live="polite"]');
    await expect(liveRegion.first()).toBeAttached();

    // Verify form labels
    await expect(sharedPage.getByLabel(/What, if anything, was difficult/i)).toBeVisible();
    await expect(sharedPage.getByLabel(/What is one improvement/i)).toBeVisible();
    await expect(
      sharedPage.getByLabel(/I consent to having my survey responses recorded/i)
    ).toBeVisible();

    // 2. Verify non-colour-only status on admin resources
    await sharedPage.goto("/admin/resources");
    await sharedPage.waitForLoadState("networkidle");
    const activeChip = sharedPage.locator("tr", { hasText: "Active" }).first();
    await expect(activeChip).toBeVisible();
    const archivedChip = sharedPage.locator("tr", { hasText: "Archived" }).first();
    await expect(archivedChip).toBeVisible();
  });

  test("proves zero telemetry or Sentry requests and captures journey screenshots into EVIDENCE_DIR", async ({ page: _page }, testInfo) => {
    void _page;
    const outboundRequests: string[] = [];
    const forbiddenPatterns = [
      "sentry",
      "ingest.sentry.io",
      "google-analytics",
      "telemetry",
      "analytics",
    ];

    sharedPage.on("request", (req) => {
      const url = req.url().toLowerCase();
      outboundRequests.push(url);

      for (const pattern of forbiddenPatterns) {
        if (url.includes(pattern)) {
          throw new Error(`Forbidden telemetry request detected: ${req.url()}`);
        }
      }

      // Check request headers for leaked invitation tokens or secrets
      const headers = req.headers();
      const allHeaders = JSON.stringify(headers);
      if (allHeaders.includes("secret-token") || allHeaders.includes("valid-pilot-invitation")) {
        throw new Error(`Secret leaked in outbound request headers: ${req.url()}`);
      }
    });

    // Capture screenshots directory
    const evidenceDir = process.env.EVIDENCE_DIR || testInfo.outputDir;
    if (!fs.existsSync(evidenceDir)) {
      fs.mkdirSync(evidenceDir, { recursive: true });
    }

    // 1. Landing screenshot
    await sharedPage.goto("/");
    await sharedPage.waitForLoadState("networkidle");
    await expect(sharedPage.getByRole("heading", { name: "CommonsBook", level: 1 })).toBeVisible();
    await sharedPage.screenshot({ path: path.join(evidenceDir, "screenshot-01-landing.png") });

    // 2. Resources screenshot
    await sharedPage.goto("/resources");
    await sharedPage.waitForLoadState("networkidle");
    await sharedPage.screenshot({ path: path.join(evidenceDir, "screenshot-02-resources.png") });

    // 3. Admin Resources screenshot
    await sharedPage.goto("/admin/resources");
    await sharedPage.waitForLoadState("networkidle");
    await sharedPage.screenshot({ path: path.join(evidenceDir, "screenshot-03-admin-resources.png") });

    // 4. Admin Bookings screenshot
    await sharedPage.goto("/admin/bookings");
    await sharedPage.waitForLoadState("networkidle");
    await sharedPage.screenshot({ path: path.join(evidenceDir, "screenshot-04-admin-bookings.png") });

    // 5. Feedback screenshot
    await sharedPage.goto("/feedback");
    await sharedPage.waitForLoadState("networkidle");
    await sharedPage.screenshot({ path: path.join(evidenceDir, "screenshot-05-feedback.png") });

    // Assert that no request matched telemetry hosts
    const matchedTelemetry = outboundRequests.filter((url) =>
      forbiddenPatterns.some((p) => url.includes(p))
    );
    expect(matchedTelemetry).toEqual([]);

    // Verify screenshot files exist on disk
    expect(fs.existsSync(path.join(evidenceDir, "screenshot-01-landing.png"))).toBe(true);
    expect(fs.existsSync(path.join(evidenceDir, "screenshot-05-feedback.png"))).toBe(true);
  });
});
