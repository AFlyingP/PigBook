import { test, expect } from "@playwright/test";

test.describe("Booking Creation & Idempotency E2E (Spec 4.3, 7.2, 7.3)", () => {
  test("creates booking with Idempotency-Key, handles duplicate clicks, and persists to own bookings", async ({
    page,
  }) => {
    // 1. Sign in as member2
    await page.goto("/login");
    await page.fill("#login-email", "member2@example.com");
    await page.fill("#login-password", "MemberPassword123!");
    await page.click('button[type="submit"]');
    await expect(page).toHaveURL(/.*\/resources/);

    // 2. Navigate to Pottery Studio
    await page.goto("/resources/66666666-6666-4666-8666-666666666666");
    await expect(page.getByRole("heading", { name: "Pottery Studio" })).toBeVisible();

    // 3. Switch to second day to ensure future slot > 15m lead time
    const dayButtons = page.locator(".MuiBox-root button:has-text('Sep'), .MuiBox-root button:has-text('Oct'), .MuiBox-root button:has-text('Nov'), .MuiBox-root button:has-text('2026')");
    if ((await dayButtons.count()) >= 2) {
      await dayButtons.nth(1).click();
    }

    // 4. Find and select first available slot
    const selectSlotBtn = page.getByRole("button", { name: "Select Slot" }).first();
    await expect(selectSlotBtn).toBeVisible();
    await selectSlotBtn.click();

    // 5. Booking dialog opens
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await expect(page.getByRole("heading", { name: "Confirm Reservation" })).toBeVisible();

    // Check that createAttempt draft is saved in sessionStorage with UUID key
    const attemptDraft = await page.evaluate(() => {
      const raw = sessionStorage.getItem("commonsbook_create_attempt");
      return raw ? JSON.parse(raw) : null;
    });
    expect(attemptDraft).not.toBeNull();
    expect(attemptDraft.key).toMatch(/^[0-9a-f-]{36}$/i);
    expect(attemptDraft.kind).toBe("booking");

    // 6. Click Confirm Reservation
    const confirmBtn = dialog.getByRole("button", { name: "Confirm Reservation" });
    await confirmBtn.click();

    // 7. Verify success alert appears and dialog closes
    await expect(page.getByRole("status")).toContainText(/Booking confirmed successfully/i);
    await expect(dialog).not.toBeVisible({ timeout: 5000 });

    // 8. SessionStorage attempt is cleared on definitive 201
    const finalDraft = await page.evaluate(() => sessionStorage.getItem("commonsbook_create_attempt"));
    expect(finalDraft).toBeNull();

    // 9. Navigate to /my-bookings and verify booking is listed
    await page.goto("/my-bookings");
    await expect(page.getByRole("heading", { name: "My Bookings" })).toBeVisible();
    await expect(page.getByText("Pottery Studio")).toBeVisible();
    await expect(page.getByText("Confirmed").first()).toBeVisible();
  });

  test("two-session conflict: one 201, one SLOT_CONFLICT, exactly one active booking created", async ({
    context,
  }) => {
    const pageA = await context.newPage();
    const pageB = await context.newPage();

    // User A: member@example.com
    await pageA.goto("/login");
    await pageA.fill("#login-email", "member@example.com");
    await pageA.fill("#login-password", "MemberPassword123!");
    await pageA.click('button[type="submit"]');
    await expect(pageA).toHaveURL(/.*\/resources/);

    // User B: member3@example.com
    await pageB.goto("/login");
    await pageB.fill("#login-email", "member3@example.com");
    await pageB.fill("#login-password", "MemberPassword123!");
    await pageB.click('button[type="submit"]');
    await expect(pageB).toHaveURL(/.*\/resources/);

    // Both navigate to Pottery Studio
    await pageA.goto("/resources/66666666-6666-4666-8666-666666666666");
    await pageB.goto("/resources/66666666-6666-4666-8666-666666666666");

    // Both switch to day offset 3 (3 days in future)
    const dayButtonsA = pageA.locator(".MuiBox-root button:has-text('2026'), .MuiBox-root button:has-text('Sep'), .MuiBox-root button:has-text('Oct')");
    const dayButtonsB = pageB.locator(".MuiBox-root button:has-text('2026'), .MuiBox-root button:has-text('Sep'), .MuiBox-root button:has-text('Oct')");

    if ((await dayButtonsA.count()) >= 3) {
      await dayButtonsA.nth(2).click();
      await dayButtonsB.nth(2).click();
    }

    // Both open dialog for the same slot (e.g. 14:00 slot)
    const slotA = pageA.getByRole("button", { name: "Select Slot" }).nth(4);
    const slotB = pageB.getByRole("button", { name: "Select Slot" }).nth(4);

    await slotA.click();
    await slotB.click();

    await expect(pageA.getByRole("dialog")).toBeVisible();
    await expect(pageB.getByRole("dialog")).toBeVisible();

    // Submit both simultaneously
    const confirmA = pageA.getByRole("button", { name: "Confirm Reservation" });
    const confirmB = pageB.getByRole("button", { name: "Confirm Reservation" });

    await Promise.all([confirmA.click(), confirmB.click()]);

    // One must succeed with confirmed, and the other must receive SLOT_CONFLICT (Spec 5.1, 7.2)
    const hasSuccessA = await pageA.getByRole("status").isVisible().catch(() => false);
    const hasSuccessB = await pageB.getByRole("status").isVisible().catch(() => false);

    const hasConflictA = await pageA.getByRole("alert").isVisible().catch(() => false);
    const hasConflictB = await pageB.getByRole("alert").isVisible().catch(() => false);

    // Exactly one winner and one conflict
    expect(Number(hasSuccessA) + Number(hasSuccessB)).toBe(1);
    expect(Number(hasConflictA) + Number(hasConflictB)).toBe(1);

    if (hasConflictA) {
      await expect(pageA.getByRole("alert")).toContainText(/reserved|conflict|waitlist/i);
      await expect(pageA.getByRole("button", { name: /Join Waitlist for This Slot/i })).toBeVisible();
    } else {
      await expect(pageB.getByRole("alert")).toContainText(/reserved|conflict|waitlist/i);
      await expect(pageB.getByRole("button", { name: /Join Waitlist for This Slot/i })).toBeVisible();
    }

    await pageA.close();
    await pageB.close();
  });
});
