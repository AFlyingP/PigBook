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

  test("validates booking launch contract and labels booking action unavailable pending feature registration", async ({
    page,
  }) => {
    await page.goto("/resources/55555555-5555-4555-8555-555555555555");
    await expect(page.getByRole("heading", { name: "Community Woodshop" })).toBeVisible();

    // Booking entry button must be labeled unavailable until route feature registered (Spec 7.1)
    const bookEntry = page.getByRole("button", { name: /booking dialog registration pending/i });
    await expect(bookEntry).toBeDisabled();
    await expect(bookEntry).toHaveText("Book Slot (Feature registration pending)");

    // Switch to tomorrow's date to select guaranteed future slots
    const dayButtons = page.locator('button:has-text(",")');
    if (await dayButtons.count() > 1) {
      await dayButtons.nth(1).click();
    }

    // Test launch contract initiation from available future slot
    const selectSlotBtn = page.getByRole("button", { name: "Select Slot" }).first();
    await expect(selectSlotBtn).toBeVisible();
    await selectSlotBtn.click();

    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await expect(dialog.getByText("Booking Launch Contract Initiated")).toBeVisible();
    await expect(dialog.getByText(/Community Woodshop/)).toBeVisible();
    await dialog.getByRole("button", { name: "Close" }).click();
    await expect(dialog).not.toBeVisible();
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
