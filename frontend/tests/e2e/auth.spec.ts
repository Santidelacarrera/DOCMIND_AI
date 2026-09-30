import { expect, test } from "@playwright/test";

test("a registered user can sign in and reach the dashboard", async ({ page, request }) => {
  const email = `e2e-${Date.now()}@example.com`;
  const password = "correct-horse-battery-staple";
  const registration = await request.post("http://localhost:8000/api/v1/auth/register", {
    data: { email, password, organization_name: "Browser E2E" },
  });
  expect(registration.status()).toBe(201);
  await page.goto("/login");
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.getByRole("heading", { name: "Documents, under control." })).toBeVisible();
});
