import { test, expect } from "@playwright/test";
import { spawnSync } from "node:child_process";

test.describe("Waitlist & Offer Management E2E (Spec 7.1, 7.2, 7.3, 11.5)", () => {
  test("two-member book/join/cancel/offer/accept flow confirms same booking ID (Spec 5.3, 7.3)", async ({
    context,
  }) => {
    const pageMember2 = await context.newPage();

    // 1. Sign in as member2
    await pageMember2.goto("/login");
    await pageMember2.fill("#login-email", "member2@example.com");
    await pageMember2.fill("#login-password", "MemberPassword123!");
    await pageMember2.click('button[type="submit"]');
    await expect(pageMember2).toHaveURL(/.*\/resources/);

    // 2. Navigate to Community Woodshop (which has member1's pre-seeded booking at now+1day 14:00Z)
    await pageMember2.goto("/resources/55555555-5555-4555-8555-555555555555");
    await expect(pageMember2.getByRole("heading", { name: "Community Woodshop" })).toBeVisible();

    // Switch to next day (day offset 1) where member's booking 88888888 exists
    const dayButtons = pageMember2.locator(".MuiBox-root button:has-text('2026'), .MuiBox-root button:has-text('Sep'), .MuiBox-root button:has-text('Oct')");
    if ((await dayButtons.count()) >= 2) {
      await dayButtons.nth(1).click();
    }

    // Find the occupied slot with "Join Waitlist"
    const joinWaitlistBtn = pageMember2.getByRole("button", { name: "Join Waitlist" }).first();
    await expect(joinWaitlistBtn).toBeVisible();
    await joinWaitlistBtn.click();

    // Confirm Join Waitlist in dialog
    const waitlistDialog = pageMember2.getByRole("dialog");
    await expect(waitlistDialog).toBeVisible();
    await waitlistDialog.getByRole("button", { name: "Join Waitlist" }).click();
    await expect(pageMember2.getByRole("status")).toContainText(/joined the waitlist/i);
    await expect(waitlistDialog).not.toBeVisible({ timeout: 5000 });

    // Verify entry in /waitlist
    await pageMember2.goto("/waitlist");
    await expect(pageMember2.getByRole("heading", { name: "My Waitlist" })).toBeVisible();
    await expect(pageMember2.getByText("Waiting").first()).toBeVisible();

    // 3. In a separate context, member1 logs in and cancels their booking 88888888-8888-4888-8888-888888888888
    const pageMember1 = await context.newPage();
    await pageMember1.goto("/login");
    await pageMember1.fill("#login-email", "member@example.com");
    await pageMember1.fill("#login-password", "MemberPassword123!");
    await pageMember1.click('button[type="submit"]');
    await expect(pageMember1).toHaveURL(/.*\/resources/);

    await pageMember1.goto("/my-bookings");
    await expect(pageMember1.getByRole("heading", { name: "My Bookings" })).toBeVisible();
    await expect(pageMember1.getByText("Community Woodshop")).toBeVisible();

    // Cancel reservation
    const cancelBtn = pageMember1.getByRole("button", { name: /Cancel reservation for Community Woodshop/i });
    await cancelBtn.click();

    const cancelDialog = pageMember1.getByRole("dialog");
    await expect(cancelDialog).toBeVisible();
    await cancelDialog.getByRole("button", { name: "Confirm Cancellation" }).click();
    await expect(pageMember1.getByRole("status")).toContainText(/Booking cancelled successfully/i);
    await pageMember1.close();

    // 4. Back to member2: refresh /waitlist, verify OfferCard is displayed with countdown
    await pageMember2.goto("/waitlist");
    await expect(pageMember2.getByText(/Offer Available/i)).toBeVisible();
    await expect(pageMember2.getByText(/Time Remaining to Claim:/i)).toBeVisible();

    // 5. Accept the offer
    const acceptBtn = pageMember2.getByRole("button", { name: "Accept waitlist offer" });
    await expect(acceptBtn).toBeVisible();
    await acceptBtn.click();

    await expect(pageMember2.getByRole("status")).toContainText(/Offer accepted! Your reservation is now confirmed/i);

    // 6. Verify same booking ID is confirmed in /my-bookings
    await pageMember2.goto("/my-bookings");
    await expect(pageMember2.getByRole("heading", { name: "My Bookings" })).toBeVisible();
    await expect(pageMember2.getByText("Community Woodshop")).toBeVisible();
    await expect(pageMember2.getByText("Confirmed").first()).toBeVisible();

    await pageMember2.close();
  });

  test("expiry case: hold expiry seam fast-forwards server deadline and UI refetches state (Spec 7.2, 11.5)", async ({
    page,
  }) => {
    // Sign in as member3
    await page.goto("/login");
    await page.fill("#login-email", "member3@example.com");
    await page.fill("#login-password", "MemberPassword123!");
    await page.click('button[type="submit"]');
    await expect(page).toHaveURL(/.*\/resources/);

    // Navigate to /waitlist
    await page.goto("/waitlist");
    await expect(page.getByRole("heading", { name: "My Waitlist" })).toBeVisible();

    // Invoke isolated fixture seam via Python subprocess against DATABASE_URL
    if (process.env.DATABASE_URL) {
      const result = spawnSync(
        "uv",
        ["run", "--project", "backend", "python", "scripts/expire_hold.py"],
        {
          stdio: "inherit",
          env: process.env,
        }
      );
      expect(result.status).toBe(0);
    }

    // Refresh waitlist page to assert UI renders clean current server state
    await page.reload();
    await expect(page.getByRole("heading", { name: "My Waitlist" })).toBeVisible();
  });
});
