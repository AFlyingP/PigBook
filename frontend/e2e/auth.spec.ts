import { test, expect } from "@playwright/test";

test.describe("Authentication E2E Flow (Spec 7.1, 7.2, 8.1)", () => {
  test("allows active member to sign in, stores token in memory, and persists session via cookie refresh", async ({
    page,
  }) => {
    await page.goto("/login");

    await page.fill("#login-email", "member@example.com");
    await page.fill("#login-password", "MemberPassword123!");
    await page.click('button[type="submit"]');

    // Expect navigation to /resources
    await expect(page).toHaveURL(/.*\/resources/);
    await expect(page.getByText("Member User")).toBeVisible();
    await expect(page.getByRole("button", { name: /sign out/i })).toBeVisible();

    // Exhaustive token persistence check (R5, Spec 7.2):
    // localStorage must be completely empty, and neither localStorage nor sessionStorage
    // may contain JWT-shaped tokens or bearer values.
    const storageAudit = await page.evaluate(() => {
      const jwtRegex = /^[A-Za-z0-9-_]+\.[A-Za-z0-9-_]+\.[A-Za-z0-9-_]+$/;
      const issues: string[] = [];

      if (localStorage.length > 0) {
        issues.push(`localStorage must be empty but has ${localStorage.length} item(s)`);
      }

      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i)!;
        const v = localStorage.getItem(k) || "";
        if (jwtRegex.test(v)) {
          issues.push(`localStorage key '${k}' contains JWT`);
        }
      }

      for (let i = 0; i < sessionStorage.length; i++) {
        const k = sessionStorage.key(i)!;
        const v = sessionStorage.getItem(k) || "";
        if (k !== "commonsbook_create_attempt") {
          issues.push(`Unexpected sessionStorage key '${k}'`);
        }
        if (jwtRegex.test(v)) {
          issues.push(`sessionStorage key '${k}' contains JWT`);
        }
      }

      return issues;
    });
    expect(storageAudit).toEqual([]);

    // Reload page to verify cookie-based session restoration
    await page.reload();
    await expect(page).toHaveURL(/.*\/resources/);
    await expect(page.getByText("Member User")).toBeVisible();
  });

  test("rejects login for disabled user with explicit 401 error message", async ({
    page,
  }) => {
    await page.goto("/login");

    await page.fill("#login-email", "disabled@example.com");
    await page.fill("#login-password", "MemberPassword123!");
    await page.click('button[type="submit"]');

    const alert = page.getByRole("alert");
    await expect(alert).toBeVisible();
    await expect(alert).toContainText("Invalid email or password, or account disabled.");
    await expect(page).toHaveURL(/.*\/login/);
  });

  test("reads invitation fragment exactly once, erases it from URL bar, and registers user", async ({
    page,
  }) => {
    // Navigate with invitation token in URL fragment
    await page.goto("/register#token=valid-pilot-invitation-token-123");

    // Fragment must be erased immediately via replaceState (Spec 7.1)
    await expect(page).toHaveURL(/.*\/register$/);
    const hash = await page.evaluate(() => window.location.hash);
    expect(hash).toBe("");

    // Registration form fields are rendered
    await page.fill("#register-email", "invited@example.com");
    await page.fill("#register-display-name", "Invited Participant");
    await page.fill("#register-password", "InvitedPassword123!");
    await page.click('button[type="submit"]');

    // Automatically logs in and redirects to resources
    await expect(page).toHaveURL(/.*\/resources/);
    await expect(page.getByText("Invited Participant")).toBeVisible();
  });

  test("signs out successfully and protects authenticated routes", async ({
    page,
  }) => {
    await page.goto("/login");
    await page.fill("#login-email", "member@example.com");
    await page.fill("#login-password", "MemberPassword123!");
    await page.click('button[type="submit"]');

    await expect(page).toHaveURL(/.*\/resources/);

    // Sign out
    await page.click('button:has-text("Sign Out")');
    await expect(page).toHaveURL(/.*\/login/);

    // Attempt to access protected route after sign out redirects to /login
    await page.goto("/resources");
    await expect(page).toHaveURL(/.*\/login/);
  });

  test("synchronizes sign out across multiple tabs via BroadcastChannel", async ({
    context,
  }) => {
    const page1 = await context.newPage();
    const page2 = await context.newPage();

    // Log in on page 1
    await page1.goto("/login");
    await page1.fill("#login-email", "member@example.com");
    await page1.fill("#login-password", "MemberPassword123!");
    await page1.click('button[type="submit"]');
    await expect(page1).toHaveURL(/.*\/resources/);

    // Open resources on page 2
    await page2.goto("/resources");
    await expect(page2.getByText("Member User")).toBeVisible();

    // Sign out on page 1
    await page1.click('button:has-text("Sign Out")');
    await expect(page1).toHaveURL(/.*\/login/);

    // Page 2 should automatically sync logout
    await expect(page2).toHaveURL(/.*\/login/);

    await page1.close();
    await page2.close();
  });
});
