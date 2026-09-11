import { test, expect } from "@playwright/test";

test.describe("Resource Catalog & Availability UI E2E (Spec 1.2, 7.1, 7.2)", () => {
  test.beforeEach(async ({ page }) => {
    // Log in before each test using member2 account
    await page.goto("/login");
    await page.fill("#login-email", "member2@example.com");
    await page.fill("#login-password", "MemberPassword123!");
    await page.click('button[type="submit"]');
    await expect(page).toHaveURL(/.*\/resources/);
  });

  test("browses resource catalog and filters resources with client-side search", async ({
    page,
  }) => {
    await expect(page.getByText("Community Woodshop")).toBeVisible();
    await expect(page.getByText("Pottery Studio")).toBeVisible();

    // Client-side search within page
    const searchInput = page.getByLabel(/search page resources/i);
    await searchInput.fill("Woodshop");

    await expect(page.getByText("Community Woodshop")).toBeVisible();
    await expect(page.getByText("Pottery Studio")).not.toBeVisible();

    // Clear search
    await page.getByLabel("Clear search query").click();
    await expect(page.getByText("Community Woodshop")).toBeVisible();
    await expect(page.getByText("Pottery Studio")).toBeVisible();
  });

  test("displays resource detail, organization locked timezone, and 7-day availability grid", async ({
    page,
  }) => {
    // Navigate to Community Woodshop details
    await page.click('a:has-text("View Availability & Details") >> nth=0');
    await expect(page).toHaveURL(/.*\/resources\/55555555-5555-4555-8555-555555555555/);

    // Verify resource details
    await expect(page.getByRole("heading", { name: "Community Woodshop" })).toBeVisible();
    await expect(page.getByText("Workshop Bay B")).toBeVisible();

    // Verify organization timezone display (America/New_York with visible offset, locked display)
    await expect(page.getByText("Organization Timezone:")).toBeVisible();
    await expect(page.getByRole("combobox", { name: /America\/New_York/ })).toBeVisible();

    // Verify 7-day availability schedule
    await expect(page.getByText("7-Day Availability Schedule")).toBeVisible();

    // Verify accessible list alternative
    const listToggle = page.getByRole("button", { name: /accessible list/i });
    await listToggle.click();

    const accessibleList = page.getByLabel(/accessible 7-day availability schedule/i);
    await expect(accessibleList).toBeVisible();
    // Confirms owner identity is not shown (US-02)
    await expect(page.getByText(/member@example.com/i)).not.toBeVisible();
    await expect(page.getByText(/member2@example.com/i)).not.toBeVisible();
  });

  test("validates selecting slot opens booking dialog and no mutation is issued before confirmation (R4, R7)", async ({
    page,
  }) => {
    let bookingMutationOccurred = false;
    page.on("request", (req) => {
      if (req.url().includes("/api/v1/bookings") && req.method() === "POST") {
        bookingMutationOccurred = true;
      }
    });

    await page.goto("/resources/55555555-5555-4555-8555-555555555555");
    await expect(page.getByRole("heading", { name: "Community Woodshop" })).toBeVisible();

    // Switch to tomorrow (day offset 1) to find available future slots
    const dayTabs = page.getByRole("button", { name: /\w{3},\s*\d{2}\/\d{2}/ });
    await expect(dayTabs.nth(1)).toBeVisible();
    await dayTabs.nth(1).click();

    // Shipped app exposes real slot selection controls
    const selectSlotBtn = page.getByRole("button", { name: "Select Slot" }).first();
    await expect(selectSlotBtn).toBeVisible();
    await expect(selectSlotBtn).toBeEnabled();

    // Clicking slot opens the booking dialog without issuing a mutation
    await selectSlotBtn.click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("heading", { name: "Confirm Reservation" })).toBeVisible();

    // Ensure opening the dialog did NOT issue any booking mutation to the backend
    expect(bookingMutationOccurred).toBe(false);

    // Cancel / close the dialog without confirming
    const cancelBtn = dialog.getByRole("button", { name: "Cancel" });
    await cancelBtn.click();
    await expect(dialog).not.toBeVisible();

    // Ensure no mutation was issued upon cancellation
    expect(bookingMutationOccurred).toBe(false);
  });

  test("displays archived resource notice preventing member access to inactive resources", async ({
    page,
  }) => {
    await page.goto("/resources/77777777-7777-4777-8777-777777777777");
    // Inactive resources return 404 to non-admins and display the error with back action
    await expect(page.getByRole("alert")).toBeVisible();
    await expect(page.getByText(/resource not found/i)).toBeVisible();
    await expect(page.getByRole("link", { name: /back to resource catalog/i })).toBeVisible();
  });
});
