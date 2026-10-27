import { expect, test } from "@playwright/test";

const API = process.env.E2E_API_URL ?? "http://localhost:8000";

// A blank one-page PDF: the mock extraction returns values that do not occur in it, so the page must say
// so in words, not only with a colour, and must not present the scores as calibrated probabilities.
const pdf = Buffer.from(
  "JVBERi0xLjMKJeLjz9MKMSAwIG9iago8PAovUHJvZHVjZXIgKHB5cGRmKQo+PgplbmRvYmoKMiAwIG9iago8PAovVHlwZSAvUGFnZXMKL0NvdW50IDEKL0tpZHMgWyA0IDAgUiBdCj4+CmVuZG9iagozIDAgb2JqCjw8Ci9UeXBlIC9DYXRhbG9nCi9QYWdlcyAyIDAgUgo+PgplbmRvYmoKNCAwIG9iago8PAovVHlwZSAvUGFnZQovUmVzb3VyY2VzIDw8Cj4+Ci9NZWRpYUJveCBbIDAuMCAwLjAgNjEyIDc5MiBdCi9QYXJlbnQgMiAwIFIKPj4KZW5kb2JqCnhyZWYKMCA1CjAwMDAwMDAwMDAgNjU1MzUgZiAKMDAwMDAwMDAxNSAwMDAwMCBuIAowMDAwMDAwMDU0IDAwMDAwIG4gCjAwMDAwMDAxMTMgMDAwMDAgbiAKMDAwMDAwMDE2MiAwMDAwMCBuIAp0cmFpbGVyCjw8Ci9TaXplIDUKL1Jvb3QgMyAwIFIKL0luZm8gMSAwIFIKPj4Kc3RhcnR4cmVmCjI1NgolJUVPRgo=",
  "base64",
);

test("extracted values show their verification status and an uncalibrated-score warning", async ({ page, request }) => {
  const email = `evidence-e2e-${Date.now()}@example.com`;
  const password = "correct-horse-battery-staple";
  const registered = await request.post(`${API}/api/v1/auth/register`, {
    data: { email, password, organization_name: "Evidence E2E" },
  });
  const account = await registered.json();
  const project = await request.post(
    `${API}/api/v1/projects?organization_id=${account.organization_id}&name=E2E`,
    { headers: { Authorization: `Bearer ${account.access_token}` } },
  );
  expect(project.status()).toBe(201);

  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  await page.goto("/documents");
  await page.waitForFunction(() => {
    const select = document.querySelector("select");
    return !!select && Array.from(select.options).some((option) => option.textContent?.includes("E2E"));
  });
  await page.locator("select").first().selectOption({ label: "E2E" });
  await page.getByLabel("PDF").setInputFiles({ name: "evidence.pdf", mimeType: "application/pdf", buffer: pdf });
  await page.getByRole("button", { name: "Upload and process" }).click();
  await page.getByRole("link", { name: "evidence.pdf" }).click();

  await expect(page.getByText("customer_name", { exact: true })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText("Not verified automatically").first()).toBeVisible();
  await expect(page.getByText("The value could not be found in the document text").first()).toBeVisible();
  await expect(page.getByRole("status").filter({ hasText: "Not found in the document text" }).first()).toBeVisible();
  await expect(page.getByText(/not calibrated probabilities/)).toBeVisible();
});
