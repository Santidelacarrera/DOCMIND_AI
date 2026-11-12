import { expect, test } from "@playwright/test";

const API = process.env.E2E_API_URL ?? "http://localhost:8000";

// Phone width: every main page must fit without horizontal scrolling and keep its navigation usable.
test.use({ viewport: { width: 375, height: 812 } });

test("main pages fit a phone screen without horizontal scrolling", async ({ page, request }) => {
  const email = `mobile-e2e-${Date.now()}@example.com`;
  const password = "correct-horse-battery-staple";
  const registered = await request.post(`${API}/api/v1/auth/register`, {
    data: { email, password, organization_name: "Mobile E2E" },
  });
  expect(registered.status()).toBe(201);
  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  for (const path of ["/dashboard", "/documents", "/ask", "/conversations", "/members", "/schemas"]) {
    await page.goto(path);
    await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();
    // Lists load after the first paint (and some pages poll, so the network never goes idle): give the
    // data a moment to render before measuring the final layout.
    await page.waitForTimeout(1500);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow, `${path} overflows horizontally by ${overflow}px`).toBeLessThanOrEqual(1);
  }
});
