import { test, expect, type Page } from "@playwright/test";
import path from "path";
import fs from "fs";
import { theme } from "../src/theme";

/**
 * WCAG 2.2 Relative Luminance and Contrast Ratio calculation (Spec 7.2, 12.2, R4).
 * Parses hex (#rrggbb) and rgb/rgba formats directly from theme tokens and computed styles.
 */
function parseColorToRgb(colorStr: string): { r: number; g: number; b: number; a: number } {
  const str = colorStr.trim();
  if (str.startsWith("#")) {
    const hex = str.replace("#", "");
    const r = parseInt(hex.substring(0, 2), 16);
    const g = parseInt(hex.substring(2, 4), 16);
    const b = parseInt(hex.substring(4, 6), 16);
    return { r, g, b, a: 1 };
  }

  const match = str.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)/);
  if (match) {
    return {
      r: parseInt(match[1], 10),
      g: parseInt(match[2], 10),
      b: parseInt(match[3], 10),
      a: match[4] !== undefined ? parseFloat(match[4]) : 1,
    };
  }

  return { r: 255, g: 255, b: 255, a: 1 };
}

function getRelativeLuminance(colorStr: string, bgStr = "#ffffff"): number {
  const fg = parseColorToRgb(colorStr);
  const bg = parseColorToRgb(bgStr);

  const r = (fg.r * fg.a + bg.r * (1 - fg.a)) / 255;
  const g = (fg.g * fg.a + bg.g * (1 - fg.a)) / 255;
  const b = (fg.b * fg.a + bg.b * (1 - fg.a)) / 255;

  const toLinear = (c: number) =>
    c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);

  return 0.2126 * toLinear(r) + 0.7152 * toLinear(g) + 0.0722 * toLinear(b);
}

function getContrastRatio(fgStr: string, bgStr: string): number {
  const l1 = getRelativeLuminance(fgStr, bgStr);
  const l2 = getRelativeLuminance(bgStr, "#ffffff");
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

  // R4: Contrast ratios derived from real theme tokens and computed styles
  test("computes WCAG 2.2 AA contrast ratios derived from theme tokens and computed styles (R4)", async () => {
    const primaryMain = theme.palette.primary.main;
    const primaryContrastText = theme.palette.primary.contrastText;
    const secondaryMain = theme.palette.secondary.main;
    const secondaryContrastText = theme.palette.secondary.contrastText;
    const errorMain = theme.palette.error.main;
    const errorContrastText = theme.palette.error.contrastText;
    const bgPaper = theme.palette.background.paper;
    const bgDefault = theme.palette.background.default;
    const textPrimary = theme.palette.text.primary;

    // WCAG AA requirement for normal text: >= 4.5:1
    expect(getContrastRatio(primaryContrastText, primaryMain)).toBeGreaterThanOrEqual(4.5);
    expect(getContrastRatio(primaryMain, bgPaper)).toBeGreaterThanOrEqual(4.5);
    expect(getContrastRatio(secondaryContrastText, secondaryMain)).toBeGreaterThanOrEqual(4.5);
    expect(getContrastRatio(secondaryMain, bgPaper)).toBeGreaterThanOrEqual(4.5);
    expect(getContrastRatio(errorContrastText, errorMain)).toBeGreaterThanOrEqual(4.5);
    expect(getContrastRatio(errorMain, bgPaper)).toBeGreaterThanOrEqual(4.5);
    expect(getContrastRatio(textPrimary, bgPaper)).toBeGreaterThanOrEqual(4.5);
    expect(getContrastRatio(textPrimary, bgDefault)).toBeGreaterThanOrEqual(4.5);

    // Also verify rendered computed styles from the live browser DOM
    await sharedPage.goto("/resources");
    await sharedPage.waitForLoadState("networkidle");

    const liveStyles = await sharedPage.evaluate(() => {
      const body = window.getComputedStyle(document.body);
      return {
        color: body.color,
        backgroundColor: body.backgroundColor,
      };
    });

    const liveContrast = getContrastRatio(liveStyles.color, liveStyles.backgroundColor);
    expect(liveContrast).toBeGreaterThanOrEqual(4.5);
  });

  // R1: Reduced-motion support implemented and behaviourally proven
  test("proves reduced-motion support suppresses transitions and animations under prefers-reduced-motion (R1)", async ({
    browser,
  }) => {
    // 1. Context with reduced-motion enabled
    const reducedContext = await browser.newContext({ reducedMotion: "reduce" });
    const reducedPage = await reducedContext.newPage();
    await reducedPage.goto("/login");
    await reducedPage.waitForLoadState("domcontentloaded");

    const reducedDuration = await reducedPage.evaluate(() => {
      const btn = document.querySelector('button[type="submit"]');
      if (!btn) return "none";
      const cs = window.getComputedStyle(btn);
      return cs.transitionDuration;
    });

    const durationSeconds = parseFloat(reducedDuration);
    expect(durationSeconds).toBeLessThanOrEqual(0.001);
    await reducedContext.close();

    // 2. Context without reduced-motion (no-preference) has non-zero transition duration
    const standardContext = await browser.newContext({ reducedMotion: "no-preference" });
    const standardPage = await standardContext.newPage();
    await standardPage.goto("/login");
    await standardPage.waitForLoadState("domcontentloaded");

    const standardDuration = await standardPage.evaluate(() => {
      const btn = document.querySelector('button[type="submit"]');
      if (!btn) return "0s";
      const cs = window.getComputedStyle(btn);
      return cs.transitionDuration;
    });

    const standardSeconds = parseFloat(standardDuration);
    expect(standardSeconds).toBeGreaterThan(0.01);
    await standardContext.close();
  });

  // R2: Focus visible indicator and focus restoration to opener on Escape and Cancel
  test("verifies visible focus indicator and focus restoration to opener on dialog close (R2)", async () => {
    await sharedPage.goto("/admin/resources");
    await sharedPage.waitForLoadState("networkidle");

    const addBtn = sharedPage.locator("#add-resource-button");
    await addBtn.focus();

    // 1. Assert visible focus indicator is active on the opener
    const isFocusVisible = await addBtn.evaluate((el) => {
      const cs = window.getComputedStyle(el);
      return (
        (cs.outlineStyle !== "none" && cs.outlineWidth !== "0px") ||
        (cs.boxShadow && cs.boxShadow !== "none")
      );
    });
    expect(isFocusVisible).toBe(true);

    // 2. Open dialog with Enter
    await sharedPage.keyboard.press("Enter");
    const dialog = sharedPage.getByRole("dialog");
    await expect(dialog).toBeVisible();

    // Verify focus is trapped inside dialog
    for (let i = 0; i < 4; i++) {
      await sharedPage.keyboard.press("Tab");
      const isInside = await sharedPage.evaluate(() => {
        const dlg = document.querySelector('[role="dialog"]');
        return dlg?.contains(document.activeElement);
      });
      expect(isInside).toBe(true);
    }

    // 3. Close with Escape: assert focus is restored to #add-resource-button
    await sharedPage.keyboard.press("Escape");
    await expect(dialog).not.toBeVisible();
    await expect(addBtn).toBeFocused();

    // 4. Open again and close via Cancel button: assert focus is restored to #add-resource-button
    await sharedPage.keyboard.press("Enter");
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(dialog).not.toBeVisible();
    await expect(addBtn).toBeFocused();
  });

  // R3: Complete critical-route sweep for keyboard traversal, accessible names, and live regions
  test("exercises keyboard traversal, accessible control names, and live regions across all critical routes (R3)", async () => {
    const criticalRoutes = [
      { path: "/login", requiresLive: false },
      { path: "/resources", requiresLive: true },
      { path: "/resources/55555555-5555-4555-8555-555555555555", requiresLive: false },
      { path: "/my-bookings", requiresLive: false },
      { path: "/waitlist", requiresLive: false },
      { path: "/feedback", requiresLive: true },
      { path: "/admin/resources", requiresLive: true },
      { path: "/admin/bookings", requiresLive: true },
      { path: "/admin/users", requiresLive: true },
      { path: "/admin/audit", requiresLive: false },
      { path: "/admin/outbox", requiresLive: true },
      { path: "/admin/feedback", requiresLive: false },
    ];

    for (const route of criticalRoutes) {
      await sharedPage.goto(route.path);
      await sharedPage.waitForLoadState("networkidle");

      // 1. Keyboard Tab traversal reaches an interactive element
      await sharedPage.keyboard.press("Tab");
      const hasFocusedEl = await sharedPage.evaluate(() => {
        const el = document.activeElement;
        return el !== null && el !== document.body;
      });
      expect(hasFocusedEl, `Keyboard tab from start reached no element on ${route.path}`).toBe(true);

      // 2. Every enabled interactive control exposes an accessible name
      const unnamedControls = await sharedPage.evaluate(() => {
        const controls = Array.from(
          document.querySelectorAll<HTMLElement>(
            'button:not([disabled]):not([aria-hidden="true"]), a[href]:not([aria-hidden="true"]), input:not([type="hidden"]):not([disabled]):not([aria-hidden="true"]), select:not([disabled]):not([aria-hidden="true"]), textarea:not([disabled]):not([aria-hidden="true"])'
          )
        );
        const missing: string[] = [];
        for (const el of controls) {
          const ariaLabel = el.getAttribute("aria-label")?.trim();
          const ariaLabelledBy = el.getAttribute("aria-labelledby");
          const title = el.getAttribute("title")?.trim();
          const placeholder = el.getAttribute("placeholder")?.trim();
          const text = el.innerText?.trim();
          const id = el.id;
          const label = id ? document.querySelector(`label[for="${id}"]`)?.textContent?.trim() : null;
          const parentLabel = el.closest("label")?.textContent?.trim();
          const val = (el as HTMLInputElement).value?.trim();

          const name =
            ariaLabel ||
            text ||
            label ||
            parentLabel ||
            title ||
            placeholder ||
            (el.tagName === "INPUT" && val) ||
            (ariaLabelledBy ? "has-labelledby" : null);

          if (!name) {
            missing.push(`${el.tagName.toLowerCase()}${id ? "#" + id : ""}`);
          }
        }
        return missing;
      });
      expect(unnamedControls, `Interactive controls missing accessible names on ${route.path}`).toEqual([]);

      // 3. Live region check on announcing routes
      if (route.requiresLive) {
        const liveRegionCount = await sharedPage.locator('[aria-live="polite"], [role="status"]').count();
        expect(
          liveRegionCount,
          `Expected at least one live announcement region on ${route.path}`
        ).toBeGreaterThanOrEqual(1);
      }
    }

    // 4. Non-colour-only status check: verify self-contained pre-seeded "Old Darkroom" is Archived
    await sharedPage.goto("/admin/resources");
    await sharedPage.waitForLoadState("networkidle");
    const darkroomRow = sharedPage.locator("tr", { hasText: "Old Darkroom" });
    await expect(darkroomRow).toBeVisible();
    await expect(darkroomRow.getByText("Archived")).toBeVisible();
  });

  // R5: Unmasked mobile-width layout check asserting boundingClientRect.right <= viewport width
  test("proves mobile viewport (375px) has no bounding box overflow without relying on global clipping (R5)", async ({
    browser,
  }) => {
    const mobileContext = await browser.newContext({ viewport: { width: 375, height: 667 } });
    const mobilePage = await mobileContext.newPage();

    // Check login route unauthenticated
    await mobilePage.goto("/login");
    await mobilePage.waitForLoadState("domcontentloaded");

    const routesToCheck = [
      "/",
      "/login",
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

    // Log in on mobile page to sweep all routes
    await mobilePage.fill("#login-email", "admin@example.com");
    await mobilePage.fill("#login-password", "AdminPassword123!");
    await mobilePage.click('button[type="submit"]');
    await expect(mobilePage).toHaveURL(/.*\/resources/);
    await mobilePage.waitForLoadState("networkidle");

    for (const r of routesToCheck) {
      await mobilePage.goto(r);
      await mobilePage.waitForLoadState("networkidle");

      // Assert no element bounding box exceeds 375px + 1px tolerance (excluding internal scroll containers)
      const overflowingElements = await mobilePage.evaluate((maxRight) => {
        const elements = Array.from(document.querySelectorAll<HTMLElement>("*"));
        const offenders: string[] = [];
        for (const el of elements) {
          // Skip deliberate accessible internal scroll containers
          let parent = el.parentElement;
          let insideScrollable = false;
          while (parent && parent !== document.body && parent !== document.documentElement) {
            const cs = window.getComputedStyle(parent);
            if (cs.overflowX === "auto" || cs.overflowX === "scroll") {
              insideScrollable = true;
              break;
            }
            parent = parent.parentElement;
          }
          if (insideScrollable) {
            continue;
          }

          const rect = el.getBoundingClientRect();
          if (rect.right > maxRight + 1) {
            offenders.push(
              `${el.tagName.toLowerCase()}${el.id ? "#" + el.id : ""}[${el.className.split(" ")[0]}]: right=${Math.round(rect.right)}px text="${(el.innerText || el.textContent || "").substring(0, 30)}"`
            );
          }
        }
        return offenders.slice(0, 5);
      }, 375);

      expect(
        overflowingElements,
        `Route ${r} has element(s) overflowing viewport width (375px) without clipping`
      ).toEqual([]);
    }

    await mobileContext.close();
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

      const headers = req.headers();
      const allHeaders = JSON.stringify(headers);
      if (allHeaders.includes("secret-token") || allHeaders.includes("valid-pilot-invitation")) {
        throw new Error(`Secret leaked in outbound request headers: ${req.url()}`);
      }
    });

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

    expect(fs.existsSync(path.join(evidenceDir, "screenshot-01-landing.png"))).toBe(true);
    expect(fs.existsSync(path.join(evidenceDir, "screenshot-05-feedback.png"))).toBe(true);
  });
});
