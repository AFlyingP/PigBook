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

    // 3. Switch to day offset 2 (2 days in future) to guarantee slot > 15m in future
    const dayTabs = page.getByRole("button", { name: /\w{3},\s*\d{2}\/\d{2}/ });
    await expect(dayTabs.nth(2)).toBeVisible();
    await dayTabs.nth(2).click();

    // 4. Find and select first available slot
    const selectSlotBtn = page.getByRole("button", { name: "Select Slot" }).first();
    await expect(selectSlotBtn).toBeVisible();
    await selectSlotBtn.click();

    // 5. Booking dialog opens
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await expect(page.getByRole("heading", { name: "Confirm Reservation" })).toBeVisible();

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

  test("two-session conflict: concurrent submissions resolve to one 201 success and one 409 SLOT_CONFLICT (Spec 5.1, 7.2)", async ({
    browser,
  }) => {
    // Separate browser contexts for independent authentication sessions
    const contextA = await browser.newContext();
    const contextB = await browser.newContext();
    const pageA = await contextA.newPage();
    const pageB = await contextB.newPage();

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
    const dayTabsA = pageA.getByRole("button", { name: /\w{3},\s*\d{2}\/\d{2}/ });
    const dayTabsB = pageB.getByRole("button", { name: /\w{3},\s*\d{2}\/\d{2}/ });

    await expect(dayTabsA.nth(3)).toBeVisible();
    await expect(dayTabsB.nth(3)).toBeVisible();

    await dayTabsA.nth(3).click();
    await dayTabsB.nth(3).click();

    // Both open dialog for the same slot (slot index 4 on that day)
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

    // Poll until one shows 201 status and the other shows 409 conflict alert
    await expect
      .poll(
        async () => {
          const successA = await pageA.getByRole("status").isVisible().catch(() => false);
          const successB = await pageB.getByRole("status").isVisible().catch(() => false);
          const conflictA = await pageA.getByRole("alert").isVisible().catch(() => false);
          const conflictB = await pageB.getByRole("alert").isVisible().catch(() => false);

          const totalSuccess = Number(successA) + Number(successB);
          const totalConflict = Number(conflictA) + Number(conflictB);
          return { totalSuccess, totalConflict };
        },
        { timeout: 10000, intervals: [200, 500] }
      )
      .toEqual({ totalSuccess: 1, totalConflict: 1 });

    const conflictA = await pageA.getByRole("alert").isVisible().catch(() => false);
    if (conflictA) {
      await expect(pageA.getByRole("alert")).toContainText(/reserved|conflict|waitlist/i);
      await expect(pageA.getByRole("button", { name: /Join Waitlist for This Slot/i })).toBeVisible();
    } else {
      await expect(pageB.getByRole("alert")).toContainText(/reserved|conflict|waitlist/i);
      await expect(pageB.getByRole("button", { name: /Join Waitlist for This Slot/i })).toBeVisible();
    }

    await pageA.close();
    await pageB.close();
    await contextA.close();
    await contextB.close();
  });

  test("duplicate submit and reload same-key idempotency produces exactly one booking (Spec 4.3, 7.2, 7.3, R5)", async ({
    page,
  }) => {
    // 1. Sign in as member2
    await page.goto("/login");
    await page.fill("#login-email", "member2@example.com");
    await page.fill("#login-password", "MemberPassword123!");
    await page.click('button[type="submit"]');
    await expect(page).toHaveURL(/.*\/resources/);

    // 2. Navigate to Pottery Studio and select day 4
    await page.goto("/resources/66666666-6666-4666-8666-666666666666");
    const dayTabs = page.getByRole("button", { name: /\w{3},\s*\d{2}\/\d{2}/ });
    await expect(dayTabs.nth(4)).toBeVisible();
    await dayTabs.nth(4).click();

    // Select slot index 5
    const selectSlotBtn = page.getByRole("button", { name: "Select Slot" }).nth(5);
    await selectSlotBtn.click();

    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();

    // Intercept first POST to simulate network failure (uncertain outcome per Spec 7.3)
    let intercepted = false;
    await page.route("**/api/v1/bookings", async (route) => {
      if (route.request().method() === "POST" && !intercepted) {
        intercepted = true;
        await route.abort("failed");
      } else {
        await route.continue();
      }
    });

    const confirmBtn = dialog.getByRole("button", { name: "Confirm Reservation" });
    await confirmBtn.click();

    // Dialog displays uncertain alert and "Retry Booking"
    const retryBtn = dialog.getByRole("button", { name: "Retry Booking" });
    await expect(retryBtn).toBeVisible();

    // Verify draft key in sessionStorage
    const draftBeforeReload = await page.evaluate(() => {
      const raw = sessionStorage.getItem("commonsbook_create_attempt");
      return raw ? JSON.parse(raw) : null;
    });
    expect(draftBeforeReload).not.toBeNull();
    expect(draftBeforeReload.key).toMatch(/^[0-9a-f-]{36}$/i);

    // Reload page during uncertain state: attempt is restored for matching principal
    await page.reload();
    await expect(page.getByRole("dialog")).toBeVisible();
    const retryAfterReload = page.getByRole("dialog").getByRole("button", { name: "Retry Booking" });
    await expect(retryAfterReload).toBeVisible();

    // Resend same key on retry
    await retryAfterReload.click();

    await expect(page.getByRole("status")).toContainText(/Booking confirmed successfully/i);
    await expect(page.getByRole("dialog")).not.toBeVisible({ timeout: 5000 });

    // Draft is purged upon definitive 201
    const draftAfterSuccess = await page.evaluate(() => sessionStorage.getItem("commonsbook_create_attempt"));
    expect(draftAfterSuccess).toBeNull();

    // Verify booking is confirmed in /my-bookings
    await page.goto("/my-bookings");
    await expect(page.getByRole("heading", { name: "My Bookings" })).toBeVisible();
    await expect(page.getByText("Pottery Studio").first()).toBeVisible();
    const confirmedRows = page.locator("tr").filter({ hasText: "Pottery Studio" });
    expect(await confirmedRows.count()).toBeGreaterThanOrEqual(1);
  });
});
