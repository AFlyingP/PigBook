import { test, expect, type Page } from "@playwright/test";

test.describe.serial("Administrator Inventory & Blackout E2E Flow (Spec 7.1, 7.2, 4.2 E17-E23)", () => {
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

  test("administrator creates, edits and archives a resource with soft archive only", async () => {
    // 1. Navigate to /admin/resources via navigation header
    await adminPage.goto("/admin/resources");
    await expect(adminPage.getByRole("heading", { name: "Resource Inventory" })).toBeVisible();

    // 2. Create a new resource
    await adminPage.click("#add-resource-button");
    const dialog = adminPage.getByRole("dialog");
    await expect(dialog).toBeVisible();

    await adminPage.fill("#resource-create-name", "3D Printing Lab");
    await adminPage.fill("#resource-create-location", "Innovation Wing Room 204");
    await adminPage.fill("#resource-create-description", "Equipped with FDM and SLA printers");
    await adminPage.click('button[type="submit"]:has-text("Create Resource")');

    await expect(dialog).not.toBeVisible();
    await expect(adminPage.getByRole("cell", { name: "3D Printing Lab" })).toBeVisible();
    await expect(adminPage.getByText("Innovation Wing Room 204")).toBeVisible();

    // 3. Edit the newly created resource
    const row = adminPage.locator("tr", { hasText: "3D Printing Lab" });
    await row.getByRole("button", { name: /Edit/i }).click();

    const editDialog = adminPage.getByRole("dialog");
    await expect(editDialog).toBeVisible();
    await adminPage.fill("#resource-edit-name", "3D Printing & Prototyping Lab");
    await adminPage.click('button[type="submit"]:has-text("Save Changes")');

    await expect(editDialog).not.toBeVisible();
    await expect(adminPage.getByRole("cell", { name: "3D Printing & Prototyping Lab" })).toBeVisible();

    // 4. Soft archive the resource
    const updatedRow = adminPage.locator("tr", { hasText: "3D Printing & Prototyping Lab" });
    await updatedRow.getByRole("button", { name: /Archive/i }).click();

    const archiveDialog = adminPage.getByRole("dialog");
    await expect(archiveDialog).toBeVisible();
    await expect(archiveDialog.getByText(/soft archive/i)).toBeVisible();

    // Guardrail: verify NO hard delete or forced cancel button exists anywhere
    await expect(adminPage.getByRole("button", { name: /hard delete/i })).not.toBeVisible();
    await expect(adminPage.getByRole("button", { name: /force cancel/i })).not.toBeVisible();

    await archiveDialog.getByRole("button", { name: "Archive Resource" }).click();
    await expect(archiveDialog).not.toBeVisible();

    // Status is now Archived (soft archive)
    const archivedRow = adminPage.locator("tr", { hasText: "3D Printing & Prototyping Lab" });
    await expect(archivedRow.getByText("Archived")).toBeVisible();
  });

  test("administrator creates and cancels a blackout window for a resource", async () => {
    // Go to admin resources
    await adminPage.goto("/admin/resources");
    await expect(adminPage.getByText("Pottery Studio")).toBeVisible();

    // Navigate to Pottery Studio blackouts
    const potteryRow = adminPage.locator("tr", { hasText: "Pottery Studio" });
    await potteryRow.locator('a[href*="/blackouts"]').click();
    await expect(adminPage).toHaveURL(/.*\/admin\/resources\/66666666-6666-4666-8666-666666666666\/blackouts/);
    await expect(adminPage.getByRole("heading", { name: /Blackouts for Pottery Studio/i })).toBeVisible();

    // Add blackout window
    await adminPage.click("#add-blackout-button");
    const createDialog = adminPage.getByRole("dialog");
    await expect(createDialog).toBeVisible();

    // Date range
    const targetDate = new Date();
    targetDate.setDate(targetDate.getDate() + 3);
    const yyyy = targetDate.getFullYear();
    const mm = String(targetDate.getMonth() + 1).padStart(2, "0");
    const dd = String(targetDate.getDate()).padStart(2, "0");

    await adminPage.fill("#blackout-starts-at", `${yyyy}-${mm}-${dd}T08:00`);
    await adminPage.fill("#blackout-ends-at", `${yyyy}-${mm}-${dd}T16:00`);

    await createDialog.getByRole("button", { name: "Create Blackout" }).click();
    await expect(createDialog).not.toBeVisible();

    // Verify blackout is listed with Active Blackout status
    await expect(adminPage.getByText("Active Blackout").first()).toBeVisible();

    // Cancel the blackout
    const blackoutRow = adminPage.locator("tr", { hasText: "Active Blackout" }).first();
    await blackoutRow.getByRole("button", { name: /Cancel/i }).click();

    const cancelDialog = adminPage.getByRole("dialog");
    await expect(cancelDialog).toBeVisible();
    await cancelDialog.getByRole("button", { name: "Confirm Cancellation" }).click();
    await expect(cancelDialog).not.toBeVisible();

    // Verify status changed to Cancelled
    await expect(adminPage.getByText("Cancelled").first()).toBeVisible();
  });

  test("concurrent-edit displays 412 VERSION_MISMATCH refresh prompt without silent overwrite", async ({
    request: apiRequest,
  }) => {
    await adminPage.goto("/admin/resources");
    await expect(adminPage.getByText("Community Woodshop")).toBeVisible();

    // Open edit dialog for Community Woodshop
    const woodshopRow = adminPage.locator("tr", { hasText: "Community Woodshop" });
    await woodshopRow.getByRole("button", { name: /Edit/i }).click();
    const editDialog = adminPage.getByRole("dialog");
    await expect(editDialog).toBeVisible();

    // 1. Get current version from server
    const adminLoginRes = await apiRequest.post("/api/v1/auth/login", {
      data: {
        email: "admin@example.com",
        password: "AdminPassword123!",
      },
    });
    const adminToken = (await adminLoginRes.json()).access_token;

    const getRes = await apiRequest.get("/api/v1/resources/55555555-5555-4555-8555-555555555555", {
      headers: { Authorization: `Bearer ${adminToken}` },
    });
    const currentVersion = getRes.headers()["etag"] || `"${(await getRes.json()).version}"`;

    // 2. Concurrently patch resource via API to bump version on server
    const patchRes = await apiRequest.patch(
      "/api/v1/admin/resources/55555555-5555-4555-8555-555555555555",
      {
        headers: {
          Authorization: `Bearer ${adminToken}`,
          "If-Match": currentVersion,
        },
        data: {
          description: "Concurrent background update to bump version",
        },
      }
    );
    expect(patchRes.status()).toBe(200);

    // Now submit the edit form in the UI (holding old version)
    await adminPage.fill("#resource-edit-location", "Workshop Bay B Concurrently Edited");
    await editDialog.getByRole("button", { name: "Save Changes" }).click();

    // Assert 412 warning alert is visible
    await expect(
      adminPage.getByText(/Another update occurred to this resource/i)
    ).toBeVisible();

    // Dialog stays open without silent overwrite
    await expect(editDialog).toBeVisible();
    await editDialog.getByRole("button", { name: "Cancel" }).click();
  });

  test("signed-in member attempting to access admin routes receives 403 Forbidden denial", async ({
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

    // Verify Admin button is NOT visible in navigation for member
    await expect(memberPage.getByRole("link", { name: "Admin", exact: true })).not.toBeVisible();

    // Direct navigation to /admin/resources
    await memberPage.goto("/admin/resources");
    await expect(memberPage.getByRole("alert")).toBeVisible();
    await expect(memberPage.getByText("403 Forbidden")).toBeVisible();
    await expect(memberPage.getByText(/Administrator privileges are required/i)).toBeVisible();
    await expect(memberPage.getByRole("button", { name: /Add Resource/i })).not.toBeVisible();

    // Direct navigation to /admin/resources/:id/blackouts
    await memberPage.goto("/admin/resources/55555555-5555-4555-8555-555555555555/blackouts");
    await expect(memberPage.getByRole("alert")).toBeVisible();
    await expect(memberPage.getByText("403 Forbidden")).toBeVisible();
    await expect(memberPage.getByRole("button", { name: /Add Blackout/i })).not.toBeVisible();

    await memberContext.close();
  });
});
