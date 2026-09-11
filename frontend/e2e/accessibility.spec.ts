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

/**
 * Pure accessible name computation adhering strictly to R8:
 * Accepts:
 *  - non-empty aria-label
 *  - aria-labelledby resolving to non-empty text from referenced element IDs
 *  - text content (or child img[alt]) for buttons, links, summaries
 *  - explicit <label for="id"> or wrapping <label> with non-empty text for form controls
 * Strictly rejects placeholder and input values as accessible names.
 */
function findUnnamedInteractiveControls(): string[] {
  const controls = Array.from(
    document.querySelectorAll<HTMLElement>(
      'button:not([disabled]):not([aria-hidden="true"]), a[href]:not([aria-hidden="true"]), input:not([type="hidden"]):not([disabled]):not([aria-hidden="true"]), select:not([disabled]):not([aria-hidden="true"]), textarea:not([disabled]):not([aria-hidden="true"])'
    )
  );

  const missing: string[] = [];

  for (const el of controls) {
    // 1. Check aria-label
    const ariaLabel = el.getAttribute("aria-label")?.trim();
    if (ariaLabel) continue;

    // 2. Check aria-labelledby resolving referenced IDs to non-empty text
    const ariaLabelledBy = el.getAttribute("aria-labelledby")?.trim();
    if (ariaLabelledBy) {
      const ids = ariaLabelledBy.split(/\s+/);
      const resolvedText = ids
        .map((id) => document.getElementById(id)?.textContent?.trim() || "")
        .filter(Boolean)
        .join(" ");
      if (resolvedText) continue;
    }

    // 3. For buttons, links, and summaries: text content
    const tag = el.tagName.toLowerCase();
    if (tag === "button" || tag === "a" || tag === "summary") {
      const text = el.textContent?.trim();
      if (text) continue;
      const imgAlt = el.querySelector("img[alt]")?.getAttribute("alt")?.trim();
      if (imgAlt) continue;
    }

    // 4. For form controls (input, select, textarea): explicit <label for> or wrapping <label>
    if (el.id) {
      const explicitLabel = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      const labelText = explicitLabel?.textContent?.trim();
      if (labelText) continue;
    }

    const wrappingLabel = el.closest("label");
    if (wrappingLabel) {
      const labelText = wrappingLabel.textContent?.trim();
      if (labelText) continue;
    }

    // Notice: placeholder and value are strictly NOT accepted as accessible names (R8)
    missing.push(`${el.tagName.toLowerCase()}${el.id ? "#" + el.id : ""}`);
  }

  return missing;
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

  // R2 & R7: Visible focus indicator proven by difference and focus restoration on Escape and Cancel
  test("verifies visible focus indicator by difference and focus restoration to opener on dialog close (R2, R7)", async () => {
    await sharedPage.goto("/admin/resources");
    await sharedPage.waitForLoadState("networkidle");

    const addBtn = sharedPage.locator("#add-resource-button");

    // 1. Capture unfocused style of the opener button
    await sharedPage.evaluate(() => (document.activeElement as HTMLElement)?.blur?.());
    const unfocusedOpenerStyle = await addBtn.evaluate((el) => {
      const cs = window.getComputedStyle(el);
      return {
        outlineStyle: cs.outlineStyle,
        outlineWidth: cs.outlineWidth,
        outlineColor: cs.outlineColor,
      };
    });

    // Unfocused button carries no focus outline
    expect(
      unfocusedOpenerStyle.outlineStyle === "none" || parseFloat(unfocusedOpenerStyle.outlineWidth) === 0
    ).toBe(true);

    // 2. Focus the opener with keyboard Tab traversal to trigger :focus-visible
    const filterSelect = sharedPage.locator("#resource-active-filter");
    await filterSelect.focus();
    await sharedPage.keyboard.press("Tab");
    await expect(addBtn).toBeFocused();

    // 3. Capture keyboard-focused style: prove focus difference and active focus-visible outline
    const focusedOpenerStyle = await addBtn.evaluate((el) => {
      const cs = window.getComputedStyle(el);
      return {
        outlineStyle: cs.outlineStyle,
        outlineWidth: cs.outlineWidth,
        outlineColor: cs.outlineColor,
      };
    });

    // R7: Prove focus styling by difference (fails if :focus-visible is removed)
    expect(focusedOpenerStyle.outlineStyle).not.toBe(unfocusedOpenerStyle.outlineStyle);
    expect(focusedOpenerStyle.outlineStyle).toBe("solid");
    expect(parseFloat(focusedOpenerStyle.outlineWidth)).toBeGreaterThanOrEqual(2);
    expect(focusedOpenerStyle.outlineColor).toContain("25, 118, 210");

    // 4. Open dialog with Enter
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

    // 5. Close with Escape: assert focus is restored to #add-resource-button (R2)
    await sharedPage.keyboard.press("Escape");
    await expect(dialog).not.toBeVisible();
    await expect(addBtn).toBeFocused();

    // 6. Open again and close via Cancel button: assert focus is restored to #add-resource-button (R2)
    await sharedPage.keyboard.press("Enter");
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(dialog).not.toBeVisible();
    await expect(addBtn).toBeFocused();
  });

  // R3, R7, R8, R9: Sweep all critical routes with real accessible names, visible focus by difference, and unauth /login
  test("exercises keyboard traversal, visible focus by difference, accessible names, and live regions across all critical routes (R3, R7, R8, R9)", async ({
    browser,
  }) => {
    // 1. R9: Sweep /login in a fresh unauthenticated context to guarantee the real login screen is tested
    const unauthContext = await browser.newContext();
    const unauthPage = await unauthContext.newPage();
    await unauthPage.goto("/login");
    await unauthPage.waitForLoadState("domcontentloaded");
    await expect(unauthPage).toHaveURL(/.*\/login/);
    await expect(unauthPage.getByRole("heading", { name: "Sign In to CommonsBook" })).toBeVisible();

    // R7 on /login: Keyboard Tab traversal reaches interactive element with visible focus difference
    await unauthPage.evaluate(() => (document.activeElement as HTMLElement)?.blur?.());
    await unauthPage.keyboard.press("Tab");
    const loginFocusDifference = await unauthPage.evaluate(() => {
      const el = document.activeElement as HTMLElement;
      if (!el || el === document.body) return false;
      const focusedCs = window.getComputedStyle(el);
      const fStyle = {
        outlineStyle: focusedCs.outlineStyle,
        outlineWidth: focusedCs.outlineWidth,
        boxShadow: focusedCs.boxShadow,
      };
      el.blur();
      const unfocusedCs = window.getComputedStyle(el);
      const uStyle = {
        outlineStyle: unfocusedCs.outlineStyle,
        outlineWidth: unfocusedCs.outlineWidth,
        boxShadow: unfocusedCs.boxShadow,
      };
      el.focus();
      return (
        fStyle.outlineStyle !== uStyle.outlineStyle ||
        fStyle.outlineWidth !== uStyle.outlineWidth ||
        fStyle.boxShadow !== uStyle.boxShadow
      );
    });
    expect(loginFocusDifference, "Keyboard focus on /login must produce visible style difference (R7)").toBe(true);

    // R8 on /login: Every enabled control must expose a real accessible name
    const loginMissingNames = await unauthPage.evaluate(findUnnamedInteractiveControls);
    expect(loginMissingNames, "Controls missing accessible names on /login (R8)").toEqual([]);
    await unauthContext.close();

    // 2. Sweep authenticated routes on sharedPage
    const authenticatedRoutes = [
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

    for (const route of authenticatedRoutes) {
      if (route.path.startsWith("/admin/") && !sharedPage.url().includes("/admin")) {
        const adminLink = sharedPage.locator('a[href="/admin/resources"]').first();
        if (await adminLink.isVisible()) {
          await adminLink.click();
        }
      }

      const link = sharedPage.locator(`a[href="${route.path}"]`).first();
      if ((await link.count()) > 0 && (await link.isVisible())) {
        await link.click();
      } else {
        await sharedPage.goto(route.path);
      }
      // Assert URL matches route path to ensure no redirect took place and page is rendered
      await expect(sharedPage).toHaveURL(new RegExp(route.path));
      await expect(sharedPage.locator("main")).toBeVisible();

      // R7 per-route check: Tab from start reaches interactive element with visible focus difference
      await sharedPage.evaluate(() => (document.activeElement as HTMLElement)?.blur?.());
      await sharedPage.keyboard.press("Tab");

      const perRouteFocusDiff = await sharedPage.evaluate(() => {
        const el = document.activeElement as HTMLElement;
        if (!el || el === document.body) return false;
        const focusedCs = window.getComputedStyle(el);
        const fStyle = {
          outlineStyle: focusedCs.outlineStyle,
          outlineWidth: focusedCs.outlineWidth,
          boxShadow: focusedCs.boxShadow,
        };
        el.blur();
        const unfocusedCs = window.getComputedStyle(el);
        const uStyle = {
          outlineStyle: unfocusedCs.outlineStyle,
          outlineWidth: unfocusedCs.outlineWidth,
          boxShadow: unfocusedCs.boxShadow,
        };
        el.focus();
        return (
          fStyle.outlineStyle !== uStyle.outlineStyle ||
          fStyle.outlineWidth !== uStyle.outlineWidth ||
          fStyle.boxShadow !== uStyle.boxShadow
        );
      });
      expect(
        perRouteFocusDiff,
        `Keyboard focus on ${route.path} must produce visible style difference (R7)`
      ).toBe(true);

      // R8 per-route check: Every enabled interactive control exposes a real accessible name
      const unnamedControls = await sharedPage.evaluate(findUnnamedInteractiveControls);
      expect(
        unnamedControls,
        `Interactive controls missing real accessible names on ${route.path} (R8)`
      ).toEqual([]);

      // Live region check on announcing routes
      if (route.requiresLive) {
        const liveRegionCount = await sharedPage.locator('[aria-live="polite"], [role="status"]').count();
        expect(
          liveRegionCount,
          `Expected at least one live announcement region on ${route.path}`
        ).toBeGreaterThanOrEqual(1);
      }
    }

    // 3. Non-colour-only status check: verify self-contained pre-seeded "Old Darkroom" is Archived
    await sharedPage.locator('a[href="/admin/resources"]').first().click();
    await expect(sharedPage).toHaveURL(/.*\/admin\/resources/);
    const darkroomRow = sharedPage.locator("tr", { hasText: "Old Darkroom" });
    await expect(darkroomRow).toBeVisible();
    await expect(darkroomRow.getByText("Archived")).toBeVisible();
  });

  // R5: Unmasked mobile-width layout check asserting boundingClientRect.right <= viewport width
  test("proves mobile viewport (375px) has no bounding box overflow without relying on global clipping (R5)", async ({
    browser,
  }) => {
    // 1. Check unauthenticated public routes on independent mobile context
    const anonContext = await browser.newContext({ viewport: { width: 375, height: 667 } });
    const anonPage = await anonContext.newPage();
    for (const r of ["/", "/login"]) {
      await anonPage.goto(r);
      await anonPage.waitForLoadState("domcontentloaded");
      const overflowing = await anonPage.evaluate((maxRight) => {
        return Array.from(document.querySelectorAll<HTMLElement>("*"))
          .filter((el) => {
            const cs = window.getComputedStyle(el);
            if (cs.overflowX === "auto" || cs.overflowX === "scroll") return false;
            return el.getBoundingClientRect().right > maxRight + 1;
          })
          .map((el) => `${el.tagName.toLowerCase()}: right=${Math.round(el.getBoundingClientRect().right)}px`);
      }, 375);
      expect(overflowing, `Public route ${r} has overflowing elements on mobile`).toEqual([]);
    }
    await anonContext.close();

    // 2. Check authenticated routes on sharedPage by setting mobile viewport and client navigating
    await sharedPage.setViewportSize({ width: 375, height: 667 });

    const routesToCheck = [
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
      const link = sharedPage.locator(`a[href="${r}"]`).first();
      if ((await link.count()) > 0 && (await link.isVisible())) {
        await link.click();
      } else {
        await sharedPage.goto(r);
      }
      await sharedPage.waitForLoadState("domcontentloaded");

      // Assert no element bounding box exceeds 375px + 1px tolerance (excluding internal scroll containers)
      const overflowingElements = await sharedPage.evaluate((maxRight) => {
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

    // Reset viewport size
    await sharedPage.setViewportSize({ width: 1280, height: 720 });
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

    // 1. Landing screenshot on unauthenticated page
    const unauthLanding = await sharedPage.context().browser()!.newPage();
    await unauthLanding.goto("/");
    await unauthLanding.waitForLoadState("domcontentloaded");
    await expect(unauthLanding.getByRole("heading", { name: "CommonsBook", level: 1 })).toBeVisible();
    await unauthLanding.screenshot({ path: path.join(evidenceDir, "screenshot-01-landing.png") });
    await unauthLanding.close();

    // 2. Resources screenshot
    await sharedPage.goto("/resources");
    await sharedPage.waitForLoadState("domcontentloaded");
    await sharedPage.screenshot({ path: path.join(evidenceDir, "screenshot-02-resources.png") });

    // 3. Admin Resources screenshot
    await sharedPage.goto("/admin/resources");
    await sharedPage.waitForLoadState("domcontentloaded");
    await sharedPage.screenshot({ path: path.join(evidenceDir, "screenshot-03-admin-resources.png") });

    // 4. Admin Bookings screenshot
    await sharedPage.goto("/admin/bookings");
    await sharedPage.waitForLoadState("domcontentloaded");
    await sharedPage.screenshot({ path: path.join(evidenceDir, "screenshot-04-admin-bookings.png") });

    // 5. Feedback screenshot
    await sharedPage.goto("/feedback");
    await sharedPage.waitForLoadState("domcontentloaded");
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
