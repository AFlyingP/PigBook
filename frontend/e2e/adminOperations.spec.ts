import { test, expect, type Page } from "@playwright/test";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const repoRoot = path.resolve(__dirname, "../..");

test.describe.serial("Administrator Operations, Users & Auditing E2E (Spec 7.1, 7.2, 8.3)", () => {
  let adminPage: Page;

  test.beforeAll(async ({ browser }) => {
    adminPage = await browser.newPage();
    await adminPage.goto("/login");
    await adminPage.fill("#login-email", "admin@example.com");
    await adminPage.fill("#login-password", "AdminPassword123!");
    await adminPage.click('button[type="submit"]');
    await expect(adminPage).toHaveURL(/.*\/resources/);
  });

  test.afterAll(async () => {
    await adminPage?.close();
  });

  test("administrator filters and cancels a reservation with reason", async () => {
    await adminPage.goto("/admin/bookings");
    await expect(adminPage.getByRole("heading", { name: "Reservations & Bookings" })).toBeVisible();

    // Verify seeded booking exists
    await expect(adminPage.locator("tr", { hasText: "confirmed" })).toBeVisible();

    // Open detail dialog
    const bookingRow = adminPage.locator("tr", { hasText: "confirmed" }).first();
    await bookingRow.getByRole("button", { name: /View booking|Details/i }).click();

    const detailDialog = adminPage.getByRole("dialog");
    await expect(detailDialog).toBeVisible();
    await expect(detailDialog.getByText(/Reservation Details/i)).toBeVisible();
    await expect(detailDialog.getByText(/CONFIRMED/i)).toBeVisible();

    // Close details with keyboard Escape
    await adminPage.keyboard.press("Escape");
    await expect(detailDialog).not.toBeVisible();

    // Cancel the reservation
    await bookingRow.getByRole("button", { name: /Cancel booking|Cancel/i }).click();
    const cancelDialog = adminPage.getByRole("dialog");
    await expect(cancelDialog).toBeVisible();

    await adminPage.fill("#admin-cancel-reason", "Administrative schedule adjustment");
    await cancelDialog.getByRole("button", { name: "Confirm Cancellation" }).click();

    await expect(cancelDialog).not.toBeVisible();
    // Verify booking row status updated to cancelled
    await expect(adminPage.locator("tr", { hasText: "cancelled" })).toBeVisible();
  });

  test("user management prevents disabling or demoting last active administrator", async () => {
    await adminPage.goto("/admin/users");
    await expect(adminPage.getByRole("heading", { name: "User Accounts & Roles" })).toBeVisible();

    // Verify admin user is in table
    const adminRow = adminPage.locator("tr", { hasText: "Admin User" });
    await expect(adminRow).toBeVisible();
    await expect(adminRow.getByText("Administrator")).toBeVisible();

    // Try demoting last active administrator
    await adminRow.getByRole("button", { name: /Change role for Admin User/i }).click();

    // Expect 409 LAST_ADMIN error alert
    await expect(adminPage.getByRole("alert")).toBeVisible();
    await expect(
      adminPage.getByText(/Cannot demote the last active administrator/i)
    ).toBeVisible();
    // Role remains Administrator
    await expect(adminRow.getByText("Administrator")).toBeVisible();

    // Try disabling last active administrator
    await adminRow.getByRole("button", { name: /Disable Admin User/i }).click();

    // Expect 409 LAST_ADMIN error alert
    await expect(adminPage.getByRole("alert")).toBeVisible();
    await expect(
      adminPage.getByText(/Cannot disable the last active administrator/i)
    ).toBeVisible();
    // Status remains Enabled
    await expect(adminRow.getByText("Enabled")).toBeVisible();

    // Now promote another member to admin
    const memberRow = adminPage.locator("tr", { hasText: "Member Three" });
    await expect(memberRow).toBeVisible();
    await memberRow.getByRole("button", { name: /Change role for Member Three/i }).click();

    // Promoted successfully
    await expect(memberRow.getByText("Administrator")).toBeVisible();
  });

  test("invitation dialog displays link once, allows copy, and clears link on close without persistence", async () => {
    await adminPage.goto("/admin/users");
    await adminPage.click("#invite-user-button");

    const dialog = adminPage.getByRole("dialog");
    await expect(dialog).toBeVisible();

    await adminPage.fill("#invite-email", "pilot-e2e-invitee@example.com");
    await dialog.getByRole("button", { name: /Generate Invitation/i }).click();

    // Expect link generated
    await expect(dialog.getByText("Invitation Link Generated")).toBeVisible();
    const linkInput = dialog.locator("#invitation-url-display");
    await expect(linkInput).toBeVisible();
    const inviteUrl = await linkInput.inputValue();
    expect(inviteUrl).toContain("/register#token=");

    // Verify storage has no token or invite url
    const storageCheck = await adminPage.evaluate(() => {
      const issues: string[] = [];
      if (JSON.stringify(localStorage).includes("token=")) {
        issues.push("localStorage contains invitation token");
      }
      if (JSON.stringify(sessionStorage).includes("token=")) {
        issues.push("sessionStorage contains invitation token");
      }
      return issues;
    });
    expect(storageCheck).toEqual([]);

    // Close dialog
    await dialog.getByRole("button", { name: "Done" }).click();
    await expect(dialog).not.toBeVisible();

    // Reopen dialog: verify previous link is completely cleared from state
    await adminPage.click("#invite-user-button");
    const freshDialog = adminPage.getByRole("dialog");
    await expect(freshDialog).toBeVisible();
    await expect(freshDialog.getByText("Invite New User")).toBeVisible();
    await expect(freshDialog.locator("#invitation-url-display")).not.toBeVisible();
    await freshDialog.getByRole("button", { name: "Cancel" }).click();
  });

  test("reviews audit log and exercises dead outbox retry with confirmation", async () => {
    // 1. Check audit log view
    await adminPage.goto("/admin/audit");
    await expect(adminPage.getByRole("heading", { name: "System Audit Log" })).toBeVisible();
    await expect(adminPage.locator("table[aria-label='Audit log table']")).toBeVisible();

    // 2. Run dead outbox fixture seam to ensure a dead outbox event exists
    const seedResult = spawnSync(
      "uv",
      ["run", "--project", "backend", "python", "scripts/seed_dead_outbox.py"],
      {
        cwd: repoRoot,
        stdio: "inherit",
        env: process.env,
      }
    );
    expect(seedResult.status).toBe(0);

    // 3. Navigate to outbox view
    await adminPage.goto("/admin/outbox");
    await expect(adminPage.getByRole("heading", { name: "Transactional Outbox Queue" })).toBeVisible();

    // Filter by dead events
    const statusSelect = adminPage.locator("#outbox-status-filter");
    await statusSelect.click();
    await adminPage.getByRole("option", { name: "Dead (Failed)" }).click();

    // Dead item should be present with enabled Retry button
    const deadRow = adminPage.locator("tr", { hasText: "Dead" }).first();
    await expect(deadRow).toBeVisible();
    const retryBtn = deadRow.getByRole("button", { name: /Retry/i });
    await expect(retryBtn).toBeEnabled();

    // Click retry -> confirmation modal opens
    await retryBtn.click();
    const confirmDialog = adminPage.getByRole("dialog");
    await expect(confirmDialog).toBeVisible();
    await expect(confirmDialog.getByText("Retry Dead Outbox Event")).toBeVisible();

    // Confirm retry
    await confirmDialog.locator("#confirm-outbox-retry-btn").click();
    await expect(confirmDialog).not.toBeVisible();

    // Status filter "All Events" to see it was reset to pending
    await adminPage.locator("#outbox-status-filter").click();
    await adminPage.getByRole("option", { name: "All Events" }).click();
    await expect(adminPage.locator("tr", { hasText: "Pending" }).first()).toBeVisible();
  });

  test("signed-in member cannot access operations routes (403 denial)", async ({
    browser,
  }) => {
    // Separate context for member
    const memberContext = await browser.newContext();
    const memberPage = await memberContext.newPage();

    await memberPage.goto("/login");
    await memberPage.fill("#login-email", "member@example.com");
    await memberPage.fill("#login-password", "MemberPassword123!");
    await memberPage.click('button[type="submit"]');
    await expect(memberPage).toHaveURL(/.*\/resources/);

    // Direct navigation to admin operations routes
    for (const adminPath of ["/admin/bookings", "/admin/users"]) {
      await memberPage.goto(adminPath);
      await expect(memberPage.getByRole("alert")).toBeVisible();
      await expect(memberPage.getByText("403 Forbidden")).toBeVisible();
      await expect(memberPage.getByText(/Administrator privileges are required/i)).toBeVisible();
    }

    await memberContext.close();
  });
});
