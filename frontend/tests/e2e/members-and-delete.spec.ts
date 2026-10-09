import { expect, test } from "@playwright/test";

import { pdf } from "./fixtures";

const API = process.env.E2E_API_URL ?? "http://localhost:8000";
const password = "correct-horse-battery-staple";

test("a user can create a project, upload, see status and delete a document", async ({ page }) => {
  const email = `delete-e2e-${Date.now()}@example.com`;
  await page.request.post(`${API}/api/v1/auth/register`, {
    data: { email, password, organization_name: "Delete E2E" },
  });
  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);

  await page.goto("/documents");
  await page.getByLabel("New project").fill("Created in UI");
  await page.getByRole("button", { name: "Create project" }).click();
  await expect(page.getByRole("status")).toHaveText("Project created.");

  await page.getByLabel("PDF").setInputFiles({ name: "ui-upload.pdf", mimeType: "application/pdf", buffer: pdf });
  await page.getByRole("button", { name: "Upload and process" }).click();
  await expect(page.getByRole("link", { name: "ui-upload.pdf" })).toBeVisible();

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Delete ui-upload.pdf" }).click();
  await expect(page.getByRole("link", { name: "ui-upload.pdf" })).toHaveCount(0);
});

test("signing out ends the session", async ({ page }) => {
  const email = `signout-e2e-${Date.now()}@example.com`;
  await page.request.post(`${API}/api/v1/auth/register`, {
    data: { email, password, organization_name: "Signout E2E" },
  });
  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login$/);
  await page.goto("/dashboard");
  await expect(page).toHaveURL(/\/login$/);
});
