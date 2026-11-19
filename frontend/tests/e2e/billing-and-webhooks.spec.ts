import { expect, test } from "@playwright/test";

const API = process.env.E2E_API_URL ?? "http://localhost:8000";
const password = "correct-horse-battery-staple";

test("owner can manage billing plans, sandbox checkout, and project-scoped webhooks", async ({ page }) => {
  const email = `billing-e2e-${Date.now()}@example.com`;
  await page.request.post(`${API}/api/v1/auth/register`, {
    data: { email, password, organization_name: "Billing E2E" },
  });

  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);

  await page.goto("/billing");
  await expect(page.getByRole("heading", { name: "SaaS plans & consumption" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Available commercial plans (configurable)" })).toBeVisible();

  await page.getByRole("button", { name: "Select Starter" }).click();
  await expect(page.getByRole("status")).toContainText("Workspace plan updated to STARTER");

  const starterCard = page.locator("article", { hasText: "Starter" });
  await starterCard.getByRole("button", { name: "Checkout" }).click();
  await expect(page.getByRole("status")).toContainText("Checkout session created");
  await expect(page.getByRole("heading", { name: "Transactions & payment history" })).toBeVisible();

  await page.goto("/documents");
  await page.getByLabel("New project").fill("Webhook Scope Alpha");
  await page.getByRole("button", { name: "Create project" }).click();
  await expect(page.getByRole("status")).toHaveText("Project created.");

  await page.goto("/webhooks");
  await expect(page.getByRole("heading", { name: "Webhooks", exact: true })).toBeVisible();
  await page.getByLabel("Endpoint URL").fill("https://1.1.1.1/docmind-e2e");
  await page.getByLabel("Description (optional)").fill("Project-scoped E2E hook");
  await expect(page.getByLabel("Webhook Scope Alpha")).toBeVisible();
  await page.getByLabel("Webhook Scope Alpha").check();
  await page.getByRole("button", { name: "Create webhook" }).click();

  await expect(page.getByRole("status")).toContainText("Webhook created. Copy the signing secret now");
  await expect(page.getByText("https://1.1.1.1/docmind-e2e")).toBeVisible();
  await expect(page.getByText("Projects: 1")).toBeVisible();

  await page.getByRole("button", { name: "Delete" }).first().click();
  await expect(page.getByRole("status")).toHaveText("Webhook deleted.");
});
