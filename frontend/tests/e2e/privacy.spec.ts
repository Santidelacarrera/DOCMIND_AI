import { expect, test } from "@playwright/test";

const API = process.env.E2E_API_URL ?? "http://localhost:8000";

test("public privacy page renders notice and authenticated user can inspect posture, export JSON, and erase history", async ({
  page,
  request,
}) => {
  // 1. Public unauthenticated access shows Ley N° 21.719 notice and sign-in CTA.
  await page.goto("/privacy");
  await expect(page.getByRole("heading", { name: /Privacy & ARCOP Rights/i })).toBeVisible();
  await expect(page.getByText(/Ley N° 21\.719/i).first()).toBeVisible();
  await expect(page.getByRole("link", { name: /Sign in to manage privacy rights/i })).toBeVisible();

  // 2. Register and sign in.
  const email = `privacy-e2e-${Date.now()}@example.com`;
  const password = "correct-horse-battery-staple";
  const reg = await request.post(`${API}/api/v1/auth/register`, {
    data: { email, password, organization_name: "Privacy E2E Org" },
  });
  expect(reg.status()).toBe(201);

  await page.goto("/login?next=/privacy");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/privacy$/);

  // 3. Authenticated workspace privacy posture renders.
  await expect(page.getByRole("heading", { name: /Workspace Privacy Posture/i })).toBeVisible();
  await expect(page.getByRole("heading", { name: /Self-Service ARCOP Controls/i })).toBeVisible();

  // 4. Download personal data (JSON export) triggers a download and displays summary.
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: /Download my data \(JSON\)/i }).click(),
  ]);
  expect(download.suggestedFilename()).toMatch(/^docmind-privacy-export-\d{4}-\d{2}-\d{2}\.json$/);
  await expect(page.getByRole("status")).toContainText(/Exported JSON \(cl-21719-v1\)/i);

  // 5. Erase personal history requires explicit confirmation and completes cleanly.
  await page.getByRole("button", { name: /Erase my query & conversation history/i }).click();
  await page.getByRole("button", { name: /Confirm permanent erasure/i }).click();
  await expect(page.getByRole("status")).toContainText(/Erased \d+ query history entries/i);
});
