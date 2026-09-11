import { test, expect } from "@playwright/test";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const repoRoot = path.resolve(__dirname, "../..");

test.describe("Waitlist & Offer Management E2E (Spec 7.1, 7.2, 7.3, 11.5)", () => {
  test("two-member book/join/cancel/offer/accept flow confirms same booking ID (Spec 5.3, 7.3, R7)", async ({
    browser,
  }) => {
    // Separate browser contexts for independent authentication sessions
    const contextMember2 = await browser.newContext();
    const contextMember1 = await browser.newContext();
    const pageMember2 = await contextMember2.newPage();
    const pageMember1 = await contextMember1.newPage();

    // 1. Sign in as member2
    await pageMember2.goto("/login");
    await pageMember2.fill("#login-email", "member2@example.com");
    await pageMember2.fill("#login-password", "MemberPassword123!");
    await pageMember2.click('button[type="submit"]');
    await expect(pageMember2).toHaveURL(/.*\/resources/);

    // 2. Navigate to Community Woodshop (which has member1's pre-seeded booking)
    await pageMember2.goto("/resources/55555555-5555-4555-8555-555555555555");
    await expect(pageMember2.getByRole("heading", { name: "Community Woodshop" })).toBeVisible();

    // Find the occupied slot with "Join Waitlist" across 7-day tabs
    const dayTabs = pageMember2.getByRole("button", { name: /\w{3},\s*\d{2}\/\d{2}/ });
    const tabCount = await dayTabs.count();
    let foundOccupied = false;

    for (let i = 0; i < tabCount; i++) {
      await dayTabs.nth(i).click();
      const joinBtn = pageMember2.getByRole("button", { name: "Join Waitlist" }).first();
      if (await joinBtn.isVisible({ timeout: 1000 }).catch(() => false)) {
        foundOccupied = true;
        await joinBtn.click();
        break;
      }
    }
    expect(foundOccupied).toBe(true);

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

    // 3. Member1 logs in on their own context and cancels their booking 88888888-8888-4888-8888-888888888888
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
    await contextMember1.close();

    // 4. Back to member2: capture offered_booking_id with waitForResponse (R7)
    const waitlistResponsePromise = pageMember2.waitForResponse(
      (resp) => resp.url().includes("/api/v1/waitlist") && resp.request().method() === "GET"
    );
    await pageMember2.reload();
    const waitlistResp = await waitlistResponsePromise;
    const waitlistData = await waitlistResp.json();
    const offered = waitlistData.items?.find(
      (item: { status: string; offered_booking_id?: string }) => item.status === "offered"
    );
    const capturedOfferedBookingId = offered?.offered_booking_id;
    expect(capturedOfferedBookingId).toBeTruthy();

    await expect(pageMember2.getByText(/Offer Available/i).first()).toBeVisible();
    await expect(pageMember2.getByText(/Time Remaining to Claim:/i)).toBeVisible();

    // 5. Accept the offer
    const acceptBtn = pageMember2.getByRole("button", { name: "Accept waitlist offer" });
    await expect(acceptBtn).toBeVisible();
    await acceptBtn.click();

    await expect(pageMember2.getByRole("status")).toContainText(/Offer accepted! Your reservation is now confirmed/i);

    // 6. Navigate to /my-bookings and verify the confirmed booking ID matches the offer (R7)
    const bookingsResponsePromise = pageMember2.waitForResponse(
      (resp) => resp.url().includes("/api/v1/bookings") && resp.request().method() === "GET"
    );
    await pageMember2.goto("/my-bookings");
    const bookingsResp = await bookingsResponsePromise;
    const bookingsData = await bookingsResp.json();

    await expect(pageMember2.getByRole("heading", { name: "My Bookings" })).toBeVisible();
    await expect(pageMember2.getByText("Community Woodshop")).toBeVisible();
    await expect(pageMember2.getByText("Confirmed").first()).toBeVisible();

    const confirmedBooking = bookingsData.items?.find(
      (b: { status: string; resource_id: string; id: string }) =>
        b.status === "confirmed" && b.resource_id === "55555555-5555-4555-8555-555555555555"
    );
    expect(confirmedBooking).toBeDefined();

    // Exact booking ID assertion per Spec 5.3, 7.3 and R7:
    // The newly confirmed booking is the exact same entity promoted from the hold
    expect(capturedOfferedBookingId).toBeTruthy();
    expect(confirmedBooking.id).toBe(capturedOfferedBookingId);

    await pageMember2.close();
    await contextMember2.close();
  });

  test("expiry case: hold expiry seam fast-forwards server deadline and UI refetches state (Spec 7.2, 11.5, R2)", async ({
    browser,
  }) => {
    // Build real offered-hold scenario through product path (R2)
    const contextA = await browser.newContext();
    const contextB = await browser.newContext();
    const pageA = await contextA.newPage();
    const pageB = await contextB.newPage();

    // 1. User A (admin@example.com) books an available slot on Pottery Studio (day 5)
    await pageA.goto("/login");
    await pageA.fill("#login-email", "admin@example.com");
    await pageA.fill("#login-password", "AdminPassword123!");
    await pageA.click('button[type="submit"]');
    await expect(pageA).toHaveURL(/.*\/resources/);

    await pageA.goto("/resources/66666666-6666-4666-8666-666666666666");
    const dayTabsA = pageA.getByRole("button", { name: /\w{3},\s*\d{2}\/\d{2}/ });
    await expect(dayTabsA.nth(5)).toBeVisible();
    await dayTabsA.nth(5).click();

    // Select slot index 8
    const slotBtnA = pageA.getByRole("button", { name: "Select Slot" }).nth(8);
    await slotBtnA.click();
    const dialogA = pageA.getByRole("dialog");
    await expect(dialogA).toBeVisible();
    await dialogA.getByRole("button", { name: "Confirm Reservation" }).click();
    await expect(pageA.getByRole("status")).toContainText(/Booking confirmed successfully/i);
    await expect(dialogA).not.toBeVisible({ timeout: 5000 });

    // 2. User B (member3@example.com) joins the waitlist for that exact slot
    await pageB.goto("/login");
    await pageB.fill("#login-email", "member3@example.com");
    await pageB.fill("#login-password", "MemberPassword123!");
    await pageB.click('button[type="submit"]');
    await expect(pageB).toHaveURL(/.*\/resources/);

    await pageB.goto("/resources/66666666-6666-4666-8666-666666666666");
    const dayTabsB = pageB.getByRole("button", { name: /\w{3},\s*\d{2}\/\d{2}/ });
    await expect(dayTabsB.nth(5)).toBeVisible();
    await dayTabsB.nth(5).click();

    // The slot is now occupied: click "Join Waitlist"
    const joinWaitlistBtn = pageB.getByRole("button", { name: "Join Waitlist" }).first();
    await expect(joinWaitlistBtn).toBeVisible();
    await joinWaitlistBtn.click();

    const waitlistDialogB = pageB.getByRole("dialog");
    await expect(waitlistDialogB).toBeVisible();
    await waitlistDialogB.getByRole("button", { name: "Join Waitlist" }).click();
    await expect(pageB.getByRole("status")).toContainText(/joined the waitlist/i);
    await expect(waitlistDialogB).not.toBeVisible({ timeout: 5000 });

    // 3. User A cancels their booking, promoting User B to offered
    await pageA.goto("/my-bookings");
    await expect(pageA.getByRole("heading", { name: "My Bookings" })).toBeVisible();
    const cancelBtn = pageA.getByRole("button", { name: /Cancel reservation for Pottery Studio/i }).first();
    await cancelBtn.click();
    const cancelDialogA = pageA.getByRole("dialog");
    await expect(cancelDialogA).toBeVisible();
    await cancelDialogA.getByRole("button", { name: "Confirm Cancellation" }).click();
    await expect(pageA.getByRole("status")).toContainText(/Booking cancelled successfully/i);
    await pageA.close();
    await contextA.close();

    // 4. User B navigates to /waitlist: asserts the offer card is visible with countdown (R2.2)
    await pageB.goto("/waitlist");
    await expect(pageB.getByRole("heading", { name: "My Waitlist" })).toBeVisible();
    await expect(pageB.getByText(/Offer Available/i).first()).toBeVisible();
    await expect(pageB.getByText(/Time Remaining to Claim:/i)).toBeVisible();
    await expect(pageB.getByRole("button", { name: "Accept waitlist offer" })).toBeEnabled();

    // 5. Run the isolated fixture to expire User B's hold (R2.3, R2.4 - must fail loudly if missing)
    const result = spawnSync(
      "uv",
      ["run", "--project", "backend", "python", "scripts/expire_hold.py"],
      {
        cwd: repoRoot,
        stdio: "inherit",
        env: process.env,
      }
    );
    expect(result.status).toBe(0);

    // 6. Reload /waitlist: assert UI reflects server state after refetch (R2.3)
    // The offer is no longer claimable/confirmable and entry shows expired state
    await pageB.reload();
    await expect(pageB.getByRole("heading", { name: "My Waitlist" })).toBeVisible();

    // Offer card is gone (no longer claimable)
    await expect(pageB.getByRole("button", { name: "Accept waitlist offer" })).not.toBeVisible();

    // Entry status in table reflects expired state
    await expect(pageB.getByText("Expired").first()).toBeVisible();

    await pageB.close();
    await contextB.close();
  });
});
